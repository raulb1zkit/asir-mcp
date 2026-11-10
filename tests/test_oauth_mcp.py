import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import jwt
import httpx2
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from scripts.oauth_mcp import DualTokenVerifier, OAuthUserStore, SupabaseOAuthVerifier
from scripts.mcp_http import build_app
from tests.test_mcp_auth import record

ISSUER = "https://example.supabase.co/auth/v1"
RESOURCE = "https://mcp.example.org/mcp"
ORIGIN = "https://mcp.example.org"
USER = "d834aafa-b060-45df-b64d-3adab57cb091"
CLIENT = "d8e96d29-a10c-4c3d-a892-86e4d86bde78"
SESSION = "4279c8d0-2f7d-43ef-874f-dc0e2239798e"


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.json"
        self.write([{"id": USER, "subjects": ["ASO"]}])
        key = SimpleNamespace(key=self.private.public_key())
        self.verifier = SupabaseOAuthVerifier(ISSUER, RESOURCE, RESOURCE, OAuthUserStore(self.path), jwks_client=SimpleNamespace(get_signing_key_from_jwt=lambda token: key))

    def write(self, users):
        self.path.write_text(json.dumps({"version": 1, "users": users}))
        os.chmod(self.path, 0o600)

    def token(self, *, updates=None, remove=(), key=None):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": RESOURCE, "sub": USER, "client_id": CLIENT, "session_id": SESSION, "role": "authenticated", "iat": now, "exp": now + 300, "user_metadata": {"subjects": ["FOL"]}}
        claims.update(updates or {})
        for field in remove:
            claims.pop(field, None)
        return jwt.encode(claims, key or self.private, algorithm="RS256", headers={"kid": "test"})

    async def test_valid_token_uses_server_allowlist_not_user_metadata(self):
        access = await self.verifier.verify_token(self.token())
        self.assertEqual(access.claims["subjects"], ["ASO"])
        self.assertEqual(access.resource, RESOURCE)
        self.assertEqual(access.scopes, ["asir:read"])

    async def test_es256_supabase_style_signature(self):
        private = ec.generate_private_key(ec.SECP256R1())
        key = SimpleNamespace(key=private.public_key())
        verifier = SupabaseOAuthVerifier(ISSUER, RESOURCE, RESOURCE, OAuthUserStore(self.path), jwks_client=SimpleNamespace(get_signing_key_from_jwt=lambda token: key))
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": RESOURCE, "sub": USER, "client_id": CLIENT, "session_id": SESSION, "role": "authenticated", "iat": now, "exp": now + 300}
        access = await verifier.verify_token(jwt.encode(claims, private, algorithm="ES256", headers={"kid": "test"}))
        self.assertEqual(access.subject, USER)

    async def test_wrong_audience_issuer_signature_and_expiry_are_rejected(self):
        now = int(time.time())
        tokens = [self.token(updates={"aud": "authenticated"}), self.token(updates={"aud": [RESOURCE, "https://other.invalid"]}), self.token(updates={"iss": "https://other.invalid/auth/v1"}), self.token(key=self.other_private), self.token(updates={"exp": now - 1})]
        for token in tokens:
            with self.subTest(token=token[:20]):
                self.assertIsNone(await self.verifier.verify_token(token))

    async def test_missing_oauth_claims_or_unapproved_user_are_rejected(self):
        for field in ("client_id", "session_id", "sub", "exp"):
            self.assertIsNone(await self.verifier.verify_token(self.token(remove=[field])))
        self.assertIsNone(await self.verifier.verify_token(self.token(updates={"is_anonymous": True})))
        self.write([])
        self.assertIsNone(await self.verifier.verify_token(self.token()))

    async def test_revocation_and_file_permissions_fail_closed(self):
        self.assertIsNotNone(await self.verifier.verify_token(self.token()))
        self.write([])
        self.assertIsNone(await self.verifier.verify_token(self.token()))
        self.write([{"id": USER, "subjects": ["ASO"]}])
        os.chmod(self.path, 0o644)
        self.assertIsNone(await self.verifier.verify_token(self.token()))

    async def test_open_access_grants_configured_subjects_only_to_valid_tokens(self):
        self.write([])
        key = SimpleNamespace(key=self.private.public_key())
        verifier = SupabaseOAuthVerifier(ISSUER, RESOURCE, RESOURCE, OAuthUserStore(self.path), jwks_client=SimpleNamespace(get_signing_key_from_jwt=lambda token: key), open_subjects={"ASO", "IAW"})
        access = await verifier.verify_token(self.token())
        self.assertEqual(access.claims["subjects"], ["ASO", "IAW"])
        self.assertIsNone(await verifier.verify_token(self.token(updates={"aud": "authenticated"})))
        self.assertIsNone(await verifier.verify_token(self.token(updates={"is_anonymous": True})))

    async def test_dual_verifier_preserves_personal_token(self):
        personal = object()
        static = SimpleNamespace(verify_token=AsyncMock(side_effect=[personal, None]))
        oauth = SimpleNamespace(verify_token=AsyncMock(return_value=None))
        verifier = DualTokenVerifier(static, oauth)
        self.assertIs(await verifier.verify_token("personal"), personal)
        self.assertIsNone(await verifier.verify_token("other"))
        oauth.verify_token.assert_awaited_once_with("other")

    async def test_oauth_metadata_advertises_oidc_scope_and_keeps_auth_required(self):
        tokens = Path(self.tmp.name) / "tokens.json"
        tokens.write_text(json.dumps({"version": 1, "tokens": [record("personal")]}))
        os.chmod(tokens, 0o600)
        env = {"ASIR_TOKEN_FILE": str(tokens), "ASIR_PUBLIC_URL": ORIGIN, "ASIR_ALLOWED_HOSTS": "mcp.example.org", "ASIR_OAUTH_ISSUER": ISSUER, "ASIR_OAUTH_USERS_FILE": str(self.path)}
        with patch.dict(os.environ, env), patch("scripts.mcp_http.service_config", return_value={}):
            app, _, _, _ = build_app()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as client:
                metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
                self.assertEqual(metadata.status_code, 200)
                self.assertEqual(metadata.json()["authorization_servers"], [ISSUER])
                self.assertEqual(metadata.json()["scopes_supported"], ["openid"])
                root_metadata = await client.get("/.well-known/oauth-protected-resource")
                self.assertEqual(root_metadata.status_code, 200)
                self.assertEqual(root_metadata.json(), metadata.json())
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                self.assertEqual(response.status_code, 401)

    async def test_http_accepts_oauth_and_personal_tokens_and_rejects_wrong_audience(self):
        tokens = Path(self.tmp.name) / "tokens.json"
        tokens.write_text(json.dumps({"version": 1, "tokens": [record("personal")]}))
        os.chmod(tokens, 0o600)
        env = {"ASIR_TOKEN_FILE": str(tokens), "ASIR_PUBLIC_URL": ORIGIN, "ASIR_ALLOWED_HOSTS": "mcp.example.org", "ASIR_OAUTH_ISSUER": ISSUER, "ASIR_OAUTH_USERS_FILE": str(self.path)}
        jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.private.public_key()))
        retriever = SimpleNamespace(close=lambda: None)
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        with patch.dict(os.environ, env), patch("scripts.mcp_http.service_config", return_value={}), patch("scripts.mcp_http.PostgresRetriever", return_value=retriever), patch("scripts.oauth_mcp.jwt.PyJWKClient", return_value=jwks):
            app, _, _, _ = build_app()
            async with app.app.app.router.lifespan_context(app.app.app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as client:
                    for token in ("personal", self.token()):
                        response = await client.post("/mcp", json=body, headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})
                        self.assertEqual(response.status_code, 200)
                    bad = await client.post("/mcp", json=body, headers={"Authorization": "Bearer " + self.token(updates={"aud": "authenticated"}), "Accept": "application/json, text/event-stream"})
                    self.assertEqual(bad.status_code, 401)


if __name__ == "__main__":
    unittest.main()
