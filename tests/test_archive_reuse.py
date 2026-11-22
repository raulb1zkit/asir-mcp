import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import expand_archives
from scripts.expand_archives import cached_members, run


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_archive(root, name, members):
    """Crea un ZIP sintético dentro del manifiesto y devuelve su entrada Moodle."""
    root = root / "moodle"
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in members.items():
            archive.writestr(member, content)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"status": "downloaded", "url": "https://moodle.test/" + name, "title": "Material " + name, "section": "UD1", "filename": name, "path": name, "mime": "application/zip", "sha256": sha, "moodle_active": True}


def source_manifest(root, items):
    path = root / "moodle/manifest.json"
    write_json(path, {"complete": True, "issues": [], "course_url": "https://moodle.test/course", "subject": "IAW", "items": items})
    return path


def expanded_manifest(directory):
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def member_contents(directory, manifest):
    contents = {}
    for item in manifest["items"]:
        contents[item["archive_member"]] = (directory / item["path"]).read_bytes()
    return contents


class ArchiveReuseTests(unittest.TestCase):
    def test_cached_members_returns_verified_previous_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_archive(root, "source.zip", {"docs/readme.md": b"hello", "docs/notes.txt": b"notes"})
            manifest = source_manifest(root, [item])
            run(manifest, root / "previous")
            previous = root / "previous/manifest.json"
            cached = cached_members(previous, item)
            self.assertIsNotNone(cached)
            self.assertEqual(dict(cached), {"docs/notes.txt": b"notes", "docs/readme.md": b"hello"})

    def test_same_archive_is_reused_without_calling_archive_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_archive(root, "source.zip", {"docs/readme.md": b"hello", "docs/notes.txt": b"notes"})
            manifest = source_manifest(root, [item])
            run(manifest, root / "previous")
            expected = member_contents(root / "previous", expanded_manifest(root / "previous"))
            with patch("scripts.expand_archives.archive_members", side_effect=AssertionError("no debe expandir un archivo ya verificado")) as mocked:
                run(manifest, root / "current", reuse_manifest=root / "previous/manifest.json")
            mocked.assert_not_called()
            current = expanded_manifest(root / "current")
            self.assertEqual(current["summary"], {"reused_archives": 1, "new_archives": 0})
            self.assertEqual(member_contents(root / "current", current), expected)

    def test_reused_members_inherit_current_moodle_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_archive(root, "source.zip", {"docs/readme.md": b"hello"})
            run(source_manifest(root, [{**item, "title": "Título antiguo", "moodle_active": True}]), root / "previous")
            current = {**item, "title": "Título actual", "section": "UD2", "moodle_active": False}
            with patch("scripts.expand_archives.archive_members", side_effect=AssertionError("no debe expandir un archivo ya verificado")):
                run(source_manifest(root, [current]), root / "current", reuse_manifest=root / "previous/manifest.json")
            report = expanded_manifest(root / "current")
            self.assertEqual(len(report["items"]), 1)
            entry = report["items"][0]
            self.assertIs(entry["moodle_active"], False)
            self.assertEqual(entry["title"], "Título actual / docs/readme.md")
            self.assertEqual(entry["section"], "UD2")
            self.assertEqual(report["summary"], {"reused_archives": 1, "new_archives": 0})

    def test_new_hash_is_expanded_instead_of_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_archive(root, "first.zip", {"docs/first.md": b"first"})
            run(source_manifest(root, [first]), root / "previous")
            second = make_archive(root, "second.zip", {"docs/second.md": b"second"})
            original = expand_archives.archive_members
            with patch("scripts.expand_archives.archive_members", side_effect=original) as mocked:
                run(source_manifest(root, [second]), root / "current", reuse_manifest=root / "previous/manifest.json")
            self.assertEqual(mocked.call_count, 1)
            report = expanded_manifest(root / "current")
            self.assertEqual(report["summary"], {"reused_archives": 0, "new_archives": 1})
            self.assertEqual([item["archive_member"] for item in report["items"]], ["docs/second.md"])
            self.assertEqual(member_contents(root / "current", report), {"docs/second.md": b"second"})

    def test_corrupted_cached_object_forces_full_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_archive(root, "source.zip", {"docs/readme.md": b"hello", "docs/notes.txt": b"notes"})
            manifest = source_manifest(root, [item])
            run(manifest, root / "previous")
            previous = expanded_manifest(root / "previous")
            corrupted = root / "previous" / previous["items"][0]["path"]
            corrupted.write_bytes(b"corrupted cache")
            self.assertIsNone(cached_members(root / "previous/manifest.json", item))
            original = expand_archives.archive_members
            with patch("scripts.expand_archives.archive_members", side_effect=original) as mocked:
                run(manifest, root / "current", reuse_manifest=root / "previous/manifest.json")
            self.assertEqual(mocked.call_count, 1)
            report = expanded_manifest(root / "current")
            self.assertEqual(report["summary"], {"reused_archives": 0, "new_archives": 1})
            self.assertEqual(member_contents(root / "current", report), {"docs/notes.txt": b"notes", "docs/readme.md": b"hello"})

    def test_incomplete_previous_manifest_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = make_archive(root, "source.zip", {"docs/readme.md": b"hello"})
            manifest = source_manifest(root, [item])
            run(manifest, root / "previous")
            previous = json.loads((root / "previous/manifest.json").read_text(encoding="utf-8"))
            previous["complete"] = False
            write_json(root / "previous/manifest.json", previous)
            self.assertIsNone(cached_members(root / "previous/manifest.json", item))


if __name__ == "__main__":
    unittest.main()
