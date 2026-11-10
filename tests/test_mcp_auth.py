import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from scripts.mcp_auth import (
    RateLimitMiddleware,
    StaticTokenVerifier,
    TokenBucketLimiter,
    TokenConfigError,
    TokenStore,
    extract_bearer,
)


def record(token, *, identifier="tok_test", subjects=("ASO",), scopes=("asir:read",), rpm=30, expires_at=None):
    return {
        "id": identifier,
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "label": "test",
        "subjects": list(subjects),
        "scopes": list(scopes),
        "requests_per_minute": rpm,
        "expires_at": expires_at,
    }


class TokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tokens.json"
        self.token = "student-token"
        self.write([record(self.token, expires_at="2099-01-01T00:00:00Z")])

    def write(self, tokens):
        self.path.write_text(json.dumps({"version": 1, "tokens": tokens}), encoding="utf-8")
        os.chmod(self.path, 0o600)

    async def test_valid_token_exposes_subjects_and_resource(self):
        verifier = StaticTokenVerifier(TokenStore(self.path), "https://asir.example/mcp")
        access = await verifier.verify_token(self.token)
        self.assertEqual(access.client_id, "tok_test")
        self.assertEqual(access.scopes, ["asir:read"])
        self.assertEqual(access.claims["subjects"], ["ASO"])
        self.assertEqual(access.resource, "https://asir.example/mcp")
        self.assertIsNone(await verifier.verify_token("other"))

    async def test_expired_token_is_rejected(self):
        self.write([record(self.token, expires_at="2000-01-01T00:00:00Z")])
        verifier = StaticTokenVerifier(TokenStore(self.path), "https://asir.example/mcp")
        self.assertIsNone(await verifier.verify_token(self.token))

    async def test_all_subjects_mode_preserves_authentication_and_expiry(self):
        verifier = StaticTokenVerifier(TokenStore(self.path), "https://asir.example/mcp", all_subjects={"ASO", "IAW"})
        self.assertEqual((await verifier.verify_token(self.token)).claims["subjects"], ["ASO", "IAW"])
        self.assertIsNone(await verifier.verify_token("invalid"))
        self.write([record(self.token, expires_at="2000-01-01T00:00:00Z")])
        self.assertIsNone(await verifier.verify_token(self.token))

    async def test_reload_picks_up_new_token_without_restart(self):
        store = TokenStore(self.path)
        verifier = StaticTokenVerifier(store, "https://asir.example/mcp")
        self.assertIsNone(await verifier.verify_token("new-token"))
        self.write([record(self.token), record("new-token", identifier="tok_new")])
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        self.assertIsNotNone(await verifier.verify_token("new-token"))

    def test_file_permissions_and_record_validation(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(TokenConfigError, "600"):
            TokenStore(self.path)
        self.path.chmod(0o600)
        for tokens in [
            [record(self.token), record(self.token)],
            [record(self.token, identifier="bad id")],
            [record(self.token, subjects=[])],
            [record(self.token, scopes=["INVALID"])],
            [record(self.token, rpm=0)],
        ]:
            with self.subTest(tokens=tokens):
                self.write(tokens)
                with self.assertRaises(TokenConfigError):
                    TokenStore(self.path)

    def test_rate_limiter_uses_buckets_and_refills(self):
        now = [1000.0]
        limiter = TokenBucketLimiter(clock=lambda: now[0])
        self.assertTrue(limiter.allow("a", 2))
        self.assertTrue(limiter.allow("a", 2))
        self.assertFalse(limiter.allow("a", 2))
        now[0] += 30
        self.assertTrue(limiter.allow("a", 2))

    def test_extract_bearer_is_case_insensitive(self):
        scope = {"headers": [(b"authorization", b"Bearer abc")]}
        self.assertEqual(extract_bearer(scope), "abc")
        self.assertIsNone(extract_bearer({"headers": [(b"authorization", b"Basic abc")]}))
        self.assertIsNone(extract_bearer({}))


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_rejects_after_burst_and_skips_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            token = "limited"
            path.write_text(json.dumps({"version": 1, "tokens": [record(token, rpm=1)]}), encoding="utf-8")
            os.chmod(path, 0o600)
            store = TokenStore(path)
            calls = []

            async def app(scope, receive, send):
                calls.append(scope["path"])
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            middleware = RateLimitMiddleware(app, store, default_rpm=1, limiter=TokenBucketLimiter())
            messages = []

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "path": "/mcp", "headers": [(b"authorization", f"Bearer {token}".encode())]}
            await middleware(scope, None, send)
            await middleware(scope, None, send)
            self.assertEqual(calls, ["/mcp"])
            self.assertEqual(messages[-2]["status"], 429)
            await middleware({"type": "http", "path": "/healthz", "headers": []}, None, send)
            self.assertEqual(calls, ["/mcp", "/healthz"])
