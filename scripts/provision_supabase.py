#!/usr/bin/env python3
"""Crea el rol de lectura del MCP, sus permisos mínimos y su configuración local."""

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

try:
    from .supabase_db import DatabaseConfigError, admin_config, connect, service_config
except ImportError:
    from supabase_db import DatabaseConfigError, admin_config, connect, service_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".private/asir-service-connection.json"


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="service-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def provision(admin, role, output):
    from psycopg import sql

    password = secrets.token_urlsafe(32)
    with connect(admin, application_name="asir-provision") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            exists = cursor.fetchone() is not None
            if not exists:
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
                )
            cursor.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {} CONNECTION LIMIT 10").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            cursor.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '20s'").format(sql.Identifier(role)))
            cursor.execute(
                sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '30s'").format(sql.Identifier(role))
            )
            cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(admin["database"]), sql.Identifier(role)
            ))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA asir TO {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA asir TO {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA extensions TO {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA asir GRANT SELECT ON TABLES TO {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(sql.Identifier(role)))
        connection.commit()

    config = {
        "project_ref": admin["project_ref"],
        "host": admin["host"],
        "port": admin["port"],
        "user": f"{role}.{admin['project_ref']}",
        "password": password,
        "database": admin["database"],
        "sslrootcert": admin["sslrootcert"],
    }
    write_private(output, config)
    service = service_config(output)

    checks = {}
    with connect(service, application_name="asir-provision-check") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT count(*) AS chunks FROM asir.chunks")
            checks["chunks"] = cursor.fetchone()["chunks"]
            cursor.execute("SELECT count(*) AS windows FROM asir.embedding_windows")
            checks["windows"] = cursor.fetchone()["windows"]
            cursor.execute("SELECT count(*) AS sources FROM asir.sources")
            checks["sources"] = cursor.fetchone()["sources"]
        connection.rollback()
    with connect(service, application_name="asir-provision-write", autocommit=True) as connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute("CREATE TABLE asir.should_not_exist (id integer)")
                checks["write_denied"] = False
            except Exception:
                checks["write_denied"] = True
    return {
        "role": role,
        "user": config["user"],
        "host": config["host"],
        "port": config["port"],
        "tls": "verify-full",
        "output": str(output),
        "permissions": "SELECT sobre asir; sin escritura ni acceso a public",
        **checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--role", default="asir_reader")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        if not args.role.replace("_", "").isalnum() or not args.role.islower() or len(args.role) > 63:
            raise DatabaseConfigError("invalid_role_name")
        result = provision(admin_config(args.config) if args.config else admin_config(), args.role, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DatabaseConfigError, OSError, ValueError, KeyError) as error:
        message = str(error) if isinstance(error, DatabaseConfigError) else "provision_failed"
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "error": "provision_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
