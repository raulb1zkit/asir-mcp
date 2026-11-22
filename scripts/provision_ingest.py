#!/usr/bin/env python3
"""Provisiona y verifica el rol fijo de ingesta sin imprimir credenciales."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

try:
    from .supabase_db import ROOT, admin_config, connect, use_staging_schema, load_config
except ImportError:
    from supabase_db import ROOT, admin_config, connect, use_staging_schema, load_config

ROLE = "asir_ingest"
SCHEMAS = ("asir", "asir_staging")
TABLES = ("corpus", "embedding_metadata", "sources", "chunks", "citations", "citation_segments", "embedding_windows", "source_occurrences")
# Conjunto privado exacto del publicador delta (scripts/sql/rag_versioning.sql):
# nada más se acepta como privilegio de escritura, y nunca en el esquema de ensayo.
VERSION_TABLES = {"asir": ("rag_head", "rag_operations")}
VERSION_PRIVILEGES = {"asir.rag_head": ("SELECT", "INSERT", "UPDATE"), "asir.rag_operations": ("SELECT", "INSERT")}
VERSION_FUNCTIONS = ("asir.rag_hash(jsonb)", "asir.rag_canonical(jsonb)")
OUTPUT = ROOT / ".private/asir-ingest-connection.json"


class ProvisionFailure(Exception):
    pass


def create_private(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def bootstrap_staging(connection):
    """Solo el administrador prepara objetos; nunca modificar el esquema servido."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regnamespace('asir') IS NOT NULL AS served, to_regnamespace('asir_staging') IS NOT NULL AS staging")
        state = cursor.fetchone()
    connection.rollback()
    if not state["served"]:
        raise ProvisionFailure("served_schema_missing")
    if not state["staging"]:
        try:
            from .import_supabase import create_schema
        except ImportError:
            from import_supabase import create_schema
        use_staging_schema(connection)
        try:
            create_schema(connection, grant_reader=False)
        finally:
            # El wrapper solo aplica a esta conexión, que no se reutiliza.
            connection.rollback()
        return True
    return False


def grant_role(connection, password):
    from psycopg import sql
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
        if cursor.fetchone():
            raise ProvisionFailure("ingest_role_already_exists_no_rotation")
        cursor.execute(sql.SQL("CREATE ROLE asir_ingest LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {} ").format(sql.Literal(password)))
        # Solo objetos ya existentes; no ALL TABLES ni privilegios por defecto.
        for schema in SCHEMAS:
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO asir_ingest").format(sql.Identifier(schema)))
            for table in TABLES:
                cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE {}.{} TO asir_ingest").format(sql.Identifier(schema), sql.Identifier(table)))
            cursor.execute(sql.SQL("GRANT SELECT ON TABLE {}.citation_sources TO asir_ingest").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {}.norm_text(text) TO asir_ingest").format(sql.Identifier(schema)))
        cursor.execute("GRANT USAGE ON SCHEMA extensions TO asir_ingest")
        cursor.execute("""SELECT ns.nspname AS schema, seq.relname AS sequence
          FROM pg_class seq JOIN pg_namespace ns ON ns.oid=seq.relnamespace
          JOIN pg_depend dep ON dep.classid='pg_class'::regclass AND dep.objid=seq.oid AND dep.deptype IN ('a','i')
          JOIN pg_class tab ON tab.oid=dep.refobjid
          JOIN pg_namespace tn ON tn.oid=tab.relnamespace
          JOIN pg_attribute a ON a.attrelid=tab.oid AND a.attnum=dep.refobjsubid
          WHERE seq.relkind='S' AND ns.nspname=ANY(%s) AND tn.nspname=ns.nspname
            AND tab.relname=ANY(%s) AND a.atttypid='int8'::regtype
          ORDER BY ns.nspname,seq.relname""", (list(SCHEMAS), list(TABLES)))
        sequences = cursor.fetchall()
        if len(sequences) != 4:
            raise ProvisionFailure("unexpected_corpus_sequence_layout")
        for sequence in sequences:
            cursor.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {}.{} TO asir_ingest").format(sql.Identifier(sequence["schema"]), sql.Identifier(sequence["sequence"])))
    return [{"schema": item["schema"], "sequence": item["sequence"]} for item in sequences]


def expect_denied(cursor, statement):
    """La consulta es fija; el savepoint revierte incluso un éxito inesperado."""
    cursor.execute("SAVEPOINT permission_probe")
    denied = False
    try:
        cursor.execute(statement)
    except Exception as error:
        # No considerar inexistencia de objeto ni errores de sintaxis como denegación.
        denied = getattr(error, "sqlstate", None) == "42501"
    finally:
        cursor.execute("ROLLBACK TO SAVEPOINT permission_probe")
        cursor.execute("RELEASE SAVEPOINT permission_probe")
    if not denied:
        raise ProvisionFailure("ingest_forbidden_operation_not_denied")


def check_role(connection):
    from psycopg import sql
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout='10s'")
        cursor.execute("SET LOCAL lock_timeout='3s'")
        cursor.execute("SELECT current_user AS name, rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        role = cursor.fetchone()
        if not role or role["name"] != ROLE or not role["rolcanlogin"] or any(role[key] for key in ("rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
            raise ProvisionFailure("ingest_role_attributes_invalid")
        cursor.execute("SELECT count(*) AS n FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)")
        if cursor.fetchone()["n"]:
            raise ProvisionFailure("ingest_role_memberships_forbidden")
        cursor.execute("SELECT count(*) AS n FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)")
        if cursor.fetchone()["n"]:
            raise ProvisionFailure("ingest_schema_ownership_forbidden")
        cursor.execute("SELECT count(*) AS n FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)")
        if cursor.fetchone()["n"]:
            raise ProvisionFailure("ingest_object_ownership_forbidden")
        # No escritura del corpus fuera del alcance. PostgreSQL concede UPDATE
        # de pg_settings a PUBLIC: solo modifica ajustes de la sesión y mantiene
        # sus comprobaciones internas para opciones reservadas al administrador.
        cursor.execute("""SELECT n.nspname AS schema,c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT LIKE 'pg_temp_%'
          AND (has_table_privilege(c.oid,'INSERT,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR (has_table_privilege(c.oid,'UPDATE') AND c.oid <> 'pg_catalog.pg_settings'::regclass))""")
        writable = cursor.fetchall()
        allowed = {(schema, name) for schema in SCHEMAS for name in TABLES}
        for schema, names in VERSION_TABLES.items():
            if schema in SCHEMAS:
                allowed.update((schema, name) for name in names)
        if any((row["schema"], row["name"]) not in allowed for row in writable):
            raise ProvisionFailure("ingest_unexpected_write_privilege")
        cursor.execute("""SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_temp_%'
          AND has_schema_privilege(oid,'CREATE')""")
        if cursor.fetchall():
            raise ProvisionFailure("ingest_schema_create_forbidden")
        for schema in SCHEMAS:
            for table in TABLES:
                cursor.execute(sql.SQL("SELECT 1 FROM {}.{} LIMIT 0").format(sql.Identifier(schema), sql.Identifier(table)))
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    cursor.execute("SELECT has_table_privilege(%s,%s) AS allowed", (schema + "." + table, privilege))
                    if not cursor.fetchone()["allowed"]:
                        raise ProvisionFailure("ingest_required_table_privilege_missing")
            cursor.execute(sql.SQL("SELECT 1 FROM {}.citation_sources LIMIT 0").format(sql.Identifier(schema)))
        cursor.execute("""SELECT n.nspname AS schema,s.relname AS sequence,
          has_sequence_privilege(s.oid,'USAGE') AS usage,has_sequence_privilege(s.oid,'SELECT') AS read,
          has_sequence_privilege(s.oid,'UPDATE') AS update FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace
          WHERE s.relkind='S' AND (has_sequence_privilege(s.oid,'USAGE') OR has_sequence_privilege(s.oid,'SELECT') OR has_sequence_privilege(s.oid,'UPDATE'))""")
        sequences = cursor.fetchall()
        expected = {(schema, name) for schema in SCHEMAS for name in ("citations_id_seq", "embedding_windows_id_seq")}
        if {(row["schema"], row["sequence"]) for row in sequences} != expected or any(not row["usage"] or not row["read"] or row["update"] for row in sequences):
            raise ProvisionFailure("ingest_sequence_privileges_invalid")
        # Datos sintéticos SOLO en ensayo. ROLLBACK final incluso si todo pasa.
        cursor.execute("SAVEPOINT staging_dml")
        names = sql.SQL(',').join(sql.SQL('asir_staging.{}').format(sql.Identifier(table)) for table in TABLES)
        cursor.execute(sql.SQL("TRUNCATE {} CONTINUE IDENTITY RESTRICT").format(names))
        marker = secrets.token_hex(32)
        chunk_id = "chunk:" + marker
        cursor.execute("INSERT INTO asir_staging.sources(sha256,subject,section,title,filename) VALUES (%s,'TEST','TEST','TEST','TEST')", (marker,))
        cursor.execute("UPDATE asir_staging.sources SET title='UPDATED' WHERE sha256=%s", (marker,))
        if cursor.rowcount != 1:
            raise ProvisionFailure("ingest_staging_update_failed")
        cursor.execute("DELETE FROM asir_staging.sources WHERE sha256=%s", (marker,))
        if cursor.rowcount != 1:
            raise ProvisionFailure("ingest_staging_delete_failed")
        cursor.execute("INSERT INTO asir_staging.sources(sha256,subject,section,title,filename) VALUES (%s,'TEST','TEST','TEST','TEST')", (marker,))
        cursor.execute("INSERT INTO asir_staging.chunks(id,ordinal,text,text_sha256,characters,warnings,requires_review,titles,sections) VALUES (%s,1,'TEST',%s,4,'[]',false,'TEST','TEST') RETURNING search_text", (chunk_id, marker))
        if not cursor.fetchone()["search_text"]:
            raise ProvisionFailure("ingest_generated_search_text_failed")
        cursor.execute("INSERT INTO asir_staging.source_occurrences(source_key,sha256,subject,section,title,filename,moodle_active) VALUES (%s,%s,'TEST','TEST','TEST','TEST',false)", (marker, marker))
        cursor.execute("INSERT INTO asir_staging.citations(chunk_id,ordinal,source_sha256,method,source_key) VALUES (%s,1,%s,'native',%s) RETURNING id", (chunk_id, marker, marker))
        citation_id = cursor.fetchone()["id"]
        cursor.execute("SELECT moodle_active FROM asir_staging.citation_sources WHERE citation_id=%s", (citation_id,))
        if cursor.fetchone()["moodle_active"] is not False:
            raise ProvisionFailure("ingest_moodle_status_view_failed")
        cursor.execute("INSERT INTO asir_staging.citation_segments(citation_id,ordinal,source_unit_id,locator,start_char,end_char,method,quality) VALUES (%s,1,'TEST','{}',0,4,'native','TEST')", (citation_id,))
        cursor.execute("INSERT INTO asir_staging.embedding_windows(chunk_id,ordinal,char_start,char_end,tokens,embedding) VALUES (%s,1,0,4,1,%s::extensions.vector)", (chunk_id, "[1," + ",".join(["0"] * 383) + "]"))
        cursor.execute("INSERT INTO asir_staging.corpus(schema_version,corpus_sha256,chunker_version,created_at,report) VALUES (1,%s,'permission-probe',now(),'{}')", (marker,))
        cursor.execute("INSERT INTO asir_staging.embedding_metadata(schema_version,corpus_sha256,model_fingerprint,model_repository,model_revision,dimensions,max_tokens,overlap_tokens,context,vectors_sha256,summary) VALUES (1,%s,'permission-probe','TEST','TEST',384,512,64,'TEST',%s,'{}')", (marker, marker))
        cursor.execute("ROLLBACK TO SAVEPOINT staging_dml")
        cursor.execute("RELEASE SAVEPOINT staging_dml")
        for statement in (
            "SELECT 1 FROM auth.users LIMIT 0", "SELECT 1 FROM storage.objects LIMIT 0",
            "SELECT rolpassword FROM pg_authid LIMIT 0", "CREATE SCHEMA asir_ingest_permission_probe",
            "CREATE TABLE public.asir_ingest_permission_probe(id integer)",
            "CREATE TABLE asir_staging.asir_ingest_permission_probe(id integer)",
            "CREATE ROLE asir_ingest_permission_probe", "SET ROLE asir_reader",
        ):
            expect_denied(cursor, statement)
        # Privilegios exactos del esquema privado del publicador delta: si el
        # versionado está instalado se comprueba el conjunto necesario y se
        # niega cualquier sobrante (borrados, actualizaciones de ledger, TRUNCATE).
        cursor.execute("SELECT to_regclass('asir.rag_head') IS NOT NULL AS head, to_regclass('asir.rag_operations') IS NOT NULL AS ops")
        versioning = cursor.fetchone()
        denied_versioning = 0
        if versioning["head"] or versioning["ops"]:
            if not (versioning["head"] and versioning["ops"]):
                raise ProvisionFailure("ingest_versioning_schema_incomplete")
            for table, privileges in VERSION_PRIVILEGES.items():
                for privilege in privileges:
                    cursor.execute("SELECT has_table_privilege(%s,%s) AS allowed", (table, privilege))
                    if not cursor.fetchone()["allowed"]:
                        raise ProvisionFailure("ingest_required_table_privilege_missing")
            for function in VERSION_FUNCTIONS:
                cursor.execute("SELECT has_function_privilege(%s,'EXECUTE') AS allowed", (function,))
                if not cursor.fetchone()["allowed"]:
                    raise ProvisionFailure("ingest_required_function_privilege_missing")
            for statement in (
                "DELETE FROM asir.rag_head", "TRUNCATE asir.rag_head",
                "DELETE FROM asir.rag_operations",
                "UPDATE asir.rag_operations SET base_version='probe' WHERE operation_id='probe'",
            ):
                expect_denied(cursor, statement)
            denied_versioning = 4
    connection.rollback()
    return {"role": ROLE, "complete": True, "staging_dml_rolled_back": True, "staging_truncate_rolled_back": True, "staging_sequence_values_may_advance": True, "generated_search_text_tested": True, "production_dml_tested": False, "schemas": list(SCHEMAS), "tables_per_schema": len(TABLES), "sequences": len(sequences), "forbidden_operations_denied": 8 + denied_versioning, "rag_versioning_checked": denied_versioning > 0}


def provision(config_path, output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ProvisionFailure("ingest_config_exists_no_overwrite")
    admin = admin_config(config_path)
    # Bootstrap tiene DDL con commits propios; conexión aislada de la del rol.
    with connect(admin, application_name="asir-ingest-bootstrap", autocommit=False) as connection:
        bootstrap_staging(connection)
    password = secrets.token_urlsafe(48)
    config = {key: value for key, value in admin.items() if key != "role"}
    config.update(user=ROLE + "." + admin["project_ref"], password=password)
    created_file = False
    try:
        with connect(admin, application_name="asir-ingest-provision", autocommit=False) as connection:
            sequences = grant_role(connection, password)
            create_private(output, config)
            created_file = True
            # Archivo durable antes de commit: si falla el commit se elimina.
        return {"role": ROLE, "created": True, "sequences": len(sequences), "credential_saved": True}
    except BaseException:
        if created_file:
            output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-config", type=Path, default=ROOT / ".private/supabase-connection.json")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="Prueba con el rol restringido; revierte los cambios de ensayo.")
    args = parser.parse_args()
    try:
        if args.check:
            config = load_config(args.output, service=True)
            if config["role"] != ROLE:
                raise ProvisionFailure("ingest_role_required")
            with connect(config, application_name="asir-ingest-check", autocommit=False) as connection:
                report = check_role(connection)
        else:
            report = provision(args.admin_config, args.output)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception as error:
        # Nunca imprimir errores del driver (pueden incluir SQL, DSN o secretos).
        print(json.dumps({"complete": False, "error": str(error) if isinstance(error, ProvisionFailure) else "ingest_provision_or_check_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
