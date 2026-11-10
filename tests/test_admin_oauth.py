import base64
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from scripts.admin_oauth import make_handler
from scripts.manage_oauth_users import initialize, load

USER = "d834aafa-b060-45df-b64d-3adab57cb091"


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "users.json"
        initialize(self.path)
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.path, "password", "csrf-test", 8765))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.auth = "Basic " + base64.b64encode(b"admin:password").decode()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, body=None, **headers):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        headers.setdefault("Host", "localhost:8765")
        connection.request(method, "/", body, headers)
        response = connection.getresponse()
        status, text = response.status, response.read().decode()
        connection.close()
        return status, text

    def test_requires_auth_and_correct_host(self):
        self.assertEqual(self.request("GET")[0], 401)
        self.assertEqual(self.request("GET", Authorization=self.auth, Host="evil.example")[0], 400)
        self.assertEqual(self.request("GET", Authorization=self.auth)[0], 200)

    def test_csrf_grant_and_revoke(self):
        body = urlencode({"csrf": "csrf-test", "user": USER, "action": "grant", "subject": "ASO"})
        headers = {"Authorization": self.auth, "Content-Type": "application/x-www-form-urlencoded", "Origin": "http://localhost:8765"}
        self.assertEqual(self.request("POST", body, **{**headers, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", body.replace("csrf-test", "wrong"), **headers)[0], 400)
        self.assertEqual(load(self.path)["users"], [])
        self.assertEqual(self.request("POST", body, **headers)[0], 303)
        self.assertEqual(load(self.path)["users"][0]["subjects"], ["ASO"])
        revoke = urlencode({"csrf": "csrf-test", "user": USER, "action": "revoke"})
        self.assertEqual(self.request("POST", revoke, **headers)[0], 303)
        self.assertEqual(load(self.path)["users"], [])


if __name__ == "__main__":
    unittest.main()
