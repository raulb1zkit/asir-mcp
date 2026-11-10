import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.merge_corpora import merge


class MergeTests(unittest.TestCase):
    def test_duplicate_text_preserves_citations_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sha = hashlib.sha256(b"shared text").hexdigest()
            for index in range(2):
                part = root / str(index)
                part.mkdir()
                chunk = {"id": "chunk:" + sha, "text_sha256": sha, "text": "shared text", "citations": [{"source": {"subject": str(index)}}], "warnings": [str(index)], "requires_review": bool(index)}
                payload = (json.dumps(chunk) + "\n").encode()
                (part / "chunks.jsonl").write_bytes(payload)
                (part / "report.json").write_text(json.dumps({"complete": True, "output_sha256": hashlib.sha256(payload).hexdigest()}))
            merge([root / "0", root / "1"], root / "out")
            result = json.loads((root / "out/chunks.jsonl").read_text())
            self.assertEqual(len(result["citations"]), 2)
            self.assertEqual(result["warnings"], ["0", "1"])
            self.assertTrue(result["requires_review"])


if __name__ == "__main__":
    unittest.main()
