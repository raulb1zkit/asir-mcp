#!/usr/bin/env python3
"""Paquetes deduplicables del corpus canónico, sin rutas privadas ni índices monolíticos."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

SHARDS = 64
FORMAT = 1
MAX_FILE = 256 * 1024 * 1024


class BundleError(ValueError):
    pass


def _versions():
    try:
        from . import rag_versions
    except ImportError:
        import rag_versions
    return rag_versions


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise BundleError('bundle_file_invalid')
    return path.read_bytes()


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_bundle(manifest_path, destination):
    """64 particiones estables por tabla: una edición no invalida todo el corpus."""
    versioning = _versions()
    manifest = json.loads(_read(Path(manifest_path)))
    versioning.validate_manifest(manifest)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BundleError('bundle_destination_exists')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix='.bundle-', dir=destination.parent))
    try:
        records = []
        for table in versioning.TABLES:
            partitions = {}
            for key, row in manifest['rows'][table].items():
                shard = hashlib.sha256(key.encode()).digest()[0] % SHARDS
                partitions.setdefault(shard, {})[key] = row
            for shard, values in sorted(partitions.items()):
                relative = f'rows/{table}/{shard:02x}.json'
                content = versioning.canonical(values)
                if len(content) > MAX_FILE:
                    raise BundleError('bundle_shard_too_large')
                _write(staging / relative, content)
                records.append({'path': relative, 'sha256': hashlib.sha256(content).hexdigest(),
                                'size': len(content), 'count': len(values)})
        header = {'bundle_format': FORMAT, 'shards': SHARDS,
                  'manifest_head': {k: v for k, v in manifest.items() if k != 'rows'},
                  'files': records}
        _write(staging / 'bundle.json', versioning.canonical(header))
        # Publicar solo después de reconstruir y verificar todas las piezas.
        recovered = read_bundle(staging)
        if recovered != manifest:
            raise BundleError('bundle_roundtrip_failed')
        for directory in sorted((p for p in staging.rglob('*') if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            _sync(directory)
        _sync(staging)
        os.replace(staging, destination)
        _sync(destination.parent)
        return {'version': manifest['version'], 'files': len(records) + 1,
                'bytes': sum(r['size'] for r in records), 'format': FORMAT}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def read_bundle(bundle_dir):
    versioning = _versions()
    root = Path(bundle_dir)
    if root.is_symlink() or not root.is_dir():
        raise BundleError('bundle_directory_invalid')
    header = json.loads(_read(root / 'bundle.json'))
    if header.get('bundle_format') != FORMAT or header.get('shards') != SHARDS:
        raise BundleError('bundle_format_invalid')
    files = header.get('files')
    if not isinstance(files, list) or len(files) > len(versioning.TABLES) * SHARDS:
        raise BundleError('bundle_inventory_invalid')
    rows = {table: {} for table in versioning.TABLES}
    expected = {'bundle.json'}
    for entry in files:
        match = re.fullmatch(r'rows/([a-z]+)/([0-9a-f]{2})\.json', entry.get('path', ''))
        if not match or match[1] not in rows or int(match[2], 16) >= SHARDS:
            raise BundleError('bundle_path_invalid')
        relative, table = entry['path'], match[1]
        if relative in expected:
            raise BundleError('bundle_duplicate_shard')
        expected.add(relative)
        path = root / relative
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise BundleError('bundle_symlink')
        content = _read(path)
        if len(content) != entry.get('size') or hashlib.sha256(content).hexdigest() != entry.get('sha256'):
            raise BundleError('bundle_hash_mismatch')
        part = json.loads(content)
        if not isinstance(part, dict) or len(part) != entry.get('count'):
            raise BundleError('bundle_shard_invalid')
        for key in part:
            if hashlib.sha256(key.encode()).digest()[0] % SHARDS != int(match[2], 16):
                raise BundleError('bundle_partition_invalid')
        if rows[table].keys() & part.keys():
            raise BundleError('bundle_duplicate_record')
        rows[table].update(part)
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise BundleError('bundle_symlink')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise BundleError('bundle_unexpected_files')
    manifest = dict(header['manifest_head'], rows=rows)
    versioning.validate_manifest(manifest)
    return manifest


def restore_bundle(bundle_dir, manifest_output):
    manifest = read_bundle(bundle_dir)
    output = Path(manifest_output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Salida exclusiva: no sobrescribir una versión que el operador conserva.
    _write(output, _versions().canonical(manifest))
    _sync(output.parent)
    return {'version': manifest['version'], 'restored': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pack = commands.add_parser('pack')
    pack.add_argument('--manifest', required=True, type=Path)
    pack.add_argument('--output', required=True, type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('--bundle', required=True, type=Path)
    restore.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = create_bundle(args.manifest, args.output) if args.command == 'pack' else restore_bundle(args.bundle, args.output)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'error': 'bundle_validation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
