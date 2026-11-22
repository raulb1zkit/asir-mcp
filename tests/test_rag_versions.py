import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.rag_versions import (build_manifest, canonical, digest, embedding_prefix, plan,
                                  retain_missing, vector_hex, VersionError, write_version, read_manifest)


def fixture(count=4, *, context='stable-subject-section-title-v2'):
    chunks, windows, vectors = [], [], []
    for n in range(count):
        text = 'Texto sintético número ' + str(n) + '. Unicode: ñ, 漢字.'
        sha = hashlib.sha256(text.encode()).hexdigest()
        source = {'sha256': hashlib.sha256(('original'+str(n)).encode()).hexdigest(),
                  'subject': 'ASO', 'section': 'Unidad 1', 'title': 'Título '+str(n),
                  'filename': str(n)+'.txt', 'url': 'https://example.invalid/mod/resource/view.php?id='+str(n),
                  'mime': 'text/plain', 'path': 'synthetic/'+str(n), 'moodle_active': True,
                  'resource_id': 'moodle:course:1:cm:'+str(n)}
        chunk = {'id': 'chunk:'+sha, 'text': text, 'text_sha256': sha, 'characters': len(text),
                 'warnings': [], 'requires_review': False, 'schema_version': 1,
                 'citations': [{'source': source, 'method': 'native', 'segments': [
                     {'source_unit_id': 'unit:'+str(n), 'locator': {'page': 1, 'label': 'ñ'},
                      'start': 0, 'end': len(text), 'method': 'native', 'quality': 'native'}]}]}
        chunks.append(chunk)
        windows.append({'chunk_id': chunk['id'], 'char_start': 0, 'char_end': len(text), 'tokens': 12,
                        'input_sha256': hashlib.sha256((embedding_prefix(chunk, context)+text).encode()).hexdigest()})
        vector = [0.0]*384
        vector[n] = 1.0
        vectors.append(vector)
    return {'chunks': chunks, 'vectors': vectors,
            'metadata': {'schema_version': 1, 'corpus_sha256': 'c'*64, 'model_fingerprint': 'd'*64,
                         'model_repository': 'synthetic', 'model_revision': 'v1', 'dimensions': 384,
                         'max_tokens': 512, 'overlap_tokens': 64, 'context': context,
                         'vectors_sha256': 'e'*64, 'summary': {}, 'windows': windows},
            'report': {'schema_version': 1, 'output_sha256': 'c'*64, 'chunker_version': '1', 'created_at': '2026-11-21T00:00:00Z'}}


def rehash_inputs(data):
    for w in data['metadata']['windows']:
        c = next(c for c in data['chunks'] if c['id'] == w['chunk_id'])
        w['input_sha256'] = hashlib.sha256((embedding_prefix(c, data['metadata']['context'])+c['text'][w['char_start']:w['char_end']]).encode()).hexdigest()
    return data


class ManifestTests(unittest.TestCase):
    def test_determinism_reordered_chunks_windows_citations(self):
        data = fixture()
        other = copy.deepcopy(data['chunks'][0]['citations'][0])
        other['source'].update(resource_id='external:extra', title='Z título', sha256='9'*64, url='https://example.invalid/extra')
        data['chunks'][0]['citations'].append(other)
        rehash_inputs(data)
        first = build_manifest(data)
        data['chunks'].reverse()
        data['chunks'][-1]['citations'].reverse()
        data['metadata']['windows'].reverse()
        data['vectors'].reverse()
        self.assertEqual(first, build_manifest(data))

    def test_restore_derivatives_load_local_without_model_inference(self):
        from scripts.rag_versions import restore_artifacts
        from scripts.import_supabase import load_local
        for context in ('stable-subject-section-title-v2', 'subject-section-title'):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = root / 'model'
                model.mkdir()
                # Solo model.json: no pesos/tokenizer/ONNX, demuestra cero inferencia.
                model_manifest = {'dimensions': 384, 'max_tokens': 512, 'repository': 'synthetic', 'revision': 'v1'}
                (model / 'model.json').write_text(json.dumps(model_manifest))
                data = fixture(context=context)
                extra = copy.deepcopy(data['chunks'][0]['citations'][0])
                extra['source'].update(resource_id='external:extra', title='A primera por sort', url='https://example.invalid/extra')
                data['chunks'][0]['citations'].append(extra)
                data['metadata']['model_fingerprint'] = hashlib.sha256(json.dumps(model_manifest, sort_keys=True).encode()).hexdigest()
                value = build_manifest(rehash_inputs(data))
                paths = restore_artifacts(value, root / 'recovered')
                restored = load_local(paths['chunks.jsonl'], paths['report.json'], paths['semantic.npz'], model)
                self.assertEqual(build_manifest(restored), value)
                self.assertEqual(value['version'], value['content_root'])
                self.assertFalse((root / 'recovered/artifacts.json').exists())
                with self.assertRaises(FileExistsError):
                    restore_artifacts(value, root / 'recovered')

    def test_restore_orders_terminal_window_last_and_loads(self):
        # Regresión real (docs/47): ordenar por (chunk_id, char_start,
        # char_end) dejaba tras la ventana terminal una ventana corta
        # solapada con char_start mayor (ventana terminal de un segmento
        # previo del mismo chunk) y load_local fallaba en 24 chunks aunque
        # el multiconjunto de 27.259 ventanas era idéntico. Texto largo
        # (> 8192) con esa topología exacta: la ventana que cubre hasta el
        # final del texto tiene char_start menor que la corta solapada.
        from scripts.rag_versions import restore_artifacts
        from scripts.import_supabase import load_local
        for context in ('stable-subject-section-title-v2', 'subject-section-title'):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = root / 'model'
                model.mkdir()
                # Solo model.json: no pesos/tokenizer/ONNX, demuestra cero inferencia.
                model_manifest = {'dimensions': 384, 'max_tokens': 512, 'repository': 'synthetic', 'revision': 'v1'}
                (model / 'model.json').write_text(json.dumps(model_manifest))
                data = fixture(count=1, context=context)
                text = 'abñ漢字' * 2000
                self.assertEqual(len(text), 10000)
                sha = hashlib.sha256(text.encode()).hexdigest()
                source = {'sha256': hashlib.sha256(b'long-original').hexdigest(),
                          'subject': 'ASO', 'section': 'Unidad 2', 'title': 'Documento largo',
                          'filename': 'long.txt', 'url': 'https://example.invalid/mod/resource/view.php?id=99',
                          'mime': 'text/plain', 'path': 'synthetic/long', 'moodle_active': True,
                          'resource_id': 'moodle:course:1:cm:99'}
                long_chunk = {'id': 'chunk:' + sha, 'text': text, 'text_sha256': sha,
                              'characters': len(text), 'warnings': [], 'requires_review': False,
                              'schema_version': 1,
                              'citations': [{'source': source, 'method': 'native', 'segments': [
                                  {'source_unit_id': 'unit:long', 'locator': {'page': 1, 'label': 'l'},
                                   'start': 0, 'end': len(text), 'method': 'native', 'quality': 'native'}]}]}
                data['chunks'].append(long_chunk)
                # La última aquí (7900, 7936) es la ventana corta solapada con
                # char_start mayor que la terminal (7680, 10000): con el orden
                # antiguo quedaba tras ella y el archivo acababa sin cobertura.
                spans = ((0, 4096, 512), (3840, 7936, 500), (7680, 10000, 480), (7900, 7936, 60))
                data['metadata']['windows'] += [
                    {'chunk_id': long_chunk['id'], 'char_start': s, 'char_end': e, 'tokens': t}
                    for s, e, t in spans]
                data['vectors'] += [[1.0 if i == n else 0.0 for i in range(384)] for n in range(len(spans))]
                data['metadata']['model_fingerprint'] = hashlib.sha256(json.dumps(model_manifest, sort_keys=True).encode()).hexdigest()
                value = build_manifest(rehash_inputs(data))
                # Preconditions del fallo original: con el orden antiguo la
                # terminal no era la última ventana del chunk.
                by_char = sorted((r['char_start'], r['char_end']) for r in value['rows']['vectors'].values()
                                 if r['chunk_id'] == long_chunk['id'])
                self.assertEqual(by_char[-1], (7900, 7936))
                paths = restore_artifacts(value, root / 'recovered')
                restored = load_local(paths['chunks.jsonl'], paths['report.json'], paths['semantic.npz'], model)
                self.assertEqual(build_manifest(restored), value)
                windows = [w for w in restored['metadata']['windows'] if w['chunk_id'] == long_chunk['id']]
                self.assertEqual((windows[-1]['char_start'], windows[-1]['char_end']), (7680, 10000))
                # El multiconjunto y los input_sha256 no cambian: solo el orden.
                manifest_rows = [r for r in value['rows']['vectors'].values() if r['chunk_id'] == long_chunk['id']]
                self.assertEqual(sorted((w['char_start'], w['char_end'], w['tokens'], w['input_sha256']) for w in windows),
                                 sorted((r['char_start'], r['char_end'], r['tokens'], r['input_sha256']) for r in manifest_rows))

    def test_resource_identity_survives_changed_sha_path_title(self):
        data = fixture()
        base = build_manifest(data)
        source = data['chunks'][0]['citations'][0]['source']
        source.update(sha256='7'*64, path='another/path', title='Editado')
        target = build_manifest(rehash_inputs(data))
        self.assertEqual(set(base['resources']), set(target['resources']))
        self.assertEqual(plan(base, target)['ratios']['removed_resources'], 0)

    def test_stable_resources_required_no_sha_path_title_fallback(self):
        data = fixture()
        for chunk in data['chunks']:
            chunk['citations'][0]['source'].pop('resource_id')
        with self.assertRaisesRegex(VersionError, 'stable_resource'):
            build_manifest(data)

    def test_metadata_and_input_invalidate_only_affected_vector(self):
        data = fixture()
        base = build_manifest(data)
        data['chunks'][0]['citations'][0]['source']['title'] = 'Editado'
        with self.assertRaisesRegex(VersionError, 'input_not_verified'):
            build_manifest(data)
        target = build_manifest(rehash_inputs(data))
        delta = plan(base, target)
        self.assertEqual(len(delta['actions']['references']['UPDATE_METADATA']), 1)
        self.assertEqual(len(delta['actions']['vectors']['ADD']), 1)
        self.assertEqual(len(delta['actions']['chunks']['UPDATE_METADATA']), 1)
        self.assertEqual(delta['blocked'], [])

    def test_float_metadata_rejected_float32_signed_zero(self):
        with self.assertRaises(VersionError):
            canonical({'x': 1.5})
        # El cero negativo se conserva exacto: pgvector 0.8.2 guarda el bit
        # 0x80000000 y la prueba de hashes debe reflejar los bytes reales.
        self.assertEqual(vector_hex([-0.0, 1.0]), '800000003f800000')
        self.assertEqual(vector_hex([0.0, 1.0]), '000000003f800000')
        with self.assertRaises(VersionError):
            vector_hex([float('nan')])

    def test_signed_zero_survives_manifest_and_restore_roundtrip(self):
        from scripts.rag_versions import restore_artifacts
        from scripts.import_supabase import load_local
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model'
            model.mkdir()
            (model / 'model.json').write_text(json.dumps({'dimensions': 384, 'max_tokens': 512, 'repository': 'synthetic', 'revision': 'v1'}))
            data = fixture()
            data['metadata']['model_fingerprint'] = hashlib.sha256(json.dumps({'dimensions': 384, 'max_tokens': 512, 'repository': 'synthetic', 'revision': 'v1'}, sort_keys=True).encode()).hexdigest()
            data['vectors'][0][7] = -0.0
            data['vectors'][1][5] = -0.0
            data['vectors'][1][9] = -0.0
            value = build_manifest(data)
            by_chunk = {row['chunk_id']: row['vector_hex'] for row in value['rows']['vectors'].values()}
            words = lambda hexes: {hexes[i:i+8] for i in range(0, len(hexes), 8)}
            self.assertIn('80000000', words(by_chunk[data['chunks'][0]['id']]))
            self.assertEqual(words(by_chunk[data['chunks'][1]['id']]), {'00000000', '3f800000', '80000000'})
            self.assertNotIn('80000000', words(by_chunk[data['chunks'][2]['id']]))
            paths = restore_artifacts(value, root / 'recovered')
            restored = load_local(paths['chunks.jsonl'], paths['report.json'], paths['semantic.npz'], model)
            self.assertEqual(build_manifest(restored), value)

    def test_model_change_even_identical_vectors(self):
        data = fixture()
        base = build_manifest(data)
        data['metadata']['model_revision'] = 'v2'
        target = build_manifest(data)
        delta = plan(base, target)
        self.assertEqual(len(delta['actions']['vectors']['ADD']), 4)
        self.assertIn('embedding_config_change_requires_authorization', delta['blocked'])

    def test_shared_content_reference_not_removed(self):
        data = fixture()
        extra = copy.deepcopy(data['chunks'][0]['citations'][0])
        extra['source']['resource_id'] = 'external:shared'
        extra['source']['url'] = 'https://example.invalid/shared'
        data['chunks'][0]['citations'].append(extra)
        base = build_manifest(rehash_inputs(data))
        data['chunks'][0]['citations'].pop(0)
        target = build_manifest(rehash_inputs(data))
        self.assertEqual(plan(base, target)['actions']['chunks']['REMOVE'], [])
        self.assertEqual(len(plan(base, target)['actions']['references']['REMOVE']), 1)

    def test_missing_retained_but_edit_not_resurrected(self):
        data = fixture()
        base = build_manifest(data)
        observed = [c['citations'][0]['source']['resource_id'] for c in data['chunks'][1:]]
        data['chunks'].pop(0)
        data['vectors'].pop(0)
        data['metadata']['windows'].pop(0)
        candidate = build_manifest(data)
        with self.assertRaisesRegex(VersionError, 'inventory_incomplete'):
            retain_missing(base, candidate, observed)
        target = retain_missing(base, candidate, observed, inventory_complete=True)
        self.assertEqual(len(target['rows']['chunks']), 4)
        self.assertFalse(target['resources']['moodle:course:1:cm:0']['moodle_active'])
        self.assertEqual(plan(base, target)['ratios']['removed_resources'], 0)
        # Observado pero excluido: no conservar su texto antiguo.
        target = retain_missing(base, candidate, list(base['resources']), inventory_complete=True)
        self.assertEqual(len(target['rows']['chunks']), 3)

    def test_threshold_strict_and_replacements_not_double_counted(self):
        base = build_manifest(fixture())
        data = fixture(5)
        target = build_manifest(data)
        delta = plan(base, target)
        self.assertEqual(delta['ratios']['changed_chunks'], .25)
        self.assertEqual(delta['blocked'], [])
        larger = build_manifest(fixture(6))
        self.assertIn('changed_chunks_over_25_percent', plan(base, larger)['blocked'])
        self.assertEqual(plan(None, larger)['ratios']['changed_chunks'], 0)

    def test_immutable_and_operation_independent(self):
        value = build_manifest(fixture())
        with tempfile.TemporaryDirectory() as directory:
            path = write_version(value, directory)
            self.assertEqual(read_manifest(path/'manifest.json'), value)
            self.assertEqual(write_version(value, directory), path)
            corrupt = copy.deepcopy(value)
            corrupt['rows']['chunks'].clear()
            with self.assertRaises(VersionError):
                write_version(corrupt, directory)
        self.assertTrue(plan(value, value)['unchanged'])

    def test_archived_and_active_revisions_coexist_same_resource(self):
        # Colisión real reportada: mismo recurso (URL/miembro de archivo) con
        # dos revisiones físicas, una archivada inactiva y la activa.
        data = fixture()
        archived = copy.deepcopy(data['chunks'][0]['citations'][0])
        archived['source'].update(sha256='3'*64, title='Revisión archivada', moodle_active=False,
                                  resource_id=data['chunks'][0]['citations'][0]['source']['resource_id'])
        data['chunks'][0]['citations'].append(archived)
        # Y en el mismo chunk, con método y localizadores idénticos: los IDs de
        # referencia se distinguen por la revisión física, sin descartar filas.
        same_chunk = copy.deepcopy(data['chunks'][0]['citations'][0])
        same_chunk['source'].update(sha256='4'*64, title='Revisión previa', moodle_active=False)
        data['chunks'][0]['citations'].append(same_chunk)
        value = build_manifest(rehash_inputs(data))
        rid = data['chunks'][0]['citations'][0]['source']['resource_id']
        revisions = value['resources'][rid]['revisions']
        self.assertEqual(len(revisions), 3)
        active = value['resources'][rid]
        self.assertTrue(active['moodle_active'])
        self.assertEqual(active['sha256'], data['chunks'][0]['citations'][0]['source']['sha256'])
        self.assertEqual(len({r['source_key'] for r in value['rows']['references'].values() if r['chunk_id'] == data['chunks'][0]['id']}), 3)
        self.assertEqual(value['checks']['occurrences']['count'], 6)
        # Edición posterior: identidad estable, retirada lógica cero, revisión contada.
        data['chunks'][0]['citations'][0]['source'].update(sha256='5'*64, title='Editado')
        edited = build_manifest(rehash_inputs(data))
        delta = plan(value, edited)
        self.assertEqual(delta['ratios']['removed_resources'], 0)
        self.assertEqual(delta['resource_changes'], {'removed': 0, 'added': 0, 'revised': 1})
        self.assertEqual(len(delta['actions']['references']['REMOVE']), 1)
        self.assertEqual(len(delta['actions']['references']['ADD']), 1)
        self.assertEqual(len(delta['actions']['occurrences']['REMOVE']), 1)
        # Reconstrucción de derivados conserva todas las revisiones.
        restored_rid = {key: rid for rid, entry in edited['resources'].items() for key in entry['revisions']}
        self.assertEqual(len(restored_rid), 6)

    def test_multiple_active_revisions_rejected(self):
        data = fixture()
        second = copy.deepcopy(data['chunks'][0]['citations'][0])
        second['source'].update(sha256='6'*64, title='Otra activa', resource_id=data['chunks'][0]['citations'][0]['source']['resource_id'])
        data['chunks'][0]['citations'].append(second)
        with self.assertRaisesRegex(VersionError, 'multiple_active_revisions'):
            build_manifest(rehash_inputs(data))

    def test_same_sha_active_metadata_occurrences_share_resource(self):
        # El mismo original citado en dos secciones: dos ocurrencias de
        # metadatos activas del mismo sha no son revisiones de contenido.
        data = fixture()
        sha = data['chunks'][0]['citations'][0]['source']['sha256']
        second = copy.deepcopy(data['chunks'][1]['citations'][0])
        second['source'].update(sha256=sha, resource_id=data['chunks'][0]['citations'][0]['source']['resource_id'],
                                section='Otra sección', title='Otra aparición')
        data['chunks'][1]['citations'] = [second]
        value = build_manifest(rehash_inputs(data))
        rid = data['chunks'][0]['citations'][0]['source']['resource_id']
        entry = value['resources'][rid]
        self.assertEqual(len(entry['revisions']), 2)
        self.assertEqual({r['sha256'] for r in entry['revisions'].values()}, {data['chunks'][0]['citations'][0]['source']['sha256']})
        self.assertEqual(entry['source_key'], min(entry['revisions']))
        data['chunks'].reverse()
        data['metadata']['windows'].reverse()
        data['vectors'].reverse()
        self.assertEqual(value, build_manifest(rehash_inputs(data)))

    def test_same_ref_with_different_metadata_occurrence_is_ambiguous(self):
        # Misma cita física (chunk, recurso, sha, método, localizadores) con dos
        # ocurrencias de metadatos distintas: colisión de referencia que el
        # operador debe diagnosticar; no se descarta ninguna fila en silencio.
        data = fixture()
        second = copy.deepcopy(data['chunks'][0]['citations'][0])
        second['source'].update(title='Mismo contenido, otra aparición',
                                resource_id=data['chunks'][0]['citations'][0]['source']['resource_id'])
        data['chunks'][0]['citations'].append(second)
        with self.assertRaisesRegex(VersionError, 'ambiguous_reference_occurrence'):
            build_manifest(rehash_inputs(data))

    def test_same_occurrence_key_conflicting_status_rejected(self):
        data = fixture()
        second = copy.deepcopy(data['chunks'][0]['citations'][0])
        second['source'].update(moodle_active=False)  # mismos 8 campos, estado opuesto
        data['chunks'][0]['citations'].append(second)
        with self.assertRaisesRegex(VersionError, 'ambiguous_occurrence_status'):
            build_manifest(rehash_inputs(data))

    def test_all_inactive_resource_representative_is_deterministic(self):
        data = fixture()
        data['chunks'][0]['citations'][0]['source'].update(sha256='7'*64, moodle_active=False)
        inactive = copy.deepcopy(data['chunks'][0]['citations'][0])
        inactive['source'].update(sha256='8'*64, title='Otra inactiva')
        data['chunks'][0]['citations'].append(inactive)
        value = build_manifest(rehash_inputs(data))
        rid = data['chunks'][0]['citations'][0]['source']['resource_id']
        entry = value['resources'][rid]
        self.assertFalse(entry['moodle_active'])
        self.assertEqual(entry['source_key'], min(entry['revisions']))
        data['chunks'].reverse()
        data['metadata']['windows'].reverse()
        data['vectors'].reverse()
        self.assertEqual(value, build_manifest(rehash_inputs(data)))


if __name__ == '__main__':
    unittest.main()
