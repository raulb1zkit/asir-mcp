#!/usr/bin/env python3
"""MCP remoto de ASIR: Streamable HTTP, tokens por alumno y backend Postgres."""

import asyncio
import logging
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

try:
    from .request_metrics import RequestMetricsMiddleware
    from .mcp_auth import RateLimitMiddleware, StaticTokenVerifier, TokenConfigError, TokenStore
    from .mcp_server import create_server
    from .material_service import MaterialError, Settings
    from .postgres_search import PostgresRetriever
    from .supabase_db import DatabaseConfigError, check_health, service_config
    from .oauth_mcp import DualTokenVerifier, OAuthUserStore, SupabaseOAuthVerifier
except ImportError:
    from request_metrics import RequestMetricsMiddleware
    from mcp_auth import RateLimitMiddleware, StaticTokenVerifier, TokenConfigError, TokenStore
    from mcp_server import create_server
    from material_service import MaterialError, Settings
    from postgres_search import PostgresRetriever
    from supabase_db import DatabaseConfigError, check_health, service_config
    from oauth_mcp import DualTokenVerifier, OAuthUserStore, SupabaseOAuthVerifier

LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def env_path(name, default):
    return Path(os.environ.get(name, default))


def env_list(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(default)


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise MaterialError("invalid_environment") from None
    if not minimum <= value <= maximum:
        raise MaterialError("invalid_environment")
    return value


def build_app():
    config_path = env_path("ASIR_DATABASE_CONFIG", "/etc/asir/database.json")
    token_path = env_path("ASIR_TOKEN_FILE", "/etc/asir/tokens.json")
    model_dir = env_path("ASIR_MODEL_DIR", "/var/lib/asir/models/multilingual-e5-small")
    host = os.environ.get("ASIR_HOST", "127.0.0.1")
    port = env_int("ASIR_PORT", 8080, 1, 65535)
    public_url = os.environ.get("ASIR_PUBLIC_URL", f"http://{host}:{port}").rstrip("/")
    parsed = urlsplit(public_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MaterialError("invalid_public_url")
    subjects = frozenset(
        item.strip() for item in os.environ.get("ASIR_ALLOWED_SUBJECTS", "ASO").split(",") if item.strip()
    )
    if not subjects or any(len(item) > 40 for item in subjects):
        raise MaterialError("invalid_server_subjects")
    max_pending = env_int("ASIR_MAX_PENDING", 8, 1, 64)
    default_rpm = env_int("ASIR_DEFAULT_RPM", 30, 1, 600)
    log_level = os.environ.get("ASIR_LOG_LEVEL", "WARNING").upper()
    if log_level not in LOG_LEVELS:
        raise MaterialError("invalid_log_level")
    allowed_hosts = env_list("ASIR_ALLOWED_HOSTS", ["127.0.0.1:*", "localhost:*", "[::1]:*"])
    allowed_origins = env_list(
        "ASIR_ALLOWED_ORIGINS",
        ["http://127.0.0.1:*", "http://localhost:*", "https://127.0.0.1:*", "https://localhost:*"],
    )

    config = service_config(config_path)
    store = TokenStore(token_path)
    if not store.records:
        raise TokenConfigError("no_tokens_configured")
    settings = Settings(
        model_dir=model_dir,
        allowed_subjects=subjects,
        backend="postgres",
        max_pending=max_pending,
        transport="remote_http",
    )
    oauth_issuer = os.environ.get("ASIR_OAUTH_ISSUER", "").strip().rstrip("/")
    oauth_users = os.environ.get("ASIR_OAUTH_USERS_FILE", "").strip()
    if bool(oauth_issuer) != bool(oauth_users):
        raise MaterialError("incomplete_oauth_configuration")
    if oauth_issuer and parsed.path not in {"", "/"}:
        raise MaterialError("invalid_oauth_public_origin")
    resource_url = public_url + "/mcp" if oauth_issuer else public_url
    all_tokens = os.environ.get("ASIR_TOKENS_ALL_SUBJECTS", "0")
    if all_tokens not in {"0", "1"}:
        raise MaterialError("invalid_token_access_mode")
    verifier = StaticTokenVerifier(store, resource_url, all_subjects=subjects if all_tokens == "1" else None)
    if oauth_issuer:
        open_oauth = os.environ.get("ASIR_OAUTH_OPEN_ACCESS", "0")
        if open_oauth not in {"0", "1"}:
            raise MaterialError("invalid_oauth_access_mode")
        oauth = SupabaseOAuthVerifier(oauth_issuer, resource_url, resource_url, OAuthUserStore(oauth_users), open_subjects=subjects if open_oauth == "1" else None)
        # Valida el archivo al arrancar; una lista vacía no concede acceso.
        oauth.users.get_subjects("00000000-0000-0000-0000-000000000000")
        verifier = DualTokenVerifier(verifier, oauth)
    auth = AuthSettings(
        issuer_url=oauth_issuer or public_url,
        resource_server_url=resource_url,
        required_scopes=["asir:read"],
        validate_token_resource=True,
    )
    server = create_server(
        settings,
        retriever_factory=lambda: PostgresRetriever(config, model_dir),
        token_verifier=verifier,
        auth_settings=auth,
    )
    app = server.streamable_http_app(
        host=host,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=256 * 1024,
        session_idle_timeout=300,
        max_sessions=100,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
    )
    if oauth_issuer:
        # Supabase solo ofrece scopes OIDC estándar. El permiso asir:read se
        # comprueba internamente con la lista privada, no se pide a Supabase.
        metadata = create_protected_resource_routes(auth.resource_server_url, [auth.issuer_url], scopes_supported=["openid"])
        paths = {route.path for route in metadata}
        app.routes[:] = [route for route in app.routes if getattr(route, "path", None) not in paths]
        # Algunos clientes consultan la ruta raíz aun cuando el desafío señala
        # la ruta específica del recurso. Ambas describen el mismo MCP.
        root_metadata = Route("/.well-known/oauth-protected-resource", metadata[0].endpoint, methods=["GET", "OPTIONS"])
        app.routes[:0] = [*metadata, root_metadata]

    async def health(request):
        try:
            info = await asyncio.to_thread(check_health, config)
            return JSONResponse({"status": "ok", "service": "asir-mcp", "backend": "postgres", **info})
        except Exception:
            logging.getLogger("asir.health").warning("health check failed")
            return JSONResponse({"status": "degraded", "service": "asir-mcp"}, status_code=503)

    app.routes.append(Route("/healthz", health, methods=["GET"]))
    limited = RateLimitMiddleware(app, store, default_rpm=default_rpm)
    metrics_path = env_path("ASIR_METRICS_DB", "/var/lib/asir/request-metrics.sqlite")
    return RequestMetricsMiddleware(limited, metrics_path), host, port, log_level


def main():
    try:
        app, host, port, log_level = build_app()
    except (DatabaseConfigError, TokenConfigError, MaterialError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, (DatabaseConfigError, TokenConfigError, MaterialError)) else "startup_failed"
        print(f"asir-mcp: {message}", file=sys.stderr)
        return 1
    logging.basicConfig(level=getattr(logging, log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("ASIR_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
