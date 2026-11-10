import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_derived import check


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DerivedAuditTests(unittest.TestCase):
    def test_detects_text_segment_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sha = "a" * 64
            unit = {"id": "unit", "kind": "text", "text": "hello", "locator": {"block": 1}}
            document = {"source": {"sha256": sha}, "units": [unit]}
            doc_path = root / "extracted/all/doc.json"
            doc_hash = save(doc_path, document)
            native_path = root / "extracted/all/report.json"
            native_hash = save(native_path, {"complete": True, "errors": [], "documents": [{"path": "doc.json", "document_sha256": doc_hash, "source_sha256": sha}]})
            save(root / "ocr/all/report.json", {"complete": True, "errors": [], "source_report_sha256": native_hash, "pages": []})
            chunk = {"text": "hello", "text_sha256": hashlib.sha256(b"hello").hexdigest(), "citations": [{"source": {"sha256": sha}, "method": "native", "segments": [{"source_unit_id": "unit", "locator": {"block": 1}, "start": 0, "end": 5}]}]}
            chunks = root / "chunks/chunks.jsonl"
            chunks.parent.mkdir()
            chunks.write_text(json.dumps(chunk) + "\n")
            save(root / "chunks/report.json", {"complete": True, "output_sha256": hashlib.sha256(chunks.read_bytes()).hexdigest()})
            check(root, root / "audit.json")
            chunk["citations"][0]["segments"][0]["end"] = 4
            chunks.write_text(json.dumps(chunk) + "\n")
            save(root / "chunks/report.json", {"complete": True, "output_sha256": hashlib.sha256(chunks.read_bytes()).hexdigest()})
            with self.assertRaisesRegex(ValueError, "segment_text_mismatch"):
                check(root, root / "audit.json")


if __name__ == "__main__":
    unittest.main()
