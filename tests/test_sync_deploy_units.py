"""Unidades systemd efectivas: la combinación base + drop-in debe publicar en delta."""
import configparser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / 'deploy/asir-sync.service'
DROPIN = ROOT / 'deploy/asir-sync-publish.conf'
REGISTRY = '/var/lib/asir-sync/resource-registry.json'


def exec_start(path):
    parser = configparser.ConfigParser(interpolation=None, comment_prefixes=('#',), strict=False)
    parser.read_string(path.read_text(encoding='utf-8'))
    return parser['Service']['ExecStart'].strip()


def arguments(line):
    return line.split()[2:]  # sin python ni script


class EffectiveUnitTests(unittest.TestCase):
    def setUp(self):
        self.base = exec_start(SERVICE)
        self.dropin = exec_start(DROPIN)
        self.base_args = arguments(self.base)
        self.dropin_args = arguments(self.dropin)

    def flag_value(self, args, flag):
        return args[args.index(flag) + 1]

    def test_both_units_activate_delta_mode(self):
        self.assertIn('--rag-delta-publish', self.base_args)
        self.assertIn('--rag-delta-publish', self.dropin_args)

    def test_both_units_use_the_resource_registry(self):
        for args in (self.base_args, self.dropin_args):
            self.assertIn('--rag-resource-map', args)
            self.assertEqual(self.flag_value(args, '--rag-resource-map'), REGISTRY)

    def test_dropin_repeats_base_and_adds_publication(self):
        # Sin flags base omitidas: el drop-in solo añade publicación.
        shared = [arg for arg in self.base_args if arg != '--execute']
        self.assertTrue(set(shared) <= set(self.dropin_args))
        self.assertIn('--publish', self.dropin_args)
        self.assertIn('--staging', self.dropin_args)
        self.assertIn('--rollback-candidate', self.dropin_args)
        self.assertEqual(self.flag_value(self.dropin_args, '--rollback-candidate'),
                         '/var/lib/asir-sync/bootstrap')
        self.assertIn('--probe-token-file', self.dropin_args)
        self.assertNotIn('--legacy-full-publish', self.dropin_args)

    def test_sync_unit_triggers_separate_backup_on_success(self):
        text = SERVICE.read_text(encoding='utf-8')
        [unit] = [section for section in text.split('\n[') if section.startswith('[Unit]')]
        self.assertIn('OnSuccess=asir-rag-backup.service', unit)

    def test_dropin_preserves_sudo_for_mcp_restart(self):
        parser = configparser.ConfigParser(interpolation=None, comment_prefixes=('#',), strict=False)
        parser.read_string(DROPIN.read_text(encoding='utf-8'))
        self.assertEqual(parser['Service']['NoNewPrivileges'], 'no')


if __name__ == '__main__':
    unittest.main()
