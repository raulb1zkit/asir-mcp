import json
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.manage_oauth_users import initialize, load, update
from scripts.mcp_auth import TokenConfigError
from scripts.oauth_mcp import OAuthUserStore

USER = "d834aafa-b060-45df-b64d-3adab57cb091"


class UserManagementTests(unittest.TestCase):
    def test_grant_and_revoke_are_private_and_immediately_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.json"
            initialize(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            store = OAuthUserStore(path)
            self.assertIsNone(store.get_subjects(USER))
            update(path, USER, ["ASO", "SAD"])
            self.assertEqual(store.get_subjects(USER), ["ASO", "SAD"])
            update(path, USER, ["IAW"])
            self.assertEqual(store.get_subjects(USER), ["IAW"])
            update(path, USER)
            self.assertIsNone(store.get_subjects(USER))
            self.assertEqual(load(path)["users"], [])

    def test_invalid_user_and_existing_file_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.json"
            initialize(path)
            before = path.read_bytes()
            with self.assertRaises(TokenConfigError):
                update(path, "not-a-uuid", ["ASO"])
            with self.assertRaises(TokenConfigError):
                initialize(path)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
