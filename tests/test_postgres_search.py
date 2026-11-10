import unittest
from unittest.mock import Mock

from scripts.postgres_search import PostgresRetriever, excerpt_for


class ExcerptTests(unittest.TestCase):
    def test_accent_insensitive_term_keeps_original_text(self):
        text = "x" * 300 + " La prioridad de un proceso se cambia con renice. " + "y" * 300
        excerpt = excerpt_for(text, terms=["prioridad"])
        self.assertIn("prioridad", excerpt)
        self.assertIn("renice", excerpt)
        self.assertNotIn("\n", excerpt)

    def test_literal_is_case_sensitive_and_centered(self):
        text = "A" * 300 + "Renice cambia la prioridad" + "B" * 300
        self.assertIn("Renice", excerpt_for(text, literal="Renice"))
        self.assertTrue(excerpt_for(text, literal="renice").startswith("A" * 300))

    def test_missing_term_falls_back_to_prefix(self):
        self.assertEqual(excerpt_for("texto corto", terms=["ausente"]), "texto corto")

    def test_hybrid_hydrates_only_selected_results_and_preserves_rrf(self):
        retriever = PostgresRetriever.__new__(PostgresRetriever)
        engine = Mock()
        retriever.engine = engine
        engine.lexical.return_value = [{"chunk_id": "a", "score": 1.0, "score_kind": "bm25", "lexical_mode": "all_terms"}, {"chunk_id": "b", "score": 0.5, "score_kind": "bm25", "lexical_mode": "all_terms"}]
        engine.semantic.return_value = [{"chunk_id": "b", "score": 0.9, "score_kind": "cosine", "matched_window": {"char_start": 0, "char_end": 4, "tokens": 2}}]
        engine._chunk_rows.return_value = {"b": {"text": "beta", "requires_review": False, "warnings": [], "citations": [{"source": {}}]}}
        rows = retriever.search("beta", mode="hybrid", limit=1, subject="IAW")
        self.assertEqual([row["chunk_id"] for row in rows], ["b"])
        self.assertAlmostEqual(rows[0]["score"], 1 / 62 + 1 / 61)
        self.assertEqual(rows[0]["excerpt"], "beta")
        self.assertEqual(engine._chunk_rows.call_args.args[0], ["b"])
        self.assertTrue(engine.lexical.call_args.kwargs["candidates_only"])
        self.assertTrue(engine.semantic.call_args.kwargs["candidates_only"])
