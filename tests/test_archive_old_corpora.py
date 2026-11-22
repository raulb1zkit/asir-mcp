import unittest
from scripts.archive_old_corpora import copy_counts


class ArchiveCountsTests(unittest.TestCase):
    def test_copy_rows_handle_escaped_newlines_and_empty_tables(self):
        self.assertEqual(copy_counts(['COPY asir_before_sri.chunks (id, text) FROM stdin;\n', 'a\tline\\nnext\n', '\\.\n', 'COPY asir_previous.sources (id) FROM stdin;\n', '\\.\n']), {'asir_before_sri.chunks':1,'asir_previous.sources':0})

    def test_unfinished_or_unexpected_tables_fail_closed(self):
        for lines in [['COPY asir_before_sri.chunks (id) FROM stdin;\n','row\n'], ['COPY asir.chunks (id) FROM stdin;\n'], ['COPY asir_previous.chunks (id) FROM stdin;\n','\\.\n','COPY asir_previous.chunks (id) FROM stdin;\n']]:
            with self.assertRaises(ValueError):copy_counts(lines)
