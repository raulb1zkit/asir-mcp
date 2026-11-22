from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import chunk_text
from scripts.audit_derived import check as audit_derived
from scripts.chunk_text import run


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def source(letter, **overrides):
    sha = letter * 64
    value = {"sha256": sha, "path": "objects/" + sha, "url": "https://example.test/" + letter, "title": letter.upper(), "section": "UD1", "mime": "text/plain", "filename": letter + ".txt", "moodle_active": True}
    value.update(overrides)
    return value


def document(letter, text, units=None, aliases=None, **overrides):
    sha = letter * 64
    if units is None:
        units = [{"id": sha[:16] + ":block-1", "kind": "paragraph", "locator": {"block": 1}, "text": text, "warnings": []}]
    return {"source": source(letter, **overrides), "source_aliases": aliases or [], "warnings": [], "units": units}


def fixture(root, documents, ocr_results=None, annotations=None):
    """Construye extracted/all, ocr/all y anotaciones en el layout que audita audit_derived."""
    native_dir = root / "extracted/all"
    ocr_dir = root / "ocr/all"
    native = {"complete": True, "errors": [], "documents": []}
    for index, item in enumerate(documents):
        path = native_dir / f"{index}.json"
        write(path, item)
        native["documents"].append({"path": path.name, "source_sha256": item["source"]["sha256"], "document_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    native_path = native_dir / "report.json"
    write(native_path, native)
    ocr = {"complete": True, "errors": [], "source_report_sha256": hashlib.sha256(native_path.read_bytes()).hexdigest(), "pages": []}
    for index, (item, unit, text) in enumerate(ocr_results or []):
        value = {"source": item["source"], "source_unit_id": unit["id"], "locator": unit["locator"], "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "method": "page-raster-ocr", "warnings": ["ocr_requires_review"]}
        path = ocr_dir / f"{index}.json"
        write(path, value)
        ocr["pages"].append({"path": path.name, "source_sha256": item["source"]["sha256"], "source_unit_id": unit["id"]})
    ocr_path = ocr_dir / "report.json"
    write(ocr_path, ocr)
    annotations_path = root / "annotations/visual.json"
    write(annotations_path, {"annotations": annotations or []})
    return native_path, ocr_path, annotations_path


def read_chunks(output):
    return [json.loads(line) for line in (output / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]


class ChunkReuseTests(unittest.TestCase):
    def execute(self, native, ocr, output, annotations=None, **kwargs):
        with redirect_stdout(io.StringIO()):
            result = run(native, ocr, output, annotations_path=annotations, **kwargs)
        self.assertEqual(result, 0)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        return read_chunks(output), report

    def test_unchanged_resume_reuses_without_chunking_and_stays_auditable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            page = {"id": "a" * 16 + ":page-1", "kind": "page", "locator": {"page": 1}, "text": "Native page", "image_count": 1, "warnings": []}
            first_doc = document("a", "", units=[page])
            second_doc = document("b", "beta content")
            native, ocr, annotations = fixture(root, [first_doc, second_doc], [(first_doc, page, "Native page\nextra command")])
            chunks, first_report = self.execute(native, ocr, output, annotations)
            self.assertEqual(first_report["summary"]["new_documents"], 2)
            self.assertEqual(first_report["summary"]["reused_documents"], 0)
            self.assertTrue(all(entry["sha256"] for entry in first_report["cache"].values()))
            with redirect_stdout(io.StringIO()):
                audit_derived(root, root / "audit.json")

            # Un informe antiguo cambia solo metadatos; el mismo output reanuda sin fragmentar.
            updated = [document("a", "", units=[page], title="Current title", path="objects/new"), document("b", "beta content", title="Other title")]
            native, ocr, annotations = fixture(root, updated, [(updated[0], page, "Native page\nextra command")])
            with patch("scripts.chunk_text.group_records", side_effect=AssertionError("unchanged source was chunked")), patch("scripts.chunk_text.chunk_group", side_effect=AssertionError("unchanged source was chunked")):
                resumed, report = self.execute(native, ocr, output, annotations)
            self.assertEqual(report["summary"]["reused_documents"], 2)
            self.assertEqual(report["summary"]["new_documents"], 0)
            self.assertEqual([chunk["id"] for chunk in resumed], [chunk["id"] for chunk in chunks])
            self.assertEqual(report["summary"]["coverage"], first_report["summary"]["coverage"])
            self.assertEqual([citation["source"]["title"] for chunk in resumed for citation in chunk["citations"]], ["Current title", "Current title", "Other title"])
            with redirect_stdout(io.StringIO()):
                audit_derived(root, root / "audit.json")
            audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["covered_text_units"], audit["expected_text_units"])

    def test_changed_document_rechunks_only_itself_and_metadata_is_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            alias = {"sha256": "a" * 64, "path": "objects/alias", "url": "https://example.test/old", "title": "Old alias", "section": "UD1", "mime": "text/plain", "filename": "old.txt", "moodle_active": False}
            first = [document("a", "alpha content", aliases=[alias]), document("b", "beta content")]
            native, ocr, annotations = fixture(root, first)
            self.execute(native, ocr, output, annotations)

            current_alias = {**alias, "title": "Current alias", "url": "https://example.test/new"}
            second = [document("a", "alpha content", aliases=[current_alias], title="Alpha updated", section="UD2", url="https://example.test/current", moodle_active=False), document("b", "beta changed")]
            native, ocr, annotations = fixture(root, second)
            calls = []
            original = chunk_text.chunk_group

            def record(document_value, *args):
                calls.append(document_value["source"]["sha256"])
                return original(document_value, *args)

            with patch("scripts.chunk_text.chunk_group", side_effect=record):
                chunks, report = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            self.assertEqual(calls, ["b" * 64])
            self.assertEqual(report["summary"]["reused_documents"], 1)
            self.assertEqual(report["summary"]["new_documents"], 1)
            alpha = next(chunk for chunk in chunks if chunk["text"] == "alpha content")
            self.assertEqual([citation["source"]["title"] for citation in alpha["citations"]], ["Alpha updated", "Current alias"])
            self.assertEqual([citation["source"]["moodle_active"] for citation in alpha["citations"]], [False, False])
            self.assertNotIn("Old alias", [citation["source"]["title"] for citation in alpha["citations"]])
            self.assertEqual(next(chunk["text"] for chunk in chunks if chunk["text"] == "beta changed"), "beta changed")

            # Otra reanudación sin alias elimina los alias anteriores del mismo chunk.
            third = [document("a", "alpha content", title="Alpha updated", section="UD2", url="https://example.test/current", moodle_active=False), document("b", "beta changed")]
            native, ocr, annotations = fixture(root, third)
            with patch("scripts.chunk_text.chunk_group", side_effect=AssertionError("unchanged source was chunked")):
                chunks, report = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            alpha = next(chunk for chunk in chunks if chunk["text"] == "alpha content")
            self.assertEqual(len(alpha["citations"]), 1)
            self.assertEqual(alpha["citations"][0]["source"]["title"], "Alpha updated")
            self.assertEqual(report["summary"]["reused_documents"], 2)

    def test_ocr_and_annotation_changes_invalidate_only_their_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            image_unit = {"id": "b" * 16 + ":image-1", "kind": "image", "locator": {"block": 1}, "text": "", "image_member": "Pictures/b.png", "warnings": ["images_not_transcribed"]}
            page_unit = {"id": "c" * 16 + ":page-1", "kind": "page", "locator": {"page": 1}, "text": "page content", "image_count": 1, "warnings": []}
            items = [document("a", "alpha content"), document("b", "", units=[image_unit]), document("c", "", units=[page_unit])]
            annotation = {"source_sha256": "b" * 64, "source_unit_id": image_unit["id"], "image_member": image_unit["image_member"], "method": "visual_transcription", "text": "diagram one"}
            native, ocr, annotations = fixture(root, items, [(items[2], page_unit, "page content\nextra one")], [annotation])
            self.execute(native, ocr, output, annotations)

            annotation["text"] = "diagram two"
            native, ocr, annotations = fixture(root, items, [(items[2], page_unit, "page content\nextra two")], [annotation])
            calls = []
            original = chunk_text.chunk_group

            def record(document_value, *args):
                calls.append(document_value["source"]["sha256"])
                return original(document_value, *args)

            with patch("scripts.chunk_text.chunk_group", side_effect=record):
                chunks, report = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            self.assertEqual(set(calls), {"b" * 64, "c" * 64})
            self.assertEqual(report["summary"]["reused_documents"], 1)
            self.assertEqual(report["summary"]["new_documents"], 2)
            texts = {chunk["text"] for chunk in chunks}
            self.assertIn("diagram two", texts)
            self.assertIn("extra two", texts)
            self.assertIn("alpha content", texts)

    def test_config_change_invalidates_all_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            native, ocr, annotations = fixture(root, [document("a", "alpha content"), document("b", "beta content")])
            self.execute(native, ocr, output, annotations)
            calls = []
            original = chunk_text.chunk_group

            def record(document_value, *args):
                calls.append(document_value["source"]["sha256"])
                return original(document_value, *args)

            with patch("scripts.chunk_text.chunk_group", side_effect=record):
                _, report = self.execute(native, ocr, output, annotations, target_chars=400, overlap_chars=40, reuse_report=output / "report.json")
            self.assertEqual(sorted(calls), ["a" * 64, "b" * 64])
            self.assertEqual(report["summary"]["reused_documents"], 0)
            self.assertEqual(report["summary"]["new_documents"], 2)

    def test_corrupt_cache_and_path_traversal_are_misses_and_are_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            native, ocr, annotations = fixture(root, [document("a", "alpha content"), document("b", "beta content")])
            chunks, first_report = self.execute(native, ocr, output, annotations)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            (output / report["cache"]["a" * 64]["path"]).write_text("{}", encoding="utf-8")
            outside = root / "outside-cache.json"
            outside.write_bytes((output / report["cache"]["b" * 64]["path"]).read_bytes())
            report["cache"]["b" * 64]["path"] = "../outside-cache.json"
            write(output / "report.json", report)

            calls = []
            original = chunk_text.chunk_group

            def record(document_value, *args):
                calls.append(document_value["source"]["sha256"])
                return original(document_value, *args)

            with patch("scripts.chunk_text.chunk_group", side_effect=record):
                rebuilt, report = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            self.assertEqual(sorted(calls), ["a" * 64, "b" * 64])
            self.assertEqual(report["summary"]["reused_documents"], 0)
            self.assertEqual(report["summary"]["new_documents"], 2)
            self.assertEqual([chunk["id"] for chunk in rebuilt], [chunk["id"] for chunk in chunks])
            self.assertTrue(outside.is_file())

            with patch("scripts.chunk_text.group_records", side_effect=AssertionError("rebuilt cache was not reused")):
                _, report = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            self.assertEqual(report["summary"]["reused_documents"], 2)

    def test_cache_content_tampered_with_rewritten_report_hash_is_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            native, ocr, annotations = fixture(root, [document("a", "alpha content"), document("b", "beta content")])
            self.execute(native, ocr, output, annotations)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            cache_path = output / report["cache"]["b" * 64]["path"]
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["chunks"][0]["text"] += " injected"
            cached["chunks"][0]["text_sha256"] = hashlib.sha256(cached["chunks"][0]["text"].encode()).hexdigest()
            cached["chunks"][0]["id"] = "chunk:" + cached["chunks"][0]["text_sha256"]
            cached["chunks"][0]["characters"] = len(cached["chunks"][0]["text"])
            cache_path.write_text(json.dumps(cached), encoding="utf-8")
            report["cache"]["b" * 64]["sha256"] = hashlib.sha256(cache_path.read_bytes()).hexdigest()
            write(output / "report.json", report)

            calls = []
            original = chunk_text.chunk_group

            def record(document_value, *args):
                calls.append(document_value["source"]["sha256"])
                return original(document_value, *args)

            with patch("scripts.chunk_text.chunk_group", side_effect=record):
                chunks, report = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            self.assertEqual(calls, ["b" * 64])
            self.assertEqual(report["summary"]["reused_documents"], 1)
            self.assertEqual(report["summary"]["new_documents"], 1)
            self.assertNotIn("injected", {chunk["text"] for chunk in chunks})

    def test_legacy_report_without_cache_recomputes_once_and_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            output = root / "chunks"
            native, ocr, annotations = fixture(root, [document("a", "alpha content"), document("b", "beta content")])
            self.execute(native, ocr, output, annotations)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            report.pop("cache")
            write(output / "report.json", report)

            calls = []
            original = chunk_text.chunk_group

            def record(document_value, *args):
                calls.append(document_value["source"]["sha256"])
                return original(document_value, *args)

            with patch("scripts.chunk_text.chunk_group", side_effect=record):
                _, fresh = self.execute(native, ocr, output, annotations, reuse_report=output / "report.json")
            self.assertEqual(sorted(calls), ["a" * 64, "b" * 64])
            self.assertEqual(fresh["summary"]["reused_documents"], 0)
            self.assertEqual(fresh["summary"]["new_documents"], 2)
            self.assertEqual(set(fresh["cache"]), {"a" * 64, "b" * 64})
            for entry in fresh["cache"].values():
                self.assertEqual(hashlib.sha256((output / entry["path"]).read_bytes()).hexdigest(), entry["sha256"])


if __name__ == "__main__":
    unittest.main()
