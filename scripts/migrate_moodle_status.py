#!/usr/bin/env python3
"""Añade el estado Moodle por procedencia al esquema servido y al ensayo."""

import argparse
from pathlib import Path
import sys

try:
    from .supabase_db import DEFAULT_ADMIN_CONFIG, admin_config, connect
except ImportError:
    from supabase_db import DEFAULT_ADMIN_CONFIG, admin_config, connect


SCHEMAS = ("asir", "asir_staging")


def migrate(connection):
    from psycopg import sql

    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '3s'")
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        for schema in SCHEMAS:
            cursor.execute("SELECT to_regclass(%s) AS table_name", (schema + ".source_occurrences",))
            if cursor.fetchone()["table_name"] is None:
                raise RuntimeError("source_occurrences_missing")
            cursor.execute(sql.SQL("ALTER TABLE {}.source_occurrences ADD COLUMN IF NOT EXISTS moodle_active boolean").format(sql.Identifier(schema)))
            cursor.execute(
                sql.SQL("""
                    CREATE OR REPLACE VIEW {}.citation_sources WITH (security_invoker = true) AS
                    SELECT ci.id AS citation_id,
                      COALESCE(o.sha256, s.sha256) AS sha256,
                      COALESCE(o.subject, s.subject) AS subject,
                      COALESCE(o.section, s.section) AS section,
                      COALESCE(o.title, s.title) AS title,
                      COALESCE(o.filename, s.filename) AS filename,
                      CASE WHEN o.source_key IS NULL THEN s.url ELSE o.url END AS url,
                      CASE WHEN o.source_key IS NULL THEN s.mime ELSE o.mime END AS mime,
                      CASE WHEN o.source_key IS NULL THEN s.path ELSE o.path END AS path,
                      o.moodle_active AS moodle_active
                    FROM {}.citations ci
                    JOIN {}.sources s ON s.sha256 = ci.source_sha256
                    LEFT JOIN {}.source_occurrences o ON o.source_key = ci.source_key
                """).format(*(sql.Identifier(schema) for _ in range(4)))
            )
            cursor.execute("SELECT 1 FROM " + schema + ".citation_sources LIMIT 0")
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name='source_occurrences' AND column_name='moodle_active'",
                (schema,),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("moodle_active_column_missing")
    connection.commit()
    return {"complete": True, "schemas": list(SCHEMAS), "column": "source_occurrences.moodle_active", "existing_statuses": "unchanged_null_until_next_import"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-config", type=Path, default=DEFAULT_ADMIN_CONFIG)
    args = parser.parse_args()
    try:
        with connect(admin_config(args.admin_config), application_name="asir-moodle-status-migration") as connection:
            import json
            print(json.dumps(migrate(connection)))
    except Exception as error:
        try:
            import psycopg
            if isinstance(error, psycopg.Error):
                print("No se pudo aplicar la migración de estado Moodle; transacción revertida.", file=sys.stderr)
                return 1
        except ImportError:
            pass
        print("No se pudo aplicar la migración de estado Moodle.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
