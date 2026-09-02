from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from secure_credentials import SecureCredentialStore, redact


class SecureCredentialTests(unittest.TestCase):
    def test_dpapi_round_trip_and_plaintext_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.dat"
            store = SecureCredentialStore(path)
            store.save({"TEST_API_KEY": "top-secret-value", "API_BASE": "https://example.com"})
            self.assertNotIn(b"top-secret-value", path.read_bytes())
            self.assertEqual(store.load()["TEST_API_KEY"], "top-secret-value")

    def test_redaction_hides_keys_only(self) -> None:
        values = {"TEST_API_KEY": "secret", "API_BASE": "https://example.com"}
        self.assertEqual(redact("secret https://example.com", values), "*** https://example.com")


if __name__ == "__main__":
    unittest.main()
