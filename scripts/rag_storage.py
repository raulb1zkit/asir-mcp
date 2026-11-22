"""Almacén local CAS para versiones completas e inmutables del corpus RAG.

Los manifiestos son instantáneas lógicas: los bytes se guardan una vez por
SHA-256. Este módulo no conoce el formato del núcleo RAG.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT_VERSION = 1
DEFAULT_RETENTION_SECONDS = 90 * 24 * 60 * 60
DEFAULT_RESERVED_BYTES = 5 * 1024**3
SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHUNK_SIZE = 1024 * 1024


class StorageError(RuntimeError):
    """Error seguro del almacén; el mensaje no incluye contenido de corpus."""


class UnsafePathError(StorageError):
    pass


class InsufficientSpaceError(StorageError):
    pass


@dataclass(frozen=True)
class SpacePolicy:
    warn_percent: float = 70.0
    block_percent: float = 85.0
    reserve_bytes: int = DEFAULT_RESERVED_BYTES


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    tmp = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Bloqueo advisory entre procesos; estado no se actualiza sin exclusión."""
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not SNAPSHOT_RE.fullmatch(value) or value in {".", ".."}:
        raise UnsafePathError("identificador de versión no válido")
    return value


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _contained_regular_files(root: Path) -> list[tuple[str, Path]]:
    """Inventaría archivos regulares; rechaza enlaces y rutas fuera de raíz."""
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise UnsafePathError("la versión debe ser un directorio real")
    root_real = root.resolve(strict=True)
    results: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            candidate = current_path / name
            if candidate.is_symlink():
                raise UnsafePathError("la versión contiene un enlace simbólico")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root_real):
                raise UnsafePathError("la versión contiene una ruta externa")
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink():
                raise UnsafePathError("la versión contiene un enlace simbólico")
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePathError("la versión contiene un archivo no regular")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root_real):
                raise UnsafePathError("la versión contiene una ruta externa")
            relative = candidate.relative_to(root).as_posix()
            rel = PurePosixPath(relative)
            if rel.is_absolute() or ".." in rel.parts or "\\" in relative:
                raise UnsafePathError("ruta relativa no válida")
            results.append((relative, candidate))
    if not results:
        raise StorageError("la versión está vacía")
    results.sort(key=lambda item: item[0])
    return results


def inventory_version(version_dir: str | Path) -> dict[str, Any]:
    """Valida e inventaría una versión genérica, incluyendo hash y tamaño."""
    files = []
    for relative, path in _contained_regular_files(Path(version_dir)):
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while block := stream.read(CHUNK_SIZE):
                size += len(block)
                digest.update(block)
        files.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
    return {"files": files, "total_bytes": sum(item["size"] for item in files)}


def disk_preflight(path: str | Path, estimated_temporary_bytes: int, policy: SpacePolicy = SpacePolicy()) -> dict[str, Any]:
    """Evalúa porcentaje y reserva del filesystem antes de materializar temporales."""
    if estimated_temporary_bytes < 0 or policy.reserve_bytes < 0:
        raise ValueError("los tamaños no pueden ser negativos")
    stats = os.statvfs(path)
    total = stats.f_frsize * stats.f_blocks
    free = stats.f_frsize * stats.f_bavail
    used_after = total - free + estimated_temporary_bytes
    percent_after = (used_after / total * 100) if total else 100.0
    safe = free - estimated_temporary_bytes >= policy.reserve_bytes and percent_after < policy.block_percent
    return {
        "allowed": safe,
        "warning": percent_after >= policy.warn_percent,
        "used_percent_after": round(percent_after, 2),
        "free_bytes": free,
        "estimated_temporary_bytes": estimated_temporary_bytes,
        "reserve_bytes": policy.reserve_bytes,
    }


class RAGStore:
    """CAS local con snapshots completos, referencias protegidas y retención."""

    def __init__(self, state_dir: str | Path, *, space_policy: SpacePolicy = SpacePolicy()):
        self.root = Path(state_dir).absolute()
        self.space_policy = space_policy
        self.objects = self.root / "objects" / "sha256"
        self.snapshots = self.root / "snapshots"
        self.protected_file = self.root / "protected.json"
        self.lock_file = self.root / ".storage.lock"
        if self.root.is_symlink():
            raise UnsafePathError("directorio de estado inseguro")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise UnsafePathError("el directorio de estado debe ser privado y pertenecer al usuario")
        for directory in (self.objects, self.snapshots):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise UnsafePathError("directorio de estado inseguro")

    def _object_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise StorageError("hash de objeto no válido")
        prefix = self.objects / digest[:2]
        if prefix.is_symlink():
            raise UnsafePathError("prefijo CAS inseguro")
        return prefix / f"{digest}.blob"

    def _snapshot_file(self, snapshot_id: str) -> Path:
        return self.snapshots / f"{_safe_id(snapshot_id)}.json"

    def _read_manifest(self, snapshot_id: str) -> dict[str, Any]:
        path = self._snapshot_file(snapshot_id)
        if path.is_symlink() or not path.is_file():
            raise StorageError("manifiesto no disponible o inválido")
        try:
            raw = path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError("manifiesto no disponible o inválido") from exc
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION or
                manifest.get("snapshot_id") != snapshot_id):
            raise StorageError("manifiesto incompatible")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise StorageError("manifiesto incompatible")
        previous_path = None
        total = 0
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise StorageError("manifiesto incompatible")
            relative, digest, size = item.get("path"), item.get("sha256"), item.get("size")
            if (not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative or
                    not re.fullmatch(r"[a-f0-9]{64}", str(digest)) or type(size) is not int or size < 0):
                raise StorageError("manifiesto incompatible")
            rel = PurePosixPath(relative)
            if rel.is_absolute() or ".." in rel.parts or "." in rel.parts or relative in seen:
                raise StorageError("manifiesto incompatible")
            if previous_path is not None and relative <= previous_path:
                raise StorageError("manifiesto incompatible")
            seen.add(relative)
            previous_path = relative
            total += size
        if type(manifest.get("total_bytes")) is not int or manifest["total_bytes"] != total:
            raise StorageError("manifiesto incompatible")
        return manifest

    def _validate_manifest_objects(self, manifest: dict[str, Any],
                                   checked: set[str] | None = None) -> None:
        """Prove each CAS object a protected snapshot needs is present and intact."""
        checked = checked if checked is not None else set()
        for item in manifest["files"]:
            digest = item["sha256"]
            if digest in checked:
                continue
            raw = self._object_path(digest)
            compressed = raw.with_suffix(".gz")
            if raw.exists() or raw.is_symlink():
                if raw.is_symlink() or not stat.S_ISREG(raw.lstat().st_mode):
                    raise UnsafePathError("objeto CAS inseguro")
                if raw.stat().st_size != item["size"] or self._hash_file(raw) != digest:
                    raise StorageError("objeto CAS corrupto")
            elif compressed.exists() or compressed.is_symlink():
                if compressed.is_symlink() or not stat.S_ISREG(compressed.lstat().st_mode):
                    raise UnsafePathError("objeto comprimido inseguro")
                self._verify_compressed_object(compressed, digest, item["size"])
            else:
                raise StorageError("falta un objeto requerido")
            checked.add(digest)

    def create_snapshot(self, version_dir: str | Path, snapshot_id: str) -> dict[str, Any]:
        """Copia en CAS y confirma manifiesto solo tras verificar archivos."""
        snapshot_id = _safe_id(snapshot_id)
        inventory = inventory_version(version_dir)
        root = Path(version_dir).absolute()
        source_files = dict(_contained_regular_files(root))
        with _locked(self.lock_file):
            destination = self._snapshot_file(snapshot_id)
            if destination.exists():
                existing = self._read_manifest(snapshot_id)
                expected = [(x["path"], x["sha256"], x["size"]) for x in existing["files"]]
                actual = [(x["path"], x["sha256"], x["size"]) for x in inventory["files"]]
                if expected == actual:
                    return {"snapshot_id": snapshot_id, "status": "complete", "reused": True,
                            "files": len(actual), "bytes": inventory["total_bytes"]}
                raise StorageError("el identificador ya existe con contenido distinto")
            missing: dict[str, int] = {}
            for item in inventory["files"]:
                object_path = self._object_path(item["sha256"])
                compressed_path = object_path.with_suffix(".gz")
                if not object_path.exists() and not compressed_path.exists():
                    missing[item["sha256"]] = item["size"]
            preflight = disk_preflight(self.root, sum(missing.values()), self.space_policy)
            if not preflight["allowed"]:
                raise InsufficientSpaceError("espacio insuficiente para importar la versión")
            entries = []
            for item in inventory["files"]:
                digest = item["sha256"]
                object_path = self._object_path(digest)
                object_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if object_path.exists():
                    if object_path.is_symlink() or object_path.stat().st_size != item["size"]:
                        raise StorageError("objeto CAS existente no válido")
                    if self._hash_file(object_path) != digest:
                        raise StorageError("objeto CAS existente corrupto")
                else:
                    compressed_path = object_path.with_suffix(".gz")
                    if compressed_path.exists():
                        self._verify_compressed_object(compressed_path, digest, item["size"])
                    else:
                        self._copy_verified(source_files[item["path"]], object_path, digest, item["size"])
                entries.append(item)
            manifest = {
                "format": FORMAT_VERSION,
                "snapshot_id": snapshot_id,
                "created_at": int(time.time()),
                "total_bytes": inventory["total_bytes"],
                "files": entries,
            }
            _atomic_write(destination, _canonical_json(manifest))
            _fsync_dir(self.snapshots)
        return {"snapshot_id": snapshot_id, "status": "complete", "reused": False,
                "files": len(entries), "bytes": inventory["total_bytes"], "warning": preflight["warning"]}

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(CHUNK_SIZE):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _verify_compressed_object(path: Path, digest: str, expected_size: int) -> None:
        if path.is_symlink():
            raise UnsafePathError("objeto comprimido inseguro")
        hasher = hashlib.sha256()
        size = 0
        try:
            with gzip.open(path, "rb") as stream:
                while block := stream.read(CHUNK_SIZE):
                    size += len(block)
                    if size > expected_size:
                        raise StorageError("objeto comprimido supera tamaño inventariado")
                    hasher.update(block)
        except (OSError, EOFError) as exc:
            raise StorageError("objeto comprimido corrupto") from exc
        if size != expected_size or hasher.hexdigest() != digest:
            raise StorageError("objeto comprimido no coincide con el hash")

    @staticmethod
    def _copy_verified(source: Path, destination: Path, digest: str, expected_size: int) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".object-", dir=destination.parent)
        tmp = Path(temp_name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                while block := src.read(CHUNK_SIZE):
                    dst.write(block)
                    size += len(block)
                    hasher.update(block)
                dst.flush()
                os.fsync(dst.fileno())
            if size != expected_size or hasher.hexdigest() != digest:
                raise StorageError("la versión cambió durante su inventario")
            os.chmod(tmp, 0o600)
            os.replace(tmp, destination)
            _fsync_dir(destination.parent)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise

    def protect(self, *, current: str | None, previous: str | None, pending: list[str] | None = None) -> None:
        """Actualiza atómicamente las referencias que nunca pueden expirar."""
        ids = [x for x in (current, previous, *(pending or [])) if x is not None]
        with _locked(self.lock_file):
            checked: set[str] = set()
            for snapshot_id in ids:
                manifest = self._read_manifest(_safe_id(snapshot_id))
                self._validate_manifest_objects(manifest, checked)
            _atomic_write(self.protected_file, _canonical_json({
                "current": current, "previous": previous, "pending": sorted(set(pending or []))
            }))

    def mark_pending(self, snapshot_id: str, *, pending: bool = True) -> None:
        """Añade o retira un intento sin perder los pins actuales ni anteriores."""
        snapshot_id = _safe_id(snapshot_id)
        with _locked(self.lock_file):
            self._read_manifest(snapshot_id)
            if self.protected_file.is_symlink():
                raise UnsafePathError("estado de protección inseguro")
            try:
                state = json.loads(self.protected_file.read_text())
            except FileNotFoundError:
                state = {"current": None, "previous": None, "pending": []}
            except (OSError, json.JSONDecodeError) as exc:
                raise StorageError("estado de protección inválido") from exc
            if not isinstance(state, dict) or not isinstance(state.get("pending", []), list):
                raise StorageError("estado de protección inválido")
            current, previous = state.get("current"), state.get("previous")
            pending_ids = {_safe_id(value) for value in state.get("pending", [])}
            for value in (current, previous):
                if value is not None:
                    _safe_id(value)
            if pending:
                pending_ids.add(snapshot_id)
            else:
                pending_ids.discard(snapshot_id)
            for value in [x for x in (current, previous, *pending_ids) if x is not None]:
                self._read_manifest(value)
            _atomic_write(self.protected_file, _canonical_json({
                "current": current,
                "previous": previous,
                "pending": sorted(pending_ids),
            }))

    def _protected_ids(self) -> set[str]:
        if self.protected_file.is_symlink():
            raise UnsafePathError("estado de protección inseguro")
        try:
            state = json.loads(self.protected_file.read_text())
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError("estado de protección inválido") from exc
        if not isinstance(state, dict):
            raise StorageError("estado de protección inválido")
        pending = state.get("pending", [])
        if not isinstance(pending, list):
            raise StorageError("estado de protección inválido")
        values = {x for x in (state.get("current"), state.get("previous"), *pending) if x}
        return {_safe_id(value) for value in values}

    def restore(self, snapshot_id: str, destination: str | Path) -> dict[str, Any]:
        """Restaura en directorio nuevo y valida cada hash antes del éxito."""
        with _locked(self.lock_file):
            return self._restore_unlocked(snapshot_id, destination)

    def _restore_unlocked(self, snapshot_id: str, destination: str | Path) -> dict[str, Any]:
        snapshot_id = _safe_id(snapshot_id)
        target = Path(destination).absolute()
        if target.exists() or target.is_symlink():
            raise UnsafePathError("el destino debe ser nuevo")
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = self._read_manifest(snapshot_id)
        preflight = disk_preflight(target.parent, manifest["total_bytes"], self.space_policy)
        if not preflight["allowed"]:
            raise InsufficientSpaceError("espacio insuficiente para restaurar el snapshot")
        temp = Path(tempfile.mkdtemp(prefix=".rag-restore-", dir=target.parent))
        try:
            for item in manifest["files"]:
                rel = PurePosixPath(item["path"])
                if rel.is_absolute() or ".." in rel.parts:
                    raise UnsafePathError("ruta del manifiesto no válida")
                output = temp.joinpath(*rel.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                self._materialize_object(item["sha256"], output, item["size"])
            # El destino se vuelve visible solo cuando todos los archivos quedaron validados.
            os.replace(temp, target)
            _fsync_dir(target.parent)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        return {"snapshot_id": snapshot_id, "status": "complete", "files": len(manifest["files"]),
                "bytes": manifest["total_bytes"]}

    def verify_snapshot(self, snapshot_id: str, expected_dir: str | Path, *,
                        refresh_retention: bool = False,
                        now: int | None = None) -> dict[str, Any]:
        """Restaura y compara un archivo canónico antes de permitir retirar su origen.

        Si ``refresh_retention`` es cierto, la antigüedad empieza en esta
        verificación satisfactoria. Se usa al archivar un paquete gestionado
        antiguo, para que el snapshot verificado sobreviva otros 90 días.
        """
        snapshot_id = _safe_id(snapshot_id)
        expected_dir = Path(expected_dir).absolute()
        expected = inventory_version(expected_dir)
        with _locked(self.lock_file):
            manifest = self._read_manifest(snapshot_id)
            actual = [{"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
                      for item in manifest["files"]]
            if (actual != expected["files"] or
                    manifest["total_bytes"] != expected["total_bytes"]):
                raise StorageError("el snapshot no coincide con el archivo gestionado")
            with tempfile.TemporaryDirectory(prefix=".rag-verify-", dir=self.root) as temporary:
                restored = Path(temporary) / "restored"
                self._restore_unlocked(snapshot_id, restored)
                restored_inventory = inventory_version(restored)
                if restored_inventory != expected:
                    raise StorageError("la restauración verificada no coincide con el origen")
            if refresh_retention:
                manifest["created_at"] = int(time.time()) if now is None else int(now)
                _atomic_write(self._snapshot_file(snapshot_id), _canonical_json(manifest))
            return {"snapshot_id": snapshot_id, "status": "complete",
                    "files": len(actual), "bytes": expected["total_bytes"]}

    def _materialize_object(self, digest: str, destination: Path, expected_size: int) -> None:
        raw = self._object_path(digest)
        compressed = raw.with_suffix(".gz")
        if raw.exists():
            if raw.is_symlink() or self._hash_file(raw) != digest:
                raise StorageError("objeto CAS corrupto")
            with raw.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, CHUNK_SIZE)
        elif compressed.exists():
            self._verify_compressed_object(compressed, digest, expected_size)
            with gzip.open(compressed, "rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, CHUNK_SIZE)
        else:
            raise StorageError("falta un objeto requerido")
        if destination.stat().st_size != expected_size or self._hash_file(destination) != digest:
            destination.unlink(missing_ok=True)
            raise StorageError("restauración no coincide con el hash")
        os.chmod(destination, 0o600)
        with destination.open("rb") as file:
            os.fsync(file.fileno())

    def compact_historical(self, *, now: int | None = None) -> dict[str, Any]:
        """Comprime solo objetos históricos no protegidos; verifica antes del unlink."""
        with _locked(self.lock_file):
            return self._compact_historical_unlocked(now=now)

    def _compact_historical_unlocked(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now
        cutoff = now - DEFAULT_RETENTION_SECONDS
        protected = self._protected_ids()
        protected_hashes: set[str] = set()
        retained_hashes: set[str] = set()
        for manifest_path in self.snapshots.glob("*.json"):
            sid = manifest_path.stem
            manifest = self._read_manifest(sid)
            hashes = {x["sha256"] for x in manifest["files"]}
            if sid in protected:
                protected_hashes.update(hashes)
            elif manifest["created_at"] >= cutoff:
                retained_hashes.update(hashes)
        historical_hashes = retained_hashes - protected_hashes
        compressed_count = 0
        saved = 0
        space_blocked = False
        for digest in sorted(historical_hashes):
            raw = self._object_path(digest)
            zipped = raw.with_suffix(".gz")
            if not raw.exists() or raw.is_symlink():
                continue
            size = raw.stat().st_size
            preflight = disk_preflight(self.root, size, self.space_policy)
            if not preflight["allowed"]:
                space_blocked = True
                break
            fd, temp_name = tempfile.mkstemp(prefix=".compress-", dir=raw.parent)
            os.close(fd)
            temp = Path(temp_name)
            try:
                with raw.open("rb") as src, temp.open("wb") as raw_out:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0, compresslevel=1) as out:
                        shutil.copyfileobj(src, out, CHUNK_SIZE)
                    raw_out.flush()
                    os.fsync(raw_out.fileno())
                # Verifica el resultado y hash íntegro antes de cambiar el nombre o borrar raw.
                with gzip.open(temp, "rb") as check:
                    restored_hash = hashlib.sha256()
                    while block := check.read(CHUNK_SIZE):
                        restored_hash.update(block)
                if restored_hash.hexdigest() != digest:
                    raise StorageError("compresión no verificó el hash")
                compressed_size = temp.stat().st_size
                if compressed_size >= size:
                    temp.unlink()
                    continue
                os.chmod(temp, 0o600)
                os.replace(temp, zipped)
                _fsync_dir(raw.parent)
                # El objeto compressed verificado es ahora un ejemplar restaurable.
                raw.unlink()
                _fsync_dir(raw.parent)
                compressed_count += 1
                saved += size - compressed_size
            finally:
                temp.unlink(missing_ok=True)
        return {"compressed_objects": compressed_count, "bytes_saved": saved,
                "space_blocked": space_blocked}

    def prune(self, *, now: int | None = None, retention_seconds: int = DEFAULT_RETENTION_SECONDS) -> dict[str, int]:
        """Expira snapshots y recolecta objetos sin referencias (alcance por manifest)."""
        if retention_seconds < 0:
            raise ValueError("retención negativa")
        now = int(time.time()) if now is None else now
        cutoff = now - retention_seconds
        with _locked(self.lock_file):
            protected = self._protected_ids()
            removed_snapshots = 0
            all_hashes: set[str] = set()
            for path in sorted(self.snapshots.glob("*.json")):
                manifest = self._read_manifest(path.stem)
                if path.stem not in protected and manifest["created_at"] < cutoff:
                    path.unlink()
                    removed_snapshots += 1
                    continue
                all_hashes.update(x["sha256"] for x in manifest["files"])
            removed_objects = 0
            for prefix in self.objects.iterdir():
                if prefix.is_symlink() or not prefix.is_dir():
                    raise UnsafePathError("árbol CAS inseguro")
                for path in prefix.iterdir():
                    digest = path.name.removesuffix(".blob").removesuffix(".gz")
                    if digest not in all_hashes:
                        if path.is_symlink():
                            raise UnsafePathError("objeto huérfano inseguro")
                        path.unlink()
                        removed_objects += 1
                with contextlib.suppress(OSError):
                    prefix.rmdir()
            _fsync_dir(self.snapshots)
        return {"removed_snapshots": removed_snapshots, "removed_objects": removed_objects}

    def status(self) -> dict[str, Any]:
        snapshots = []
        for path in sorted(self.snapshots.glob("*.json")):
            manifest = self._read_manifest(path.stem)
            snapshots.append({"snapshot_id": path.stem, "created_at": manifest["created_at"],
                              "files": len(manifest["files"]), "bytes": manifest["total_bytes"]})
        return {"snapshots": snapshots, "protected": sorted(self._protected_ids())}


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("version_dir")
    snapshot.add_argument("snapshot_id")
    restore = subparsers.add_parser("restore")
    restore.add_argument("snapshot_id")
    restore.add_argument("destination")
    protect = subparsers.add_parser("protect")
    protect.add_argument("--current")
    protect.add_argument("--previous")
    protect.add_argument("--pending", action="append", default=[])
    subparsers.add_parser("status")
    prune = subparsers.add_parser("prune")
    prune.add_argument("--now", type=int)
    prune.add_argument("--retention-seconds", type=int, default=DEFAULT_RETENTION_SECONDS)
    compact = subparsers.add_parser("compact")
    compact.add_argument("--now", type=int)
    args = parser.parse_args()
    store = RAGStore(args.state_dir)
    if args.command == "snapshot":
        result = store.create_snapshot(args.version_dir, args.snapshot_id)
    elif args.command == "restore":
        result = store.restore(args.snapshot_id, args.destination)
    elif args.command == "protect":
        store.protect(current=args.current, previous=args.previous, pending=args.pending)
        result = {"status": "complete"}
    elif args.command == "status":
        result = store.status()
    elif args.command == "prune":
        result = store.prune(now=args.now, retention_seconds=args.retention_seconds)
    else:
        result = store.compact_historical(now=args.now)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
