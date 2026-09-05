from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from model_runner.adapters.base import KaiyunRelayAdapter
from model_runner.adapters.dashscope_videoedit import DashScopeVideoEditAdapter
from model_runner.core import GenerationRequest, ModelSpec, VideoGenerationError, parse_reference
from model_runner.credentials import Credential
from model_runner.registry import ModelRegistry
from model_runner.runner import ModelRunner


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ModelRegistry(SKILL_ROOT / "references")

    def test_aliases_resolve_to_specialized_adapters(self) -> None:
        self.assertEqual(self.registry.resolve("wan3").adapter, "wan3")
        self.assertEqual(self.registry.resolve("omniflash").adapter, "omniflash")
        official = self.registry.resolve("wan-official")
        self.assertEqual(official.adapter, "dashscope_videoedit")
        self.assertEqual(official.provider, "dashscope")
        self.assertEqual(official.api_model, "wan2.7-videoedit")

    def test_unknown_model_uses_compatibility_adapter(self) -> None:
        model = self.registry.resolve("future-model")
        self.assertEqual(model.adapter, "generic")
        self.assertFalse(model.registered)
        self.assertEqual(model.api_model, "future-model")


class DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_env = dict(os.environ)
        # Test credentials must not depend on the developer's installed .env.
        env_loader = patch("model_runner.credentials.load_env")
        env_loader.start()
        self.addCleanup(env_loader.stop)
        os.environ.pop("KAIYUNCODE_API_KEY", None)
        os.environ.pop("VIDEO_SWAP_API_KEY", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_omniflash_accepts_local_mp4_and_public_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.mp4"
            video.write_bytes(b"test")
            args = Namespace(
                prompt="原始提示词",
                model="omniflash",
                reference=[str(video), "image=https://example.com/ref"],
                output=str(Path(temp_dir) / "result.mp4"),
                duration=None,
                dry_run=True,
            )
            result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["adapter"], "omniflash")
            self.assertEqual(result["resolved_model"], "omni_video_edit")
            self.assertNotIn("api_key", result)

    def test_omniflash_rejects_local_reference_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.mp4"
            image = Path(temp_dir) / "ref.png"
            video.write_bytes(b"test")
            image.write_bytes(b"test")
            args = Namespace(
                prompt="原始提示词",
                model="omniflash",
                reference=[str(video), str(image)],
                output=str(Path(temp_dir) / "result.mp4"),
                duration=None,
                dry_run=True,
            )
            with self.assertRaisesRegex(VideoGenerationError, "公网 HTTPS 参考图"):
                ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)

    def test_wan3_defaults_duration_and_maps_remote_fields(self) -> None:
        args = Namespace(
            prompt="原始提示词",
            model="wan3",
            reference=["image=https://example.com/ref", "video=https://example.com/source"],
            output=str(Path(tempfile.gettempdir()) / "wan3-dry-run-result.mp4"),
            duration=None,
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertEqual(result["adapter"], "wan3")
        self.assertEqual(result["request_payload"]["duration"], 5)
        self.assertEqual(result["request_payload"]["images"], ["https://example.com/ref"])
        self.assertEqual(
            result["request_payload"]["reference_video_urls"],
            ["https://example.com/source"],
        )

    def test_output_specifications_reach_payload(self) -> None:
        args = Namespace(
            prompt="原始提示词",
            model="wan3",
            reference=["image=https://example.com/ref"],
            output=str(Path(tempfile.gettempdir()) / "wan3-specs-dry-run-result.mp4"),
            duration=10,
            aspect_ratio="16:9",
            resolution="1080p",
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        payload = result["request_payload"]
        self.assertEqual(payload["duration"], 10)
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["resolution"], "1080p")

    def test_legacy_api_key_name_remains_compatible(self) -> None:
        os.environ["VIDEO_SWAP_API_KEY"] = "legacy-test-secret"
        args = Namespace(
            prompt="原始提示词",
            model="wan3",
            reference=["image=https://example.com/ref"],
            output=str(Path(tempfile.gettempdir()) / "wan3-legacy-key-result.mp4"),
            duration=5,
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertTrue(result["credential_configured"])
        self.assertEqual(result["credential_source"], "VIDEO_SWAP_API_KEY")

    def test_wan_official_dry_run_uses_official_payload_without_uploading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.mp4"
            image = Path(temp_dir) / "product.png"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            args = Namespace(
                prompt="保持原视频动作，只替换产品",
                model="wan-official",
                reference=[f"video={video}", f"image={image}"],
                output=str(Path(temp_dir) / "result.mp4"),
                duration=8,
                aspect_ratio="9:16",
                resolution="720p",
                dry_run=True,
            )

            result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)

        self.assertEqual(result["adapter"], "dashscope_videoedit")
        self.assertEqual(result["provider"], "dashscope")
        payload = result["request_payload"]
        self.assertEqual(payload["model"], "wan2.7-videoedit")
        self.assertEqual(payload["input"]["prompt"], "保持原视频动作，只替换产品")
        self.assertEqual(payload["input"]["media"][0]["type"], "video")
        self.assertTrue(payload["input"]["media"][0]["url"].startswith("file:"))
        self.assertEqual(payload["input"]["media"][1]["type"], "reference_image")
        self.assertFalse(payload["parameters"]["prompt_extend"])
        self.assertFalse(payload["parameters"]["watermark"])
        self.assertEqual(payload["parameters"]["audio_setting"], "origin")
        self.assertEqual(payload["parameters"]["resolution"], "720P")

    def test_wan_official_rejects_unsupported_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.mp4"
            image = Path(temp_dir) / "product.png"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            args = Namespace(
                prompt="原始提示词",
                model="wan-official",
                reference=[f"video={video}", f"image={image}"],
                output=str(Path(temp_dir) / "result.mp4"),
                duration=5,
                aspect_ratio="9:16",
                resolution="480p",
                dry_run=True,
            )
            with self.assertRaisesRegex(VideoGenerationError, "720p 或 1080p"):
                ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)


class ReferenceTests(unittest.TestCase):
    def test_non_https_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(VideoGenerationError, "公网 HTTPS"):
            parse_reference("image=http://example.com/ref.png")


class DeliveryTests(unittest.TestCase):
    @staticmethod
    def request(output: Path) -> GenerationRequest:
        model = ModelSpec(
            id="test-model",
            api_model="test-model",
            aliases=(),
            provider="test-provider",
            adapter="generic",
            input_summary="test",
        )
        return GenerationRequest(
            prompt="保持原样",
            requested_model="test-model",
            model=model,
            references=[],
            output=output,
            duration=None,
            aspect_ratio=None,
            resolution=None,
        )

    @staticmethod
    def credential() -> Credential:
        return Credential(
            provider="test-provider",
            api_base="https://api.example.com/v1",
            api_key="test-key",
            api_key_source="TEST_KEY",
            submit_path="videos",
        )

    def test_download_success_saves_local_file_and_returns_url(self) -> None:
        class ResultAdapter(KaiyunRelayAdapter):
            def submit(self, session, endpoint, headers, payload, request):
                return {"video_url": "https://assets.example.com/result.mp4"}

        class Response:
            ok = True
            content = b"video-bytes"

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.mp4"
            with patch("requests.Session", return_value=Session()):
                result = ResultAdapter().execute(self.request(output), self.credential())
            self.assertEqual(output.read_bytes(), b"video-bytes")

        self.assertTrue(result["success"])
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(result["delivery"], "local_file")
        self.assertEqual(result["video_url"], "https://assets.example.com/result.mp4")

    def test_download_failure_returns_url_without_resubmitting(self) -> None:
        class ResultAdapter(KaiyunRelayAdapter):
            submit_calls = 0

            def submit(self, session, endpoint, headers, payload, request):
                self.submit_calls += 1
                return {"video_url": "https://assets.example.com/result.mp4"}

        class Session:
            def get(self, *args, **kwargs):
                raise ConnectionError("TLS connection closed")

        adapter = ResultAdapter()
        with patch("requests.Session", return_value=Session()):
            result = adapter.execute(
                self.request(Path(tempfile.gettempdir()) / "unused-result.mp4"),
                self.credential(),
            )

        self.assertEqual(adapter.submit_calls, 1)
        self.assertTrue(result["success"])
        self.assertFalse(result["local_downloaded"])
        self.assertEqual(result["delivery"], "url_fallback")
        self.assertEqual(result["video_url"], "https://assets.example.com/result.mp4")
        self.assertIn("下载视频失败", result["download_error"])

    def test_poll_waits_for_url_after_success_status(self) -> None:
        class Response:
            ok = True

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.payloads = [
                    {"status": "success"},
                    {
                        "status": "success",
                        "video_url": "https://assets.example.com/result.mp4",
                    },
                ]
                self.calls = 0

            def get(self, *args, **kwargs):
                payload = self.payloads[self.calls]
                self.calls += 1
                return Response(payload)

        session = Session()
        payload = KaiyunRelayAdapter.poll(
            session,
            "https://api.example.com/v1/videos/task-123",
            {},
            timeout=1,
            interval=0,
            result_timeout=1,
        )

        self.assertEqual(session.calls, 2)
        self.assertEqual(payload["video_url"], "https://assets.example.com/result.mp4")


class DashScopeDeliveryTests(unittest.TestCase):
    def test_official_adapter_submits_once_polls_and_downloads(self) -> None:
        calls: dict[str, object] = {}

        class FakeVideoSynthesis:
            @staticmethod
            def async_call(**kwargs):
                calls["submit"] = kwargs
                return {"status_code": 200, "output": {"task_id": "official-task"}}

            @staticmethod
            def fetch(task_id, api_key=None):
                calls["fetch"] = (task_id, api_key)
                return {
                    "status_code": 200,
                    "output": {
                        "task_status": "SUCCEEDED",
                        "video_url": "https://assets.example.com/official.mp4",
                    },
                }

        fake_dashscope = types.ModuleType("dashscope")
        fake_dashscope.base_http_api_url = "https://old.example.com/api/v1"
        fake_dashscope.VideoSynthesis = FakeVideoSynthesis

        class DownloadResponse:
            ok = True
            status_code = 200
            content = b"official-video"

        model = ModelSpec(
            id="wan-official-videoedit",
            api_model="wan2.7-videoedit",
            aliases=("wan-official",),
            provider="dashscope",
            adapter="dashscope_videoedit",
            input_summary="test",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.mp4"
            image = Path(temp_dir) / "product.png"
            output = Path(temp_dir) / "result.mp4"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            request = GenerationRequest(
                prompt="原始提示词",
                requested_model="wan-official",
                model=model,
                references=[parse_reference(f"video={video}"), parse_reference(f"image={image}")],
                output=output,
                duration=5,
                aspect_ratio="9:16",
                resolution="720p",
            )
            credential = Credential(
                provider="dashscope",
                api_base="https://dashscope.aliyuncs.com/api/v1",
                api_key="official-key",
                api_key_source="DASHSCOPE_API_KEY",
                submit_path="services/aigc/video-generation/video-synthesis",
            )
            adapter = DashScopeVideoEditAdapter()
            with patch.dict(sys.modules, {"dashscope": fake_dashscope}), patch(
                "requests.get", return_value=DownloadResponse()
            ):
                result = adapter.execute(request, credential)

            self.assertEqual(output.read_bytes(), b"official-video")

        self.assertEqual(calls["fetch"], ("official-task", "official-key"))
        submit = calls["submit"]
        self.assertEqual(submit["model"], "wan2.7-videoedit")
        self.assertEqual(submit["prompt"], "原始提示词")
        self.assertFalse(submit["prompt_extend"])
        self.assertEqual(submit["audio_setting"], "origin")
        self.assertEqual(result["provider"], "dashscope")
        self.assertTrue(result["local_downloaded"])


if __name__ == "__main__":
    unittest.main()
