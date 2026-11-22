import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import rag_version_backup as coordinator
from scripts.rag_versions import canonical
from tests.test_rag_backup import MemoryS3, _setup, _usage
from tests.test_rag_bundle import example


class ListingS3(MemoryS3):
    """S3 simulado que admite la única consulta de prefijo permitida."""

    def __init__(self):
        super().__init__()
        self.list_calls = []

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys=None, ContinuationToken=None):
        self.list_calls.append((Bucket, Prefix, MaxKeys))
        keys = sorted(key for bucket, key in self.objects
                      if bucket == Bucket and key.startswith(Prefix))
        page = keys[:MaxKeys or 1000]
        response = {'KeyCount': len(page), 'IsTruncated': len(keys) > len(page)}
        if page:
            response['Contents'] = [{'Key': key} for key in page]
        return response


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = int(time.time())

    def write_manifest(self, manifest=None, name='versioning'):
        manifest = manifest or example()
        directory = self.root / name / manifest['version']
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'manifest.json'
        path.write_bytes(canonical(manifest))
        return manifest, path

    def status_file(self, manifest, **extra):
        path = self.root / 'rag-status.json'
        value = {'phase': 'published', 'version': manifest['version'],
                 'operation': 'op-20261121', 'counts': {'chunks': 1000}}
        value.update(extra)
        path.write_text(json.dumps(value))
        return path

    def test_prepare_requires_empty_prefix_before_creating_catalog(self):
        config, _, _ = _setup(self.root, now=self.now)
        client = ListingS3()
        client.objects[('synthetic-private', 'rag-backup/v1/foreign.enc')] = b'x'
        with self.assertRaisesRegex(coordinator.PrefixNotEmpty, 'prefix_not_empty'):
            coordinator.prepare_remote(config, self.root / 'state', client=client)
        self.assertFalse((self.root / 'state' / 'remote-catalog.json').exists())

        client.objects.clear()
        result = coordinator.prepare_remote(config, self.root / 'state', client=client)
        self.assertEqual(result, {'status': 'ready', 'prefix': 'rag-backup/v1', 'objects': 0})
        catalog = json.loads((self.root / 'state' / 'remote-catalog.json').read_text())
        self.assertEqual(catalog['prefix'], 'rag-backup/v1')
        self.assertEqual(catalog['versions'], {})

    def test_prepare_refuses_to_reset_an_existing_catalog(self):
        config, _, _ = _setup(self.root, now=self.now)
        client = ListingS3()
        coordinator.prepare_remote(config, self.root / 'state', client=client)
        from scripts.rag_backup import BackupBlocked
        with self.assertRaises(BackupBlocked):
            coordinator.prepare_remote(config, self.root / 'state', client=client)

    def test_prepare_checks_fresh_usage_before_listing_or_reserving(self):
        config, usage, _ = _setup(self.root, now=self.now)
        usage.write_text(json.dumps(_usage(self.now, age=301)))
        client = ListingS3()

        from scripts.rag_backup import BackupBlocked
        with self.assertRaises(BackupBlocked):
            coordinator.prepare_remote(config, self.root / 'state', client=client, now=self.now)

        self.assertEqual(client.list_calls, [])
        self.assertFalse((self.root / 'state' / 'operation-reservations.jsonl').exists())
        self.assertFalse((self.root / 'state' / 'remote-catalog.json').exists())

    def test_prepare_reserves_each_list_retry_before_calling_r2(self):
        config, _, _ = _setup(self.root, now=self.now, retries=1)

        class RetryListingS3(ListingS3):
            def list_objects_v2(self, **kwargs):
                self.list_calls.append((kwargs['Bucket'], kwargs['Prefix'], kwargs['MaxKeys']))
                if len(self.list_calls) == 1:
                    raise OSError('synthetic timeout')
                return {'KeyCount': 0, 'IsTruncated': False}

        client = RetryListingS3()
        result = coordinator.prepare_remote(config, self.root / 'state', client=client, now=self.now)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(client.list_calls), 2)
        rows = [json.loads(line) for line in
                (self.root / 'state' / 'operation-reservations.jsonl').read_text().splitlines()]
        self.assertEqual([row['class'] for row in rows], ['A', 'A'])

    def test_bundle_is_reused_and_backup_is_idempotent(self):
        config, _, _ = _setup(self.root, now=self.now)
        manifest, path = self.write_manifest()
        client = ListingS3()
        coordinator.prepare_remote(config, self.root / 'state', client=client)

        first = coordinator.backup_version(path, config, self.root / 'state',
                                           self.root / 'bundles', client=client, now=self.now)
        self.assertEqual(first['status'], 'complete')
        self.assertFalse(first['bundle_reused'])
        self.assertEqual(first['version'], manifest['version'])
        self.assertTrue((self.root / 'bundles' / manifest['version'] / 'bundle.json').is_file())
        self.assertTrue(all(body.startswith(b'RAGAEAD1') for body in client.objects.values()))

        puts = len(client.puts)
        second = coordinator.backup_version(path, config, self.root / 'state',
                                            self.root / 'bundles', client=client, now=self.now)
        self.assertEqual(second['status'], 'complete')
        self.assertTrue(second['bundle_reused'])
        self.assertEqual(len(client.puts), puts)
        catalog = json.loads((self.root / 'state' / 'remote-catalog.json').read_text())
        self.assertIn(manifest['version'], catalog['versions'])

    def test_corrupt_bundle_is_not_reused_silently(self):
        config, _, _ = _setup(self.root, now=self.now)
        manifest, path = self.write_manifest()
        bundle = coordinator.bundle_version(path, self.root / 'bundles')
        shard = next((self.root / 'bundles' / manifest['version'] / 'rows').rglob('*.json'))
        shard.write_bytes(b'{}')
        with self.assertRaisesRegex(coordinator.CoordinatorError, 'bundle_unreadable'):
            coordinator.bundle_version(path, self.root / 'bundles')
        self.assertEqual(bundle['version'], manifest['version'])

    def test_bundle_reuse_rejects_tampered_expected_manifest(self):
        manifest, path = self.write_manifest()
        bundles = self.root / 'bundles'
        coordinator.bundle_version(path, bundles)
        tampered = json.loads(path.read_text())
        tampered['rows']['chunks']['chunk:0']['text'] = 'tampered but same version ID'
        path.write_bytes(canonical(tampered))

        with self.assertRaisesRegex(coordinator.CoordinatorError, 'manifest_invalid'):
            coordinator.bundle_version(path, bundles)

    def test_bundle_reuse_rejects_tampered_bundle_identity(self):
        manifest, path = self.write_manifest()
        bundles = self.root / 'bundles'
        coordinator.bundle_version(path, bundles)
        header_path = bundles / manifest['version'] / 'bundle.json'
        header = json.loads(header_path.read_text())
        header['manifest_head']['version'] = '0' * 64
        header_path.write_bytes(canonical(header))

        with self.assertRaisesRegex(coordinator.CoordinatorError, 'bundle_unreadable'):
            coordinator.bundle_version(path, bundles)

    def test_existing_bundle_is_reused_after_full_sequential_identity_validation(self):
        manifest, path = self.write_manifest()
        bundles = self.root / 'bundles'
        first = coordinator.bundle_version(path, bundles)
        second = coordinator.bundle_version(path, bundles)

        self.assertFalse(first['reused'])
        self.assertTrue(second['reused'])
        self.assertEqual(second['version'], manifest['version'])

    def test_backup_current_records_status_without_losing_publication_fields(self):
        config, _, _ = _setup(self.root, now=self.now)
        manifest, _ = self.write_manifest()
        status = self.status_file(manifest)
        client = ListingS3()
        coordinator.prepare_remote(client=client, config_path=config, state_dir=self.root / 'state')

        result = coordinator.backup_current(status, self.root / 'versioning', config,
                                            self.root / 'state', self.root / 'bundles',
                                            client=client, now=self.now)
        self.assertEqual(result['status'], 'complete')
        recorded = json.loads(status.read_text())
        self.assertEqual(recorded['backup'], 'complete')
        self.assertEqual(recorded['backup_bytes'], result['bytes'])
        self.assertEqual(recorded['backup_operations'], result['operations'])
        self.assertEqual(recorded['phase'], 'published')
        self.assertEqual(recorded['operation'], 'op-20261121')
        self.assertEqual(recorded['counts'], {'chunks': 1000})
        self.assertEqual(recorded['version'], manifest['version'])

    def test_blocked_backup_is_recorded_as_blocked_and_keeps_previous_state(self):
        config, usage, _ = _setup(self.root, now=self.now)
        manifest, _ = self.write_manifest()
        status = self.status_file(manifest)
        client = ListingS3()
        coordinator.prepare_remote(config, self.root / 'state', client=client)
        usage.write_text(json.dumps(_usage(self.now, age=301)))

        result = coordinator.backup_current(status, self.root / 'versioning', config,
                                            self.root / 'state', self.root / 'bundles',
                                            client=client, now=self.now)
        self.assertEqual(result['status'], 'blocked')
        recorded = json.loads(status.read_text())
        self.assertEqual(recorded['backup'], 'blocked')
        self.assertEqual(recorded['phase'], 'published')
        self.assertFalse(client.puts)

    def test_backup_current_requires_a_published_version(self):
        config, _, _ = _setup(self.root, now=self.now)
        status = self.root / 'rag-status.json'
        status.write_text(json.dumps({'phase': 'prepared'}))
        with self.assertRaisesRegex(coordinator.CoordinatorError, 'published_version_unknown'):
            coordinator.backup_current(status, self.root / 'versioning', config,
                                       self.root / 'state', self.root / 'bundles', now=self.now)

    def test_restore_rebuilds_the_manifest_from_ciphertext(self):
        config, _, _ = _setup(self.root, now=self.now)
        manifest, path = self.write_manifest()
        client = ListingS3()
        coordinator.prepare_remote(config, self.root / 'state', client=client)
        self.assertEqual(
            coordinator.backup_version(path, config, self.root / 'state', self.root / 'bundles',
                                       client=client, now=self.now)['status'], 'complete'
        )

        manifest_out = self.root / 'recovered' / 'manifest.json'
        result = coordinator.restore_version(manifest['version'], config, self.root / 'state',
                                             self.root / 'restored', manifest_out,
                                             client=client, now=self.now)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['manifest_version'], manifest['version'])
        self.assertEqual(json.loads(manifest_out.read_text()), manifest)
        self.assertEqual(manifest_out.read_bytes(), canonical(manifest))

    def test_restore_failure_does_not_write_a_manifest(self):
        config, _, _ = _setup(self.root, now=self.now)
        manifest, path = self.write_manifest()
        client = ListingS3()
        coordinator.prepare_remote(config, self.root / 'state', client=client)
        coordinator.backup_version(path, config, self.root / 'state', self.root / 'bundles',
                                   client=client, now=self.now)
        object_key = next(key for bucket, key in client.objects if '/objects/' in key)
        corrupted = bytearray(client.objects[('synthetic-private', object_key)])
        corrupted[-1] ^= 0x01
        client.objects[('synthetic-private', object_key)] = bytes(corrupted)

        manifest_out = self.root / 'recovered' / 'manifest.json'
        result = coordinator.restore_version(manifest['version'], config, self.root / 'state',
                                             self.root / 'restored', manifest_out,
                                             client=client, now=self.now)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(manifest_out.exists())
        self.assertFalse((self.root / 'restored').exists())

    def test_protect_pins_current_previous_and_pending_in_both_catalogs(self):
        config, _, _ = _setup(self.root, now=self.now)
        first, first_path = self.write_manifest(example('current'))
        second, second_path = self.write_manifest(example('previous'))
        self.assertNotEqual(first['version'], second['version'])
        client = ListingS3()
        state = self.root / 'state'
        coordinator.prepare_remote(config, state, client=client)
        for path in (first_path, second_path):
            self.assertEqual(
                coordinator.backup_version(path, config, state, self.root / 'bundles',
                                           client=client, now=self.now)['status'], 'complete'
            )

        pinned = coordinator.protect_versions(state, current=first['version'],
                                              previous=second['version'])
        self.assertEqual(pinned['current'], first['version'])
        remote = json.loads((state / 'remote-protected.json').read_text())
        self.assertEqual(remote, {'current': first['version'], 'previous': second['version'],
                                  'pending': []})
        local = json.loads((state / 'local' / 'protected.json').read_text())
        self.assertEqual(local['current'], first['version'])
        self.assertEqual(local['previous'], second['version'])

    def test_retain_local_keeps_pinned_current_and_expires_old_snapshots(self):
        from scripts.rag_storage import RAGStore, SpacePolicy

        state = self.root / 'state'
        state.mkdir(mode=0o700)
        store = RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0))
        old = self.root / 'old'
        old.mkdir()
        (old / 'old.jsonl').write_bytes(b'old synthetic object')
        current = self.root / 'current'
        current.mkdir()
        (current / 'current.jsonl').write_bytes(b'current synthetic object')
        store.create_snapshot(old, 'old')
        store.create_snapshot(current, 'current')
        manifest_path = store._snapshot_file('old')
        manifest = json.loads(manifest_path.read_text())
        manifest['created_at'] = self.now - 100 * 86400
        manifest_path.write_text(json.dumps(manifest))

        result = coordinator.retain_local(state, current='current', retention_days=90, now=self.now)
        self.assertTrue(result['pinned'])
        self.assertEqual(result['removed_snapshots'], 1)
        self.assertTrue(store._snapshot_file('current').exists())
        self.assertFalse(store._snapshot_file('old').exists())
        self.assertEqual(store.restore('current', self.root / 'restored')['status'], 'complete')

    def test_retain_local_pin_failure_fails_closed_before_prune_or_compaction(self):
        from scripts.rag_storage import RAGStore, SpacePolicy

        state = self.root / 'state'
        state.mkdir(mode=0o700)
        store = RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0))
        old = self.root / 'old'
        old.mkdir()
        old_payload = b'old synthetic current version'
        (old / 'old.jsonl').write_bytes(old_payload)
        store.create_snapshot(old, 'old')
        manifest_path = store._snapshot_file('old')
        manifest = json.loads(manifest_path.read_text())
        manifest['created_at'] = self.now - 100 * 86400
        manifest_path.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(coordinator.CoordinatorError, 'version_protection_failed'):
            coordinator.retain_local(state, current='old', previous='missing-previous',
                                     retention_days=90, now=self.now)
        self.assertTrue(store._snapshot_file('old').exists())
        self.assertTrue(store._object_path(store._read_manifest('old')['files'][0]['sha256']).exists())
        restored = self.root / 'restored-after-pin-failure'
        self.assertEqual(store.restore('old', restored)['status'], 'complete')
        self.assertEqual((restored / 'old.jsonl').read_bytes(), old_payload)

    def test_retain_cli_requires_stable_known_head_before_any_cleanup(self):
        from scripts.rag_storage import RAGStore, SpacePolicy
        import contextlib
        import io

        state = self.root / 'state'
        state.mkdir(mode=0o700)
        store = RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0))
        old = self.root / 'old'
        old.mkdir()
        (old / 'fixture').write_bytes(b'old synthetic data')
        store.create_snapshot(old, 'old')
        old_manifest = store._snapshot_file('old')
        data = json.loads(old_manifest.read_text())
        data['created_at'] = self.now - 100 * 86400
        old_manifest.write_text(json.dumps(data))
        status = self.root / 'unstable-status.json'
        status.write_text(json.dumps({'phase': 'applying', 'published_version': 'old',
                                      'version': 'candidate'}))
        output = io.StringIO()
        argv = ['rag_version_backup', 'retain', '--status', str(status),
                '--state-dir', str(state)]
        with patch('sys.argv', argv), contextlib.redirect_stdout(output):
            self.assertEqual(coordinator.main(), 1)
        self.assertIn('publication_phase_not_stable', output.getvalue())
        self.assertTrue(store._snapshot_file('old').exists())

    def test_status_pins_use_published_version_and_union_existing_pending(self):
        state = self.root / 'state'
        (state / 'local').mkdir(parents=True, mode=0o700)
        state.chmod(0o700)
        (state / 'local' / 'protected.json').write_text(json.dumps({
            'current': 'old-current', 'previous': 'old-previous', 'pending': ['unfinished']
        }))
        status = {'phase': 'published', 'published_version': 'new-current',
                  'version': 'legacy-target', 'previous_version': 'old-previous'}

        pins = coordinator._status_pins(status, state)

        self.assertEqual(pins, {'current': 'new-current', 'previous': 'old-previous',
                                'pending': ['unfinished']})

    def test_terminal_failed_and_reverted_phases_keep_the_known_published_head(self):
        state = self.root / 'state'
        for phase in ('failed', 'reverted'):
            with self.subTest(phase=phase):
                pins = coordinator._status_pins({
                    'phase': phase, 'published_version': 'published-head',
                    'version': 'operation-target', 'previous_version': None,
                    'pending_versions': [],
                }, state)
                self.assertEqual(pins['current'], 'published-head')
        with self.assertRaisesRegex(coordinator.CoordinatorError, 'publication_phase_not_stable'):
            coordinator._status_pins({
                'phase': 'recovery_required', 'published_version': 'published-head',
                'version': 'operation-target',
            }, state)
        sync_failed = {
            'phase': 'sync_failed', 'published_version': 'published-head',
            'previous_version': 'published-previous', 'pending_versions': [],
        }
        self.assertEqual(coordinator._status_pins(sync_failed, state)['current'],
                         'published-head')
        with self.assertRaisesRegex(coordinator.CoordinatorError,
                                    'publication_phase_not_stable'):
            coordinator._status_pins({
                **sync_failed, 'pending_versions': ['unresolved-operation'],
            }, state)

    def test_run_cycle_keeps_current_previous_and_pending_across_both_retention_passes(self):
        import os

        config, usage, _ = _setup(self.root, now=self.now)
        versions = {}
        manifest_paths = {}
        for name in ('current', 'previous', 'pending', 'expired'):
            versions[name] = example(name)
            _, manifest_paths[name] = self.write_manifest(versions[name])
        client = ListingS3()
        state = self.root / 'state'
        coordinator.prepare_remote(config, state, client=client, now=self.now)
        for name in ('previous', 'pending', 'expired'):
            result = coordinator.backup_version(manifest_paths[name], config, state,
                                                self.root / 'bundles', client=client, now=self.now)
            self.assertEqual(result['status'], 'complete')

        catalog_path = state / 'remote-catalog.json'
        catalog = json.loads(catalog_path.read_text())
        old_ids = [versions[name]['version'] for name in ('previous', 'pending', 'expired')]
        for name, version_id in zip(('previous', 'pending', 'expired'), old_ids):
            catalog['versions'][version_id]['created_at'] = self.now - 100 * 86400
            snapshot = state / 'local' / 'snapshots' / f'{version_id}.json'
            snapshot_data = json.loads(snapshot.read_text())
            snapshot_data['created_at'] = self.now - 100 * 86400
            snapshot.write_text(json.dumps(snapshot_data))
        catalog_path.write_text(json.dumps(catalog))
        expired_id = versions['expired']['version']
        old_time = self.now - 100 * 86400
        for managed in (self.root / 'versioning' / expired_id,
                        self.root / 'bundles' / expired_id):
            for path in managed.rglob('*'):
                if path.is_file():
                    os.utime(path, (old_time, old_time))
            os.utime(managed, (old_time, old_time))

        status = self.status_file(versions['current'], previous_version=versions['previous']['version'],
                                  pending_versions=[versions['pending']['version']])

        def refresh(_config, _credentials, *, now):
            usage.write_text(json.dumps(_usage(now)))

        with patch.object(coordinator, '_refresh_usage', side_effect=refresh) as refresh_mock:
            result = coordinator.run_cycle(status, self.root / 'versioning', config, state,
                                           self.root / 'bundles', usage_credentials=self.root / 'usage.json',
                                           client=client, now=self.now)

        self.assertEqual(result['status'], 'complete')
        refresh_mock.assert_called_once()
        current_id = versions['current']['version']
        self.assertTrue((state / 'local' / 'snapshots' / f'{current_id}.json').exists())
        for name in ('previous', 'pending'):
            version_id = versions[name]['version']
            self.assertTrue((state / 'local' / 'snapshots' / f'{version_id}.json').exists())
            self.assertIn(version_id, json.loads((state / 'remote-protected.json').read_text())['pending']
                          + [json.loads((state / 'remote-protected.json').read_text())['previous']])
        self.assertTrue((state / 'local' / 'snapshots' / f'{expired_id}.json').exists())
        self.assertFalse((self.root / 'bundles' / expired_id).exists())
        self.assertFalse((self.root / 'versioning' / expired_id).exists())
        self.assertEqual(json.loads((state / 'local' / 'snapshots' / f'{expired_id}.json').read_text())
                         ['created_at'], self.now)
        self.assertNotIn(versions['expired']['version'],
                         json.loads((state / 'remote-catalog.json').read_text())['versions'])
        recorded = json.loads(status.read_text())
        self.assertEqual(recorded['version'], current_id)
        self.assertEqual(recorded['backup'], 'complete')
        self.assertEqual(recorded['previous_version'], versions['previous']['version'])

    def test_run_cycle_refresh_failure_never_uses_stale_usage_or_cleans_up(self):
        from scripts.rag_storage import RAGStore, SpacePolicy

        config, _usage_file, _ = _setup(self.root, now=self.now)
        manifest, _ = self.write_manifest()
        status = self.status_file(manifest)
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        store = RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0))
        old = self.root / 'old'
        old.mkdir()
        (old / 'fixture').write_bytes(b'old synthetic snapshot')
        store.create_snapshot(old, 'old')
        old_path = store._snapshot_file('old')
        old_data = json.loads(old_path.read_text())
        old_data['created_at'] = self.now - 100 * 86400
        old_path.write_text(json.dumps(old_data))
        client = ListingS3()

        with patch.object(coordinator, '_refresh_usage', side_effect=OSError('synthetic failure')):
            result = coordinator.run_cycle(status, self.root / 'versioning', config, state,
                                           self.root / 'bundles', client=client, now=self.now)

        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['cleanup'], 'skipped')
        self.assertFalse(client.puts)
        self.assertTrue(store._snapshot_file('old').exists())
        self.assertEqual(json.loads(status.read_text())['backup'], 'blocked')

    def test_run_cycle_requires_real_previous_snapshot_and_records_retention_failure(self):
        from scripts import rag_backup
        from scripts.rag_storage import RAGStore, SpacePolicy

        config, usage, _ = _setup(self.root, now=self.now)
        current, _ = self.write_manifest(example('cycle-current'))
        previous, _ = self.write_manifest(example('cycle-previous'))
        status = self.status_file(
            current, previous_version=previous['version'], pending_versions=[])
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        source = self.root / 'local-current'
        source.mkdir()
        (source / 'fixture').write_bytes(b'synthetic local current snapshot')
        RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0)).create_snapshot(
            source, current['version'])

        def refresh(_config, _credentials, *, now):
            usage.write_text(json.dumps(_usage(now)))

        complete = {'status': 'complete', 'version': current['version'],
                    'bytes': 123, 'operations': 1,
                    'at': '2026-11-21T00:00:00+00:00'}
        with patch.object(coordinator, '_refresh_usage', side_effect=refresh), \
                patch.object(coordinator, 'backup_current', return_value=complete), \
                patch.object(rag_backup, 'prune_remote') as prune_remote:
            result = coordinator.run_cycle(
                status, self.root / 'versioning', config, state, self.root / 'bundles',
                client=ListingS3(), now=self.now)

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['backup']['reason'], 'local_retention_failed')
        self.assertEqual(result['backup']['bytes'], 123)
        prune_remote.assert_not_called()
        recorded = json.loads(status.read_text())
        self.assertEqual(recorded['backup'], 'failed')
        self.assertEqual(recorded['backup_bytes'], 123)
        self.assertEqual(recorded['phase'], 'published')
        self.assertEqual(recorded['version'], current['version'])
        self.assertFalse((state / 'local' / 'snapshots' /
                          f'{previous["version"]}.json').exists())

    def test_run_cycle_passes_both_managed_roots_to_retention(self):
        from scripts import rag_backup

        config, usage, _ = _setup(self.root, now=self.now)
        manifest, _ = self.write_manifest(example('cycle-roots'))
        status = self.status_file(
            manifest, phase='sync_failed', published_version=manifest['version'],
            previous_version=None, pending_versions=[])
        state = self.root / 'state'
        state.mkdir(mode=0o700)

        def refresh(_config, _credentials, *, now):
            usage.write_text(json.dumps(_usage(now)))

        with patch.object(coordinator, '_refresh_usage', side_effect=refresh), \
                patch.object(coordinator, 'backup_current', return_value={
                    'status': 'complete', 'version': manifest['version'], 'bytes': 1,
                    'operations': 1, 'at': '2026-11-21T00:00:00+00:00'}), \
                patch.object(coordinator, 'retain_local', return_value={
                    'pinned': True, 'removed_bundles': [], 'removed_versions': []}) as retain, \
                patch.object(rag_backup, 'prune_remote', return_value={'status': 'complete'}):
            result = coordinator.run_cycle(
                status, self.root / 'versioning', config, state, self.root / 'bundles',
                client=ListingS3(), now=self.now)

        self.assertEqual(result['status'], 'complete')
        self.assertEqual(retain.call_args.kwargs['version_root'], self.root / 'versioning')
        self.assertEqual(retain.call_args.kwargs['bundles_root'], self.root / 'bundles')
        recorded = json.loads(status.read_text())
        self.assertEqual(recorded['phase'], 'sync_failed')

    def test_backup_current_does_not_annotate_a_changed_publication(self):
        from scripts.rag_storage import RAGStore, SpacePolicy

        manifest, path = self.write_manifest()
        status = self.status_file(manifest)
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        local_source = self.root / 'local-source'
        local_source.mkdir()
        (local_source / 'synthetic').write_bytes(b'synthetic backup fixture')
        RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0)).create_snapshot(
            local_source, manifest['version'])
        uploaded = {'status': 'complete', 'version': manifest['version'], 'bytes': 10,
                    'operations': 1, 'at': '2026-11-21T00:00:00+00:00'}

        def publish_new_phase(*args, **kwargs):
            status.write_text(json.dumps({'phase': 'applying', 'version': 'candidate',
                                          'published_version': manifest['version'],
                                          'operation': 'next-operation'}))
            return uploaded

        with patch.object(coordinator, 'backup_version', side_effect=publish_new_phase):
            result = coordinator.backup_current(status, self.root / 'versioning', 'unused',
                                                state, 'unused-bundles', now=self.now)

        written = json.loads(status.read_text())
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['reason'], 'publication_changed_after_backup')
        self.assertEqual(written['phase'], 'applying')
        self.assertEqual(written['version'], 'candidate')
        self.assertEqual(written['backup'], 'pending')
        self.assertEqual(written['backup_version'], manifest['version'])
        local_pins = json.loads((state / 'local' / 'protected.json').read_text())
        remote_pins = json.loads((state / 'remote-protected.json').read_text())
        self.assertIn(manifest['version'], local_pins['pending'])
        self.assertIn(manifest['version'], remote_pins['pending'])

    def test_status_update_uses_the_publication_shared_lock_contract(self):
        from scripts import rag_publish

        manifest, _ = self.write_manifest()
        status = self.status_file(manifest)
        locks = []

        def update_status(path, transform):
            locks.append(Path(path))
            helper = getattr(rag_publish, '_status_lock', None)
            if helper is None:
                with coordinator._status_lock(path, rag_publish):
                    value = coordinator._read_status(path)
                    value.update(transform(value))
                    rag_publish._save_status(path, value)
                    return value
            with helper(path):
                value = rag_publish.read_status(path)
                value.update(transform(value))
                rag_publish._save_status(path, value)
                return value

        with patch.object(rag_publish, 'update_status', update_status, create=True):
            recorded = coordinator.record_backup(
                status, {'status': 'complete', 'version': manifest['version'],
                         'bytes': 20, 'operations': 2,
                         'at': '2026-11-21T00:00:00+00:00'},
                expected_version=manifest['version'], expected_operation='op-20261121')

        self.assertTrue(recorded)
        self.assertEqual(locks, [status])
        value = json.loads(status.read_text())
        self.assertEqual(value['version'], manifest['version'])
        self.assertEqual(value['backup_version'], manifest['version'])
        self.assertEqual(value['backup'], 'complete')

    def test_status_writer_keeps_backup_scalar_for_the_monitor(self):
        from scripts.rag_publish import merge_status, write_status

        path = self.root / 'rag-status.json'
        write_status(path, phase='published', version='v1', operation_id='op-1',
                     counts={'chunks': 3},
                     backup={'status': 'complete', 'bytes': 10, 'operations': 2,
                             'finished_at': '2026-11-21T21:00:00+00:00'})
        value = json.loads(path.read_text())
        self.assertEqual(value['backup'], 'complete')
        self.assertEqual(value['backup_bytes'], 10)
        self.assertEqual(value['backup_operations'], 2)
        self.assertEqual(value['backup_at'], '2026-11-21T21:00:00+00:00')

        merged = merge_status(path, backup='pending')
        self.assertEqual(merged['phase'], 'published')
        self.assertEqual(merged['operation'], 'op-1')
        self.assertEqual(merged['counts'], {'chunks': 3})
        self.assertEqual(merged['backup'], 'pending')
        self.assertEqual(merged['backup_bytes'], 10)

    def test_prune_versions_keeps_protected_and_recent_directories(self):
        import os

        version_root = self.root / 'versioning'
        protected = version_root / ('a' * 64)
        protected.mkdir(parents=True)
        (protected / 'manifest.json').write_text('{}')
        old = version_root / ('b' * 64)
        old.mkdir()
        (old / 'manifest.json').write_text('{}')
        old_time = self.now - 100 * 86400
        os.utime(old, (old_time, old_time))
        recent = version_root / ('c' * 64)
        recent.mkdir()
        (recent / 'manifest.json').write_text('{}')
        (version_root / 'not-a-version').mkdir()

        with self.assertRaisesRegex(coordinator.CoordinatorError,
                                    'archive_verification_required'):
            coordinator.prune_versions(version_root, keep=[protected.name],
                                       retention_days=90, now=self.now)
        self.assertTrue(protected.exists())
        self.assertTrue(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue((version_root / 'not-a-version').exists())

    def test_prune_versions_refuses_trees_with_symlinks(self):
        import os

        version_root = self.root / 'versioning'
        old = version_root / ('d' * 64)
        old.mkdir(parents=True)
        (old / 'manifest.json').write_text('{}')
        outside = self.root / 'outside.txt'
        outside.write_text('secret')
        (old / 'link').symlink_to(outside)
        old_time = self.now - 100 * 86400
        os.utime(old, (old_time, old_time))

        with self.assertRaisesRegex(coordinator.CoordinatorError, 'version_tree_insecure'):
            coordinator.prune_versions(version_root, retention_days=90, now=self.now)
        self.assertTrue(old.exists())
        self.assertTrue(outside.exists())

    def test_retain_end_to_end_reports_versions_and_bundles(self):
        config, _, _ = _setup(self.root, now=self.now)
        manifest, path = self.write_manifest()
        client = ListingS3()
        state = self.root / 'state'
        coordinator.prepare_remote(config, state, client=client)
        coordinator.backup_version(path, config, state, self.root / 'bundles',
                                   client=client, now=self.now)

        result = coordinator.retain_local(state, current=manifest['version'],
                                          version_root=self.root / 'versioning',
                                          bundles_root=self.root / 'bundles',
                                          retention_days=90, now=self.now)
        self.assertTrue(result['pinned'])
        self.assertEqual(result['removed_versions'], [])
        self.assertEqual(result['removed_bundles'], [])
        self.assertTrue((self.root / 'bundles' / manifest['version']).is_dir())

    def test_retain_archives_and_verifies_old_bundle_before_removing_managed_sources(self):
        import os
        from scripts.rag_storage import RAGStore, SpacePolicy

        old_manifest, old_path = self.write_manifest(example('old-managed'))
        current_manifest, current_path = self.write_manifest(example('current-managed'))
        bundles = self.root / 'bundles'
        old_bundle = coordinator.bundle_version(old_path, bundles)
        current_bundle = coordinator.bundle_version(current_path, bundles)
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        store = RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(old_bundle['path'], old_manifest['version'])
        store.create_snapshot(current_bundle['path'], current_manifest['version'])
        store.protect(current=current_manifest['version'], previous=None)
        raw_original = self.root / 'educational-original.pdf'
        raw_original.write_bytes(b'synthetic original; outside managed roots')
        old_run = self.root / 'runs' / 'old-run'
        old_run.mkdir(parents=True)
        (old_run / 'derived-index.bin').write_bytes(b'synthetic run artifact')
        old_snapshot = store._snapshot_file(old_manifest['version'])
        snapshot_data = json.loads(old_snapshot.read_text())
        snapshot_data['created_at'] = self.now - 100 * 86400
        old_snapshot.write_text(json.dumps(snapshot_data))
        old_time = self.now - 100 * 86400
        for managed in (self.root / 'versioning' / old_manifest['version'],
                        bundles / old_manifest['version']):
            for path in managed.rglob('*'):
                if path.is_file():
                    os.utime(path, (old_time, old_time))
            os.utime(managed, (old_time, old_time))

        result = coordinator.retain_local(
            state, current=current_manifest['version'],
            version_root=self.root / 'versioning', bundles_root=bundles,
            retention_days=90, now=self.now)

        self.assertEqual(result['removed_bundles'], [old_manifest['version']])
        self.assertEqual(result['removed_versions'], [old_manifest['version']])
        self.assertFalse((bundles / old_manifest['version']).exists())
        self.assertFalse((self.root / 'versioning' / old_manifest['version']).exists())
        self.assertTrue(store._snapshot_file(old_manifest['version']).exists())
        self.assertEqual(store._read_manifest(old_manifest['version'])['created_at'], self.now)
        restored = self.root / 'verified-old-bundle'
        store.restore(old_manifest['version'], restored)
        self.assertEqual(
            coordinator._modules()[1].read_bundle(restored), old_manifest)
        self.assertTrue((bundles / current_manifest['version']).exists())
        self.assertTrue((self.root / 'versioning' / current_manifest['version']).exists())
        self.assertEqual(raw_original.read_bytes(), b'synthetic original; outside managed roots')
        self.assertEqual((old_run / 'derived-index.bin').read_bytes(), b'synthetic run artifact')

    def test_retain_keeps_unarchived_old_bundle_and_fails_before_deletion(self):
        import os

        manifest, path = self.write_manifest(example('unarchived'))
        current, current_path = self.write_manifest(example('retention-head'))
        bundle = coordinator.bundle_version(path, self.root / 'bundles')
        current_bundle = coordinator.bundle_version(current_path, self.root / 'bundles')
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        # The protected head and old candidate both have local snapshots.
        from scripts.rag_storage import RAGStore, SpacePolicy
        store = RAGStore(state / 'local', space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(bundle['path'], manifest['version'])
        store.create_snapshot(current_bundle['path'], current['version'])
        store.protect(current=current['version'], previous=None)
        old_time = self.now - 100 * 86400
        for managed_file in Path(bundle['path']).rglob('*'):
            if managed_file.is_file():
                os.utime(managed_file, (old_time, old_time))
        os.utime(Path(bundle['path']), (old_time, old_time))
        version_dir = self.root / 'versioning' / manifest['version']
        os.utime(version_dir / 'manifest.json', (old_time, old_time))
        os.utime(version_dir, (old_time, old_time))
        # Corrupt the local archive after snapshot creation.
        snapshot_data = json.loads(store._snapshot_file(manifest['version']).read_text())
        snapshot_data['files'][0]['sha256'] = '0' * 64
        store._snapshot_file(manifest['version']).write_text(json.dumps(snapshot_data))

        with self.assertRaisesRegex(coordinator.CoordinatorError,
                                    'managed_archive_retention_failed'):
            coordinator.retain_local(
                state, current=current['version'], version_root=self.root / 'versioning',
                bundles_root=self.root / 'bundles', retention_days=90, now=self.now)
        self.assertTrue(Path(bundle['path']).exists())
        self.assertTrue(version_dir.exists())

    def test_cli_reports_prefix_conflict_without_leaking_configuration(self):
        from scripts import rag_backup
        config, _, _ = _setup(self.root, now=self.now)
        client = ListingS3()
        client.objects[('synthetic-private', 'rag-backup/v1/x.enc')] = b'x'
        argv = ['rag_version_backup', 'prepare', '--config', str(config),
                '--state-dir', str(self.root / 'state')]
        import contextlib
        import io
        output = io.StringIO()
        with patch('sys.argv', argv), patch.object(rag_backup, '_new_s3_client', return_value=client), \
                contextlib.redirect_stdout(output):
            self.assertEqual(coordinator.main(), 1)
        printed = output.getvalue()
        self.assertIn('prefix_not_empty', printed)
        self.assertNotIn('synthetic-secret', printed)


if __name__ == '__main__':
    unittest.main()
