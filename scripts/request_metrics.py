#!/usr/bin/env python3
"""Contadores diarios de peticiones HTTP al transporte MCP, sin datos personales."""
import argparse
import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

MADRID = ZoneInfo('Europe/Madrid')


def record(path, status, now=None):
    day = (now or datetime.now(MADRID)).astimezone(MADRID).date()
    category = f'{status // 100}xx' if 100 <= status <= 599 else 'other'
    with sqlite3.connect(path, timeout=2) as db:
        db.execute('CREATE TABLE IF NOT EXISTS daily_requests (day TEXT NOT NULL, status_class TEXT NOT NULL, requests INTEGER NOT NULL, PRIMARY KEY(day, status_class))')
        db.execute('INSERT INTO daily_requests VALUES (?, ?, 1) ON CONFLICT(day, status_class) DO UPDATE SET requests = requests + 1', (day.isoformat(), category))
        db.execute('DELETE FROM daily_requests WHERE day < ?', ((day - timedelta(days=90)).isoformat(),))


class RequestMetricsMiddleware:
    def __init__(self, app, path):
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or scope.get('method') != 'POST' or scope.get('path') != '/mcp':
            await self.app(scope, receive, send)
            return
        status = 500
        async def capture(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)
        try:
            await self.app(scope, receive, capture)
        finally:
            try:
                await asyncio.to_thread(record, self.path, status)
            except Exception:
                logging.getLogger('asir.metrics').warning('request counter unavailable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('/var/lib/asir/request-metrics.sqlite'))
    parser.add_argument('--date', default=datetime.now(MADRID).date().isoformat(), help='Fecha en Europa/Madrid (AAAA-MM-DD)')
    args = parser.parse_args()
    from datetime import date
    date.fromisoformat(args.date)
    if not args.database.exists():
        print('Sin datos registrados.'); return
    with sqlite3.connect(f'file:{args.database}?mode=ro', uri=True) as db:
        rows = db.execute('SELECT status_class, requests FROM daily_requests WHERE day = ? ORDER BY status_class', (args.date,)).fetchall()
    print(f'Fecha: {args.date} (Europa/Madrid)')
    for category, count in rows:
        print(f'{category}: {count}')
    print(f'Total: {sum(count for _, count in rows)}')

if __name__ == '__main__':
    main()
