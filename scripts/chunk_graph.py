#!/usr/bin/env python3
"""Grafo local de un snapshot coherente del corpus servido en PostgreSQL."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def graph_from_rows(corpus_id, chunks, citations):
    nodes = {r['id']: {'id': r['id'], 'kind': 'chunk', 'label': r['text'][:90],
             'text': r['text'], 'review': r['requires_review'], 'warnings': r['warnings'],
             'subjects': [], 'sources': []} for r in chunks}
    links = set()
    for r in citations:
        chunk = nodes.get(r['chunk_id'])
        if chunk is None:
            raise ValueError('citation_without_chunk')
        subject = 'subject:' + r['subject']
        source = 'source:' + r['subject'] + ':' + r['sha256']
        nodes.setdefault(subject, {'id': subject, 'kind': 'subject', 'label': r['subject'], 'subjects': [r['subject']]})
        nodes.setdefault(source, {'id': source, 'kind': 'source', 'label': r['title'] or r['filename'], 'subjects': [r['subject']]})
        if r['subject'] not in chunk['subjects']:
            chunk['subjects'].append(r['subject'])
        if source not in chunk['sources']:
            chunk['sources'].append(source)
        links.add((subject, source, 'contains'))
        links.add((source, r['chunk_id'], 'contains'))
        for key in ('previous_chunk_id', 'next_chunk_id'):
            other = r.get(key)
            if other and other in nodes and other != r['chunk_id']:
                a, b = sorted((other, r['chunk_id']))
                links.add((a, b, 'adjacent'))
    return {'corpus_id': corpus_id, 'captured_at': datetime.now(timezone.utc).isoformat(),
            'nodes': list(nodes.values()), 'links': [{'source': a, 'target': b, 'kind': k} for a,b,k in sorted(links)],
            'counts': dict(Counter(n['kind'] for n in nodes.values()))}


def load_database(config_path):
    try:
        from .supabase_db import connect, service_config
    except ImportError:
        from supabase_db import connect, service_config
    with connect(service_config(config_path), application_name='asir-local-graph') as connection:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute('SELECT corpus_sha256 FROM asir.corpus WHERE id=1')
            corpus = cursor.fetchone()['corpus_sha256']
            cursor.execute('SELECT id, text, requires_review, warnings FROM asir.chunks ORDER BY ordinal')
            chunks = cursor.fetchall()
            cursor.execute('SELECT ci.chunk_id, ci.previous_chunk_id, ci.next_chunk_id, s.sha256, s.subject, s.title, s.filename FROM asir.citations ci JOIN asir.citation_sources s ON s.citation_id=ci.id ORDER BY ci.id')
            citations = cursor.fetchall()
        connection.rollback()
    return graph_from_rows(corpus, chunks, citations)


def make_handler(graph):
    payload = json.dumps(graph, ensure_ascii=False).encode()
    assets = {'/': ('text/html; charset=utf-8', 'index.html'), '/app.js': ('text/javascript; charset=utf-8', 'app.js'), '/style.css': ('text/css; charset=utf-8', 'style.css')}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                self.send_error(403); return
            path = urlsplit(self.path).path
            if path == '/api/graph':
                body, mime = payload, 'application/json; charset=utf-8'
            elif path in assets:
                mime, filename = assets[path]
                body = (ROOT / 'graph' / filename).read_bytes()
            else:
                self.send_error(404); return
            self.send_response(200)
            for key, value in {'Content-Type': mime, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
                self.send_header(key, value)
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / '.private/asir-service-connection.json')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    try:
        graph = load_database(args.config)
    except Exception:
        parser.exit(1, 'No se pudo leer el corpus: comprueba conexión de lectura, TLS y red. No se muestran detalles sensibles.\n')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(graph))
    print(f"Grafo local: http://127.0.0.1:{server.server_port} · {graph['counts'].get('chunk',0)} chunks", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
