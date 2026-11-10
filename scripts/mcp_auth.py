#!/usr/bin/env python3
"""Tokens estáticos con permisos por asignatura y límites de uso para el MCP."""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time

from mcp.server.auth.provider import AccessToken
from starlette.responses import JSONResponse

HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
SUBJECT = re.compile(r"[^\x00-\x1f\x7f]{1,40}\Z")
SCOPE = re.compile(r"[a-z][a-z0-9:_-]{0,60}\Z")


class TokenConfigError(Exception):
    pass


def _read_private(path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        raise TokenConfigError("token_file_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise TokenConfigError("token_file_must_be_regular_600")
    if info.st_uid != os.getuid():
        raise TokenConfigError("token_file_wrong_owner")
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream), info
    except (OSError, ValueError):
        raise TokenConfigError("token_file_invalid") from None


def _epoch(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise TokenConfigError("token_expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TokenConfigError("token_expiry_invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


class TokenStore:
    def __init__(self, path):
        self.path = Path(path)
        self.records = []
        self.mtime_ns = None
        self.size = None
        self.reload()

    def reload(self):
        value, info = _read_private(self.path)
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("tokens"), list):
            raise TokenConfigError("token_file_invalid")
        records, ids, hashes = [], set(), set()
        for item in value["tokens"]:
            if not isinstance(item, dict):
                raise TokenConfigError("token_record_invalid")
            identifier = item.get("id")
            digest = item.get("sha256")
            label = item.get("label", "")
            subjects = item.get("subjects")
            scopes = item.get("scopes")
            rpm = item.get("requests_per_minute", 30)
            if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in ids:
                raise TokenConfigError("token_id_invalid")
            if not isinstance(digest, str) or not HASH.fullmatch(digest) or digest in hashes:
                raise TokenConfigError("token_hash_invalid")
            if not isinstance(label, str) or len(label) > 120:
                raise TokenConfigError("token_label_invalid")
            if not isinstance(subjects, list) or not subjects or any(not isinstance(s, str) or not SUBJECT.fullmatch(s) for s in subjects):
                raise TokenConfigError("token_subjects_invalid")
            if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) or not SCOPE.fullmatch(s) for s in scopes):
                raise TokenConfigError("token_scopes_invalid")
            if not isinstance(rpm, int) or isinstance(rpm, bool) or not 1 <= rpm <= 600:
                raise TokenConfigError("token_rate_invalid")
            expires_at = _epoch(item.get("expires_at"))
            ids.add(identifier)
            hashes.add(digest)
            records.append(
                {
                    "id": identifier,
                    "sha256": digest,
                    "label": label,
                    "subjects": list(dict.fromkeys(subjects)),
                    "scopes": list(dict.fromkeys(scopes)),
                    "requests_per_minute": rpm,
                    "expires_at": expires_at,
                }
            )
        self.records = records
        self.mtime_ns = info.st_mtime_ns
        self.size = info.st_size

    def reload_if_changed(self):
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except OSError:
            raise TokenConfigError("token_file_unreadable") from None
        if info.st_mtime_ns != self.mtime_ns or info.st_size != self.size:
            self.reload()

    def find(self, token):
        self.reload_if_changed()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self.records:
            if hmac.compare_digest(record["sha256"], digest):
                return record
        return None

    def limit_for(self, token):
        record = self.find(token)
        return record["requests_per_minute"] if record else None


class StaticTokenVerifier:
    def __init__(self, store, resource, *, all_subjects=None):
        self.store = store
        self.resource = resource
        self.all_subjects = sorted(all_subjects) if all_subjects is not None else None

    async def verify_token(self, token):
        record = self.store.find(token)
        if record is None:
            return None
        now = int(time.time())
        if record["expires_at"] is not None and record["expires_at"] <= now:
            return None
        return AccessToken(
            token="",
            client_id=record["id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource,
            claims={"subjects": self.all_subjects if self.all_subjects is not None else record["subjects"], "label": record["label"]},
        )


class TokenBucketLimiter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.buckets = {}

    def allow(self, key, requests_per_minute):
        now = self.clock()
        capacity = float(max(1, min(10, requests_per_minute)))
        refill = requests_per_minute / 60.0
        tokens, last = self.buckets.get(key, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - last) * refill)
        allowed = tokens >= 1.0
        self.buckets[key] = (tokens - 1.0 if allowed else tokens, now)
        if len(self.buckets) > 2048:
            cutoff = now - 600
            self.buckets = {k: v for k, v in self.buckets.items() if v[1] >= cutoff}
        return allowed


def extract_bearer(scope):
    for key, value in scope.get("headers", []):
        if key.lower() == b"authorization":
            try:
                text = value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            if text.lower().startswith("bearer "):
                return text[7:].strip() or None
    return None


class RateLimitMiddleware:
    """Límite por token antes de entrar en el transporte MCP.

    El token nunca se registra: la clave interna es su SHA-256.
    """

    def __init__(self, app, store, default_rpm=30, limiter=None):
        self.app = app
        self.store = store
        self.default_rpm = default_rpm
        self.limiter = limiter or TokenBucketLimiter()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") in {"/healthz", "/readyz"}:
            await self.app(scope, receive, send)
            return
        token = extract_bearer(scope)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else "anonymous"
        rpm = self.store.limit_for(token) if token else None
        if not self.limiter.allow(digest, rpm or self.default_rpm):
            response = JSONResponse(
                {"error": "rate_limited", "detail": "reduce la frecuencia de consultas"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
