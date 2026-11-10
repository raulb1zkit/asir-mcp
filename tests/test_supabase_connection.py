import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.supabase_connection import EXPECTED_PROJECT, check_connection, load_connection, parse_connection, save_connection

URI = f"postgresql://postgres.{EXPECTED_PROJECT}:fake%3Apassword%26value@aws-0-eu-west-2.pooler.supabase.com:6543/postgres"


class ConnectionTests(unittest.TestCase):
    def test_parse_percent_encoded_password_and_project(self):
        config = parse_connection(URI)
        self.assertEqual(config['password'], 'fake:password&value')
        self.assertEqual(config['project_ref'], EXPECTED_PROJECT)

    def test_wrong_host_project_or_options_rejected_without_echoing_secret(self):
        for uri in [URI.replace('pooler.supabase.com', 'example.org'), URI.replace(EXPECTED_PROJECT, 'otherproject'), URI + '?sslmode=disable', URI.replace('fake%3Apassword%26value', 'fake%0Asecret')]:
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError) as caught:
                    parse_connection(uri)
                self.assertNotIn('password', str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))

    def test_save_private_roundtrip_and_refuse_overwrite_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.private/connection.json'
            config = parse_connection(URI)
            save_connection(config, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_connection(path), config)
            with self.assertRaises(FileExistsError):
                save_connection(config, path)
            link = path.with_name('link.json')
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load_connection(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_connection(path)

    def test_check_password_only_in_temporary_file_and_readonly_tls(self):
        config = parse_connection(URI)
        with tempfile.TemporaryDirectory() as tmp:
            def fake_run(args, **kwargs):
                self.assertNotIn(config['password'], ' '.join(args))
                self.assertNotIn('PGPASSWORD', kwargs['env'])
                self.assertEqual(kwargs['env']['PGSSLMODE'], 'verify-full')
                pgpass = Path(kwargs['env']['PGPASSFILE'])
                self.assertEqual(stat.S_IMODE(pgpass.stat().st_mode), 0o600)
                self.assertIn('fake\\:password&value', pgpass.read_text())
                self.assertIn('BEGIN READ ONLY', args[-1])
                return SimpleNamespace(returncode=0, stdout=json.dumps({'read_only':'on','database':'postgres'}))
            with patch('scripts.supabase_connection.shutil.which', return_value='/usr/bin/psql'), patch('scripts.supabase_connection.subprocess.run', side_effect=fake_run):
                self.assertEqual(check_connection(config, Path(tmp))['read_only'], 'on')
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_driver_error_redacted_and_temporary_secret_removed(self):
        config = parse_connection(URI)
        with tempfile.TemporaryDirectory() as tmp:
            result = SimpleNamespace(returncode=1, stderr=config['password'], stdout='')
            with patch('scripts.supabase_connection.shutil.which', return_value='/usr/bin/psql'), patch('scripts.supabase_connection.subprocess.run', return_value=result):
                with self.assertRaises(ValueError) as caught:
                    check_connection(config, Path(tmp))
                self.assertNotIn(config['password'], str(caught.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_project_ca_used_without_relaxing_tls_validation(self):
        config = parse_connection(URI)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            certificate = root / 'supabase-ca.crt'
            certificate.write_text('fixture certificate; subprocess is mocked')
            result = SimpleNamespace(returncode=0, stdout=json.dumps({'read_only': 'on'}))
            with patch('scripts.supabase_connection.shutil.which', return_value='/usr/bin/psql'), patch('scripts.supabase_connection.subprocess.run', return_value=result) as run:
                check_connection(config, root)
                env = run.call_args.kwargs['env']
                self.assertEqual(env['PGSSLROOTCERT'], str(certificate.resolve()))
                self.assertEqual(env['PGSSLMODE'], 'verify-full')
            self.assertEqual(list(root.iterdir()), [certificate])
