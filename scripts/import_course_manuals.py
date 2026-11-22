#!/usr/bin/env python3
"""Importa manuales públicos enlazados desde un inventario de Moodle, sin enviarles la cookie."""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from moodle_download import Document, NoRedirect, compact, is_login, load_cookie, store, write_json

MOODLE = os.environ.get('ASIR_MOODLE_ORIGIN_HOST', 'moodle.example.edu')
MANUALES_HOST = os.environ.get('ASIR_MANUAL_HOST', 'manuais.example.edu')
MANUALES_PAGES_HOST = os.environ.get('ASIR_MANUAL_PAGES_HOST', 'manuais.pages.example.edu')
HOSTS = {MANUALES_HOST, MANUALES_PAGES_HOST}


def manual_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443) or p.query:
        return None
    if p.scheme == 'http' and p.hostname != MANUALES_HOST:
        return None
    if p.hostname == MANUALES_HOST and not p.path.startswith('/index.php/'):
        return None
    if p.hostname == MANUALES_PAGES_HOST and not p.path.endswith('/index.html'):
        return None
    return urlunsplit(('https', p.hostname, p.path, '', ''))


def request(opener, url, headers):
    try:
        r = opener.open(Request(url, headers=headers), timeout=30)
    except HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get('Location', '')
            e.close()
            return None, urljoin(url, location)
        code = e.code
        e.close()
        raise ValueError(f'HTTP {code}') from None
    with r:
        content = r.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError('respuesta demasiado grande')
        return content, None


def content_node(doc):
    nodes = list(doc.root.walk())
    return next((n for n in nodes if n.tag == 'article'), None) or next((n for n in nodes if n.attrs.get('id') == 'mw-content-text'), None)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory', type=Path, default=Path('data/sri/moodle/inventory.json'))
    p.add_argument('--session-file', type=Path, default=Path('.private/moodle-session.json'))
    p.add_argument('--output', type=Path, default=Path('data/sri/manuals'))
    args = p.parse_args()
    inventory = json.loads(args.inventory.read_text())
    cookie = load_cookie(args.session_file)
    opener = build_opener(NoRedirect())
    result = {'version': 1, 'course_url': inventory['course_url'], 'subject': inventory['subject'], 'complete': False, 'items': [], 'issues': [], 'started_at': datetime.now(timezone.utc).isoformat()}
    queue = deque()
    for item in inventory['items']:
        u = urlsplit(item['url'])
        if item['kind'] != 'unsupported' or u.scheme != 'https' or u.hostname != MOODLE or not re.fullmatch(r'/mod/url/view\.php', u.path):
            continue
        try:
            time.sleep(1)
            body, redirect = request(opener, item['url'], {'Cookie': cookie, 'User-Agent': 'MCP-ASIR-course-import/0.1'})
            if (redirect and '/login/' in urlsplit(redirect).path) or (body and is_login(item['url'], Document(body.decode('utf-8', 'replace')))):
                raise ValueError('sesión Moodle caducada')
            targets = [redirect] if redirect else [urljoin(item['url'], n.attrs.get('href', '')) for n in Document(body.decode('utf-8', 'replace')).main().walk() if n.tag == 'a']
            targets = list(dict.fromkeys(filter(None, (manual_url(t) for t in targets if t))))
            for target in targets:
                queue.append((target, item, target))
            if not targets:
                result['items'].append({**item, 'status': 'unsupported_external'})
        except (ValueError, URLError, OSError) as e:
            result['issues'].append({'url': item['url'], 'error': type(e).__name__})
    seen = set()
    while queue:
        url, source, root = queue.popleft()
        if url in seen:
            continue
        if len(seen) >= 300:
            result['issues'].append({'url': url, 'error': 'límite de páginas'}); break
        seen.add(url)
        try:
            time.sleep(1)
            body, redirect = request(opener, url, {'User-Agent': 'MCP-ASIR-course-import/0.1'})
            if redirect:
                dest = manual_url(redirect)
                if not dest:
                    raise ValueError('redirección fuera del alcance')
                queue.append((dest, source, root)); continue
            doc = Document(body.decode('utf-8', 'replace'))
            node = content_node(doc)
            if node is None:
                raise ValueError('contenido no reconocido')
            text = compact(node.text())
            if not text:
                raise ValueError('contenido vacío')
            saved = store(args.output, (text + '\n').encode())
            result['items'].append({'url': url, 'source_url': source['url'], 'kind': 'manual', 'title': source['title'], 'section': source['section'], 'status': 'downloaded', 'mime': 'text/plain', 'filename': 'manual.txt', 'representation': 'extracted-text', **saved})
            # Solo subpáginas del manual enlazado, no el menú completo del centro.
            if urlsplit(root).hostname == MANUALES_PAGES_HOST:
                prefix = root.removesuffix('index.html')
                for n in doc.root.walk():
                    if n.tag == 'a' and n.attrs.get('href'):
                        child = manual_url(urljoin(url, n.attrs['href']))
                        if child and child.startswith(prefix):
                            queue.append((child, source, root))
        except (ValueError, URLError, OSError) as e:
            result['issues'].append({'url': url, 'error': str(e) if isinstance(e, ValueError) else type(e).__name__})
    result['complete'] = not result['issues']
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    name = 'manifest.json' if result['complete'] else 'last-run.partial.json'
    write_json(args.output / name, result)
    print(json.dumps({'manuals': len(seen), 'downloaded': sum(i['status'] == 'downloaded' for i in result['items']), 'issues': len(result['issues']), 'report': str(args.output / name)}))
    return 0 if result['complete'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
