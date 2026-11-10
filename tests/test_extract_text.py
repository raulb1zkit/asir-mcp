from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from scripts.extract_text import (
    DocxNumbering, DRAW, ExtractionFailure, MC, O, ODF, ST, T, TB, W, XLINK,
    docx_extract, extract, number_label, odt_extract, pdf_extract, plain_extract, manual_html_extract, run,
)


class ExtractTests(unittest.TestCase):
    def test_manual_html_keeps_code_and_ignores_navigation_and_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.html"
            path.write_text('<nav>irrelevant</nav><div id="layout-content"><h1>Function</h1><pre>echo &quot;hello&quot;;</pre><script>evil()</script></div><div>footer</div>')
            units, warnings, _ = manual_html_extract(path)
            self.assertIn('echo "hello";', units[0]["text"])
            self.assertNotIn("evil", units[0]["text"])
            self.assertNotIn("footer", units[0]["text"])
            self.assertIn("html_layout_not_preserved", warnings)
    def archive(self, directory, members):
        path = Path(directory) / "source.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return path

    def test_docx_preserves_tabs_lines_table_order_and_single_textbox(self):
        xml = f'''<w:document xmlns:w="{W[1:-1]}" xmlns:mc="{MC[1:-1]}"><w:body>
        <w:p><w:r><w:t>printf</w:t><w:tab/><w:t>'%s'</w:t><w:br/><w:t>done</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>
        <w:p><w:r><w:t>BOX</w:t></w:r></w:p></w:txbxContent></w:drawing></mc:Choice>
        <mc:Fallback><w:pict><w:txbxContent><w:p><w:r><w:t>BOX</w:t></w:r></w:p>
        </w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:p>
        <w:p><w:del><w:r><w:delText>REMOVED</w:delText></w:r></w:del>
        <w:ins><w:r><w:t>INSERTED</w:t></w:r></w:ins></w:p></w:body></w:document>'''
        with tempfile.TemporaryDirectory() as tmp:
            units, warnings, _ = docx_extract(self.archive(tmp, {"word/document.xml": xml}))
        self.assertEqual(units[0]["text"], "printf\t'%s'\ndone")
        self.assertEqual(units[1]["rows"], [["A", "B"]])
        self.assertEqual([u["text"] for u in units].count("BOX"), 1)
        self.assertEqual(units[-1]["text"], "INSERTED")
        self.assertIn("tracked_changes_present", warnings)
        self.assertEqual(units[-1]["locator"], {"block": len(units)})

    def test_docx_numbering_restart_letters_and_override(self):
        root = ET.fromstring(f'''<w:numbering xmlns:w="{W[1:-1]}"><w:abstractNum w:abstractNumId="1">
        <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
        <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1.%2)"/></w:lvl>
        </w:abstractNum><w:num w:numId="4"><w:abstractNumId w:val="1"/>
        <w:lvlOverride w:ilvl="0"><w:startOverride w:val="3"/></w:lvlOverride></w:num></w:numbering>''')
        numbers = DocxNumbering(root)
        self.assertEqual(numbers.next({"numId": "4", "ilvl": "0"}), "3.")
        self.assertEqual(numbers.next({"numId": "4", "ilvl": "1"}), "3.a)")
        self.assertEqual(numbers.next({"numId": "4", "ilvl": "1"}), "3.b)")
        self.assertEqual(numbers.next({"numId": "4", "ilvl": "0"}), "4.")
        self.assertEqual(numbers.next({"numId": "4", "ilvl": "1"}), "4.a)")
        self.assertEqual(number_label(27, "lowerLetter"), "aa")
        self.assertEqual(number_label(19, "upperRoman"), "XIX")
        self.assertIsNone(number_label(1, "unknown"))

    def test_docx_explicit_never_restart(self):
        root = ET.fromstring(f'''<w:numbering xmlns:w="{W[1:-1]}"><w:abstractNum w:abstractNumId="1">
        <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
        <w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/><w:lvlText w:val="%2."/><w:lvlRestart w:val="0"/></w:lvl>
        </w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>''')
        numbers = DocxNumbering(root)
        self.assertEqual(numbers.next({"numId": "1", "ilvl": "0"}), "1.")
        self.assertEqual(numbers.next({"numId": "1", "ilvl": "1"}), "1.")
        self.assertEqual(numbers.next({"numId": "1", "ilvl": "0"}), "2.")
        self.assertEqual(numbers.next({"numId": "1", "ilvl": "1"}), "2.")

    def test_docx_unsupported_choice_uses_fallback(self):
        xml = f'''<w:document xmlns:w="{W[1:-1]}" xmlns:mc="{MC[1:-1]}"><w:body><w:p>
        <mc:AlternateContent><mc:Choice Requires="unsupported"><w:r><w:t>WRONG</w:t></w:r></mc:Choice>
        <mc:Fallback><w:r><w:t>RIGHT</w:t></w:r></mc:Fallback></mc:AlternateContent></w:p></w:body></w:document>'''
        with tempfile.TemporaryDirectory() as tmp:
            units, _, _ = docx_extract(self.archive(tmp, {"word/document.xml": xml}))
        self.assertEqual(units[0]["text"], "RIGHT")

    def test_odt_spaces_newlines_table_and_list_continuation(self):
        xml = f'''<office:document-content xmlns:office="{O[1:-1]}" xmlns:text="{T[1:-1]}"
        xmlns:table="{TB[1:-1]}" xmlns:style="{ST[1:-1]}"><office:automatic-styles>
        <text:list-style style:name="L"><text:list-level-style-number text:level="1" style:num-format="1" style:num-suffix="."/>
        </text:list-style></office:automatic-styles><office:body><office:text>
        <text:p>echo<text:s text:c="3"/>hi<text:tab/>there<text:line-break/>done</text:p>
        <text:list text:style-name="L" xml:id="first"><text:list-item><text:p>uno</text:p></text:list-item>
        <text:list-item><text:p>dos</text:p></text:list-item></text:list>
        <text:list text:style-name="L" text:continue-list="first"><text:list-item><text:p>tres</text:p></text:list-item></text:list>
        <table:table><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell>
        <table:table-cell><text:p>B</text:p></table:table-cell></table:table-row></table:table>
        </office:text></office:body></office:document-content>'''
        with tempfile.TemporaryDirectory() as tmp:
            units, warnings, _ = odt_extract(self.archive(tmp, {"content.xml": xml}))
        self.assertEqual(units[0]["text"], "echo   hi\tthere\ndone")
        self.assertEqual([u["list_label"] for u in units if "list_label" in u], ["1.", "2.", "3."])
        self.assertEqual(units[-1]["rows"], [["A", "B"]])
        self.assertNotIn("list_numbering_unresolved", warnings)

    def test_archive_xml_entity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.archive(tmp, {"content.xml": '<!DOCTYPE doc [<!ENTITY x "bad">]><doc>&x;</doc>'})
            with self.assertRaisesRegex(ExtractionFailure, "entities"):
                odt_extract(path)

    def test_odt_image_has_a_traceable_block(self):
        xml = f'''<office:document-content xmlns:office="{O[1:-1]}" xmlns:text="{T[1:-1]}"
        xmlns:draw="{DRAW[1:-1]}" xmlns:xlink="{XLINK[1:-1]}"><office:body><office:text>
        <text:p>Diagrama<draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame></text:p>
        </office:text></office:body></office:document-content>'''
        with tempfile.TemporaryDirectory() as tmp:
            path = self.archive(tmp, {"content.xml": xml, "Pictures/a.png": b"synthetic image"})
            units, _, _ = odt_extract(path)
        self.assertEqual(units[0]["text"], "Diagrama")
        self.assertEqual(units[1]["kind"], "image")
        self.assertEqual(units[1]["locator"], {"block": 2})
        self.assertEqual(units[1]["image_member"], "Pictures/a.png")
        self.assertIn("images_not_transcribed", units[1]["warnings"])

    def test_pdf_empty_page_locator_and_ocr_warning(self):
        def fake_tool(args):
            if args[0] == "pdftotext":
                return b"echo --flag\n  next\f\f"
            if args[0] == "pdfinfo":
                return b"Pages: 2\n"
            return b"page num type\n2 0 image 100 100\n"
        with patch("scripts.extract_text.tool", side_effect=fake_tool), patch("scripts.extract_text.subprocess.run") as command:
            command.return_value.stderr = b"-remove-hyphens"
            units, _, details = pdf_extract(Path("dummy.pdf"))
        self.assertEqual(details["page_count"], 2)
        self.assertEqual(units[0]["text"], "echo --flag\n  next")
        self.assertEqual(units[1]["locator"], {"page": 2})
        self.assertIn("needs_ocr", units[1]["warnings"])

    def test_pdf_page_mismatch_is_an_error(self):
        with patch("scripts.extract_text.tool", side_effect=[b"one\f", b"Pages: 2\n"]), patch("scripts.extract_text.subprocess.run") as command:
            command.return_value.stderr = b"-remove-hyphens"
            with self.assertRaisesRegex(ExtractionFailure, "count_mismatch"):
                pdf_extract(Path("dummy.pdf"))

    def test_csv_multiline_cells_and_single_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.csv"
            path.write_text('name;command\nexample;"echo x\necho y"\n')
            units, _, details = plain_extract(path, "text/csv")
            self.assertEqual(details["delimiter"], ";")
            self.assertEqual(units[1]["cells"][1], "echo x\necho y")
            path.write_text("one\ntwo\n")
            units, _, _ = plain_extract(path, "text/csv")
            self.assertEqual([u["cells"] for u in units], [["one"], ["two"]])

    def test_hash_and_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "a.txt").write_text("hello")
            source = {"path": "a.txt", "sha256": "0" * 64}
            with self.assertRaisesRegex(ExtractionFailure, "hash_mismatch"):
                extract(source, root)
            source["path"] = "../outside.txt"
            with self.assertRaisesRegex(ExtractionFailure, "outside_root"):
                extract(source, root)

    def test_invalid_text_encoding_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.txt"
            path.write_bytes(b"\xff\x00\x80")
            with self.assertRaisesRegex(ExtractionFailure, "encoding"):
                plain_extract(path, "text/plain")

    def test_sql_unicode_and_legacy_encoding_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "example.sql"
            path.write_bytes("selección".encode("utf-16"))
            units, warnings, _ = plain_extract(path, "text/plain")
            self.assertEqual(units[0]["text"], "selección")
            self.assertFalse(warnings)
            path.write_bytes("selección".encode("cp1252"))
            units, warnings, _ = plain_extract(path, "text/plain")
            self.assertEqual(units[0]["text"], "selección")
            self.assertIn("encoding_cp1252_assumed_requires_review", warnings)

    def test_duplicate_hash_preserves_source_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b"hello"
            (root / "source.txt").write_bytes(raw)
            source = {"path": "source.txt", "sha256": hashlib.sha256(raw).hexdigest(), "mime": "text/plain", "url": "https://example.test/a", "title": "A", "section": "UD1", "filename": "source.txt"}
            manifest = {"complete": True, "issues": [], "items": [source, {**source, "url": "https://example.test/b", "title": "B"}]}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run(path, root / "output"), 0)
            report = json.loads((root / "output/report.json").read_text())
            doc = json.loads((root / "output" / report["documents"][0]["path"]).read_text())
            self.assertEqual(doc["source"]["title"], "A")
            self.assertEqual(doc["source_aliases"][0]["title"], "B")

    def test_batch_failure_is_reported_and_original_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b"hello"
            (root / "source.txt").write_bytes(raw)
            manifest = {"complete": True, "issues": [], "items": [{"sha256": "0" * 64, "path": "source.txt"}]}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            with redirect_stdout(io.StringIO()):
                status = run(path, root / "output")
            self.assertEqual(status, 1)
            self.assertEqual((root / "source.txt").read_bytes(), raw)
            report = json.loads((root / "output/report.json").read_text())
            self.assertFalse(report["complete"])
            self.assertEqual(report["errors"][0]["error"], "source_hash_mismatch")


if __name__ == "__main__":
    unittest.main()
