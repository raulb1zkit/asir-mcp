"""Integración real aislada. Ejecutar con tests/rag/run-postgres.sh."""
import copy
import hashlib
import json
import os
import threading
import unittest
from unittest.mock import patch

from scripts.import_supabase import create_schema, import_corpus
from scripts.rag_versions import build_manifest, plan, retain_missing
from scripts.rag_publish import (bootstrap, checks, head_version, install_schema, operation,
                                 publish, resolve, verify, PublishError)
from tests.test_rag_versions import fixture, rehash_inputs

DSN = os.environ.get('ASIR_RAG_TEST_DSN')


@unittest.skipUnless(DSN, 'ASIR_RAG_TEST_DSN ausente: usar tests/rag/run-postgres.sh')
class PostgreSQLTests(unittest.TestCase):
    def factory(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DSN, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def setUp(self):
        import numpy as np
        self.conn = self.factory()
        self.addCleanup(self.conn.close)
        self.conn.execute('DROP SCHEMA IF EXISTS asir CASCADE')
        self.conn.execute('CREATE SCHEMA IF NOT EXISTS extensions')
        create_schema(self.conn)
        with self.conn.transaction():
            install_schema(self.conn)
        self.data = fixture(context='subject-section-title')
        self.resource_map = {}
        for c in self.data['chunks']:
            src = c['citations'][0]['source']
            self.resource_map[src['url']] = src.pop('resource_id')
            c['citations'][0].update(previous_chunk_id=None, next_chunk_id=None)
        self.data['vectors'] = np.asarray(self.data['vectors'], dtype=np.float32)
        self.data['metadata']['vectors_sha256'] = hashlib.sha256(self.data['vectors'].tobytes()).hexdigest()
        # Solo semilla sintética del esquema heredado. Publicador no usa importador.
        import_corpus(self.conn, self.data)
        self.base = build_manifest(self.data, self.resource_map)
        bootstrap(self.conn, self.base)

    def manifest(self, data):
        return build_manifest(rehash_inputs(data), self.resource_map)

    def test_bootstrap_server_checks_no_vector_egress_and_unicode_canon(self):
        with patch('scripts.import_supabase.verify_corpus', side_effect=AssertionError('full download forbidden')):
            result = bootstrap(self.conn, self.base)
            self.assertEqual(result, self.base['checks'])
            self.assertLess(len(json.dumps(result)), 1000)
        from scripts.rag_versions import canonical, digest
        values = [{'z': ['ñ', '漢字', '\n', '\b', '\\', '"'], 'a': {'nested': -2}}, [None, True, False, 'é', 'é'], {'big': 12345678901234567890}]
        for value in values:
            row = self.conn.execute('SELECT asir.rag_canonical(%s::jsonb) AS canon, asir.rag_hash(%s::jsonb) AS hash', (json.dumps(value), json.dumps(value))).fetchone()
            self.assertEqual(row['canon'].encode(), canonical(value))
            self.assertEqual(row['hash'], digest(value))

    def test_unchanged_zero_corpus_writes_no_ledger(self):
        before = self.conn.execute('SELECT id,xmin::text AS xmin FROM asir.chunks ORDER BY id').fetchall()
        result = publish(self.conn, self.base, self.base, 'no-change')
        self.assertEqual(result['status'], 'unchanged')
        self.assertIsNone(operation(self.conn, 'no-change'))
        self.assertEqual(before, self.conn.execute('SELECT id,xmin::text AS xmin FROM asir.chunks ORDER BY id').fetchall())

    def test_add_delta_keeps_ids_ordinals_and_retry(self):
        initial = self.conn.execute('SELECT id,ordinal,xmin::text AS xmin FROM asir.chunks ORDER BY id').fetchall()
        data = fixture(5, context='subject-section-title')
        self.resource_map[data['chunks'][-1]['citations'][0]['source']['url']] = data['chunks'][-1]['citations'][0]['source'].pop('resource_id')
        target = self.manifest(data)
        publish(self.conn, self.base, target, 'add')
        self.assertEqual(head_version(self.conn), target['version'])
        self.assertTrue(publish(self.conn, self.base, target, 'add')['replay'])
        for row in initial:
            self.assertEqual(row, self.conn.execute('SELECT id,ordinal,xmin::text AS xmin FROM asir.chunks WHERE id=%s', (row['id'],)).fetchone())
        self.assertEqual(resolve(self.factory, 'add', self.base, target)['target_version'], target['version'])
        with self.assertRaisesRegex(PublishError, 'reused'):
            publish(self.conn, target, self.base, 'add', allow_anomaly=True)
        with self.assertRaisesRegex(PublishError, 'compare_and_swap'):
            publish(self.conn, self.base, target, 'other')

    def test_metadata_shared_refs_neighbors_and_inverse(self):
        data = copy.deepcopy(self.data)
        first, second = data['chunks'][:2]
        first['citations'][0]['next_chunk_id'] = second['id']
        second['citations'][0]['previous_chunk_id'] = first['id']
        first['citations'][0]['source']['section'] = 'Unidad nueva'
        # Otra ocurrencia comparte el original y texto, sin perder filtros.
        extra = copy.deepcopy(first['citations'][0])
        extra['source'].update(subject='SRI', url='https://example.invalid/shared', title='Referencia SRI')
        self.resource_map[extra['source']['url']] = 'external:shared'
        first['citations'].append(extra)
        target = self.manifest(data)
        old_cites = self.conn.execute('SELECT id FROM asir.citations ORDER BY id').fetchall()
        publish(self.conn, self.base, target, 'metadata')
        verify(self.conn, target)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM asir.chunks').fetchone()['n'], 4)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM asir.citation_sources WHERE subject='SRI'").fetchone()['n'], 1)
        self.assertEqual(self.conn.execute('SELECT next_chunk_id FROM asir.citations WHERE id=%s', (old_cites[0]['id'],)).fetchone()['next_chunk_id'], second['id'])
        publish(self.conn, target, self.base, 'inverse', allow_anomaly=True)
        verify(self.conn, self.base)
        self.assertEqual(head_version(self.conn), self.base['version'])

    def test_edit_removes_old_text_except_shared_ref(self):
        data = copy.deepcopy(self.data)
        source = data['chunks'][0]['citations'][0]['source']
        old_id = data['chunks'][0]['id']
        # Sustituir el recurso por otro texto, conservando identidad lógica.
        replacement = fixture(5, context='subject-section-title')['chunks'][-1]
        replacement['citations'][0]['source'] = dict(source, sha256='7'*64)
        data['chunks'][0] = replacement
        data['metadata']['windows'][0] = {'chunk_id': replacement['id'], 'char_start': 0, 'char_end': len(replacement['text']), 'tokens': 12}
        target = self.manifest(data)
        self.assertEqual(plan(self.base, target)['ratios']['changed_chunks'], .25)
        publish(self.conn, self.base, target, 'edit')
        self.assertIsNone(self.conn.execute('SELECT id FROM asir.chunks WHERE id=%s', (old_id,)).fetchone())
        publish(self.conn, target, self.base, 'undo-edit', allow_anomaly=True)
        verify(self.conn, self.base)

    def test_failure_before_commit_retry_and_uncertain_after_commit(self):
        data = copy.deepcopy(self.data)
        data['chunks'][0]['warnings'] = ['test']
        target = self.manifest(data)
        def before(phase):
            if phase == 'before_commit':
                raise ConnectionError('synthetic before commit')
        with self.assertRaises(ConnectionError):
            publish(self.conn, self.base, target, 'failure', fault=before)
        self.assertEqual(resolve(self.factory, 'failure', self.base, target)['status'], 'not_applied')
        verify(self.conn, self.base)
        def after(phase):
            if phase == 'after_commit':
                raise ConnectionError('synthetic response lost')
        with self.assertRaises(ConnectionError):
            publish(self.conn, self.base, target, 'failure', fault=after)
        self.assertEqual(resolve(self.factory, 'failure', self.base, target)['status'], 'committed_pending_probe')
        self.assertTrue(publish(self.conn, self.base, target, 'failure')['replay'])
        verify(self.conn, target)

    def test_concurrent_publish_one_cas_wins(self):
        data = copy.deepcopy(self.data)
        data['chunks'][0]['warnings'] = ['one']
        one = self.manifest(data)
        data['chunks'][0]['warnings'] = ['two']
        two = self.manifest(data)
        barrier = threading.Barrier(2)
        results = []
        def worker(target, oid):
            with self.factory() as conn:
                barrier.wait(timeout=10)
                try:
                    publish(conn, self.base, target, oid)
                    results.append('ok')
                except PublishError as error:
                    results.append(str(error))
        threads = [threading.Thread(target=worker, args=(one,'one')), threading.Thread(target=worker, args=(two,'two'))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
            self.assertFalse(t.is_alive())
        self.assertCountEqual(results, ['ok', 'base_compare_and_swap_failed'])

    def test_corrupt_vector_and_metadata_cannot_hide_behind_head(self):
        data = copy.deepcopy(self.data)
        data['chunks'][0]['warnings'] = ['changed']
        target = self.manifest(data)
        # Cambiar bytes reales sin tocar hashes publicados.
        vector = '[0,1,' + ','.join(['0']*382) + ']'
        self.conn.execute('UPDATE asir.embedding_windows SET embedding=%s::extensions.vector WHERE id=(SELECT min(id) FROM asir.embedding_windows)', (vector,))
        with self.assertRaisesRegex(PublishError, 'remote_state_mismatch'):
            publish(self.conn, self.base, target, 'corrupt')
        with self.assertRaisesRegex(PublishError, 'remote_state_mismatch'):
            bootstrap(self.conn, self.base)
        self.assertIsNone(operation(self.conn, 'corrupt'))

    def test_inactive_resources_and_model_change(self):
        data = copy.deepcopy(self.data)
        data['chunks'][0]['citations'][0]['source']['moodle_active'] = False
        inactive = self.manifest(data)
        self.assertEqual(plan(self.base, inactive)['blocked'], [])
        publish(self.conn, self.base, inactive, 'inactive')
        verify(self.conn, inactive)
        data['metadata']['model_revision'] = 'new'
        model = self.manifest(data)
        with self.assertRaisesRegex(PublishError, 'anomalous'):
            publish(self.conn, inactive, model, 'model')
        publish(self.conn, inactive, model, 'model', allow_anomaly=True)
        verify(self.conn, model)

    def test_request_repeatable_read_and_metadata_refresh(self):
        from scripts.postgres_search import PostgresEngine
        engine = PostgresEngine.__new__(PostgresEngine)
        engine.connection = self.factory()
        self.addCleanup(engine.connection.close)
        engine._request_depth = 0
        engine.encoder = None
        engine.metadata = engine._load_metadata()
        data = copy.deepcopy(self.data)
        data['chunks'][0]['warnings'] = ['changed']
        target = self.manifest(data)
        cid = data['chunks'][0]['id']
        with engine.request():
            self.assertEqual(engine.metadata['version'], self.base['version'])
            self.assertEqual(engine.eligible(cid)['warnings'], [])
            publish(self.conn, self.base, target, 'while-reading')
            self.assertEqual(engine.eligible(cid)['warnings'], [])
            self.assertEqual(engine.metadata['version'], self.base['version'])
        with engine.request():
            self.assertEqual(engine.metadata['version'], target['version'])
            self.assertEqual(engine.eligible(cid)['warnings'], ['changed'])


@unittest.skipUnless(DSN, 'ASIR_RAG_TEST_DSN ausente: usar tests/rag/run-postgres.sh')
class EmptySectionProjectionTests(unittest.TestCase):
    """Regresión de secciones vacías en la proyección de chunks.

    El importador legacy guarda `chunks.sections=''` cuando la única línea de
    sección es vacía; `string_to_array('', E'\\n')` devuelve un array de 0
    elementos y el manifiesto canónico representa esa misma fila como [''].
    El CASE de `rag_rows` corrige solo esa proyección, sin DML de corpus:
    bootstrap y publish deben pasar con el corpus íntegro. La cadena vacía es
    el único caso perdido por el split: chunks con mezcla de sección vacía y
    no vacía ya se representaban bien.
    """

    def factory(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DSN, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def setUp(self):
        self.conn = self.factory()
        self.addCleanup(self.conn.close)
        self.conn.execute('DROP SCHEMA IF EXISTS asir CASCADE')
        self.conn.execute('CREATE SCHEMA IF NOT EXISTS extensions')
        create_schema(self.conn)
        with self.conn.transaction():
            install_schema(self.conn)

    def prepare(self, data):
        import numpy as np
        self.resource_map = {}
        for c in data['chunks']:
            for citation in c['citations']:
                src = citation['source']
                self.resource_map[src['url']] = src.pop('resource_id')
            c['citations'][0].update(previous_chunk_id=None, next_chunk_id=None)
        rehash_inputs(data)
        data['vectors'] = np.asarray(data['vectors'], dtype=np.float32)
        data['metadata']['vectors_sha256'] = hashlib.sha256(data['vectors'].tobytes()).hexdigest()
        import_corpus(self.conn, data)
        return build_manifest(data, self.resource_map)

    def test_all_citations_empty_section_bootstrap_and_publish(self):
        data = fixture(context='subject-section-title')
        for c in data['chunks']:
            c['citations'][0]['source']['section'] = ''
        base = self.prepare(data)
        rows = self.conn.execute("SELECT (sections='') AS empty, (sections IS NULL) AS null FROM asir.chunks").fetchall()
        # Contrato legacy: cadena vacía almacenada, nunca NULL (no hay tal writer).
        self.assertTrue(rows and all(r['empty'] and not r['null'] for r in rows))
        projected = self.conn.execute("SELECT payload FROM asir.rag_rows WHERE kind='chunks'").fetchall()
        self.assertTrue(projected and all(r['payload']['sections'] == [''] for r in projected))
        result = bootstrap(self.conn, base)
        self.assertEqual(result, base['checks'])
        verify(self.conn, base)
        data2 = copy.deepcopy(data)
        data2['chunks'][0]['citations'][0]['source']['title'] = 'Editado'
        target = build_manifest(rehash_inputs(data2), self.resource_map)
        publish(self.conn, base, target, 'empty-section-edit')
        self.assertEqual(head_version(self.conn), target['version'])
        verify(self.conn, target)
        publish(self.conn, target, base, 'empty-section-inverse', allow_anomaly=True)
        verify(self.conn, base)

    def test_mixed_empty_and_nonempty_sections_sorted_arrays(self):
        data = fixture(context='subject-section-title')
        data['chunks'][0]['citations'][0]['source']['section'] = ''
        extra = copy.deepcopy(data['chunks'][0]['citations'][0])
        extra['source'].update(sha256='9' * 64, url='https://example.invalid/extra', section='Unidad 2', title='Extra',
                               resource_id='external:extra')
        extra.update(previous_chunk_id=None, next_chunk_id=None)
        data['chunks'][0]['citations'].append(extra)
        base = self.prepare(data)
        mixed = base['rows']['chunks'][data['chunks'][0]['id']]
        self.assertEqual(mixed['sections'], ['', 'Unidad 2'])
        result = bootstrap(self.conn, base)
        self.assertEqual(result, base['checks'])
        verify(self.conn, base)


if __name__ == '__main__':
    unittest.main()
