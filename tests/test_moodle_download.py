import argparse
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import BaseHandler
from urllib.response import addinfourl

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


class UrlOpener:
    """Responde por URL para no depender del orden de la cola."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses[request.full_url]
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


class CookieJarTransportTests(unittest.TestCase):
    def setUp(self):
        from scripts.moodle_auth import save_private
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.json"
        save_private(self.path, {"name": "MoodleSession", "value": "original"})

    def client(self, replies):
        class HTTPS(BaseHandler):
            handler_order = 50
            def __init__(self):
                self.requests = []
                self.replies = iter(replies)

            def https_open(self, request):
                self.requests.append(request)
                code, headers, body = next(self.replies)
                response = addinfourl(io.BytesIO(body), headers, request.full_url, code)
                response.msg = "test"
                return response
        transport = HTTPS()
        client = Client(DEFAULT_COURSE, "unused", delay=0, session_file=self.path)
        client.opener.add_handler(transport)
        return client, transport

    def headers(self, **values):
        headers = Message()
        for key, value in values.items():
            headers[key.replace("_", "-")] = value
        return headers

    def test_redirect_set_cookie_is_sent_and_persisted(self):
        client, transport = self.client([
            (302, self.headers(Location="/pluginfile.php/12/mod_resource/content/1/file.pdf", Set_Cookie="MoodleSession=rotated; Path=/; Secure; HttpOnly"), b""),
            (200, self.headers(Content_Type="application/pdf"), b"%PDF-test"),
        ])
        client.get(DEFAULT_COURSE)
        self.assertEqual(transport.requests[0].get_header("Cookie"), "MoodleSession=original")
        self.assertEqual(transport.requests[1].get_header("Cookie"), "MoodleSession=rotated")
        self.assertEqual(load_cookie(self.path), "MoodleSession=rotated")

    def test_cross_origin_does_not_send_or_persist_rotated_cookie(self):
        client, transport = self.client([
            (302, self.headers(Location="https://attacker.invalid/", Set_Cookie="MoodleSession=untrusted; Path=/; Secure"), b""),
        ])
        with self.assertRaises(ImportFailure):
            client.get(DEFAULT_COURSE)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(load_cookie(self.path), "MoodleSession=original")

    def test_login_response_does_not_overwrite_session(self):
        client, _ = self.client([
            (200, self.headers(Content_Type="text/html", Set_Cookie="MoodleSession=anonymous; Path=/; Secure"), b'<html><input type="password"></html>'),
        ])
        with self.assertRaises(SessionExpired):
            client.get(DEFAULT_COURSE)
        self.assertEqual(load_cookie(self.path), "MoodleSession=original")


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

    def test_outer_section_heading_wins_over_nested_activity_list(self):
        doc = Document('''<main><li class="section"><h3 class="sectionname">UD1 Linux</h3>
        <ul class="section img-text"><h3>Tareas</h3>
        <a href="/mod/resource/view.php?id=4">Práctica</a></ul></li></main>''')
        found = list(links(doc, DEFAULT_COURSE, DEFAULT_COURSE))
        self.assertEqual(found[0]["section"], "UD1 Linux")

    def test_main_ignores_inactive_template_and_hidden_candidates(self):
        doc = Document('''<template><main id="region-main">
          <a href="/mod/resource/view.php?id=1">Plantilla</a></main></template>
          <div hidden><main id="region-main">
          <a href="/mod/resource/view.php?id=2">Oculto</a></main></div>
          <main><a href="/mod/resource/view.php?id=3">Visible</a></main>''')
        self.assertEqual(doc.main().tag, "main")
        found = list(links(doc, DEFAULT_COURSE, DEFAULT_COURSE))
        self.assertEqual([item["url"].rsplit("=", 1)[-1] for item in found], ["3"])

    def test_region_main_takes_precedence_over_an_earlier_main(self):
        doc = Document('''<main><a href="/mod/resource/view.php?id=1">Fuera</a></main>
          <div id="region-main"><a href="/mod/resource/view.php?id=2">Dentro</a></div>''')
        found = list(links(doc, DEFAULT_COURSE, DEFAULT_COURSE))
        self.assertEqual([item["url"].rsplit("=", 1)[-1] for item in found], ["2"])

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
        base = "https://moodle.example.edu"
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

    def test_resource_redirect_to_pluginfile_html_is_saved_without_visiting_its_links(self):
        course = b'<main><a href="/mod/resource/view.php?id=4">Example</a></main>'
        headers = Message()
        headers["Location"] = "/pluginfile.php/1/mod_resource/content/0/example.html"
        redirect = HTTPError("https://moodle.example.edu/mod/resource/view.php?id=4", 302, "redirect", headers, io.BytesIO())
        source = b'<html><body><a href="/pluginfile.php/2/mod_resource/content/0/other.pdf">other</a></body></html>'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            client = self.client([Response(course), redirect, Response(source), Response(b"%PDF-other", "application/pdf")])
            self.assertEqual(run(self.args(output), client), 0)
            report = json.loads((output / "manifest.json").read_text())
            item = next(i for i in report["items"] if i["kind"] == "resource")
            self.assertEqual((output / item["path"]).read_bytes(), source)
            self.assertEqual(client.requests, 3)
            self.assertEqual(len([i for i in report["items"] if i["status"] == "downloaded"]), 1)

    def test_section_page_cannot_enqueue_an_unscoped_section(self):
        course = b'<main><li class="section" data-sectionname="UD1">\n          <a href="/course/section.php?id=10">UD1</a></li></main>'
        section = b'<main><a href="/course/section.php?id=999">Otro curso</a>\n          <a href="/pluginfile.php/1/mod_resource/content/0/current.pdf">Actual</a></main>'
        other = b'<main><a href="/pluginfile.php/1/mod_resource/content/0/other.pdf">Ajeno</a></main>'
        base = "https://moodle.example.edu"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            client = Client(DEFAULT_COURSE, "MoodleSession=test", delay=0)
            client.opener = UrlOpener({
                DEFAULT_COURSE: Response(course),
                base + "/course/section.php?id=10": Response(section),
                base + "/course/section.php?id=999": Response(other),
                base + "/pluginfile.php/1/mod_resource/content/0/current.pdf": Response(b"%PDF-current", "application/pdf"),
                base + "/pluginfile.php/1/mod_resource/content/0/other.pdf": Response(b"%PDF-other", "application/pdf"),
            })
            self.assertEqual(run(self.args(output), client), 0)
            report = json.loads((output / "manifest.json").read_text())
            self.assertEqual(client.requests, 3)
            self.assertNotIn("section.php?id=999", json.dumps(report))
            self.assertNotIn("other.pdf", json.dumps(report))
            current = next(i for i in report["items"] if i.get("filename") == "current.pdf")
            self.assertEqual((output / current["path"]).read_bytes(), b"%PDF-current")

    def test_inventory_queue_scans_course_sections_and_lists_their_links(self):
        course = b'<main><li class="section" data-sectionname="UD1">\n          <a href="/course/section.php?id=10">UD1</a></li></main>'
        section = b'<main><a href="/pluginfile.php/1/mod_resource/content/0/current.pdf">Actual</a></main>'
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client([Response(course), Response(section)])
            self.assertEqual(run(self.args(Path(tmp), "inventory"), client), 0)
            report = json.loads((Path(tmp) / "inventory.json").read_text())
            self.assertEqual(client.requests, 2)
            self.assertEqual([item["status"] for item in report["items"]], ["scanned", "scanned", "discovered"])
            self.assertEqual(report["items"][1]["kind"], "section")
            self.assertEqual(report["items"][2]["section"], "UD1")

    def test_inventory_opens_module_views_but_never_requests_file_payload(self):
        course = b'<main><a href="/mod/resource/view.php?id=4">Guia</a></main>'
        resource = b'<main><a href="/pluginfile.php/1/mod_resource/content/0/guia.pdf">Descargar</a></main>'
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client([Response(course), Response(resource)])
            self.assertEqual(run(self.args(Path(tmp), "inventory"), client), 0)
            report = json.loads((Path(tmp) / "inventory.json").read_text())
            self.assertEqual(client.requests, 2)
            self.assertEqual([item["status"] for item in report["items"]], ["scanned", "scanned", "discovered"])
            self.assertEqual(report["items"][1]["kind"], "resource")
            self.assertEqual(report["items"][2]["kind"], "file")
            self.assertFalse((Path(tmp) / "objects").exists())

    def test_inventory_redirect_to_file_does_not_read_or_store_body(self):
        course = b'<main><a href="/mod/resource/view.php?id=4">Guia</a></main>'
        headers = Message()
        headers["Location"] = "/pluginfile.php/1/mod_resource/content/0/guia.pdf"
        redirect = HTTPError("https://moodle.example.edu/mod/resource/view.php?id=4", 302, "redirect", headers, io.BytesIO())

        class NoReadResponse(Response):
            def read(self, *args, **kwargs):
                raise AssertionError("inventory read a file payload")

        with tempfile.TemporaryDirectory() as tmp:
            client = self.client([Response(course), redirect, NoReadResponse(b"%PDF-secret", "application/pdf")])
            self.assertEqual(run(self.args(Path(tmp), "inventory"), client), 0)
            report = json.loads((Path(tmp) / "inventory.json").read_text())
            item = next(item for item in report["items"] if item["kind"] == "resource")
            self.assertEqual(item["status"], "discovered")
            self.assertIn("pluginfile.php", item["resolved_url"])
            self.assertFalse((Path(tmp) / "objects").exists())

    def test_download_hash_change_and_no_duplicate_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for expected in ["new", "unchanged"]:
                client = self.client([Response(self.COURSE), Response(b"%PDF-test", "application/pdf")])
                self.assertEqual(run(self.args(output), client), 0)
                report = json.loads((output / "manifest.json").read_text())
                item = next(i for i in report["items"] if i["status"] == "downloaded")
                self.assertEqual(item["change"], expected)
                self.assertIs(item["moodle_active"], True)
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
            resource = b'<main><a href="/pluginfile.php/1/mod_resource/content/0/lesson.pdf">Descargar</a></main>'
            client = self.client([Response(self.COURSE), Response(resource)])
            run(self.args(Path(tmp), "inventory"), client)
            self.assertEqual(client.requests, 2)
            self.assertFalse((Path(tmp) / "objects").exists())
            report = json.loads((Path(tmp) / "inventory.json").read_text())
            self.assertEqual([item["status"] for item in report["items"]], ["scanned", "scanned", "discovered"])

    def test_subject_and_excluded_url_are_recorded_without_visiting_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(Path(tmp))
            args.subject = "Bases de Datos"
            args.exclude_url = ["https://moodle.example.edu/mod/resource/view.php?id=4"]
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
