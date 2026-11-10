from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.search_material import SearchEngine, SearchFailure, build_index, query_terms


def chunk(text, subject="ASO", section="UD1", method="native"):
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {"id": "chunk:" + digest, "text": text, "text_sha256": digest, "requires_review": method != "native", "warnings": ["ocr_requires_review"] if method != "native" else [], "citations": [{"source": {"subject": subject, "section": section, "title": "Material " + section, "filename": "material.txt", "sha256": digest}, "segments": [{"source_unit_id": digest[:16] + ":block-1", "locator": {"block": 1}, "start": 0, "end": len(text), "method": method, "quality": "native" if method == "native" else "ocr_unreviewed"}], "method": method}]}


def build_fixture(root, chunks):
    data = root / "chunks.jsonl"
    data.write_text("".join(json.dumps(c) + "\n" for c in chunks))
    report = root / "report.json"
    report.write_text(json.dumps({"complete": True, "output_sha256": hashlib.sha256(data.read_bytes()).hexdigest(), "summary": {"chunks": len(chunks)}}))
    db = root / "index.sqlite"
    with redirect_stdout(io.StringIO()):
        build_index(data, report, db)
    return db


class LexicalTests(unittest.TestCase):
    def test_accents_literals_operators_and_review_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = build_fixture(Path(tmp), [chunk("Administración de procesos: ps aux | grep bash"), chunk('grep -E "^ERROR" log', method="ocr_supplement")])
            engine = SearchEngine(db)
            try:
                self.assertEqual(len(engine.lexical("administracion procesos")), 1)
                self.assertEqual(engine.lexical('grep -E "^ERROR"', exact=True)[0]["score_kind"], "literal")
                self.assertTrue(engine.lexical('grep -E "^ERROR"', exact=True)[0]["requires_review"])
                self.assertEqual(engine.lexical('grep -e "^ERROR"', exact=True), [])
                self.assertEqual(len(engine.lexical("ps aux | grep bash", exact=True)), 1)
            finally:
                engine.close()

    def test_filters_apply_to_candidates_and_returned_citations(self):
        shared = chunk("crontab tareas")
        alias = copy.deepcopy(shared["citations"][0])
        alias["source"]["subject"] = "FOL"
        shared["citations"].append(alias)
        with tempfile.TemporaryDirectory() as tmp:
            engine = SearchEngine(build_fixture(Path(tmp), [shared, chunk("crontab imágenes", section="UD2", method="ocr_supplement")]))
            try:
                rows = engine.lexical("crontab", subject="FOL", native_only=True)
                self.assertEqual(len(rows), 1)
                self.assertEqual([c["source"]["subject"] for c in rows[0]["citations"]], ["FOL"])
                self.assertEqual(engine.lexical("crontab", section="missing"), [])
                self.assertEqual(len(engine.lexical("crontab", native_only=True)), 1)
            finally:
                engine.close()

    def test_raw_fts_syntax_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = SearchEngine(build_fixture(Path(tmp), [chunk("cron tareas")]))
            try:
                engine.lexical('cron" OR body:*; DROP TABLE chunks; --')
                self.assertEqual(len(engine.all_chunks()), 1)
                with self.assertRaisesRegex(SearchFailure, "no_searchable_terms"):
                    engine.lexical("el la y de")
                with self.assertRaises(SearchFailure):
                    engine.lexical(" ")
                with self.assertRaises(SearchFailure):
                    engine.lexical("cron", limit=101)
            finally:
                engine.close()

    def test_failed_rebuild_preserves_previous_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_fixture(root, [chunk("cron")])
            before = db.read_bytes()
            (root / "chunks.jsonl").write_text("tampered")
            with self.assertRaisesRegex(SearchFailure, "stale"):
                build_index(root / "chunks.jsonl", root / "report.json", db)
            self.assertEqual(db.read_bytes(), before)

    def test_terms_normalized_without_boolean_operators(self):
        self.assertEqual(query_terms("¿Cómo cambiar la PRIORIDAD del proceso?"), ["cambiar", "prioridad", "proceso"])
        self.assertEqual(query_terms("permisos r w x"), ["permisos", "r", "w", "x"])
