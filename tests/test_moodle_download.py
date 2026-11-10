import argparse
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from scripts.moodle_download import (
    Client, DEFAULT_COURSE, Document, ImportFailure, SessionExpired,
    canonical, kind, links, load_cookie, run,
)


class Response(io.BytesIO):
    def __init__(self, data, mime="text/html"):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = mime


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class TransportTests(unittest.TestCase):
    def client(self, responses):
        client = Client(DEFAULT_COURSE, "MoodleSession=test-secret", delay=0)
        client.opener = Opener(responses)
        return client

    def test_cross_origin_redirect_never_receives_cookie(self):
        headers = Message()
        headers["Location"] = "https://attacker.invalid/file.pdf"
        error = HTTPError(DEFAULT_COURSE, 302, "redirect", headers, io.BytesIO())
        client = self.client([error])
        with self.assertRaisesRegex(ImportFailure, "otro origen"):
            client.get(DEFAULT_COURSE)
        self.assertEqual(len(client.opener.requests), 1)
        self.assertEqual(client.opener.requests[0].get_header("Cookie"), "MoodleSession=test-secret")

    def test_login_html_mislabeled_as_pdf_is_rejected(self):
        response = Response(b'<html><input type="password" name="password"></html>', "application/pdf")
        with self.assertRaises(SessionExpired):
            self.client([response]).get(DEFAULT_COURSE)

    def test_login_redirect_is_not_requested(self):
        headers = Message()
        headers["Location"] = "/login/index.php"
        error = HTTPError(DEFAULT_COURSE, 303, "redirect", headers, io.BytesIO())
        client = self.client([error])
        with self.assertRaises(SessionExpired):
            client.get(DEFAULT_COURSE)
        self.assertEqual(len(client.opener.requests), 1)

    def test_same_origin_file_redirect(self):
        headers = Message()
        headers["Location"] = "/pluginfile.php/12/mod_resource/content/1/apuntes.pdf"
        error = HTTPError(DEFAULT_COURSE, 302, "redirect", headers, io.BytesIO())
        result = self.client([error, Response(b"%PDF-test", "application/pdf")]).get(DEFAULT_COURSE)
        self.assertEqual(result["content"], b"%PDF-test")

    def test_rate_limit_is_not_retried(self):
        client = self.client([HTTPError(DEFAULT_COURSE, 429, "limit", Message(), io.BytesIO())])
        with self.assertRaisesRegex(ImportFailure, "429"):
            client.get(DEFAULT_COURSE)
        self.assertEqual(len(client.opener.requests), 1)


class DiscoveryTests(unittest.TestCase):
    def test_embedded_resource_image_is_discovered_but_profile_image_is_not(self):
        doc = Document('''<main><img src="/pluginfile.php/1/mod_resource/content/1/diagram.png">
          <img src="/pluginfile.php/2/user/icon/profile.png"></main>''')
        found = list(links(doc, DEFAULT_COURSE, DEFAULT_COURSE))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "file")

    def test_moodle_section_label_wins_over_nested_activity_heading(self):
        doc = Document('''<main><li class="section" data-sectionname="UD1 Procesos">
        <h3 class="sectionname">UD1 Procesos</h3><ul class="section"><h3>Tareas</h3>
        <a href="/mod/resource/view.php?id=4&amp;forceview=1">Práctica</a></ul></li></main>''')
        found = list(links(doc, DEFAULT_COURSE, DEFAULT_COURSE))
        self.assertEqual(found[0]["section"], "UD1 Procesos")
        self.assertTrue(found[0]["url"].endswith("?id=4"))

    def test_course_scope_actions_and_section_metadata(self):
        doc = Document('''<main><li class="section"><h3>UD1 Linux</h3>
          <ul class="section"><li><a href="/mod/resource/view.php?id=4">Teoría</a></li></ul>
          <a href="/mod/resource/view.php?id=5&amp;sesskey=SECRET">Acción</a>
          <a href="/course/view.php?id=999">Otro curso</a>
          <a href="https://other.invalid/mod/resource/view.php?id=8">Externo</a>
          <a href="/mod/quiz/view.php?id=9">Cuestionario</a>
        </li></main>''')
        found = list(links(doc, DEFAULT_COURSE, DEFAULT_COURSE))
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]["section"], "UD1 Linux")
        self.assertEqual(found[1]["kind"], "unsupported")
        self.assertNotIn("SECRET", json.dumps(found))

    def test_submissions_and_profile_files_are_excluded(self):
        base = "https://moodle.example.org"
        self.assertIsNone(kind(base + "/pluginfile.php/1/assignsubmission_file/submission_files/1/a.pdf", DEFAULT_COURSE))
        self.assertIsNone(kind(base + "/pluginfile.php/1/user/private/a.pdf", DEFAULT_COURSE))

    def test_cookie_permissions_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text('{"name":"MoodleSession","value":"test"}')
            path.chmod(0o644)
            with self.assertRaises(ImportFailure):
                load_cookie(path)
            path.chmod(0o600)
            self.assertEqual(load_cookie(path), "MoodleSession=test")
            path.write_text('{"name":"MoodleSession","value":"test; injected=1"}')
            with self.assertRaises(ImportFailure):
                load_cookie(path)


class ImportTests(unittest.TestCase):
    COURSE = b'<main><li class="section"><h3>UD1</h3><a href="/mod/resource/view.php?id=4">Teoria</a></li></main>'

    def args(self, output, mode="download", limit=500):
        return argparse.Namespace(output=output, course_url=DEFAULT_COURSE, mode=mode, max_resources=limit)

    def client(self, data):
        client = Client(DEFAULT_COURSE, "MoodleSession=test", delay=0)
        client.opener = Opener(data)
        return client

    def test_html_file_is_saved_as_source_without_visiting_embedded_links(self):
        course = b'<main><a href="/pluginfile.php/1/mod_folder/content/0/example.html">Example</a></main>'
        source = b'<html><body><a href="/pluginfile.php/1/mod_folder/content/0/$variable">example</a></body></html>'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            client = self.client([Response(course), Response(source)])
            self.assertEqual(run(self.args(output), client), 0)
            report = json.loads((output / "manifest.json").read_text())
            self.assertEqual(client.requests, 2)
            self.assertEqual((output / report["items"][1]["path"]).read_bytes(), source)

    def test_download_hash_change_and_no_duplicate_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for expected in ["new", "unchanged"]:
                client = self.client([Response(self.COURSE), Response(b"%PDF-test", "application/pdf")])
                self.assertEqual(run(self.args(output), client), 0)
                report = json.loads((output / "manifest.json").read_text())
                item = next(i for i in report["items"] if i["status"] == "downloaded")
                self.assertEqual(item["change"], expected)
                self.assertEqual((output / item["path"]).read_bytes(), b"%PDF-test")
                self.assertEqual(item["section"], "UD1")
            self.assertEqual(len(list((output / "objects").glob("*/*"))), 1)

    def test_expired_session_preserves_previous_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            original = json.dumps({"course_url": DEFAULT_COURSE, "items": []})
            (output / "manifest.json").write_text(original)
            client = self.client([Response(b'<main><input type="password"></main>')])
            with self.assertRaises(SessionExpired):
                run(self.args(output), client)
            self.assertEqual((output / "manifest.json").read_text(), original)
            self.assertFalse(json.loads((output / "last-run.partial.json").read_text())["complete"])

    def test_inventory_does_not_download_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client([Response(self.COURSE)])
            run(self.args(Path(tmp), "inventory"), client)
            self.assertEqual(client.requests, 1)
            report = json.loads((Path(tmp) / "inventory.json").read_text())
            self.assertEqual(report["items"][1]["status"], "discovered")

    def test_subject_and_excluded_url_are_recorded_without_visiting_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(Path(tmp))
            args.subject = "Bases de Datos"
            args.exclude_url = ["https://moodle.example.org/mod/resource/view.php?id=4"]
            client = self.client([Response(self.COURSE)])
            self.assertEqual(run(args, client), 0)
            report = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(report["subject"], "Bases de Datos")
            self.assertEqual(report["items"][1]["status"], "excluded")
            self.assertEqual(client.requests, 1)

    def test_resume_reuses_verified_objects_from_partial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            first = self.client([Response(self.COURSE), Response(b"%PDF-test", "application/pdf")])
            self.assertEqual(run(self.args(output), first), 0)
            (output / "manifest.json").rename(output / "last-run.partial.json")
            args = self.args(output)
            args.resume = True
            second = self.client([Response(self.COURSE)])
            self.assertEqual(run(args, second), 0)
            report = json.loads((output / "manifest.json").read_text())
            self.assertEqual(report["items"][1]["change"], "cached")
            self.assertEqual(second.requests, 1)

    def test_assignment_only_exports_description_and_its_attachments(self):
        course = b'<main><a href="/mod/assign/view.php?id=7">Control</a></main>'
        assignment = b'''<main><div id="intro">Enunciado
          <a href="/pluginfile.php/1/mod_assign/introattachment/0/control.pdf">Archivo</a></div>
          <div class="submission">PRIVATE STUDENT DATA
          <a href="/pluginfile.php/1/assignsubmission_file/submission_files/0/secret.pdf">Entrega</a>
          <a href="/mod/resource/view.php?id=99">Fuera del enunciado</a></div></main>'''
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            client = self.client([Response(course), Response(assignment), Response(b"%PDF-enunciado", "application/pdf")])
            self.assertEqual(run(self.args(output), client), 0)
            report = json.loads((output / "manifest.json").read_text())
            self.assertEqual(client.requests, 3)
            text = next(i for i in report["items"] if i["kind"] == "assign")
            self.assertNotIn(b"PRIVATE", (output / text["path"]).read_bytes())
            self.assertNotIn("secret.pdf", json.dumps(report))

    def test_resume_revisits_assignment_to_keep_attachment_links(self):
        course = b'<main><a href="/mod/assign/view.php?id=7">Control</a></main>'
        assignment = b'<main><div id="intro">Exercise <a href="/pluginfile.php/1/mod_assign/introattachment/0/a.pdf">A</a></div></main>'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.assertEqual(run(self.args(output), self.client([Response(course), Response(assignment), Response(b"%PDF-test", "application/pdf")])), 0)
            args = self.args(output)
            args.resume = True
            client = self.client([Response(course), Response(assignment)])
            self.assertEqual(run(args, client), 0)
            report = json.loads((output / "manifest.json").read_text())
            self.assertEqual(len([i for i in report["items"] if i.get("sha256")]), 2)
            self.assertEqual(client.requests, 2)

    def test_resume_reuses_verified_objects_without_fetching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            run(self.args(output), self.client([Response(self.COURSE), Response(b"%PDF-test", "application/pdf")]))
            args = self.args(output)
            args.resume = True
            client = self.client([Response(self.COURSE)])
            self.assertEqual(run(args, client), 0)
            self.assertEqual(client.requests, 1)
            report = json.loads((output / "manifest.json").read_text())
            self.assertEqual(report["items"][1]["change"], "cached")

    def test_limit_creates_partial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client([Response(self.COURSE)])
            with self.assertRaises(ImportFailure):
                run(self.args(Path(tmp), limit=1), client)
            self.assertFalse((Path(tmp) / "manifest.json").exists())
            self.assertTrue((Path(tmp) / "last-run.partial.json").exists())


if __name__ == "__main__":
    unittest.main()
