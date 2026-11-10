import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.supabase_db import (
    DatabaseConfigError,
    EXPECTED_PROJECT,
    load_config,
    validate_config,
    vector_literal,
)


def config(root, *, user="postgres." + EXPECTED_PROJECT, **overrides):
    value = {
        "project_ref": EXPECTED_PROJECT,
        "host": "aws-0-eu-west-2.pooler.supabase.com",
        "port": 6543,
        "user": user,
        "password": "not-a-real-secret",
        "database": "postgres",
        "sslrootcert": str(root / "ca.crt"),
    }
    value.update(overrides)
    return value


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "ca.crt").write_text("test certificate", encoding="utf-8")

    def test_admin_and_service_roles_are_separated(self):
        admin = validate_config(config(self.root))
        self.assertEqual(admin["role"], "postgres")
        service = validate_config(config(self.root, user="asir_reader." + EXPECTED_PROJECT), service=True)
        self.assertEqual(service["role"], "asir_reader")
        with self.assertRaisesRegex(DatabaseConfigError, "admin_role"):
            validate_config(config(self.root, user="asir_reader." + EXPECTED_PROJECT))
        with self.assertRaisesRegex(DatabaseConfigError, "admin_role"):
            validate_config(config(self.root), service=True)

    def test_wrong_project_host_port_or_user_is_rejected(self):
        for value in [
            config(self.root, project_ref="other"),
            config(self.root, host="example.org"),
            config(self.root, port=5432),
            config(self.root, user="postgres.other"),
            config(self.root, database="other"),
        ]:
            with self.subTest(value=value):
                with self.assertRaises(DatabaseConfigError):
                    validate_config(value)

    def test_control_characters_and_missing_ca_are_rejected(self):
        with self.assertRaisesRegex(DatabaseConfigError, "password"):
            validate_config(config(self.root, password="bad\nvalue"))
        with self.assertRaisesRegex(DatabaseConfigError, "missing_ca"):
            validate_config(config(self.root, sslrootcert=str(self.root / "missing.crt")))

    def test_load_requires_private_regular_file(self):
        path = self.root / "connection.json"
        path.write_text(json.dumps(config(self.root)), encoding="utf-8")
        os.chmod(path, 0o600)
        self.assertEqual(load_config(path)["role"], "postgres")
        path.chmod(0o644)
        with self.assertRaisesRegex(DatabaseConfigError, "600"):
            load_config(path)

    def test_vector_literal_keeps_float32_roundtrip(self):
        self.assertEqual(vector_literal([0.0, 1.0, -0.5]), "[0,1,-0.5]")
        self.assertTrue(vector_literal([0.123456789]).startswith("[0.123456789"))
