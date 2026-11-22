#!/usr/bin/env python3
"""Despliegue y operación del worker externo de salud vía API de Cloudflare
(sin wrangler).

- Carga credenciales de .private en memoria; NUNCA las imprime, ni las pasa
  por argv ni por sustituciones de comando.
- Salida sanitizada: estados HTTP, success, códigos de error y nombres de
  bindings. Nunca valores de secretos ni IDs completos.
- Solo toca recursos dedicados con nombres propios (mcp-asir-external-health);
  el namespace KV se reutiliza solo si tiene el título exacto de este
  despliegue.

Modos:
- (por defecto) crea el worker: verify → colisiones → KV → código+bindings
  → estado inicial (con [TEST] one-off salvo --skip-test) → cron al final.
- --update redespliega módulo+bindings del worker EXISTENTE: verifica por
  API que sus bindings son exactamente los de este despliegue (nombres y
  tipos; si hay algo distinto, aborta), NO siembra estado (preserva el KV
  actual y su cola) y no toca el cron salvo que esté vacío.
- --status lee en modo solo-lectura el resumen saneado del estado y los
  schedules (whitelist: schema, health, incidente/cola por conteo y tipos,
  timestamps). Nunca imprime secretos ni cuerpos completos.

Uso: python3 deploy.py [--skip-test | --update | --status]
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
import uuid

SECRETS = os.environ.get('ASIR_RAG_INTEGRATIONS_FILE',
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), '.private', 'rag-integrations.json'))
BASE = 'https://api.cloudflare.com/client/v4'
WORKER_NAME = 'mcp-asir-external-health'
KV_TITLE = 'mcp-asir-external-health-state'
CRON = '*/5 * * * *'
COMPAT_DATE = '2026-11-01'
HEALTH_URL = 'https://mcp.bztracker.xyz/healthz'

INITIAL_STATE = {
    'schema': 2,
    'health': {
        'status': 'unknown', 'failCount': 0,
        'lastCheckAt': None, 'lastHealthyAt': None, 'lastError': None,
    },
    'incident': None,
    'queue': [],
    'testPending': True,  # un único mensaje [TEST] en el primer ciclo real
}

# Bindings que identifican a ESTE despliegue (nombre → tipo esperado).
OWN_BINDINGS = {
    'STATE': 'kv_namespace',
    'TELEGRAM_TOKEN': 'secret_text',
    'TELEGRAM_CHAT_ID': 'secret_text',
    'HEALTH_URL': 'plain_text',
}


def load_secrets():
    with open(SECRETS) as fh:
        data = json.load(fh)
    return (
        data['cloudflare_account_id'],
        data['cloudflare_api_token'],
        data['telegram']['token'],
        str(data['telegram']['chat_id']),
    )


def call(method, path, token, body=None, raw=None, ctype=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if raw is not None:
        req.data = raw
        req.add_header('Content-Type', ctype or 'application/octet-stream')
    elif body is not None:
        req.data = json.dumps(body).encode()
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read().decode())
        except Exception:
            return err.code, {}
    except Exception as exc:  # red
        return None, {'success': False, 'errors': [{'code': 0, 'message': type(exc).__name__}]}


def show(step, status, result, extra=''):
    errors = result.get('errors') or []
    errs = '; '.join(f"code={e.get('code')} msg={(e.get('message') or '')[:120]}" for e in errors)
    ok = result.get('success')
    line = f"[{step}] HTTP {status} success={ok}"
    if errs:
        line += f" errors: {errs}"
    if extra:
        line += f" | {extra}"
    print(line, flush=True)
    return bool(ok) and status and status < 400


def multipart(parts):
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, filename, ctype, payload in parts:
        out += f'--{boundary}\r\n'.encode()
        disp = f'form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        out += f'Content-Disposition: {disp}\r\n'.encode()
        out += f'Content-Type: {ctype}\r\n\r\n'.encode()
        out += payload + b'\r\n'
    out += f'--{boundary}--\r\n'.encode()
    return bytes(out), f'multipart/form-data; boundary={boundary}'


def call_raw(method, path, token):
    """Llamada que devuelve (status, texto) — para lecturas sin envelope
    JSON (el REST de KV devuelve el valor tal cual, sin success/errors)."""
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read().decode()
        except Exception:
            return err.code, ''
    except Exception as exc:
        return None, type(exc).__name__


def find_namespace(account_id, token):
    """Devuelve el id del namespace con el título exacto de este despliegue."""
    st, r = call('GET', f'/accounts/{account_id}/storage/kv/namespaces?per_page=100', token)
    if not show('list-kv', st, r):
        return None
    ns = next((n for n in (r.get('result') or []) if n.get('title') == KV_TITLE), None)
    if ns is None:
        print(f"ABORTA: no existe el namespace '{KV_TITLE}'.")
        return None
    print(f"[find-kv] reutilizado title={KV_TITLE} (creado por este despliegue)")
    return ns.get('id')


def upload_module(account_id, token, ns_id, tg_token, tg_chat):
    """Sube el módulo con bindings; los secretos van desde memoria."""
    with open(__file__.replace('deploy.py', 'worker.js'), 'rb') as fh:
        worker_code = fh.read()
    metadata = {
        'main_module': 'worker.js',
        'compatibility_date': COMPAT_DATE,
        'bindings': [
            {'type': 'kv_namespace', 'name': 'STATE', 'namespace_id': ns_id},
            {'type': 'secret_text', 'name': 'TELEGRAM_TOKEN', 'text': tg_token},
            {'type': 'secret_text', 'name': 'TELEGRAM_CHAT_ID', 'text': tg_chat},
            {'type': 'plain_text', 'name': 'HEALTH_URL', 'text': HEALTH_URL},
        ],
    }
    body, ctype = multipart([
        ('metadata', None, 'application/json', json.dumps(metadata).encode()),
        ('worker.js', 'worker.js', 'application/javascript+module', worker_code),
    ])
    st, r = call('PUT', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}', token,
                 raw=body, ctype=ctype)
    return show('upload-worker', st, r, extra=f"name={WORKER_NAME}")


def read_back(account_id, token):
    st, r = call('GET', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}/schedules', token)
    scheds = (r.get('result') or {}).get('schedules', [])
    show('read-back-schedule', st, r, extra=f"{[s.get('cron') for s in scheds]}")
    st, r = call('GET', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}/settings', token)
    bindings = [
        {'name': b.get('name'), 'type': b.get('type')}
        for b in ((r.get('result') or {}).get('bindings') or [])
    ]
    show('read-back-bindings', st, r, extra=f"{bindings}")
    return scheds


def verify_own_bindings(account_id, token, ns_id):
    """Confirma que el worker existente es exactamente el de este despliegue."""
    st, r = call('GET', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}/settings', token)
    if not show('read-bindings', st, r):
        return False
    bindings = (r.get('result') or {}).get('bindings') or []
    by_name = {b.get('name'): b for b in bindings if isinstance(b, dict)}
    if set(by_name) != set(OWN_BINDINGS):
        print(f"ABORTA: bindings inesperados: {sorted(by_name)} (esperados: {sorted(OWN_BINDINGS)})")
        return False
    for name, expected in OWN_BINDINGS.items():
        if by_name[name].get('type') != expected:
            print(f"ABORTA: binding {name} es {by_name[name].get('type')} (esperado {expected})")
            return False
    if by_name['STATE'].get('namespace_id') != ns_id:
        print('ABORTA: el binding STATE apunta a otro namespace.')
        return False
    print('[read-bindings] bindings propios confirmados (nombres, tipos y namespace)')
    return True


def create_mode(args, account_id, token):
    state = dict(INITIAL_STATE)
    state['testPending'] = not args.skip_test

    # 1) Inventario: nunca sobrescribir un worker existente.
    st, r = call('GET', f'/accounts/{account_id}/workers/scripts', token)
    names = [s.get('id') for s in (r.get('result') or []) if isinstance(s, dict)]
    if not show('list-scripts', st, r, extra=f"existentes={names}"):
        return 1
    if WORKER_NAME in names:
        print(f"ABORTA: ya existe un worker '{WORKER_NAME}' (usa --update).")
        return 1

    # 2) KV dedicado: crear solo si no existe el título propio.
    st, r = call('GET', f'/accounts/{account_id}/storage/kv/namespaces?per_page=100', token)
    if not show('list-kv', st, r):
        return 1
    ns = next((n for n in (r.get('result') or []) if n.get('title') == KV_TITLE), None)
    if ns is None:
        st, r = call('POST', f'/accounts/{account_id}/storage/kv/namespaces', token,
                     body={'title': KV_TITLE})
        if not show('create-kv', st, r, extra=f"title={KV_TITLE}"):
            return 1
        ns = r['result']
    else:
        print(f"[create-kv] reutilizado título={KV_TITLE} (creado por este despliegue)")

    # 3) Subida del módulo con bindings (secretos embebidos desde memoria).
    if not upload_module(account_id, token, ns['id'], tg_token, tg_chat):
        return 1

    # 4) Estado inicial sembrado (incluye el mensaje [TEST] one-off si procede).
    st, r = call('PUT', f"/accounts/{account_id}/storage/kv/namespaces/{ns['id']}/values/state",
                 token, raw=json.dumps(state).encode(), ctype='application/json')
    if not show('seed-state', st, r, extra=f"testPending={state['testPending']}"):
        return 1

    # 5) Cron al final: el primer ciclo ya encuentra estado y secretos listos.
    st, r = call('PUT', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}/schedules',
                 token, body=[{'cron': CRON}])
    if not show('set-schedule', st, r,
                extra=f"crons={[s.get('cron') for s in (r.get('result') or {}).get('schedules', [])]}"):
        return 1

    read_back(account_id, token)
    print(f"DESPLIEGUE OK: worker={WORKER_NAME} cron={CRON} kv_title={KV_TITLE}")
    return 0


def update_mode(account_id, token, tg_token, tg_chat):
    # 1) El worker debe existir y ser el de este despliegue.
    st, r = call('GET', f'/accounts/{account_id}/workers/scripts', token)
    names = [s.get('id') for s in (r.get('result') or []) if isinstance(s, dict)]
    if not show('list-scripts', st, r, extra=f"existentes={names}"):
        return 1
    if WORKER_NAME not in names:
        print(f"ABORTA: no existe '{WORKER_NAME}' (usa el modo de creación).")
        return 1

    # 2) Namespace propio existente (nunca se crea en update).
    ns_id = find_namespace(account_id, token)
    if ns_id is None:
        return 1

    # 3) Verificar propiedad por bindings antes de sobrescribir.
    if not verify_own_bindings(account_id, token, ns_id):
        return 1

    # 4) Redespliegue del módulo; el estado KV NO se toca.
    if not upload_module(account_id, token, ns_id, tg_token, tg_chat):
        return 1

    # 5) Cron: solo se reestablece si quedó vacío (PUT de módulo no lo borra).
    scheds = read_back(account_id, token)
    if not scheds:
        st, r = call('PUT', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}/schedules',
                     token, body=[{'cron': CRON}])
        if not show('set-schedule', st, r, extra=f"crons={CRON}"):
            return 1
        read_back(account_id, token)

    print(f"UPDATE OK: worker={WORKER_NAME} módulo+bindings actualizados, estado KV preservado")
    return 0


def status_mode(account_id, token):
    st, r = call('GET', f'/accounts/{account_id}/workers/scripts/{WORKER_NAME}/schedules', token)
    scheds = (r.get('result') or {}).get('schedules', [])
    if not show('schedules', st, r, extra=f"{[s.get('cron') for s in scheds]}"):
        return 1

    ns_id = find_namespace(account_id, token)
    if ns_id is None:
        return 1
    st, text = call_raw('GET', f'/accounts/{account_id}/storage/kv/namespaces/{ns_id}/values/state', token)
    print(f"[kv-state] HTTP {st}")
    if not st or st >= 400:
        return 1
    # Whitelist de salida: nunca secretos ni cuerpos completos.
    try:
        s = json.loads(text)
    except Exception:
        print('[kv-state] estado no es JSON legible')
        return 1
    if not isinstance(s, dict) or not s:
        print('[kv-state] estado vacío o ilegible')
        return 1
    health = s.get('health') or (
        # Estado v1 pre-migración: campos en la raíz.
        {
            'status': s.get('status'),
            'failCount': s.get('failCount'),
            'lastCheckAt': s.get('lastCheckAt'),
            'lastHealthyAt': s.get('lastHealthyAt'),
            'lastError': s.get('lastError'),
        }
    )
    incident = s.get('incident')
    queue = s.get('queue') or []
    summary = {
        'schema': s.get('schema'),
        'status': health.get('status'),
        'failCount': health.get('failCount'),
        'lastCheckAt': health.get('lastCheckAt'),
        'lastHealthyAt': health.get('lastHealthyAt'),
        'lastError': health.get('lastError'),
        'incidentId': (incident or {}).get('id'),
        'queueCount': len(queue),
        'queueKinds': [m.get('kind') for m in queue],
        'testPending': bool(s.get('testPending')),
    }
    print(f"ESTADO: {json.dumps(summary, ensure_ascii=False)}")
    return 0


def main():
    parser = argparse.ArgumentParser(description='Despliegue/operación del worker de salud (API CF, sin wrangler)')
    parser.add_argument('--skip-test', action='store_true', help='(crear) no sembrar testPending')
    parser.add_argument('--update', action='store_true', help='redesplegar módulo+bindings del worker existente (preserva estado KV)')
    parser.add_argument('--status', action='store_true', help='solo lectura: resumen saneado del estado y schedules')
    args = parser.parse_args()

    account_id, token, tg_token, tg_chat = load_secrets()

    # 0) Verificación del token de cuenta (todos los modos).
    st, r = call('GET', f'/accounts/{account_id}/tokens/verify', token)
    if not show('verify', st, r, extra=f"status={r.get('result', {}).get('status')}"):
        return 1

    if args.status:
        return status_mode(account_id, token)
    if args.update:
        return update_mode(account_id, token, tg_token, tg_chat)
    return create_mode(args, account_id, token)


if __name__ == '__main__':
    sys.exit(main())
