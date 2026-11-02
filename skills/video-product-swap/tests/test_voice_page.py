"""Exercise the voice pages in an isolated, offline desktop session."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from voice_acceptance import run


class VoicePageTests(unittest.TestCase):
    def test_voice_workflow_in_real_tk(self):
        report = run()
        self.assertTrue(report)
        self.assertTrue(all(report.values()), report)


if __name__ == "__main__":
    unittest.main()
