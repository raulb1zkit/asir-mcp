#!/usr/bin/env python3
"""Monitorización ligera de recursos del servidor para el panel privado.

Módulo exclusivo de monitorización (docs/33 y docs/34). Muestrea el host en
cada ejecución (cada 60 s programados con systemd), guarda en SQLite con
retención de 30 días y exporta un JSON sanitizado para el panel.

Reglas del contrato:

- Sin consultas a Supabase ni a la base de datos del MCP en cada muestra; la
  sonda HTTP solo comprueba que el proceso responde, sin reutilizar
  `healthz` (que consulta PostgreSQL).
- La información de contenedores la obtiene un collector raíz aparte
  (`collector`) que escribe JSON sanitizado; el panel y este muestreo nunca
  acceden al socket de Docker.
- Permisos ausentes, sondas sin configurar o datos caducados se representan
  como desconocidos (null / `unknown`); nunca se fabrican ceros.
- El snapshot exportado no contiene rutas del host, credenciales, documentos
  ni errores sin filtrar.
"""
import argparse
import json
import os
import sqlite3
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MADRID = ZoneInfo('Europe/Madrid')
RETENTION_DAYS = 30
SERIES_HOURS = 24
SERIES_MAX_POINTS = 1440
CONTAINER_STALE_SECONDS = 600
PROBE_TIMEOUT = 5.0
COLLECTOR_TIMEOUT = 5.0

DEFAULT_DB = Path('/var/lib/asir-monitor/metrics.sqlite')
DEFAULT_STATE_DIR = Path('/var/lib/asir-monitor')
DEFAULT_CONTAINER_JSON = DEFAULT_STATE_DIR / 'container.json'
DEFAULT_RESOURCES = Path('/var/lib/asir-dashboard/resources.json')
DEFAULT_ALERTS_DB = DEFAULT_STATE_DIR / 'alerts.sqlite'
DEFAULT_RAG_STATUS = Path('/var/lib/asir-sync/rag-status.json')

SECTOR_BYTES = 512

COLUMNS = (
    'ts', 'created_at', 'load1', 'load5', 'load15',
    'cpu_count', 'cpu_total', 'cpu_idle', 'cpu_percent',
    'mem_total', 'mem_available', 'swap_total', 'swap_free',
    'fs_total', 'fs_free', 'inodes_total', 'inodes_free',
    'io_read_bytes', 'io_write_bytes', 'io_ms', 'io_util_percent',
    'rag_version', 'rag_phase', 'rag_operation', 'rag_backup',
    'container_name', 'container_state', 'container_health',
    'container_oom', 'container_restarts',
    'container_mem_usage', 'container_mem_limit', 'container_cpu_percent',
    'container_collected_at', 'container_fresh',
    'probe', 'probe_latency_ms',
)

_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS samples (\n'
    '    ts REAL PRIMARY KEY,\n'
    '    created_at TEXT NOT NULL,\n'
    '    load1 REAL, load5 REAL, load15 REAL,\n'
    '    cpu_count INTEGER, cpu_total REAL, cpu_idle REAL, cpu_percent REAL,\n'
    '    mem_total INTEGER, mem_available INTEGER,\n'
    '    swap_total INTEGER, swap_free INTEGER,\n'
    '    fs_total INTEGER, fs_free INTEGER,\n'
    '    inodes_total INTEGER, inodes_free INTEGER,\n'
    '    io_read_bytes REAL, io_write_bytes REAL, io_ms REAL, io_util_percent REAL,\n'
    '    rag_version TEXT, rag_phase TEXT, rag_operation TEXT, rag_backup TEXT,\n'
    '    container_name TEXT, container_state TEXT, container_health TEXT,\n'
    '    container_oom INTEGER, container_restarts INTEGER,\n'
    '    container_mem_usage INTEGER, container_mem_limit INTEGER,\n'
    '    container_cpu_percent REAL, container_collected_at TEXT, container_fresh INTEGER,\n'
    '    probe TEXT, probe_latency_ms REAL\n'
    ')'
)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _ts(value):
    """Convierte ISO-8601 (con Z) a epoch; None si no es válido."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _pct(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator * 100, 2)


# ---------------------------------------------------------------------------
# Lectura del host. Cada función admite rutas alternativas para pruebas.
# ---------------------------------------------------------------------------

def read_meminfo(path=Path('/proc/meminfo')):
    """Devuelve (total, disponible, swap_total, swap_free) en bytes o None."""
    fields = {}
    try:
        for line in path.read_text(encoding='ascii').splitlines():
            key, _, rest = line.partition(':')
            fields[key.strip()] = rest.strip().split(' ')[0]
        return (
            int(fields['MemTotal']) * 1024,
            int(fields['MemAvailable']) * 1024,
            int(fields.get('SwapTotal', '0')) * 1024,
            int(fields.get('SwapFree', '0')) * 1024,
        )
    except (OSError, KeyError, ValueError):
        return None


def read_loadavg(path=Path('/proc/loadavg')):
    """Devuelve (load1, load5, load15) o None si no se puede leer."""
    try:
        parts = path.read_text(encoding='ascii').split()
        return tuple(float(p) for p in parts[:3])
    except (OSError, ValueError, IndexError):
        return None


def read_cpu(path=Path('/proc/stat')):
    """Devuelve (total, idle) de la línea `cpu` agregada o None."""
    try:
        for line in path.read_text(encoding='ascii').splitlines():
            if not line.startswith('cpu '):
                continue
            values = [int(v) for v in line.split()[1:]]
            idle = values[3] + values[4]  # idle + iowait
            return float(sum(values)), float(idle)
        return None
    except (OSError, ValueError):
        return None


def read_cpu_count(path=Path('/proc/stat')):
    """Número de núcleos lógicos contando las líneas `cpuN`, o None."""
    try:
        return sum(1 for line in path.read_text(encoding='ascii').splitlines()
                   if line.startswith('cpu') and line[3:4].isdigit())
    except OSError:
        return None


def root_device(fs_path='/'):
    """Par (major, minor) del dispositivo del filesystem indicado, o None."""
    try:
        dev = os.stat(fs_path).st_dev
        return (os.major(dev), os.minor(dev))
    except OSError:
        return None


def read_diskstats(path=Path('/proc/diskstats'), fs_path='/'):
    """Contadores acumulados de I/O del dispositivo del filesystem indicado.

    Devuelve None si el archivo no es legible o el dispositivo no aparece:
    en esos casos la I/O se muestra desconocida, no cero.
    """
    device = root_device(fs_path)
    if device is None:
        return None
    try:
        for line in path.read_text(encoding='ascii').splitlines():
            fields = line.split()
            if len(fields) < 14:
                continue
            if (int(fields[0]), int(fields[1])) != device:
                continue
            # Sectores de 512 B; campo 12 = ms del disco ocupado en E/S.
            return {
                'read_bytes': float(fields[5]) * SECTOR_BYTES,
                'write_bytes': float(fields[9]) * SECTOR_BYTES,
                'io_ms': float(fields[12]),
            }
        return None
    except (OSError, ValueError, IndexError):
        return None


def read_filesystem(fs_path='/'):
    """Espacio total/libre e inodos libres del filesystem real del host."""
    try:
        usage = os.statvfs(fs_path)
        return {
            'fs_total': usage.f_blocks * usage.f_frsize,
            'fs_free': usage.f_bavail * usage.f_frsize,
            'inodes_total': usage.f_files,
            'inodes_free': usage.f_ffree,
        }
    except OSError:
        return None


RAG_KEYS = ('version', 'operation', 'phase', 'backup')


def read_rag_status(path):
    """Lee rag-status.json y extrae solo los campos permitidos por contrato.

    Devuelve None si no existe o es ilegible: el pipeline es opcional y su
    ausencia es desconocida e inocua, nunca un error ni una publicación
    asumida. La sincronización pausada a propósito no se trata como fallo.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = {key: data[key] for key in RAG_KEYS if isinstance(data.get(key), (str, int, float))}
    return result or None


def probe_http(url, timeout=PROBE_TIMEOUT):
    """Sonda de proceso del MCP: cualquier respuesta HTTP (incluidos 401 y
    404) cuenta como `up`; no consulta healthz ni la base de datos.

    Devuelve ('up', ms) | ('down', None) | ('unknown', None) si no hay URL.
    No lanza excepciones y nunca revela la URL en mensajes.
    """
    if not url:
        return 'unknown', None
    start = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'asir-monitor/1.0'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
        return 'up', round((time.monotonic() - start) * 1000)
    except urllib.error.HTTPError as response:
        # HTTPError también es una respuesta del proceso (p. ej. endpoint
        # protegido o ruta inexistente); no equivale a un fallo de transporte.
        try:
            response.close()
        except OSError:
            pass
        return 'up', round((time.monotonic() - start) * 1000)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 'down', None


# ---------------------------------------------------------------------------
# Collector de contenedores (solo root). Habla con Docker por el socket UNIX
# aquí y escribe JSON sanitizado; nada del panel consume el socket.
# ---------------------------------------------------------------------------

def _http_unix(socket_path, request_path, timeout=COLLECTOR_TIMEOUT):
    """Petición HTTP/1.1 mínima por socket UNIX con soporte chunked."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(
            f'GET {request_path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n'.encode('ascii')
        )
        chunks = []
        while True:
            piece = conn.recv(65536)
            if not piece:
                break
            chunks.append(piece)
        raw = b''.join(chunks)
    head, _, body = raw.partition(b'\r\n\r\n')
    if not head:
        return None
    headers = {}
    for line in head.split(b'\r\n')[1:]:
        key, _, value = line.partition(b':')
        headers[key.strip().lower().decode('ascii', 'replace')] = value.strip().decode('ascii', 'replace')
    if headers.get('transfer-encoding', '').lower() == 'chunked':
        body = _decode_chunked(body)
    try:
        return json.loads(body.decode('utf-8'))
    except ValueError:
        return None


def _decode_chunked(body):
    try:
        result, offset = [], 0
        while offset < len(body):
            end = body.index(b'\r\n', offset)
            size = int(body[offset:end].split(b';')[0], 16)
            if size == 0:
                break
            result.append(body[end + 2:end + 2 + size])
            offset = end + 2 + size + 2
        return b''.join(result)
    except (ValueError, IndexError):
        return b''


def _cpu_percent_from_stats(stats):
    """Porcentaje de CPU del contenedor según docker stats. None si faltan
    campos (contenedor recién arrancado, formatos incompletos...)."""
    try:
        cpu = stats['cpu_stats']['cpu_usage']
        previous = stats['precpu_stats']['cpu_usage']['total_usage']
        delta_cpu = cpu['total_usage'] - previous
        delta_sys = (
            stats['cpu_stats']['system_cpu_usage']
            - stats['precpu_stats']['system_cpu_usage']
        )
        if delta_cpu < 0 or delta_sys <= 0:
            return None
        online = stats['cpu_stats'].get('online_cpus') or len(cpu.get('percpu_usage') or [1])
        return round(delta_cpu / delta_sys * online * 100, 2)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def collect_containers(socket_path=Path('/var/run/docker.sock'), timeout=COLLECTOR_TIMEOUT):
    """Inspecciona los contenedores en ejecución y devuelve solo campos
    seguros: nombre, estado, salud, OOM, memoria frente a límite y CPU.
    Nunca incluye montajes, variables de entorno, IPs ni credenciales. Si
    el socket no está disponible marca `available: False` (desconocido)."""
    try:
        listing = _http_unix(socket_path, '/containers/json', timeout)
    except (OSError, ValueError):
        listing = None
    if not isinstance(listing, list):
        return {'available': False, 'collected_at': _iso(time.time()), 'containers': []}
    containers = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        cid = entry.get('Id')
        names = entry.get('Names') or []
        name = (names[0].lstrip('/') if names else '') or ''
        info, stats = {}, {}
        if cid:
            try:
                info = _http_unix(socket_path, f'/containers/{cid}/json', timeout) or {}
                stats = _http_unix(socket_path, f'/containers/{cid}/stats?stream=false', timeout) or {}
            except (OSError, ValueError):
                info, stats = {}, {}
        state = info.get('State') or {}
        memory = stats.get('memory_stats') or {}
        containers.append({
            'name': name,
            'state': state.get('Status'),
            'health': (state.get('Health') or {}).get('Status'),
            'oom_killed': bool(state.get('OOMKilled')),
            'started_at': state.get('StartedAt') or None,
            'restarts': info.get('RestartCount'),
            'mem_usage': memory.get('usage'),
            'mem_limit': memory.get('limit'),
            'cpu_percent': _cpu_percent_from_stats(stats),
        })
    return {'available': True, 'collected_at': _iso(time.time()), 'containers': containers}


def load_container_json(path, now, stale_seconds=CONTAINER_STALE_SECONDS):
    """Carga el JSON del collector. Devuelve None si falta o es inválido y
    `fresh=False` si los datos son más antiguos que `stale_seconds`."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    collected = _ts(data.get('collected_at')) if isinstance(data, dict) else None
    fresh = bool(collected is not None and 0 <= now - collected <= stale_seconds)
    return {'fresh': fresh, 'collected_at': collected, 'data': data if fresh else None}


def pick_container(data, match='asir-mcp'):
    """Elige el contenedor del MCP entre los sanitizados del collector."""
    containers = data.get('containers') if isinstance(data, dict) else None
    if not isinstance(containers, list) or not containers:
        return None
    if match:
        for candidate in containers:
            if isinstance(candidate, dict) and match in str(candidate.get('name', '')):
                return candidate
    return containers[0] if isinstance(containers[0], dict) else None


# ---------------------------------------------------------------------------
# Almacenamiento SQLite.
# ---------------------------------------------------------------------------

def open_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.execute(_SCHEMA)
    return db


def row_dict(db, row):
    """Convierte una fila a diccionario según las columnas del esquema."""
    if row is None:
        return None
    columns = [column[1] for column in db.execute('PRAGMA table_info(samples)')]
    return dict(zip(columns, row))


def take_sample(db, now, proc_dir=Path('/proc'), fs_path='/', container_json=None,
                container_match='asir-mcp', rag_status=None, probe_url=None):
    """Recolecta una muestra y la guarda.

    Los valores desconocidos se guardan como NULL. `cpu_percent` y
    `io_util_percent` requieren la muestra anterior: la primera muestra de
    un histórico nuevo es desconocida, nunca cero.
    """
    load = read_loadavg(proc_dir / 'loadavg')
    cpu = read_cpu(proc_dir / 'stat')
    cpu_count = read_cpu_count(proc_dir / 'stat')
    mem = read_meminfo(proc_dir / 'meminfo')
    disk = read_diskstats(proc_dir / 'diskstats', fs_path)
    fs = read_filesystem(fs_path)
    rag = read_rag_status(rag_status) if rag_status else None
    probe_value, probe_latency = probe_http(probe_url) if probe_url else ('unknown', None)

    container_loaded = load_container_json(container_json, now) if container_json else None
    container = pick_container(container_loaded['data'], container_match) if container_loaded else None
    collected_at = container_loaded['collected_at'] if container_loaded else None

    row = {
        'ts': now,
        'created_at': _iso(now),
        'load1': load[0] if load else None,
        'load5': load[1] if load else None,
        'load15': load[2] if load else None,
        'cpu_count': cpu_count,
        'cpu_total': cpu[0] if cpu else None,
        'cpu_idle': cpu[1] if cpu else None,
        'cpu_percent': None,
        'mem_total': mem[0] if mem else None,
        'mem_available': mem[1] if mem else None,
        'swap_total': mem[2] if mem else None,
        'swap_free': mem[3] if mem else None,
        'fs_total': fs['fs_total'] if fs else None,
        'fs_free': fs['fs_free'] if fs else None,
        'inodes_total': fs['inodes_total'] if fs else None,
        'inodes_free': fs['inodes_free'] if fs else None,
        'io_read_bytes': disk['read_bytes'] if disk else None,
        'io_write_bytes': disk['write_bytes'] if disk else None,
        'io_ms': disk['io_ms'] if disk else None,
        'io_util_percent': None,
        'rag_version': rag.get('version') if rag else None,
        'rag_phase': rag.get('phase') if rag else None,
        'rag_operation': rag.get('operation') if rag else None,
        'rag_backup': rag.get('backup') if rag else None,
        'container_name': container.get('name') if container else None,
        'container_state': container.get('state') if container else None,
        'container_health': container.get('health') if container else None,
        'container_oom': int(bool(container.get('oom_killed'))) if container else None,
        'container_restarts': container.get('restarts') if container else None,
        'container_mem_usage': container.get('mem_usage') if container else None,
        'container_mem_limit': container.get('mem_limit') if container else None,
        'container_cpu_percent': container.get('cpu_percent') if container else None,
        'container_collected_at': _iso(collected_at) if collected_at is not None else None,
        'container_fresh': int(container_loaded['fresh']) if container_loaded else None,
        'probe': probe_value,
        'probe_latency_ms': probe_latency,
    }

    previous = row_dict(db, db.execute(
        'SELECT * FROM samples ORDER BY ts DESC LIMIT 1'
    ).fetchone())
    if previous:
        delta_total = previous['cpu_total']
        if row['cpu_total'] is not None and delta_total is not None:
            delta = row['cpu_total'] - delta_total
            delta_idle = row['cpu_idle'] - previous['cpu_idle']
            if delta > 0 and delta_idle >= 0:
                row['cpu_percent'] = round((1 - delta_idle / delta) * 100, 2)
        if row['io_ms'] is not None and previous['io_ms'] is not None and now > previous['ts']:
            elapsed = now - previous['ts']
            util = (row['io_ms'] - previous['io_ms']) / (elapsed * 1000) * 100
            row['io_util_percent'] = round(max(0.0, min(100.0, util)), 2)

    names = list(COLUMNS)
    db.execute(
        f"INSERT OR REPLACE INTO samples ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        [row[name] for name in names],
    )
    db.commit()
    prune(db, now)
    return row


def prune(db, now, retention_days=RETENTION_DAYS):
    """Elimina muestras anteriores a la retención (30 días por contrato)."""
    cutoff = now - retention_days * 86400
    db.execute('DELETE FROM samples WHERE ts < ?', (cutoff,))
    db.commit()


# ---------------------------------------------------------------------------
# Consultas para el panel: muestra actual, serie 24 h y agregados 30 días.
# ---------------------------------------------------------------------------

def current_sample(db):
    return row_dict(db, db.execute('SELECT * FROM samples ORDER BY ts DESC LIMIT 1').fetchone())


def current_rates(db):
    """Tasas de I/O entre las dos últimas muestras, en bytes/s, o None."""
    rows = db.execute(
        'SELECT ts, io_read_bytes, io_write_bytes FROM samples ORDER BY ts DESC LIMIT 2'
    ).fetchall()
    if len(rows) < 2 or rows[0][1] is None or rows[1][1] is None or rows[0][0] <= rows[1][0]:
        return None
    elapsed = rows[0][0] - rows[1][0]
    return {
        'read_bytes_per_s': max(0.0, (rows[0][1] - rows[1][1]) / elapsed),
        'write_bytes_per_s': max(0.0, (rows[0][2] - rows[1][2]) / elapsed),
    }


def series(db, hours=SERIES_HOURS, max_points=SERIES_MAX_POINTS, now=None):
    """Serie compacta de las últimas `hours` horas con techo de puntos.

    Con 60 s de muestreo las 24 h son 1440 puntos; con más muestras se
    seleccionan equidistantes. Nunca se vuelca el histórico completo de 30
    días a JSON.
    """
    now = now if now is not None else time.time()
    since = now - hours * 3600
    rows = db.execute(
        'SELECT ts, cpu_percent, mem_available, mem_total, fs_free, fs_total, load1, probe '
        'FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts',
        (since, now),
    ).fetchall()
    if len(rows) > max_points:
        step = len(rows) / max_points
        rows = [rows[min(len(rows) - 1, int(index * step))] for index in range(max_points)]
    return {
        'cpu': [[r[0], r[1]] for r in rows if r[1] is not None],
        'mem_available_pct': [[r[0], _pct(r[2], r[3])] for r in rows if r[2] is not None],
        'disk_free_pct': [[r[0], _pct(r[4], r[5])] for r in rows if r[4] is not None],
        'load1': [[r[0], r[6]] for r in rows if r[6] is not None],
        'probe': [[r[0], r[7]] for r in rows if r[7] is not None],
    }


def daily_totals(db, days=RETENTION_DAYS, now=None):
    """Agregados diarios en fecha Europe/Madrid de los últimos `days` días.

    Resumen compacto del histórico largo. La fecha se calcula en la zona del
    panel (no en UTC): una muestra a las 23:30 UTC de invierno (00:30 en
    Madrid) pertenece al día siguiente en Madrid.
    """
    now = now if now is not None else time.time()
    rows = db.execute(
        'SELECT ts, cpu_percent, mem_available, mem_total, fs_free, fs_total '
        'FROM samples WHERE ts >= ? ORDER BY ts',
        (now - days * 86400,),
    ).fetchall()
    buckets = {}
    for ts, cpu, mem_avail, mem_total, fs_free, fs_total in rows:
        day = datetime.fromtimestamp(ts, tz=MADRID).date().isoformat()
        bucket = buckets.get(day)
        if bucket is None:
            bucket = buckets[day] = {'date': day, 'cpu_sum': 0.0, 'cpu_n': 0, 'samples': 0,
                                     'mem_min': None, 'disk_max': None}
        bucket['samples'] += 1
        if cpu is not None:
            bucket['cpu_sum'] += cpu
            bucket['cpu_n'] += 1
        mem_pct = _pct(mem_avail, mem_total)
        if mem_pct is not None:
            bucket['mem_min'] = mem_pct if bucket['mem_min'] is None else min(bucket['mem_min'], mem_pct)
        disk_used = None if fs_free is None or not fs_total else round((1 - fs_free / fs_total) * 100, 2)
        if disk_used is not None:
            bucket['disk_max'] = disk_used if bucket['disk_max'] is None else max(bucket['disk_max'], disk_used)
    result = []
    for bucket in sorted(buckets.values(), key=lambda b: b['date']):
        cpu_avg = bucket['cpu_sum'] / bucket['cpu_n'] if bucket['cpu_n'] else None
        result.append({
            'date': bucket['date'],
            'samples': bucket['samples'],
            'cpu_avg': round(cpu_avg, 2) if cpu_avg is not None else None,
            'mem_available_min_pct': bucket['mem_min'],
            'disk_used_max_pct': bucket['disk_max'],
        })
    return result


# ---------------------------------------------------------------------------
# Exportación sanitizada.
# ---------------------------------------------------------------------------

def current_view(row, rates, now=None):
    """Vista sanitizada de la última muestra. None significa desconocido."""
    if not row:
        return None
    pipeline = {key: row[f'rag_{key}'] for key in ('version', 'phase', 'operation', 'backup')}
    known_pipeline = any(value is not None for value in pipeline.values())
    container = None
    if row['container_name'] is not None and row['container_fresh']:
        container = {
            'name': row['container_name'],
            'state': row['container_state'],
            'health': row['container_health'],
            'oom_killed': bool(row['container_oom']),
            'restarts': row['container_restarts'],
            'mem_usage': row['container_mem_usage'],
            'mem_limit': row['container_mem_limit'],
            'mem_pct': _pct(row['container_mem_usage'], row['container_mem_limit']),
            'cpu_percent': row['container_cpu_percent'],
            'collected_at': row['container_collected_at'],
        }
    return {
        'at': row['created_at'],
        'age_seconds': round(now - row['ts'], 1) if now is not None and row['ts'] is not None else None,
        'load1': row['load1'],
        'load5': row['load5'],
        'cpu_percent': row['cpu_percent'],
        'cpu_count': row['cpu_count'],
        'mem_available_pct': _pct(row['mem_available'], row['mem_total']),
        'swap_free_pct': _pct(row['swap_free'], row['swap_total']),
        'disk_used_pct': None if row['fs_free'] is None or not row['fs_total'] else round((1 - row['fs_free'] / row['fs_total']) * 100, 2),
        'disk_free_bytes': row['fs_free'],
        'inodes_free_pct': _pct(row['inodes_free'], row['inodes_total']),
        'io': None if rates is None else {
            'read_bytes_per_s': round(rates['read_bytes_per_s'], 1),
            'write_bytes_per_s': round(rates['write_bytes_per_s'], 1),
        },
        'probe': row['probe'],
        'probe_latency_ms': row['probe_latency_ms'],
        'pipeline': pipeline if known_pipeline else None,
        'container': container,
    }


def load_incidents(path):
    """Incidencias abiertas del estado de avisos (telegram_alerts.py).

    Lectura en solo lectura; si el archivo no existe todavía no hay avisos.
    Solo se exportan campos seguros: regla, severidad y fechas.
    """
    if not Path(path).is_file():
        return []
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    try:
        rows = db.execute(
            "SELECT rule, severity, opened_at, value FROM incidents WHERE state = 'open' ORDER BY opened_at"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [{'rule': r[0], 'severity': r[1], 'opened_at': r[2], 'value': r[3]} for r in rows]


def export_resources(db_path, alerts_state=None, now=None):
    """Construye el JSON sanitizado del panel (`resources.json`)."""
    now = now if now is not None else time.time()
    result = {'generated_at': _iso(now), 'current': None, 'alerts': [],
              'series_24h': None, 'daily_30d': None}
    if Path(db_path).is_file():
        db = sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True)
        try:
            result['current'] = current_view(current_sample(db), current_rates(db), now)
            result['series_24h'] = series(db, now=now)
            result['daily_30d'] = daily_totals(db, now=now)
        except sqlite3.Error:
            result['current'] = None
        finally:
            db.close()
    result['alerts'] = load_incidents(alerts_state) if alerts_state else []
    return result


def write_json(output, data, mode=0o644):
    """Escritura atómica con modo fijo (el panel lee el archivo exportado)."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fchmod(handle.fileno(), mode)
        os.fsync(handle.fileno())
    os.replace(temporary, output)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    sample = subparsers.add_parser('sample', help='Recolectar una muestra de recursos')
    sample.add_argument('--db', type=Path, default=DEFAULT_DB)
    sample.add_argument('--proc-dir', type=Path, default=Path('/proc'), help='Solo para pruebas')
    sample.add_argument('--fs-path', default='/')
    sample.add_argument('--container-json', type=Path, default=DEFAULT_CONTAINER_JSON)
    sample.add_argument('--container-match', default='asir-mcp')
    sample.add_argument('--rag-status', type=Path, default=DEFAULT_RAG_STATUS,
                        help='Estado opcional del pipeline; si no existe se muestra desconocido')
    sample.add_argument('--probe-url', default=None,
                        help='URL local del MCP para la sonda de proceso (sin healthz ni Supabase)')

    collector = subparsers.add_parser('collector', help='Inspeccionar Docker (solo root)')
    collector.add_argument('--socket', type=Path, default=Path('/var/run/docker.sock'))
    collector.add_argument('--output', type=Path, default=DEFAULT_CONTAINER_JSON)

    export = subparsers.add_parser('export', help='Escribir resources.json sanitizado')
    export.add_argument('--db', type=Path, default=DEFAULT_DB)
    export.add_argument('--alerts', type=Path, default=DEFAULT_ALERTS_DB,
                        help='Estado de avisos; solo se leen incidencias abiertas')
    export.add_argument('--output', type=Path, default=DEFAULT_RESOURCES)

    summary = subparsers.add_parser('summary', help='Mostrar el resumen actual (depuración)')
    summary.add_argument('--db', type=Path, default=DEFAULT_DB)
    summary.add_argument('--alerts', type=Path, default=DEFAULT_ALERTS_DB)

    args = parser.parse_args(argv)

    if args.command == 'sample':
        db = open_db(args.db)
        try:
            take_sample(db, time.time(), proc_dir=args.proc_dir, fs_path=args.fs_path,
                        container_json=args.container_json, container_match=args.container_match,
                        rag_status=args.rag_status, probe_url=args.probe_url)
        finally:
            db.close()
        return 0
    if args.command == 'collector':
        write_json(args.output, collect_containers(args.socket), mode=0o640)
        return 0
    if args.command == 'export':
        write_json(args.output, export_resources(args.db, args.alerts), mode=0o644)
        return 0
    if args.command == 'summary':
        print(json.dumps(export_resources(args.db, args.alerts), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())

