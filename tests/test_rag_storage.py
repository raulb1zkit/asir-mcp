from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.rag_storage import (
    InsufficientSpaceError,
    RAGStore,
    SpacePolicy,
    StorageError,
    UnsafePathError,
    disk_preflight,
    inventory_version,
)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)

    def test_snapshot_is_deduplicated_and_restores_exact_bytes(self) -> None:
        tmp_path = self.tmp_path
        version = tmp_path / "version"
        version.mkdir()
        content = (b"synthetic fixture only\n" * 200_000) + os.urandom(128)
        (version / "chunks.jsonl").write_bytes(content)
        (version / "copy.jsonl").write_bytes(content)
        (version / "nested").mkdir()
        (version / "nested" / "report.json").write_text('{"synthetic": true}\n')
        state = tmp_path / "state"
        store = RAGStore(state, space_policy=SpacePolicy(reserve_bytes=0))

        created = store.create_snapshot(version, "v1")
        again = store.create_snapshot(version, "v1")
        self.assertEqual(created["status"], "complete")
        self.assertFalse(created["reused"])
        self.assertTrue(again["reused"])
        self.assertEqual(len(list((state / "objects" / "sha256").glob("*/*.blob"))), 2)

        destination = tmp_path / "restored"
        result = store.restore("v1", destination)
        self.assertEqual(result["status"], "complete")
        self.assertEqual((destination / "chunks.jsonl").read_bytes(), content)
        self.assertEqual((destination / "copy.jsonl").read_bytes(), content)
        self.assertEqual((destination / "nested" / "report.json").read_text(), '{"synthetic": true}\n')

    def test_inventory_rejects_symlink_and_snapshot_traversal(self) -> None:
        tmp_path = self.tmp_path
        version = tmp_path / "version"
        version.mkdir()
        (version / "safe.txt").write_text("synthetic")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        (version / "escape.txt").symlink_to(outside)

        with self.assertRaises(UnsafePathError):
            inventory_version(version)
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        with self.assertRaises(UnsafePathError):
            store.create_snapshot(version, "../escape")

    def test_snapshot_immutable_and_protected_retention(self) -> None:
        tmp_path = self.tmp_path
        version = tmp_path / "version"
        version.mkdir()
        item = version / "chunks.jsonl"
        item.write_text("initial")
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(version, "active")
        store.protect(current="active", previous=None, pending=["active"])

        item.write_text("changed")
        with self.assertRaises(StorageError):
            store.create_snapshot(version, "active")
        result = store.prune(now=10_000_000, retention_seconds=1)
        self.assertEqual(result, {"removed_snapshots": 0, "removed_objects": 0})
        self.assertEqual(store.restore("active", tmp_path / "restored")["status"], "complete")

    def test_protect_requires_all_snapshot_objects_to_exist_and_match_hashes(self) -> None:
        version = self.tmp_path / "version"
        version.mkdir()
        (version / "data").write_bytes(b"synthetic protected snapshot")
        store = RAGStore(self.tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(version, "active")
        digest = store._read_manifest("active")["files"][0]["sha256"]
        store._object_path(digest).unlink()

        with self.assertRaisesRegex(StorageError, "falta un objeto requerido"):
            store.protect(current="active", previous=None)
        self.assertFalse(store.protected_file.exists())

    def test_local_expiry_uses_manifest_reachability(self) -> None:
        tmp_path = self.tmp_path
        now = int(time.time())
        old = tmp_path / "old"
        old.mkdir()
        shared = b"shared synthetic object"
        (old / "shared.jsonl").write_bytes(shared)
        (old / "old-only.jsonl").write_bytes(b"old-only synthetic")
        current = tmp_path / "current"
        current.mkdir()
        (current / "shared.jsonl").write_bytes(shared)
        (current / "active.jsonl").write_bytes(b"active synthetic")
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(old, "old")
        store.create_snapshot(current, "current")
        store.protect(current="current", previous=None)
        manifest_path = store._snapshot_file("old")
        manifest = json.loads(manifest_path.read_text())
        manifest["created_at"] = now - 31 * 24 * 60 * 60
        manifest_path.write_text(json.dumps(manifest))

        result = store.prune(now=now, retention_seconds=30 * 24 * 60 * 60)
        self.assertEqual(result, {"removed_snapshots": 1, "removed_objects": 1})
        self.assertFalse(store._snapshot_file("old").exists())
        self.assertTrue(store._snapshot_file("current").exists())
        current_restore = tmp_path / "current-restored"
        store.restore("current", current_restore)
        self.assertEqual((current_restore / "shared.jsonl").read_bytes(), shared)

    def test_historical_compression_keeps_active_exact_and_refuses_bad_restore(self) -> None:
        tmp_path = self.tmp_path
        state = tmp_path / "state"
        store = RAGStore(state, space_policy=SpacePolicy(reserve_bytes=0))
        old = tmp_path / "old"
        old.mkdir()
        old_bytes = b"synthetic prose fixture\n" * 200_000
        (old / "report.json").write_bytes(old_bytes)
        current = tmp_path / "current"
        current.mkdir()
        (current / "chunks.jsonl").write_bytes(b"active exact bytes")
        store.create_snapshot(old, "old")
        store.create_snapshot(current, "current")
        store.protect(current="current", previous=None)
        # A logical historical manifest is retained within 90 days; compression
        # removes only the raw CAS object after verifying the gzip round trip.
        result = store.compact_historical(now=store._read_manifest("old")["created_at"] + 100)
        self.assertEqual(result["compressed_objects"], 1)
        old_digest = hashlib.sha256(old_bytes).hexdigest()
        raw = store._object_path(old_digest)
        self.assertFalse(raw.exists())
        self.assertTrue(raw.with_suffix(".gz").exists())

        restored = tmp_path / "old-restored"
        self.assertEqual(store.restore("old", restored)["status"], "complete")
        self.assertEqual((restored / "report.json").read_bytes(), old_bytes)
        active = tmp_path / "current-restored"
        store.restore("current", active)
        self.assertEqual((active / "chunks.jsonl").read_bytes(), b"active exact bytes")

    def test_verify_snapshot_roundtrips_exact_source_and_refreshes_archive_retention(self) -> None:
        tmp_path = self.tmp_path
        source = tmp_path / "managed-bundle"
        source.mkdir()
        content = b"synthetic canonical bundle row\n" * 10_000
        (source / "bundle.json").write_bytes(content)
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(source, "managed")
        manifest_path = store._snapshot_file("managed")
        manifest = json.loads(manifest_path.read_text())
        manifest["created_at"] = 1
        manifest_path.write_text(json.dumps(manifest))

        result = store.verify_snapshot("managed", source, refresh_retention=True, now=1234)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(store._read_manifest("managed")["created_at"], 1234)
        store.compact_historical(now=1234)
        digest = store._read_manifest("managed")["files"][0]["sha256"]
        self.assertTrue(store._object_path(digest).with_suffix(".gz").exists())
        restored = tmp_path / "verified"
        store.restore("managed", restored)
        self.assertEqual((restored / "bundle.json").read_bytes(), content)

    def test_verify_snapshot_refuses_changed_source_and_symlink_escape(self) -> None:
        tmp_path = self.tmp_path
        source = tmp_path / "managed-bundle"
        source.mkdir()
        (source / "bundle.json").write_bytes(b"synthetic bundle")
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(source, "managed")
        (source / "bundle.json").write_bytes(b"changed")

        with self.assertRaises(StorageError):
            store.verify_snapshot("managed", source, refresh_retention=True, now=1234)
        self.assertNotEqual(store._read_manifest("managed")["created_at"], 1234)

        (source / "escape").symlink_to(tmp_path)
        with self.assertRaises(UnsafePathError):
            store.verify_snapshot("managed", source)

    def test_corrupt_compressed_object_is_not_reported_as_restored(self) -> None:
        tmp_path = self.tmp_path
        version = tmp_path / "version"
        version.mkdir()
        (version / "a").write_bytes(b"some compressible data\n" * 100)
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(version, "v")
        digest = store._read_manifest("v")["files"][0]["sha256"]
        raw = store._object_path(digest)
        raw.with_suffix(".gz").write_bytes(b"corrupt")
        raw.unlink()
        with self.assertRaises((StorageError, OSError)):
            store.restore("v", tmp_path / "restore")
        self.assertFalse((tmp_path / "restore").exists())

    def test_space_preflight_accounts_for_temporaries_and_reserve(self) -> None:
        tmp_path = self.tmp_path

        class Stats:
            f_frsize = 1
            f_blocks = 100
            f_bavail = 30

        with patch.object(os, "statvfs", lambda _: Stats()):
            blocked = disk_preflight(tmp_path, 10, SpacePolicy(block_percent=75, reserve_bytes=25))
            self.assertFalse(blocked["allowed"])  # 80% use after predicted temporary space
            self.assertTrue(blocked["warning"])
            with self.assertRaises(InsufficientSpaceError):
                version = tmp_path / "v"
                version.mkdir()
                (version / "item").write_bytes(b"x" * 10)
                RAGStore(
                    tmp_path / "state",
                    space_policy=SpacePolicy(block_percent=50, reserve_bytes=25),
                ).create_snapshot(version, "v")

    def test_restore_preflights_disk_before_creating_any_destination(self) -> None:
        tmp_path = self.tmp_path
        version = tmp_path / "version"
        version.mkdir()
        (version / "data").write_bytes(b"synthetic content")
        store = RAGStore(tmp_path / "state", space_policy=SpacePolicy(reserve_bytes=0))
        store.create_snapshot(version, "v")

        class NoSpace:
            f_frsize = 1
            f_blocks = 100
            f_bavail = 0

        destination = tmp_path / "restore"
        with patch.object(os, "statvfs", lambda _: NoSpace()):
            with self.assertRaises(InsufficientSpaceError):
                store.restore("v", destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
