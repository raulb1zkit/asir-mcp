from __future__ import annotations

import hashlib
import io
import json
import random
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.rag_backup import (
    BackupBlocked,
    _Budget,
    _canonical_json,
    _encrypt_stream,
    _load_config,
    backup_version,
    generate_recovery_key,
    initialize_remote_catalog,
    protect_remote_versions,
    restore_remote,
)


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[str] = []
        self.gets: list[str] = []
        self.deletes: list[str] = []
        self.fail_once: set[str] = set()
        self.fail_get_once: set[str] = set()

    def put_object(self, *, Bucket: str, Key: str, Body: Any, ContentLength: int, **kwargs: Any) -> dict:
        self.puts.append(Key)
        if Key in self.fail_once:
            self.fail_once.remove(Key)
            raise OSError("simulated private failure")
        data = Body.read()
        assert len(data) == ContentLength
        self.objects[(Bucket, Key)] = data
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self.gets.append(Key)
        if Key in self.fail_get_once:
            self.fail_get_once.remove(Key)
            raise OSError("simulated interrupted GET")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        self.deletes.append(Key)
        self.objects.pop((Bucket, Key), None)
        return {}

    def head_object(self, **kwargs: Any) -> dict:
        raise AssertionError("head_object no debe ejecutarse por cada objeto")

    def list_objects_v2(self, **kwargs: Any) -> dict:
        raise AssertionError("no se lista el bucket para cada objeto")


def _usage(now: int, *, storage: int = 2_940_000, class_a: int = 0, class_b: int = 0,
           complete: bool = True, age: int = 0) -> dict:
    observed = datetime.fromtimestamp(now - age, UTC)
    return {
        "schema_version": 1,
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "storage_bytes": storage,
        "class_a_operations": class_a,
        "class_b_operations": class_b,
        "period_start": (observed - timedelta(days=31)).isoformat().replace("+00:00", "Z"),
        "period_end": observed.isoformat().replace("+00:00", "Z"),
        "complete": complete,
    }


def _setup(tmp_path: Path, *, now: int | None = None, evidence: dict | None = None,
           **overrides: Any) -> tuple[Path, Path, Path]:
    now = int(time.time()) if now is None else now
    key = generate_recovery_key(tmp_path / "recovery.key")
    usage_file = tmp_path / "account-usage.json"
    usage_file.write_text(json.dumps(evidence or _usage(now)))
    usage_file.chmod(0o600)
    config_file = tmp_path / "backup.json"
    config = {
        "endpoint_url": "https://example.invalid",
        "access_key_id": "synthetic-key",
        "secret_access_key": "synthetic-secret",
        "bucket": "synthetic-private",
        "key_file": str(key),
        "account_usage_file": str(usage_file),
        "max_age_seconds": 300,
        "retries": 0,
        "reserve_bytes": 0,
        "monthly_class_a_cap": 10_000,
        "monthly_class_b_cap": 100_000,
    }
    config.update(overrides)
    config_file.write_text(json.dumps(config))
    config_file.chmod(0o600)
    return config_file, usage_file, key


def _version(tmp_path: Path, name: str = "v1", *, duplicate: bool = False) -> Path:
    version = tmp_path / name
    version.mkdir()
    payload = b"synthetic educational fixture; not real corpus\n" * 1200
    (version / "chunks.jsonl").write_bytes(payload)
    (version / "report.json").write_text('{"source":"synthetic"}\n')
    if duplicate:
        (version / "duplicate.bin").write_bytes(payload)
    return version


def _backup(version: Path, config_path: Path, state_dir: Path, client: MemoryS3, now: int) -> dict:
    catalog = state_dir / "remote-catalog.json"
    if not catalog.exists():
        prefix = json.loads(config_path.read_text()).get("prefix", "rag-backup/v1")
        initialize_remote_catalog(state_dir, prefix)
    return backup_version(version, config_path, state_dir, s3_client=client, now=now)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)

    def test_encrypted_incremental_backup_idempotence_and_shared_object_restore(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now)
        version = _version(tmp_path, duplicate=True)
        client = MemoryS3()
        state = tmp_path / "state"

        result = _backup(version, config, state, client, now)
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertEqual(len(client.puts), 3)  # two distinct payloads plus one encrypted commit manifest
        self.assertTrue(all(b"synthetic educational fixture" not in body for body in client.objects.values()))
        self.assertTrue(all(body.startswith(b"RAGAEAD1") for body in client.objects.values()))
        catalog = json.loads((state / "remote-catalog.json").read_text())
        self.assertIn("gzip", {obj["encoding"] for obj in catalog["objects"].values()})
        protected = json.loads((state / "local" / "protected.json").read_text())
        self.assertEqual(protected["pending"], [])

        put_count = len(client.puts)
        repeated = _backup(version, config, state, client, now)
        self.assertEqual(repeated["status"], "complete")
        self.assertEqual(len(client.puts), put_count)

        # A disaster-recovery checkout has the private key and a version ID, but no
        # local remote catalog. Restore must need no account/bucket listing.
        (state / "remote-catalog.json").unlink()
        restored = tmp_path / "restored"
        recovery = restore_remote("v1", config, state, restored, s3_client=client, now=now)
        self.assertEqual(recovery["status"], "complete")
        self.assertEqual((restored / "chunks.jsonl").read_bytes(), (version / "chunks.jsonl").read_bytes())
        self.assertEqual((restored / "duplicate.bin").read_bytes(), (version / "duplicate.bin").read_bytes())
        object_gets = [key for key in client.gets if "/objects/" in key]
        self.assertEqual(len(object_gets), 2)  # one GET per unique digest, despite a shared logical object

    def test_content_defined_blocks_reuse_unchanged_regions_after_insert(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now)
        first = tmp_path / "v1"
        first.mkdir()
        original = random.Random(90317).randbytes(3 * 1024 * 1024)
        (first / "shard.jsonl").write_bytes(original)
        client = MemoryS3()
        state = tmp_path / "state"

        self.assertEqual(_backup(first, config, state, client, now)["status"], "complete")
        first_object_puts = len([key for key in client.puts if "/objects/" in key])
        self.assertGreaterEqual(first_object_puts, 6)

        second = tmp_path / "v2"
        second.mkdir()
        updated = original[:1_250_000] + b'{"inserted":"synthetic"}\n' + original[1_250_000:]
        (second / "shard.jsonl").write_bytes(updated)
        puts_before = len([key for key in client.puts if "/objects/" in key])
        result = _backup(second, config, state, client, now)
        new_object_puts = len([key for key in client.puts if "/objects/" in key]) - puts_before
        self.assertEqual(result["status"], "complete")
        self.assertTrue(0 < new_object_puts < first_object_puts // 2)

        restored = tmp_path / "restored-v2"
        self.assertEqual(
            restore_remote("v2", config, state, restored, s3_client=client, now=now)["status"], "complete"
        )
        self.assertEqual((restored / "shard.jsonl").read_bytes(), updated)

    def test_account_usage_missing_stale_or_incomplete_fails_closed_without_put(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        version = _version(tmp_path)
        client = MemoryS3()
        config, usage, _ = _setup(tmp_path, now=now)
        usage.write_text(json.dumps(_usage(now, age=301)))
        blocked = _backup(version, config, tmp_path / "state", client, now)
        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse(client.puts)
        self.assertTrue((tmp_path / "state" / "local" / "snapshots" / "v1.json").exists())
        self.assertIn("v1", json.loads((tmp_path / "state" / "local" / "protected.json").read_text())["pending"])

        usage.write_text(json.dumps(_usage(now, complete=False)))
        incomplete = _backup(version, config, tmp_path / "state2", client, now)
        self.assertEqual(incomplete["status"], "blocked")
        self.assertFalse(client.puts)

        usage.unlink()
        missing = _backup(version, config, tmp_path / "state3", client, now)
        self.assertEqual(missing["status"], "blocked")
        self.assertFalse(client.puts)

    def test_budget_exhaustion_is_preflighted_before_any_put(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now, monthly_class_a_cap=1)
        version = _version(tmp_path)
        client = MemoryS3()
        result = _backup(version, config, tmp_path / "state", client, now)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(client.puts)

    def test_missing_remote_catalog_blocks_mutation_and_pruning(self) -> None:
        from scripts.rag_backup import prune_remote

        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now)
        version = _version(tmp_path)
        client = MemoryS3()
        state = tmp_path / "state"
        result = backup_version(version, config, state, s3_client=client, now=now)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(client.puts)
        self.assertEqual(prune_remote(config, state, s3_client=client, now=now)["status"], "blocked")

    def test_remote_prune_requires_a_known_protected_head(self) -> None:
        from scripts.rag_backup import prune_remote

        tmp_path = self.tmp_path
        now = int(time.time())
        config_path, _, _ = _setup(tmp_path, now=now)
        state = tmp_path / "state"
        initialize_remote_catalog(state)
        client = MemoryS3()

        result = prune_remote(config_path, state, s3_client=client, now=now)

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(client.deletes)

    def test_s3_client_uses_bounded_connect_and_read_timeouts_without_network(self) -> None:
        from unittest.mock import patch
        from scripts.rag_backup import _load_config, _new_s3_client

        config_path, _, _ = _setup(self.tmp_path)
        config = _load_config(config_path)
        with patch("boto3.client", return_value=object()) as make_client:
            _new_s3_client(config)
        client_config = make_client.call_args.kwargs["config"]
        self.assertEqual(client_config.connect_timeout, 10)
        self.assertEqual(client_config.read_timeout, 30)

    def test_account_storage_or_operation_margin_blocks_without_put(self) -> None:
        for evidence in ({"storage": 9_999_999_900}, {"class_a": 900_000}):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                now = int(time.time())
                config, _, _ = _setup(tmp_path, now=now, evidence=_usage(now, **evidence))
                version = _version(tmp_path)
                client = MemoryS3()
                result = _backup(version, config, tmp_path / "state", client, now)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(client.puts)

    def test_put_retry_is_charged_and_failure_before_manifest_is_not_complete(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now, retries=1)
        version = _version(tmp_path)
        client = MemoryS3()
        state = tmp_path / "state"
        prefix = "rag-backup/v1"
        object_key = f"{prefix}/objects/sha256/{hashlib.sha256((version / 'chunks.jsonl').read_bytes()).hexdigest()}"
        client.fail_once.add(object_key)
        result = _backup(version, config, state, client, now)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.puts.count(object_key), 2)
        self.assertEqual(result["operations"], 4)

        # New snapshot reuses both CAS objects, but its final manifest fails. No
        # version is catalogued complete and a retry can safely commit the manifest.
        config_data = json.loads(config.read_text())
        config_data["retries"] = 0
        config.write_text(json.dumps(config_data))
        config.chmod(0o600)
        version2 = _version(tmp_path, "v2")
        manifest_key = f"{prefix}/manifests/v2.enc"
        client.fail_once.add(manifest_key)
        pending = _backup(version2, config, state, client, now)
        self.assertEqual(pending["status"], "pending")
        catalog = json.loads((state / "remote-catalog.json").read_text())
        self.assertNotIn("v2", catalog["versions"])
        self.assertNotIn(("synthetic-private", manifest_key), client.objects)
        complete = _backup(version2, config, state, client, now)
        self.assertEqual(complete["status"], "complete")

    def test_corrupt_ciphertext_never_creates_a_restored_tree(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now)
        version = _version(tmp_path)
        client = MemoryS3()
        state = tmp_path / "state"
        self.assertEqual(_backup(version, config, state, client, now)["status"], "complete")
        object_key = next(key for bucket, key in client.objects if "/objects/" in key)
        corrupted = bytearray(client.objects[("synthetic-private", object_key)])
        corrupted[-1] ^= 0x01
        client.objects[("synthetic-private", object_key)] = bytes(corrupted)

        destination = tmp_path / "bad-restore"
        result = restore_remote("v1", config, state, destination, s3_client=client, now=now)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(destination.exists())

    def test_restore_retries_interrupted_get_and_reserves_each_operation(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config, _, _ = _setup(tmp_path, now=now, retries=1)
        version = _version(tmp_path)
        client = MemoryS3()
        state = tmp_path / "state"
        self.assertEqual(_backup(version, config, state, client, now)["status"], "complete")
        object_key = next(key for bucket, key in client.objects if "/objects/" in key)
        client.fail_get_once.add(object_key)

        result = restore_remote("v1", config, state, tmp_path / "restored", s3_client=client, now=now)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.gets.count(object_key), 2)
        journal = (state / "operation-reservations.jsonl").read_text().splitlines()
        self.assertGreaterEqual(sum(json.loads(row)["class"] == "B" for row in journal), 4)

    def test_unknown_operation_class_is_reserved_against_both_budgets(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config_path, _usage_path, _ = _setup(
            tmp_path, now=now, monthly_class_a_cap=1, monthly_class_b_cap=1
        )
        config = _load_config(config_path)
        from scripts.rag_backup import _check_usage_margin

        evidence = _check_usage_margin(config, now)
        state = tmp_path / "state"
        state.mkdir()
        budget = _Budget(state / "monthly.json", config, evidence, now)
        budget.reserve(None)
        reloaded = _Budget(state / "monthly.json", config, evidence, now)
        with self.assertRaises(BackupBlocked):
            reloaded.ensure_capacity({"A": 1})
        with self.assertRaises(BackupBlocked):
            reloaded.ensure_capacity({"B": 1})

    def test_bad_remote_manifest_path_is_rejected_without_traversal(self) -> None:
        from scripts.rag_backup import _load_key

        tmp_path = self.tmp_path
        now = int(time.time())
        config_path, _, key_path = _setup(tmp_path, now=now)
        config = _load_config(config_path)
        snapshot_id = "bad"
        data = b"synthetic"
        manifest = {
            "format": 1,
            "snapshot_id": snapshot_id,
            "created_at": now,
            "inventory_sha256": "irrelevant",
            "files": [{"path": "../escaped.txt", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}],
        }
        ciphertext = io.BytesIO()
        _encrypt_stream(io.BytesIO(_canonical_json(manifest)), ciphertext, _load_key(key_path),
                        b"manifest:" + snapshot_id.encode())
        client = MemoryS3()
        client.objects[(config["bucket"], f"{config['prefix']}/manifests/{snapshot_id}.enc")] = ciphertext.getvalue()
        result = restore_remote(snapshot_id, config_path, tmp_path / "state", tmp_path / "restore",
                                s3_client=client, now=now)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((tmp_path / "escaped.txt").exists())
        self.assertFalse((tmp_path / "restore").exists())

    def test_catalog_collision_is_blocked_before_remote_mutation(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        config_path, _, _ = _setup(tmp_path, now=now)
        config = _load_config(config_path)
        version = _version(tmp_path)
        state = tmp_path / "state"
        (state / "local").mkdir(parents=True, mode=0o700)
        state.chmod(0o700)
        digest = hashlib.sha256((version / "chunks.jsonl").read_bytes()).hexdigest()
        catalog = {
            "format": 1,
            "prefix": config["prefix"],
            "objects": {digest: {"key": "another-owner/object", "plain_size": (version / "chunks.jsonl").stat().st_size}},
            "versions": {},
        }
        (state / "remote-catalog.json").write_text(json.dumps(catalog))
        (state / "remote-catalog.json").chmod(0o600)
        client = MemoryS3()
        result = _backup(version, config_path, state, client, now)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(client.puts)

    def test_remote_retention_only_deletes_expired_owned_versions(self) -> None:
        from scripts.rag_backup import prune_remote

        tmp_path = self.tmp_path
        now = int(time.time())
        config_path, _, _ = _setup(tmp_path, now=now)
        config = _load_config(config_path)
        protected = tmp_path / "state"
        protected.mkdir(mode=0o700)
        protect_remote_versions(protected, current="current", previous="previous", pending=["pending"])
        self.assertEqual(json.loads((protected / "remote-protected.json").read_text()), {
            "current": "current", "previous": "previous", "pending": ["pending"]
        })
        shared = "1" * 64
        expired_only = "2" * 64

        def key(digest: str) -> str:
            return f"{config['prefix']}/objects/sha256/{digest}"

        catalog = {
            "format": 1,
            "prefix": config["prefix"],
            "objects": {
                shared: {"key": key(shared), "plain_size": 10, "encrypted_size": 50, "accounted_size": 100},
                expired_only: {"key": key(expired_only), "plain_size": 5, "encrypted_size": 45, "accounted_size": 95},
            },
            "versions": {
                "current": {"key": f"{config['prefix']}/manifests/current.enc", "created_at": now,
                            "hashes": [shared], "manifest_encrypted_size": 100},
                "previous": {"key": f"{config['prefix']}/manifests/previous.enc",
                             "created_at": now - 10, "hashes": [], "manifest_encrypted_size": 100},
                "pending": {"key": f"{config['prefix']}/manifests/pending.enc",
                            "created_at": now - 100 * 86400, "hashes": [], "manifest_encrypted_size": 100},
                "expired": {"key": f"{config['prefix']}/manifests/expired.enc",
                            "created_at": now - 100 * 86400, "hashes": [shared, expired_only],
                            "manifest_encrypted_size": 100},
            },
        }
        (protected / "remote-catalog.json").write_text(json.dumps(catalog))
        (protected / "remote-catalog.json").chmod(0o600)
        client = MemoryS3()
        result = prune_remote(config_path, protected, s3_client=client, now=now)
        self.assertEqual(result, {"status": "complete", "removed_versions": 1, "removed_objects": 1})
        self.assertEqual(client.deletes, [f"{config['prefix']}/manifests/expired.enc", key(expired_only)])
        after = json.loads((protected / "remote-catalog.json").read_text())
        self.assertEqual(set(after["versions"]), {"current", "previous", "pending"})
        self.assertEqual(set(after["objects"]), {shared})


if __name__ == "__main__":
    unittest.main()
