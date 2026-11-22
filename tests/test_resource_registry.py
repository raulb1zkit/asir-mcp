"""Registro de identidades estables: identidad por URL/miembro, fail-closed."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts import rag_resource_registry as reg
from scripts.import_supabase import source_key


def source(subject, url, sha, filename, mime, title='t', section='s', path='p', moodle_active=True):
    return {'subject': subject, 'url': url, 'sha256': sha, 'filename': filename,
            'mime': mime, 'title': title, 'section': section, 'path': path,
            'moodle_active': moodle_active}


def chunks_file(directory, name, *sources):
    path = Path(directory) / name
    line = {'id': 'chunk:' + name, 'citations': [{'source': s} for s in sources]}
    path.write_text(json.dumps(line) + '\n', encoding='utf-8')
    return path


class RegistryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.course = self.root / 'course.json'
        self.course.write_text(json.dumps({
            'course_url': 'https://campus.example.es/course/view.php?id=7',
            'subject': 'ASO',
            'items': [
                {'url': 'https://campus.example.es/mod/page/view.php?id=101', 'sha256': 'a' * 64,
                 'filename': 'tema1.html', 'mime': 'text/html'},
                {'url': 'https://campus.example.es/pluginfile.php/55/mod_folder/content/0/manual.pdf',
                 'sha256': 'b' * 64, 'filename': 'manual.pdf', 'mime': 'application/pdf'},
                {'url': 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip',
                 'sha256': 'c' * 64, 'filename': 'guia.zip', 'mime': 'application/g-zip'},
                {'url': 'https://drive.google.com/file/d/xyz/view', 'sha256': 'd' * 64,
                 'filename': 'externo.pdf', 'mime': 'application/pdf'},
            ],
        }), encoding='utf-8')

    def run_registry(self, chunks, *extra, check=None, external=None):
        registry = reg.Registry()
        registry.load_course_manifest(self.course)
        for path in extra:
            registry.load_archive_manifest(path)
        if external:
            registry.load_external(external)
        return registry.build(chunks)

    def test_module_and_pluginfile_identities_drop_numeric_revisions(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/mod/page/view.php?id=101', 'a' * 64, 'tema1.html', 'text/html'),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_folder/content/0/manual.pdf', 'b' * 64, 'manual.pdf', 'application/pdf'))
        mapping, summary = self.run_registry([chunks])
        self.assertEqual(mapping['https://campus.example.es/mod/page/view.php?id=101'], 'moodle:c7:page:101')
        self.assertEqual(mapping['https://campus.example.es/pluginfile.php/55/mod_folder/content/0/manual.pdf'],
                         'moodle:c7:pf:55:mod_folder/content/manual.pdf')

    def test_zip_members_get_individual_source_keys_and_no_url_alias(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm1' * 32, 'docs/uno.md', 'text/plain'),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm2' * 32, 'dos.sql', 'text/plain'))
        mapping, summary = self.run_registry([chunks])
        base = 'moodle:c7:pf:55:mod_resource/content/guia.zip'
        self.assertEqual(mapping[source_key({'subject': 'ASO', 'url': 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip',
                                             'sha256': 'm1' * 32, 'filename': 'docs/uno.md', 'mime': 'text/plain',
                                             'title': 't', 'section': 's', 'path': 'p', 'moodle_active': True})],
                         base + '#docs/uno.md')
        self.assertEqual(mapping[source_key({'subject': 'ASO', 'url': 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip',
                                             'sha256': 'm2' * 32, 'filename': 'dos.sql', 'mime': 'text/plain',
                                             'title': 't', 'section': 's', 'path': 'p', 'moodle_active': True})],
                         base + '#dos.sql')
        self.assertNotIn('https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', mapping)
        self.assertEqual(summary['url_aliases'], 0)

    def test_external_requires_operator_registration(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://drive.google.com/file/d/xyz/view', 'd' * 64, 'externo.pdf', 'application/pdf'))
        with self.assertRaisesRegex(reg.RegistryError, 'external_resource_unregistered'):
            self.run_registry([chunks])
        external = self.root / 'external.json'
        external.write_text(json.dumps({'https://drive.google.com/file/d/xyz/view': 'external:manual-sri-1'}))
        mapping, _ = self.run_registry([chunks], external=external)
        self.assertEqual(mapping['https://drive.google.com/file/d/xyz/view'], 'external:manual-sri-1')

    def test_unrecognised_route_on_course_host_fails_closed(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/weird/route/file.pdf', 'e' * 64, 'file.pdf', 'application/pdf'))
        registry = reg.Registry()
        registry.load_course_manifest(self.course)
        with self.assertRaisesRegex(reg.RegistryError, 'course_host_route_unrecognised'):
            registry.build([chunks])

    def test_ambiguities_fail_closed_with_private_diagnostics(self):
        # Miembro ambiguo: misma ruta de miembro, contenido distinto.
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm1' * 32, 'dos.sql', 'text/plain'),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm3' * 32, 'dos.sql', 'text/plain'))
        registry = reg.Registry()
        registry.load_course_manifest(self.course)
        with self.assertRaisesRegex(reg.RegistryError, 'ambiguous_archive_member'):
            registry.build([chunks])
        output = self.root / 'map.json'
        diagnostics = registry.write_conflicts(output)
        self.assertTrue(str(diagnostics).endswith('map.json.conflicts.json'))
        payload = json.loads(diagnostics.read_text())
        self.assertIn('ambiguous_archive_member', payload['conflicts'])

    def test_inactive_revision_can_coexist_with_one_active_archive_member(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm1' * 32, 'docs/uno.md', 'text/plain', moodle_active=False),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm2' * 32, 'docs/uno.md', 'text/plain', moodle_active=True))
        mapping, summary = self.run_registry([chunks])
        keys = [key for key in mapping if len(key) == 64]
        self.assertEqual(len(keys), 2)
        # La revisión retenida y la activa comparten identidad lógica estable;
        # el sha nunca forma parte del identificador del recurso.
        self.assertEqual(len({mapping[key] for key in keys}), 1)
        self.assertEqual(summary['url_aliases'], 0)

    def test_member_ambiguity_key_includes_subject(self):
        other = self.root / 'course2.json'
        other.write_text(json.dumps({
            'course_url': 'https://campus.example.es/course/view.php?id=9',
            'subject': 'SRI',
            'items': [{'url': 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'sha256': 'c' * 64, 'filename': 'guia.zip', 'mime': 'application/g-zip'}],
        }), encoding='utf-8')
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm1' * 32, 'dos.sql', 'text/plain'),
                             source('SRI', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm3' * 32, 'dos.sql', 'text/plain'))
        registry = reg.Registry()
        registry.load_course_manifest(self.course)
        registry.load_course_manifest(other)
        mapping, summary = registry.build([chunks])
        keys = [key for key in mapping if len(key) == 64]
        self.assertEqual(len(keys), 2)
        # Dos revisiones activas del mismo miembro lógico en cursos distintos
        # no son ambigüedad: la asignatura forma parte de la clave.
        self.assertEqual(len({mapping[key] for key in keys}), 2)
        self.assertNotIn('https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', mapping)
        self.assertEqual(summary['url_aliases'], 0)

    def test_edited_zip_member_keeps_identity_across_container_revision(self):
        # El contenedor editado pasa de revisión 8 a 9 en la URL; los segmentos
        # numéricos se descartan y cada miembro conserva su identidad estable.
        course = self.root / 'course-zip.json'
        course.write_text(json.dumps({
            'course_url': 'https://campus.example.es/course/view.php?id=7',
            'subject': 'ASO',
            'items': [
                {'url': 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/pack.zip', 'sha256': 'k' * 64, 'filename': 'pack.zip', 'mime': 'application/g-zip'},
                {'url': 'https://campus.example.es/pluginfile.php/55/mod_resource/content/9/pack.zip', 'sha256': 'l' * 64, 'filename': 'pack.zip', 'mime': 'application/g-zip'},
            ],
        }), encoding='utf-8')
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/pack.zip', 'm1' * 32, 'docs/uno.md', 'text/plain', moodle_active=False),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/9/pack.zip', 'm2' * 32, 'docs/uno.md', 'text/plain', moodle_active=True),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/9/pack.zip', 'm4' * 32, 'dos.sql', 'text/plain', moodle_active=True))
        registry = reg.Registry()
        registry.load_course_manifest(course)
        mapping, _ = registry.build([chunks])
        keys = [key for key in mapping if len(key) == 64]
        self.assertEqual(len(keys), 3)
        self.assertEqual({mapping[key] for key in keys}, {'moodle:c7:pf:55:mod_resource/content/pack.zip#docs/uno.md',
                                                          'moodle:c7:pf:55:mod_resource/content/pack.zip#dos.sql'})

    def test_two_active_archive_revisions_remain_ambiguous(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm1' * 32, 'docs/uno.md', 'text/plain', moodle_active=True),
                             source('ASO', 'https://campus.example.es/pluginfile.php/55/mod_resource/content/8/guia.zip', 'm2' * 32, 'docs/uno.md', 'text/plain', moodle_active=True))
        with self.assertRaisesRegex(reg.RegistryError, 'ambiguous_archive_member'):
            self.run_registry([chunks])

    def test_subject_without_course_and_cross_course_url_fail_closed(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('SRI', 'https://campus.example.es/mod/page/view.php?id=101', 'f' * 64, 'x.html', 'text/html'))
        with self.assertRaisesRegex(reg.RegistryError, 'subject_without_registered_course'):
            self.run_registry([chunks])

    def test_check_mode_detects_missing_keys(self):
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/mod/page/view.php?id=101', 'a' * 64, 'tema1.html', 'text/html'))
        mapping, _ = self.run_registry([chunks])
        good = self.root / 'good.json'
        good.write_text(json.dumps(mapping))
        stale = self.root / 'stale.json'
        stale.write_text(json.dumps({'clave-inexistente': 'moodle:c1:x:1'}))
        result = reg.check(good, mapping)
        self.assertEqual(result['missing_keys'], [])
        self.assertEqual(result['changed_keys'], [])
        stale_result = reg.check(stale, mapping)
        self.assertEqual(len(stale_result['missing_keys']), len(mapping))

    def test_shared_url_across_courses_gets_no_alias_but_individual_keys(self):
        other = self.root / 'course2.json'
        other.write_text(json.dumps({
            'course_url': 'https://campus.example.es/course/view.php?id=9',
            'subject': 'SRI',
            'items': [{'url': 'https://campus.example.es/mod/page/view.php?id=101', 'sha256': 'z' * 64, 'filename': 'otro.html', 'mime': 'text/html'}],
        }), encoding='utf-8')
        chunks = chunks_file(self.root, 'c.jsonl',
                             source('ASO', 'https://campus.example.es/mod/page/view.php?id=101', 'a' * 64, 'tema1.html', 'text/html'),
                             source('SRI', 'https://campus.example.es/mod/page/view.php?id=101', 'z' * 64, 'otro.html', 'text/html'))
        registry = reg.Registry()
        registry.load_course_manifest(self.course)
        registry.load_course_manifest(other)
        mapping, summary = registry.build([chunks])
        self.assertNotIn('https://campus.example.es/mod/page/view.php?id=101', mapping)
        self.assertEqual(summary['url_aliases'], 0)
        self.assertEqual(summary['source_keys'], 2)


class RegistryCliTests(unittest.TestCase):
    def test_main_writes_private_map_and_reports_safe_summary(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('course.json').write_text(json.dumps({
                'course_url': 'https://campus.example.es/course/view.php?id=7',
                'subject': 'ASO',
                'items': [{'url': 'https://campus.example.es/mod/page/view.php?id=101', 'sha256': 'a' * 64, 'filename': 'tema1.html', 'mime': 'text/html'}],
            }))
            root.joinpath('chunks.jsonl').write_text(json.dumps({
                'id': 'chunk:x',
                'citations': [{'source': source('ASO', 'https://campus.example.es/mod/page/view.php?id=101', 'a' * 64, 'tema1.html', 'text/html')}],
            }) + '\n')
            output = root / 'map.json'
            result = subprocess.run([sys.executable, '-m', 'scripts.rag_resource_registry',
                                     '--chunks', str(root / 'chunks.jsonl'),
                                     '--course-manifest', str(root / 'course.json'),
                                     '--output', str(output)], capture_output=True, text=True, cwd='.')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary['entries'], 2)  # source_key + alias por URL
            self.assertEqual(summary['url_aliases'], 1)
            self.assertEqual(summary['source_keys'], 1)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            mapping = json.loads(output.read_text())
            self.assertEqual(mapping['https://campus.example.es/mod/page/view.php?id=101'], 'moodle:c7:page:101')

    def test_main_fails_closed_without_writing_map(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('course.json').write_text(json.dumps({
                'course_url': 'https://campus.example.es/course/view.php?id=7',
                'subject': 'ASO',
                'items': [],
            }))
            root.joinpath('chunks.jsonl').write_text(json.dumps({
                'id': 'chunk:x',
                'citations': [{'source': source('ASO', 'https://drive.google.com/file/d/xyz/view', 'd' * 64, 'externo.pdf', 'application/pdf')}],
            }) + '\n')
            output = root / 'map.json'
            result = subprocess.run([sys.executable, '-m', 'scripts.rag_resource_registry',
                                     '--chunks', str(root / 'chunks.jsonl'),
                                     '--course-manifest', str(root / 'course.json'),
                                     '--output', str(output)], capture_output=True, text=True, cwd='.')
            self.assertEqual(result.returncode, 1)
            summary = json.loads(result.stdout)
            self.assertEqual(summary['error'], 'external_resource_unregistered')
            self.assertEqual(summary['external_resource_unregistered'], 1)
            self.assertFalse(output.exists())
            diagnostics = root / 'map.json.conflicts.json'
            self.assertTrue(diagnostics.exists())
            self.assertEqual(diagnostics.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
