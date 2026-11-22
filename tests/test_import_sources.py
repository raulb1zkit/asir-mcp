import unittest
from unittest.mock import patch

from scripts.import_supabase import source_key


def corpus_fixture():
    subject = "SRI"
    chunk_id = "chunk:" + "b" * 64
    source = {"sha256": "a" * 64, "subject": subject, "section": "Unidad 1", "title": "Prueba", "filename": "prueba.txt", "url": "https://example.invalid/1", "mime": "text/plain", "path": "objects/aa/a", "moodle_active": True}
    return {
        "chunks": [{"id": chunk_id, "text": "texto", "text_sha256": "c" * 64, "characters": 5, "warnings": [], "requires_review": False, "citations": [{"source": source, "method": "native", "segments": []}]}],
        "report": {"schema_version": 1, "output_sha256": "d" * 64, "chunker_version": "1", "created_at": "2026-11-16T00:00:00+00:00"},
        "metadata": {"schema_version": 1, "corpus_sha256": "d" * 64, "model_fingerprint": "e" * 64, "model_repository": "local", "model_revision": "1", "dimensions": 2, "max_tokens": 512, "overlap_tokens": 64, "context": "subject-section-title", "vectors_sha256": "f" * 64, "summary": {}, "windows": [{"chunk_id": chunk_id, "char_start": 0, "char_end": 5, "tokens": 1}]},
        "vectors": [[1.0, 0.0]],
    }


def run_import(reset_identity):
    from scripts import import_supabase as module
    queries = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            queries.append(query if isinstance(query, str) else str(query))

        def executemany(self, query, params_seq):
            queries.append(query if isinstance(query, str) else str(query))

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    with patch.object(module, "verify_corpus"), patch.object(module, "vector_literal", return_value="[1,0]"):
        module.import_corpus(Connection(), corpus_fixture(), reset_identity=reset_identity)
    return queries


class SourceIdentityTests(unittest.TestCase):
    def test_same_object_in_two_sections_has_distinct_provenance(self):
        source = {"sha256": "a" * 64, "subject": "SAD", "section": "Actividades", "title": "Enunciado", "filename": "enunciado.pdf", "url": "https://example.invalid/1", "mime": "application/pdf", "path": "objects/aa/a"}
        self.assertEqual(source_key(source), source_key(dict(reversed(list(source.items())))))
        self.assertNotEqual(source_key(source), source_key({**source, "section": "Evaluación"}))
        self.assertEqual(source_key(source), source_key({**source, "moodle_active": False}))


class ImportSequenceTests(unittest.TestCase):
    def test_daily_import_never_writes_the_citation_sequence(self):
        # El rol asir_ingest solo tiene USAGE/SELECT sobre secuencias.
        self.assertFalse(any("setval" in query for query in run_import(False)))

    def test_administrative_restart_syncs_the_citation_sequence(self):
        self.assertTrue(any("setval" in query for query in run_import(True)))


if __name__ == "__main__":
    unittest.main()
