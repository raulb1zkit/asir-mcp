import unittest
import importlib.util

if importlib.util.find_spec("numpy"):
    from scripts.audit_all_vectors import original_batches


@unittest.skipUnless(importlib.util.find_spec("numpy"), "Vector audit requires numpy")
class BatchReconstructionTests(unittest.TestCase):
    def test_reconstructs_sorted_stable_batches_without_duplicates(self):
        windows = [{"tokens": n} for n in [4, 1, 2, 1, 3, 2, 4, 3, 5]]
        self.assertEqual(original_batches(windows, [0, 2, 8], batch_size=4), [[1, 3, 2, 5], [4, 7, 0, 6], [8]])
        self.assertEqual(original_batches(windows, [1, 3], batch_size=4), [[1, 3, 2, 5]])


if __name__ == "__main__":
    unittest.main()
