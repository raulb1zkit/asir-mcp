#!/usr/bin/env python3
"""Coordinación entre el manifiesto versionado del RAG y el respaldo cifrado.

Une el paquete estable del corpus (`rag_bundle`) con el almacenamiento remoto
(`rag_backup`), verifica el prefijo antes de inicializar el catálogo y mantiene
el resultado compacto en `rag-status.json`. No incluye rutas del host, textos ni
secretos, y no activa nada por sí mismo: expone comandos para la coordinación.
"""
import argparse
import contextlib
import json
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path


class CoordinatorError(RuntimeError):
    """Fallo de coordinación con motivo seguro, sin rutas ni secretos."""


DEFAULT_RETENTION_DAYS = 90
DEFAULT_USAGE_CREDENTIALS = Path('/etc/asir-sync/cloudflare-usage.json')
STABLE_PHASES = frozenset({'published', 'reverted', 'failed', 'sync_failed'})


class PrefixNotEmpty(CoordinatorError):
    """El prefijo remoto ya tiene objetos: no se asume propiedad del espacio."""


def _is_stable_status(status):
    phase = status.get('phase') if isinstance(status, dict) else None
    if phase == 'sync_failed':
        pending = status.get('pending_versions', [])
        version = _published_version(status)
        return (isinstance(version, str) and bool(version) and
                isinstance(pending, list) and not pending)
    return phase in {'published', 'reverted', 'failed'}


def _modules():
    try:
        from . import rag_backup, rag_bundle, rag_publish
    except ImportError:
        import rag_backup
        import rag_bundle
        import rag_publish
    return rag_backup, rag_bundle, rag_publish


def _manifest_version(path):
    """Valida la identidad completa del manifiesto y devuelve solo su ID."""
    try:
        data = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise CoordinatorError('manifest_unreadable') from exc
    version = data.get('version') if isinstance(data, dict) else None
    if not isinstance(version, str) or not version:
        raise CoordinatorError('manifest_version_missing')
    backup, _, _ = _modules()
    if not backup.SAFE_VERSION_RE.fullmatch(version) or version in {'.', '..'}:
        raise CoordinatorError('manifest_version_invalid')
    _, bundle, _ = _modules()
    try:
        bundle._versions().validate_manifest(data)
    except Exception as exc:
        raise CoordinatorError('manifest_invalid') from exc
    return version


def bundle_version(manifest_path, bundles_root):
    """Empaqueta una versión una sola vez; reutiliza el paquete si coincide."""
    _, bundle, _ = _modules()
    version = _manifest_version(manifest_path)
    _release_manifest_memory()
    destination = Path(bundles_root).absolute() / version
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise CoordinatorError('bundle_destination_invalid')
        try:
            current_version = _bundle_version_id(bundle, destination)
            _release_manifest_memory()
            header = json.loads((destination / 'bundle.json').read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise CoordinatorError('bundle_unreadable') from exc
        if current_version != version:
            raise CoordinatorError('bundle_conflict')
        files = header.get('files') or []
        return {'version': version, 'path': str(destination), 'reused': True,
                'files': len(files) + 1, 'bytes': sum(entry.get('size', 0) for entry in files)}
    try:
        summary = bundle.create_bundle(manifest_path, destination)
    except (ValueError, OSError) as exc:
        raise CoordinatorError('bundle_failed') from exc
    return {'version': summary['version'], 'path': str(destination), 'reused': False,
            'files': summary['files'], 'bytes': summary['bytes']}


def _release_manifest_memory():
    """Devuelve el heap temporal entre validaciones grandes independientes."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _bundle_version_id(bundle, bundle_path):
    """Validates/reconstructs one bundle, then releases its full manifest."""
    manifest = bundle.read_bundle(bundle_path)
    return manifest.get('version')


def verify_prefix_empty(client, bucket, prefix, *, budget=None, retries=0):
    """Consulta acotada y presupuestada antes de inicializar un catálogo."""
    attempts = 0
    while True:
        if budget is not None:
            budget.reserve("A")
        attempts += 1
        try:
            response = client.list_objects_v2(Bucket=bucket, Prefix=prefix + '/', MaxKeys=1)
            break
        except Exception as exc:
            if attempts > retries:
                raise CoordinatorError('prefix_listing_failed') from exc
    if not isinstance(response, dict):
        raise CoordinatorError('prefix_listing_invalid')
    contents = response.get('Contents') or []
    key_count = response.get('KeyCount')
    if (not isinstance(contents, list) or type(key_count) is not int or
            key_count != len(contents) or type(response.get('IsTruncated')) is not bool):
        raise CoordinatorError('prefix_listing_invalid')
    if contents or response.get('IsTruncated') is True:
        raise PrefixNotEmpty('prefix_not_empty')
    return {'objects': 0, 'prefix': prefix}


def prepare_remote(config_path, state_dir, *, client=None, now=None):
    """Valida frescura y presupuesto antes de listar el prefijo remoto."""
    backup, _, _ = _modules()
    now = int(time.time()) if now is None else int(now)
    state_dir = Path(state_dir).absolute()
    backup._secure_state_dir(state_dir)
    config = backup._load_config(config_path)
    catalog_path = state_dir / 'remote-catalog.json'
    if catalog_path.exists() or catalog_path.is_symlink():
        raise backup.BackupBlocked('el catálogo remoto local ya existe')
    evidence = backup._check_usage_margin(config, now)
    with backup._locked(state_dir / '.backup.lock'):
        budget = backup._Budget(state_dir / 'monthly-budget.json', config, evidence, now)
        budget.ensure_capacity({'A': config['retries'] + 1})
        # Cada intento se reserva duramente antes de una consulta R2.
        client = client or backup._new_s3_client(config)
        verified = verify_prefix_empty(client, config['bucket'], config['prefix'],
                                       budget=budget, retries=config['retries'])
    backup.initialize_remote_catalog(state_dir, config['prefix'])
    return {'status': 'ready', 'prefix': verified['prefix'], 'objects': 0}


def backup_version(manifest_path, config_path, state_dir, bundles_root, *, client=None, now=None):
    """Paquete inmutable + subida cifrada; devuelve estado y contadores seguros."""
    backup, _, _ = _modules()
    bundled = bundle_version(manifest_path, bundles_root)
    result = backup.backup_version(bundled['path'], config_path, state_dir,
                                   s3_client=client, now=now)
    result = dict(result)
    result['version'] = bundled['version']
    result['bundle_reused'] = bundled['reused']
    return result


def backup_current(status_path, version_root, config_path, state_dir, bundles_root, *,
                   client=None, now=None):
    """Respalda la versión publicada según `rag-status.json` y anota el resultado."""
    status_path = Path(status_path)
    status = _read_status(status_path)
    version = _published_version(status)
    phase = status.get('phase')
    if not _is_stable_status(status) or not version:
        raise CoordinatorError('published_version_unknown')
    backup, _, _ = _modules()
    if not backup.SAFE_VERSION_RE.fullmatch(version) or version in {'.', '..'}:
        raise CoordinatorError('published_version_invalid')
    manifest_path = Path(version_root) / version / 'manifest.json'
    if not manifest_path.is_file():
        raise CoordinatorError('manifest_not_found')
    result = backup_version(manifest_path, config_path, state_dir, bundles_root,
                            client=client, now=now)
    if not record_backup(status_path, result, expected_version=version,
                         expected_operation=status.get('operation'), expected_phase=phase):
        try:
            _record_stale_pending(
                status_path, result, expected_version=version,
                expected_operation=status.get('operation'), expected_phase=phase)
        except Exception:
            pass
        try:
            _mark_pending_attempt(state_dir, version)
        except Exception:
            pending_saved = False
        else:
            pending_saved = True
        result = dict(result)
        result['uploaded_version'] = version
        result['status'] = 'pending'
        result['reason'] = ('publication_changed_after_backup' if pending_saved else
                            'publication_changed_pending_pin_failed')
    return result


def restore_version(snapshot_id, config_path, state_dir, destination, manifest_output, *,
                    client=None, now=None):
    """Restaura la versión cifrada y reconstruye despues el manifiesto completo."""
    backup, bundle, _ = _modules()
    result = backup.restore_remote(snapshot_id, config_path, state_dir, destination,
                                   s3_client=client, now=now)
    result = dict(result)
    if result.get('status') != 'complete':
        return result
    try:
        restored = bundle.restore_bundle(destination, manifest_output)
    except (ValueError, OSError) as exc:
        raise CoordinatorError('manifest_restore_failed') from exc
    result['manifest_version'] = restored['version']
    return result


def protect_versions(state_dir, *, current, previous=None, pending=None):
    """Fija los punteros del núcleo en almacenamiento local y catálogo remoto."""
    backup, _, _ = _modules()
    backup.protect_remote_versions(state_dir, current=current, previous=previous,
                                   pending=list(pending or []))
    store = backup.RAGStore(Path(state_dir) / 'local')
    try:
        store.protect(current=current, previous=previous, pending=list(pending or []))
    except backup.StorageError as exc:
        raise CoordinatorError('local_protection_failed') from exc
    return {'current': current, 'previous': previous, 'pending': list(pending or [])}


def prune_versions(version_root, *, keep=(), retention_days=DEFAULT_RETENTION_DAYS, now=None):
    """Legacy guard: version trees require archive verification before removal."""
    backup, _, _ = _modules()
    root = Path(version_root)
    if root.is_symlink() or not root.is_dir():
        raise CoordinatorError('version_root_invalid')
    timestamp = int(time.time()) if now is None else int(now)
    cutoff = timestamp - retention_days * 86400
    protected = {value for value in keep if value}
    for entry in sorted(root.iterdir()):
        if entry.name in protected or entry.is_symlink() or not entry.is_dir():
            continue
        if not backup.SAFE_VERSION_RE.fullmatch(entry.name):
            continue
        if not (entry / 'manifest.json').is_file():
            continue
        if entry.stat().st_mtime >= cutoff:
            continue
        if any(path.is_symlink() for path in entry.rglob('*')):
            raise CoordinatorError('version_tree_insecure')
        raise CoordinatorError('archive_verification_required')
    return []


def prune_managed_archives(state_dir, bundles_root, *, version_root=None, keep=(),
                           retention_days=DEFAULT_RETENTION_DAYS, now=None):
    """Prune aged canonical bundles only after a compressed local archive verifies.

    This intentionally does not walk or remove sync run directories, raw
    educational sources, or arbitrary files. The only removable bundle is a
    version-id directory paired with a matching version manifest (when supplied).
    """
    backup, _, _ = _modules()
    root = Path(bundles_root)
    if (root.is_symlink() or not root.is_dir() or
            root.absolute() != root.resolve(strict=True)):
        raise CoordinatorError('bundles_root_invalid')
    version_path = Path(version_root) if version_root is not None else None
    if version_path is not None and (
            version_path.is_symlink() or not version_path.is_dir() or
            version_path.absolute() != version_path.resolve(strict=True)):
        raise CoordinatorError('version_root_invalid')
    timestamp = int(time.time()) if now is None else int(now)
    cutoff = timestamp - retention_days * 86400
    protected = {value for value in keep if value}
    store = backup.RAGStore(Path(state_dir) / 'local')
    candidates = []
    for entry in sorted(root.iterdir()):
        if (entry.name in protected or entry.is_symlink() or not entry.is_dir() or
                not backup.SAFE_VERSION_RE.fullmatch(entry.name) or
                entry.name in {'.', '..'} or entry.stat().st_mtime >= cutoff):
            continue
        version_dir = version_path / entry.name if version_path is not None else None
        if version_dir is not None:
            if (version_dir.is_symlink() or not version_dir.is_dir() or
                    version_dir.stat().st_mtime >= cutoff):
                continue
            version_files = list(version_dir.rglob('*'))
            if any(path.is_symlink() for path in version_files):
                raise CoordinatorError('version_tree_insecure')
            if any(path.is_file() and path.stat().st_mtime >= cutoff
                   for path in version_files):
                continue
            # Version directories may include derived artifacts. Never remove
            # these as a side effect; only a manifest-only directory is eligible.
            children = list(version_dir.iterdir())
            if (len(children) != 1 or children[0].name != 'manifest.json' or
                    children[0].is_symlink() or not children[0].is_file()):
                version_dir = None
        try:
            inventory = backup.inventory_version(entry)
            if any((entry / item['path']).stat().st_mtime >= cutoff
                   for item in inventory['files']):
                continue
            header_path = entry / 'bundle.json'
            if header_path.is_symlink() or not header_path.is_file():
                raise CoordinatorError('bundle_header_invalid')
            header = json.loads(header_path.read_text(encoding='utf-8'))
            if (not isinstance(header, dict) or
                    not isinstance(header.get('manifest_head'), dict) or
                    header['manifest_head'].get('version') != entry.name):
                raise CoordinatorError('bundle_identity_invalid')
            records = header.get('files')
            if not isinstance(records, list):
                raise CoordinatorError('bundle_inventory_invalid')
            declared = {}
            for record in records:
                if not isinstance(record, dict):
                    raise CoordinatorError('bundle_inventory_invalid')
                relative, digest, size = record.get('path'), record.get('sha256'), record.get('size')
                if (not isinstance(relative, str) or
                        not re.fullmatch(r'rows/[a-z]+/[0-9a-f]{2}\.json', relative) or
                        not re.fullmatch(r'[a-f0-9]{64}', str(digest)) or
                        type(size) is not int or size < 0 or relative in declared):
                    raise CoordinatorError('bundle_inventory_invalid')
                declared[relative] = (digest, size)
            actual = {item['path']: (item['sha256'], item['size'])
                      for item in inventory['files'] if item['path'] != 'bundle.json'}
            if actual != declared or len(inventory['files']) != len(declared) + 1:
                raise CoordinatorError('bundle_hash_mismatch')
            # The bundle header carries the manifest identity. Avoid loading the
            # potentially large full manifest into memory during retention.
            if version_path is not None:
                manifest_path = version_path / entry.name / 'manifest.json'
                if (manifest_path.is_symlink() or not manifest_path.is_file()):
                    raise CoordinatorError('version_manifest_invalid')
            store.verify_snapshot(entry.name, entry, refresh_retention=True, now=timestamp)
        except CoordinatorError:
            raise
        except Exception as exc:
            raise CoordinatorError('archive_verification_failed') from exc
        candidates.append((entry, version_dir))

    if not candidates:
        return {'removed_bundles': [], 'removed_versions': []}

    # Convert eligible historical CAS objects first, and then prove the gzip/raw
    # archive restores to the exact bundle before unlinking any source directory.
    compacted = store.compact_historical(now=timestamp)
    if compacted.get('space_blocked'):
        raise CoordinatorError('archive_compaction_blocked')
    try:
        for entry, _version_dir in candidates:
            store.verify_snapshot(entry.name, entry, now=timestamp)
    except Exception as exc:
        raise CoordinatorError('archive_verification_failed') from exc

    removed_bundles = []
    removed_versions = []
    for entry, version_dir in candidates:
        # rmtree does not follow child symlinks, but reject them before mutation
        # so the whole retention pass fails closed rather than partially pruning.
        if any(path.is_symlink() for path in entry.rglob('*')):
            raise CoordinatorError('bundle_tree_insecure')
        if version_dir is not None and any(path.is_symlink() for path in version_dir.rglob('*')):
            raise CoordinatorError('version_tree_insecure')
    for entry, version_dir in candidates:
        shutil.rmtree(entry)
        removed_bundles.append(entry.name)
        if version_dir is not None:
            shutil.rmtree(version_dir)
            removed_versions.append(entry.name)
    return {'removed_bundles': removed_bundles, 'removed_versions': removed_versions}


def retain_local(state_dir, *, current, previous=None, pending=None,
                 retention_days=DEFAULT_RETENTION_DAYS, now=None,
                 version_root=None, bundles_root=None,
                 preserve_existing_previous=True):
    """Pins del núcleo, poda y compresión local; los protegidos nunca se tocan.

    No empieza ninguna operación destructiva hasta que se escriben y validan
    los pins locales y remotos.
    """
    backup, _, _ = _modules()
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 0:
        raise CoordinatorError('retention_days_invalid')
    if not isinstance(current, str) or not current:
        raise CoordinatorError('published_version_unknown')
    if not isinstance(pending, (list, tuple, set, type(None))):
        raise CoordinatorError('pending_versions_invalid')
    existing = _existing_pins(state_dir)
    pending_ids = set(pending or [])
    pending_ids.update(existing['pending'])
    if preserve_existing_previous and previous is None:
        pending_ids.update(value for value in existing['previous'] if value != current)
    if any(not isinstance(value, str) or not backup.SAFE_VERSION_RE.fullmatch(value)
           or value in {'.', '..'} for value in [current, *([previous] if previous else []),
                                                  *pending_ids]):
        raise CoordinatorError('version_pin_invalid')
    store = backup.RAGStore(Path(state_dir) / 'local')
    try:
        store.protect(current=current, previous=previous, pending=sorted(pending_ids))
        backup.protect_remote_versions(state_dir, current=current, previous=previous,
                                       pending=sorted(pending_ids))
    except (backup.StorageError, backup.BackupBlocked, backup.UnsafePathError, OSError) as exc:
        raise CoordinatorError('version_protection_failed') from exc
    timestamp = int(time.time()) if now is None else int(now)
    archived = {'removed_bundles': [], 'removed_versions': []}
    if bundles_root is not None:
        try:
            archived = prune_managed_archives(
                state_dir, bundles_root, version_root=version_root,
                keep=[value for value in (current, previous, *pending_ids) if value],
                retention_days=retention_days, now=timestamp)
        except (CoordinatorError, backup.StorageError, backup.UnsafePathError,
                backup.InsufficientSpaceError, OSError) as exc:
            raise CoordinatorError('managed_archive_retention_failed') from exc
    removed = store.prune(now=timestamp, retention_seconds=retention_days * 86400)
    compressed = store.compact_historical(now=timestamp)
    result = {'pinned': True, 'retention_days': retention_days,
              'removed_snapshots': removed['removed_snapshots'],
              'removed_objects': removed['removed_objects'],
              'compressed_objects': compressed.get('compressed_objects', 0),
              'removed_versions': archived['removed_versions'],
              'removed_bundles': archived['removed_bundles']}
    return result


def _read_status(path):
    path = Path(path)
    if path.is_symlink():
        return {}
    try:
        _, _, publish = _modules()
        reader = getattr(publish, 'read_status', None)
        if reader is not None:
            value = reader(path)
            return value if isinstance(value, dict) else {}
    except (ImportError, OSError, ValueError):
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _published_version(status):
    if not isinstance(status, dict):
        return None
    value = status.get('published_version')
    if value is None and status.get('phase') == 'published':
        value = status.get('version')
    return value if isinstance(value, str) and value else None


@contextlib.contextmanager
def _status_lock(path, publish):
    """Usa el lock compartido del publicador cuando está disponible.

    El fallback conserva las pruebas y compatibilidad con revisiones antiguas.
    En integración, `_status_lock` de rag_publish serializa también sus writers.
    """
    helper = getattr(publish, '_status_lock', None)
    if helper is not None:
        with helper(Path(path)):
            yield
        return
    backup, _, _ = _modules()
    lock_path = Path(path).with_name(Path(path).name + '.lock')
    with backup._locked(lock_path):
        yield


def _status_matches(status, version, operation=None, expected_phase='published'):
    return (status.get('phase') == expected_phase and _is_stable_status(status) and
            _published_version(status) == version and
            (operation is None or status.get('operation') == operation))


def _existing_pins(state_dir):
    """Lee los pins locales/remotos sin aceptar corrupción como ausencia."""
    state_dir = Path(state_dir)
    local_root = state_dir / 'local'
    if state_dir.is_symlink() or local_root.is_symlink():
        raise CoordinatorError('protection_state_invalid')
    files = (state_dir / 'local' / 'protected.json', state_dir / 'remote-protected.json')
    result = {'current': set(), 'previous': set(), 'pending': set()}
    for path in files:
        if path.is_symlink():
            raise CoordinatorError('protection_state_invalid')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise CoordinatorError('protection_state_invalid') from exc
        if not isinstance(value, dict) or not isinstance(value.get('pending', []), list):
            raise CoordinatorError('protection_state_invalid')
        for field in ('current', 'previous'):
            item = value.get(field)
            if item is not None:
                if (not isinstance(item, str) or not
                        re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', item) or
                        item in {'.', '..'}):
                    raise CoordinatorError('protection_state_invalid')
                result[field].add(item)
        for item in value.get('pending', []):
            if (not isinstance(item, str) or not
                    re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', item) or
                    item in {'.', '..'}):
                raise CoordinatorError('protection_state_invalid')
            result['pending'].add(item)
    return result


def _mark_pending_attempt(state_dir, version):
    """Conserva el snapshot si el publicador cambió durante el backup."""
    backup, _, _ = _modules()
    store = backup.RAGStore(Path(state_dir) / 'local')
    store.mark_pending(version)
    remote_path = Path(state_dir) / 'remote-protected.json'
    if remote_path.is_symlink():
        raise CoordinatorError('protection_state_invalid')
    try:
        remote = json.loads(remote_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        remote = {}
    except (OSError, ValueError) as exc:
        raise CoordinatorError('protection_state_invalid') from exc
    if not isinstance(remote, dict) or not isinstance(remote.get('pending', []), list):
        raise CoordinatorError('protection_state_invalid')
    pending = set(remote.get('pending', []))
    pending.add(version)
    backup.protect_remote_versions(
        state_dir, current=remote.get('current'), previous=remote.get('previous'),
        pending=sorted(pending))


def _status_pins(status, state_dir):
    """Resuelve pins de un head estable; incompletos preservan pins anteriores."""
    if not _is_stable_status(status):
        raise CoordinatorError('publication_phase_not_stable')
    current = _published_version(status)
    if current is None:
        raise CoordinatorError('published_version_unknown')
    backup, _, _ = _modules()
    if not backup.SAFE_VERSION_RE.fullmatch(current) or current in {'.', '..'}:
        raise CoordinatorError('version_pin_invalid')
    existing = _existing_pins(state_dir)
    pending = set(existing['pending'])
    if 'previous_version' in status:
        previous = status['previous_version']
        if previous is not None and (
                not isinstance(previous, str) or not backup.SAFE_VERSION_RE.fullmatch(previous)
                or previous in {'.', '..'}):
            raise CoordinatorError('version_pin_invalid')
    else:
        previous_candidates = sorted(value for value in existing['previous'] if value != current)
        previous = previous_candidates[0] if previous_candidates else None
        pending.update(value for value in existing['current'] | existing['previous']
                       if value not in {current, previous})
    pending_state = status.get('pending_versions', [])
    if 'pending_versions' in status and (
            not isinstance(pending_state, list) or any(
                not isinstance(value, str) or not backup.SAFE_VERSION_RE.fullmatch(value)
                or value in {'.', '..'} for value in pending_state)):
        raise CoordinatorError('pending_versions_invalid')
    pending.update(pending_state)
    pending.discard(current)
    if previous:
        pending.discard(previous)
    return {'current': current, 'previous': previous, 'pending': sorted(pending)}


def _write_status_if_current(status_path, result, *, expected_version,
                             expected_operation=None, expected_phase='published'):
    """CAS-annota solo el mismo head/fase leído antes de iniciar el backup."""
    _, _, publish = _modules()
    summary = {'status': result.get('status'), 'finished_at': result.get('finished_at') or result.get('at')}
    for key in ('bytes', 'operations'):
        if isinstance(result.get(key), int) and not isinstance(result.get(key), bool):
            summary[key] = result[key]
    fields = publish._backup_fields(summary)
    version = result.get('version')
    if isinstance(version, str):
        fields['backup_version'] = version
    update = getattr(publish, 'update_status', None)
    conflict = getattr(publish, 'StatusConflict', RuntimeError)
    if update is not None:
        def transform(status):
            if not _status_matches(status, expected_version, expected_operation, expected_phase):
                raise conflict('status_version_changed')
            return fields
        try:
            update(status_path, transform)
        except conflict:
            return False
        return True
    with _status_lock(status_path, publish):
        status = _read_status(status_path)
        if not _status_matches(status, expected_version, expected_operation, expected_phase):
            return False
        status.update(fields)
        status['updated_at'] = datetime.now(UTC).isoformat()
        publish._save_status(status_path, status)
    return True


def _record_stale_pending(status_path, result, *, expected_version,
                          expected_operation=None, expected_phase='published'):
    """Marca pendiente sin alterar la fase/head nuevos cuando cambió la publicación."""
    _, _, publish = _modules()
    summary = {'status': 'pending', 'finished_at': result.get('finished_at') or result.get('at')}
    for key in ('bytes', 'operations'):
        if isinstance(result.get(key), int) and not isinstance(result.get(key), bool):
            summary[key] = result[key]
    fields = publish._backup_fields(summary)
    fields['backup_version'] = expected_version

    def transform(status):
        if _status_matches(status, expected_version, expected_operation, expected_phase):
            return {}
        if (status.get('backup') == 'complete' and
                status.get('backup_version') not in (None, expected_version)):
            return {}
        return fields

    update = getattr(publish, 'update_status', None)
    if update is not None:
        try:
            update(status_path, transform)
        except Exception:
            return False
        return True
    with _status_lock(status_path, publish):
        status = _read_status(status_path)
        changes = transform(status)
        if changes:
            status.update(changes)
            status['updated_at'] = datetime.now(UTC).isoformat()
            publish._save_status(status_path, status)
        return bool(changes)


def record_backup(status_path, result, *, expected_version=None, expected_operation=None,
                  expected_phase='published'):
    """Anota el resultado sin pisar la versión publicada ni perder estado."""
    if expected_version is None:
        expected_version = result.get('version')
    if not isinstance(expected_version, str) or not expected_version:
        return False
    return _write_status_if_current(status_path, result, expected_version=expected_version,
                                     expected_operation=expected_operation,
                                     expected_phase=expected_phase)


def _refresh_usage(config_path, credentials_path, *, now):
    """Renueva evidencia account-wide; nunca usa una copia antigua al fallar."""
    backup, _, _ = _modules()
    try:
        from .r2_account_usage import refresh_account_usage
    except ImportError:
        from r2_account_usage import refresh_account_usage
    config = backup._load_config(config_path)
    output_path = config.get('account_usage_file')
    if not isinstance(output_path, str) or not output_path:
        raise CoordinatorError('account_usage_path_missing')
    return refresh_account_usage(credentials_path, output_path,
                                 now=datetime.fromtimestamp(now, UTC))


def _run_cycle_impl(status_path, version_root, config_path, state_dir, bundles_root, *,
                    usage_credentials=DEFAULT_USAGE_CREDENTIALS, client=None, now=None):
    """Renueva uso, respalda el head y solo entonces retiene copias confirmadas."""
    backup, _, _ = _modules()
    fixed_time = now is not None
    cycle_now = int(time.time()) if now is None else int(now)
    status_path = Path(status_path)
    initial_status = _read_status(status_path)
    try:
        pins = _status_pins(initial_status, state_dir)
    except CoordinatorError as exc:
        version = _published_version(initial_status)
        failed = {'status': 'blocked', 'reason': str(exc), 'version': version,
                  'bytes': 0, 'operations': 0,
                  'at': datetime.fromtimestamp(cycle_now, UTC).isoformat()}
        if (isinstance(version, str) and version and
                _is_stable_status(initial_status)):
            if not record_backup(status_path, failed, expected_version=version,
                                 expected_operation=initial_status.get('operation'),
                                 expected_phase=initial_status.get('phase')):
                return {'status': 'pending', 'reason': 'publication_changed_before_backup',
                        'backup': 'pending', 'cleanup': 'skipped'}
        return {'status': 'blocked', 'backup': failed, 'cleanup': 'skipped'}
    operation = initial_status.get('operation')
    expected_phase = initial_status.get('phase')
    try:
        refresh_now = cycle_now if fixed_time else int(time.time())
        _refresh_usage(config_path, usage_credentials, now=refresh_now)
    except Exception:  # el helper sanitiza proveedores; no se usa evidencia previa
        blocked = {'status': 'blocked', 'reason': 'account_usage_refresh_failed',
                   'version': pins['current'], 'bytes': 0, 'operations': 0,
                   'at': datetime.fromtimestamp(refresh_now, UTC).isoformat()}
        if not record_backup(status_path, blocked, expected_version=pins['current'],
                             expected_operation=operation, expected_phase=expected_phase):
            return {'status': 'pending', 'reason': 'publication_changed_before_backup',
                    'backup': 'pending', 'cleanup': 'skipped'}
        return {'status': 'blocked', 'backup': blocked, 'cleanup': 'skipped'}

    try:
        result = backup_current(status_path, version_root, config_path, state_dir,
                                bundles_root, client=client,
                                now=cycle_now if fixed_time else None)
    except CoordinatorError as exc:
        if not _status_matches(_read_status(status_path), pins['current'], operation,
                               expected_phase):
            return {'status': 'pending', 'reason': 'publication_changed_before_backup',
                    'backup': 'pending', 'cleanup': 'skipped'}
        result = {'status': 'failed', 'reason': str(exc), 'version': pins['current'],
                  'bytes': 0, 'operations': 0,
                  'at': datetime.fromtimestamp(cycle_now, UTC).isoformat()}
        if not record_backup(status_path, result, expected_version=pins['current'],
                             expected_operation=operation, expected_phase=expected_phase):
            return {'status': 'pending', 'reason': 'publication_changed_before_backup',
                    'backup': 'pending', 'cleanup': 'skipped'}
        return {'status': 'failed', 'backup': result, 'cleanup': 'skipped'}
    if result.get('status') != 'complete':
        return {'status': result.get('status', 'failed'), 'backup': result,
                'cleanup': 'skipped'}

    # Mantener el lock compartido durante la comprobación CAS y las dos podas:
    # el publicador no puede cambiar la fase/head a mitad de la retención.
    _, _, publish = _modules()
    retention_failure = None
    local = remote = None
    with _status_lock(status_path, publish):
        current_status = _read_status(status_path)
        if not _status_matches(current_status, pins['current'], operation, expected_phase):
            return {'status': 'pending', 'reason': 'publication_changed_before_retention',
                    'backup': result, 'cleanup': 'skipped'}
        try:
            pins = _status_pins(current_status, state_dir)
        except CoordinatorError:
            retention_failure = ('blocked', 'version_pins_invalid')
        try:
            if retention_failure is None:
                config = backup._load_config(config_path)
                cleanup_now = cycle_now if fixed_time else int(time.time())
                backup._check_usage_margin(config, cleanup_now)
        except Exception:
            retention_failure = ('blocked', 'account_usage_stale_before_retention')
        if retention_failure is None:
            try:
                local = retain_local(state_dir, current=pins['current'], previous=pins['previous'],
                                     pending=pins['pending'], retention_days=DEFAULT_RETENTION_DAYS,
                                     now=cleanup_now, version_root=version_root,
                                     bundles_root=bundles_root,
                                     preserve_existing_previous=False)
            except Exception:
                retention_failure = ('failed', 'local_retention_failed')
        if retention_failure is None:
            try:
                remote = backup.prune_remote(
                    config_path, state_dir, s3_client=client, now=cleanup_now,
                    retention_seconds=DEFAULT_RETENTION_DAYS * 86400)
            except Exception:
                retention_failure = ('failed', 'remote_retention_failed')
            else:
                if remote.get('status') != 'complete':
                    retention_failure = (remote.get('status', 'failed'),
                                         'remote_retention_incomplete')

    if retention_failure is not None:
        status, reason = retention_failure
        failed = {'status': status, 'reason': reason, 'version': pins['current'],
                  'bytes': result.get('bytes', 0), 'operations': result.get('operations', 0),
                  'at': datetime.fromtimestamp(cycle_now, UTC).isoformat()}
        if not record_backup(status_path, failed, expected_version=pins['current'],
                             expected_operation=operation, expected_phase=expected_phase):
            return {'status': 'pending', 'reason': 'publication_changed_after_backup',
                    'backup': 'pending', 'cleanup': 'skipped'}
        return {'status': status, 'backup': failed, 'cleanup': 'skipped'}
    return {'status': 'complete', 'backup': result, 'local_retention': local,
            'remote_retention': remote}


def run_cycle(status_path, version_root, config_path, state_dir, bundles_root, *,
              usage_credentials=DEFAULT_USAGE_CREDENTIALS, client=None, now=None):
    """Fail with a compact backup status, never with an exception or secret."""
    try:
        return _run_cycle_impl(status_path, version_root, config_path, state_dir, bundles_root,
                               usage_credentials=usage_credentials, client=client, now=now)
    except Exception:
        timestamp = int(time.time()) if now is None else int(now)
        status = _read_status(status_path)
        version = _published_version(status)
        phase = status.get('phase')
        failed = {'status': 'failed', 'reason': 'run_cycle_failed', 'version': version,
                  'bytes': 0, 'operations': 0,
                  'at': datetime.fromtimestamp(timestamp, UTC).isoformat()}
        backup, _, _ = _modules()
        if (isinstance(version, str) and backup.SAFE_VERSION_RE.fullmatch(version) and
                _is_stable_status(status)):
            try:
                record_backup(status_path, failed, expected_version=version,
                              expected_operation=status.get('operation'),
                              expected_phase=phase)
            except Exception:
                pass
        return {'status': 'failed', 'backup': failed, 'cleanup': 'skipped'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Verifica prefijo vacío e inicializa catálogo.')
    prepare.add_argument('--config', required=True, type=Path)
    prepare.add_argument('--state-dir', required=True, type=Path)
    pack = commands.add_parser('backup', help='Respalda un manifiesto concreto.')
    pack.add_argument('--manifest', required=True, type=Path)
    pack.add_argument('--bundles', required=True, type=Path)
    current = commands.add_parser('backup-current', help='Respalda la versión publicada.')
    current.add_argument('--status', required=True, type=Path)
    current.add_argument('--version-root', required=True, type=Path)
    current.add_argument('--bundles', required=True, type=Path)
    cycle = commands.add_parser('run-cycle', help='Refresca uso, respalda y retiene el head publicado.')
    cycle.add_argument('--status', required=True, type=Path)
    cycle.add_argument('--version-root', required=True, type=Path)
    cycle.add_argument('--bundles', required=True, type=Path)
    cycle.add_argument('--usage-credentials', type=Path, default=DEFAULT_USAGE_CREDENTIALS)
    restore = commands.add_parser('restore', help='Restaura y reconstruye el manifiesto.')
    restore.add_argument('snapshot', help='Identificador de versión respaldada.')
    restore.add_argument('--destination', required=True, type=Path)
    restore.add_argument('--manifest-output', required=True, type=Path)
    protect = commands.add_parser('protect', help='Fija actual/anterior/pendientes.')
    protect.add_argument('--current', required=True)
    protect.add_argument('--previous')
    protect.add_argument('--pending', action='append', default=[])
    protect.add_argument('--state-dir', required=True, type=Path)
    retain = commands.add_parser('retain', help='Poda y comprime copias locales según retención.')
    retain.add_argument('--current')
    retain.add_argument('--status', type=Path, help='Toma la versión actual de rag-status.json.')
    retain.add_argument('--previous')
    retain.add_argument('--pending', action='append', default=[])
    retain.add_argument('--retention-days', type=int, default=DEFAULT_RETENTION_DAYS)
    retain.add_argument('--version-root', type=Path,
                        help='Elimina directorios de versión fuera de retención (opt-in).')
    retain.add_argument('--bundles', type=Path,
                        help='Elimina paquetes fuera de retención (opt-in).')
    retain.add_argument('--state-dir', required=True, type=Path)
    for parent in (pack, current, restore, cycle):
        parent.add_argument('--config', required=True, type=Path)
        parent.add_argument('--state-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare_remote(args.config, args.state_dir)
        elif args.command == 'backup':
            result = backup_version(args.manifest, args.config, args.state_dir, args.bundles)
        elif args.command == 'backup-current':
            result = backup_current(args.status, args.version_root, args.config,
                                    args.state_dir, args.bundles)
        elif args.command == 'run-cycle':
            result = run_cycle(args.status, args.version_root, args.config, args.state_dir,
                               args.bundles, usage_credentials=args.usage_credentials)
        elif args.command == 'restore':
            result = restore_version(args.snapshot, args.config, args.state_dir,
                                     args.destination, args.manifest_output)
        elif args.command == 'retain':
            if args.status:
                pins = _status_pins(_read_status(args.status), args.state_dir)
                current, previous, pending = pins['current'], pins['previous'], pins['pending']
                preserve_existing_previous = False
            else:
                current, previous, pending = args.current, args.previous, args.pending
                preserve_existing_previous = args.previous is None
            if not isinstance(current, str) or not current:
                raise CoordinatorError('published_version_unknown')
            result = retain_local(args.state_dir, current=current,
                                  previous=previous, pending=pending,
                                  retention_days=args.retention_days,
                                  version_root=args.version_root, bundles_root=args.bundles,
                                  preserve_existing_previous=preserve_existing_previous)
        else:
            result = protect_versions(args.state_dir, current=args.current,
                                      previous=args.previous, pending=args.pending)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get('status') in (None, 'complete', 'ready') else 1
    except CoordinatorError as error:
        print(json.dumps({'status': 'blocked', 'error': str(error)}, sort_keys=True))
        return 1
    except Exception as error:  # motivo acotado: nunca se imprimen valores de configuración
        print(json.dumps({'status': 'failed', 'error': type(error).__name__}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
