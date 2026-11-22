#!/usr/bin/env python3
"""Benchmark sintético local del empaquetado/backup a escala de VPS.

No usa red ni corpus real. El cliente S3 consume el stream y descarta los bytes.
Ejecución recomendada: `/usr/bin/time -v python tests/benchmark_rag_backup_scale.py`.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.rag_backup import generate_recovery_key
from scripts.rag_version_backup import backup_version, prepare_remote
from scripts.rag_versions import build_manifest, canonical, embedding_prefix

CHUNKS = 13_000
VECTORS = 27_000
DIMENSIONS = 384


class StreamingS3:
    """Doble local que consume cada PUT sin mantener el objeto en RAM."""

    def __init__(self) -> None:
        self.puts = 0
        self.bytes = 0

    def list_objects_v2(self, **_kwargs):
        return {"KeyCount": 0, "IsTruncated": False}

    def put_object(self, *, Body, ContentLength, **_kwargs):
        size = 0
        while block := Body.read(1024 * 1024):
            size += len(block)
        if size != ContentLength:
            raise AssertionError("longitud de fixture inesperada")
        self.puts += 1
        self.bytes += size


def synthetic_manifest():
    chunks = []
    windows = []
    vectors = []
    vector = [0.0] * DIMENSIONS
    for number in range(CHUNKS):
        text = f"SYNTHETIC-{number:05d} " + ("synthetic fixture only. " * 420)
        chunk_hash = hashlib.sha256(text.encode()).hexdigest()
        source_hash = hashlib.sha256(f"synthetic-source-{number}".encode()).hexdigest()
        source = {
            "sha256": source_hash,
            "subject": "SYNTHETIC",
            "section": "Fixture",
            "title": f"Fixture {number:05d}",
            "filename": f"fixture-{number:05d}.txt",
            "url": f"https://example.invalid/synthetic/{number}",
            "mime": "text/plain",
            "path": f"synthetic/{number:05d}",
            "moodle_active": True,
            "resource_id": f"external:synthetic-{number:05d}",
        }
        chunk = {
            "id": "chunk:" + chunk_hash,
            "text": text,
            "text_sha256": chunk_hash,
            "characters": len(text),
            "warnings": [],
            "requires_review": False,
            "schema_version": 1,
            "citations": [{
                "source": source,
                "method": "native",
                "segments": [{
                    "source_unit_id": f"unit:{number}",
                    "locator": {"page": 1},
                    "start": 0,
                    "end": len(text),
                    "method": "native",
                    "quality": "native",
                }],
            }],
        }
        chunks.append(chunk)
        window_count = 3 if number < 1_000 else 2
        spans = [
            (len(text) * index // window_count, len(text) * (index + 1) // window_count)
            for index in range(window_count)
        ]
        for start, end in spans:
            window = {
                "chunk_id": chunk["id"],
                "char_start": start,
                "char_end": end,
                "tokens": 256,
            }
            window["input_sha256"] = hashlib.sha256(
                (embedding_prefix(chunk, "stable-subject-section-title-v2") +
                 text[start:end]).encode()
            ).hexdigest()
            windows.append(window)
            vectors.append(vector)
    if len(vectors) != VECTORS:
        raise AssertionError("cantidad de vectores sintéticos inesperada")

    return build_manifest({
        "chunks": chunks,
        "vectors": vectors,
        "metadata": {
            "schema_version": 1,
            "corpus_sha256": "c" * 64,
            "model_fingerprint": "d" * 64,
            "model_repository": "synthetic",
            "model_revision": "fixture-v1",
            "dimensions": DIMENSIONS,
            "max_tokens": 512,
            "overlap_tokens": 64,
            "context": "stable-subject-section-title-v2",
            "vectors_sha256": "e" * 64,
            "summary": {},
            "windows": windows,
        },
        "report": {
            "schema_version": 1,
            "output_sha256": "c" * 64,
            "chunker_version": "synthetic-v1",
        },
    })


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rag-backup-scale-") as temp_dir:
        root = Path(temp_dir)
        now = int(time.time())
        now_utc = datetime.fromtimestamp(now, UTC)
        manifest_path = root / "manifest.json"
        manifest_path.write_bytes(canonical(synthetic_manifest()))

        key_path = generate_recovery_key(root / "recovery.key")
        usage_path = root / "account-usage.json"
        usage_path.write_text(json.dumps({
            "schema_version": 1,
            "observed_at": now_utc.isoformat(),
            "storage_bytes": 0,
            "class_a_operations": 0,
            "class_b_operations": 0,
            "period_start": (now_utc - timedelta(days=31)).isoformat(),
            "period_end": now_utc.isoformat(),
            "complete": True,
        }))
        usage_path.chmod(0o600)
        config_path = root / "backup.json"
        config_path.write_text(json.dumps({
            "endpoint_url": "https://example.invalid",
            "access_key_id": "synthetic-key",
            "secret_access_key": "synthetic-secret",
            "bucket": "synthetic-private",
            "key_file": str(key_path),
            "account_usage_file": str(usage_path),
            "max_age_seconds": 300,
            "retries": 0,
            # El override solo evita que el benchmark dependa de una reserva
            # local de 5 GiB; no cambia la política de la unidad de producción.
            "reserve_bytes": 0,
        }))
        config_path.chmod(0o600)

        state_dir = root / "state"
        state_dir.mkdir(mode=0o700)
        client = StreamingS3()
        prepare_remote(config_path, state_dir, client=client, now=now)
        result = backup_version(manifest_path, config_path, state_dir,
                                root / "bundles", client=client, now=now)
        if result.get("status") != "complete":
            raise RuntimeError("backup sintético no completado")

        print(json.dumps({
            "status": result["status"],
            "chunks": CHUNKS,
            "vectors": VECTORS,
            "dimensions": DIMENSIONS,
            "manifest_bytes": manifest_path.stat().st_size,
            "put_operations": client.puts,
            "bytes_streamed": client.bytes,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
