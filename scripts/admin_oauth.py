#!/usr/bin/env python3
"""Panel temporal de usuarios OAuth, exclusivamente por túnel SSH."""

import argparse
import base64
import hmac
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
from urllib.parse import parse_qs

try:
    from .manage_oauth_users import load, update
    from .mcp_auth import TokenConfigError
except ImportError:
    from manage_oauth_users import load, update
    from mcp_auth import TokenConfigError


SUBJECTS = ("ASO", "Bases de Datos", "SAD", "IAW", "Digitalizacion", "SRI")


def make_handler(path, password, csrf, port):
    origin = f"http://localhost:{port}"
    credentials = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Nunca registrar credenciales ni identificadores.

        def reply(self, status, body, extra=()):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if self.headers.get("Host") != f"localhost:{port}":
                self.reply(400, "Host inválido")
                return False
            if not hmac.compare_digest(self.headers.get("Authorization", ""), credentials):
                self.reply(401, "Autenticación requerida", [("WWW-Authenticate", 'Basic realm="MCP-ASIR admin", charset="UTF-8"')])
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/":
                self.reply(404, "No encontrado")
                return
            try:
                users = load(path)["users"]
            except (OSError, TokenConfigError):
                self.reply(500, "No se puede leer la lista de usuarios")
                return
            esc = html.escape
            rows = "".join(f"<li><code>{esc(u['id'])}</code> — {esc(', '.join(u['subjects']))}</li>" for u in users) or "<li>Ninguno</li>"
            checks = "".join(f'<label><input type="checkbox" name="subject" value="{esc(s, quote=True)}"> {esc(s)}</label> ' for s in SUBJECTS)
            self.reply(200, f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Administrar MCP-ASIR</title><style>body{{font:16px system-ui;max-width:48rem;margin:3rem auto;padding:0 1rem;line-height:1.6}}label{{display:inline-block}}input[type=text]{{width:100%;max-width:30rem;padding:.5rem}}button{{padding:.6rem;margin:.5rem .4rem .5rem 0}}</style><h1>Acceso MCP-ASIR</h1><p>Comprueba el UUID en Supabase y confirma con la persona la cuenta utilizada. Conceder sustituye los permisos anteriores. Revocar retira todas las asignaturas.</p><h2>Usuarios autorizados</h2><ul>{rows}</ul><form method="post" action="/"><input type="hidden" name="csrf" value="{csrf}"><label for="user">UUID de Supabase</label><br><input id="user" type="text" name="user" required maxlength="36"><p>Asignaturas:</p>{checks}<p><button name="action" value="grant">Conceder / actualizar</button><button name="action" value="revoke">Revocar</button></p></form></html>''')

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/" or self.headers.get("Origin") != origin or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.reply(403, "Solicitud rechazada")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError()
                form = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=20)
                if not hmac.compare_digest(form.get("csrf", [""])[0], csrf):
                    raise ValueError()
                user = form.get("user", [""])[0]
                action = form.get("action", [""])[0]
                subjects = form.get("subject", [])
                if action == "grant" and subjects and set(subjects) <= set(SUBJECTS):
                    update(path, user, subjects)
                elif action == "revoke":
                    update(path, user)
                else:
                    raise ValueError()
            except (ValueError, UnicodeError, TokenConfigError, OSError):
                self.reply(400, 'No se ha aplicado el cambio. Comprueba el UUID y las asignaturas. <a href="/">Volver</a>')
                return
            self.reply(303, "Cambio aplicado", [("Location", "/")])

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="/etc/asir/oauth-users.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("puerto inválido")
    from pathlib import Path
    path = Path(args.file)
    load(path)  # Fallar antes de escuchar si la lista no es válida.
    password, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    server = HTTPServer(("127.0.0.1", args.port), make_handler(path, password, csrf, args.port))
    print(f"Panel: http://localhost:{args.port}/\nUsuario: admin\nContraseña temporal: {password}\nCierra con Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
