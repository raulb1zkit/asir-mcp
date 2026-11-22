"""Pruebas del monitor de recursos (scripts/server_monitor.py).

Usa fixtures sintéticos de /proc y un socket UNIX con respuestas de Docker
grabadas; no toca Docker, el VPS ni recursos reales.
"""
import json
import os
import socket
import sqlite3
import threading
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server_monitor as monitor


MADRID = ZoneInfo('Europe/Madrid')


def write_proc(proc, mem_total=4_000_000, mem_available=2_500_000, cpu_lines=None):
    (proc / 'meminfo').write_text(
        f'MemTotal:       {mem_total} kB\n'
        f'MemFree:         900000 kB\n'
        f'MemAvailable:    {mem_available} kB\n'
        f'SwapTotal:            0 kB\n'
        f'SwapFree:             0 kB\n', encoding='ascii')
    (proc / 'loadavg').write_text('0.10 0.20 0.30 1/100 500\n', encoding='ascii')
    if cpu_lines is None:
        cpu_lines = ['cpu  1000 0 500 9000 100 0 0 0 0 0', 'cpu0 1000 0 500 9000 100 0 0 0 0 0']
    (proc / 'stat').write_text('\n'.join(cpu_lines) + '\n', encoding='ascii')


def write_diskstats(proc, fs_path, read_sectors, write_sectors, io_ms):
    dev = os.stat(fs_path).st_dev
    (proc / 'diskstats').write_text(
        f'{os.major(dev)}   {os.minor(dev)} xvda1 100 0 {read_sectors} 50 200 0 {write_sectors} 60 0 {io_ms} 110\n'
        '7       0 loop0 0 0 0 0 0 0 0 0 0 0 0\n', encoding='ascii')


class FakeDocker(threading.Thread):
    """Socket UNIX que responde peticiones HTTP con cuerpos chunked fijos."""

    def __init__(self, responses):
        super().__init__(daemon=True)
        self.responses = responses
        self.requests = []
        self.path = None

    def __enter__(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        handle, self.path = tempfile.mkstemp(prefix='docker-fake-')
        os.close(handle)
        os.unlink(self.path)
        server.bind(self.path)
        server.listen(4)
        self.start()
        return self

    def __exit__(self, *args):
        self._server.close()

    def run(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            data = conn.recv(65536).decode('ascii', 'replace')
            path = data.split('\r\n', 1)[0].split(' ')[1] if data else ''
            self.requests.append(path)
            body = self.responses.get(path.split('?')[0])
            if body is None:
                conn.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n1a\r\n{"message": "not found"}\r\n0\r\n\r\n')
            else:
                payload = json.dumps(body).encode()
                framed = (f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n' + framed)
            conn.close()


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.proc = base / 'proc'
        self.fs = base / 'srv'
        self.proc.mkdir()
        self.fs.mkdir()
        self.db = base / 'metrics.sqlite'

    def tearDown(self):
        self.temp.cleanup()

    def sample(self, now, read_sectors=100, write_sectors=200, io_ms=500, cpu_lines=None,
               container_json=None, rag_status=None):
        write_proc(self.proc, cpu_lines=cpu_lines)
        write_diskstats(self.proc, self.fs, read_sectors, write_sectors, io_ms)
        db = monitor.open_db(self.db)
        try:
            return monitor.take_sample(
                db, now, proc_dir=self.proc, fs_path=str(self.fs),
                container_json=container_json, rag_status=rag_status)
        finally:
            db.close()

    def test_first_sample_unknown_percent_without_fabricating_zero(self):
        row = self.sample(1_800_000_000.0)
        self.assertIsNone(row['cpu_percent'])
        self.assertIsNone(row['io_util_percent'])
        self.assertEqual(row['mem_available'], 2_500_000 * 1024)
        self.assertEqual(row['load1'], 0.10)
        self.assertGreater(row['fs_free'], 0)

    def test_second_sample_derives_cpu_and_io_percent(self):
        self.sample(1_800_000_000.0)
        second = ['cpu  2000 0 700 10000 100 0 0 0 0 0', 'cpu0 1000 0 500 5000 50 0 0 0 0 0',
                  'cpu1 1000 0 200 5000 50 0 0 0 0 0']
        row = self.sample(1_800_000_060.0, read_sectors=700, write_sectors=2000, io_ms=1500, cpu_lines=second)
        # cpu: total 10600→12800 (delta 2200), idle 9100→10100 (delta 1000)
        self.assertAlmostEqual(row['cpu_percent'], (1 - 1000 / 2200) * 100, places=2)
        # io: 1000 ms ocupados en 60 s → 1,67 %
        self.assertAlmostEqual(row['io_util_percent'], 1000 / (60 * 1000) * 100, places=2)
        self.assertEqual(row['io_read_bytes'], 700 * monitor.SECTOR_BYTES)

    def test_missing_proc_reads_become_unknown_not_zero(self):
        write_proc(self.proc, cpu_lines=['incompleto'])
        (self.proc / 'loadavg').write_text('x', encoding='ascii')
        (self.proc / 'meminfo').write_text('incompleto', encoding='ascii')
        # sin diskstats el dispositivo no aparece
        (self.proc / 'diskstats').write_text('', encoding='ascii')
        db = monitor.open_db(self.db)
        try:
            row = monitor.take_sample(db, 1_800_000_000.0, proc_dir=self.proc, fs_path=str(self.fs))
        finally:
            db.close()
        self.assertIsNone(row['load1'])
        self.assertIsNone(row['mem_total'])
        self.assertIsNone(row['io_read_bytes'])
        self.assertIsNone(row['cpu_total'])

    def test_rag_status_optional_absent_is_unknown(self):
        row = self.sample(1_800_000_000.0, rag_status=self.proc / 'no-existe.json')
        self.assertIsNone(row['rag_version'])
        self.assertIsNone(row['rag_phase'])
        status = self.proc / 'rag.json'
        status.write_text(json.dumps({'version': 'v7', 'phase': 'unchanged', 'operation': 'op-1',
                                      'secret': 'no-debe-entrar', 'paths': ['/var/lib/x']}), encoding='utf-8')
        row = self.sample(1_800_000_061.0, rag_status=status)
        self.assertEqual(row['rag_version'], 'v7')
        self.assertEqual(row['rag_phase'], 'unchanged')
        self.assertIsNone(row['rag_backup'])
        # el campo no permitido no se guarda en ningún sitio
        with sqlite3.connect(self.db) as raw:
            blob = str(raw.execute('SELECT * FROM samples').fetchall())
        self.assertNotIn('no-debe-entrar', blob)
        self.assertNotIn('/var/lib/x', blob)

    def test_container_json_fresh_and_stale(self):
        path = self.proc / 'container.json'
        path.write_text(json.dumps({'available': True, 'collected_at': monitor._iso(1_800_000_010.0),
                                    'containers': [{'name': 'asir-mcp-mcp-1', 'state': 'running',
                                                    'health': 'healthy', 'oom_killed': False,
                                                    'mem_usage': 522_190_848, 'mem_limit': 2_147_483_648,
                                                    'cpu_percent': 0.2}]}), encoding='utf-8')
        row = self.sample(1_800_000_020.0, container_json=path)
        self.assertEqual(row['container_name'], 'asir-mcp-mcp-1')
        self.assertEqual(row['container_fresh'], 1)
        self.assertAlmostEqual(row['container_mem_usage'] / row['container_mem_limit'] * 100, 24.32, places=1)
        # caducado (> 600 s) → desconocido, no cero
        row = self.sample(1_800_000_700.0, container_json=path)
        self.assertEqual(row['container_fresh'], 0)
        self.assertIsNone(row['container_mem_usage'])
        self.assertIsNone(row['container_state'])
        path.write_text('{incompleto', encoding='utf-8')
        row = self.sample(1_800_001_000.0, container_json=path)
        self.assertIsNone(row['container_name'])

    def test_prune_respects_retention_boundary(self):
        self.sample(1_000_000_000.0)
        self.sample(1_000_000_000.0 + 29 * 86400)
        self.sample(1_000_000_000.0 + 30 * 86400 + 1)
        db = monitor.open_db(self.db)
        try:
            monitor.prune(db, now=1_000_000_000.0 + 30 * 86400 + 1)
            rows = db.execute('SELECT COUNT(*) FROM samples').fetchall()
        finally:
            db.close()
        # la muestra en el límite exacto (29 días) se conserva; la antigua no
        self.assertEqual(rows[0][0], 2)


class QueryAndExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = monitor.open_db(self.base / 'metrics.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def insert(self, db, now, cpu=10.0, mem_available=2_500_000, mem_total=4_000_000,
               fs_free=28_000_000_000, fs_total=40_000_000_000, probe='up'):
        db.execute(
            'INSERT OR REPLACE INTO samples (ts, created_at, cpu_percent, cpu_count, mem_available, mem_total,'
            ' fs_free, fs_total, inodes_free, inodes_total, probe) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (now, monitor._iso(now), cpu, 2, mem_available, mem_total, fs_free, fs_total,
             1_000_000, 2_000_000, probe))
        db.commit()

    def test_series_is_capped_at_1440_points(self):
        start = 1_800_000_000.0
        for index in range(1500):  # más de 1440 muestras en 24 h
            self.insert(self.db, start + index * 30, cpu=index % 50)
        data = monitor.series(self.db, now=start + 1500 * 30)
        self.assertLessEqual(len(data['cpu']), monitor.SERIES_MAX_POINTS)
        self.assertEqual(len(data['mem_available_pct']), len(data['cpu']))
        # el histórico de 30 días no se vuelca completo: la serie solo cubre 24 h
        self.assertTrue(all(point[0] >= start + (1500 * 30 - 24 * 3600) - 60 for point in data['cpu']))

    def test_daily_totals_rollover_in_madrid(self):
        # 2026-11-21 23:30 UTC (invierno) = 2026-11-22 00:30 en Madrid
        utc_late = datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc).timestamp()
        utc_early = datetime(2026, 11, 21, 21, 30, tzinfo=timezone.utc).timestamp()
        self.insert(self.db, utc_early, cpu=10.0, fs_free=10_000_000_000, fs_total=40_000_000_000)
        self.insert(self.db, utc_late, cpu=50.0, fs_free=36_000_000_000, fs_total=40_000_000_000)
        totals = monitor.daily_totals(self.db, now=utc_late + 60)
        days = {row['date']: row for row in totals}
        self.assertEqual(utc_late and datetime.fromtimestamp(utc_late, tz=MADRID).date().isoformat(), '2026-11-22')
        self.assertIn('2026-11-21', days)
        self.assertIn('2026-11-22', days)
        self.assertEqual(days['2026-11-21']['cpu_avg'], 10.0)
        self.assertEqual(days['2026-11-22']['cpu_avg'], 50.0)
        self.assertEqual(days['2026-11-21']['disk_used_max_pct'], 75.0)

    def test_daily_totals_limit_to_retention(self):
        now = 1_800_000_000.0
        self.insert(self.db, now - 31 * 86400)
        self.insert(self.db, now)
        totals = monitor.daily_totals(self.db, now=now)
        self.assertEqual(len(totals), 1)

    def test_export_sanitized_without_paths_or_private_metadata(self):
        self.insert(self.db, 1_800_000_000.0, cpu=20.0)
        self.db.execute(
            "UPDATE samples SET rag_version='v3', rag_phase='unchanged', container_name='asir-mcp-mcp-1',"
            " container_state='running', container_fresh=1, container_mem_usage=?,"
            " container_mem_limit=?, container_collected_at=?",
            (1_000_000_000, 2_147_483_648, monitor._iso(1_800_000_000.0)))
        self.db.commit()
        data = monitor.export_resources(self.base / 'metrics.sqlite',
                                        alerts_state=self.base / 'no-existe.sqlite', now=1_800_000_030.0)
        text = json.dumps(data)
        self.assertNotIn(str(self.base), text)
        self.assertNotIn('/var/lib', text)
        self.assertNotIn('sqlite', text)
        self.assertEqual(sorted(data.keys()),
                         ['alerts', 'current', 'daily_30d', 'generated_at', 'series_24h'])
        current = data['current']
        self.assertEqual(sorted(current.keys()), [
            'age_seconds', 'at', 'container', 'cpu_count', 'cpu_percent', 'disk_free_bytes',
            'disk_used_pct', 'inodes_free_pct', 'io', 'load1', 'load5', 'mem_available_pct',
            'pipeline', 'probe', 'probe_latency_ms', 'swap_free_pct'])
        self.assertEqual(current['container']['mem_pct'], round(1_000_000_000 / 2_147_483_648 * 100, 2))
        self.assertEqual(current['pipeline'], {'backup': None, 'operation': None,
                                              'phase': 'unchanged', 'version': 'v3'})
        # sin muestra previa no se fabrica una vista actual
        empty = monitor.export_resources(self.base / 'otro.sqlite', now=1_800_000_030.0)
        self.assertIsNone(empty['current'])

    def test_export_container_stale_is_unknown(self):
        self.insert(self.db, 1_800_000_000.0)
        self.db.execute(
            "UPDATE samples SET container_name='asir-mcp-mcp-1', container_state='running',"
            " container_fresh=0, container_mem_usage=?, container_mem_limit=?",
            (1_000_000_000, 2_147_483_648))
        self.db.commit()
        data = monitor.export_resources(self.base / 'metrics.sqlite', now=1_800_000_030.0)
        self.assertIsNone(data['current']['container'])

    def test_incidents_export_only_open_with_safe_fields(self):
        alerts = self.base / 'alerts.sqlite'
        with sqlite3.connect(alerts) as db:
            db.execute('CREATE TABLE incidents (rule TEXT, state TEXT, severity TEXT, opened_at TEXT, value TEXT)')
            db.execute("INSERT INTO incidents VALUES ('disk_used', 'open', 'warning', '2026-11-21T08:00:00+00:00', '87.3%')")
            db.execute("INSERT INTO incidents VALUES ('ram_available', 'resolved', 'warning', '2026-11-20T08:00:00+00:00', '8.1%')")
        data = monitor.export_resources(self.base / 'metrics.sqlite', alerts_state=alerts, now=1_800_000_030.0)
        self.assertEqual(data['alerts'], [{'rule': 'disk_used', 'severity': 'warning',
                                           'opened_at': '2026-11-21T08:00:00+00:00', 'value': '87.3%'}])


class CollectorTests(unittest.TestCase):
    INSPECT = {'State': {'Status': 'running', 'OOMKilled': False, 'StartedAt': '2026-11-21T07:00:00.000000000Z',
                         'Health': {'Status': 'healthy'}}, 'RestartCount': 0,
               'Mounts': [{'Destination': '/etc/asir'}], 'Config': {'Env': ['SECRET=1']},
               'NetworkSettings': {'IPAddress': '172.17.0.2'}}
    STATS = {'memory_stats': {'usage': 522_190_848, 'limit': 2_147_483_648},
             'cpu_stats': {'cpu_usage': {'total_usage': 2_000_000_000, 'percpu_usage': [1_000_000_000, 1_000_000_000]},
                           'system_cpu_usage': 100_000_000_000, 'online_cpus': 2},
             'precpu_stats': {'cpu_usage': {'total_usage': 1_000_000_000}, 'system_cpu_usage': 98_000_000_000}}

    def test_decode_chunked(self):
        self.assertEqual(monitor._decode_chunked(b'4\r\nabcd\r\n0\r\n\r\n'), b'abcd')

    def test_cpu_percent_from_stats(self):
        self.assertAlmostEqual(monitor._cpu_percent_from_stats(self.STATS), 100.0)
        self.assertIsNone(monitor._cpu_percent_from_stats({'memory_stats': {}}))

    def test_collect_via_fake_socket_is_sanitized(self):
        responses = {'/containers/json': [{'Id': 'abc', 'Names': ['/asir-mcp-mcp-1']}],
                     '/containers/abc/json': self.INSPECT,
                     '/containers/abc/stats': self.STATS}
        with FakeDocker(responses) as fake:
            data = monitor.collect_containers(socket_path=fake.path, timeout=2)
        self.assertTrue(data['available'])
        self.assertEqual(len(data['containers']), 1)
        container = data['containers'][0]
        self.assertEqual(container['name'], 'asir-mcp-mcp-1')
        self.assertEqual(container['state'], 'running')
        self.assertEqual(container['health'], 'healthy')
        self.assertEqual(container['mem_limit'], 2_147_483_648)
        self.assertAlmostEqual(container['cpu_percent'], 100.0)
        text = json.dumps(data)
        # campos sensibles de Docker no se exportan nunca
        for forbidden in ('Mounts', 'Env', 'SECRET', 'IPAddress', '172.17'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(fake.requests, ['/containers/json', '/containers/abc/json', '/containers/abc/stats?stream=false'])

    def test_unavailable_socket_reports_unknown(self):
        data = monitor.collect_containers(socket_path='/tmp/opencode/no-existe.sock', timeout=0.5)
        self.assertFalse(data['available'])
        self.assertEqual(data['containers'], [])

    def test_pick_container_prefers_mcp(self):
        data = {'containers': [{'name': 'otro-1'}, {'name': 'asir-mcp-mcp-1'}]}
        self.assertEqual(monitor.pick_container(data)['name'], 'asir-mcp-mcp-1')
        self.assertIsNone(monitor.pick_container({'containers': []}))
        self.assertIsNone(monitor.pick_container(None))


class ProbeTests(unittest.TestCase):
    def test_any_local_http_status_counts_as_process_up(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = int(self.path.rsplit('/', 1)[-1])
                self.send_response(status)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for status in (401, 404, 503):
                with self.subTest(status=status):
                    result, latency = monitor.probe_http(
                        f'http://127.0.0.1:{server.server_port}/{status}', timeout=2)
                    self.assertEqual(result, 'up')
                    self.assertIsInstance(latency, int)
                    self.assertGreaterEqual(latency, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_transport_errors_remain_down(self):
        with mock.patch.object(monitor.urllib.request, 'urlopen',
                               side_effect=monitor.urllib.error.URLError('offline')):
            result, latency = monitor.probe_http('http://example.invalid/private')
        self.assertEqual((result, latency), ('down', None))

    def test_missing_url_is_unknown(self):
        self.assertEqual(monitor.probe_http(None), ('unknown', None))

    def test_refused_local_connection_is_down(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        value, latency = monitor.probe_http(f'http://127.0.0.1:{port}/', timeout=0.3)
        self.assertEqual(value, 'down')
        self.assertIsNone(latency)

    def test_timeout_is_down_without_disclosing_url(self):
        private_url = 'http://127.0.0.1/private-token'
        with mock.patch.object(monitor.urllib.request, 'urlopen',
                               side_effect=TimeoutError('timed out')) as urlopen:
            result = monitor.probe_http(private_url, timeout=0.25)
        self.assertEqual(result, ('down', None))
        urlopen.assert_called_once()
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 0.25)

if __name__ == '__main__':
    unittest.main()
