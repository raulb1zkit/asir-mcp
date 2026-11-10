import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts import manage_tokens
from scripts.mcp_auth import StaticTokenVerifier, TokenConfigError, TokenStore


def arguments(**values):
    base = {"file": None, "label": "grupo", "subjects": "ASO", "scopes": "asir:read", "rpm": 30, "days": 30}
    base.update(values)
    return argparse.Namespace(**base)


class TokenManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"

    def create(self, **values):
        return manage_tokens.create(arguments(file=self.path, **values))

    async def test_created_token_is_hashed_stored_private_and_usable(self):
        created = self.create()
        self.assertTrue(created["token"])
        self.assertEqual(created["sha256"], hashlib.sha256(created["token"].encode()).hexdigest())
        self.assertEqual(created["id"], "tok_" + created["sha256"][:12])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn(created["token"], self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["tokens"][0]["sha256"], created["sha256"])

        verifier = StaticTokenVerifier(TokenStore(self.path), "https://asir.example/mcp")
        access = await verifier.verify_token(created["token"])
        self.assertIsNotNone(access)
        self.assertEqual(access.claims["subjects"], ["ASO"])
        self.assertEqual(access.scopes, ["asir:read"])
        self.assertIsNone(await verifier.verify_token(created["sha256"]))

    async def test_list_never_returns_the_secret_or_its_hash(self):
        created = self.create(label="alumno")
        listed = manage_tokens.list_tokens(arguments(file=self.path))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["label"], "alumno")
        self.assertNotIn("token", listed[0])
        self.assertNotIn("sha256", listed[0])
        self.assertNotIn(created["token"], json.dumps(listed))

    async def test_revoke_removes_only_the_requested_token(self):
        first = self.create(label="uno")
        second = self.create(label="dos", subjects="ASO,FOL")
        result = manage_tokens.revoke(arguments(file=self.path, id=first["id"]))
        self.assertEqual(result, {"revoked": first["id"], "remaining": 1})
        verifier = StaticTokenVerifier(TokenStore(self.path), "https://asir.example/mcp")
        self.assertIsNone(await verifier.verify_token(first["token"]))
        self.assertIsNotNone(await verifier.verify_token(second["token"]))
        with self.assertRaisesRegex(TokenConfigError, "token_not_found"):
            manage_tokens.revoke(arguments(file=self.path, id=first["id"]))

    def test_invalid_arguments_do_not_create_a_file(self):
        for values in [
            {"subjects": ""},
            {"subjects": "ASO\x00"},
            {"subjects": "A" * 41},
            {"scopes": ""},
            {"scopes": "INVALID"},
        ]:
            with self.subTest(values=values):
                with self.assertRaises(TokenConfigError):
                    self.create(**values)
                self.assertFalse(self.path.exists())

    def test_existing_file_with_loose_permissions_is_refused(self):
        self.create()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(TokenConfigError, "600"):
            manage_tokens.list_tokens(arguments(file=self.path))
