from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from media_inspection import VideoInfo, classify_aspect_ratio


class MediaInspectionTests(unittest.TestCase):
    def test_standard_ratios_follow_source(self) -> None:
        self.assertEqual(classify_aspect_ratio(1080, 1920), "9:16")
        self.assertEqual(classify_aspect_ratio(1920, 1080), "16:9")
        self.assertEqual(classify_aspect_ratio(1024, 1024), "1:1")
        self.assertEqual(classify_aspect_ratio(1440, 1080), "4:3")

    def test_nonstandard_ratio_falls_back_to_vertical(self) -> None:
        self.assertEqual(classify_aspect_ratio(1000, 700), "9:16")

    def test_source_duration_rounds_to_nearest_second(self) -> None:
        self.assertEqual(VideoInfo(8.6, 1080, 1920, "9:16").duration_seconds, 9)
        self.assertEqual(VideoInfo(8.4, 1080, 1920, "9:16").duration_seconds, 8)


if __name__ == "__main__":
    unittest.main()
