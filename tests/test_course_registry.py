import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from course_registry import DEFAULT_REGISTRY, downloader_args, load_registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(DEFAULT_REGISTRY.read_text())

    def load(self, raw):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'courses.json'
            path.write_text(json.dumps(raw))
            return load_registry(path)

    def test_template_courses_are_synthetic(self):
        courses = {c.slug: c for c in load_registry()}
        self.assertEqual(
            {k: c.course_url.rsplit('=', 1)[1] for k, c in courses.items()},
            {'ejemplo-aso': '1001', 'ejemplo-bd': '1002', 'ejemplo-sri': '1003'})
        self.assertEqual(courses['ejemplo-aso'].exclusions[0].url,
                         'https://moodle.example.edu/mod/assign/view.php?id=24601')
        self.assertIn('excluida', courses['ejemplo-aso'].exclusions[0].reason)
        self.assertEqual(courses['ejemplo-bd'].exclusions, ())
        self.assertEqual(courses['ejemplo-sri'].preserve_sources, ())
        # La plantilla es sintética: origen de ejemplo y sin datos institucionales.
        for course in courses.values():
            self.assertTrue(course.course_url.startswith('https://moodle.example.edu/'))

    def test_downloader_is_refresh_and_preserves_all_exclusions(self):
        course = next(c for c in load_registry() if c.slug == 'ejemplo-aso')
        args = downloader_args(course, Path('/tmp/test data'), Path('/tmp/session.json'))
        self.assertEqual(args[0], 'download')
        self.assertEqual(args[args.index('--output') + 1], '/tmp/test data/ejemplo-aso/moodle')
        self.assertEqual(args[args.index('--exclude-url') + 1], course.exclusions[0].url)
        self.assertNotIn('--resume', args)
        self.assertEqual(course.downloader_args('/tmp/test data', '/tmp/session.json'), args)

    def test_rejects_urls_with_credentials_or_secret_parameters(self):
        for url in ('https://moodle.example.edu/course/view.php?id=1001&sesskey=secret',
                    'https://moodle.example.edu.evil.test/course/view.php?id=1001',
                    'https://name:secret@moodle.example.edu/course/view.php?id=1001',
                    'http://moodle.example.edu/course/view.php?id=1001'):
            raw = copy.deepcopy(self.raw)
            raw['courses'][0]['course_url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(raw)

    def test_rejects_duplicates_and_path_traversal(self):
        raw = copy.deepcopy(self.raw)
        raw['courses'].append(copy.deepcopy(raw['courses'][0]))
        with self.assertRaises(ValueError):
            self.load(raw)
        source = {'name': 'fuentes', 'kind': 'archives',
                  'manifest': 'ejemplo-aso/x-manifest.json', 'chunks': 'ejemplo-aso/x-chunks'}
        for value in ('../other', '/etc/passwd', 'ejemplo-aso/../../other', 'ejemplo-bd/x-chunks'):
            raw = copy.deepcopy(self.raw)
            raw['courses'][0]['preserve_sources'] = [dict(source, chunks=value)]
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(raw)

    def test_exclusion_cannot_request_an_action(self):
        raw = copy.deepcopy(self.raw)
        raw['courses'][0]['exclusions'] = [{'url': 'https://moodle.example.edu/login/logout.php?sesskey=x', 'reason': 'invalid'}]
        with self.assertRaises(ValueError):
            self.load(raw)


if __name__ == '__main__':
    unittest.main()
