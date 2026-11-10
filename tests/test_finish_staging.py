import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import finish_staging


class StagingRunnerTests(unittest.TestCase):
    def test_missing_embeddings_does_not_start_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(finish_staging, "ROOT", Path(tmp)), patch("sys.argv", ["finish_staging", "--embedding-pid", "2147483647"]), patch("subprocess.run") as run:
                self.assertEqual(finish_staging.main(), 1)
                run.assert_not_called()
            status = json.loads((Path(tmp) / "data/all/eval/nightly-status.json").read_text())
            self.assertIn("semantic_index_missing", status["failures"])

    def test_unrelated_live_pid_does_not_start_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(finish_staging, "ROOT", Path(tmp)), patch("sys.argv", ["finish_staging", "--embedding-pid", str(os.getpid())]), patch("subprocess.run") as run:
                self.assertEqual(finish_staging.main(), 1)
                run.assert_not_called()
            status = json.loads((Path(tmp) / "data/all/eval/nightly-status.json").read_text())
            self.assertIn("embedding_pid_reused", status["failures"])


if __name__ == "__main__":
    unittest.main()
