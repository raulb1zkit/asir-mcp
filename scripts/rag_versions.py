#!/usr/bin/env python3
"""Manifiestos inmutables y planes delta locales del corpus (sin modelos ni red)."""
import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import struct

try:
    from .import_supabase import SearchFailure, source_key, validate_coverage
except ImportError:
    from import_supabase import SearchFailure, source_key, validate_coverage


class VersionError(ValueError):
    pass


def canonical(value):
    """JSON UTF-8 compacto; números de metadatos exclusivamente enteros."""
    def check(v):
        if isinstance(v, float):
            raise VersionError('metadata_float_not_supported')
        if isinstance(v, str) and ('\x00' in v or any(0xD800 <= ord(c) <= 0xDFFF for c in v)):
            raise VersionError('invalid_unicode')
        if isinstance(v, dict):
            if not all(isinstance(k, str) for k in v):
                raise VersionError('non_string_key')
            for k, x in v.items():
                check(k)
                check(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                check(x)
        elif not isinstance(v, (str, int, bool, type(None))):
            raise VersionError('unsupported_canonical_value')
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def vector_hex(vector):
    values = [float(x) for x in vector]
    if not values or not all(math.isfinite(x) for x in values):
        raise VersionError('invalid_vector')
    # IEEE754 float32 big endian: conservar el cero negativo exacto. En
    # pgvector 0.8.2 vector_send conserva el bit 0x80000000 y la entrada
    # textual -0.0 lo reconstruye; normalizarlo aquí ocultaría bytes reales
    # del servidor y falsearía la prueba de hashes.
    return struct.pack('>' + 'f' * len(values), *values).hex()


def signature(rows):
    hashes = sorted(digest(row) for row in rows.values())
    return {'count': len(hashes), 'sha256': hashlib.sha256(''.join(hashes).encode('ascii')).hexdigest()}


TABLES = ('sources', 'occurrences', 'chunks', 'references', 'vectors', 'config')
SOURCE_FIELDS = ('sha256', 'subject', 'section', 'title', 'filename', 'url', 'mime', 'path')
CONFIG_FIELDS = ('model_fingerprint', 'model_repository', 'model_revision', 'dimensions', 'max_tokens', 'overlap_tokens', 'context')


def resource_id(source, resource_map):
    """No inferir identidad de sha/ruta/título. Exigir registro estable explícito."""
    value = source.get('resource_id') or resource_map.get(source_key(source)) or resource_map.get(source.get('url'))
    if not isinstance(value, str) or not value.startswith(('moodle:', 'external:')):
        raise VersionError('stable_resource_id_required')
    return value


def _representative(revisions):
    """Revisión representativa del recurso: la activa, o la mínima canónica.

    Un recurso puede conservar varias revisiones (por ejemplo la revisión
    archivada inactiva junto a la activa) y varias ocurrencias de metadatos del
    mismo sha (aparece en dos secciones): no son revisiones de contenido.
    Solo se rechaza más de un sha activo distinto; entre ocurrencias activas
    del mismo sha se elige el source_key mínimo, sin depender del orden.
    """
    actives = sorted(k for k, revision in revisions.items() if revision['moodle_active'] is True)
    if len({revisions[k]['sha256'] for k in actives}) > 1:
        raise VersionError('multiple_active_revisions_of_resource')
    return actives[0] if actives else min(revisions)


def embedding_prefix(chunk, context):
    prefixes = ["passage: " + c['source']['subject'] + " | " + c['source']['section'][:120] + " | " + c['source']['title'][:160] + "\n" for c in chunk['citations']]
    if context == 'stable-subject-section-title-v2':
        return min(prefixes)
    if context == 'subject-section-title':
        return prefixes[0]  # Bootstrap: respetar exactamente la entrada histórica.
    raise VersionError('unsupported_embedding_context')


def build_manifest(data, resource_map=None):
    resource_map = resource_map or {}
    rows = {t: {} for t in TABLES}
    resources = {}
    resource_by_occurrence = {}
    config = {k: data['metadata'][k] for k in CONFIG_FIELDS}
    rows['config']['embedding'] = config
    rows['config']['pipeline'] = {k: data['report'][k] for k in ('schema_version', 'chunker_version')}
    rows['config']['pipeline']['config'] = deepcopy(data['report'].get('config', {}))
    chunks = data['chunks']
    ids = {c['id'] for c in chunks}
    if len(ids) != len(chunks) or not ids:
        raise VersionError('empty_or_duplicate_chunks')
    for chunk in chunks:
        if chunk['id'] != 'chunk:' + hashlib.sha256(chunk['text'].encode()).hexdigest():
            raise VersionError('chunk_hash_mismatch')
        if chunk['text_sha256'] != chunk['id'][6:] or chunk['characters'] != len(chunk['text']):
            raise VersionError('chunk_payload_mismatch')
        if not chunk.get('citations'):
            raise VersionError('chunk_without_references')
        titles, sections = set(), set()
        for citation in chunk['citations']:
            source = {k: citation['source'].get(k) for k in SOURCE_FIELDS}
            rid = resource_id(citation['source'], resource_map)
            key = source_key(source)
            if key in resource_by_occurrence and resource_by_occurrence[key] != rid:
                raise VersionError('ambiguous_resource_occurrence')
            resource_by_occurrence[key] = rid
            # La misma ocurrencia citada dos veces debe tener un estado único:
            # None (desconocido) cede ante un valor explícito; True y False a la
            # vez son ambigüedad, no identidad. Resuelto así, el manifiesto no
            # depende del orden de los chunks.
            flag = citation['source'].get('moodle_active')
            previous = rows['occurrences'].get(key)
            explicit = {previous['moodle_active'], flag} - {None} if previous else {flag} - {None}
            if len(explicit) > 1:
                raise VersionError('ambiguous_occurrence_status')
            resolved = next(iter(explicit)) if explicit else None
            occurrence = {**source, 'source_key': key, 'moodle_active': resolved}
            rows['occurrences'][key] = occurrence
            # Revisiones retenidas del mismo recurso lógico: la inactiva
            # archivada y la activa conviven; todas sus filas se conservan.
            entry = resources.setdefault(rid, {'revisions': {}})
            entry['revisions'][key] = {'sha256': source['sha256'], 'moodle_active': resolved}
            representative = _representative(entry['revisions'])
            entry.update(source_key=representative, sha256=entry['revisions'][representative]['sha256'],
                         moodle_active=entry['revisions'][representative]['moodle_active'])
            # sources es un catálogo físico por original; elegir representante determinista.
            old = rows['sources'].get(source['sha256'])
            if old is None or canonical(source) < canonical(old):
                rows['sources'][source['sha256']] = source
            ref = {'chunk_id': chunk['id'], 'source_sha256': source['sha256'], 'source_key': key,
                   'method': citation['method'], 'previous_chunk_id': citation.get('previous_chunk_id'),
                   'next_chunk_id': citation.get('next_chunk_id'), 'segments': deepcopy(citation['segments'])}
            if not ref['segments'] or any(ref[k] is not None and ref[k] not in ids for k in ('previous_chunk_id', 'next_chunk_id')):
                raise VersionError('invalid_reference')
            for seg in ref['segments']:
                if not 0 <= seg['start'] <= seg['end'] <= len(chunk['text']):
                    raise VersionError('invalid_segment')
            # Localizadores/unidades, recurso y revisión física (sha256)
            # distinguen dos citas: una edición de metadatos con igual sha
            # conserva el ID de referencia (UPDATE_METADATA), mientras dos
            # revisiones del mismo recurso en un mismo chunk no colisionan.
            ref_id = digest([chunk['id'], rid, ref['source_sha256'], ref['method'], [(s['source_unit_id'], s['locator']) for s in ref['segments']]])
            previous_ref = rows['references'].get(ref_id)
            if previous_ref is not None:
                # Mismo chunk, recurso, sha, método y localizadores: si solo
                # cambia la ocurrencia de metadatos la referencia es ambigua
                # (diagnóstico del operador); si es idéntica, cita duplicada.
                if previous_ref['source_key'] != key:
                    raise VersionError('ambiguous_reference_occurrence')
                raise VersionError('duplicate_reference')
            rows['references'][ref_id] = ref
            titles.update((source['title'] + ' ' + source['filename']).split('\n'))
            sections.update(source['section'].split('\n'))
        rows['chunks'][chunk['id']] = {k: chunk[k] for k in ('id', 'text', 'text_sha256', 'characters', 'warnings', 'requires_review')}
        rows['chunks'][chunk['id']].update(titles=sorted(titles), sections=sorted(sections))
    chunk_map = {c['id']: c for c in chunks}
    covered = set()
    spans_by_chunk = {}
    for window, vector in zip(data['metadata']['windows'], data['vectors'], strict=True):
        payload = {k: window[k] for k in ('chunk_id', 'char_start', 'char_end', 'tokens')}
        if payload['chunk_id'] not in ids or not 0 <= payload['char_start'] < payload['char_end'] <= len(rows['chunks'][payload['chunk_id']]['text']):
            raise VersionError('invalid_window')
        binary = vector_hex(vector)
        if len(binary) != config['dimensions'] * 8:
            raise VersionError('vector_dimensions')
        chunk = chunk_map[payload['chunk_id']]
        input_hash = hashlib.sha256((embedding_prefix(chunk, config['context']) + chunk['text'][payload['char_start']:payload['char_end']]).encode()).hexdigest()
        if config['context'] == 'stable-subject-section-title-v2' and window.get('input_sha256') != input_hash:
            raise VersionError('embedding_input_not_verified')
        payload['input_sha256'] = input_hash
        payload['vector_hex'] = binary
        payload['config_hash'] = digest(config)
        key = digest([payload['chunk_id'], payload['char_start'], payload['char_end'], input_hash, payload['config_hash']])
        if key in rows['vectors']:
            raise VersionError('duplicate_window')
        rows['vectors'][key] = payload
        covered.add(payload['chunk_id'])
        spans_by_chunk.setdefault(payload['chunk_id'], []).append(payload)
    if covered != ids:
        raise VersionError('vector_coverage')
    for cid, spans in spans_by_chunk.items():
        end = 0
        for span in sorted(spans, key=lambda s: s['char_start']):
            if span['char_start'] > end or not 0 < span['tokens'] <= config['max_tokens']:
                raise VersionError('incomplete_vector_coverage')
            end = max(end, span['char_end'])
        if end != len(rows['chunks'][cid]['text']):
            raise VersionError('incomplete_vector_coverage')
    return seal(rows, resources)


def seal(rows, resources):
    result = {'format': 1, 'rows': rows, 'resources': resources}
    result['checks'] = {t: signature(rows[t]) for t in TABLES}
    result['content_root'] = digest({'format': 1, 'checks': result['checks'], 'resources': resources})
    result['version'] = result['content_root']
    return result


def _validate_resources(resources):
    keys_by_resource = {}
    for rid, entry in resources.items():
        revisions = entry.get('revisions') if isinstance(entry, dict) else None
        if not isinstance(revisions, dict) or not revisions:
            raise VersionError('manifest_resources_invalid')
        representative = entry.get('source_key')
        if representative not in revisions or entry.get('sha256') != revisions[representative]['sha256'] \
                or entry.get('moodle_active') != revisions[representative]['moodle_active']:
            raise VersionError('manifest_resources_invalid')
        if _representative(revisions) != representative:
            raise VersionError('manifest_resources_invalid')
        keys_by_resource.update({key: rid for key in revisions})
    return keys_by_resource


def validate_manifest(manifest):
    keys_by_resource = _validate_resources(manifest['resources'])
    for ref in manifest['rows']['references'].values():
        if ref['source_key'] not in keys_by_resource:
            raise VersionError('manifest_resources_invalid')
    if seal(manifest['rows'], manifest['resources']) != manifest:
        raise VersionError('manifest_integrity')
    return manifest


def retain_missing(base, candidate, observed_resources, *, inventory_complete=False):
    """Conservar ausencias solo tras inventario completo; ediciones no se restauran."""
    if not inventory_complete:
        raise VersionError('inventory_incomplete')
    result = deepcopy(candidate)
    missing = set(base['resources']) - set(observed_resources)
    if missing & set(candidate['resources']):
        raise VersionError('inventory_candidate_conflict')
    for rid in missing:
        resource = deepcopy(base['resources'][rid])
        # Desaparición lógica: todas las revisiones retenidas quedan inactivas,
        # ninguna fila ni referencia se descarta en silencio.
        for revision in resource['revisions'].values():
            revision['moodle_active'] = False
        resource['moodle_active'] = False
        representative = _representative(resource['revisions'])
        resource.update(source_key=representative, sha256=resource['revisions'][representative]['sha256'],
                        moodle_active=resource['revisions'][representative]['moodle_active'])
        result['resources'][rid] = resource
        for sk, revision in resource['revisions'].items():
            occurrence = deepcopy(base['rows']['occurrences'][sk])
            occurrence['moodle_active'] = False
            result['rows']['occurrences'][sk] = occurrence
            result['rows']['sources'].setdefault(revision['sha256'], deepcopy(base['rows']['sources'][revision['sha256']]))
            for key, ref in base['rows']['references'].items():
                if ref['source_key'] != sk:
                    continue
                result['rows']['references'][key] = deepcopy(ref)
                cid = ref['chunk_id']
                result['rows']['chunks'].setdefault(cid, deepcopy(base['rows']['chunks'][cid]))
                for vk, vec in base['rows']['vectors'].items():
                    if vec['chunk_id'] == cid:
                        if vec['config_hash'] != digest(result['rows']['config']['embedding']):
                            raise VersionError('retained_resource_requires_reembedding')
                        result['rows']['vectors'].setdefault(vk, deepcopy(vec))
    # Recalcular metadatos de búsqueda según referencias vigentes, sin títulos obsoletos.
    for cid, chunk in result['rows']['chunks'].items():
        occurrences = [result['rows']['occurrences'][r['source_key']] for r in result['rows']['references'].values() if r['chunk_id'] == cid]
        chunk['titles'] = sorted({s for o in occurrences for s in (o['title'] + ' ' + o['filename']).split('\n')})
        chunk['sections'] = sorted({s for o in occurrences for s in o['section'].split('\n')})
    return seal(result['rows'], result['resources'])


def plan(base, target):
    validate_manifest(target)
    if base:
        validate_manifest(base)
    actions = {}
    for table in TABLES:
        before = base['rows'][table] if base else {}
        after = target['rows'][table]
        flags = {name: [] for name in ('KEEP', 'ADD', 'REMOVE', 'UPDATE_METADATA')}
        for key in sorted(set(before) | set(after)):
            flag = 'ADD' if key not in before else 'REMOVE' if key not in after else 'KEEP' if before[key] == after[key] else 'UPDATE_METADATA'
            # Vectores con nuevo contexto/config se sustituyen aunque bytes iguales.
            flags[flag].append(key)
        actions[table] = flags
    removed_resources = len(set(base['resources']) - set(target['resources'])) if base else 0
    added_resources = len(set(target['resources']) - set(base['resources'])) if base else 0
    # Cambio de revisión/contenido con identidad estable: no es retirada lógica.
    revised_resources = sum(1 for rid in set(base['resources']) & set(target['resources'])
                            if base['resources'][rid].get('revisions') != target['resources'][rid].get('revisions')) if base else 0
    removed = len(actions['chunks']['REMOVE'])
    added = len(actions['chunks']['ADD'])
    denominator = len(base['rows']['chunks']) if base else 0
    # Reemplazo REMOVE+ADD cuenta una vez; altas/retiradas netas también cuentan.
    chunk_fraction = max(removed, added) / denominator if denominator else 0
    resource_fraction = removed_resources / len(base['resources']) if base and base['resources'] else 0
    blocked = []
    if resource_fraction > .10:
        blocked.append('removed_resources_over_10_percent')
    if chunk_fraction > .25:
        blocked.append('changed_chunks_over_25_percent')
    if base and base['rows']['config'] != target['rows']['config']:
        blocked.append('embedding_config_change_requires_authorization')
    return {'base': base['version'] if base else None, 'target': target['version'], 'actions': actions,
            'unchanged': bool(base and base['version'] == target['version']), 'blocked': blocked,
            'estimated_payload_bytes': sum(len(canonical(target['rows'][t][k])) for t in TABLES for k in actions[t]['ADD'] + actions[t]['UPDATE_METADATA']),
            'ratios': {'removed_resources': resource_fraction, 'changed_chunks': chunk_fraction},
            'resource_changes': {'removed': removed_resources, 'added': added_resources, 'revised': revised_resources},
            'counts': {t: {f: len(keys) for f, keys in flags.items()} for t, flags in actions.items()}}


def write_version(manifest, root, artifacts=None):
    """Crear una versión una sola vez. Rutas/hashes de artefactos fuera de identidad."""
    import os
    validate_manifest(manifest)
    directory = Path(root) / manifest['version']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'manifest.json'
    payload = canonical(manifest) + b'\n'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise VersionError('immutable_version_conflict') from None
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    if artifacts and not (directory / 'artifacts.json').exists():
        # Registro separado; no copiar documentos ni incluir rutas en métricas.
        records = {name: {'path': str(Path(p).resolve()), 'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()} for name, p in artifacts.items()}
        with (directory / 'artifacts.json').open('x', encoding='utf-8') as stream:
            json.dump(records, stream, sort_keys=True)
    return directory


def restore_artifacts(manifest, destination):
    """Reconstruir derivados recuperables sin modelo, originales, red ni inferencia.

    destination debe ser nuevo. El modelo exacto se provisiona por separado antes
    de validar con load_local o servir consultas; no se fabrica su identidad.
    """
    import os
    import numpy as np
    validate_manifest(manifest)
    rows = manifest['rows']
    config = rows['config']['embedding']
    rid_by_occurrence = {key: rid for rid, entry in manifest['resources'].items()
                         for key in entry['revisions']}
    chunks = []
    # El contrato de load_local es el orden del archivo: validate_coverage exige
    # que la última ventana de cada chunk termine en len(text). Ordenar solo por
    # (chunk_id, char_start, char_end) deja tras la ventana terminal una ventana
    # corta solapada cuyo char_start es mayor (la ventana terminal de un
    # segmento previo del propio chunk). Se ordena por (char_start, char_end)
    # y se fija la ventana terminal en última posición; después se comprueba
    # la cobertura de cada chunk con la misma función que usa load_local,
    # antes de escribir ningún artefacto. El multiconjunto de ventanas no cambia.
    spans_by_chunk = {}
    for key, row in rows['vectors'].items():
        spans_by_chunk.setdefault(row['chunk_id'], []).append((key, row))
    ordered_vectors = []
    for cid in sorted(spans_by_chunk):
        text_length = len(rows['chunks'][cid]['text'])
        items = sorted(spans_by_chunk[cid], key=lambda kv: (kv[1]['char_start'], kv[1]['char_end'], kv[0]))
        terminal = [i for i, (_, row) in enumerate(items) if row['char_end'] == text_length]
        if len(terminal) != 1:
            raise VersionError('terminal_window_not_unique')
        items.append(items.pop(terminal[0]))
        try:
            validate_coverage([{k: row[k] for k in ('chunk_id', 'char_start', 'char_end', 'tokens')}
                               for _, row in items], text_length, config['max_tokens'])
        except SearchFailure as error:
            raise VersionError('restored_window_coverage_invalid') from error
        ordered_vectors.extend(items)
    for cid, row in sorted(rows['chunks'].items()):
        chunk = {k: deepcopy(row[k]) for k in ('id', 'text', 'text_sha256', 'characters', 'warnings', 'requires_review')}
        chunk.update(schema_version=1, citations=[])
        for _, ref in sorted(rows['references'].items()):
            if ref['chunk_id'] != cid:
                continue
            source = deepcopy(rows['occurrences'][ref['source_key']])
            source.pop('source_key')
            source['resource_id'] = rid_by_occurrence[ref['source_key']]
            citation = {k: deepcopy(ref[k]) for k in ('method', 'previous_chunk_id', 'next_chunk_id', 'segments')}
            citation['source'] = source
            chunk['citations'].append(citation)
        if config['context'] == 'subject-section-title':
            vector = next(v for _, v in ordered_vectors if v['chunk_id'] == cid)
            text = chunk['text'][vector['char_start']:vector['char_end']]
            chosen = next((c for c in chunk['citations'] if hashlib.sha256((embedding_prefix({'citations': [c]}, config['context']) + text).encode()).hexdigest() == vector['input_sha256']), None)
            if chosen is None:
                raise VersionError('legacy_embedding_prefix_not_found')
            chunk['citations'].remove(chosen)
            chunk['citations'].insert(0, chosen)
        chunks.append(chunk)
    vectors = np.asarray([np.frombuffer(bytes.fromhex(v['vector_hex']), dtype='>f4') for _, v in ordered_vectors], dtype=np.float32)
    windows = [{k: v[k] for k in ('chunk_id', 'char_start', 'char_end', 'tokens', 'input_sha256')} for _, v in ordered_vectors]
    payload = b''.join(canonical(c) + b'\n' for c in chunks)
    corpus_hash = hashlib.sha256(payload).hexdigest()
    report = {**deepcopy(rows['config']['pipeline']), 'complete': True, 'created_at': '1970-01-01T00:00:00+00:00', 'output_sha256': corpus_hash}
    metadata = {**deepcopy(config), 'schema_version': 1, 'corpus_sha256': corpus_hash,
                'vectors_sha256': hashlib.sha256(vectors.tobytes()).hexdigest(), 'windows': windows,
                'summary': {'chunks': len(chunks), 'windows': len(windows)}}
    # Comprobar semántica antes de escribir cualquier artefacto.
    if build_manifest({'chunks': chunks, 'report': report, 'metadata': metadata, 'vectors': vectors}) != manifest:
        raise VersionError('artifact_reconstruction_mismatch')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    chunk_dir, search_dir = destination / 'all/chunks', destination / 'all/search'
    chunk_dir.mkdir(parents=True, mode=0o700)
    search_dir.mkdir(parents=True, mode=0o700)
    paths = {'chunks.jsonl': chunk_dir / 'chunks.jsonl', 'report.json': chunk_dir / 'report.json', 'semantic.npz': search_dir / 'semantic.npz'}
    for path, data in ((paths['chunks.jsonl'], payload), (paths['report.json'], canonical(report)), (search_dir / 'semantic.json', canonical(metadata))):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
    fd = os.open(paths['semantic.npz'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        np.savez(stream, vectors=vectors, metadata=np.array(json.dumps(metadata)))
    return paths


def read_manifest(path):
    return validate_manifest(json.loads(Path(path).read_text(encoding='utf-8')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chunks', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--semantic', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--resource-map', type=Path, required=True)
    parser.add_argument('--base', type=Path)
    parser.add_argument('--versions-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        try:
            from .import_supabase import load_local
        except ImportError:
            from import_supabase import load_local
        data = load_local(args.chunks, args.report, args.semantic, args.model)
        target = build_manifest(data, json.loads(args.resource_map.read_text()))
        write_version(target, args.versions_dir, {'chunks.jsonl': args.chunks, 'report.json': args.report, 'semantic.npz': args.semantic})
        result = plan(read_manifest(args.base) if args.base else None, target)
        print(json.dumps({k: v for k, v in result.items() if k != 'actions'}, ensure_ascii=False))
        return 0
    except (VersionError, ValueError, OSError):
        print(json.dumps({'error': 'local_version_validation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
