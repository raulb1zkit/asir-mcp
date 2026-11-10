import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.evaluate_search import evaluate, first_relevant_rank
from scripts.semantic_search import LocalEncoder, Retriever, SearchFailure, build_semantic, encode_windows, mean_pool, validate_coverage
from test_search_material import build_fixture, chunk


class Encoding:
    def __init__(self, text, special=True):
        self.ids = ([0] if special else []) + [10] * len(text) + ([2] if special else [])
        self.offsets = [(i, i + 1) for i in range(len(text))]


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return Encoding(text, add_special_tokens)


class WindowTests(unittest.TestCase):
    def test_large_text_is_segmented_without_gaps_before_tokenization(self):
        class BoundedTokenizer(CharacterTokenizer):
            def encode(self, text, add_special_tokens=True):
                self_limit = 4200
                if len(text) > self_limit:
                    raise AssertionError("input too large")
                return super().encode(text, add_special_tokens)
        text = "x" * 12000
        windows = encode_windows(BoundedTokenizer(), text, "passage: title\n", max_tokens=100, overlap_tokens=10)
        validate_coverage([span for _, span in windows], len(text), 100)
        self.assertEqual(windows[-1][1]["char_end"], len(text))

    def test_empty_corpus_is_rejected_before_loading_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_fixture(root, [])
            with self.assertRaisesRegex(SearchFailure, "empty_corpus"):
                build_semantic(db, root / "no-model", root / "index.npz")

    def test_evaluation_rejects_empty_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "questions.json").write_text('{"questions": []}')
            (root / "gold.json").write_text('{}')
            with self.assertRaisesRegex(SearchFailure, "questions_empty"):
                evaluate(root / "questions.json", root / "gold.json", root / "results.json")
    def test_long_text_fully_covered_with_repeated_prefix_and_overlap(self):
        text = "comando --flag\n  日本語 ñ\n" * 80
        windows = encode_windows(CharacterTokenizer(), text, "passage: title\n", max_tokens=100, overlap_tokens=10)
        self.assertGreater(len(windows), 1)
        covered = set()
        for encoding, span in windows:
            self.assertLessEqual(len(encoding.ids), 100)
            covered.update(range(span["char_start"], span["char_end"]))
        self.assertEqual(covered, set(range(len(text))))
        self.assertLess(windows[1][1]["char_start"], windows[0][1]["char_end"])

    def test_coverage_rejects_gaps_and_too_many_tokens(self):
        with self.assertRaises(SearchFailure):
            validate_coverage([{"char_start": 0, "char_end": 10, "tokens": 20}, {"char_start": 12, "char_end": 15, "tokens": 20}], 15)
        with self.assertRaises(SearchFailure):
            validate_coverage([{"char_start": 0, "char_end": 5, "tokens": 513}], 5)

    def test_literal_query_does_not_load_semantic_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retriever = Retriever(build_fixture(root, [chunk("grep -E pattern")]), root / "no-model", root / "no-index")
            try:
                self.assertEqual(len(retriever.search("grep -E", exact=True)), 1)
                self.assertIsNone(retriever.encoder)
            finally:
                retriever.close()

    def test_rrf_uses_ranks_not_incompatible_raw_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retriever = Retriever(build_fixture(root, [chunk("alpha"), chunk("beta")]))
            a = {"chunk_id": "a", "score": 9000, "score_kind": "bm25"}
            b = {"chunk_id": "b", "score": 0.01, "score_kind": "bm25"}
            try:
                with patch.object(retriever.engine, "lexical", return_value=[a, b]), patch.object(retriever, "semantic", return_value=[{**b, "score": 0.9, "score_kind": "cosine"}, {**a, "score": 0.8, "score_kind": "cosine"}]):
                    rows = retriever.search("query", limit=2)
                self.assertAlmostEqual(rows[0]["score"], 1 / 61 + 1 / 62)
                self.assertEqual(rows[0]["score"], rows[1]["score"])
                self.assertEqual(rows[0]["score_kind"], "rrf")
            finally:
                retriever.close()

    def test_reference_metric_checks_all_citations(self):
        rows = [{"citations": [{"segments": [{"source_unit_id": "wrong"}]}]}, {"citations": [{"segments": [{"source_unit_id": "right"}]}]}]
        self.assertEqual(first_relevant_rank(rows, ["right"]), 2)
        self.assertIsNone(first_relevant_rank(rows, ["missing"]))


@unittest.skipUnless(importlib.util.find_spec("numpy"), "Semantic numerical tests require project venv")
class NumericalTests(unittest.TestCase):
    def test_length_sorted_batches_restore_the_original_vector_order(self):
        import numpy as np
        class Input:
            def __init__(self, ids):
                self.ids = ids
                self.attention_mask = [1] * len(ids)
                self.type_ids = [0] * len(ids)
        class Session:
            def run(self, outputs, inputs):
                values = inputs['input_ids'].astype(np.float32)
                return [np.stack([values, np.ones_like(values)], axis=-1)]
        encoder = LocalEncoder.__new__(LocalEncoder)
        encoder.np = np
        encoder.manifest = {'dimensions': 2, 'max_tokens': 512}
        encoder.pad_id = 0
        encoder.input_names = {'input_ids', 'attention_mask', 'token_type_ids'}
        encoder.session = Session()
        inputs = [Input([10, 10, 10]), Input([2]), Input([6, 6])]
        vectors = encoder.encode(inputs, batch_size=2)
        for vector, value in zip(vectors, [10, 2, 6]):
            expected = np.array([value, 1.], dtype=np.float32)
            expected /= np.linalg.norm(expected)
            np.testing.assert_allclose(vector, expected, rtol=1e-6)

    def test_padding_does_not_change_pooling_and_vectors_are_normalized(self):
        import numpy as np
        hidden = np.array([[[1., 0.], [1., 0.], [100., 100.]]], dtype=np.float32)
        pooled = mean_pool(hidden, np.array([[1, 1, 0]]))
        np.testing.assert_allclose(pooled, [[1., 0.]])
        self.assertEqual(pooled.dtype, np.float32)

    def test_failed_semantic_load_cannot_leave_a_valid_cached_state(self):
        import numpy as np
        class FakeEncoder:
            def __init__(self, path):
                self.np = np
                self.fingerprint = "expected"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = build_fixture(root, [chunk("example")])
            matrix = np.array([[1., 0.]], dtype=np.float32)
            metadata = {"schema_version": 1, "corpus_sha256": "stale", "model_fingerprint": "expected", "windows": [{}], "dimensions": 2, "vectors_sha256": hashlib.sha256(matrix.tobytes()).hexdigest()}
            np.savez(root / "index.npz", vectors=matrix, metadata=np.array(json.dumps(metadata)))
            retriever = Retriever(db, root, root / "index.npz")
            try:
                with patch("scripts.semantic_search.LocalEncoder", FakeEncoder):
                    for _ in range(2):
                        with self.assertRaisesRegex(SearchFailure, "stale"):
                            retriever.load_semantic()
                        self.assertIsNone(retriever.encoder)
            finally:
                retriever.close()
