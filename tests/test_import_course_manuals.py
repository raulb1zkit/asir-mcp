import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from import_course_manuals import content_node, manual_url
from moodle_download import Document


class ManualTests(unittest.TestCase):
    def test_manual_allowlist(self):
        valid = 'https://manuais.pages.example.edu/a/index.html'
        self.assertEqual(manual_url(valid + '#part'), valid)
        self.assertIsNone(manual_url('https://manuais.pages.example.edu.evil.test/a/index.html'))
        self.assertIsNone(manual_url('https://manuais.pages.example.edu/a/index.html?token=secret'))
        self.assertEqual(manual_url('http://manuais.example.edu/index.php/A'), 'https://manuais.example.edu/index.php/A')
        self.assertIsNone(manual_url('https://user@manuais.example.edu/index.php/A'))
        self.assertIsNone(manual_url('https://manuais.example.edu/index.php?title=A&action=edit'))

    def test_selects_content_not_navigation(self):
        doc = Document('<nav>Menu</nav><main><article>Contenido <script>secreto</script></article></main>')
        self.assertEqual(content_node(doc).text().strip(), 'Contenido')
        self.assertIsNone(content_node(Document('<main>sin estructura</main>')))


if __name__ == '__main__':
    unittest.main()
