#!/usr/bin/env python3
"""Exporta contadores sin documentos ni secretos para el panel privado.

Incluye, si existe, una sección `monitor` acotada con los recursos del
servidor que ya exportó scripts/server_monitor.py: instantánea actual,
avisos abiertos y agregados diarios. La serie de 24 h no se incrusta: el
panel la toma de /api/resources para no crecer indefinidamente.
"""
import argparse
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone
from server import snapshot

DEFAULT_RESOURCES = Path('/var/lib/asir-dashboard/resources.json')


def monitor_section(resources_path, max_days=30):
    """Sección acotada del JSON del monitor, sin la serie de 24 h.

    Devuelve None si el archivo no existe o es inválido: el monitor puede
    no estar instalado y el panel muestra esa ausencia tal cual.
    """
    try:
        data = json.loads(Path(resources_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    daily = data.get('daily_30d')
    return {
        'generated_at': data.get('generated_at'),
        'current': data.get('current') if isinstance(data.get('current'), dict) else None,
        'alerts': data.get('alerts') if isinstance(data.get('alerts'), list) else [],
        'daily': daily[:max_days] if isinstance(daily, list) else [],
    }


def export(state_dir, metrics_db, resources_path=DEFAULT_RESOURCES):
    result = snapshot(state_dir, metrics_db)
    result['generated_at'] = datetime.now(timezone.utc).isoformat()
    status_path = state_dir / 'status.json'
    try:
        status = json.loads(status_path.read_text(encoding='utf-8'))
        started = datetime.fromisoformat(status['started_at']).astimezone(timezone.utc)
        run_name = started.strftime('%Y%m%dT%H%M%S%fZ')
        # Cada directorio nuevo se nombra a partir de started_at. En reanudaciones
        # o publicaciones de candidatos no se puede deducir sin ambigüedad.
        run = state_dir / 'runs' / run_name
        if not re.fullmatch(r'\d{8}T\d{12}Z', run_name) or not run.is_dir() or run.is_symlink():
            return result
        for course in result['courses']:
            path = run / course['slug'] / 'moodle' / 'manifest.json'
            try:
                report = json.loads(path.read_text(encoding='utf-8'))
                course['download'] = 'complete' if report.get('complete') and not report.get('issues') else 'incomplete'
                course['items'] = len(report['items']) if isinstance(report.get('items'), list) else None
            except (OSError, ValueError, TypeError):
                course['download'] = 'unknown'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result['monitor'] = monitor_section(resources_path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/asir-sync'))
    parser.add_argument('--metrics-db', type=Path, default=Path('/var/lib/asir/request-metrics.sqlite'))
    parser.add_argument('--monitor-resources', type=Path, default=DEFAULT_RESOURCES,
                        help='JSON del monitor de recursos; si falta la sección se publica como None')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = json.dumps(export(args.state_dir, args.metrics_db, args.monitor_resources), ensure_ascii=False).encode()
    temporary = args.output.with_suffix('.tmp')
    with temporary.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fchmod(handle.fileno(), 0o644)
        os.fsync(handle.fileno())
    os.replace(temporary, args.output)


if __name__ == '__main__':
    main()
