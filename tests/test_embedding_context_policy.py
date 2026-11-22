"""La migración de prefijos E5 nunca debe activarse por una edición ordinaria."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scripts import sync_materials as sync


class EmbeddingPolicyTests(unittest.TestCase):
    def test_published_configuration_wins_over_resume_and_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, completed = root / 'old.npz', root / 'completed.npz'
            np.savez(old, metadata=np.array(json.dumps({'context': 'subject-section-title', 'overlap_tokens': 64})))
            np.savez(completed, metadata=np.array(json.dumps({'context': sync.EMBEDDING_CONTEXT, 'overlap_tokens': 64})))
            self.assertEqual(sync.embedding_context_for_run(old, completed), 'subject-section-title')
            self.assertEqual(sync.embedding_context_for_run(None, old), 'subject-section-title')
            self.assertEqual(sync.embedding_context_for_run(old, explicit=sync.EMBEDDING_CONTEXT), sync.EMBEDDING_CONTEXT)

    def test_unknown_previous_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.npz'
            for metadata in ({'context': 'unknown', 'overlap_tokens': 64},
                             {'context': 'subject-section-title', 'overlap_tokens': 128}):
                np.savez(path, metadata=np.array(json.dumps(metadata)))
                with self.assertRaisesRegex(sync.SyncFailure, 'previous_embedding_configuration_invalid'):
                    sync.embedding_context_for_run(path)
