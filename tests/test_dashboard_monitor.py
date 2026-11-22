"""Pruebas de la integración del monitor en el panel (dashboard/) y unidades.

Arranca el servidor HTTP del panel localmente en un puerto efímero, usa
ficheros temporales como snapshot/resources y valida las unidades de
deploy/monitor/ como texto; no toca Docker, el VPS ni credenciales.
"""
import configparser
import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / 'dashboard'
sys.path.insert(0, str(DASHBOARD))
import export_snapshot as exporter  # noqa: E402


def free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def get(url, attempts=20):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise AssertionError(f'Sin respuesta de {url}')


class PanelServer:
    def __init__(self, snapshot=None, resources=None):
        self.port = free_port()
        self.argv = [sys.executable, str(DASHBOARD / 'server.py'), '--port', str(self.port)]
        if snapshot:
            self.argv += ['--snapshot', str(snapshot)]
        if resources is not None:
            self.argv += ['--resources', str(resources)]
        self.process = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def __enter__(self):
        time.sleep(0.5)
        return self

    def __exit__(self, *args):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.resources = self.base / 'resources.json'
        self.resources.write_text(json.dumps({
            'generated_at': '2026-11-21T09:00:00+00:00',
            'current': {'disk_used_pct': 27.0, 'mem_available_pct': 66.9, 'probe': 'up'},
            'alerts': [{'rule': 'disk_used', 'severity': 'warning', 'opened_at': 'x', 'value': '71.2%'}],
            'series_24h': {'cpu': [[1_800_000_000.0, 10.0], [1_800_000_060.0, 20.0]]},
            'daily_30d': [{'date': '2026-11-21', 'samples': 1440, 'cpu_avg': 8.0,
                           'mem_available_min_pct': 40.0, 'disk_used_max_pct': 27.1}],
        }), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_resources_endpoint_serves_sanitized_json(self):
        with PanelServer(resources=self.resources) as panel:
            status, data = get(panel.url('/api/resources'))
        self.assertEqual(status, 200)
        self.assertEqual(data['current']['disk_used_pct'], 27.0)
        self.assertEqual(data['alerts'][0]['rule'], 'disk_used')
        # no filtra contenido local: sin rutas del árbol temporal
        self.assertNotIn(self.temp.name, json.dumps(data))

    def test_missing_resources_is_503_not_fabricated(self):
        with PanelServer(resources=self.base / 'no-existe.json') as panel:
            status, data = get(panel.url('/api/resources'))
        self.assertEqual(status, 503)

    def test_status_endpoint_keeps_previous_contract(self):
        snapshot = self.base / 'status.json'
        snapshot.write_text(json.dumps({
            'generated_at': '2026-11-21T09:00:00+00:00',
            'courses': [], 'sync': {'complete': True, 'started_at': '2026-11-21T02:00:00+00:00'},
            'metrics': None, 'errors': [],
        }), encoding='utf-8')
        with PanelServer(snapshot=snapshot, resources=self.resources) as panel:
            status, data = get(panel.url('/api/status'))
        self.assertEqual(status, 200)
        self.assertEqual(sorted(data.keys()), ['courses', 'errors', 'generated_at', 'metrics', 'sync'])
        self.assertTrue(data['sync']['complete'])

    def test_server_serves_resources_only_from_public_file(self):
        source = (DASHBOARD / 'server.py').read_text(encoding='utf-8')
        self.assertNotIn('docker.sock', source)
        # la ruta de recursos es el JSON público del monitor, no la base privada
        self.assertIn("/var/lib/asir-dashboard/resources.json", source)
        self.assertIn('args.resources.read_bytes()', source)


class ExportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.resources = self.base / 'resources.json'
        self.resources.write_text(json.dumps({
            'generated_at': '2026-11-21T09:00:00+00:00',
            'current': {'disk_used_pct': 27.0},
            'alerts': [],
            'series_24h': {'cpu': [[i, i % 100] for i in range(1500)]},  # no debe incrustarse
            'daily_30d': [{'date': f'2026-10-{day:02d}', 'samples': 1,
                           'cpu_avg': 1.0, 'mem_available_min_pct': None,
                           'disk_used_max_pct': None} for day in range(1, 32)],
        }), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_monitor_section_bounded_without_series(self):
        section = exporter.monitor_section(self.resources)
        self.assertEqual(section['current']['disk_used_pct'], 27.0)
        self.assertNotIn('series_24h', section)
        self.assertEqual(len(section['daily']), 30)  # máximo por defecto

    def test_monitor_section_missing_or_invalid_is_none(self):
        self.assertIsNone(exporter.monitor_section(self.base / 'no-existe.json'))
        broken = self.base / 'broken.json'
        broken.write_text('{incompleto', encoding='utf-8')
        self.assertIsNone(exporter.monitor_section(broken))

    def test_export_includes_monitor_and_keeps_previous_keys(self):
        result = exporter.export(self.base, self.base / 'metrics.sqlite', self.resources)
        self.assertEqual(sorted(result.keys()),
                         ['courses', 'errors', 'generated_at', 'metrics', 'monitor', 'sync'])
        self.assertEqual(result['monitor']['current']['disk_used_pct'], 27.0)
        self.assertIsInstance(result['courses'], list)  # registro del repositorio, sin cambios

    def test_export_without_monitor_still_works(self):
        result = exporter.export(self.base, self.base / 'metrics.sqlite', self.base / 'no-existe.json')
        self.assertIsNone(result['monitor'])
        self.assertEqual(result['errors'], [])


class UnitTests(unittest.TestCase):
    def load(self, name):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read_string((ROOT / 'deploy/monitor' / name).read_text(encoding='utf-8'))
        return parser

    def sampler_starts(self):
        text = (ROOT / 'deploy/monitor/asir-monitor.service').read_text(encoding='utf-8')
        return [line for line in text.splitlines() if line.startswith('ExecStart=')]

    def test_sampler_unit_is_root_oneshot_with_ordered_commands(self):
        unit = self.load('asir-monitor.service')
        service = unit['Service']
        self.assertEqual(service['Type'], 'oneshot')
        self.assertEqual(service['User'], 'root')
        self.assertEqual(service['UMask'], '0077')
        starts = self.sampler_starts()
        self.assertEqual(len(starts), 3)
        self.assertIn('collector', starts[0])
        self.assertIn('sample', starts[1])
        self.assertIn('export', starts[2])
        # los scripts referenciados existen en el repositorio
        for start in starts:
            args = start.split()
            script = next(arg for arg in args if arg.endswith('server_monitor.py'))
            self.assertTrue((ROOT / 'scripts/server_monitor.py').is_file())
            # nunca se pasa el socket de Docker por argv ni un token
            self.assertNotIn('docker.sock', start)

    def test_probe_does_not_use_healthz(self):
        start = self.sampler_starts()[1]
        self.assertIn('http://127.0.0.1:8080/mcp', start)
        self.assertNotIn('/healthz', start)

    def test_alerts_unit_uses_private_config_without_secrets_inline(self):
        unit = self.load('asir-monitor-alerts.service')
        start = unit['Service']['ExecStart']
        self.assertIn('/etc/asir-monitor/telegram.json', start)
        self.assertEqual(unit['Service']['UMask'], '0077')
        text = (ROOT / 'deploy/monitor/asir-monitor-alerts.service').read_text(encoding='utf-8')
        self.assertNotIn('bot', text.lower().replace('bots', ''))
        self.assertNotRegex(text, r'token["\s]*[:=]\s*["\']?\w{10,}')

    def test_timers_run_every_60_seconds(self):
        for name in ('asir-monitor.timer', 'asir-monitor-alerts.timer'):
            timer = self.load(name)
            self.assertEqual(timer['Timer']['OnUnitActiveSec'], '60s')
            self.assertTrue(timer['Timer']['Unit'])

    def test_dashboard_units_never_touch_docker_socket(self):
        for name in ('asir-dashboard.service', 'asir-dashboard-export.service',
                     'asir-dashboard-export.timer'):
            text = (ROOT / 'deploy/dashboard' / name).read_text(encoding='utf-8')
            self.assertNotIn('docker.sock', text)
            self.assertNotIn('server_monitor', text)


if __name__ == '__main__':
    unittest.main()
