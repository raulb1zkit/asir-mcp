import unittest

from scripts.import_supabase import source_key


class SourceIdentityTests(unittest.TestCase):
    def test_same_object_in_two_sections_has_distinct_provenance(self):
        source = {"sha256": "a" * 64, "subject": "SAD", "section": "Actividades", "title": "Enunciado", "filename": "enunciado.pdf", "url": "https://example.invalid/1", "mime": "application/pdf", "path": "objects/aa/a"}
        self.assertEqual(source_key(source), source_key(dict(reversed(list(source.items())))))
        self.assertNotEqual(source_key(source), source_key({**source, "section": "Evaluación"}))


if __name__ == "__main__":
    unittest.main()
