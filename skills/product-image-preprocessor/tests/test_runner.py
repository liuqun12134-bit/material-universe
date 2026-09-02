from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from image_preprocessor.runner import ImagePreprocessor


def analysis(passed: bool) -> dict:
    return {
        "model": "vision-model",
        "provider": "deepseek",
        "clean_studio_background": passed,
        "no_chinese_text": passed,
        "multi_view": passed,
        "view_count": 4 if passed else 1,
        "product_complete_and_clear": True,
        "cross_view_consistent": True,
        "reasons": [] if passed else ["不是多视角棚拍图。"],
        "visible_chinese_text": [] if passed else ["中文"],
        "passed": passed,
    }


class FakeVision:
    def __init__(self, results: list[dict]) -> None:
        self.results = list(results)

    def analyze(self, image_path, model, credential, timeout):
        return self.results.pop(0)


class FakeImage:
    def generate(self, image_path, model, credential, output, size, timeout):
        output.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        return {
            "success": True,
            "technical_completed": True,
            "quality_passed": None,
            "output": str(output),
            "model": model.api_model,
        }


class PipelineTests(unittest.TestCase):
    def _runner(self) -> ImagePreprocessor:
        env = {"DEEPSEEK_API_KEY": "vision-key", "KAIYUNCODE_API_KEY": "image-key"}
        with patch.dict(os.environ, env, clear=False):
            return ImagePreprocessor(SKILL_ROOT, credential_source="host-app")

    def test_good_input_passes_through_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "input.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
            runner = self._runner()
            with patch("image_preprocessor.runner.create_vision_adapter", return_value=FakeVision([analysis(True)])):
                result = runner.run(source, None, None, None, None, 30)
        self.assertEqual(result["action"], "passed_through")
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["final_image"], str(source.resolve()))

    def test_bad_input_generates_once_and_rechecks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "input.png"
            output = Path(temp_dir) / "output.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
            runner = self._runner()
            with (
                patch(
                    "image_preprocessor.runner.create_vision_adapter",
                    return_value=FakeVision([analysis(False), analysis(True)]),
                ),
                patch("image_preprocessor.runner.create_image_adapter", return_value=FakeImage()),
            ):
                result = runner.run(source, output, None, None, None, 30)
        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "generated")
        self.assertEqual(result["generation_calls"], 1)

    def test_rejected_generation_stops_with_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "input.png"
            output = Path(temp_dir) / "output.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
            runner = self._runner()
            with (
                patch(
                    "image_preprocessor.runner.create_vision_adapter",
                    return_value=FakeVision([analysis(False), analysis(False)]),
                ),
                patch("image_preprocessor.runner.create_image_adapter", return_value=FakeImage()),
            ):
                result = runner.run(source, output, None, None, None, 30)
        self.assertFalse(result["success"])
        self.assertEqual(result["action"], "generated_but_rejected")
        self.assertIsNone(result["final_image"])
        self.assertTrue(result["candidate_image"].endswith("output.png"))


if __name__ == "__main__":
    unittest.main()
