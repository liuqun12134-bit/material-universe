from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from image_preprocessor.core import Credential, ImagePreprocessorError, normalize_analysis
from image_preprocessor.credentials import CredentialManager
from image_preprocessor.registry import ModelRegistry
from image_preprocessor.adapters.seedream import SeedreamAsyncAdapter, _state_path


class AnalysisTests(unittest.TestCase):
    def test_passes_only_when_every_required_flag_and_three_views_pass(self) -> None:
        result = normalize_analysis(
            {
                "clean_studio_background": True,
                "no_chinese_text": True,
                "multi_view": True,
                "view_count": 4,
                "product_complete_and_clear": True,
                "cross_view_consistent": True,
                "reasons": [],
                "visible_chinese_text": [],
            }
        )
        self.assertTrue(result["passed"])

    def test_two_views_cannot_pass_as_multiview(self) -> None:
        result = normalize_analysis(
            {
                "clean_studio_background": True,
                "no_chinese_text": True,
                "multi_view": True,
                "view_count": 2,
                "product_complete_and_clear": True,
                "cross_view_consistent": True,
                "reasons": [],
                "visible_chinese_text": [],
            }
        )
        self.assertFalse(result["passed"])
        self.assertFalse(result["multi_view"])


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ModelRegistry(SKILL_ROOT / "references")

    def test_defaults_and_aliases(self) -> None:
        self.assertEqual(self.registry.default("vision_analysis"), "deepseek-v4-flash-vision-exp")
        self.assertEqual(self.registry.default("image_generation"), "seedream-5.0-pro")
        self.assertEqual(
            self.registry.resolve("seedream", "image_generation").api_model,
            "seedream-5.0-pro",
        )

    def test_capabilities_cannot_be_mixed(self) -> None:
        with self.assertRaisesRegex(ImagePreprocessorError, "不能用于"):
            self.registry.resolve("seedream", "vision_analysis")


class CredentialIsolationTests(unittest.TestCase):
    def test_direct_skill_ignores_system_key(self) -> None:
        providers = ModelRegistry(SKILL_ROOT / "references").providers
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "system-key"}, clear=False):
                manager = CredentialManager(root, providers, "skill-env")
                with self.assertRaisesRegex(ImagePreprocessorError, "不会回退"):
                    manager.resolve("deepseek")

    def test_host_app_uses_only_explicit_environment(self) -> None:
        providers = ModelRegistry(SKILL_ROOT / "references").providers
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "host-key", "DEEPSEEK_API_BASE": "https://host.example.com"},
            clear=False,
        ):
            credential = CredentialManager(SKILL_ROOT, providers, "host-app").resolve("deepseek")
        self.assertEqual(credential.api_key, "host-key")
        self.assertEqual(credential.api_base, "https://host.example.com")


class SeedreamPlanTests(unittest.TestCase):
    def test_plan_uses_documented_async_endpoint_and_configured_reference_field(self) -> None:
        registry = ModelRegistry(SKILL_ROOT / "references")
        model = registry.resolve("seedream", "image_generation")
        provider = registry.providers[model.provider]
        credential = Credential(
            provider=model.provider,
            api_base=provider["default_api_base"],
            api_key=None,
            api_key_source=None,
            submit_path=provider["submit_path"],
            poll_path=provider["poll_path"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "input.png"
            image.write_bytes(b"not-read-in-dry-run")
            plan = SeedreamAsyncAdapter().plan(
                image, model, credential, Path(temp_dir) / "out.png", "2048x2048"
            )
        self.assertTrue(plan["endpoint"].endswith("/v1/images/async/generations"))
        self.assertEqual(plan["request_payload"]["model"], "seedream-5.0-pro")
        self.assertEqual(plan["request_payload"]["image"], ["<local-image-as-data-uri>"])
        self.assertFalse(plan["reference_field_confirmed_by_supplied_doc"])

    def test_poll_retries_transient_ssl_disconnect_without_resubmitting(self) -> None:
        credential = Credential(
            provider="kaiyuncode-images",
            api_base="https://kaiyuncode.com",
            api_key="test-key",
            api_key_source="KAIYUNCODE_API_KEY",
            submit_path="v1/images/async/generations",
            poll_path="v1/images/async/{task_id}",
        )
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = (
            b'{"status":"completed","data":[{"url":"https://example.com/result.png"}]}'
        )
        with (
            patch(
                "image_preprocessor.adapters.seedream.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("temporary ssl eof"), response],
            ) as mocked_urlopen,
            patch("image_preprocessor.adapters.seedream.time.sleep"),
        ):
            payload, url = SeedreamAsyncAdapter()._poll("task-123", credential, 30)
        self.assertEqual(mocked_urlopen.call_count, 2)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(url, "https://example.com/result.png")

    def test_task_id_is_saved_before_polling(self) -> None:
        registry = ModelRegistry(SKILL_ROOT / "references")
        model = registry.resolve("seedream", "image_generation")
        credential = Credential(
            provider=model.provider,
            api_base="https://kaiyuncode.com",
            api_key="test-key",
            api_key_source="KAIYUNCODE_API_KEY",
            submit_path="v1/images/async/generations",
            poll_path="v1/images/async/{task_id}",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "input.png"
            output = Path(temp_dir) / "output.png"
            image.write_bytes(b"input-image")
            adapter = SeedreamAsyncAdapter()

            def poll(task_id, _credential, _timeout):
                saved = json.loads(_state_path(output).read_text(encoding="utf-8"))
                self.assertEqual(saved["task_id"], "task-456")
                return {"status": "completed"}, "https://example.com/result.png"

            def download(_url, target, _credential):
                target.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
                return target

            with (
                patch.object(adapter, "_submit", return_value={"task_id": "task-456", "status": "in_progress"}),
                patch.object(adapter, "_poll", side_effect=poll),
                patch.object(adapter, "_download", side_effect=download),
            ):
                result = adapter.generate(image, model, credential, output, "2048x2048", 30)
        self.assertEqual(result["task_id"], "task-456")
        self.assertFalse(result["resumed_existing_task"])


if __name__ == "__main__":
    unittest.main()
