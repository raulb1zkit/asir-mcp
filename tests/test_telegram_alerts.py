"""Pruebas de avisos Telegram (scripts/telegram_alerts.py).

Usa un servidor HTTP local falso como Telegram y fixtures de muestras; no
envía nada real ni toca el VPS.
"""
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server_monitor as monitor
import telegram_alerts as alerts


MADRID = ZoneInfo('Europe/Madrid')
TOKEN = '123:ABC-fake'
CHAT_ID = '42'


class FakeTelegram(threading.Thread):
    """API mínima de Telegram: registra rutas y responde según `behavior`."""

    def __init__(self, behavior='ok'):
        super().__init__(daemon=True)
        self.behavior = behavior
        self.requests = []
        self.ready = threading.Event()

    def __enter__(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                self.rfile.read(length)
                self.server.parent.requests.append(self.path)
                if self.server.parent.behavior == 'ok':
                    status, body = 200, b'{"ok": true, "result": {"message_id": 1}}'
                elif self.server.parent.behavior == 'reject':
                    status, body = 200, b'{"ok": false, "description": "chat not found"}'
                elif self.server.parent.behavior == 'error':
                    status, body = 500, b'{"ok": false}'
                else:
                    status, body = 200, b'{"ok": true, "result": {}}'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.server.parent = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.ready.set()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'


def sender_of(fake):
    return alerts.TelegramSender(TOKEN, CHAT_ID, api_base=fake.base, timeout=5)


class SenderTests(unittest.TestCase):
    def test_ok_true_confirms_delivery(self):
        with FakeTelegram('ok') as fake:
            self.assertTrue(sender_of(fake).send_message('hola'))
        self.assertEqual(fake.requests, [f'/bot{TOKEN}/sendMessage'])

    def test_http_error_does_not_leak_token_or_url(self):
        with FakeTelegram('error') as fake:
            with self.assertRaises(alerts.TelegramError) as caught:
                sender_of(fake).send_message('hola')
        text = str(caught.exception)
        self.assertIn('HTTP 500', text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('sendMessage', text)

    def test_network_error_is_sanitized(self):
        sender = alerts.TelegramSender(TOKEN, CHAT_ID, api_base='http://127.0.0.1:1', timeout=1)
        with self.assertRaises(alerts.TelegramError) as caught:
            sender.send_message('hola')
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_ok_false_rejection_keeps_description_bounded(self):
        with FakeTelegram('reject') as fake:
            with self.assertRaises(alerts.TelegramError) as caught:
                sender_of(fake).send_message('hola')
        self.assertIn('chat not found', str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_oversized_message_rejected_locally(self):
        with FakeTelegram('ok') as fake:
            with self.assertRaises(alerts.TelegramError):
                sender_of(fake).send_message('x' * 4097)
        self.assertEqual(fake.requests, [])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'telegram.json'
        self.path.write_text(json.dumps({'token': TOKEN, 'chat_id': CHAT_ID}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_private_file_accepted(self):
        os.chmod(self.path, 0o600)
        token, chat = alerts.load_config(self.path)
        self.assertEqual((token, chat), (TOKEN, CHAT_ID))

    def test_world_readable_rejected_without_content(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(alerts.ConfigError) as caught:
            alerts.load_config(self.path)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_missing_file_rejected(self):
        with self.assertRaises(alerts.ConfigError):
            alerts.load_config(Path(self.temp.name) / 'no-existe.json')

    def test_invalid_json_rejected_without_content(self):
        os.chmod(self.path, 0o600)
        self.path.write_text('{token', encoding='utf-8')
        with self.assertRaises(alerts.ConfigError) as caught:
            alerts.load_config(self.path)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_rules_defaults_and_override(self):
        rules = alerts.load_rules(None)
        self.assertEqual(rules['disk']['warn_pct'], 70.0)
        override = Path(self.temp.name) / 'rules.json'
        override.write_text(json.dumps({'disk': {'warn_pct': 60}, 'summary_hour': 8}), encoding='utf-8')
        rules = alerts.load_rules(override)
        self.assertEqual(rules['disk']['warn_pct'], 60)
        self.assertEqual(rules['disk']['crit_pct'], 85.0)
        self.assertEqual(rules['summary_hour'], 8)


def insert_samples(db, start, count, step=60, **fields):
    columns = {
        'created_at': 'x', 'load1': 0.2, 'cpu_count': 2, 'mem_available': 2_000_000_000,
        'mem_total': 4_000_000_000, 'fs_free': 20_000_000_000, 'fs_total': 40_000_000_000,
        'container_state': 'running', 'container_health': 'healthy', 'container_oom': 0,
        'container_mem_usage': 500_000_000, 'container_mem_limit': 2_147_483_648,
        'container_fresh': 1, 'probe': 'up',
    }
    columns.update(fields)
    keys = list(columns)
    for index in range(count):
        db.execute(
            f'INSERT OR REPLACE INTO samples (ts, {", ".join(keys)}) '
            f'VALUES (?, {", ".join("?" * len(keys))})',
            [start + index * step] + [columns[key] for key in keys])
    db.commit()


class EvaluateTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.db_path = base / 'metrics.sqlite'
        self.state = alerts.AlertState(base / 'alerts.sqlite')
        db = sqlite3.connect(self.db_path)
        db.execute(monitor_schema())
        db.commit()
        db.close()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def samples(self, count=10, **fields):
        db = sqlite3.connect(self.db_path)
        insert_samples(db, self.NOW - (count - 1) * 60, count, **fields)
        db.close()
        return alerts.recent_samples(alerts.open_metrics_ro(self.db_path), 10, self.NOW)

    def test_pipeline_failure_deduplicates_and_unknown_does_not_recover(self):
        failed = self.samples(rag_phase='sync_failed', rag_backup='blocked')
        actions, _ = alerts.evaluate(self.state, failed, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(len(actions), 2)
        self.assertTrue(any('rag_publish' in a for a in actions))
        self.assertTrue(any('rag_backup' in a for a in actions))
        actions, _ = alerts.evaluate(self.state, failed, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(actions, [])
        unknown = self.samples(rag_phase=None, rag_backup=None)
        actions, _ = alerts.evaluate(self.state, unknown, alerts.DEFAULT_RULES, self.NOW + 120)
        self.assertEqual(actions, [])
        self.assertEqual(self.state.incident('rag_backup')['state'], 'open')
        healthy = self.samples(rag_phase='unchanged', rag_backup='complete')
        actions, _ = alerts.evaluate(self.state, healthy, alerts.DEFAULT_RULES, self.NOW + 180)
        self.assertEqual(len(actions), 2)
        self.assertEqual(self.state.incident('rag_publish')['state'], 'resolved')

    def test_unresolved_commit_requests_review(self):
        result = alerts.rule_rag_publish(self.samples(rag_phase='recovery_required'), alerts.DEFAULT_RULES)
        self.assertIs(result[0], True)

    def test_disk_warning_opens_once_and_recovers_with_hysteresis(self):
        samples = self.samples(fs_free=10_000_000_000, fs_total=40_000_000_000)  # 75 %
        actions, failures = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(len(actions), 1)
        self.assertIn('disco al 75.0', actions[0])
        self.assertEqual(self.state.incident('disk_used')['state'], 'open')
        # deduplicación: repetir no genera mensaje nuevo
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(actions, [])
        # recuperación solo por debajo del umbral de aviso menos la histéresis
        samples = self.samples(fs_free=11_200_000_000, fs_total=40_000_000_000)  # 72 %
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 120)
        self.assertEqual(actions, [])  # 72 % < 75 % pero ≥ 67 %: no resuelve todavía
        samples = self.samples(fs_free=14_000_000_000, fs_total=40_000_000_000)  # 65 %
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 180)
        self.assertEqual(len(actions), 1)
        self.assertIn('Recuperado', actions[0])
        self.assertEqual(self.state.incident('disk_used')['state'], 'resolved')

    def test_disk_critical_escalates(self):
        samples = self.samples(fs_free=12_000_000_000, fs_total=40_000_000_000)  # 70 %
        alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(self.state.incident('disk_used')['severity'], 'warning')
        samples = self.samples(fs_free=4_000_000_000, fs_total=40_000_000_000)  # 90 %
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(len(actions), 1)
        self.assertIn('critical', actions[0])
        self.assertEqual(self.state.incident('disk_used')['severity'], 'critical')

    def test_ram_needs_sustained_window(self):
        # una sola muestra en incidente no alerta: la ventana exige varias
        samples = self.samples(count=1, mem_available=300_000_000, mem_total=4_000_000_000)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(actions, [])
        self.assertIsNone(self.state.incident('ram_available'))
        # toda la ventana por debajo del umbral sí abre
        samples = self.samples(count=10, mem_available=300_000_000, mem_total=4_000_000_000)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(len(actions), 1)
        self.assertIn('ram_available', actions[0])
        # toda la ventana correcta sí resuelve
        samples = self.samples(count=3, mem_available=3_500_000_000, mem_total=4_000_000_000)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(len(actions), 1)
        self.assertIn('Recuperado', actions[0])

    def test_unknown_samples_neither_alert_nor_resolve(self):
        samples = self.samples(fs_free=2_000_000_000, fs_total=40_000_000_000)  # 95 %
        alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(self.state.incident('disk_used')['state'], 'open')
        # muestras sin datos de disco: no alertan ni resuelven el abierto
        samples = self.samples(fs_free=None, fs_total=None)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(actions, [])
        self.assertEqual(self.state.incident('disk_used')['state'], 'open')

    def test_no_samples_means_no_actions(self):
        actions, _ = alerts.evaluate(self.state, [], alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(actions, [])

    def test_oom_alerts_immediately(self):
        samples = self.samples(container_oom=1)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(len(actions), 1)
        self.assertIn('OOM', actions[0])

    def test_container_unhealthy_sustained_opens(self):
        # una sola muestra unhealthy no alerta (ventana de 3 minutos)
        samples = self.samples(count=1, container_health='unhealthy')
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(actions, [])
        # ventana sostenida unhealthy sí abre
        samples = self.samples(count=10, container_health='unhealthy')
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(len(actions), 1)
        self.assertIn('unhealthy', actions[0])
        self.assertEqual(self.state.incident('container_health')['state'], 'open')

    def test_container_health_unknown_does_not_resolve(self):
        samples = self.samples(container_health='unhealthy')
        alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(self.state.incident('container_health')['state'], 'open')
        samples = self.samples(container_fresh=0, container_health=None)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(actions, [])
        self.assertEqual(self.state.incident('container_health')['state'], 'open')

    def test_probe_down_sustained_and_unknown_neutral(self):
        samples = self.samples(probe='down')
        alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        self.assertEqual(self.state.incident('mcp_probe')['state'], 'open')
        samples = self.samples(probe='unknown')
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(actions, [])
        self.assertEqual(self.state.incident('mcp_probe')['state'], 'open')
        samples = self.samples(probe='up')
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 120)
        self.assertEqual([a for a in actions if 'Recuperado' in a and 'mcp_probe' in a], [a for a in actions if 'mcp_probe' in a])

    def test_messages_contain_no_paths_or_payload(self):
        samples = self.samples(fs_free=2_000_000_000, fs_total=40_000_000_000)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        for message in actions:
            self.assertNotIn('/var/lib', message)
            self.assertNotIn(self.db_path.name, message)
            self.assertNotIn('token', message.lower())
            self.assertLess(len(message), 500)

    def test_reopened_incident_notifies_again(self):
        samples = self.samples(fs_free=10_000_000_000, fs_total=40_000_000_000)
        alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW)
        samples = self.samples(fs_free=16_000_000_000, fs_total=40_000_000_000)  # 60 %
        alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 60)
        self.assertEqual(self.state.incident('disk_used')['state'], 'resolved')
        samples = self.samples(fs_free=10_000_000_000, fs_total=40_000_000_000)
        actions, _ = alerts.evaluate(self.state, samples, alerts.DEFAULT_RULES, self.NOW + 120)
        self.assertEqual(len(actions), 1)
        self.assertIn('disco', actions[0])


class DeliveryAndSummaryTests(unittest.TestCase):
    NOW_MORNING = datetime(2026, 11, 21, 10, 0, tzinfo=MADRID).timestamp()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.state = alerts.AlertState(base / 'alerts.sqlite')
        self.db_path = base / 'metrics.sqlite'
        db = sqlite3.connect(self.db_path)
        db.execute(monitor_schema())
        db.commit()
        db.close()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_delivery_persists_until_ok_true(self):
        with FakeTelegram('error') as fake:
            failures = alerts.deliver(self.state, ['mensaje uno'], self.NOW_MORNING, sender_of(fake))
            self.assertEqual(len(failures), 1)
            self.assertEqual(self.state.pending(10)[0][2], 1)  # intentos registrados
            self.assertEqual(len(self.state.pending(10)), 1)
        with FakeTelegram('ok') as fake:
            failures = alerts.deliver(self.state, [], self.NOW_MORNING, sender_of(fake))
            self.assertEqual(failures, [])
            self.assertEqual(self.state.pending(10), [])
            # solo se marcó entregado con ok:true; no hay duplicados
            self.assertEqual(len(fake.requests), 1)

    def test_daily_summary_once_per_local_date_across_restarts(self):
        with FakeTelegram('ok') as fake:
            sent = alerts.maybe_daily_summary(self.state, None, alerts.DEFAULT_RULES,
                                              self.NOW_MORNING, sender_of(fake))
            self.assertEqual(sent, True)
            self.assertEqual(len(fake.requests), 1)
            # segunda ejecución el mismo día (nuevo proceso, mismo estado)
            sent = alerts.maybe_daily_summary(self.state, None, alerts.DEFAULT_RULES,
                                              self.NOW_MORNING + 300, sender_of(fake))
            self.assertEqual(sent, False)
            self.assertEqual(len(fake.requests), 1)
        # antes de las 09:00 de un día nuevo no se envía ni marca fecha
        early = datetime(2026, 11, 22, 8, 0, tzinfo=MADRID).timestamp()
        with FakeTelegram('ok') as fake:
            self.assertEqual(alerts.maybe_daily_summary(
                self.state, None, alerts.DEFAULT_RULES, early, sender_of(fake)), False)
            self.assertEqual(fake.requests, [])
        late = datetime(2026, 11, 22, 9, 30, tzinfo=MADRID).timestamp()
        with FakeTelegram('ok') as fake:
            self.assertEqual(alerts.maybe_daily_summary(
                self.state, None, alerts.DEFAULT_RULES, late, sender_of(fake)), True)
            self.assertEqual(len(fake.requests), 1)

    def test_summary_content_is_safe_and_bounded(self):
        db = sqlite3.connect(self.db_path)
        insert_samples(db, self.NOW_MORNING - 60, 1, fs_free=6_000_000_000, fs_total=40_000_000_000,
                       rag_version='v5', rag_phase='unchanged')
        db.commit()
        db.close()
        self.state.set_incident('disk_used', 'open', 'critical', 'x', None, '85.0 %', None)
        text = alerts.daily_summary_text(self.state, alerts.open_metrics_ro(self.db_path),
                                         alerts.DEFAULT_RULES, self.NOW_MORNING)
        self.assertIn('Resumen diario MCP ASIR', text)
        self.assertIn('disk_used', text)
        self.assertIn('Disco usado: 85.0 %', text)
        self.assertIn('v5', text)
        self.assertNotIn('/var/lib', text)
        self.assertNotIn('unknown123', text)
        self.assertLess(len(text), 1200)

    def test_run_check_dry_run_without_config(self):
        result = alerts.run_check(self.db_path, self.state.path, now=self.NOW_MORNING, dry_run=True)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['samples'], 0)
        self.assertEqual(result['pending'], 0)


def monitor_schema():
    return monitor._SCHEMA


if __name__ == '__main__':
    unittest.main()
