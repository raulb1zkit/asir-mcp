from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.extract_text import ExtractionFailure, extract as real_extract, run


class ExtractReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "notes.txt"
        self.source.write_text("contenido estable")
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.output = self.root / "output"

    def item(self, **overrides):
        item = {"sha256": self.sha, "path": "notes.txt", "url": "https://example.test/a", "title": "Título A", "section": "UD1", "mime": "text/plain", "filename": "notes.txt", "moodle_active": False}
        item.update(overrides)
        return item

    def write_manifest(self, items):
        self.manifest.write_text(json.dumps({"complete": True, "issues": [], "items": items}))
        return self.manifest

    def extract(self, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run(*args, **kwargs)

    def report(self):
        return json.loads((self.output / "report.json").read_text())

    def document(self, report, sha=None):
        entry = next(d for d in report["documents"] if d["source_sha256"] == (sha or self.sha))
        return json.loads((self.output / entry["path"]).read_text())

    def test_same_hash_reuses_without_calling_extract(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        with patch("scripts.extract_text.extract", side_effect=AssertionError("extract no debe ejecutarse")) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        mocked.assert_not_called()
        report = self.report()
        self.assertEqual(report["reused_documents"], [self.sha])
        self.assertEqual(report["new_documents"], [])
        self.assertEqual(report["summary"]["reused_documents"], 1)
        self.assertTrue(report["documents"][0]["reused"])
        self.assertEqual(self.document(report)["source"]["sha256"], self.sha)

    def test_new_hash_is_extracted(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        other = self.root / "other.txt"
        other.write_text("nuevo contenido")
        other_sha = hashlib.sha256(other.read_bytes()).hexdigest()
        self.write_manifest([self.item(), self.item(sha256=other_sha, path="other.txt", filename="other.txt", title="Nuevo")])
        with patch("scripts.extract_text.extract", side_effect=real_extract) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        self.assertEqual([call.args[0]["sha256"] for call in mocked.call_args_list], [other_sha])
        report = self.report()
        self.assertEqual(report["reused_documents"], [self.sha])
        self.assertEqual(report["new_documents"], [other_sha])

    def test_cached_extraction_still_verifies_original_bytes(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        self.source.write_text("bytes alterados sin actualizar manifiesto")
        self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 1)
        self.assertEqual(self.report()["errors"][0]["error"], "source_hash_mismatch")

    def test_metadata_update_applies_to_reused_document(self):
        self.write_manifest([self.item(), self.item(url="https://example.test/b", title="Alias")])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        self.write_manifest([self.item(title="Título actualizado", url="https://example.test/z", moodle_active=True), self.item(url="https://example.test/b", title="Alias actualizado", moodle_active=True)])
        with patch("scripts.extract_text.extract", side_effect=AssertionError("extract no debe ejecutarse")) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        mocked.assert_not_called()
        document = self.document(self.report())
        self.assertEqual(document["source"]["title"], "Título actualizado")
        self.assertEqual(document["source"]["url"], "https://example.test/z")
        self.assertIs(document["source"]["moodle_active"], True)
        self.assertEqual(document["source_aliases"][0]["title"], "Alias actualizado")
        self.assertIs(document["source_aliases"][0]["moodle_active"], True)

    def test_corrupted_cached_document_is_recalculated(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        (self.output / self.report()["documents"][0]["path"]).write_text("{not json")
        with patch("scripts.extract_text.extract", side_effect=real_extract) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        self.assertEqual(mocked.call_count, 1)
        report = self.report()
        self.assertEqual(report["reused_documents"], [])
        self.assertEqual(report["new_documents"], [self.sha])
        self.assertFalse(report["documents"][0]["reused"])
        self.assertEqual(self.document(report)["units"][0]["text"], "contenido estable")

    def test_cached_document_with_foreign_source_hash_is_recalculated(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        report = self.report()
        document_path = self.output / report["documents"][0]["path"]
        document = self.document(report)
        document["source"]["sha256"] = "0" * 64
        document_path.write_text(json.dumps(document))
        report["documents"][0]["document_sha256"] = hashlib.sha256(document_path.read_bytes()).hexdigest()
        (self.output / "report.json").write_text(json.dumps(report))
        with patch("scripts.extract_text.extract", side_effect=real_extract) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.report()["new_documents"], [self.sha])

    def test_document_path_outside_report_directory_is_not_reused(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        report = self.report()
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.document(report)))
        report["documents"][0]["path"] = "../outside.json"
        report["documents"][0]["document_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        (self.output / "report.json").write_text(json.dumps(report))
        with patch("scripts.extract_text.extract", side_effect=real_extract) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        self.assertEqual(mocked.call_count, 1)

    def test_extractor_version_change_forces_recalculation(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        report = self.report()
        report["extractor_version"] = "0"
        (self.output / "report.json").write_text(json.dumps(report))
        with patch("scripts.extract_text.extract", side_effect=real_extract) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.report()["new_documents"], [self.sha])

    def test_render_html_change_forces_recalculation(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        self.assertIs(self.report()["render_html"], False)
        with patch("scripts.extract_text.extract", side_effect=real_extract) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, render_html=True, reuse_report=self.output / "report.json"), 0)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.report()["new_documents"], [self.sha])

    def test_report_without_render_html_is_treated_as_false(self):
        self.write_manifest([self.item()])
        self.assertEqual(self.extract(self.manifest, self.output), 0)
        report = self.report()
        report.pop("render_html")
        (self.output / "report.json").write_text(json.dumps(report))
        with patch("scripts.extract_text.extract", side_effect=AssertionError("extract no debe ejecutarse")) as mocked:
            self.assertEqual(self.extract(self.manifest, self.output, reuse_report=self.output / "report.json"), 0)
        mocked.assert_not_called()
        self.assertEqual(self.report()["reused_documents"], [self.sha])

    def test_corrupt_reuse_report_fails_explicitly(self):
        self.write_manifest([self.item()])
        report = self.root / "bad-report.json"
        report.write_text("{")
        with patch("scripts.extract_text.extract", side_effect=AssertionError("extract no debe ejecutarse")) as mocked:
            with self.assertRaisesRegex(ExtractionFailure, "reuse_report_unreadable"):
                self.extract(self.manifest, self.output, reuse_report=report)
        mocked.assert_not_called()
        self.assertFalse((self.output / "report.json").exists())

    def test_incomplete_reuse_report_fails_explicitly(self):
        self.write_manifest([self.item()])
        report = self.root / "incomplete-report.json"
        report.write_text(json.dumps({"complete": False, "documents": []}))
        with patch("scripts.extract_text.extract", side_effect=AssertionError("extract no debe ejecutarse")) as mocked:
            with self.assertRaisesRegex(ExtractionFailure, "reuse_report_incomplete"):
                self.extract(self.manifest, self.output, reuse_report=report)
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
