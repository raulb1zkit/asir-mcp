#!/usr/bin/env python3
"""Panel local de solo lectura para los informes del sincronizador."""
import argparse
import json
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def snapshot(state_dir, metrics_db):
    courses = json.loads((ROOT / 'config/courses.json').read_text(encoding='utf-8'))['courses']
    result = {'courses': [{'slug': c['slug'], 'subject': c['subject'], 'exclusions': len(c.get('exclusions', [])), 'preserved': [p['name'] for p in c.get('preserve_sources', [])]} for c in courses], 'sync': None, 'metrics': None, 'errors': []}
    try:
        status = json.loads((state_dir / 'status.json').read_text(encoding='utf-8'))
        # No se sirven rutas, mensajes de error, documentos ni credenciales.
        allowed = ('complete', 'started_at', 'finished_at', 'step', 'publication', 'successful_date', 'published_date', 'staged_date', 'built_date', 'new_windows', 'incremental', 'vectors')
        result['sync'] = {key: status[key] for key in allowed if key in status}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        result['errors'].append('No se pudo leer el estado de sincronización.')
    try:
        if metrics_db.is_file():
            with sqlite3.connect(metrics_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
                rows = db.execute('SELECT status_class, requests FROM daily_requests WHERE day = ? ORDER BY status_class', (datetime.now(ZoneInfo('Europe/Madrid')).date().isoformat(),)).fetchall()
            result['metrics'] = {key: value for key, value in rows}
    except (OSError, sqlite3.Error):
        result['errors'].append('No se pudieron leer las métricas MCP.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'data/sync')
    parser.add_argument('--metrics-db', type=Path, default=Path('/var/lib/asir/request-metrics.sqlite'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--snapshot', type=Path, help='Resumen previamente exportado, sin acceso directo a los datos privados.')
    parser.add_argument('--resources', type=Path, default=Path('/var/lib/asir-dashboard/resources.json'),
                        help='JSON sanitizado del monitor de recursos; solo lectura y sin acceso a Docker ni a métricas privadas.')
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/status':
                try:
                    content = args.snapshot.read_bytes() if args.snapshot else json.dumps(snapshot(args.state_dir, args.metrics_db), ensure_ascii=False).encode()
                except OSError:
                    self.send_error(503, 'Resumen no disponible')
                    return
                content_type = 'application/json; charset=utf-8'
            elif self.path == '/api/resources':
                # Solo se sirve el archivo ya saneado por el monitor; el panel
                # no accede a SQLite, al socket de Docker ni a datos privados.
                try:
                    content = args.resources.read_bytes()
                except OSError:
                    self.send_error(503, 'Recursos no disponibles')
                    return
                content_type = 'application/json; charset=utf-8'
            elif self.path in ('/', '/index.html'):
                content = (ROOT / 'dashboard/index.html').read_bytes()
                content_type = 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(content)

    print(f'Panel local: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
