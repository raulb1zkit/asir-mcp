#!/usr/bin/env python3
"""Importación local y acotada de recursos visibles de un curso Moodle."""

import argparse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COURSE = "https://moodle.example.org/course/view.php?id=123"
MODULES = {"resource", "folder", "page", "book", "assign"}
SAFE_QUERY = {"id", "section", "chapterid", "forceview", "redirect", "forcedownload"}


class ImportFailure(Exception):
    """Mensaje controlado; nunca contiene cabeceras ni respuestas del servidor."""


class SessionExpired(ImportFailure):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    parent: object = None
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {"script", "style", "form", "nav", "noscript"}:
            return ""
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children)


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        current = self.current
        while current.parent:
            if current.tag == tag:
                self.current = current.parent
                return
            current = current.parent

    def handle_data(self, data):
        self.current.children.append(data)

    def main(self):
        nodes = list(self.root.walk())
        for node in nodes:
            if node.attrs.get("id") == "region-main":
                return node
        return next((n for n in nodes if n.tag == "main" or n.attrs.get("role") == "main"), self.root)


def compact(text):
    return " ".join(text.split())


def canonical(url):
    """Retiene únicamente parámetros necesarios para leer recursos."""
    parsed = urlsplit(url)
    keys = {"id", "chapterid"} if re.fullmatch(r"/mod/[a-z]+/view\.php", parsed.path) else SAFE_QUERY
    query = sorted((k, v) for k, v in parse_qsl(parsed.query) if k in keys)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def same_origin(url, course):
    a, b = urlsplit(url), urlsplit(course)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port) and not a.username and not a.password


def kind(url, course):
    if not same_origin(url, course):
        return None
    path = urlsplit(url).path
    query = dict(parse_qsl(urlsplit(url).query))
    course_id = dict(parse_qsl(urlsplit(course).query)).get("id")
    if path == "/course/view.php" and query.get("id") == course_id:
        return "course"
    if path == "/course/section.php" and query.get("id", "").isdigit():
        return "section"
    match = re.fullmatch(r"/mod/([a-z]+)/view\.php", path)
    if match and query.get("id", "").isdigit():
        return match[1] if match[1] in MODULES else "unsupported"
    # Solo archivos publicados como contenido educativo; evita entregas y perfiles.
    if re.match(r"^/pluginfile\.php/\d+/(mod_resource/content|mod_folder/content|mod_page/content|mod_book/chapter|mod_assign/introattachment|mod_assign/intro|course/section)/", unquote(path)):
        return "file"
    return None


def is_login(url, doc):
    if "/login/" in urlsplit(url).path or "/auth/" in urlsplit(url).path:
        return True
    return any(n.tag == "input" and n.attrs.get("type", "").lower() == "password" for n in doc.root.walk())


def section_title(node):
    current = node
    candidates = []
    while current:
        if current.attrs.get("data-sectionname"):
            return compact(current.attrs["data-sectionname"])[:200]
        classes = current.attrs.get("class", "").split()
        if "section" in classes or "course-section" in classes or current.attrs.get("id", "").startswith("section-"):
            headings = [n for n in current.walk() if n.tag in {"h2", "h3"}]
            heading = next((n for n in headings if "sectionname" in n.attrs.get("class", "").split()), next(iter(headings), None))
            if heading:
                candidates.append(compact(heading.text())[:200])
        current = current.parent
    return candidates[0] if candidates else ""


def links(doc, base, course, root=None):
    for node in (root or doc.main()).walk():
        raw = node.attrs.get("href") if node.tag == "a" else node.attrs.get("data") if node.tag == "object" else node.attrs.get("src") if node.tag in {"iframe", "embed", "img"} else None
        if not raw:
            continue
        absolute = urljoin(base, raw)
        # Rechaza links de acción o firmados en vez de eliminar su secreto y seguirlos.
        if any(k.lower() not in SAFE_QUERY for k, _ in parse_qsl(urlsplit(absolute).query)):
            continue
        target = canonical(absolute)
        category = kind(target, course)
        if category:
            title = compact(node.text()) or unquote(Path(urlsplit(target).path).name)
            yield {"url": target, "kind": category, "title": title[:300], "section": section_title(node)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, course, cookie, delay=1.0, max_bytes=64 * 1024 * 1024):
        self.course, self.cookie = course, cookie
        self.delay, self.max_bytes = delay, max_bytes
        self.opener = build_opener(NoRedirect())
        self.last_request = 0.0
        self.requests = 0

    def get(self, url):
        for _ in range(8):
            if not same_origin(url, self.course):
                raise ImportFailure("Redirección a otro origen bloqueada; la cookie no se ha enviado.")
            if "/login/" in urlsplit(url).path or "/auth/" in urlsplit(url).path:
                raise SessionExpired("Sesión ausente o caducada; renueva la cookie local.")
            if kind(canonical(url), self.course) not in {"course", "section", "resource", "folder", "page", "book", "assign", "file"}:
                raise ImportFailure("Redirección fuera de los recursos permitidos.")
            if any(k not in SAFE_QUERY for k, _ in parse_qsl(urlsplit(url).query)):
                raise ImportFailure("Redirección con parámetros no permitidos.")
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            req = Request(url, headers={"Cookie": self.cookie, "User-Agent": "MCP-ASIR-course-import/0.1", "Accept-Encoding": "identity"})
            self.requests += 1
            self.last_request = time.monotonic()
            try:
                response = self.opener.open(req, timeout=30)
            except HTTPError as error:
                if error.code in {301, 302, 303, 307, 308}:
                    location = error.headers.get("Location")
                    error.close()
                    if not location:
                        raise ImportFailure("Redirección sin destino.") from None
                    url = urljoin(url, location)
                    continue
                code = error.code
                error.close()
                if code == 401:
                    raise SessionExpired("Moodle ha rechazado la sesión.") from None
                raise ImportFailure(f"Moodle devolvió HTTP {code}; no se reintenta automáticamente.") from None
            except (URLError, TimeoutError, OSError):
                raise ImportFailure("Fallo de red/TLS o tiempo de espera agotado.") from None
            with response:
                content = response.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise ImportFailure("Recurso superior al límite de tamaño configurado.")
                mime = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                filename = response.headers.get_filename()
                # Moodle puede devolver HTML de login con HTTP 200, incluso al pedir un PDF.
                prefix = content[:512].lstrip().lower()
                html = mime == "text/html" or prefix.startswith((b"<!doctype html", b"<html"))
                doc = Document(content.decode(charset, errors="replace")) if html else None
                if doc and is_login(url, doc):
                    raise SessionExpired("Moodle devolvió el formulario de login; renueva la cookie.")
                return {"url": canonical(url), "content": content, "mime": mime, "filename": filename, "doc": doc}
        raise ImportFailure("Demasiadas redirecciones.")


def load_cookie(path):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ImportFailure("La sesión debe ser un archivo privado (chmod 600) y no un enlace.")
    value = json.loads(path.read_text(encoding="utf-8"))
    name, secret = value.get("name", ""), value.get("value", "")
    if not isinstance(name, str) or not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(r"[A-Za-z0-9%._~+/-]+", secret):
        raise ImportFailure("Formato de cookie no válido.")
    return f"{name}={secret}"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def store(output, content):
    digest = hashlib.sha256(content).hexdigest()
    relative = Path("objects") / digest[:2] / digest
    dest = output / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(dest)
    return {"sha256": digest, "path": relative.as_posix(), "bytes": len(content)}


def page_text(doc, category):
    main = doc.main()
    nodes = list(main.walk())
    if category == "assign":
        content = next((n for n in nodes if n.attrs.get("id") == "intro"), None)
    elif category == "page":
        content = next((n for n in nodes if "generalbox" in n.attrs.get("class", "").split()), None)
    else:
        content = next((n for n in nodes if "book_content" in n.attrs.get("class", "").split()), None)
    if content is None:
        raise ImportFailure("Estructura de contenido no reconocida; requiere revisar el HTML del curso.")
    return (compact(content.text()) + "\n").encode("utf-8")


def run(args, client):
    output = args.output
    old_path = output / "manifest.json"
    if getattr(args, "resume", False) and not old_path.exists() and (output / "last-run.partial.json").exists():
        old_path = output / "last-run.partial.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {}
    if old and old.get("course_url") != args.course_url:
        raise ImportFailure("El directorio contiene otro curso; utiliza otro --output.")
    previous = {item["url"]: item.get("sha256") for item in old.get("items", [])}
    previous_items = {item["url"]: item for item in old.get("items", [])}
    subject = getattr(args, "subject", "ASO")
    excluded = set(getattr(args, "exclude_url", []))
    report = {"version": 1, "course_url": args.course_url, "subject": subject, "started_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode, "complete": False, "items": [], "issues": []}
    queue = deque([{"url": args.course_url, "kind": "course", "title": subject, "section": ""}])
    seen = set()
    try:
        while queue:
            item = queue.popleft()
            if item["url"] in seen:
                continue
            if len(seen) >= args.max_resources:
                raise ImportFailure("Límite de recursos alcanzado; el informe queda parcial.")
            seen.add(item["url"])
            if item["url"] in excluded:
                report["items"].append({**item, "status": "excluded"})
                continue
            if item["kind"] == "unsupported":
                report["items"].append({**item, "status": "unsupported"})
                continue
            if args.mode == "inventory" and item["kind"] not in {"course", "section"}:
                report["items"].append({**item, "status": "discovered"})
                continue
            cached = previous_items.get(item["url"], {})
            if getattr(args, "resume", False) and args.mode == "download" and item["kind"] in {"resource", "file"} and cached.get("sha256"):
                local = (output / cached["path"]).resolve()
                if local.is_relative_to(output.resolve()) and local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == cached["sha256"]:
                    report["items"].append({**cached, "section": item["section"], "change": "cached"})
                    continue
            try:
                response = client.get(item["url"])
                doc = response["doc"]
                # Un HTML servido desde pluginfile es un archivo de clase, no
                # una página de navegación Moodle: no sigas enlaces de su código.
                if item["kind"] == "file" and urlsplit(response["url"]).path.startswith("/pluginfile.php/"):
                    doc = None
                if doc:
                    content_root = None
                    if item["kind"] == "assign":
                        content_root = next((n for n in doc.main().walk() if n.attrs.get("id") == "intro"), None)
                        if content_root is None:
                            raise ImportFailure("Tarea sin bloque de enunciado reconocido; requiere revisión.")
                    found = list(links(doc, response["url"], args.course_url, root=content_root))
                    for target in found:
                        target["section"] = (target["section"] or item["section"]) if item["kind"] in {"course", "section"} else item["section"]
                        if target["kind"] == "file" and item["kind"] in {"resource", "folder", "page", "book", "assign"}:
                            target["title"] = item["title"]
                        queue.append(target)
                    if item["kind"] in {"page", "book", "assign"}:
                        content = page_text(doc, item["kind"])
                        saved = {**store(output, content), "mime": "text/plain", "filename": item["kind"] + ".txt", "representation": "extracted-text"}
                    elif item["kind"] in {"resource", "file"} and not any(n["kind"] == "file" for n in found):
                        raise ImportFailure("Recurso HTML sin enlace de archivo reconocido; requiere revisión.")
                    else:
                        saved = {}
                else:
                    if item["kind"] in {"course", "section"}:
                        raise ImportFailure("La página del curso no devolvió HTML.")
                    filename = response["filename"] or unquote(Path(urlsplit(response["url"]).path).name) or "resource"
                    filename = re.sub(r"[^\w. -]", "_", filename.replace("\\", "/").split("/")[-1])[:180]
                    saved = {**store(output, response["content"]), "mime": response["mime"], "filename": filename, "representation": "original"}
                entry = {**item, **saved, "status": "downloaded" if saved else "scanned"}
                if saved:
                    entry["change"] = "unchanged" if previous.get(item["url"]) == saved["sha256"] else "updated" if previous.get(item["url"]) else "new"
                report["items"].append(entry)
            except SessionExpired:
                raise
            except ImportFailure as error:
                if item["kind"] == "course":
                    raise
                report["issues"].append({**item, "error": str(error)})
        report["complete"] = True
    finally:
        report["requests"] = client.requests
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        # Los errores de autenticación o los límites nunca sustituyen el último inventario completo.
        name = "inventory.json" if args.mode == "inventory" else "manifest.json"
        if not report["complete"] or report["issues"]:
            name = "last-run.partial.json"
        write_json(output / name, report)
    counts = {status: sum(i["status"] == status for i in report["items"]) for status in {"downloaded", "scanned", "discovered", "unsupported"}}
    print(json.dumps({"resources": counts, "errors": len(report["issues"]), "requests": client.requests, "report": str(output / name)}, ensure_ascii=False))
    return 2 if report["issues"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["inventory", "download"])
    parser.add_argument("--course-url", default=DEFAULT_COURSE)
    parser.add_argument("--subject", default="ASO", help="Nombre de la asignatura para el manifiesto.")
    parser.add_argument("--exclude-url", action="append", default=[], help="URL canónica de recurso que no se debe visitar; repetible.")
    parser.add_argument("--session-file", type=Path, default=ROOT / ".private/moodle-session.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/aso/moodle")
    parser.add_argument("--delay", type=float, default=1.0, help="Segundos entre peticiones (mínimo 0.5).")
    parser.add_argument("--max-resources", type=int, default=500)
    parser.add_argument("--max-mb", type=int, default=64)
    parser.add_argument("--resume", action="store_true", help="Reutiliza objetos verificados del manifiesto previo; no comprueba cambios remotos en ellos.")
    args = parser.parse_args()
    try:
        if not args.course_url.startswith("https://") or kind(args.course_url, args.course_url) != "course" or args.course_url != canonical(args.course_url):
            raise ImportFailure("Usa una URL HTTPS normal de course/view.php?id=..., sin parámetros secretos.")
        if args.delay < 0.5 or args.max_resources < 1 or args.max_mb < 1:
            raise ImportFailure("Límites no válidos.")
        cookie = load_cookie(args.session_file)
        return run(args, Client(args.course_url, cookie, args.delay, args.max_mb * 1024 * 1024))
    except FileNotFoundError:
        print("Falta un archivo local; prepara la sesión con python scripts/moodle_session.py.", file=sys.stderr)
    except (ImportFailure, ValueError, RecursionError, socket.timeout) as error:
        message = str(error) if isinstance(error, ImportFailure) else "Datos o respuesta no válidos; revisa el informe local."
        print(message, file=sys.stderr)
    except OSError:
        print("No se ha podido leer o escribir un archivo local.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
