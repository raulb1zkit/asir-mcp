from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.chunk_text import ExtractionFailure, group_records, run, supplemental_text


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def document(letter="a", text="echo --flag\n  next", units=None):
    sha = letter * 64
    if units is None:
        units = [{"id": sha[:16] + ":block-1", "kind": "paragraph", "locator": {"block": 1}, "text": text, "warnings": []}]
    return {"source": {"sha256": sha, "path": "objects/" + sha, "url": "https://example.test/" + letter, "title": letter, "section": "UD1", "mime": "text/plain", "filename": letter + ".txt"}, "source_aliases": [], "warnings": [], "units": units}


def fixture(root, docs, ocr_results=None):
    native = {"complete": True, "errors": [], "documents": []}
    for i, doc in enumerate(docs):
        path = root / "native" / f"{i}.json"
        write(path, doc)
        native["documents"].append({"path": path.name, "source_sha256": doc["source"]["sha256"], "document_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    native_path = root / "native/report.json"
    write(native_path, native)
    ocr = {"complete": True, "errors": [], "source_report_sha256": hashlib.sha256(native_path.read_bytes()).hexdigest(), "pages": []}
    for i, (doc, unit, text) in enumerate(ocr_results or []):
        data = {"source": doc["source"], "source_unit_id": unit["id"], "locator": unit["locator"], "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "method": "page-raster-ocr", "warnings": ["ocr_requires_review"]}
        path = root / "ocr" / f"{i}.json"
        write(path, data)
        ocr["pages"].append({"path": path.name, "source_sha256": doc["source"]["sha256"], "source_unit_id": unit["id"]})
    ocr_path = root / "ocr/report.json"
    write(ocr_path, ocr)
    return native_path, ocr_path


class ChunkTests(unittest.TestCase):
    def execute(self, native, ocr, output, **kwargs):
        with redirect_stdout(io.StringIO()):
            result = run(native, ocr, output, **kwargs)
        self.assertEqual(result, 0)
        return [json.loads(line) for line in (output / "chunks.jsonl").read_text().splitlines()]

    def test_whole_units_overlap_and_oversize_are_not_truncated(self):
        records = [{"kind": "paragraph", "text": c * 100} for c in "abc"]
        groups = list(group_records(records, 250, 120))
        self.assertEqual(groups, [records[:2], records[1:]])
        table = {"kind": "table", "text": "column\tvalue\n" * 100}
        self.assertEqual(list(group_records([table], 250, 120)), [[table]])

    def test_supplement_removes_only_exact_case_sensitive_whole_words(self):
        native = "terminal\nrm -rf /tmp/example\nTwo words"
        ocr = "terminal\nrm -rf /tmp/example\nTwo  words\nrm\nRM -rf /tmp/example\nchmod 600 config"
        self.assertEqual(supplemental_text(native, ocr), "RM -rf /tmp/example\nchmod 600 config")
        self.assertEqual(supplemental_text("terminal", "rm"), "rm")

    def test_native_and_ocr_stay_separate_with_exact_segments(self):
        unit = {"id": "a:page-1", "kind": "page", "locator": {"page": 1}, "text": "Native text", "image_count": 1, "warnings": ["images_not_transcribed"]}
        doc = document(units=[unit])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [doc], [(doc, unit, "Native text\nchmod 600 config")])
            chunks = self.execute(native, ocr, root / "out")
        self.assertEqual([c["text"] for c in chunks], ["Native text", "chmod 600 config"])
        self.assertFalse(chunks[0]["requires_review"])
        self.assertTrue(chunks[1]["requires_review"])
        segment = chunks[1]["citations"][0]["segments"][0]
        self.assertEqual(chunks[1]["text"][segment["start"]:segment["end"]], "chmod 600 config")
        self.assertEqual(segment["locator"], {"page": 1})
        self.assertEqual(chunks[0]["citations"][0]["source"]["subject"], "ASO")

    def test_exact_dedup_keeps_both_sources_and_no_self_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [document("a"), document("b")])
            chunks = self.execute(native, ocr, root / "out")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]["citations"]), 2)
        for citation in chunks[0]["citations"]:
            self.assertNotEqual(citation["next_chunk_id"], chunks[0]["id"])
            self.assertNotEqual(citation["previous_chunk_id"], chunks[0]["id"])

    def test_native_critical_warnings_propagate_review(self):
        doc = document()
        doc["warnings"] = ["tracked_changes_present"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [doc])
            chunks = self.execute(native, ocr, root / "out")
        self.assertTrue(chunks[0]["requires_review"])

    def test_stale_report_and_tampered_native_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [document()])
            report = json.loads(ocr.read_text())
            report["source_report_sha256"] = "0" * 64
            write(ocr, report)
            with self.assertRaisesRegex(ExtractionFailure, "stale"):
                run(native, ocr, root / "out")
            report["source_report_sha256"] = hashlib.sha256(native.read_bytes()).hexdigest()
            write(ocr, report)
            (native.parent / "0.json").write_text("{}")
            with self.assertRaisesRegex(ExtractionFailure, "native_document_hash"):
                run(native, ocr, root / "out")
            self.assertFalse((root / "out/chunks.jsonl").exists())

    def test_duplicate_or_tampered_ocr_results_fail(self):
        doc = document()
        unit = doc["units"][0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [doc], [(doc, unit, "extra")])
            report = json.loads(ocr.read_text())
            report["pages"].append(report["pages"][0])
            write(ocr, report)
            with self.assertRaisesRegex(ExtractionFailure, "duplicate_ocr"):
                run(native, ocr, root / "out")
            report["pages"].pop()
            write(ocr, report)
            data = json.loads((ocr.parent / "0.json").read_text())
            data["text"] = "tampered"
            write(ocr.parent / "0.json", data)
            with self.assertRaisesRegex(ExtractionFailure, "ocr_text_hash"):
                run(native, ocr, root / "out")

    def test_uncovered_images_prevent_complete_corpus(self):
        doc = document()
        doc["units"][0]["image_count"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [doc])
            with self.assertRaisesRegex(ExtractionFailure, "image_coverage_incomplete"):
                run(native, ocr, root / "out")

    def test_duplicate_visual_annotations_are_rejected(self):
        unit = {"id": "a:image-1", "kind": "image", "locator": {"block": 1}, "text": "", "image_member": "Pictures/a.png", "warnings": ["images_not_transcribed"]}
        doc = document(units=[unit])
        annotation = {"source_sha256": doc["source"]["sha256"], "source_unit_id": unit["id"], "image_member": unit["image_member"], "method": "visual_transcription", "text": "parent\n  child"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [doc])
            annotations = root / "visual.json"
            write(annotations, {"annotations": [annotation, annotation]})
            with self.assertRaisesRegex(ExtractionFailure, "duplicate_annotation"):
                run(native, ocr, root / "out", annotations_path=annotations)
            write(annotations, {"annotations": [annotation]})
            chunks = self.execute(native, ocr, root / "out", annotations_path=annotations)
            self.assertEqual(chunks[0]["text"], annotation["text"])
            self.assertTrue(chunks[0]["requires_review"])

    def test_oversize_table_is_preserved_with_warning(self):
        doc = document(text="head\tvalue\n" * 100)
        doc["units"][0]["kind"] = "table"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, ocr = fixture(root, [doc])
            chunks = self.execute(native, ocr, root / "out", target_chars=250, overlap_chars=100)
        self.assertEqual(chunks[0]["text"], doc["units"][0]["text"])
        self.assertIn("oversized_source_unit_preserved", chunks[0]["warnings"])


if __name__ == "__main__":
    unittest.main()
