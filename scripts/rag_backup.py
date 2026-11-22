"""Respaldo cifrado de versiones RAG a un bucket R2 Standard privado.

La API es compatible S3 y no realiza llamadas al crear el módulo. Las credenciales
se leen de JSON privado. Los objetos y manifiestos se cifran en cliente con AES-GCM
por bloques; no se cargan los bytes del corpus enteros en memoria.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from scripts.rag_storage import (
    CHUNK_SIZE,
    InsufficientSpaceError,
    RAGStore,
    SpacePolicy,
    StorageError,
    UnsafePathError,
    _atomic_write,
    _canonical_json,
    _contained_regular_files,
    _fsync_dir,
    _locked,
    disk_preflight,
    inventory_version,
)

MAGIC = b"RAGAEAD1"
FRAME_SIZE = 12 + 4 + 16
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_SINGLE_PUT_BYTES = 5 * 1024**3
REMOTE_METADATA_RESERVE = 4096
CDC_MIN_BLOCK = 64 * 1024
CDC_AVERAGE_BLOCK = 256 * 1024
CDC_MAX_BLOCK = 1024 * 1024
CDC_MASK = CDC_AVERAGE_BLOCK - 1
CDC_FORMAT = "gear64-sha256-v1"
COMPRESSION_FORMAT = "gzip-1-if-smaller-v1"
CDC_GEAR = tuple(
    int.from_bytes(hashlib.sha256(f"mcp-asir-rag-cdc-v1:{value}".encode()).digest()[:8], "big")
    for value in range(256)
)
DEFAULT_STORAGE_CAP = 8_000_000_000
DEFAULT_CLASS_A_CAP = 10_000
DEFAULT_CLASS_B_CAP = 100_000
FREE_CLASS_A = 1_000_000
FREE_CLASS_B = 10_000_000
FREE_STORAGE_BYTES = 10_000_000_000
EVIDENCE_MAX_AGE = 24 * 60 * 60
ACCOUNT_USAGE_SCHEMA = 1
SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BackupError(RuntimeError):
    pass


class BackupBlocked(BackupError):
    pass


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise BackupBlocked("falta la dependencia cryptography") from exc
    return AESGCM


def generate_recovery_key(key_file: str | Path) -> Path:
    """Genera una clave AES-256 nueva, exclusiva y con permisos 0600."""
    target = Path(key_file).absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() or target.is_symlink():
        raise BackupError("el archivo de clave ya existe")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(target, 0o600)
        _fsync_dir(target.parent)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _load_key(path: str | Path) -> bytes:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise BackupBlocked("archivo de clave no disponible")
    info = target.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise BackupBlocked("el archivo de clave debe pertenecer al usuario y tener modo 0600")
    key = target.read_bytes()
    if len(key) != 32:
        raise BackupBlocked("el archivo de clave tiene formato inválido")
    return key


def _load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if config_path.is_symlink() or not config_path.is_file():
        raise BackupBlocked("configuración privada no disponible")
    info = config_path.stat()
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupBlocked("la configuración debe tener permisos privados")
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupBlocked("configuración privada inválida") from exc
    if not isinstance(config, dict):
        raise BackupBlocked("configuración privada inválida")
    required = {"endpoint_url", "access_key_id", "secret_access_key", "bucket", "key_file"}
    if not required.issubset(config):
        raise BackupBlocked("faltan parámetros de configuración")
    if any(not isinstance(config[name], str) or not config[name] for name in required):
        raise BackupBlocked("valores de configuración no válidos")
    endpoint = config["endpoint_url"]
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or "@" in endpoint:
        raise BackupBlocked("endpoint HTTPS no válido")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,63}", config["bucket"]):
        raise BackupBlocked("nombre de bucket no válido")
    config.setdefault("prefix", "rag-backup/v1")
    if (not isinstance(config["prefix"], str) or
            not re.fullmatch(r"[A-Za-z0-9/_-]{1,160}", config["prefix"].strip("/"))):
        raise BackupBlocked("prefijo de bucket no válido")
    config["prefix"] = config["prefix"].strip("/")
    config.setdefault("storage_cap_bytes", DEFAULT_STORAGE_CAP)
    config.setdefault("monthly_class_a_cap", DEFAULT_CLASS_A_CAP)
    config.setdefault("monthly_class_b_cap", DEFAULT_CLASS_B_CAP)
    # La cuenta ya es compartida; la exclusividad no es un mecanismo válido aquí.
    if config.get("account_exclusive") is True:
        raise BackupBlocked("no se admite asumir exclusividad de la cuenta R2")
    config.pop("account_exclusive", None)
    config.setdefault("account_usage_file", None)
    config.setdefault("max_age_seconds", 300)
    config.setdefault("retries", 2)
    if config["account_usage_file"] is not None and not isinstance(config["account_usage_file"], str):
        raise BackupBlocked("ruta de evidencia no válida")
    numeric_fields = ("storage_cap_bytes", "monthly_class_a_cap", "monthly_class_b_cap",
                      "retries", "max_age_seconds", "reserve_bytes")
    config.setdefault("reserve_bytes", 5 * 1024**3)
    if any(type(config[name]) is not int for name in numeric_fields):
        raise BackupBlocked("presupuesto o caducidad no válidos")
    for field in ("warn_percent", "block_percent"):
        value = config.setdefault(field, 70 if field == "warn_percent" else 85)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise BackupBlocked("política de disco no válida")
    if (config["reserve_bytes"] < 0 or
            not 0 <= config["warn_percent"] < config["block_percent"] <= 100):
        raise BackupBlocked("política de disco no válida")
    if not 0 <= config["storage_cap_bytes"] <= DEFAULT_STORAGE_CAP:
        raise BackupBlocked("el límite local no puede superar 8 GB decimales")
    if not 1 <= config["monthly_class_a_cap"] <= FREE_CLASS_A:
        raise BackupBlocked("presupuesto A fuera de la franquicia gratuita")
    if not 1 <= config["monthly_class_b_cap"] <= FREE_CLASS_B:
        raise BackupBlocked("presupuesto B fuera de la franquicia gratuita")
    if not 0 <= config["retries"] <= 3:
        raise BackupBlocked("número de reintentos no válido")
    if not 1 <= config["max_age_seconds"] <= EVIDENCE_MAX_AGE:
        raise BackupBlocked("caducidad de evidencia no válida")
    if not config["account_usage_file"]:
        raise BackupBlocked("falta account_usage_file con evidencia agregada reciente")
    return config


def _secure_state_dir(path: Path) -> None:
    if path.is_symlink():
        raise BackupBlocked("directorio de estado inseguro")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupBlocked("el directorio de estado debe ser privado y pertenecer al usuario")


def _new_s3_client(config: dict[str, Any]):
    try:
        import boto3
    except ImportError as exc:
        raise BackupBlocked("falta boto3; instale requirements-backup.txt") from exc
    # R2 pide region_name=auto; no se invocan APIs de provisioning ni ACL/lifecycle.
    return boto3.client(
        "s3",
        endpoint_url=config["endpoint_url"],
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        region_name="auto",
        config=__import__("botocore.config", fromlist=["Config"]).Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _utc_month(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp type")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp timezone")
    return parsed.astimezone(UTC)


def _check_usage_margin(config: dict[str, Any], now: int) -> dict[str, Any]:
    evidence_path = config.get("account_usage_file")
    if not evidence_path:
        raise BackupBlocked("margen gratuito desconocido; falta evidencia agregada reciente")
    path = Path(evidence_path)
    if path.is_symlink():
        raise BackupBlocked("evidencia de uso insegura")
    try:
        evidence = json.loads(path.read_text())
        if not isinstance(evidence, dict):
            raise TypeError("evidence object")
        if evidence.get("schema_version") != ACCOUNT_USAGE_SCHEMA or evidence.get("complete") is not True:
            raise ValueError("incomplete")
        observed_at = _parse_utc(evidence["observed_at"])
        period_start = _parse_utc(evidence["period_start"])
        period_end = _parse_utc(evidence["period_end"])
        storage = evidence["storage_bytes"]
        class_a = evidence["class_a_operations"]
        class_b = evidence["class_b_operations"]
        if any(type(value) is not int for value in (storage, class_a, class_b)):
            raise ValueError("counter type")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise BackupBlocked("evidencia de uso agregada inválida") from exc
    age = datetime.fromtimestamp(now, UTC) - observed_at
    if (age.total_seconds() < 0 or age.total_seconds() > config["max_age_seconds"] or
            period_start > period_end or period_end > observed_at or
            (observed_at - period_end).total_seconds() > config["max_age_seconds"] or
            (observed_at - period_start).total_seconds() < 31 * 24 * 60 * 60 or
            min(storage, class_a, class_b) < 0):
        raise BackupBlocked("evidencia de uso agregada caducada o inválida")
    if storage > FREE_STORAGE_BYTES or class_a > FREE_CLASS_A or class_b > FREE_CLASS_B:
        raise BackupBlocked("franquicia gratuita ya agotada según evidencia")
    return {
        "observed_at": observed_at,
        "period_start": period_start,
        "period_end": period_end,
        "storage_bytes": storage,
        "class_a_operations": class_a,
        "class_b_operations": class_b,
    }


def _encrypted_size(plain_size: int) -> int:
    chunks = (plain_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return len(MAGIC) + 4 + plain_size + chunks * FRAME_SIZE


def _split_content_defined(
    source_path: Path, staging: Path, known_hashes: set[str]
) -> tuple[list[dict[str, Any]], str]:
    """Divide el contenido en bloques estables mediante CDC, con un máximo de 1 MiB."""
    blocks: list[dict[str, Any]] = []
    block = bytearray()
    fingerprint = 0
    hasher = hashlib.sha256()

    def finish() -> None:
        nonlocal block, fingerprint
        if not block:
            return
        digest = hashlib.sha256(block).hexdigest()
        if digest not in known_hashes:
            block_path = staging / f"{digest}.raw"
            if not block_path.exists():
                with block_path.open("xb") as output:
                    output.write(block)
        blocks.append({"sha256": digest, "size": len(block)})
        block = bytearray()
        fingerprint = 0

    with source_path.open("rb") as source:
        while data := source.read(CHUNK_SIZE):
            hasher.update(data)
            for value in data:
                block.append(value)
                fingerprint = ((fingerprint << 1) + CDC_GEAR[value]) & 0xFFFFFFFFFFFFFFFF
                length = len(block)
                if length >= CDC_MIN_BLOCK and (
                    (fingerprint & CDC_MASK) == 0 or length >= CDC_MAX_BLOCK
                ):
                    finish()
    finish()
    return blocks, hasher.hexdigest()


def _prepare_compressed_payload(raw_path: Path) -> tuple[Path, str, int, str]:
    """Conserva gzip solo si reduce tamaño y genera el payload determinísticamente."""
    compressed_path = raw_path.with_suffix(".gz")
    with raw_path.open("rb") as source, compressed_path.open("xb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0, compresslevel=1) as output:
            shutil.copyfileobj(source, output, CHUNK_SIZE)
        raw_output.flush()
        os.fsync(raw_output.fileno())
    raw_size = raw_path.stat().st_size
    compressed_size = compressed_path.stat().st_size
    if compressed_size < raw_size:
        raw_path.unlink()
        payload_path = compressed_path
        encoding = "gzip"
    else:
        compressed_path.unlink()
        payload_path = raw_path
        encoding = "identity"
    return payload_path, encoding, payload_path.stat().st_size, _hash_path(payload_path)


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(CHUNK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def _encrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes, aad: bytes) -> tuple[int, str]:
    AESGCM = _crypto()
    aes = AESGCM(key)
    destination.write(MAGIC + CHUNK_SIZE.to_bytes(4, "big"))
    total = 0
    digest = hashlib.sha256()
    while block := source.read(CHUNK_SIZE):
        nonce = os.urandom(12)
        encrypted = aes.encrypt(nonce, block, aad + total.to_bytes(8, "big"))
        destination.write(nonce)
        destination.write(len(encrypted).to_bytes(4, "big"))
        destination.write(encrypted)
        total += len(block)
        digest.update(block)
    return total, digest.hexdigest()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    value = _read_up_to(stream, size)
    if len(value) != size:
        raise BackupError("objeto cifrado truncado")
    return value


def _read_up_to(stream: BinaryIO, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        block = stream.read(size - len(parts))
        if not block:
            break
        parts.extend(block)
    return bytes(parts)


def _decrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes, aad: bytes,
                    *, expected_hash: str | None = None, expected_size: int | None = None,
                    max_size: int | None = None) -> tuple[int, str]:
    AESGCM = _crypto()
    aes = AESGCM(key)
    if _read_exact(source, len(MAGIC)) != MAGIC:
        raise BackupError("formato cifrado no reconocido")
    chunk_size = int.from_bytes(_read_exact(source, 4), "big")
    if chunk_size != CHUNK_SIZE:
        raise BackupError("tamaño de bloque cifrado no compatible")
    total = 0
    digest = hashlib.sha256()
    while True:
        nonce = _read_up_to(source, 12)
        if nonce == b"":
            break
        if len(nonce) != 12:
            raise BackupError("objeto cifrado truncado")
        size = int.from_bytes(_read_exact(source, 4), "big")
        if size < 16 or size > CHUNK_SIZE + 16:
            raise BackupError("bloque cifrado no válido")
        encrypted = _read_exact(source, size)
        try:
            block = aes.decrypt(nonce, encrypted, aad + total.to_bytes(8, "big"))
        except Exception as exc:
            raise BackupError("verificación AEAD fallida") from exc
        if max_size is not None and total + len(block) > max_size:
            raise BackupBlocked("contenido descifrado supera el límite seguro")
        if expected_size is not None and total + len(block) > expected_size:
            raise BackupError("objeto descifrado supera el tamaño esperado")
        destination.write(block)
        total += len(block)
        digest.update(block)
    result = digest.hexdigest()
    if expected_hash is not None and result != expected_hash:
        raise BackupError("hash restaurado distinto del inventario")
    if expected_size is not None and total != expected_size:
        raise BackupError("tamaño restaurado distinto del inventario")
    return total, result


def _response_body(response: Any) -> BinaryIO:
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None or not hasattr(body, "read"):
        raise BackupError("respuesta remota inválida")
    return body


def _base_result(status: str, reason: str, *, snapshot_id: str | None = None,
                 bytes_count: int = 0, operations: int = 0, at: int | None = None) -> dict[str, Any]:
    timestamp = int(time.time()) if at is None else at
    return {
        "status": status,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "bytes": bytes_count,
        "operations": operations,
        "at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
    }


class _Budget:
    """Reserva durable antes de llamadas, comparada con métrica account-wide."""

    def __init__(self, path: Path, config: dict[str, Any], evidence: dict[str, Any], now: int):
        self.path = path
        self.journal = path.with_name("operation-reservations.jsonl")
        self.month = _utc_month(now)
        self.now = now
        if path.is_symlink() or self.journal.is_symlink():
            raise BackupBlocked("ledger de operaciones inseguro")
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupBlocked("ledger mensual ilegible") from exc
        if not isinstance(state, dict):
            raise BackupBlocked("ledger mensual ilegible")
        journal_exists = self.journal.exists()
        if state.get("month") != self.month:
            state = {"month": self.month, "class_a": 0, "class_b": 0, "unknown": 0}
        elif any(type(state.get(field, 0)) is not int or state.get(field, 0) < 0
                 for field in ("class_a", "class_b", "unknown")):
            raise BackupBlocked("ledger mensual ilegible")
        if not journal_exists and any(state.get(field, 0) for field in ("class_a", "class_b", "unknown")):
            raise BackupBlocked("falta el ledger durable de operaciones")
        self.state = state
        self.config = config
        self.evidence = evidence
        self.reservations: list[dict[str, Any]] = []
        try:
            for line in self.journal.read_text().splitlines():
                row = json.loads(line)
                if (not isinstance(row, dict) or type(row.get("at")) is not int or
                        row.get("class") not in {"A", "B", "unknown"}):
                    raise ValueError("reservation row")
                if row.get("at", 0) >= self.evidence["period_start"].timestamp():
                    self.reservations.append(row)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise BackupBlocked("ledger de operaciones ilegible") from exc
        except ValueError as exc:
            raise BackupBlocked("ledger de operaciones ilegible") from exc
        if journal_exists and len(self.reservations) != len(self.journal.read_text().splitlines()):
            _atomic_write(self.journal, b"".join(_canonical_json(row) for row in self.reservations))
        counts = {
            "class_a": sum(row["class"] in {"A", "unknown"} and
                           _utc_month(row["at"]) == self.month for row in self.reservations),
            "class_b": sum(row["class"] in {"B", "unknown"} and
                           _utc_month(row["at"]) == self.month for row in self.reservations),
            "unknown": sum(row["class"] == "unknown" and
                           _utc_month(row["at"]) == self.month for row in self.reservations),
        }
        if any(state.get(field, 0) != value for field, value in counts.items()):
            self.state.update(counts)
            _atomic_write(self.path, _canonical_json(self.state))

    def ensure_capacity(self, planned: dict[str, int]) -> None:
        """Comprueba toda la operación, incluidos reintentos, antes del primer PUT."""
        for klass, count in planned.items():
            if klass not in {"A", "B"} or count < 0:
                raise ValueError("plan de operaciones no válido")
            observed = self.evidence["class_a_operations" if klass == "A" else "class_b_operations"]
            reserved = sum(1 for row in self.reservations
                           if row["at"] >= self.evidence["period_start"].timestamp()
                           and row["class"] in {klass, "unknown"})
            free_limit = FREE_CLASS_A if klass == "A" else FREE_CLASS_B
            account_guard = config_guard_limit(klass)
            if observed + reserved + count > min(free_limit, account_guard):
                raise BackupBlocked(f"margen account-wide de clase {klass} desconocido o agotado")
            local_limit = self.config["monthly_class_a_cap" if klass == "A" else "monthly_class_b_cap"]
            already = sum(
                1 for row in self.reservations
                if row["class"] in {klass, "unknown"} and
                _utc_month(int(row["at"])) == self.month
            )
            if already + count > local_limit:
                raise BackupBlocked(f"presupuesto preventivo local de clase {klass} agotado")

    def reserve(self, operation_class: str | None) -> None:
        now = self.now
        if operation_class not in {"A", "B"}:
            classes = ("A", "B")
        else:
            classes = (operation_class,)
        # Reservas fallidas también cuentan; si la clasificación no es segura, cuenta en ambas.
        for klass in classes:
            observed_field = "class_a_operations" if klass == "A" else "class_b_operations"
            observed = self.evidence[observed_field]
            reserved = sum(1 for row in self.reservations if row["at"] >= self.evidence["period_start"].timestamp()
                           and (row["class"] in {klass, "unknown"}))
            free_limit = FREE_CLASS_A if klass == "A" else FREE_CLASS_B
            account_guard = int(config_guard_limit(klass))
            if observed + reserved + 1 > min(free_limit, account_guard):
                raise BackupBlocked(f"margen account-wide de clase {klass} desconocido o agotado")
        field, limit = (("class_a", self.config["monthly_class_a_cap"]) if operation_class == "A"
                        else ("class_b", self.config["monthly_class_b_cap"]))
        if operation_class not in {"A", "B"}:
            field, limit = "unknown", min(self.config["monthly_class_a_cap"], self.config["monthly_class_b_cap"])
        local_count = sum(
            1 for row in self.reservations
            if row["class"] in {operation_class, "unknown"} and
            _utc_month(int(row["at"])) == self.month
        )
        if local_count + 1 > limit:
            raise BackupBlocked(f"presupuesto preventivo local de clase {operation_class or 'unknown'} agotado")
        row = {"at": now, "class": operation_class or "unknown"}
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            payload = _canonical_json(row)
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        self.reservations.append(row)
        if operation_class in {"A", "B"}:
            self.state[field] = self.state.get(field, 0) + 1
        else:
            self.state["unknown"] = self.state.get("unknown", 0) + 1
        _atomic_write(self.path, _canonical_json(self.state))


def config_guard_limit(operation_class: str) -> int:
    """Deja margen account-wide para actividad ajena y latencia de métricas."""
    return 900_000 if operation_class == "A" else 9_000_000


class _RemoteCatalog:
    def __init__(self, path: Path, prefix: str):
        self.path = path
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9/_-]{1,160}", prefix.strip("/")):
            raise BackupBlocked("prefijo de catálogo no válido")
        self.prefix = prefix.strip("/")
        if path.is_symlink():
            raise BackupBlocked("catálogo local remoto inseguro")
        try:
            catalog = json.loads(path.read_text())
        except FileNotFoundError:
            catalog = {"format": 1, "prefix": self.prefix, "objects": {}, "versions": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupBlocked("catálogo local remoto ilegible") from exc
        if (not isinstance(catalog, dict) or catalog.get("format") != 1 or
                catalog.get("prefix") != self.prefix or not isinstance(catalog.get("objects"), dict) or
                not isinstance(catalog.get("versions"), dict)):
            raise BackupBlocked("catálogo local remoto ilegible")
        for digest, obj in catalog["objects"].items():
            if (not re.fullmatch(r"[a-f0-9]{64}", str(digest)) or not isinstance(obj, dict) or
                    obj.get("key") != self.object_key(digest) or
                    type(obj.get("plain_size")) is not int or obj["plain_size"] < 0 or
                    type(obj.get("encrypted_size")) is not int or obj["encrypted_size"] <= 0):
                raise BackupBlocked("catálogo local remoto inválido")
            if ("encoding" in obj and obj["encoding"] not in {"identity", "gzip"}) or (
                    "stored_size" in obj and (type(obj["stored_size"]) is not int or obj["stored_size"] <= 0)):
                raise BackupBlocked("catálogo local remoto inválido")
        for snapshot_id, version in catalog["versions"].items():
            if (not isinstance(snapshot_id, str) or not SAFE_VERSION_RE.fullmatch(snapshot_id) or
                    not isinstance(version, dict) or version.get("key") != self.manifest_key(snapshot_id) or
                    type(version.get("created_at")) is not int or not isinstance(version.get("hashes"), list) or
                    any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                        for value in version["hashes"])):
                raise BackupBlocked("catálogo local remoto inválido")
            if not set(version["hashes"]).issubset(catalog["objects"]):
                raise BackupBlocked("catálogo local remoto incompleto")
        self.data = catalog

    def save(self) -> None:
        _atomic_write(self.path, _canonical_json(self.data))

    def object_key(self, digest: str) -> str:
        return f"{self.prefix}/objects/sha256/{digest}"

    def manifest_key(self, snapshot_id: str) -> str:
        return f"{self.prefix}/manifests/{snapshot_id}.enc"


def initialize_remote_catalog(state_dir: str | Path, prefix: str = "rag-backup/v1") -> None:
    """Crea el catálogo local vacío tras verificar externamente que el prefijo es nuevo."""
    root = Path(state_dir).absolute()
    _secure_state_dir(root)
    catalog_path = root / "remote-catalog.json"
    with _locked(root / ".backup.lock"):
        if catalog_path.exists() or catalog_path.is_symlink():
            raise BackupBlocked("el catálogo remoto local ya existe")
        catalog = _RemoteCatalog(catalog_path, prefix)
        catalog.save()


def _remote_put(client: Any, bucket: str, key: str, body: BinaryIO, size: int,
                budget: _Budget, retries: int) -> int:
    attempts = 0
    while True:
        budget.reserve("A")
        attempts += 1
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=size,
                              ContentType="application/octet-stream", StorageClass="STANDARD")
            return attempts
        except Exception as exc:
            if attempts > retries:
                raise BackupError("falló la escritura remota; no se confirma el manifiesto") from exc
            body.seek(0)


def _remote_decrypt_to_file(
    client: Any,
    bucket: str,
    key_name: str,
    destination: Path | BinaryIO,
    encryption_key: bytes,
    aad: bytes,
    budget: _Budget,
    retries: int,
    *,
    expected_hash: str | None = None,
    expected_size: int | None = None,
    max_size: int | None = None,
) -> tuple[int, str, int]:
    """Reintenta una lectura completa, contabilizando cada GET incluso si se corta el stream."""
    attempts = 0
    while True:
        budget.reserve("B")
        attempts += 1
        try:
            response = client.get_object(Bucket=bucket, Key=key_name)
            output_context = destination.open("wb") if isinstance(destination, Path) else contextlib.nullcontext(destination)
            with contextlib.closing(_response_body(response)) as encrypted, output_context as output:
                if not isinstance(destination, Path):
                    output.seek(0)
                    output.truncate()
                size, digest = _decrypt_stream(
                    encrypted, output, encryption_key, aad, expected_hash=expected_hash,
                    expected_size=expected_size, max_size=max_size)
                output.flush()
                os.fsync(output.fileno())
            return size, digest, attempts
        except Exception as exc:
            if isinstance(destination, Path):
                destination.unlink(missing_ok=True)
            else:
                destination.seek(0)
                destination.truncate()
            if attempts > retries:
                raise BackupError("falló la lectura o validación remota") from exc


def _snapshot_id(version_dir: Path) -> str:
    value = version_dir.name
    if not SAFE_VERSION_RE.fullmatch(value) or value in {".", ".."}:
        raise UnsafePathError("nombre de versión no válido para respaldo")
    return value


def backup_version(version_dir: str | Path, config_path: str | Path, state_dir: str | Path,
                   *, s3_client: Any = None, now: int | None = None) -> dict[str, Any]:
    """Crea primero la copia local y sube objetos cifrados, manifest commit al final.

    Devuelve siempre `complete|pending|blocked|failed`; los motivos son seguros y
    no contienen secretos, rutas privadas, excepciones remotas ni contenido.
    """
    now = int(time.time()) if now is None else int(now)
    version_dir = Path(version_dir).absolute()
    state_dir = Path(state_dir).absolute()
    snapshot_id = None
    try:
        _secure_state_dir(state_dir)
        inventory = inventory_version(version_dir)
        snapshot_id = _snapshot_id(version_dir)
        store = RAGStore(state_dir / "local")
        store.create_snapshot(version_dir, snapshot_id)
        store.mark_pending(snapshot_id)
    except BackupBlocked as exc:
        return _base_result("blocked", str(exc), snapshot_id=snapshot_id, at=now)
    except InsufficientSpaceError as exc:
        return _base_result("blocked", str(exc), snapshot_id=snapshot_id, at=now)
    except (StorageError, OSError, ValueError) as exc:
        return _base_result("failed", str(exc) if isinstance(exc, StorageError) else "versión local no disponible",
                            snapshot_id=snapshot_id, at=now)

    try:
        config = _load_config(config_path)
        key = _load_key(config["key_file"])
        evidence = _check_usage_margin(config, now)
        policy = SpacePolicy(
            reserve_bytes=int(config.get("reserve_bytes", 5 * 1024**3)),
            warn_percent=float(config.get("warn_percent", 70)),
            block_percent=float(config.get("block_percent", 85)),
        )
        # Compressed/plain block staging stays on disk; only one bounded AEAD
        # bloque cifrado se materializa junto al payload. Reservar el contenido completo, el
        # mayor bloque cifrado y el manifiesto acotado.
        temporary_estimate = inventory["total_bytes"] + _encrypted_size(CDC_MAX_BLOCK) + MAX_MANIFEST_BYTES
        if not disk_preflight(state_dir, temporary_estimate, policy)["allowed"]:
            raise BackupBlocked("espacio local insuficiente para temporales de respaldo")
        with _locked(state_dir / ".backup.lock"):
            client = s3_client if s3_client is not None else _new_s3_client(config)
            catalog_path = state_dir / "remote-catalog.json"
            if not catalog_path.is_file() or catalog_path.is_symlink():
                raise BackupBlocked("catálogo local remoto ausente; no se puede deducir el contenido del prefijo")
            catalog = _RemoteCatalog(catalog_path, config["prefix"])
            budget = _Budget(state_dir / "monthly-budget.json", config, evidence, now)
            previous = catalog.data["versions"].get(snapshot_id)
            identity = hashlib.sha256(_canonical_json(inventory)).hexdigest()
            if previous and previous.get("inventory_sha256") == identity:
                store.mark_pending(snapshot_id, pending=False)
                return _base_result("complete", "ya respaldada", snapshot_id=snapshot_id,
                                    bytes_count=previous.get("encrypted_bytes", 0), at=now)

            files_by_path = dict(_contained_regular_files(version_dir))
            remote_ops = 0
            sent_bytes = 0
            with tempfile.TemporaryDirectory(prefix=".rag-backup-", dir=state_dir) as staging:
                staging_path = Path(staging)
                raw_blocks: dict[str, dict[str, Any]] = {}
                file_entries: list[dict[str, Any]] = []
                known_hashes = set(catalog.data["objects"])
                for entry in inventory["files"]:
                    descriptors, source_hash = _split_content_defined(
                        files_by_path[entry["path"]], staging_path, known_hashes | set(raw_blocks))
                    if source_hash != entry["sha256"]:
                        raise BackupError("la versión local cambió durante el inventario")
                    for block in descriptors:
                        digest = block["sha256"]
                        known = catalog.data["objects"].get(digest)
                        if known is not None:
                            if (known.get("key") != catalog.object_key(digest) or
                                    int(known.get("plain_size", -1)) != block["size"]):
                                raise BackupBlocked("colisión o catálogo local inconsistente")
                            block["encoding"] = known.get("encoding", "identity")
                            block["stored_size"] = int(known.get("stored_size", block["size"]))
                        else:
                            raw_blocks.setdefault(digest, {"path": staging_path / f"{digest}.raw",
                                                            "size": block["size"]})
                    file_entries.append({**entry, "blocks": descriptors})

                # La compresión se decide antes de modificar R2 y solo se conserva
                # si reduce tamaño; catálogo y ciphertext mantienen la misma forma.
                for digest, block in raw_blocks.items():
                    payload, encoding, stored_size, stored_hash = _prepare_compressed_payload(block["path"])
                    block.update({"path": payload, "encoding": encoding,
                                  "stored_size": stored_size, "stored_hash": stored_hash})
                    for file_entry in file_entries:
                        for descriptor in file_entry["blocks"]:
                            if descriptor["sha256"] == digest:
                                descriptor["encoding"] = encoding
                                descriptor["stored_size"] = stored_size

                projected = sum(_encrypted_size(block["stored_size"]) for block in raw_blocks.values())
                if any(_encrypted_size(block["stored_size"]) > MAX_SINGLE_PUT_BYTES
                       for block in raw_blocks.values()):
                    raise BackupBlocked("bloque supera el límite de PutObject; no se usa multipart")
                manifest_plain = {
                    "format": 2,
                    "chunker": CDC_FORMAT,
                    "compression": COMPRESSION_FORMAT,
                    "snapshot_id": snapshot_id,
                    "created_at": now,
                    "inventory_sha256": identity,
                    "files": file_entries,
                }
                manifest_bytes = _canonical_json(manifest_plain)
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    raise BackupBlocked("manifiesto supera el límite seguro de 64 MiB")
                manifest_encrypted_size = _encrypted_size(len(manifest_bytes))
                remote_bytes = sum(int(x.get("accounted_size", int(x["encrypted_size"]) + REMOTE_METADATA_RESERVE))
                                   for x in catalog.data["objects"].values())
                versions_bytes = sum(int(x.get("manifest_accounted_size",
                                                int(x.get("manifest_encrypted_size", 0)) + REMOTE_METADATA_RESERVE))
                                     for x in catalog.data["versions"].values())
                projected_account_bytes = projected + len(raw_blocks) * REMOTE_METADATA_RESERVE
                manifest_accounted_size = manifest_encrypted_size + REMOTE_METADATA_RESERVE
                repo_projected = remote_bytes + versions_bytes + projected_account_bytes + manifest_accounted_size
                if repo_projected > config["storage_cap_bytes"]:
                    raise BackupBlocked("el presupuesto preventivo de almacenamiento gratuito no cabe")
                # No restar el uso propio ya observado: el coste transitorio de sustitución
                # puede solaparse con el snapshot de métricas, y el sobreconteo es intencional.
                if evidence["storage_bytes"] + projected_account_bytes + manifest_accounted_size > FREE_STORAGE_BYTES:
                    raise BackupBlocked("la franquicia agregada de almacenamiento no tiene margen")
                put_calls = len(raw_blocks) + 1
                budget.ensure_capacity({"A": put_calls * (config["retries"] + 1)})

                for digest in sorted(raw_blocks):
                    block = raw_blocks[digest]
                    encrypted_path = staging_path / f"{digest}.enc"
                    with block["path"].open("rb") as source, encrypted_path.open("wb") as output:
                        stored_size, stored_hash = _encrypt_stream(
                            source, output, key, b"block:" + digest.encode())
                        output.flush()
                        os.fsync(output.fileno())
                    encrypted_size = encrypted_path.stat().st_size
                    if (stored_size != block["stored_size"] or stored_hash != block["stored_hash"] or
                            encrypted_size != _encrypted_size(block["stored_size"])):
                        raise BackupError("bloque cambió durante el cifrado")
                    with encrypted_path.open("rb") as encrypted:
                        remote_ops += _remote_put(client, config["bucket"], catalog.object_key(digest),
                                                  encrypted, encrypted_size, budget, config["retries"])
                    encrypted_path.unlink()
                    catalog.data["objects"][digest] = {
                        "key": catalog.object_key(digest),
                        "encrypted_size": encrypted_size,
                        "accounted_size": encrypted_size + REMOTE_METADATA_RESERVE,
                        "plain_size": block["size"],
                        "stored_size": block["stored_size"],
                        "encoding": block["encoding"],
                    }
                    catalog.save()
                    sent_bytes += encrypted_size

                # El manifiesto se cifra y se publica como último PUT; es el commit remoto.
                manifest_path = staging_path / "manifest.enc"
                with manifest_path.open("wb") as output:
                    size, _ = _encrypt_stream(
                        __import__("io").BytesIO(manifest_bytes), output, key,
                        b"manifest:" + snapshot_id.encode())
                    output.flush()
                    os.fsync(output.fileno())
                if size != len(manifest_bytes) or manifest_path.stat().st_size != manifest_encrypted_size:
                    raise BackupError("manifiesto cifrado no coincide con el cálculo")
                with manifest_path.open("rb") as encrypted:
                    remote_ops += _remote_put(client, config["bucket"], catalog.manifest_key(snapshot_id),
                                              encrypted, manifest_encrypted_size, budget, config["retries"])
                sent_bytes += manifest_encrypted_size
            hashes = sorted({block["sha256"] for file_entry in file_entries for block in file_entry["blocks"]})
            catalog.data["versions"][snapshot_id] = {
                "key": catalog.manifest_key(snapshot_id),
                "inventory_sha256": identity,
                "created_at": now,
                "hashes": hashes,
                "manifest_encrypted_size": manifest_encrypted_size,
                "manifest_accounted_size": manifest_accounted_size,
                "encrypted_bytes": projected + manifest_encrypted_size,
            }
            catalog.save()
            store.mark_pending(snapshot_id, pending=False)
            return _base_result("complete", "respaldo cifrado verificado y manifiesto confirmado",
                                snapshot_id=snapshot_id, bytes_count=sent_bytes, operations=remote_ops, at=now)
    except BackupBlocked as exc:
        return _base_result("blocked", str(exc), snapshot_id=snapshot_id, at=now)
    except (BackupError, StorageError, OSError, ValueError, KeyError, TypeError) as exc:
        safe_reason = str(exc) if isinstance(exc, (BackupBlocked, StorageError)) else "fallo de respaldo remoto"
        return _base_result("pending", safe_reason, snapshot_id=snapshot_id, at=now)
    except Exception:  # noqa: BLE001 - se sanitizan los errores del SDK antes de devolverlos
        # No exponer mensajes de SDK, que pueden incluir endpoint/identificadores.
        return _base_result("pending", "fallo remoto no clasificado; la copia local se conserva",
                            snapshot_id=snapshot_id, at=now)


def restore_remote(snapshot_id: str, config_path: str | Path, state_dir: str | Path,
                   destination: str | Path, *, s3_client: Any = None, now: int | None = None) -> dict[str, Any]:
    """Descarga y valida una instantánea completa en directorio aislado."""
    now = int(time.time()) if now is None else int(now)
    state_dir = Path(state_dir).absolute()
    try:
        _secure_state_dir(state_dir)
        with _locked(state_dir / ".backup.lock"):
            return _restore_remote_unlocked(snapshot_id, config_path, state_dir, destination,
                                            s3_client=s3_client, now=now)
    except BackupBlocked as exc:
        return _base_result("blocked", str(exc), snapshot_id=snapshot_id, at=now)


def _restore_remote_unlocked(snapshot_id: str, config_path: str | Path, state_dir: Path,
                             destination: str | Path, *, s3_client: Any, now: int) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    if not SAFE_VERSION_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        return _base_result("blocked", "identificador no válido", snapshot_id=snapshot_id, at=now)
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        return _base_result("blocked", "el destino debe ser nuevo", snapshot_id=snapshot_id, at=now)
    state_dir = Path(state_dir).absolute()
    try:
        config = _load_config(config_path)
        key = _load_key(config["key_file"])
        evidence = _check_usage_margin(config, now)
        client = s3_client if s3_client is not None else _new_s3_client(config)
        budget = _Budget(state_dir / "monthly-budget.json", config, evidence, now)
        manifest_key = f"{config['prefix']}/manifests/{snapshot_id}.enc"
        budget.ensure_capacity({"B": config["retries"] + 1})
        with tempfile.TemporaryFile() as plaintext:
            _remote_decrypt_to_file(
                client, config["bucket"], manifest_key, plaintext, key,
                b"manifest:" + snapshot_id.encode(), budget, config["retries"],
                max_size=MAX_MANIFEST_BYTES)
            plaintext.seek(0)
            manifest_bytes = plaintext.read(MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BackupBlocked("manifiesto remoto supera el límite seguro")
        manifest = json.loads(manifest_bytes)
        if manifest.get("format") not in {1, 2} or manifest.get("snapshot_id") != snapshot_id:
            raise BackupError("manifiesto remoto incompatible")
        if manifest["format"] == 2 and (
                manifest.get("chunker") != CDC_FORMAT or manifest.get("compression") != COMPRESSION_FORMAT):
            raise BackupError("formato de bloques remoto no compatible")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise BackupError("manifiesto sin archivos")
        seen_paths: set[str] = set()
        canonical_files: list[dict[str, Any]] = []
        file_blocks: dict[str, list[dict[str, Any]]] = {}
        unique_blocks: dict[str, dict[str, Any]] = {}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise BackupError("entrada de archivo remota no válida")
            path_value = item["path"]
            rel = PurePosixPath(path_value)
            if (rel.is_absolute() or not rel.parts or ".." in rel.parts or "." in rel.parts or
                    "\\" in path_value or "\x00" in path_value or path_value in seen_paths or
                    not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))) or
                    type(item.get("size")) is not int or item["size"] < 0):
                raise UnsafePathError("manifiesto remoto contiene rutas no válidas")
            seen_paths.add(path_value)
            canonical = {"path": path_value, "sha256": item["sha256"], "size": item["size"]}
            canonical_files.append(canonical)
            if manifest["format"] == 1:
                blocks = ([] if item["size"] == 0 else [{
                    "sha256": item["sha256"], "size": item["size"],
                    "stored_size": item["size"], "encoding": "identity",
                }])
            else:
                blocks = item.get("blocks")
                if not isinstance(blocks, list):
                    raise BackupError("manifiesto remoto sin bloques")
            total = 0
            normalized: list[dict[str, Any]] = []
            for block in blocks:
                if not isinstance(block, dict):
                    raise BackupError("bloque remoto no válido")
                digest = block.get("sha256")
                size, stored_size, encoding = block.get("size"), block.get("stored_size"), block.get("encoding")
                if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or
                        type(size) is not int or size <= 0 or type(stored_size) is not int or stored_size <= 0 or
                        encoding not in {"identity", "gzip"} or
                        (encoding == "identity" and stored_size != size)):
                    raise BackupError("bloque remoto no válido")
                normalized_block = {"sha256": digest, "size": size, "stored_size": stored_size,
                                    "encoding": encoding}
                existing = unique_blocks.get(digest)
                if existing is not None and existing != normalized_block:
                    raise BackupError("referencias incompatibles al mismo bloque")
                unique_blocks[digest] = normalized_block
                normalized.append(normalized_block)
                total += size
            if total != item["size"] or (item["size"] > 0 and not normalized):
                raise BackupError("tamaño de bloques no coincide con archivo")
            file_blocks[path_value] = normalized
        inventory_identity = hashlib.sha256(_canonical_json({
            "files": canonical_files, "total_bytes": sum(item["size"] for item in canonical_files)
        })).hexdigest()
        if inventory_identity != manifest.get("inventory_sha256"):
            raise BackupError("identidad del manifiesto remoto no coincide")
        unique_digests = set(unique_blocks)
        budget.ensure_capacity({"B": (len(unique_digests) + 1) * (config["retries"] + 1)})
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".rag-restore-", dir=target.parent) as tmp:
            temp_root = Path(tmp)
            staging = temp_root / "tree"
            object_cache = temp_root / "objects"
            staging.mkdir(mode=0o700)
            object_cache.mkdir(mode=0o700)
            cached: dict[str, Path] = {}
            aad_prefix = b"object:" if manifest["format"] == 1 else b"block:"
            for digest, block in unique_blocks.items():
                encrypted_plain = temp_root / f"{digest}.payload"
                _remote_decrypt_to_file(
                    client, config["bucket"], f"{config['prefix']}/objects/sha256/{digest}", encrypted_plain,
                    key, aad_prefix + digest.encode(), budget, config["retries"],
                    expected_size=block["stored_size"], max_size=block["stored_size"])
                cached_object = object_cache / digest
                if block["encoding"] == "gzip":
                    hasher = hashlib.sha256()
                    restored_size = 0
                    try:
                        with gzip.open(encrypted_plain, "rb") as compressed, cached_object.open("xb") as output:
                            while data := compressed.read(CHUNK_SIZE):
                                restored_size += len(data)
                                if restored_size > block["size"]:
                                    raise BackupError("bloque descomprimido supera tamaño esperado")
                                hasher.update(data)
                                output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                    except (OSError, EOFError) as exc:
                        raise BackupError("bloque comprimido remoto corrupto") from exc
                    if restored_size != block["size"] or hasher.hexdigest() != digest:
                        raise BackupError("bloque restaurado distinto del hash")
                else:
                    if encrypted_plain.stat().st_size != block["size"] or _hash_path(encrypted_plain) != digest:
                        raise BackupError("bloque restaurado distinto del hash")
                    os.replace(encrypted_plain, cached_object)
                encrypted_plain.unlink(missing_ok=True)
                os.chmod(cached_object, 0o600)
                cached[digest] = cached_object

            for item in files:
                output = staging.joinpath(*PurePosixPath(item["path"]).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with output.open("xb") as assembled:
                    for block in file_blocks[item["path"]]:
                        source = cached[block["sha256"]]
                        with source.open("rb") as block_stream:
                            while data := block_stream.read(CHUNK_SIZE):
                                assembled.write(data)
                                digest.update(data)
                                size += len(data)
                    assembled.flush()
                    os.fsync(assembled.fileno())
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise BackupError("archivo restaurado distinto del manifiesto")
            # Comprueba la identidad del árbol completo antes de hacerlo visible.
            restored = inventory_version(staging)
            if restored["files"] != canonical_files:
                raise BackupError("el inventario restaurado no coincide")
            os.replace(staging, target)
        return _base_result("complete", "restauración completa validada", snapshot_id=snapshot_id,
                            bytes_count=sum(int(item["size"]) for item in files), at=now)
    except BackupBlocked as exc:
        return _base_result("blocked", str(exc), snapshot_id=snapshot_id, at=now)
    except Exception:  # noqa: BLE001 - no revelar paths ni errores del transporte
        return _base_result("failed", "restauración remota no validada", snapshot_id=snapshot_id, at=now)


def prune_remote(config_path: str | Path, state_dir: str | Path, *, s3_client: Any = None,
                 now: int | None = None, retention_seconds: int = 90 * 24 * 60 * 60) -> dict[str, Any]:
    """Expira solo versiones marcadas en catálogo y borra objetos ya inalcanzables."""
    now = int(time.time()) if now is None else int(now)
    state_dir = Path(state_dir).absolute()
    try:
        _secure_state_dir(state_dir)
        with _locked(state_dir / ".backup.lock"):
            return _prune_remote_unlocked(config_path, state_dir, s3_client=s3_client,
                                          now=now, retention_seconds=retention_seconds)
    except BackupBlocked as exc:
        return {"status": "blocked", "reason": str(exc), "removed_versions": 0, "removed_objects": 0}


def _prune_remote_unlocked(config_path: str | Path, state_dir: Path, *, s3_client: Any, now: int,
                           retention_seconds: int) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    try:
        config = _load_config(config_path)
        evidence = _check_usage_margin(config, now)
        # Deletes currently have no charge, but constructing the same validated
        # ledger keeps retention from bypassing a broken/stale account budget.
        _Budget(state_dir / "monthly-budget.json", config, evidence, now)
        client = s3_client if s3_client is not None else _new_s3_client(config)
        catalog_path = state_dir / "remote-catalog.json"
        if not catalog_path.is_file() or catalog_path.is_symlink():
            raise BackupBlocked("catálogo local remoto ausente; no se ejecuta retención")
        catalog = _RemoteCatalog(catalog_path, config["prefix"])
        protected_path = state_dir / "remote-protected.json"
        if protected_path.is_symlink():
            raise BackupBlocked("pins remotos ilegibles; no se ejecuta retención")
        try:
            protected_state = json.loads(protected_path.read_text())
        except FileNotFoundError:
            raise BackupBlocked("head remoto desconocido; no se ejecuta retención")
        if (not isinstance(protected_state, dict) or
                not isinstance(protected_state.get("pending", []), list)):
            raise BackupBlocked("pins remotos ilegibles; no se ejecuta retención")
        local_root = state_dir / "local"
        local_protected_path = local_root / "protected.json"
        if local_root.is_symlink() or local_protected_path.is_symlink():
            raise BackupBlocked("pins locales ilegibles; no se ejecuta retención")
        try:
            local_state = json.loads(local_protected_path.read_text())
        except FileNotFoundError:
            local_state = {}
        if (not isinstance(local_state, dict) or
                not isinstance(local_state.get("pending", []), list)):
            raise BackupBlocked("pins locales ilegibles; no se ejecuta retención")
        pin_states = (protected_state, local_state)
        protected: set[str] = set()
        for pin_state in pin_states:
            for value in (pin_state.get("current"), pin_state.get("previous"),
                          *pin_state.get("pending", [])):
                if value is None:
                    continue
                if not isinstance(value, str) or not SAFE_VERSION_RE.fullmatch(value) or value in {".", ".."}:
                    raise BackupBlocked("pins de versión no válidos; no se ejecuta retención")
                protected.add(value)
        head = protected_state.get("current")
        if (not isinstance(head, str) or not SAFE_VERSION_RE.fullmatch(head) or
                head not in catalog.data["versions"]):
            raise BackupBlocked("head remoto desconocido; no se ejecuta retención")
        expired = [sid for sid, version in catalog.data["versions"].items()
                   if sid not in protected and int(version["created_at"]) < now - retention_seconds]
        removed_objects = 0
        for sid in expired:
            client.delete_object(Bucket=config["bucket"], Key=catalog.manifest_key(sid))
            del catalog.data["versions"][sid]
        reachable = {digest for version in catalog.data["versions"].values() for digest in version["hashes"]}
        for digest, obj in list(catalog.data["objects"].items()):
            if digest not in reachable and obj.get("key") == catalog.object_key(digest):
                client.delete_object(Bucket=config["bucket"], Key=obj["key"])
                del catalog.data["objects"][digest]
                removed_objects += 1
        catalog.save()
        return {"status": "complete", "removed_versions": len(expired), "removed_objects": removed_objects}
    except BackupBlocked as exc:
        return {"status": "blocked", "reason": str(exc), "removed_versions": 0, "removed_objects": 0}
    except Exception:  # noqa: BLE001 - no revelar paths ni errores del transporte
        return {"status": "failed", "reason": "no se completó la retención remota",
                "removed_versions": 0, "removed_objects": 0}


def protect_remote_versions(state_dir: str | Path, *, current: str | None, previous: str | None,
                            pending: list[str] | None = None) -> None:
    """Persiste los pins que `prune_remote` respeta sin consultar el bucket."""
    values = [x for x in (current, previous, *(pending or [])) if x is not None]
    if any(not SAFE_VERSION_RE.fullmatch(x) or x in {".", ".."} for x in values):
        raise UnsafePathError("identificador remoto no válido")
    root = Path(state_dir).absolute()
    _secure_state_dir(root)
    with _locked(root / ".backup.lock"):
        _atomic_write(root / "remote-protected.json", _canonical_json({
            "current": current,
            "previous": previous,
            "pending": sorted(set(pending or [])),
        }))


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("init-key")
    key.add_argument("key_file")
    catalog = sub.add_parser("init-catalog")
    catalog.add_argument("--state-dir", required=True)
    catalog.add_argument("--prefix", default="rag-backup/v1")
    backup = sub.add_parser("backup")
    backup.add_argument("version_dir")
    backup.add_argument("--config", required=True)
    backup.add_argument("--state-dir", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("snapshot_id")
    restore.add_argument("destination")
    restore.add_argument("--config", required=True)
    restore.add_argument("--state-dir", required=True)
    prune = sub.add_parser("prune")
    prune.add_argument("--config", required=True)
    prune.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    if args.command == "init-key":
        generate_recovery_key(args.key_file)
        print(json.dumps({"status": "complete", "reason": "clave creada; conserve copia externa antes de activar"}))
        return 0
    if args.command == "init-catalog":
        initialize_remote_catalog(args.state_dir, args.prefix)
        print(json.dumps({"status": "complete", "reason": "catálogo local inicializado"}))
        return 0
    if args.command == "backup":
        result = backup_version(args.version_dir, args.config, args.state_dir)
    elif args.command == "restore":
        result = restore_remote(args.snapshot_id, args.config, args.state_dir, args.destination)
    else:
        result = prune_remote(args.config, args.state_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(_main())
