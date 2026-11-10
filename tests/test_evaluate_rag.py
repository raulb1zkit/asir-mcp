import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.search_material import SearchFailure

try:
    from scripts.evaluate_rag import evidence_in_chunk, evidence_in_read, prepare, summary
except ImportError:
    evidence_in_chunk = None


def fixture():
    first = "Enunciado: compara los procesos del sistema."
    second = "Solución: renice 5 1234 cambia la prioridad del proceso."
    text = first + "\n\n" + second
    start = len(first) + 2
    source = {"sha256": "a" * 64, "subject": "ASO", "section": "UD1", "filename": "fixture.pdf", "title": "Fixture"}
    chunk = {"id": "chunk:" + hashlib.sha256(text.encode()).hexdigest(), "text": text, "citations": [{"source": source, "method": "native", "segments": [{"source_unit_id": "unit-1", "locator": {"page": 1}, "start": 0, "end": len(first)}, {"source_unit_id": "unit-2", "locator": {"page": 2}, "start": start, "end": len(text)}]}]}
    evidence = {"source_sha256": source["sha256"], "method": "native", "source_unit_id": "unit-2", "locator": {"page": 2}, "quote": "renice 5 1234"}
    fragment = {"fragment_id": chunk["id"], "text": text, "range": {"start": 0, "end": len(text), "total": len(text)}, "references": [{"source_id": source["sha256"], "method": "native", "locations": [{"kind": "page", "start": 1, "end": 2}]}]}
    return chunk, evidence, fragment


@unittest.skipIf(evidence_in_chunk is None, "MCP evaluation requires project dependencies")
class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.chunk, self.evidence, self.fragment = fixture()

    def test_exact_quote_in_correct_source_method_unit_and_locator_is_evidence(self):
        self.assertTrue(evidence_in_chunk(self.chunk, [self.evidence]))
        self.assertTrue(evidence_in_read(self.fragment, [self.evidence], self.chunk))

    def test_quote_in_another_unit_of_same_chunk_and_document_is_not_evidence(self):
        ref = {**self.evidence, "source_unit_id": "unit-1", "locator": {"page": 1}}
        self.assertFalse(evidence_in_chunk(self.chunk, [ref]))
        self.assertFalse(evidence_in_read(self.fragment, [ref], self.chunk))

    def test_forged_locator_cannot_reuse_a_correct_unit_id_and_quote(self):
        ref = {**self.evidence, "locator": {"page": 99}}
        self.assertFalse(evidence_in_chunk(self.chunk, [ref]))
        self.assertFalse(evidence_in_read(self.fragment, [ref], self.chunk))

    def test_source_and_method_must_match(self):
        for mutation in [{"source_sha256": "b" * 64}, {"method": "ocr_supplement"}]:
            ref = self.evidence | mutation
            with self.subTest(mutation=mutation):
                self.assertFalse(evidence_in_chunk(self.chunk, [ref]))
                self.assertFalse(evidence_in_read(self.fragment, [ref], self.chunk))

    def test_quote_must_stay_inside_one_source_segment(self):
        # This substring occurs in the chunk but crosses the separator and units.
        ref = self.evidence | {"source_unit_id": "unit-1", "locator": {"page": 1}, "quote": "sistema.\n\nSolución"}
        self.assertIn(ref["quote"], self.chunk["text"])
        self.assertFalse(evidence_in_chunk(self.chunk, [ref]))
        self.assertFalse(evidence_in_read(self.fragment, [ref], self.chunk))

    def test_excerpt_quote_is_a_literal_diagnostic_separate_from_chunk_hit(self):
        self.assertTrue(evidence_in_chunk(self.chunk, [self.evidence]))
        self.assertFalse(evidence_in_chunk(self.chunk, [self.evidence], text="Enunciado: compara los procesos"))
        self.assertFalse(evidence_in_chunk(self.chunk, [self.evidence], text="renice [5] 1234"))
        self.assertTrue(evidence_in_chunk(self.chunk, [self.evidence], text="Extracto: renice 5 1234 cambia la prioridad"))

    def test_one_valid_alternative_is_enough_but_empty_evidence_is_not(self):
        invalid = self.evidence | {"source_sha256": "b" * 64}
        self.assertTrue(evidence_in_chunk(self.chunk, [invalid, self.evidence]))
        self.assertTrue(evidence_in_read(self.fragment, [invalid, self.evidence], self.chunk))
        self.assertFalse(evidence_in_chunk(self.chunk, []))
        self.assertFalse(evidence_in_read(self.fragment, [], self.chunk))

    def test_empty_or_whitespace_quotes_cannot_create_trivial_hits(self):
        for quote in ["", " "]:
            ref = self.evidence | {"quote": quote}
            with self.subTest(quote=repr(quote)):
                self.assertFalse(evidence_in_chunk(self.chunk, [ref]))
                self.assertFalse(evidence_in_read(self.fragment, [ref], self.chunk))

    def test_partial_read_without_quote_does_not_count_even_with_broad_references(self):
        end = self.chunk["citations"][0]["segments"][0]["end"]
        fragment = copy.deepcopy(self.fragment)
        fragment["range"]["end"] = end
        fragment["text"] = self.chunk["text"][:end]
        self.assertFalse(evidence_in_read(fragment, [self.evidence], self.chunk))

    def test_valid_partial_read_can_count_with_its_correct_reference(self):
        span = self.chunk["citations"][0]["segments"][1]
        fragment = copy.deepcopy(self.fragment)
        fragment["range"]["start"] = span["start"]
        fragment["text"] = self.chunk["text"][span["start"]:]
        fragment["references"][0]["locations"] = [{"kind": "page", "start": 2, "end": 2}]
        self.assertTrue(evidence_in_read(fragment, [self.evidence], self.chunk))

    def test_server_cannot_alter_text_while_preserving_quote_and_reference(self):
        fragment = copy.deepcopy(self.fragment)
        fragment["text"] = fragment["text"].replace("Enunciado", "Inventado")
        self.assertEqual(len(fragment["text"]), len(self.fragment["text"]))
        self.assertIn(self.evidence["quote"], fragment["text"])
        self.assertFalse(evidence_in_read(fragment, [self.evidence], self.chunk))

    def test_server_cannot_invent_id_range_or_total_for_correct_text(self):
        for mutation in [
            {"fragment_id": "chunk:" + "0" * 64},
            {"range": {"start": 1, "end": len(self.chunk["text"]), "total": len(self.chunk["text"])}},
            {"range": {"start": 0, "end": len(self.chunk["text"]) - 1, "total": len(self.chunk["text"])}},
            {"range": {"start": 0, "end": len(self.chunk["text"]), "total": len(self.chunk["text"]) + 1}},
        ]:
            with self.subTest(mutation=mutation):
                self.assertFalse(evidence_in_read(self.fragment | mutation, [self.evidence], self.chunk))

    def test_returned_reference_source_method_and_locator_are_checked(self):
        for mutation in [
            {"source_id": "b" * 64},
            {"method": "ocr_supplement"},
            {"locations": [{"kind": "page", "start": 1, "end": 1}]},
            {"locations": [{"kind": "block", "start": 2, "end": 2}]},
        ]:
            fragment = copy.deepcopy(self.fragment)
            fragment["references"][0].update(mutation)
            with self.subTest(mutation=mutation):
                self.assertFalse(evidence_in_read(fragment, [self.evidence], self.chunk))


@unittest.skipIf(evidence_in_chunk is None, "MCP evaluation requires project dependencies")
class SummaryTests(unittest.TestCase):
    def test_mrr_at_five_excludes_ranks_after_five(self):
        result = summary([{"rank": value} for value in [1, 2, 5, None, 6]])
        self.assertEqual(result["cases"], 5)
        self.assertEqual(result["hits_at_1"], 1)
        self.assertEqual(result["hits_at_3"], 2)
        self.assertEqual(result["hits_at_5"], 3)
        self.assertEqual(result["mrr_at_5"], .34)

    def test_no_hit_has_zero_mrr(self):
        result = summary([{"rank": None}, {"rank": None}])
        self.assertEqual(result["hits_at_5"], 0)
        self.assertEqual(result["mrr_at_5"], 0)

    def test_empty_evaluation_is_rejected(self):
        with self.assertRaises(SearchFailure):
            summary([])

    def test_invalid_ranks_are_rejected_instead_of_counted_as_hits(self):
        for value in [0, -1, True, 1.5, "1"]:
            with self.subTest(value=value):
                with self.assertRaises(SearchFailure):
                    summary([{"rank": value}])


@unittest.skipIf(evidence_in_chunk is None, "MCP evaluation requires project dependencies")
class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.questions_path, self.gold_path = root / "questions.json", root / "gold.json"
        self.chunk, self.evidence, _ = fixture()
        self.questions = {"questions": [{"id": "priority", "query": "cambiar prioridad", "category": "command"}]}
        self.gold = {"corpus_sha256": "d" * 64, "evidence": {"priority": [self.evidence]}}
        engine = SimpleNamespace(metadata={"corpus_sha256": "d" * 64}, all_chunks=Mock(return_value=[self.chunk]))
        self.retriever = SimpleNamespace(engine=engine, search=Mock(return_value=[{"chunk_id": self.chunk["id"], "excerpt": "Enunciado: compara procesos"}]), close=Mock())

    def invoke(self):
        self.questions_path.write_text(json.dumps(self.questions), encoding="utf-8")
        self.gold_path.write_text(json.dumps(self.gold), encoding="utf-8")
        with patch("scripts.evaluate_rag.Retriever", return_value=self.retriever):
            return prepare(self.questions_path, self.gold_path)

    def test_prepare_distinguishes_retrieved_quote_from_quote_in_excerpt(self):
        _, _, report = self.invoke()
        for mode in ["lexical", "semantic", "hybrid"]:
            case = report["modes"][mode]["cases"][0]
            self.assertEqual(case["rank"], 1)
            self.assertIsNone(case["exact_quote_in_excerpt_rank"])
            self.assertEqual(report["modes"][mode]["summary"]["hits_at_5"], 1)
        self.retriever.close.assert_called_once()

    def test_per_question_subject_is_used_for_each_search(self):
        self.questions["questions"][0]["subject"] = "SAD"
        self.invoke()
        self.assertEqual([call.kwargs["subject"] for call in self.retriever.search.call_args_list], ["SAD"] * 3)

    def test_empty_duplicate_or_incomplete_question_sets_are_rejected(self):
        for cases, evidence in [([], {}), (self.questions["questions"] * 2, self.gold["evidence"]), (self.questions["questions"], {}), (self.questions["questions"], self.gold["evidence"] | {"unexpected": [self.evidence]})]:
            self.questions["questions"] = cases
            self.gold["evidence"] = evidence
            with self.subTest(cases=len(cases), evidence=list(evidence)):
                with self.assertRaises(SearchFailure):
                    self.invoke()

    def test_stale_gold_is_rejected_and_retriever_closed(self):
        self.gold["corpus_sha256"] = "0" * 64
        with self.assertRaises(SearchFailure):
            self.invoke()
        self.retriever.close.assert_called_once()

    def test_every_gold_reference_must_match_its_authoritative_segment(self):
        for mutation in [{"quote": ""}, {"quote": " "}, {"quote": "una cita inventada"}, {"source_unit_id": "unit-1", "locator": {"page": 1}}, {"locator": {"page": 99}}, {"source_sha256": "b" * 64}, {"method": "ocr_supplement"}]:
            self.gold["evidence"]["priority"] = [self.evidence, self.evidence | mutation]
            self.retriever.close.reset_mock()
            with self.subTest(mutation=mutation):
                with self.assertRaises(SearchFailure):
                    self.invoke()
                self.retriever.close.assert_called_once()

    def test_empty_reference_list_is_rejected(self):
        self.gold["evidence"]["priority"] = []
        with self.assertRaises(SearchFailure):
            self.invoke()
        self.retriever.close.assert_called_once()
