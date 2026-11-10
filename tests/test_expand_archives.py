import io
import tarfile
from pathlib import Path
import tempfile
import unittest

from scripts.expand_archives import archive_members, member_mime


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


if __name__ == "__main__":
    unittest.main()
