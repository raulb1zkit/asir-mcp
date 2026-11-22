import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from scripts.r2_account_usage import UsageUnavailable, refresh_account_usage, summarize


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
METRICS = {'success': True, 'result': {'standard': {
    'published': {'payloadSize': 100, 'metadataSize': 10},
    'uploaded': {'payloadSize': 20, 'metadataSize': 2}}}}


def operations(rows):
    return {'errors': None, 'data': {'viewer': {'accounts': [{
        'r2OperationsAdaptiveGroups': [{'sum': {'requests': n},
        'dimensions': {'actionType': action}} for action, n in rows]}]}}}


class UsageTests(unittest.TestCase):
    def test_unknown_operations_count_against_both_budgets(self):
        result = summarize(METRICS, operations([('PutObject', 3), ('GetObject', 7),
                           ('NewAction', 2), ('DeleteObject', 20)]), NOW)
        self.assertEqual(result['storage_bytes'], 132)
        self.assertEqual(result['class_a_operations'], 5)
        self.assertEqual(result['class_b_operations'], 9)
        self.assertEqual(result['period_start'], '2026-10-21T00:00:00+00:00')

    def test_incomplete_or_partial_usage_never_becomes_zero(self):
        for data in ({}, {'success': False}, {'success': True, 'result': {}},
                     {'success': True, 'result': {'standard': {'published': {}}}}):
            with self.subTest(data=data), self.assertRaises(UsageUnavailable):
                summarize(data, operations([]), NOW)
        partial = operations([])
        partial['errors'] = [{'message': 'partial'}]
        with self.assertRaises(UsageUnavailable):
            summarize(METRICS, partial, NOW)

    def test_invalid_numeric_values_fail_closed(self):
        for value in (-1, True, float('nan'), 1.5, '0'):
            bad = copy.deepcopy(METRICS)
            bad['result']['standard']['published']['payloadSize'] = value
            with self.subTest(value=value), self.assertRaises(UsageUnavailable):
                summarize(bad, operations([]), NOW)

    def test_refresh_is_atomic_and_preserves_last_evidence_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'secret.json'
            config.write_text(json.dumps({'cloudflare_account_id': 'a' * 32,
                                          'cloudflare_api_token': 'synthetic-only'}))
            config.chmod(0o600)
            output = root / 'usage.json'
            def request(url, token, payload=None):
                return METRICS if payload is None else operations([('HeadObject', 4)])
            result = refresh_account_usage(config, output, request=request, now=NOW)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            old = output.read_bytes()
            with self.assertRaises(UsageUnavailable):
                refresh_account_usage(config, output, request=lambda *a: {}, now=NOW)
            self.assertEqual(output.read_bytes(), old)
            config.chmod(0o644)
            with self.assertRaises(UsageUnavailable):
                refresh_account_usage(config, output, request=request, now=NOW)


if __name__ == '__main__':
    unittest.main()
