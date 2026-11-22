import io
import hashlib
import json
import zipfile
import tarfile
from pathlib import Path
import tempfile
import unittest

from scripts.expand_archives import archive_members, member_mime, run


class ArchiveTests(unittest.TestCase):
    def test_unsafe_paths_and_secrets_are_rejected(self):
        for name in ("../secret.txt", "/etc/passwd.txt", "docs/.env.local", ".private/a.txt", "x/.git/config.txt"):
            self.assertIsNone(member_mime(name))
        self.assertEqual(member_mime("docs/readme.md"), "text/plain")

    def test_tar_reads_only_selected_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "test.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name, content in (("docs/readme.md", b"hello"), ("../escape.txt", b"private"), ("data/image.png", b"image")):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                symlink = tarfile.TarInfo("docs/link.txt")
                symlink.type = tarfile.SYMTYPE
                symlink.linkname = "../secret"
                archive.addfile(symlink)
            self.assertEqual(list(archive_members(archive_path, "application/g-zip")), [("docs/readme.md", b"hello")])

    def test_archive_members_inherit_parent_moodle_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "source.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("docs/readme.md", "hello")
            raw = archive_path.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"complete": True, "issues": [], "course_url": "https://moodle.test/course", "subject": "IAW", "items": [{"status": "downloaded", "url": "https://moodle.test/file.zip", "title": "Apuntes", "section": "UD1", "filename": "source.zip", "path": "source.zip", "mime": "application/zip", "sha256": sha, "moodle_active": False}]}))
            run(manifest, root / "expanded")
            expanded = json.loads((root / "expanded/manifest.json").read_text())
            self.assertEqual(len(expanded["items"]), 1)
            self.assertIs(expanded["items"][0]["moodle_active"], False)

    def test_identical_member_in_active_and_preserved_archive_keeps_active_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = []
            for name, version, active in (("old.zip", "v2", False), ("new.zip", "v3", True)):
                path = root / name
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("docs/readme.md", "hello")
                    archive.writestr("docs/version.txt", version)
                items.append({"status": "downloaded", "url": "https://moodle.test/file.zip", "title": "Apuntes", "section": "UD1", "filename": name, "path": name, "mime": "application/zip", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "moodle_active": active})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"complete": True, "issues": [], "course_url": "https://moodle.test/course", "subject": "IAW", "items": items}))
            run(manifest, root / "expanded")
            expanded = json.loads((root / "expanded/manifest.json").read_text())
            files = expanded["items"]
            readme = [item for item in files if item["archive_member"] == "docs/readme.md"]
            versions = [item for item in files if item["archive_member"] == "docs/version.txt"]
            self.assertEqual((len(readme), len(versions)), (1, 2))
            self.assertIs(readme[0]["moodle_active"], True)
            self.assertEqual(sorted(item["moodle_active"] for item in versions), [False, True])
            self.assertEqual([entry["members"] for entry in expanded["archives"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
