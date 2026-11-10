#!/usr/bin/env python3
"""Verificación OAuth para MCP; no se habilita sin configuración explícita.

La autorización procede de la configuración del servidor o de una lista local,
no de user_metadata ni del email que el usuario pueda modificar. Los JWT ordinarios
de Supabase con audiencia authenticated se rechazan: el servidor OAuth debe
emitir una audiencia específica de este MCP mediante configuración verificada.
"""

import asyncio
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from mcp.server.auth.provider import AccessToken

try:
    from .mcp_auth import SUBJECT, TokenConfigError, _read_private
except ImportError:
    from mcp_auth import SUBJECT, TokenConfigError, _read_private


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value.lower() else None


class OAuthUserStore:
    """Lista privada por UUID de usuario con recarga; sin autoinscripción."""

    def __init__(self, path):
        self.path = Path(path)

    def get_subjects(self, user_id):
        value, _ = _read_private(self.path)
        return validate_users(value).get(user_id)


def validate_users(value):
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("users"), list):
        raise TokenConfigError("oauth_user_file_invalid")
    users = {}
    for item in value["users"]:
        if not isinstance(item, dict):
            raise TokenConfigError("oauth_user_file_invalid")
        identifier, subjects = item.get("id"), item.get("subjects")
        if not _uuid(identifier) or _uuid(identifier) != identifier or identifier in users or not isinstance(subjects, list) or not subjects or any(not isinstance(s, str) or not SUBJECT.fullmatch(s) for s in subjects):
            raise TokenConfigError("oauth_user_file_invalid")
        users[identifier] = list(dict.fromkeys(subjects))
    return users


class SupabaseOAuthVerifier:
    """Comprueba JWT asimétricos y audiencia dedicada antes de autorizar."""

    def __init__(self, issuer, audience, resource, users, *, jwks_client=None, open_subjects=None):
        parsed = urlsplit(issuer)
        target = urlsplit(audience)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.path.endswith("/auth/v1"):
            raise ValueError("invalid_oauth_issuer")
        if target.scheme != "https" or not target.hostname or target.query or target.fragment or target.username or target.password:
            raise ValueError("invalid_oauth_audience")
        if resource != audience:
            raise ValueError("oauth_resource_audience_mismatch")
        self.issuer, self.audience, self.resource, self.users = issuer, audience, resource, users
        self.open_subjects = tuple(sorted(open_subjects)) if open_subjects else None
        self.jwks = jwks_client or jwt.PyJWKClient(issuer + "/.well-known/jwks.json", timeout=5, lifespan=300)

    def _verify(self, token):
        if not isinstance(token, str) or len(token) > 16384 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            return None
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=self.audience, issuer=self.issuer, options={"strict_aud": True, "require": ["exp", "iat", "iss", "sub", "aud", "client_id", "session_id"]})
            user_id = _uuid(claims.get("sub"))
            client_id = _uuid(claims.get("client_id"))
            session_id = _uuid(claims.get("session_id"))
            if not user_id or not client_id or not session_id or claims.get("role") != "authenticated" or claims.get("is_anonymous", False) is not False:
                return None
            subjects = list(self.open_subjects) if self.open_subjects else self.users.get_subjects(user_id)
            if not subjects:
                return None
            return AccessToken(token="", client_id=client_id, scopes=["asir:read"], expires_at=claims["exp"], resource=self.resource, subject=user_id, claims={"subjects": subjects})
        except (jwt.PyJWTError, TokenConfigError, OSError, ValueError, TypeError, KeyError):
            return None

    async def verify_token(self, token):
        return await asyncio.to_thread(self._verify, token)


class DualTokenVerifier:
    """Mantiene la verificación del token personal y añade OAuth."""

    def __init__(self, static_verifier, oauth_verifier):
        self.static, self.oauth = static_verifier, oauth_verifier

    async def verify_token(self, token):
        personal = await self.static.verify_token(token)
        return personal if personal is not None else await self.oauth.verify_token(token)
