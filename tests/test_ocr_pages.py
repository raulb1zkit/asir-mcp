import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ocr_pages import ExtractionFailure, VERSION, parse_tsv, process, tasks_from_report, run, reuse_index, copy_cached

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
TSV = (HEADER + '5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t90\techo\n'
       + '5\t1\t1\t1\t1\t2\t45\t20\t30\t10\t100\t"\n'
       + '5\t1\t1\t1\t2\t1\t10\t40\t50\t10\t80\t--flag\n').encode()


class OCRTests(unittest.TestCase):
    def test_standalone_image_uses_original_without_pdf_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b"image bytes"
            sha = hashlib.sha256(raw).hexdigest()
            (root / "image").write_bytes(raw)
            source = {"mime": "image/png", "sha256": sha, "path": "image"}
            unit = {"id": "image:page-1", "kind": "page", "text": "", "image_count": 1, "locator": {"page": 1}, "warnings": ["needs_ocr"]}
            doc = {"source": source, "extractor": {"page_count": 1}, "units": [unit]}
            (root / "doc.json").write_text(json.dumps(doc))
            (root / "report.json").write_text(json.dumps({"complete": True, "documents": [{"path": "doc.json", "source_sha256": sha}]}))
            task = tasks_from_report(root / "report.json", root)[0]
            with patch("scripts.ocr_pages.call", return_value=TSV) as runner:
                result = process(task, root, {"dpi": 180, "languages": "eng", "psm": 11}, root)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(runner.call_args.args[0][0], "tesseract")
            self.assertEqual(json.loads((root / result["path"]).read_text())["locator"], {"page": 1})

    def test_image_scope_includes_mixed_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b"%PDF-test"
            sha = hashlib.sha256(raw).hexdigest()
            (root / "source.pdf").write_bytes(raw)
            unit = {"id": "mixed:page-1", "kind": "page", "text": "native text", "image_count": 1, "locator": {"page": 1}, "warnings": ["images_not_transcribed"]}
            doc = {"source": {"mime": "application/pdf", "sha256": sha, "path": "source.pdf"}, "extractor": {"page_count": 1}, "units": [unit]}
            (root / "doc.json").write_text(json.dumps(doc))
            (root / "report.json").write_text(json.dumps({"complete": True, "documents": [{"path": "doc.json", "source_sha256": sha}]}))
            self.assertEqual(tasks_from_report(root / "report.json", root), [])
            self.assertEqual(len(tasks_from_report(root / "report.json", root, "images")), 1)
            doc["units"].append({**unit, "id": "ambiguous:page-1"})
            (root / "doc.json").write_text(json.dumps(doc))
            with self.assertRaisesRegex(ExtractionFailure, "ambiguous_ocr_page"):
                tasks_from_report(root / "report.json", root, "images")
    def test_tsv_quotes_confidence_lines_and_boxes(self):
        text, words, mean = parse_tsv(TSV)
        self.assertEqual(text, 'echo "\n--flag')
        self.assertEqual(mean, 90)
        self.assertEqual(words[2]["left"], 10)
        self.assertEqual(words[2]["top"], 40)

    def test_plain_text_is_not_mistaken_for_empty_tsv(self):
        with self.assertRaisesRegex(ExtractionFailure, "tsv_header"):
            parse_tsv(b"A real OCR result without TSV format\n")

    def test_valid_empty_tsv_has_no_words(self):
        self.assertEqual(parse_tsv(HEADER.encode()), ("", [], 0))

    def test_cache_checks_config_and_output_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = {"sha256": "a" * 64, "title": "Synthetic test"}
            task = {"source": source, "page": 1, "original": root / "original.pdf", "unit_id": "source:page-1"}
            config = {"dpi": 180, "languages": "eng", "psm": 11, "format": "tsv"}
            def fake_call(args):
                if args[0] == "pdftoppm":
                    Path(args[-1]).with_suffix(".png").write_bytes(b"fake preview")
                    return b""
                return TSV
            with patch("scripts.ocr_pages.call", side_effect=fake_call) as runner:
                first = process(task, root, config, root)
                self.assertFalse(first["cached"])
                self.assertEqual(runner.call_count, 2)
                cached = process(task, root, config, root)
                self.assertTrue(cached["cached"])
                self.assertEqual(runner.call_count, 2)
                output = root / first["path"]
                document = json.loads(output.read_text())
                document["text"] = "tampered"
                output.write_text(json.dumps(document))
                self.assertFalse(process(task, root, config, root)["cached"])
                self.assertEqual(runner.call_count, 4)
                self.assertFalse(process(task, root, {**config, "dpi": 200}, root)["cached"])
                self.assertEqual(runner.call_count, 6)
                self.assertEqual(json.loads(output.read_text())["locator"], {"page": 1})

    def test_original_hash_and_page_locator_verified_before_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b"%PDF-test"
            sha = hashlib.sha256(original).hexdigest()
            (root / "source.pdf").write_bytes(original)
            source = {"mime": "application/pdf", "path": "source.pdf", "sha256": sha}
            unit = {"id": "source:page-1", "warnings": ["needs_ocr"], "locator": {"page": 1}}
            doc = {"source": source, "extractor": {"page_count": 1}, "units": [unit]}
            report = {"complete": True, "documents": [{"path": "document.json", "source_sha256": sha}]}
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report))
            (root / "document.json").write_text(json.dumps(doc))
            self.assertEqual(len(tasks_from_report(report_path, root)), 1)
            doc["units"][0]["locator"]["page"] = 2
            (root / "document.json").write_text(json.dumps(doc))
            with self.assertRaisesRegex(ExtractionFailure, "page_locator"):
                tasks_from_report(report_path, root)
            (root / "source.pdf").write_bytes(b"changed")
            with self.assertRaisesRegex(ExtractionFailure, "hash_mismatch"):
                tasks_from_report(report_path, root)


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous = self.root / "previous"
        self.previous.mkdir()
        self.output = self.root / "new"
        self.output.mkdir()
        self.native = self.root / "native.json"
        self.native.write_text('{"complete":true}')
        self.config = {"pipeline_version": VERSION, "tesseract": "tesseract test", "languages": "eng", "models_sha256": {"eng": "model-hash"}, "dpi": 180, "psm": 11, "format": "tsv"}
        self.task = {"source": {"sha256": "a" * 64, "title": "Old title", "path": "old-original"}, "page": 1, "original": self.root / "image", "unit_id": "source:page-1", "standalone_image": True}
        with patch("scripts.ocr_pages.call", return_value=TSV):
            entry = process(self.task, self.previous, self.config, self.root)
        self.cache_path = self.previous / entry["path"]
        self.report_path = self.previous / "report.json"
        self.report = {"schema_version": 1, "mode": "all", "complete": True, "errors": [], "scope": "images", "config": self.config, "pages": [entry]}
        self.save_report()

    def save_report(self):
        self.report_path.write_text(json.dumps(self.report))

    def test_identical_run_reuses_without_process_and_updates_metadata(self):
        task = {**self.task, "source": {**self.task["source"], "title": "Current title", "path": "new-original"}}
        with patch("scripts.ocr_pages.call", return_value=b"tesseract test\n"), patch("scripts.ocr_pages.model_hashes", return_value={"eng": "model-hash"}), patch("scripts.ocr_pages.tasks_from_report", return_value=[task]), patch("scripts.ocr_pages.process") as processor:
            self.assertEqual(run(self.native, self.root, self.output, self.root, languages="eng", scope="images", reuse_report=self.report_path), 0)
        processor.assert_not_called()
        fresh_report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(fresh_report["summary"]["reused_pages"], 1)
        self.assertEqual(fresh_report["summary"]["new_pages"], 0)
        self.assertEqual(fresh_report["source_report_sha256"], hashlib.sha256(self.native.read_bytes()).hexdigest())
        reused = json.loads((self.output / fresh_report["pages"][0]["path"]).read_text())
        previous = json.loads(self.cache_path.read_text())
        self.assertEqual(reused["source"], task["source"])
        for key in ("text", "text_sha256", "words", "warnings", "mean_confidence"):
            self.assertEqual(reused[key], previous[key])

    def test_changed_config_or_scope_or_incomplete_report_misses(self):
        self.assertEqual(reuse_index(self.report_path, {**self.config, "dpi": 200}, "images"), {})
        self.report["scope"] = "missing"
        self.save_report()
        self.assertEqual(reuse_index(self.report_path, self.config, "images"), {})
        self.report["scope"] = "images"
        self.report["complete"] = False
        self.save_report()
        self.assertEqual(reuse_index(self.report_path, self.config, "images"), {})

    def test_changed_source_or_unit_or_locator_misses(self):
        index = reuse_index(self.report_path, self.config, "images")
        for task in [{**self.task, "source": {**self.task["source"], "sha256": "b" * 64}}, {**self.task, "unit_id": "other"}, {**self.task, "locator": {"page": 1, "block": 2}}]:
            with self.subTest(task=task):
                self.assertIsNone(copy_cached(task, self.output, self.config, index))

    def test_corrupt_text_and_quality_miss_even_legacy_without_file_digest(self):
        self.report["pages"][0].pop("document_sha256")
        self.save_report()
        index = reuse_index(self.report_path, self.config, "images")
        original = json.loads(self.cache_path.read_text())
        for key, value in [("text", "tampered"), ("mean_confidence", float("nan")), ("warnings", []), ("words", [{"text": "bad"}]), ("schema_version", 99)]:
            self.cache_path.write_text(json.dumps({**original, key: value}))
            with self.subTest(key=key):
                self.assertIsNone(copy_cached(self.task, self.output, self.config, index))

    def test_document_digest_rejects_tampering_with_text_hash_updated(self):
        original = json.loads(self.cache_path.read_text())
        original["text"] = "tampered"
        original["text_sha256"] = hashlib.sha256(b"tampered").hexdigest()
        self.cache_path.write_text(json.dumps(original))
        self.assertIsNone(copy_cached(self.task, self.output, self.config, reuse_index(self.report_path, self.config, "images")))

    def test_traversal_and_symlink_escape_never_read(self):
        outside = self.root / "outside.json"
        outside.write_text("private sentinel")
        link = self.previous / "link.json"
        link.symlink_to(outside)
        for path in ("../outside.json", str(outside), "link.json"):
            self.report["pages"][0]["path"] = path
            self.save_report()
            index = reuse_index(self.report_path, self.config, "images")
            self.assertEqual(index, {})
            with patch.object(Path, "read_bytes", side_effect=AssertionError("external cache was read")):
                self.assertIsNone(copy_cached(self.task, self.output, self.config, index))

    def test_cache_miss_invokes_processor(self):
        task = {**self.task, "source": {**self.task["source"], "sha256": "b" * 64}}
        # Real process executes only the mocked local tesseract call on a standalone image.
        def tools(argv):
            return b"tesseract test\n" if argv == ["tesseract", "--version"] else TSV
        with patch("scripts.ocr_pages.call", side_effect=tools) as tools_call, patch("scripts.ocr_pages.model_hashes", return_value={"eng": "model-hash"}), patch("scripts.ocr_pages.tasks_from_report", return_value=[task]):
            self.assertEqual(run(self.native, self.root, self.output, self.root, languages="eng", scope="images", reuse_report=self.report_path), 0)
        self.assertEqual(tools_call.call_count, 2)
        summary = json.loads((self.output / "report.json").read_text())["summary"]
        self.assertEqual(summary["new_pages"], 1)
        self.assertEqual(summary["reused_pages"], 0)


if __name__ == "__main__":
    unittest.main()
