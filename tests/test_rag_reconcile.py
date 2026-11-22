"""Reconciliación legacy de sources, errores CLI de publicación y grants exactos.

Las pruebas sintéticas no tocan red ni base real; las de PostgreSQL usan la
base desechable de tests/rag/run-postgres.sh. Nunca imprimen materiales.
"""
import copy
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.rag_publish import (ReconcileDenied, PublishError, canonical_source_rows,
                                 legacy_source_rows, reconcile_legacy_sources)
from scripts.rag_versions import build_manifest, plan, write_version, VersionError
from tests.test_rag_versions import fixture, rehash_inputs

DSN = os.environ.get('ASIR_RAG_TEST_DSN')


def duplicate_variant_data():
    """Corpus legacy con el mismo original citado dos veces con metadatos distintos.

    El importador antiguo deja en asir.sources el último representante
    (Variante B, chunk 2), mientras el manifiesto elige el mínimo canónico
    (Título 0, chunk 0): bootstrap fallaría hasta reconciliar sources.
    """
    data = fixture(context='subject-section-title')
    source = data['chunks'][0]['citations'][0]['source']
    variant = copy.deepcopy(data['chunks'][2]['citations'][0]['source'])
    variant.update(sha256=source['sha256'], title='Variante B', path='synthetic/variante',
                   url='https://example.invalid/variant', resource_id='external:variant')
    data['chunks'][2]['citations'][0]['source'] = variant
    return data, rehash_inputs(data)


class ProjectionTests(unittest.TestCase):
    def test_legacy_last_writer_and_canonical_minimum_differ(self):
        data, _ = duplicate_variant_data()
        legacy = legacy_source_rows(data)
        computed = canonical_source_rows(data)
        sha = data['chunks'][0]['citations'][0]['source']['sha256']
        self.assertEqual(legacy[sha]['title'], 'Variante B')
        self.assertEqual(computed[sha]['title'], 'Título 0')
        self.assertEqual(len(legacy), len(computed))
        manifest = build_manifest(data)
        self.assertEqual(manifest['rows']['sources'], computed)

    def test_reconcile_denies_manifest_from_other_data_without_db(self):
        _, data = duplicate_variant_data()
        manifest = build_manifest(fixture(context='subject-section-title'))
        with self.assertRaisesRegex(PublishError, 'manifest_legacy_sources_mismatch'):
            reconcile_legacy_sources(None, manifest, data)
        other = copy.deepcopy(data)
        other['chunks'][0]['citations'][0]['source']['title'] = 'Otra copia'
        with self.assertRaisesRegex(PublishError, 'manifest_legacy_sources_mismatch'):
            reconcile_legacy_sources(None, build_manifest(data), other)

    def test_denied_counts_are_printed_without_rows(self):
        error = ReconcileDenied('legacy_sources_diverged', {'sources_diverged': 2, 'sources_unknown': 0})
        self.assertEqual(str(error), 'legacy_sources_diverged')
        self.assertEqual(error.counts, {'sources_diverged': 2, 'sources_unknown': 0})


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        manifest = build_manifest(fixture(context='subject-section-title'))
        self.base = write_version(manifest, root / 'versions') / 'manifest.json'
        self.status = root / 'rag-status.json'
        self.argv = ['rag_publish.py', '--base', str(self.base), '--target', str(self.base),
                     '--database-config', str(root / 'db.json'), '--execute',
                     '--operation-id', 'op-test', '--status', str(self.status)]
        self.config = {'user': 'asir_ingest.test', 'password': 'x', 'host': 'db'}
        patcher = patch('scripts.supabase_db.ingest_config', return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        connection = unittest.mock.MagicMock()
        patcher = patch('scripts.supabase_db.connect', return_value=connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connect = connection

    def run_main(self, argv):
        stdout = io.StringIO()
        from scripts import rag_publish
        with patch('sys.argv', argv), redirect_stdout(stdout):
            code = rag_publish.main()
        return code, stdout.getvalue()

    def expect_status(self, phase, **fields):
        value = json.loads(self.status.read_text())
        self.assertEqual(value['phase'], phase)
        for key, expected in fields.items():
            self.assertEqual(value.get(key), expected)
        return value

    def test_deterministic_failure_marks_failed_without_resolve(self):
        from scripts import rag_publish
        published = {'phase': 'published', 'version': 'v0', 'published_version': 'v0', 'previous_version': 'v0'}
        self.status.write_text(json.dumps(published))
        with patch.object(rag_publish, 'publish', side_effect=PublishError('anomalous_plan')) as publish, \
                patch.object(rag_publish, 'resolve') as resolve:
            code, output = self.run_main(self.argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error'], 'anomalous_plan')
        publish.assert_called_once()
        resolve.assert_not_called()
        state = self.expect_status('failed', published_version='v0', previous_version='v0')
        self.assertEqual(state['pending_versions'], [])

    def test_uncertain_ack_resolved_not_applied_is_failed_safe_retry(self):
        from scripts import rag_publish
        with patch.object(rag_publish, 'publish', side_effect=ConnectionError('connection reset')), \
                patch.object(rag_publish, 'resolve', return_value={'status': 'not_applied', 'operation_id': 'op-test'}) as resolve:
            code, output = self.run_main(self.argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error'], 'not_applied_safe_to_retry_same_operation')
        resolve.assert_called_once()
        self.expect_status('failed')

    def test_uncertain_ack_resolved_committed_writes_probe_phase(self):
        from scripts import rag_publish
        probe = {'status': 'committed_pending_probe', 'operation_id': 'op-test', 'target_version': 'v'}
        with patch.object(rag_publish, 'publish', side_effect=ConnectionError('response lost')), \
                patch.object(rag_publish, 'resolve', return_value=probe) as resolve:
            code, output = self.run_main(self.argv)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'committed_pending_probe')
        # Dos resoluciones: la del ack incierto y la sonda de confirmación.
        self.assertEqual(resolve.call_count, 2)
        self.expect_status('committed_pending_probe')

    def test_unresolvable_head_is_recovery_required(self):
        from scripts import rag_publish
        with patch.object(rag_publish, 'publish', side_effect=ConnectionError('connection reset')), \
                patch.object(rag_publish, 'resolve', side_effect=PublishError('recovery_required')):
            code, output = self.run_main(self.argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error'], 'recovery_required')
        self.expect_status('recovery_required')

    def test_confirmation_probe_failure_keeps_committed_phase(self):
        from scripts import rag_publish
        probe = {'status': 'committed_pending_probe', 'operation_id': 'op-test'}
        with patch.object(rag_publish, 'publish', return_value=probe), \
                patch.object(rag_publish, 'resolve', side_effect=ConnectionError('probe failed')) as resolve:
            code, output = self.run_main(self.argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error'], 'confirmation_probe_failed_committed_pending')
        resolve.assert_called_once()
        self.expect_status('committed_pending_probe')

    def test_reconcile_flag_requires_legacy_artifacts(self):
        from scripts import rag_publish
        argv = [a for a in self.argv] + ['--reconcile-legacy-sources']
        with patch.object(rag_publish, 'bootstrap') as bootstrap:
            code, output = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error'], 'legacy_artifacts_required')
        bootstrap.assert_not_called()
        self.assertFalse(self.status.exists())

    def test_reconcile_denied_prints_safe_counts(self):
        from scripts import rag_publish
        artifacts = Path(self.directory.name) / 'legacy'
        argv = [a for a in self.argv] + ['--reconcile-legacy-sources',
                                         '--legacy-chunks', str(artifacts / 'c'),
                                         '--legacy-report', str(artifacts / 'r'),
                                         '--legacy-semantic', str(artifacts / 's'),
                                         '--legacy-model', str(artifacts / 'm')]
        loader = patch('scripts.import_supabase.load_local', return_value={'chunks': [], 'report': {}, 'metadata': {}, 'vectors': []})
        denied = ReconcileDenied('legacy_sources_diverged', {'sources_diverged': 1, 'sources_unknown': 0})
        with loader, patch.object(rag_publish, 'reconcile_legacy_sources', side_effect=denied):
            code, output = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {'error': 'legacy_sources_diverged', 'sources_diverged': 1, 'sources_unknown': 0})

    def test_reconcile_and_bootstrap_flags_are_exclusive(self):
        from scripts import rag_publish
        argv = [a for a in self.argv] + ['--reconcile-legacy-sources', '--bootstrap',
                                         '--legacy-chunks', 'x', '--legacy-report', 'x',
                                         '--legacy-semantic', 'x', '--legacy-model', 'x']
        code, output = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['error'], 'mutually_exclusive_flags')


@unittest.skipUnless(DSN, 'ASIR_RAG_TEST_DSN ausente: usar tests/rag/run-postgres.sh')
class ReconcilePostgreSQLTests(unittest.TestCase):
    def factory(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DSN, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def setUp(self):
        import numpy as np
        from scripts.import_supabase import create_schema, import_corpus
        from scripts.rag_publish import install_schema
        self.conn = self.factory()
        self.addCleanup(self.conn.close)
        self.conn.execute('DROP SCHEMA IF EXISTS asir CASCADE')
        self.conn.execute('CREATE SCHEMA IF NOT EXISTS extensions')
        create_schema(self.conn)
        with self.conn.transaction():
            install_schema(self.conn)
        self.data = fixture(context='subject-section-title')
        self.data['vectors'] = np.asarray(self.data['vectors'], dtype=np.float32)
        self.data['metadata']['vectors_sha256'] = hashlib.sha256(self.data['vectors'].tobytes()).hexdigest()
        _, self.data = duplicate_variant_data()
        self.data['vectors'] = np.asarray(self.data['vectors'], dtype=np.float32)
        self.data['metadata']['vectors_sha256'] = hashlib.sha256(self.data['vectors'].tobytes()).hexdigest()
        # La identidad del recurso va en el mapa, no dentro del corpus importado.
        self.resource_map = {}
        for c in self.data['chunks']:
            src = c['citations'][0]['source']
            self.resource_map[src['url']] = src.pop('resource_id')
            c['citations'][0].update(previous_chunk_id=None, next_chunk_id=None)
        import_corpus(self.conn, self.data)
        # Cero negativo real en el almacén. El roundtrip textual del importador
        # legacy pierde el bit (json.loads('-0') es 0), de modo que los bytes se
        # escriben tras la importación: el manifiesto y vector_send deben
        # coincidir exactos en bootstrap, publicación y rollback.
        self.data['vectors'][0][7] = -0.0
        self.data['metadata']['vectors_sha256'] = hashlib.sha256(self.data['vectors'].tobytes()).hexdigest()
        literal = '[' + ','.join(format(float(v), '.9g') for v in self.data['vectors'][0]) + ']'
        self.conn.execute('UPDATE asir.embedding_windows SET embedding=%s::extensions.vector WHERE chunk_id=%s AND ordinal=0',
                          (literal, self.data['chunks'][0]['id']))
        self.base = build_manifest(self.data, self.resource_map)

    def test_reconcile_updates_last_writer_to_canonical_and_bootstrap_passes(self):
        from scripts.import_supabase import verify_corpus
        from scripts.rag_publish import bootstrap, verify
        with patch('scripts.import_supabase.verify_corpus', side_effect=AssertionError('no download')), \
                self.assertRaisesRegex(PublishError, 'remote_state_mismatch'):
            bootstrap(self.conn, self.base)
        visible = self.conn.execute('SELECT citation_id,subject,section,title,filename FROM asir.citation_sources ORDER BY citation_id').fetchall()
        occurrences = self.conn.execute('SELECT source_key,xmin::text AS xmin FROM asir.source_occurrences ORDER BY source_key').fetchall()
        citations = self.conn.execute('SELECT id,source_key FROM asir.citations ORDER BY id').fetchall()
        report = reconcile_legacy_sources(self.conn, self.base, self.data)
        self.assertEqual(report['status'], 'reconciled')
        self.assertEqual(report['counts']['sources_updated'], 1)
        self.assertEqual(report['counts']['citations_without_source_key'], 0)
        self.assertEqual(report['counts']['sources'], report['counts']['sources_updated'] + report['counts']['sources_already_canonical'])
        self.assertNotEqual(report['before']['sources'], report['after']['sources'])
        sha = self.data['chunks'][0]['citations'][0]['source']['sha256']
        self.assertEqual(self.conn.execute('SELECT title FROM asir.sources WHERE sha256=%s', (sha,)).fetchone()['title'], 'Título 0')
        self.assertEqual(visible, self.conn.execute('SELECT citation_id,subject,section,title,filename FROM asir.citation_sources ORDER BY citation_id').fetchall())
        self.assertEqual(occurrences, self.conn.execute('SELECT source_key,xmin::text AS xmin FROM asir.source_occurrences ORDER BY source_key').fetchall())
        self.assertEqual(citations, self.conn.execute('SELECT id,source_key FROM asir.citations ORDER BY id').fetchall())
        verify(self.conn, self.base)
        bootstrap(self.conn, self.base)

    def test_reconcile_denies_unknown_drift_and_rogue_row(self):
        sha = self.data['chunks'][0]['citations'][0]['source']['sha256']
        self.conn.execute('UPDATE asir.sources SET title=%s WHERE sha256=%s', ('Drift', sha))
        before = self.conn.execute('SELECT xmin::text AS xmin FROM asir.sources WHERE sha256=%s', (sha,)).fetchone()
        with self.assertRaisesRegex(PublishError, 'legacy_sources_diverged') as caught:
            reconcile_legacy_sources(self.conn, self.base, self.data)
        self.assertEqual(caught.exception.counts, {'sources_diverged': 1, 'sources_unknown': 0})
        self.assertEqual(self.conn.execute('SELECT title FROM asir.sources WHERE sha256=%s', (sha,)).fetchone()['title'], 'Drift')
        self.assertEqual(before, self.conn.execute('SELECT xmin::text AS xmin FROM asir.sources WHERE sha256=%s', (sha,)).fetchone())
        self.conn.execute("INSERT INTO asir.sources(sha256,subject,section,title,filename) VALUES (%s,'X','X','X','X')", ('f'*64,))
        with self.assertRaisesRegex(PublishError, 'legacy_sources_diverged') as caught:
            reconcile_legacy_sources(self.conn, self.base, self.data)
        self.assertEqual(caught.exception.counts, {'sources_diverged': 1, 'sources_unknown': 1})

    def test_reconcile_denies_legacy_corpus_without_source_keys(self):
        cid = self.data['chunks'][0]['id']
        updated = self.conn.execute('UPDATE asir.citations SET source_key=NULL WHERE chunk_id=%s', (cid,)).rowcount
        self.assertEqual(updated, 1)
        with self.assertRaisesRegex(PublishError, 'legacy_citations_without_source_key') as caught:
            reconcile_legacy_sources(self.conn, self.base, self.data)
        self.assertEqual(caught.exception.counts, {'citations_without_source_key': 1})

    def test_negative_zero_bytes_survive_bootstrap_publish_and_rollback(self):
        import numpy as np
        from scripts.rag_publish import bootstrap, head_version, publish, verify, reconcile_legacy_sources
        # La fila representativa legacy debe estar canónica antes del bootstrap.
        reconcile_legacy_sources(self.conn, self.base, self.data)
        bootstrap(self.conn, self.base)
        cid = self.data['chunks'][0]['id']
        server_hex = self.conn.execute(
            "SELECT encode(substring(extensions.vector_send(embedding) FROM 5),'hex') AS h FROM asir.embedding_windows WHERE chunk_id=%s ORDER BY ordinal LIMIT 1", (cid,)).fetchone()['h']
        self.assertEqual(server_hex[7*8:7*8+8], '80000000')
        verify(self.conn, self.base)
        # Publicar un delta cuyo vector nuevo contiene -0.0: la entrada textual
        # debe reconstruir exactamente los mismos bytes.
        extra = fixture(5, context='subject-section-title')['chunks'][-1]
        source = self.data['chunks'][0]['citations'][0]['source']
        extra['citations'][0]['source'] = dict(source, sha256='a'*64, title='Con cero negativo',
                                               url='https://example.invalid/negative-zero')
        self.resource_map['https://example.invalid/negative-zero'] = 'external:negative-zero'
        extra['citations'][0].update(previous_chunk_id=None, next_chunk_id=None)
        target_data = copy.deepcopy(self.data)
        target_data['chunks'].append(extra)
        target_data['metadata']['windows'].append({'chunk_id': extra['id'], 'char_start': 0, 'char_end': len(extra['text']), 'tokens': 12})
        extra_vector = np.asarray([0.0] * 384, dtype=np.float32)
        extra_vector[3] = -0.0
        target_data['vectors'] = np.concatenate([np.asarray(target_data['vectors'], dtype=np.float32),
                                                 np.asarray([extra_vector], dtype=np.float32)])
        target_data['metadata']['vectors_sha256'] = hashlib.sha256(target_data['vectors'].tobytes()).hexdigest()
        target = build_manifest(rehash_inputs(target_data), self.resource_map)
        publish(self.conn, self.base, target, 'negative-zero')
        verify(self.conn, target)
        self.assertEqual(head_version(self.conn), target['version'])
        new_hex = self.conn.execute(
            "SELECT encode(substring(extensions.vector_send(embedding) FROM 5),'hex') AS h FROM asir.embedding_windows WHERE chunk_id=%s ORDER BY ordinal LIMIT 1", (extra['id'],)).fetchone()['h']
        self.assertEqual(new_hex[3*8:3*8+8], '80000000')
        publish(self.conn, target, self.base, 'rollback', allow_anomaly=True)
        verify(self.conn, self.base)
        server_hex = self.conn.execute(
            "SELECT encode(substring(extensions.vector_send(embedding) FROM 5),'hex') AS h FROM asir.embedding_windows WHERE chunk_id=%s ORDER BY ordinal LIMIT 1", (cid,)).fetchone()['h']
        self.assertEqual(server_hex[7*8:7*8+8], '80000000')


@unittest.skipUnless(DSN, 'ASIR_RAG_TEST_DSN ausente: usar tests/rag/run-postgres.sh')
class ProvisionPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from scripts.import_supabase import create_schema, import_corpus
        from scripts.rag_publish import install_schema
        self.conn = self.factory()
        self.addCleanup(self.conn.close)
        self.conn.execute('DROP SCHEMA IF EXISTS asir CASCADE')
        self.conn.execute('CREATE SCHEMA IF NOT EXISTS extensions')
        create_schema(self.conn)
        with self.conn.transaction():
            install_schema(self.conn)
        self.data = fixture(context='subject-section-title')
        self.data['vectors'] = np.asarray(self.data['vectors'], dtype=np.float32)
        self.data['metadata']['vectors_sha256'] = hashlib.sha256(self.data['vectors'].tobytes()).hexdigest()
        self.resource_map = {}
        for c in self.data['chunks']:
            src = c['citations'][0]['source']
            self.resource_map[src['url']] = src.pop('resource_id')
            c['citations'][0].update(previous_chunk_id=None, next_chunk_id=None)
        import_corpus(self.conn, self.data)

    def factory(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DSN, autocommit=True, row_factory=dict_row, prepare_threshold=None)

    def role_dsn(self):
        from scripts import provision_ingest as ingest
        return re.sub(r'(^|\s)user=\S+', '', DSN) + ' user=' + ingest.ROLE

    def role_factory(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.role_dsn(), row_factory=dict_row, prepare_threshold=None)

    def prepare_role(self):
        from scripts import provision_ingest as ingest
        from scripts.rag_publish import install_schema
        self.conn.execute('DROP SCHEMA IF EXISTS asir_staging CASCADE')
        for role in ('asir_ingest', 'asir_reader'):
            if self.conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                self.conn.execute('DROP POLICY IF EXISTS rag_head_ingest ON asir.rag_head')
                self.conn.execute('DROP POLICY IF EXISTS rag_ops_ingest ON asir.rag_operations')
                self.conn.execute('DROP POLICY IF EXISTS rag_head_reader ON asir.rag_head')
                self.conn.execute('DROP OWNED BY ' + role)
                self.conn.execute('DROP ROLE ' + role)
        self.conn.execute('CREATE ROLE asir_reader NOLOGIN')
        self.conn.execute('CREATE SCHEMA IF NOT EXISTS auth')
        self.conn.execute('CREATE TABLE IF NOT EXISTS auth.users(id integer)')
        self.conn.execute('CREATE SCHEMA IF NOT EXISTS storage')
        self.conn.execute('CREATE TABLE IF NOT EXISTS storage.objects(id integer)')
        # Conexión separada para el bootstrap de ensayo: el wrapper de staging
        # fija cursor_factory en la conexión que lo usa y no se reutiliza
        # (así lo hace provision() en producción).
        with self.factory() as bootstrap_conn:
            ingest.bootstrap_staging(bootstrap_conn)
        with self.conn.transaction():
            ingest.grant_role(self.conn, 'test-only-password')
        with self.conn.transaction():
            install_schema(self.conn)

    def test_check_role_accepts_exact_versioning_grants(self):
        from scripts import provision_ingest as ingest
        self.prepare_role()
        with self.role_factory() as connection:
            report = ingest.check_role(connection)
        self.assertTrue(report['complete'])
        self.assertTrue(report['rag_versioning_checked'])
        self.assertEqual(report['forbidden_operations_denied'], 12)

    def test_extra_grant_on_ledger_is_rejected(self):
        from scripts import provision_ingest as ingest
        self.prepare_role()
        self.conn.execute('GRANT DELETE ON asir.rag_operations TO asir_ingest')
        with self.role_factory() as connection:
            with self.assertRaisesRegex(ingest.ProvisionFailure, 'not_denied'):
                ingest.check_role(connection)
        self.conn.execute('REVOKE DELETE ON asir.rag_operations FROM asir_ingest')
        self.conn.execute('REVOKE UPDATE ON asir.rag_head FROM asir_ingest')
        with self.role_factory() as connection:
            with self.assertRaisesRegex(ingest.ProvisionFailure, 'privilege_missing'):
                ingest.check_role(connection)


if __name__ == '__main__':
    unittest.main()
