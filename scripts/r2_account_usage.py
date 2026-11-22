#!/usr/bin/env python3
"""Consulta uso R2 de toda la cuenta y escribe evidencia privada para presupuestos."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import urllib.request


class UsageUnavailable(Exception):
    """La ausencia de evidencia no debe interpretarse como consumo cero."""


CLASS_A = frozenset(('ListBuckets', 'PutBucket', 'ListObjects', 'ListObjectsV2',
    'PutObject', 'CopyObject', 'CompleteMultipartUpload', 'CreateMultipartUpload',
    'LifecycleStorageTierTransition', 'ListMultipartUploads', 'UploadPart',
    'UploadPartCopy', 'ListParts', 'PutBucketEncryption', 'PutBucketCors',
    'PutBucketLifecycleConfiguration'))
CLASS_B = frozenset(('HeadBucket', 'HeadObject', 'GetObject', 'UsageSummary',
    'GetBucketEncryption', 'GetBucketLocation', 'GetBucketCors',
    'GetBucketLifecycleConfiguration'))
FREE = frozenset(('DeleteObject', 'DeleteBucket', 'AbortMultipartUpload'))
QUERY = '''query($accountTag: string!, $startDate: Time!, $endDate: Time!) {
 viewer { accounts(filter: {accountTag: $accountTag}) {
 r2OperationsAdaptiveGroups(limit: 10000,
 filter: {datetime_geq: $startDate, datetime_leq: $endDate}) {
 sum { requests } dimensions { actionType }
 } } } }'''


def integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageUnavailable('invalid_usage_value')
    if not math.isfinite(value) or value < 0 or int(value) != value:
        raise UsageUnavailable('invalid_usage_value')
    return int(value)


def summarize(metrics, operations, now):
    """Ventana móvil 31 días: sobrecuenta prudentemente al cambiar de mes."""
    try:
        if metrics.get('success') is not True or operations.get('errors'):
            raise UsageUnavailable('provider_usage_incomplete')
        standard = metrics['result']['standard']
        total = sum(integer(standard[state][field]) for state in ('published', 'uploaded')
                    for field in ('payloadSize', 'metadataSize'))
        accounts = operations['data']['viewer']['accounts']
        if len(accounts) != 1:
            raise UsageUnavailable('account_usage_ambiguous')
        rows = accounts[0]['r2OperationsAdaptiveGroups']
        if not isinstance(rows, list) or len(rows) >= 10000:
            raise UsageUnavailable('operations_usage_incomplete')
        a = b = 0
        for row in rows:
            count = integer(row['sum']['requests'])
            action = row['dimensions']['actionType']
            if not isinstance(action, str) or not action:
                raise UsageUnavailable('unknown_action_identity')
            if action in FREE:
                continue
            if action in CLASS_A:
                a += count
            elif action in CLASS_B:
                b += count
            else:
                # Nuevas operaciones sin clasificar nunca se consideran gratis.
                a += count
                b += count
        return {'schema_version': 1, 'observed_at': now.isoformat(),
                'period_start': (now - timedelta(days=31)).isoformat(),
                'period_end': now.isoformat(), 'storage_bytes': total,
                'class_a_operations': a, 'class_b_operations': b,
                'complete': True, 'source': 'cloudflare_account_api'}
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageUnavailable('provider_usage_invalid') from None


def private_config(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.getuid()):
        raise UsageUnavailable('credentials_must_be_private_owned_regular_file')
    config = json.loads(path.read_text())
    if not re.fullmatch('[a-f0-9]{32}', config.get('cloudflare_account_id', '')):
        raise UsageUnavailable('account_id_invalid')
    if not config.get('cloudflare_api_token'):
        raise UsageUnavailable('api_token_missing')
    return config


def provider_request(url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception:
        # No reproducir respuesta, cabeceras ni texto de excepciones HTTP.
        raise UsageUnavailable('provider_request_failed') from None


def refresh_account_usage(credentials_path, output_path, *, request=provider_request, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise UsageUnavailable('timezone_required')
    now = now.astimezone(timezone.utc)
    config = private_config(credentials_path)
    account, token = config['cloudflare_account_id'], config['cloudflare_api_token']
    metrics = request('https://api.cloudflare.com/client/v4/accounts/' + account + '/r2/metrics', token)
    operations = request('https://api.cloudflare.com/client/v4/graphql', token, {
        'query': QUERY, 'variables': {'accountTag': account,
            'startDate': (now - timedelta(days=31)).isoformat(), 'endDate': now.isoformat()}})
    result = summarize(metrics, operations, now)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.usage-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(result, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(refresh_account_usage(args.config, args.output), sort_keys=True))
        return 0
    except (UsageUnavailable, OSError, ValueError):
        print(json.dumps({'complete': False, 'error': 'account_usage_unavailable'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
