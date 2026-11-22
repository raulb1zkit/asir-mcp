import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.rag_bundle import BundleError, create_bundle, read_bundle, restore_bundle
from scripts.rag_versions import TABLES, canonical, seal


def example(value='before'):
    rows = {table: {} for table in TABLES}
    rows['chunks'] = {'chunk:' + str(n): {'text': value if n == 0 else str(n)} for n in range(1000)}
    rows['config']['embedding'] = {'dimensions': 384}
    return seal(rows, {})


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def pack(self, name, manifest):
        source = self.root / (name + '.json')
        source.write_bytes(canonical(manifest))
        output = self.root / name
        create_bundle(source, output)
        return output

    def test_roundtrip_and_exclusive_output(self):
        original = example()
        output = self.pack('before', original)
        self.assertEqual(read_bundle(output), original)
        restored = self.root / 'restored.json'
        restore_bundle(output, restored)
        self.assertEqual(json.loads(restored.read_text()), original)
        with self.assertRaises(FileExistsError):
            restore_bundle(output, restored)

    def test_one_record_edit_changes_only_one_data_partition(self):
        before = self.pack('before', example())
        after = self.pack('after', example('after'))
        a = {e['path']: e['sha256'] for e in json.loads((before / 'bundle.json').read_text())['files']}
        b = {e['path']: e['sha256'] for e in json.loads((after / 'bundle.json').read_text())['files']}
        changed = [key for key in a if a[key] != b[key]]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0].startswith('rows/chunks/'))

    def test_corruption_and_extra_file_rejected(self):
        bundle = self.pack('before', example())
        extra = bundle / 'private-config.json'
        extra.write_text('synthetic')
        with self.assertRaisesRegex(BundleError, 'unexpected_files'):
            read_bundle(bundle)
        extra.unlink()
        entry = json.loads((bundle / 'bundle.json').read_text())['files'][0]
        (bundle / entry['path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(BundleError, 'hash_mismatch'):
            read_bundle(bundle)

    def test_traversal_and_symlink_rejected(self):
        bundle = self.pack('before', example())
        path = bundle / 'bundle.json'
        header = json.loads(path.read_text())
        first = header['files'][0]
        actual = bundle / first['path']
        saved = actual.read_bytes()
        actual.unlink()
        outside = self.root / 'outside'
        outside.write_bytes(saved)
        actual.symlink_to(outside)
        with self.assertRaises(BundleError):
            read_bundle(bundle)
        actual.unlink()
        actual.write_bytes(saved)
        header['files'][0]['path'] = '../outside'
        path.write_bytes(canonical(header))
        with self.assertRaisesRegex(BundleError, 'path_invalid'):
            read_bundle(bundle)


if __name__ == '__main__':
    unittest.main()
