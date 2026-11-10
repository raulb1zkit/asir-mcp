#!/usr/bin/env python3
"""Configuración y conexión SQL a Supabase con TLS verificado.

Los archivos de credenciales se cargan en memoria; este módulo nunca imprime
contraseñas, cadenas de conexión ni parámetros sensibles.
"""

import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".private"
EXPECTED_PROJECT = "kwvezxkhrjouaidzoglj"
DEFAULT_ADMIN_CONFIG = PRIVATE / "supabase-connection.json"
DEFAULT_CA = PRIVATE / "supabase-ca.crt"
POOLER_HOST = re.compile(r"[a-z0-9.-]+\.pooler\.supabase\.com\Z")
ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class DatabaseConfigError(Exception):
    pass


def _regular_private_file(path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        raise DatabaseConfigError("database_config_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise DatabaseConfigError("database_config_must_be_regular_600")
    if info.st_uid != os.getuid():
        raise DatabaseConfigError("database_config_wrong_owner")
    return info


def validate_config(value, *, path=None, service=False):
    """Valida una configuración ya deserializada y devuelve solo campos seguros."""
    try:
        if not isinstance(value, dict):
            raise ValueError
        project = value.get("project_ref", EXPECTED_PROJECT)
        host = value["host"]
        port = int(value["port"])
        user = value["user"]
        password = value["password"]
        database = value["database"]
        root = value.get("sslrootcert")
    except (KeyError, TypeError, ValueError):
        raise DatabaseConfigError("database_config_invalid") from None
    if project != EXPECTED_PROJECT:
        raise DatabaseConfigError("database_config_wrong_project")
    if not isinstance(host, str) or not POOLER_HOST.fullmatch(host):
        raise DatabaseConfigError("database_config_wrong_host")
    if port != 6543 or database != "postgres":
        raise DatabaseConfigError("database_config_wrong_endpoint")
    if not isinstance(user, str) or not user.endswith("." + EXPECTED_PROJECT):
        raise DatabaseConfigError("database_config_wrong_user")
    role = user[: -len(EXPECTED_PROJECT) - 1]
    if not ROLE.fullmatch(role):
        raise DatabaseConfigError("database_config_wrong_role")
    if service and role == "postgres":
        raise DatabaseConfigError("database_service_must_not_use_admin_role")
    if not service and role != "postgres":
        raise DatabaseConfigError("database_admin_role_required")
    if not isinstance(password, str) or not password or len(password) > 512 or CONTROL.search(password):
        raise DatabaseConfigError("database_config_invalid_password")
    if root is None:
        root = str(DEFAULT_CA)
    if not isinstance(root, str) or not root:
        raise DatabaseConfigError("database_config_missing_ca")
    root_path = Path(root)
    if not root_path.is_absolute():
        root_path = (Path(path).parent if path else PRIVATE) / root_path
    try:
        certificate = root_path.resolve(strict=True)
        info = os.stat(certificate, follow_symlinks=False)
    except OSError:
        raise DatabaseConfigError("database_config_missing_ca") from None
    if not stat.S_ISREG(info.st_mode):
        raise DatabaseConfigError("database_config_missing_ca")
    return {
        "project_ref": project,
        "host": host,
        "port": port,
        "user": user,
        "role": role,
        "password": password,
        "database": database,
        "sslrootcert": str(certificate),
    }


def load_config(path, *, service=False):
    _regular_private_file(path)
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        raise DatabaseConfigError("database_config_invalid") from None
    return validate_config(value, path=path, service=service)


def admin_config(path=DEFAULT_ADMIN_CONFIG):
    return load_config(path, service=False)


def service_config(path):
    return load_config(path, service=True)


def connect(config, *, application_name="asir", autocommit=False):
    """Abre una conexión con TLS verify-full y sin sentencias preparadas.

    El pooler de transacciones no conserva estado de sesión; por eso se
    desactivan las sentencias preparadas de psycopg.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise DatabaseConfigError("psycopg_missing") from None
    return psycopg.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        dbname=config["database"],
        sslmode="verify-full",
        sslrootcert=config["sslrootcert"],
        connect_timeout=15,
        application_name=application_name,
        prepare_threshold=None,
        row_factory=dict_row,
        autocommit=autocommit,
    )


def use_staging_schema(connection):
    """Aísla las sentencias del importador en el esquema privado de ensayo.

    Solo acepta consultas escritas por el programa; los valores siguen pasando
    como parámetros. No modifica el `search_path` del pooler ni da permisos al
    rol de servicio sobre el esquema de ensayo.
    """
    import re
    import psycopg

    class StagingCursor(psycopg.Cursor):
        def _staging_query(self, query):
            if not isinstance(query, str):
                query = query.as_string(self.connection)
            return re.sub(r"\basir\b", "asir_staging", query)

        def execute(self, query, params=None, *, prepare=None, binary=None):
            return super().execute(self._staging_query(query), params, prepare=prepare, binary=binary)

        def executemany(self, query, params_seq, *, returning=False):
            return super().executemany(self._staging_query(query), params_seq, returning=returning)

    connection.cursor_factory = StagingCursor


def vector_literal(values):
    """Serializa float32 con 9 cifras, suficiente para reconstruir el mismo valor."""
    return "[" + ",".join(format(float(value), ".9g") for value in values) + "]"


def check_health(config):
    """Comprobación de salud sin exponer datos sensibles."""
    with connect(config, application_name="asir-health") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute(
                "SELECT current_database() AS database, "
                "(SELECT count(*) FROM asir.chunks) AS chunks, "
                "(SELECT count(*) FROM asir.embedding_windows) AS windows, "
                "(SELECT corpus_sha256 FROM asir.corpus WHERE id = 1) AS corpus_sha256"
            )
            row = cursor.fetchone()
        connection.rollback()
    return {
        "database": row["database"],
        "chunks": row["chunks"],
        "windows": row["windows"],
        "corpus_id": row["corpus_sha256"],
    }
