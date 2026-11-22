"""Estado compacto rag-status.json: lock común, campos canónicos, CAS y concurrencia."""
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from scripts.rag_publish import (StatusConflict, merge_status, read_status, update_status,
                                 write_status)

class StatusSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'rag-status.json'

    def test_prepared_and_applying_preserve_published_and_previous(self):
        write_status(self.path, phase='published', version='v1', operation_id='op1',
                     published_version='v1', previous_version='v0')
        write_status(self.path, phase='prepared', version='v2', operation_id='op2')
        value = read_status(self.path)
        self.assertEqual(value['published_version'], 'v1')
        self.assertEqual(value['previous_version'], 'v0')
        self.assertEqual(value['version'], 'v2')
        self.assertEqual(value['pending_versions'], ['v2'])

    def test_pending_accumulates_and_resolves(self):
        write_status(self.path, phase='prepared', version='v2', operation_id='op2')
        write_status(self.path, phase='applying', version='v2', operation_id='op2')
        self.assertEqual(read_status(self.path)['pending_versions'], ['v2'])
        write_status(self.path, phase='published', version='v2', operation_id='op2',
                     published_version='v2', previous_version='v1')
        value = read_status(self.path)
        self.assertEqual(value['pending_versions'], [])
        self.assertEqual(value['published_version'], 'v2')
        self.assertEqual(value['previous_version'], 'v1')

    def test_recovery_required_keeps_pending_version(self):
        write_status(self.path, phase='recovery_required', version='v3', operation_id='op3')
        value = read_status(self.path)
        self.assertEqual(value['pending_versions'], ['v3'])

    def test_failed_not_applied_drops_pending(self):
        write_status(self.path, phase='published', version='v1', operation_id='op0', published_version='v1')
        write_status(self.path, phase='prepared', version='v2', operation_id='op2')
        write_status(self.path, phase='failed', version='v1', operation_id='op2')
        value = read_status(self.path)
        self.assertEqual(value['pending_versions'], [])
        # Falló sin aplicar: lo publicado sigue siendo v1.
        self.assertEqual(value['published_version'], 'v1')

    def test_write_preserves_backup_annotation_of_the_publisher(self):
        write_status(self.path, phase='published', version='v1', published_version='v1')
        merge_status(self.path, backup='pending', backup_bytes=5, backup_version='v1')
        write_status(self.path, phase='prepared', version='v2', operation_id='op2')
        value = read_status(self.path)
        self.assertEqual(value['backup'], 'pending')
        self.assertEqual(value['backup_bytes'], 5)
        self.assertEqual(value['backup_version'], 'v1')
        self.assertEqual(value['version'], 'v2')

    def test_new_cycle_preserves_backup_of_still_published_version(self):
        write_status(self.path, phase='published', version='v1', published_version='v1')
        merge_status(self.path, backup='pending', backup_version='v1')
        write_status(self.path, phase='prepared', version='v2', operation_id='op2')
        value = read_status(self.path)
        # El respaldo pendiente sigue refiriéndose a v1, que sigue publicada:
        # `backup_version` lo declara y el CAS del respaldo impide marcarla
        # completa cuando la versión vigente ya no sea esa.
        self.assertEqual(value['backup'], 'pending')
        self.assertEqual(value['backup_version'], 'v1')
        self.assertEqual(value['published_version'], 'v1')

    def test_backup_cas_rejects_stale_version_without_writing(self):
        write_status(self.path, phase='published', version='v1', published_version='v1')
        merge_status(self.path, backup='pending', backup_version='v1')
        write_status(self.path, phase='published', version='v2', operation_id='op2',
                     published_version='v2', previous_version='v1')
        before = read_status(self.path)
        with self.assertRaises(StatusConflict):
            merge_status(self.path, backup='complete', backup_version='v1')
        self.assertEqual(read_status(self.path), before)
        # Confirmado en la versión vigente sí se anota.
        merge_status(self.path, backup='complete', backup_version='v2')
        value = read_status(self.path)
        self.assertEqual(value['backup'], 'complete')
        self.assertEqual(value['backup_version'], 'v2')

    def test_merge_keeps_scalar_backup_and_publisher_fields(self):
        write_status(self.path, phase='published', version='v1', operation_id='op1',
                     counts={'chunks': 3}, published_version='v1',
                     backup={'status': 'complete', 'bytes': 10, 'operations': 2,
                             'finished_at': '2026-11-21T21:00:00+00:00'})
        merged = merge_status(self.path, backup='pending')
        self.assertEqual(merged['phase'], 'published')
        self.assertEqual(merged['operation'], 'op1')
        self.assertEqual(merged['counts'], {'chunks': 3})
        self.assertEqual(merged['backup'], 'pending')
        self.assertEqual(merged['backup_bytes'], 10)
        self.assertEqual(merged['backup_operations'], 2)
        self.assertEqual(merged['backup_at'], '2026-11-21T21:00:00+00:00')
        self.assertIsInstance(merged['backup'], str)


def _interleaved_writer(path, count):
    for index in range(count):
        if index % 2 == 0:
            write_status(path, phase='applying', version='v2', operation_id='op2', counts={'n': index})
        else:
            merge_status(path, backup='pending', backup_version='v2')


def _increment(path, times):
    # La transformación se ejecuta con el lock tomado: leer-y-sumar es atómico.
    for _ in range(times):
        update_status(path, lambda value: {'n': value.get('n', 0) + 1})


class ConcurrentStatusTests(unittest.TestCase):
    ctx = multiprocessing.get_context('fork')

    def test_interleaved_writers_do_not_lose_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rag-status.json'
            write_status(path, phase='published', version='v1', published_version='v1')
            processes = [self.ctx.Process(target=_interleaved_writer, args=(path, 60)) for _ in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=120)
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            value = read_status(path)
            # Ninguna escritura borra el campo de la otra: ambas familias sobreviven.
            self.assertEqual(value['version'], 'v2')
            self.assertEqual(value['phase'], 'applying')
            self.assertEqual(value['operation'], 'op2')
            self.assertEqual(value['backup'], 'pending')
            self.assertEqual(value['backup_version'], 'v2')

    def test_lock_is_exclusive_between_two_mergers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rag-status.json'
            # Leer-y-mutar atómico bajo el lock común: dos procesos que
            # incrementan 100 veces no pierden actualizaciones.
            write_status(path, phase='published', version='v1')
            processes = [self.ctx.Process(target=_increment, args=(path, 100)) for _ in range(2)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=120)
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            self.assertEqual(read_status(path)['n'], 200)


if __name__ == '__main__':
    unittest.main()
