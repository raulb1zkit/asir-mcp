"""Preflight de espacio del sincronizador: estimación, umbrales y orden de ejecución."""
import io
import contextlib
import os
from collections import namedtuple
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import sync_materials as sync

FakeStatvfs = namedtuple('statvfs', 'f_bsize f_frsize f_blocks f_bfree f_bavail '
                                    'f_files f_ffree f_favail f_flag f_namemax')


class EstimationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_estimate_measures_base_and_margin(self):
        directory = self.base / 'prior'
        directory.mkdir()
        (directory / 'artifacts.jsonl').write_bytes(b'x' * 1000)
        estimate = sync.estimate_preflight_bytes([directory], margin_percent=50, minimum_bytes=100)
        self.assertEqual(estimate, 1500)

    def test_estimate_never_zero_without_base(self):
        estimate = sync.estimate_preflight_bytes([self.base / 'missing'], margin_percent=50, minimum_bytes=2048)
        self.assertEqual(estimate, 2048)

    def test_estimate_min_floor_even_with_small_base(self):
        directory = self.base / 'tiny'
        directory.mkdir()
        (directory / 'f').write_bytes(b'x' * 10)
        estimate = sync.estimate_preflight_bytes([directory], margin_percent=0, minimum_bytes=4096)
        self.assertEqual(estimate, 4096)

    def test_negative_parameters_rejected(self):
        with self.assertRaisesRegex(sync.SyncFailure, 'preflight_parameters_invalid'):
            sync.estimate_preflight_bytes([], margin_percent=-1)

    def test_symlinked_files_are_not_followed(self):
        directory = self.base / 'prior'
        directory.mkdir()
        (directory / 'f').write_bytes(b'x' * 10)
        outside = self.base / 'outside'
        outside.write_bytes(b'y' * 10 ** 6)
        (directory / 'link').symlink_to(outside)
        estimate = sync.estimate_preflight_bytes([directory], margin_percent=0, minimum_bytes=0)
        self.assertEqual(estimate, 10)


class SpacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def statvfs(self, *, total, free):
        frsize = 4096
        return lambda path: FakeStatvfs(4096, frsize, total // frsize, free // frsize,
                                        free // frsize, 0, 0, 0, 0, 255)

    def test_healthy_filesystem_allowed_without_warning(self):
        with patch.object(os, 'statvfs', self.statvfs(total=100 * 1024**3, free=35 * 1024**3)):
            result = sync.space_preflight(self.root, 4 * 1024**3)
        self.assertTrue(result['allowed'])
        self.assertFalse(result['warning'])

    def test_warn_zone_records_warning_but_allows(self):
        # Tras estimar: ~72 % ocupado y reserva absoluta sobrada.
        with patch.object(os, 'statvfs', self.statvfs(total=100 * 1024**3, free=29 * 1024**3)):
            result = sync.space_preflight(self.root, 2 * 1024**3)
        self.assertTrue(result['allowed'])
        self.assertTrue(result['warning'])

    def test_block_over_85_percent(self):
        # Tras estimar: ~86 % ocupado.
        with patch.object(os, 'statvfs', self.statvfs(total=100 * 1024**3, free=15 * 1024**3)):
            with self.assertRaisesRegex(sync.SyncFailure, 'disk_preflight_blocked'):
                sync.space_preflight(self.root, 2 * 1024**3)

    def test_block_when_free_below_reserve(self):
        # Porcentaje bajo pero el estimado deja menos de 5 GiB de reserva final.
        with patch.object(os, 'statvfs', self.statvfs(total=1000 * 1024**3, free=6 * 1024**3)):
            with self.assertRaisesRegex(sync.SyncFailure, 'disk_preflight_blocked'):
                sync.space_preflight(self.root, 2 * 1024**3)

    def test_nonpositive_estimate_rejected(self):
        with self.assertRaisesRegex(sync.SyncFailure, 'preflight_estimate_required'):
            sync.space_preflight(self.root, 0)


class OrderingTests(unittest.TestCase):
    def test_preflight_blocks_before_download_and_build(self):
        order = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            def failing_preflight(path, estimate):
                order.append('preflight')
                raise sync.SyncFailure('disk_preflight_blocked')
            def build(*args, **kwargs):
                order.append('build')
                return state / 'unused'
            argv = ['sync_materials.py', '--execute', '--staging', '--rag-delta-publish',
                    '--rag-resource-map', str(state / 'map.json'), '--state-dir', str(state),
                    '--registry', str(state / 'missing-courses.json')]
            with patch('sys.argv', argv), patch.object(sync, 'space_preflight', failing_preflight), \
                    patch.object(sync, 'build', build), \
                    patch('scripts.course_registry.load_registry', lambda *a, **k: []), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                result = sync.main()
            self.assertEqual(result, 1)
            self.assertEqual(order, ['preflight'])
            self.assertIn('disk_preflight_blocked', output.getvalue())

    def test_second_preflight_runs_before_embeddings(self):
        order = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'courses.json').write_text('[]')
            course = type('Course', (), {'subject': 'ASO', 'slug': 'aso',
                                         'course_url': 'https://x/course/view.php?id=1',
                                         'preserve_sources': []})()
            def preflight(path, estimate):
                order.append('preflight')
                return {'allowed': True, 'warning': False}
            def build(*args, **kwargs):
                order.append('build')
                return state / 'chunks'
            def build_vectors(*args, **kwargs):
                order.append('embeddings')
                raise sync.SyncFailure('stop_here')
            argv = ['sync_materials.py', '--execute', '--rag-delta-publish',
                    '--rag-resource-map', str(state / 'map.json'), '--state-dir', str(state),
                    '--registry', str(state / 'courses.json')]
            with patch('sys.argv', argv), patch.object(sync, 'space_preflight', preflight), \
                    patch('scripts.course_registry.load_registry', lambda *a, **k: [course]), \
                    patch.object(sync, 'Runner') as runner, \
                    patch.object(sync, 'build', build), \
                    patch.object(sync, 'incremental_summary', lambda *a, **k: {}), \
                    patch.object(sync, 'unchanged_corpus', lambda *a, **k: False), \
                    patch.object(sync, 'build_vectors', build_vectors), \
                    contextlib.redirect_stderr(io.StringIO()):
                runner.return_value.run.side_effect = lambda argv, timeout=14400: order.append('subprocess')
                result = sync.main()
            self.assertEqual(result, 1)
            # Preflight de arranque antes de cualquier paso (incluida la red);
            # segundo preflight antes de inferir embeddings.
            self.assertEqual(order[0], 'preflight')
            self.assertLess(order.index('preflight'), order.index('subprocess'))
            second = order.index('preflight', order.index('preflight') + 1)
            self.assertLess(second, order.index('embeddings'))


if __name__ == '__main__':
    unittest.main()
