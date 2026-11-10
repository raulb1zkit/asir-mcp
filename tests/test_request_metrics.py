import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from request_metrics import RequestMetricsMiddleware, record


class MetricsTests(unittest.TestCase):
    def test_daily_counts_and_madrid_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'metrics.sqlite'
            now = datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)
            record(path, 200, now); record(path, 401, now); record(path, 200, now)
            with sqlite3.connect(path) as db:
                rows = db.execute('SELECT day, status_class, requests FROM daily_requests ORDER BY status_class').fetchall()
            self.assertEqual(rows, [('2026-11-10','2xx',2), ('2026-11-10','4xx',1)])

    def test_counts_mcp_only_without_recording_headers_or_body(self):
        async def run(path):
            async def app(scope, receive, send):
                await send({'type': 'http.response.start', 'status': 401, 'headers': []})
            async def send(message): pass
            async def receive(): return {'type':'http.request','body':b'secret'}
            wrapped = RequestMetricsMiddleware(app, path)
            await wrapped({'type':'http','method':'POST','path':'/mcp','headers':[(b'authorization', b'secret')]}, receive, send)
            await wrapped({'type':'http','method':'GET','path':'/healthz'}, receive, send)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'metrics.sqlite'
            asyncio.run(run(path))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT status_class, requests FROM daily_requests').fetchall(), [('4xx',1)])
            self.assertNotIn(b'secret',path.read_bytes())

if __name__ == '__main__': unittest.main()
