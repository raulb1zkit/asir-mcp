import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import provision_ingest as ingest


class FileTests(unittest.TestCase):
    def test_secret_saved_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            ingest.create_private(path, {"password": "test-only-secret"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                ingest.create_private(path, {"password": "replacement"})
            self.assertEqual(json.loads(path.read_text())["password"], "test-only-secret")

    def test_symlink_target_never_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("original")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                ingest.create_private(link, {"password": "test"})
            self.assertEqual(target.read_text(), "original")

    def test_existing_config_refuses_before_admin_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text("existing")
            with patch.object(ingest, "admin_config") as admin:
                with self.assertRaisesRegex(ingest.ProvisionFailure, "no_overwrite"):
                    ingest.provision(Path("admin"), path)
                admin.assert_not_called()

    def test_driver_error_redacted(self):
        stderr = io.StringIO()
        with patch.object(ingest, "provision", side_effect=RuntimeError("password SECRET DSN SQL")), patch("sys.argv", ["provision_ingest.py"]), contextlib.redirect_stderr(stderr):
            self.assertEqual(ingest.main(), 1)
        self.assertNotIn("SECRET", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())["error"], "ingest_provision_or_check_failed")


class DenialTests(unittest.TestCase):
    def test_permission_denial_rollback_always_runs(self):
        class Denied(Exception): sqlstate = "42501"
        class Cursor:
            def __init__(self): self.commands = []
            def execute(self, query):
                self.commands.append(query)
                if query == "FORBIDDEN": raise Denied()
        cursor = Cursor()
        ingest.expect_denied(cursor, "FORBIDDEN")
        self.assertEqual(cursor.commands[-2:], ["ROLLBACK TO SAVEPOINT permission_probe", "RELEASE SAVEPOINT permission_probe"])

    def test_wrong_failure_or_success_is_not_accepted_as_permission_denial(self):
        class Wrong(Exception): sqlstate = "42P01"
        for failure in (None, Wrong()):
            class Cursor:
                def execute(self, query):
                    if query == "FORBIDDEN" and failure: raise failure
            with self.assertRaisesRegex(ingest.ProvisionFailure, "not_denied"):
                ingest.expect_denied(Cursor(), "FORBIDDEN")


@unittest.skipUnless(importlib.util.find_spec("psycopg"), "requires project venv psycopg")
class GrantsTests(unittest.TestCase):
    class Cursor:
        def __init__(self, exists=False, sequences=None):
            self.queries = []
            self.exists = exists
            self.sequences = sequences if sequences is not None else [{"schema": schema, "sequence": name} for schema in ingest.SCHEMAS for name in ("citations_id_seq", "embedding_windows_id_seq")]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None):
            self.queries.append((query.as_string() if hasattr(query, "as_string") else query, params))
        def fetchone(self): return {"exists": 1} if self.exists else None
        def fetchall(self): return self.sequences

    def test_grants_are_fixed_explicit_and_no_ownership_or_sequence_update(self):
        cursor = self.Cursor()
        connection = unittest.mock.Mock()
        connection.cursor.return_value = cursor
        result = ingest.grant_role(connection, "test-only-password")
        self.assertEqual(len(result), 4)
        sql = "\n".join(query for query, params in cursor.queries)
        self.assertIn("LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", sql)
        self.assertEqual(sql.count("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLE"), 16)
        self.assertEqual(sql.count("GRANT USAGE, SELECT ON SEQUENCE"), 4)
        self.assertNotIn("ALL TABLES", sql)
        self.assertNotIn("ALL SEQUENCES", sql)
        self.assertNotIn("OWNER TO", sql)
        self.assertNotIn("WITH GRANT OPTION", sql)
        self.assertNotIn("GRANT CREATE", sql)
        self.assertNotIn("GRANT UPDATE ON SEQUENCE", sql)

    def test_existing_role_is_not_altered_or_rotated(self):
        cursor = self.Cursor(exists=True)
        connection = unittest.mock.Mock()
        connection.cursor.return_value = cursor
        with self.assertRaisesRegex(ingest.ProvisionFailure, "no_rotation"):
            ingest.grant_role(connection, "new-password")
        self.assertEqual(len(cursor.queries), 1)

    def test_unexpected_sequences_abort_grants_transaction(self):
        cursor = self.Cursor(sequences=[])
        connection = unittest.mock.Mock()
        connection.cursor.return_value = cursor
        with self.assertRaisesRegex(ingest.ProvisionFailure, "sequence_layout"):
            ingest.grant_role(connection, "test-password")


class BootstrapTests(unittest.TestCase):
    def test_existing_staging_never_runs_schema_ddl(self):
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {"served": True, "staging": True}
        connection = unittest.mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch("scripts.import_supabase.create_schema") as create:
            self.assertFalse(ingest.bootstrap_staging(connection))
            create.assert_not_called()

    def test_missing_served_schema_is_not_created(self):
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {"served": False, "staging": False}
        connection = unittest.mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch("scripts.import_supabase.create_schema") as create:
            with self.assertRaisesRegex(ingest.ProvisionFailure, "served_schema_missing"):
                ingest.bootstrap_staging(connection)
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
