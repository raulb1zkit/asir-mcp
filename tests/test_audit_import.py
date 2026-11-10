import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_import import audit


class ImportAuditTests(unittest.TestCase):
    def test_valid_object_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"%PDF-test"
            (root / "object").write_bytes(data)
            manifest = {"complete": True, "issues": [], "items": [{"path": "object", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "mime": "application/pdf"}]}
            (root / "manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(audit([root / "manifest.json"], root / "report.json"), 0)
            (root / "object").write_bytes(b"wrong")
            self.assertEqual(audit([root / "manifest.json"], root / "report.json"), 1)
            issues = json.loads((root / "report.json").read_text())["issues"]
            self.assertIn("hash_or_size_mismatch", [issue["error"] for issue in issues])


if __name__ == "__main__":
    unittest.main()
