#!/usr/bin/env python3
"""Avisos Telegram del monitor del servidor (incidencias y resumen diario).

Módulo exclusivo de monitorización (docs/33 y docs/34). Evalúa reglas sobre
las muestras de scripts/server_monitor.py, mantiene estados de incidente
abierto/recuperado con deduplicación e histéresis, y envía mensajes a
Telegram con reintentos acotados y cola persistente.

Reglas del contrato:

- Configuración privada JSON {"token": ..., "chat_id": ...}; el token vive
  solo en memoria y nunca aparece en URLs de error, trazas ni registros.
- Un mensaje se marca entregado únicamente cuando sendMessage responde
  ok:true; los envíos fallidos se conservan y se reintentan acotados.
- Desconocido no alerta: sin muestras, datos caducados o sonda sin
  configurar no abren ni resuelven incidentes.
- La sincronización pausada a propósito (successful_date antiguo) no es un
  fallo; el pipeline rag-status.json ausente es desconocido e inocuo.
- Los mensajes no contienen documentos, preguntas, URLs privadas, rutas,
  secretos ni el payload completo de métricas; solo regla, severidad,
  valores acotados y fechas.
- Este CLI no envía nada durante la implementación: los envíos reales los
  hace el coordinador con credenciales provisionadas.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MADRID = ZoneInfo('Europe/Madrid')

DEFAULT_DB = Path('/var/lib/asir-monitor/metrics.sqlite')
DEFAULT_STATE_DB = Path('/var/lib/asir-monitor/alerts.sqlite')
DEFAULT_CONFIG = Path('/etc/asir-monitor/telegram.json')
DEFAULT_API_BASE = 'https://api.telegram.org'

# Umbral prudente inicial; configurable por archivo JSON con las mismas claves.
DEFAULT_RULES = {
    'disk': {'warn_pct': 70.0, 'crit_pct': 85.0, 'recover_offset_pct': 3.0},
    'ram': {'min_available_pct': 10.0, 'window_minutes': 5},
    'container_mem': {'max_pct': 85.0, 'window_minutes': 5},
    'cpu': {'load1_per_core': 2.0, 'window_minutes': 5},
    'container_health': {'window_minutes': 3},
    'container_state': {'window_minutes': 3},
    'mcp_probe': {'down_minutes': 3},
    'summary_hour': 9,
    'max_pending_per_run': 8,
    'max_attempts': 200,
}

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    rule TEXT PRIMARY KEY, state TEXT NOT NULL, severity TEXT,
    opened_at TEXT, resolved_at TEXT, value TEXT, last_message TEXT
)
"""
_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT
)
"""
_STATE_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""


class ConfigError(Exception):
    """Configuración de Telegram inválida; el mensaje no incluye contenido."""


class TelegramError(Exception):
    """Fallo de envío con mensaje saneado: sin URL, token ni traceback."""


class TelegramSender:
    """Cliente sendMessage con urllib; el token se compone solo en memoria."""

    def __init__(self, token, chat_id, api_base=DEFAULT_API_BASE, timeout=10.0):
        self.token = token
        self.chat_id = chat_id
        self.api_base = api_base.rstrip('/')
        self.timeout = timeout

    def send_message(self, text):
        """Devuelve True solo cuando la API responde ok:true; en caso
        contrario lanza TelegramError con mensaje sin URL ni token."""
        if not text.strip() or len(text) > 4096:
            raise TelegramError('Mensaje de Telegram vacío o demasiado largo.')
        payload = json.dumps({
            'chat_id': self.chat_id,
            'text': text,
            'disable_web_page_preview': True,
        }).encode('utf-8')
        # La URL con token nunca se guarda ni se imprime; el error se
        # construye por tipo, sin str(excepción) que incluiría la URL.
        request = urllib.request.Request(
            f'{self.api_base}/bot{self.token}/sendMessage',
            data=payload, headers={'Content-Type': 'application/json'},
            method='POST')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read(65536).decode('utf-8', 'replace'))
        except urllib.error.HTTPError as error:
            # Se descarta y cierra el cuerpo del error sin interpretarlo:
            # su contenido no debe filtrarse a mensajes ni registros.
            try:
                error.read(1)
            except OSError:
                pass
            finally:
                error.close()
            raise TelegramError(f'Telegram respondió HTTP {error.code}.') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise TelegramError('Error de red contactando Telegram.') from None
        if not body.get('ok'):
            description = str(body.get('description', ''))[:120]
            raise TelegramError(f'Telegram rechazó el envío. {description}' if description
                                else 'Telegram rechazó el envío.')
        return True


def load_config(path, check_owner=True):
    """Carga el token/chat_id de un archivo privado.

    Exige modo 0600 (o 0400) y propietario root o el propio proceso; nunca
    devuelve ni imprime el contenido y los errores no incluyen valores.
    """
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as error:
        raise ConfigError(f'No se puede leer la configuración de Telegram en {path}.') from None
    if check_owner:
        if stat.st_mode & 0o077:
            raise ConfigError(
                f'La configuración de Telegram en {path} tiene permisos demasiado amplios.')
        if stat.st_uid not in (os.geteuid(), 0):
            raise ConfigError(
                f'La configuración de Telegram en {path} no pertenece a root ni al proceso.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        token = str(data['token'])
        chat_id = str(data['chat_id'])
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError(f'La configuración de Telegram en {path} es inválida.') from None
    if not token or not chat_id:
        raise ConfigError(f'La configuración de Telegram en {path} está incompleta.')
    return token, chat_id


def load_rules(path=None):
    """Umbrales por defecto más cualquier clave del JSON de configuración."""
    rules = {}
    for group, values in DEFAULT_RULES.items():
        rules[group] = dict(values) if isinstance(values, dict) else values
    if not path:
        return rules
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ConfigError(f'La configuración de avisos en {path} es inválida.') from None
    if not isinstance(data, dict):
        raise ConfigError(f'La configuración de avisos en {path} debe ser un objeto.')
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(rules.get(key), dict):
            rules[key].update(value)
        else:
            rules[key] = value
    return rules


# ---------------------------------------------------------------------------
# Estado: incidentes, cola de envíos y marcadores de resumen.
# ---------------------------------------------------------------------------

class AlertState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.execute(_STATE_SCHEMA)
        self.db.execute(_DELIVERY_SCHEMA)
        self.db.execute(_STATE_KV_SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def incident(self, rule):
        row = self.db.execute('SELECT state, severity, opened_at, resolved_at, value, last_message '
                              'FROM incidents WHERE rule = ?', (rule,)).fetchone()
        if not row:
            return None
        return {'state': row[0], 'severity': row[1], 'opened_at': row[2],
                'resolved_at': row[3], 'value': row[4], 'last_message': row[5]}

    def set_incident(self, rule, state, severity=None, opened_at=None,
                     resolved_at=None, value=None, message=None):
        self.db.execute(
            'INSERT OR REPLACE INTO incidents (rule, state, severity, opened_at, resolved_at, value, last_message) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (rule, state, severity, opened_at, resolved_at, value, message))
        self.db.commit()

    def queue(self, text, now):
        self.db.execute('INSERT INTO deliveries (created_at, text, attempts, last_attempt) '
                        'VALUES (?, ?, 0, NULL)', (_iso(now), text))
        self.db.commit()

    def pending(self, limit):
        return self.db.execute('SELECT id, text, attempts FROM deliveries '
                               'ORDER BY id LIMIT ?', (limit,)).fetchall()

    def mark_sent(self, delivery_id):
        self.db.execute('DELETE FROM deliveries WHERE id = ?', (delivery_id,))
        self.db.commit()

    def mark_attempt(self, delivery_id, attempts, now):
        self.db.execute('UPDATE deliveries SET attempts = ?, last_attempt = ? WHERE id = ?',
                        (attempts, _iso(now), delivery_id))
        self.db.commit()

    def counts(self):
        sent = self.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]
        return {'pending': sent}

    def get_state(self, key):
        row = self.db.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', (key, value))
        self.db.commit()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Lectura de muestras (solo lectura sobre el SQLite del monitor).
# ---------------------------------------------------------------------------

def open_metrics_ro(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)
    except sqlite3.Error:
        return None


def recent_samples(db, window_minutes, now):
    """Muestras de la ventana (más antigua primero) o [] si no hay base."""
    if db is None:
        return []
    db.row_factory = sqlite3.Row
    since = now - window_minutes * 60
    return db.execute(
        'SELECT ts, created_at, load1, cpu_count, mem_available, mem_total, fs_free, fs_total,'
        ' container_state, container_health, container_oom, container_mem_usage, container_mem_limit,'
        ' container_fresh, probe, rag_phase, rag_backup FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts',
        (since, now)).fetchall()


def disk_used_pct(latest):
    if latest is None or latest['fs_free'] is None or not latest['fs_total']:
        return None
    return (1 - latest['fs_free'] / latest['fs_total']) * 100


def ram_available_pct(latest):
    if latest is None or latest['mem_available'] is None or not latest['mem_total']:
        return None
    return latest['mem_available'] / latest['mem_total'] * 100


def container_mem_pct(latest):
    if latest is None or latest['container_mem_usage'] is None or not latest['container_mem_limit']:
        return None
    return latest['container_mem_usage'] / latest['container_mem_limit'] * 100


def all_true(samples, predicate):
    """True si hay suficientes muestras y todas cumplen el predicado.

    Con menos de dos muestras o con alguna desconocida (None) el resultado
    es indeterminado: no alerta y tampoco resuelve un incidente abierto.
    """
    if len(samples) < 2:
        return None
    values = [predicate(sample) for sample in samples]
    if any(value is None for value in values):
        return None
    return all(values)


# ---------------------------------------------------------------------------
# Reglas: cada una devuelve (violación, severidad, valor, detalle).
# ---------------------------------------------------------------------------

def rule_disk(samples, rules):
    latest = samples[-1] if samples else None
    value = disk_used_pct(latest)
    if value is None:
        return None, None, None, None
    warn, crit = rules['disk']['warn_pct'], rules['disk']['crit_pct']
    recover = warn - float(rules['disk'].get('recover_offset_pct', 3.0))
    if value >= crit:
        return True, 'critical', value, f'disco al {value:.1f} % (crítico ≥ {crit:.0f} %)'
    if value >= warn:
        return True, 'warning', value, f'disco al {value:.1f} % (aviso ≥ {warn:.0f} %)'
    # Histéresis: entre el umbral de recuperación y el aviso el estado queda
    # indeterminado, de modo que una incidente abierto no parpadea cerca del
    # límite; por debajo del umbral de recuperación sí se resuelve.
    if value >= recover:
        return None, None, value, None
    return False, None, value, None


def rule_ram(samples, rules):
    window = rules['ram']['window_minutes']
    threshold = rules['ram']['min_available_pct']

    def violating(sample):
        value = ram_available_pct(sample)
        return None if value is None else value < threshold

    sustained = all_true(samples, violating)
    value = ram_available_pct(samples[-1]) if samples else None
    if sustained is None:
        return None, None, value, None
    if sustained:
        return True, 'warning', value, f'RAM disponible por debajo del {threshold:.0f} % durante {window} min'
    return False, None, value, None


def rule_container_mem(samples, rules):
    threshold, window = rules['container_mem']['max_pct'], rules['container_mem']['window_minutes']

    def violating(sample):
        value = container_mem_pct(sample)
        return None if value is None else value > threshold

    sustained = all_true(samples, violating)
    value = container_mem_pct(samples[-1]) if samples else None
    if sustained is None:
        return None, None, value, None
    if sustained:
        return True, 'warning', value, f'memoria del contenedor al {value:.1f} % del límite durante {window} min'
    return False, None, value, None


def rule_cpu(samples, rules):
    threshold = rules['cpu']['load1_per_core']
    window = rules['cpu']['window_minutes']

    def over(sample):
        if sample['load1'] is None or not sample['cpu_count']:
            return None
        return sample['load1'] / sample['cpu_count'] > threshold

    sustained = all_true(samples, over)
    if sustained is None:
        return None, None, None, None
    if sustained:
        latest = samples[-1]
        value = latest['load1'] / latest['cpu_count']
        return True, 'warning', value, f'carga {latest["load1"]:.2f} sobre {latest["cpu_count"]} núcleos durante {window} min'
    return False, None, None, None


def rule_container_health(samples, rules):
    """Salud unhealthy sostenida → incidente; sin healthcheck o datos
    caducados es desconocido y no alerta."""
    window = rules['container_health']['window_minutes']

    def unhealthy(sample):
        if not sample['container_fresh'] or sample['container_health'] is None:
            return None
        return sample['container_health'] == 'unhealthy'

    sustained = all_true(samples, unhealthy)
    if sustained is None:
        return None, None, None, None
    if not sustained:
        return False, None, None, None
    return True, 'warning', None, 'salud del contenedor MCP unhealthy de forma sostenida'


def rule_container_state(samples, rules):
    window = rules['container_state']['window_minutes']

    def broken(sample):
        if not sample['container_fresh'] or sample['container_state'] is None:
            return None
        return sample['container_state'] in ('exited', 'dead', 'restarting')

    sustained = all_true(samples, broken)
    if sustained is None:
        return None, None, None, None
    if not sustained:
        return False, None, None, None
    state = samples[-1]['container_state']
    return True, 'critical', None, f'contenedor MCP en estado {state} de forma sostenida'


def rule_container_oom(samples, rules):
    """OOM inmediato: basta una muestra con OOMKilled en la ventana."""
    for sample in samples:
        if sample['container_fresh'] and sample['container_oom']:
            return True, 'critical', None, 'el contenedor MCP fue terminado por OOM'
    return False, None, None, None


def rule_mcp_probe(samples, rules):
    """Sonda de proceso: 'down' sostenido → incidente; 'unknown' (sin URL
    configurada) no alerta ni resuelve."""
    window = rules['mcp_probe']['down_minutes']

    def down(sample):
        if sample['probe'] is None or sample['probe'] == 'unknown':
            return None
        return sample['probe'] == 'down'

    sustained = all_true(samples, down)
    if sustained is None:
        return None, None, None, None
    if not sustained:
        return False, None, None, None
    return True, 'critical', None, 'el proceso del MCP no responde HTTP de forma sostenida'


def _pipeline_rule(samples, field, failures, successes, detail):
    value = samples[-1][field] if samples else None
    if value in failures:
        return True, 'warning', None, detail
    if value in successes:
        return False, None, None, None
    return None, None, None, None


def rule_rag_publish(samples, rules):
    return _pipeline_rule(samples, 'rag_phase',
                          {'failed', 'sync_failed', 'blocked', 'indeterminate', 'absent', 'reverted', 'recovery_required'},
                          {'published', 'unchanged'},
                          'la sincronización o publicación requiere revisión; se conserva la versión protegida')


def rule_rag_backup(samples, rules):
    return _pipeline_rule(samples, 'rag_backup', {'failed', 'blocked', 'pending', 'deferred'},
                          {'complete'}, 'el respaldo remoto no está completo; se conserva la copia local')


RULES = {
    'rag_publish': rule_rag_publish,
    'rag_backup': rule_rag_backup,
    'disk_used': rule_disk,
    'ram_available': rule_ram,
    'container_memory': rule_container_mem,
    'cpu_load': rule_cpu,
    'container_health': rule_container_health,
    'container_state': rule_container_state,
    'container_oom': rule_container_oom,
    'mcp_probe': rule_mcp_probe,
}


# ---------------------------------------------------------------------------
# Evaluación y envío.
# ---------------------------------------------------------------------------

def _stamp(epoch):
    return datetime.fromtimestamp(epoch, tz=MADRID).strftime('%d/%m %H:%M')


def format_message(rule, severity, detail, epoch, kind='incidente'):
    prefix = {'incidente': 'Incidencia', 'recuperado': 'Recuperado', 'escalado': 'Agravado'}.get(kind, kind)
    head = f'[{severity}] MCP ASIR · {prefix}: {rule}' if severity else f'MCP ASIR · {prefix}: {rule}'
    return f'{head}\n{detail}\n{_stamp(epoch)} Europe/Madrid'


def evaluate(state, samples, rules, now, sender=None, dry_run=False):
    """Evalúa todas las reglas, actualiza incidentes y encola mensajes.

    Devuelve (acciones, fallos). La recuperación exige señales correctas
    sostenidas; desconocido no resuelve.
    """
    actions = []
    for rule, checker in RULES.items():
        violation, severity, value, detail = checker(samples, rules)
        current = state.incident(rule)
        value_text = f'{value:.1f} %' if isinstance(value, float) else (None if value is None else str(value))
        if violation is True:
            if current is None or current['state'] != 'open':
                message = format_message(rule, severity, detail, now, 'incidente')
                state.set_incident(rule, 'open', severity, _iso(now), None, value_text, message)
                actions.append(message)
            elif current['severity'] == 'warning' and severity == 'critical':
                message = format_message(rule, severity, detail, now, 'escalado')
                state.set_incident(rule, 'open', severity, current['opened_at'], None, value_text, message)
                actions.append(message)
            else:
                # Deduplicación: incidente ya abierto con la misma severidad,
                # no se repite el aviso.
                state.set_incident(rule, 'open', severity or current['severity'],
                                   current['opened_at'], None, value_text, current['last_message'])
        elif violation is False and current is not None and current['state'] == 'open':
            message = format_message(rule, 'ok', f'{rule} ha vuelto a valores normales.', now, 'recuperado')
            state.set_incident(rule, 'resolved', current['severity'], current['opened_at'], _iso(now),
                               value_text, message)
            actions.append(message)
    failures = [] if dry_run else deliver(state, actions, now, sender, rules)
    return actions, failures


def deliver(state, new_messages, now, sender, rules=None):
    """Encola los mensajes nuevos y vacía la cola con reintentos acotados.

    Marca entregado solo cuando sender confirma ok:true; los mensajes
    fallidos siguen en la cola para el siguiente intento (no se pierden) y
    al entregarse se borran, sin duplicados entre reinicios.
    """
    for text in new_messages:
        state.queue(text, now)
    if sender is None:
        return []
    rules = rules or DEFAULT_RULES
    limit = int(rules.get('max_pending_per_run', DEFAULT_RULES['max_pending_per_run']))
    max_attempts = int(rules.get('max_attempts', DEFAULT_RULES['max_attempts']))
    failures = []
    for delivery_id, text, attempts in state.pending(limit):
        if attempts >= max_attempts:
            # Acotado: el mensaje supera el número de reintentos previstos y
            # se mantiene en cola con su contador; no se borra ni se finge
            # entrega. El coordinador revisa la cola con la CLI.
            failures.append(f'entrega {delivery_id} supera {max_attempts} intentos')
            continue
        try:
            sender.send_message(text)
        except TelegramError as error:
            state.mark_attempt(delivery_id, attempts + 1, now)
            failures.append(str(error))
        else:
            state.mark_sent(delivery_id)
    return failures


def daily_summary_text(state, db, rules, now):
    """Resumen diario: incidentes activos/cerrados y recursos actuales."""
    open_incidents = [
        row for row in state.db.execute(
            "SELECT rule, severity FROM incidents WHERE state = 'open'").fetchall()
    ]
    closed = state.db.execute(
        "SELECT COUNT(*) FROM incidents WHERE state = 'resolved' AND resolved_at >= ?",
        (_iso(now - 86400),)).fetchone()[0]
    lines = [f'Resumen diario MCP ASIR · {_stamp(now)} Europe/Madrid']
    if open_incidents:
        lines.append('Incidencias abiertas: ' + ', '.join(
            f'{rule} ({severity or "warning"})' for rule, severity in open_incidents))
    else:
        lines.append('Sin incidencias abiertas.')
    lines.append(f'Recuperadas en las últimas 24 h: {closed}.')
    if db is not None:
        db.row_factory = sqlite3.Row
        latest = db.execute('SELECT * FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        if latest is not None:
            used = disk_used_pct(latest)
            ram = ram_available_pct(latest)
            lines.append(f'Disco usado: {used:.1f} %.' if used is not None else 'Disco: desconocido.')
            lines.append(f'RAM disponible: {ram:.1f} %.' if ram is not None else 'RAM disponible: desconocida.')
            if latest['load1'] is not None:
                lines.append(f'Carga 1 min: {latest["load1"]:.2f}.')
            if latest['container_state'] is not None:
                lines.append(f'Contenedor MCP: {latest["container_state"]}.')
            if latest['rag_version'] is not None:
                lines.append(f'Versión publicada: {latest["rag_version"]}.')
    return '\n'.join(lines)


def maybe_daily_summary(state, db, rules, now, sender=None, dry_run=False):
    """Envía el resumen a las rules['summary_hour'] (09:00 Europe/Madrid).

    Marca la fecha local enviada; un reinicio no lo duplica y un día nuevo
    lo vuelve a permitir. Si el envío falla, el mensaje queda en cola y la
    fecha ya marcada evita duplicados al reintentar.
    """
    local = datetime.fromtimestamp(now, tz=MADRID)
    hour = int(rules.get('summary_hour', DEFAULT_RULES['summary_hour']))
    if local.hour < hour:
        return False
    today = local.date().isoformat()
    if state.get_state('daily_summary_date') == today:
        return False
    text = daily_summary_text(state, db, rules, now)
    if dry_run:
        return text
    deliver(state, [text], now, sender, rules)
    state.set_state('daily_summary_date', today)
    return True


def run_check(db_path, state_path, rules_path=None, config_path=DEFAULT_CONFIG,
              api_base=DEFAULT_API_BASE, now=None, dry_run=False, no_summary=False):
    """Ejecución de un ciclo: evaluar, gestionar incidentes y enviar."""
    now = time.time() if now is None else now
    rules = load_rules(rules_path)
    sender = None
    if not dry_run:
        token, chat_id = load_config(config_path)
        sender = TelegramSender(token, chat_id, api_base=api_base)
    db = open_metrics_ro(db_path)
    state = AlertState(state_path)
    try:
        window = max(rules['ram']['window_minutes'], rules['cpu']['window_minutes'],
                     rules['container_health']['window_minutes'],
                     rules['container_state']['window_minutes'],
                     rules['mcp_probe']['down_minutes'])
        samples = recent_samples(db, window, now)
        actions, failures = evaluate(state, samples, rules, now, sender=sender, dry_run=dry_run)
        summary = None
        if not no_summary:
            summary = maybe_daily_summary(state, db, rules, now, sender=sender, dry_run=dry_run)
        if dry_run:
            return {'actions': actions, 'summary': summary if summary is not False else None,
                    'samples': len(samples), 'pending': state.counts()['pending']}
        return {'actions': actions, 'failures': failures,
                'pending': state.counts()['pending']}
    finally:
        state.close()
        if db is not None:
            db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE_DB,
                        help='Estado de incidentes y cola de envíos (SQLite privado)')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                        help='Archivo privado con token y chat_id (nunca en argv los valores)')
    parser.add_argument('--rules', type=Path, default=None,
                        help='JSON opcional de umbrales y horario del resumen')
    parser.add_argument('--api-base', default=DEFAULT_API_BASE,
                        help='Base de la API de Telegram (solo pruebas)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Evaluar y mostrar mensajes sin enviar ni mutar estado')
    parser.add_argument('--no-summary', action='store_true',
                        help='Omitir el resumen diario en esta ejecución')
    args = parser.parse_args(argv)
    try:
        result = run_check(args.db, args.state, rules_path=args.rules,
                           config_path=args.config, api_base=args.api_base,
                           dry_run=args.dry_run, no_summary=args.no_summary)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 3
    except TelegramError as error:
        print(str(error), file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
