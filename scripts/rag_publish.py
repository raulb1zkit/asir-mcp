#!/usr/bin/env python3
"""Publicación delta PostgreSQL con CAS, comprobación SQL y ledger de operaciones."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import struct
import uuid

try:
    from .rag_versions import (SOURCE_FIELDS, TABLES, canonical, digest, plan, read_manifest,
                               validate_manifest, VersionError)
except ImportError:
    from rag_versions import (SOURCE_FIELDS, TABLES, canonical, digest, plan, read_manifest,
                              validate_manifest, VersionError)


class PublishError(RuntimeError):
    pass


class ReconcileDenied(PublishError):
    """Negación de reconciliación con recuentos seguros (nunca filas ni contenido)."""

    def __init__(self, reason, counts):
        super().__init__(reason)
        self.counts = counts


class StatusConflict(RuntimeError):
    """El estado cambió respecto a la lectura esperada; no se aplica la mezcla."""


# Fases cuya versión sigue sin resolver: entra en pending_versions.
PENDING_PHASES = frozenset({'prepared', 'applying', 'committed_pending_probe', 'recovery_required'})
# Fases que consolidan la versión como publicada actual.
PUBLISHED_PHASES = frozenset({'published', 'reverted'})
BACKUP_FIELDS = ('backup', 'backup_bytes', 'backup_operations', 'backup_at', 'backup_version')


def install_schema(connection):
    """Solo bootstrap administrativo explícito. No hace commit implícito."""
    with connection.cursor() as cur:
        cur.execute((Path(__file__).parent / 'sql/rag_versioning.sql').read_text())


def checks(connection):
    """Una fila por grupo; nunca devuelve textos/vectores ni hashes de cada fila.

    El hash canónico se evalúa una sola vez por fila en un CTE materializado:
    el agregado original lo recalculaba como argumento y como clave de orden,
    duplicando el coste sobre el corpus completo.
    """
    with connection.cursor() as cur:
        cur.execute("""WITH hashed AS MATERIALIZED (
          SELECT kind, asir.rag_hash(payload) AS h FROM asir.rag_rows)
          SELECT kind, count(*) AS count,
          encode(sha256(convert_to(string_agg(h, '' ORDER BY h COLLATE "C"),'UTF8')),'hex') AS sha256
          FROM hashed GROUP BY kind""")
        found = {r['kind']: {'count': r['count'], 'sha256': r['sha256']} for r in cur.fetchall()}
    import hashlib
    return {t: found.get(t, {'count': 0, 'sha256': hashlib.sha256(b'').hexdigest()}) for t in TABLES}


def verify(connection, manifest, *, expected_corpus_id=None):
    with connection.cursor() as cur:
        cur.execute('SELECT c.corpus_sha256 AS corpus_id,c.schema_version,m.corpus_sha256 AS embedding_corpus_id FROM asir.corpus c CROSS JOIN asir.embedding_metadata m')
        control = cur.fetchall()
        if len(control) != 1 or control[0]['schema_version'] != 1 or control[0]['corpus_id'] != control[0]['embedding_corpus_id']:
            raise PublishError('remote_control_state_mismatch')
        if expected_corpus_id is None:
            cur.execute('SELECT corpus_id FROM asir.rag_head WHERE id=1 AND version=%s', (manifest['version'],))
            head = cur.fetchone()
            expected_corpus_id = head['corpus_id'] if head else None
        if expected_corpus_id is not None and control[0]['corpus_id'] != expected_corpus_id:
            raise PublishError('remote_control_state_mismatch')
    result = checks(connection)
    if result != manifest['checks']:
        raise PublishError('remote_state_mismatch')
    return result


def lock(cur):
    # Mismo bloqueo que el importador antiguo, para excluir una migración concurrente.
    cur.execute("SET LOCAL statement_timeout='180s'")
    cur.execute("SET LOCAL lock_timeout='30s'")
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('asir-import'))")
    # Bloquear también DML ajeno al protocolo mientras calculamos la prueba completa.
    cur.execute('LOCK TABLE asir.corpus, asir.embedding_metadata, asir.sources, asir.source_occurrences, asir.chunks, asir.citations, asir.citation_segments, asir.embedding_windows IN SHARE ROW EXCLUSIVE MODE')
    cur.execute("SET LOCAL statement_timeout='180s'")


def bootstrap(connection, manifest):
    """Reconciliar corpus existente server-side. No importar ni descargar vectores."""
    validate_manifest(manifest)
    with connection.transaction():
        with connection.cursor() as cur:
            lock(cur)
            cur.execute('SELECT version FROM asir.rag_head WHERE id=1 FOR UPDATE')
            head = cur.fetchone()
            if head and head['version'] != manifest['version']:
                raise PublishError('bootstrap_already_initialized')
            result = verify(connection, manifest)
            if not head:
                cur.execute('INSERT INTO asir.rag_head(id,version,corpus_id) SELECT 1,%s,corpus_sha256 FROM asir.corpus WHERE id=1', (manifest['version'],))
    return result


def canonical_source_rows(data):
    """Mismo catálogo canónico mínimo por sha que construye build_manifest."""
    rows = {}
    for chunk in data['chunks']:
        for citation in chunk['citations']:
            source = {k: citation['source'].get(k) for k in SOURCE_FIELDS}
            old = rows.get(source['sha256'])
            if old is None or canonical(source) < canonical(old):
                rows[source['sha256']] = source
    return rows


def legacy_source_rows(data):
    """Último representante por sha: lo que escribió el importador antiguo.

    import_supabase iteraba chunks y citas en orden y sobrescribía la fila de
    sources por sha256, de modo que la base legacy contiene la última variante
    citada, no el mínimo canónico que elige el manifiesto.
    """
    rows = {}
    for chunk in data['chunks']:
        for citation in chunk['citations']:
            source = {k: citation['source'].get(k) for k in SOURCE_FIELDS}
            rows[source['sha256']] = source
    return rows


def _group_hash(connection, kind):
    """Agregado de un solo grupo: mismo valor que checks() para ese kind.

    La reconciliación lo usa antes y después de actualizar sources; el grupo
    es pequeño y evita repetir la prueba completa del corpus.
    """
    with connection.cursor() as cur:
        cur.execute("""WITH hashed AS MATERIALIZED (
          SELECT asir.rag_hash(payload) AS h FROM asir.rag_rows WHERE kind=%s)
          SELECT count(*) AS count,
          encode(sha256(convert_to(string_agg(h, '' ORDER BY h COLLATE "C"),'UTF8')),'hex') AS sha256
          FROM hashed""", (kind,))
        return dict(cur.fetchone())


def reconcile_legacy_sources(connection, manifest, data):
    """Reconciliar SOLO la fila representativa de sources del corpus legacy.

    El bootstrap exige coincidencia exacta server-side y fallaría si el
    importador antiguo dejó en asir.sources un representante distinto del
    mínimo canónico. Este procedimiento separado y explícito:

    - niega el corpus legacy inseguro: recuenta (sin devolver filas) citas sin
      `source_key` u ocurrencias incompletas, pues actualizar sources cambiaría
      referencias visibles;
    - compara server-side, sin descargar corpus, cada fila de sources y
      ocurrencias con los hashes compactos de la proyección local; niega
      cualquier divergencia desconocida;
    - actualiza únicamente las filas distintas al canónico del manifiesto y
      verifica el resultado completo antes de confirmar.

    El informe solo contiene recuentos y hashes agregados before/after.
    """
    validate_manifest(manifest)
    legacy = legacy_source_rows(data)
    computed = canonical_source_rows(data)
    expected = manifest['rows']['sources']
    if computed != expected or set(legacy) != set(expected):
        raise PublishError('manifest_legacy_sources_mismatch')
    with connection.transaction():
        with connection.cursor() as cur:
            lock(cur)
            cur.execute('SELECT count(*) AS n FROM asir.citations WHERE source_key IS NULL')
            without_key = cur.fetchone()['n']
            if without_key:
                raise ReconcileDenied('legacy_citations_without_source_key', {'citations_without_source_key': without_key})
            cur.execute("""SELECT count(*) AS n FROM asir.citations ci
              LEFT JOIN asir.source_occurrences o ON o.source_key=ci.source_key
              WHERE ci.source_key IS NOT NULL AND o.source_key IS NULL""")
            orphan = cur.fetchone()['n']
            if orphan:
                raise ReconcileDenied('legacy_occurrences_incomplete', {'occurrences_missing': orphan})
            server = {r['physical_id']: r['hash'] for r in cur.execute(
                "SELECT physical_id, asir.rag_hash(payload) AS hash FROM asir.rag_rows WHERE kind='sources'").fetchall()}
            occ_server = {r['physical_id']: r['hash'] for r in cur.execute(
                "SELECT physical_id, asir.rag_hash(payload) AS hash FROM asir.rag_rows WHERE kind='occurrences'").fetchall()}
            before = _group_hash(connection, 'sources')
            diverged = [sha for sha, row in legacy.items() if server.get(sha) != digest(row)]
            unknown = [sha for sha in server if sha not in legacy]
            if diverged or unknown:
                raise ReconcileDenied('legacy_sources_diverged',
                                      {'sources_diverged': len(diverged), 'sources_unknown': len(unknown)})
            occ_expected = manifest['rows']['occurrences']
            occ_diverged = [key for key, row in occ_expected.items() if occ_server.get(key) != digest(row)]
            occ_unknown = [key for key in occ_server if key not in occ_expected]
            if occ_diverged or occ_unknown:
                raise ReconcileDenied('legacy_occurrences_diverged',
                                      {'occurrences_diverged': len(occ_diverged), 'occurrences_unknown': len(occ_unknown)})
            outdated = sorted(sha for sha, row in expected.items() if server[sha] != digest(row))
            for sha in outdated:
                row = expected[sha]
                cur.execute('UPDATE asir.sources SET subject=%s,section=%s,title=%s,filename=%s,url=%s,mime=%s,path=%s WHERE sha256=%s',
                            (row['subject'], row['section'], row['title'], row['filename'], row['url'], row['mime'], row['path'], sha))
                if cur.rowcount != 1:
                    raise PublishError('legacy_source_update_missing')
            # Prueba completa del estado final: nada queda desviado tras el paso.
            verify(connection, manifest)
            after = _group_hash(connection, 'sources')
    return {'status': 'reconciled',
            'counts': {'sources': len(expected), 'sources_updated': len(outdated),
                       'sources_already_canonical': len(expected) - len(outdated),
                       'citations_without_source_key': 0, 'occurrences': len(occ_expected)},
            'before': {'sources': before}, 'after': {'sources': after}}


def operation(connection, operation_id):
    with connection.cursor() as cur:
        cur.execute('SELECT operation_id,base_version,target_version,status,checks FROM asir.rag_operations WHERE operation_id=%s', (operation_id,))
        return cur.fetchone()


def head_version(connection):
    with connection.cursor() as cur:
        cur.execute('SELECT version FROM asir.rag_head WHERE id=1')
        row = cur.fetchone()
        return row['version'] if row else None


def _physical_id(cur, kind, payload):
    cur.execute('SELECT physical_id FROM asir.rag_rows WHERE kind=%s AND asir.rag_hash(payload)=%s', (kind, digest(payload)))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise PublishError('delta_row_missing_or_ambiguous')
    return rows[0]['physical_id']


def _upsert(cur, table, key, payload, *, json_fields=()):
    from psycopg import sql
    columns = list(payload)
    values = [json.dumps(payload[c], ensure_ascii=False) if c in json_fields else payload[c] for c in columns]
    placeholders = [sql.SQL('%s::jsonb') if c in json_fields else sql.SQL('%s') for c in columns]
    updates = [sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c), sql.Identifier(c)) for c in columns if c != key]
    statement = sql.SQL('INSERT INTO asir.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(placeholders),
        sql.Identifier(key), sql.SQL(',').join(updates))
    cur.execute(statement, values)


def _changed(delta, table):
    return delta['actions'][table]['ADD'] + delta['actions'][table]['UPDATE_METADATA']


def _apply(cur, base, target, delta):
    old, new = base['rows'], target['rows']
    # Localizar/quitar referencias y ventanas antes de modificar sus fuentes o config.
    # Las huellas de ventana dependen de las citas: retirarlas primero.
    for key in delta['actions']['vectors']['REMOVE'] + delta['actions']['vectors']['UPDATE_METADATA']:
        pid = int(_physical_id(cur, 'vectors', old['vectors'][key]))
        cur.execute('DELETE FROM asir.embedding_windows WHERE id=%s', (pid,))
    ref_ids = {}
    for key in delta['actions']['references']['REMOVE'] + delta['actions']['references']['UPDATE_METADATA']:
        ref_ids[key] = int(_physical_id(cur, 'references', old['references'][key]))
        cur.execute('DELETE FROM asir.citation_segments WHERE citation_id=%s', (ref_ids[key],))
        cur.execute('DELETE FROM asir.citations WHERE id=%s', (ref_ids[key],))
    for key in _changed(delta, 'sources'):
        _upsert(cur, 'sources', 'sha256', new['sources'][key])
    for key in _changed(delta, 'occurrences'):
        _upsert(cur, 'source_occurrences', 'source_key', new['occurrences'][key])
    cur.execute('SELECT coalesce(max(ordinal),-1) AS n FROM asir.chunks')
    ordinal = cur.fetchone()['n'] + 1
    for key in _changed(delta, 'chunks'):
        row = dict(new['chunks'][key])
        row['titles'], row['sections'] = '\n'.join(row['titles']), '\n'.join(row['sections'])
        if key in old['chunks']:
            cur.execute('SELECT ordinal FROM asir.chunks WHERE id=%s', (key,))
            row['ordinal'] = cur.fetchone()['ordinal']
        else:
            row['ordinal'] = ordinal
            ordinal += 1
        _upsert(cur, 'chunks', 'id', row, json_fields=('warnings',))
    # Todos los chunks existen antes de instalar vecinos/FK.
    # IDs explícitos monotónicos: las secuencias antiguas pueden estar desfasadas.
    cur.execute('SELECT coalesce(max(id),0) AS n FROM asir.citations')
    next_id = max([cur.fetchone()['n'], *ref_ids.values()]) + 1
    for key in _changed(delta, 'references'):
        row = dict(new['references'][key])
        segments = row.pop('segments')
        cid = ref_ids.get(key, next_id)
        if key not in ref_ids:
            next_id += 1
        cur.execute('SELECT coalesce(max(ordinal),-1)+1 AS n FROM asir.citations WHERE chunk_id=%s', (row['chunk_id'],))
        row.update(id=cid, ordinal=cur.fetchone()['n'])
        _upsert(cur, 'citations', 'id', row)
        for n, seg in enumerate(segments):
            cur.execute('INSERT INTO asir.citation_segments(citation_id,ordinal,source_unit_id,locator,start_char,end_char,method,quality) VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s)',
                        (cid, n, seg['source_unit_id'], json.dumps(seg['locator'], ensure_ascii=False), seg['start'], seg['end'], seg['method'], seg['quality']))
    if new['config']['embedding']['context'] == 'subject-section-title':
        # La entrada histórica dependía de la primera cita. Reordenar solo las
        # citas afectadas para preservar el prefijo probado en input_sha256.
        import hashlib
        changed_chunks = {new['references'][k]['chunk_id'] for k in _changed(delta, 'references')}
        for chunk_id in sorted(changed_chunks):
            vector = next(v for v in new['vectors'].values() if v['chunk_id'] == chunk_id)
            text = new['chunks'][chunk_id]['text'][vector['char_start']:vector['char_end']]
            cur.execute('SELECT ci.id,ci.ordinal,s.subject,s.section,s.title FROM asir.citations ci JOIN asir.citation_sources s ON s.citation_id=ci.id WHERE ci.chunk_id=%s ORDER BY ci.ordinal', (chunk_id,))
            refs = cur.fetchall()
            chosen = next((r for r in refs if hashlib.sha256(('passage: '+r['subject']+' | '+r['section'][:120]+' | '+r['title'][:160]+'\n'+text).encode()).hexdigest() == vector['input_sha256']), None)
            if chosen is None:
                raise PublishError('legacy_embedding_prefix_not_found')
            first = refs[0]
            if first['id'] != chosen['id']:
                temporary = max(r['ordinal'] for r in refs)+1
                cur.execute('UPDATE asir.citations SET ordinal=%s WHERE id=%s', (temporary, chosen['id']))
                cur.execute('UPDATE asir.citations SET ordinal=%s WHERE id=%s', (chosen['ordinal'], first['id']))
                cur.execute('UPDATE asir.citations SET ordinal=%s WHERE id=%s', (first['ordinal'], chosen['id']))
    cur.execute('SELECT coalesce(max(id),0)+1 AS n FROM asir.embedding_windows')
    next_window = cur.fetchone()['n']
    for key in _changed(delta, 'vectors'):
        row = new['vectors'][key]
        raw = bytes.fromhex(row['vector_hex'])
        vector = struct.unpack('>' + 'f' * (len(raw)//4), raw)
        literal = '[' + ','.join(repr(x) for x in vector) + ']'
        cur.execute('SELECT coalesce(max(ordinal),-1)+1 AS n FROM asir.embedding_windows WHERE chunk_id=%s', (row['chunk_id'],))
        cur.execute('INSERT INTO asir.embedding_windows(id,chunk_id,ordinal,char_start,char_end,tokens,embedding) VALUES(%s,%s,%s,%s,%s,%s,%s::extensions.vector)',
                    (next_window, row['chunk_id'], cur.fetchone()['n'], row['char_start'], row['char_end'], row['tokens'], literal))
        next_window += 1
    for key in delta['actions']['chunks']['REMOVE']:
        cur.execute('DELETE FROM asir.chunks WHERE id=%s AND NOT EXISTS(SELECT 1 FROM asir.citations WHERE chunk_id=%s)', (key, key))
        if cur.rowcount != 1:
            raise PublishError('chunk_still_referenced')
    for key in delta['actions']['occurrences']['REMOVE']:
        cur.execute('DELETE FROM asir.source_occurrences WHERE source_key=%s', (key,))
    for key in delta['actions']['sources']['REMOVE']:
        cur.execute('DELETE FROM asir.sources WHERE sha256=%s', (key,))
    config = new['config']['embedding']
    # corpus_id sigue siendo compatible con el catálogo: ahora es versión lógica.
    pipeline = new['config']['pipeline']
    cur.execute("UPDATE asir.corpus SET corpus_sha256=%s,schema_version=%s,chunker_version=%s,report=jsonb_set(report,'{config}',%s::jsonb),imported_at=now() WHERE id=1",
                (target['version'], pipeline['schema_version'], pipeline['chunker_version'], json.dumps(pipeline['config'])))
    assignments = ','.join(k+'=%s' for k in config)
    cur.execute('UPDATE asir.embedding_metadata SET '+assignments+',corpus_sha256=%s,imported_at=now() WHERE id=1', [*config.values(), target['version']])


def publish(connection, base, target, operation_id, *, allow_anomaly=False, fault=None):
    """Ejecuta en conexión autocommit, resultado commit resuelto por otra conexión.

    fault(phase) es un hook de pruebas; fases before_commit / after_commit.
    """
    if not connection.autocommit:
        raise PublishError('autocommit_connection_required')
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        raise PublishError('invalid_operation_id')
    delta = plan(base, target)
    if not base:
        raise PublishError('bootstrap_required')
    if delta['blocked'] and not allow_anomaly:
        raise PublishError('anomalous_plan')
    with connection.transaction():
        with connection.cursor() as cur:
            lock(cur)
            existing = operation(connection, operation_id)
            if existing:
                if (existing['base_version'], existing['target_version']) != (base['version'], target['version']):
                    raise PublishError('operation_id_reused')
                if head_version(connection) != target['version']:
                    raise PublishError('operation_superseded')
                verify(connection, target)
                return dict(existing, replay=True)
            cur.execute('SELECT version FROM asir.rag_head WHERE id=1 FOR UPDATE')
            head = cur.fetchone()
            if not head or head['version'] != base['version']:
                raise PublishError('base_compare_and_swap_failed')
            verify(connection, base)
            if delta['unchanged']:
                return {'status': 'unchanged', 'target_version': target['version']}
            _apply(cur, base, target, delta)
            result = verify(connection, target, expected_corpus_id=target['version'])
            cur.execute('UPDATE asir.rag_head SET version=%s,corpus_id=%s,updated_at=now() WHERE id=1 AND version=%s', (target['version'], target['version'], base['version']))
            if cur.rowcount != 1:
                raise PublishError('base_compare_and_swap_failed')
            cur.execute("INSERT INTO asir.rag_operations(operation_id,base_version,target_version,status,checks) VALUES(%s,%s,%s,'committed_pending_probe',%s::jsonb)",
                        (operation_id, base['version'], target['version'], json.dumps(result)))
            if fault:
                fault('before_commit')
    if fault:
        fault('after_commit')
    return {'operation_id': operation_id, 'base_version': base['version'], 'target_version': target['version'], 'status': 'committed_pending_probe', 'checks': result}


def resolve(connect_factory, operation_id, base, target):
    """Conexión NUEVA. Distingue commit confirmado, no aplicado y estado ajeno."""
    with connect_factory() as connection:
        with connection.transaction():
            with connection.cursor() as cur:
                lock(cur)
                record = operation(connection, operation_id)
                head = head_version(connection)
                if record:
                    if record['base_version'] != base['version'] or record['target_version'] != target['version']:
                        raise PublishError('operation_id_reused')
                    if head != target['version']:
                        raise PublishError('operation_superseded')
                    verify(connection, target)
                    return record
                if head != base['version']:
                    raise PublishError('recovery_required')
                verify(connection, base)
                return {'status': 'not_applied', 'operation_id': operation_id}


@contextmanager
def _status_lock(path):
    """Exclusión mutua entre write_status y merge_status del mismo archivo.

    El renombrado atómico evita JSON parcial pero no actualizaciones perdidas
    entre procesos (publicador y respaldo corren fuera del sincronizador).
    El flock es el punto de exclusión común; el archivo de candado persiste.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + '.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_status(path):
    """Lectura instantánea tolerante: ausencia o corrupción devuelven dict vacío."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pending(existing, phase, version):
    """Fases sin resolver acumulan la versión; fases concluyentes la vacían.

    published/reverted/failed terminan el ciclo: nada queda pendiente.
    recovery_required conserva su versión sin resolver en pending_versions.
    """
    previous = existing.get('pending_versions')
    previous = [v for v in previous if isinstance(v, str)] if isinstance(previous, list) else []
    if phase in PENDING_PHASES:
        if version not in previous:
            previous.append(version)
        return previous
    return []


def _backup_values(existing, backup):
    """El panel guarda `backup` como texto: se separan estado, contadores y fecha.

    Sin `backup` explícito se conservan los campos previos: el publicador nunca
    borra lo anotado por el respaldo y viceversa.
    """
    if backup is not None:
        return _backup_fields(backup)
    return {k: existing[k] for k in BACKUP_FIELDS if k in existing}


def _save_status(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return value


def _backup_fields(backup):
    fields = {}
    if not isinstance(backup, dict):
        return fields
    status = backup.get('status')
    if isinstance(status, str):
        fields['backup'] = status
    for key, name in (('bytes', 'backup_bytes'), ('operations', 'backup_operations')):
        value = backup.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            fields[name] = value
    finished = backup.get('finished_at') or backup.get('at')
    if isinstance(finished, str):
        fields['backup_at'] = finished
    return fields


def write_status(path, *, phase, version, operation_id=None, counts=None, backup=None,
                 published_version=None, previous_version=None, pending_versions=None):
    """Escritura atómica del estado compacto bajo lock exclusivo.

    `version` mantiene el significado histórico (versión objetivo o publicada
    según fase). Los campos canónicos `published_version`/`previous_version`
    describen la publicación vigente y se conservan de la lectura anterior si no
    se pasan: prepared/applying/failed nunca borran publicado ni anterior.
    `pending_versions` acumula versiones con operación sin resolver y se deriva
    de la fase salvo que se pase explícitamente. Sin `backup` explícito se
    conservan las anotaciones previas del respaldo; nunca rutas, textos ni
    excepciones sin filtrar.
    """
    with _status_lock(path):
        existing = read_status(path)
        value = {'phase': phase, 'version': version, 'operation': operation_id, 'counts': counts or {}, 'updated_at': datetime.now(timezone.utc).isoformat()}
        if published_version is not None:
            value['published_version'] = published_version
        elif phase in PUBLISHED_PHASES:
            value['published_version'] = version
        elif 'published_version' in existing:
            value['published_version'] = existing['published_version']
        if previous_version is not None:
            value['previous_version'] = previous_version
        elif 'previous_version' in existing:
            value['previous_version'] = existing['previous_version']
        if pending_versions is not None:
            value['pending_versions'] = [v for v in pending_versions if isinstance(v, str)]
        else:
            value['pending_versions'] = _pending(existing, phase, version)
        value.update(_backup_values(existing, backup))
        return _save_status(path, value)


def merge_status(path, **changes):
    """Actualiza campos del estado compacto sin borrar lo que escribió el publicador.

    El respaldo corre fuera del sincronizador: la lectura, mezcla y escritura
    ocurren bajo el mismo lock exclusivo que write_status, de modo que ninguna
    escritura concurrente pierde campos. Con `backup_version` se aplica un CAS:
    solo se anota si la versión vigente del estado coincide; si alguien publicó
    otra versión mientras el respaldo trabajaba, falla cerrado sin arrastrar
    campos obsoletos a la versión nueva. La escritura sigue siendo atómica.
    """
    backup_version = changes.pop('backup_version', None)
    with _status_lock(path):
        existing = read_status(path)
        if backup_version is not None:
            if existing.get('version') != backup_version:
                raise StatusConflict('status_version_changed')
            changes['backup_version'] = backup_version
        value = dict(existing)
        value.update(changes)
        value['updated_at'] = datetime.now(timezone.utc).isoformat()
        return _save_status(path, value)


def update_status(path, transform):
    """Lectura, transformación y escritura atómicas bajo el lock común.

    `transform(recursos_actuales) -> cambios` se ejecuta con el lock tomado,
    así que un llamador que necesita leer-y-mutar (contadores, pines, listas)
    no pierde actualizaciones frente a write_status/merge_status concurrentes.
    """
    with _status_lock(path):
        value = read_status(path)
        changes = transform(value)
        if changes is None:
            changes = {}
        if not isinstance(changes, dict):
            raise StatusConflict('status_update_invalid')
        value.update(changes)
        value['updated_at'] = datetime.now(timezone.utc).isoformat()
        return _save_status(path, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--database-config', type=Path)
    parser.add_argument('--operation-id')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--allow-anomaly', action='store_true', help='Autorización explícita de cambio masivo/modelo.')
    parser.add_argument('--bootstrap', action='store_true', help='Reconciliar base existente, sin importar filas.')
    parser.add_argument('--reconcile-legacy-sources', action='store_true',
                        help='Alinear la fila representativa de sources del corpus legacy con el canónico del manifiesto.')
    parser.add_argument('--legacy-chunks', type=Path, help='chunks.jsonl legacy, misma copia que produjo la base.')
    parser.add_argument('--legacy-report', type=Path)
    parser.add_argument('--legacy-semantic', type=Path)
    parser.add_argument('--legacy-model', type=Path)
    parser.add_argument('--install-schema', action='store_true', help='Requiere configuración administrativa; preparación separada.')
    parser.add_argument('--status', type=Path)
    args = parser.parse_args()
    try:
        base, target = read_manifest(args.base), read_manifest(args.target)
        delta = plan(base, target)
        if not args.execute:
            print(json.dumps({k: v for k, v in delta.items() if k != 'actions'}))
            return 0
        if not args.database_config:
            raise PublishError('database_config_required')
        try:
            from .supabase_db import admin_config, ingest_config, connect
        except ImportError:
            from supabase_db import admin_config, ingest_config, connect
        config = (admin_config if args.install_schema else ingest_config)(args.database_config)
        factory = lambda: connect(config, application_name='asir-rag-delta', autocommit=True)
        if args.install_schema:
            with factory() as conn:
                with conn.transaction():
                    install_schema(conn)
            print(json.dumps({'status': 'schema_prepared'}))
            return 0
        if args.reconcile_legacy_sources and args.bootstrap:
            raise PublishError('mutually_exclusive_flags')
        if args.reconcile_legacy_sources:
            if not (args.legacy_chunks and args.legacy_report and args.legacy_semantic and args.legacy_model):
                raise PublishError('legacy_artifacts_required')
            try:
                from .import_supabase import load_local
            except ImportError:
                from import_supabase import load_local
            data = load_local(args.legacy_chunks, args.legacy_report, args.legacy_semantic, args.legacy_model)
            with factory() as conn:
                report = reconcile_legacy_sources(conn, base, data)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        if args.bootstrap:
            with factory() as conn:
                result = bootstrap(conn, base)
            print(json.dumps({'status': 'reconciled', 'checks': result}))
            return 0
        if not args.operation_id:
            raise PublishError('persisted_operation_id_required')
        if args.status:
            write_status(args.status, phase='applying', version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
        try:
            with factory() as conn:
                result = publish(conn, base, target, args.operation_id, allow_anomaly=args.allow_anomaly)
        except PublishError as error:
            # Fallo determinista: la transacción abortó en el servidor y no hay
            # ack incierto que resolver. No se fabrica 'not_applied' ni se
            # reintenta: fase failed preservando lo publicado.
            if args.status:
                write_status(args.status, phase='failed', version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
            raise PublishError(str(error)) from None
        except Exception:
            # Fallo transitorio o confirmación perdida: resolver en conexión nueva.
            try:
                result = resolve(factory, args.operation_id, base, target)
            except PublishError as error:
                reason = str(error)
                phase = 'recovery_required' if reason == 'recovery_required' else 'failed'
                if args.status:
                    write_status(args.status, phase=phase, version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
                raise PublishError(reason) from None
            except Exception:
                if args.status:
                    write_status(args.status, phase='recovery_required', version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
                raise PublishError('publication_resolution_failed_recovery_required') from None
            if result['status'] == 'not_applied':
                if args.status:
                    write_status(args.status, phase='failed', version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
                raise PublishError('not_applied_safe_to_retry_same_operation') from None
        if result['status'] != 'unchanged':
            try:
                result = resolve(factory, args.operation_id, base, target)
            except Exception:
                # El commit quedó registrado con su operación: la sonda de
                # confirmación fallida no borra ese hecho ni reinventa estado.
                if args.status:
                    write_status(args.status, phase='committed_pending_probe', version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
                raise PublishError('confirmation_probe_failed_committed_pending') from None
        if args.status:
            write_status(args.status, phase=result['status'], version=target['version'], operation_id=args.operation_id, counts=delta['counts'])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (PublishError, VersionError) as error:
        print(json.dumps({'error': str(error), **getattr(error, 'counts', {})}))
        return 1
    except Exception:
        print(json.dumps({'error': 'publication_or_resolution_failed_recovery_required'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
