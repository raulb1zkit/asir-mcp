import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from email.message import Message
from scripts.moodle_auth import AuthError, WebSession, ensure_session, private_json, save_private
from scripts.moodle_download import Document, load_cookie

COURSE = 'https://moodle.example/course/view.php?id=208'
FORM = '<form method="post" action="/login/index.php"><input name="logintoken" type="hidden" value="csrf"><input name="username"><input name="password" type="password"></form>'


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.json'
        save_private(self.path, {'name': 'MoodleSession', 'value': 'test-secret'})

    def test_private_and_legacy_cookie(self):
        self.assertEqual(load_cookie(self.path), 'MoodleSession=test-secret')
        web = WebSession(COURSE, self.path)
        web.save()
        self.assertEqual(load_cookie(self.path), 'MoodleSession=test-secret')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.chmod(0o644)
        with self.assertRaises(AuthError):
            WebSession(COURSE, self.path)

    def test_symlink_rejected(self):
        link = self.path.with_name('link')
        link.symlink_to(self.path)
        with self.assertRaises(AuthError):
            save_private(link, {})
        with self.assertRaises(AuthError):
            private_json(link)

    def test_anonymous_course_not_authenticated(self):
        web = WebSession(COURSE, self.path)
        with patch.object(web, 'request', return_value=(COURSE, Document('<main>public course</main>'))):
            self.assertFalse(web.check())
        with patch.object(web, 'request', return_value=(COURSE, Document('<a href="/login/logout.php?sesskey=x">Logout</a>'))):
            self.assertTrue(web.check())

    def test_form_login_csrf_and_check(self):
        web = WebSession(COURSE, self.path)
        with patch.object(web, 'request', side_effect=[('https://moodle.example/login/index.php', Document(FORM + '<form method="post"><input type="hidden" name="password" value="guest"></form>')), (COURSE, Document('')), (COURSE, Document('<a href="/login/logout.php">logout</a>'))]) as request:
            web.renew({'username': 'u', 'password': 'p'})
        self.assertIn(b'logintoken=csrf', request.call_args_list[1].args[1])

    def test_external_form_and_captcha_rejected(self):
        web = WebSession(COURSE, self.path)
        for form in [FORM.replace('/login/index.php', 'https://evil.example/login'), FORM.replace('logintoken', 'captcha')]:
            with self.subTest(form=form), patch.object(web, 'request', return_value=(COURSE, Document(form))):
                with self.assertRaises(AuthError):
                    web.renew({'username': 'u', 'password': 'p'})

    def test_cross_origin_redirect_no_follow(self):
        web = WebSession(COURSE, self.path)
        headers = Message()
        headers['Location'] = 'https://evil.example/'
        error = HTTPError(COURSE, 302, '', headers, io.BytesIO())
        with patch.object(web.opener, 'open', side_effect=error) as opened:
            with self.assertRaises(AuthError):
                web.request(COURSE)
        self.assertEqual(opened.call_count, 1)

    def test_failed_renewal_keeps_previous_session(self):
        previous = self.path.read_bytes()
        credentials = self.path.with_name('credentials.json')
        save_private(credentials, {'username': 'u', 'password': 'p'})
        with patch.object(WebSession, 'check', return_value=False), patch.object(WebSession, 'renew', side_effect=AuthError('rejected')):
            with self.assertRaises(AuthError):
                ensure_session(self.path, credentials, COURSE)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_cookie_rotation_persisted(self):
        web = WebSession(COURSE, self.path)
        cookie = next(iter(web.jar))
        cookie.value = 'rotated'
        web.save()
        self.assertEqual(load_cookie(self.path), 'MoodleSession=rotated')
        restored = WebSession(COURSE, self.path)
        self.assertEqual(next(iter(restored.jar)).value, 'rotated')

    def test_unsafe_origin_rejected(self):
        with self.assertRaises(AuthError):
            WebSession(COURSE.replace('https:', 'http:'), self.path)


if __name__ == '__main__':
    unittest.main()
