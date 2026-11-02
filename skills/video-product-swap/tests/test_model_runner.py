from __future__ import annotations

import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from model_runner.adapters.fal_generic import FalGenericAdapter
from model_runner.core import GenerationRequest, ModelSpec, VideoGenerationError, parse_reference
from model_runner.credentials import Credential
from model_runner.registry import ModelRegistry
from model_runner.runner import ModelRunner


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ModelRegistry(SKILL_ROOT / "references")

    def test_aliases_resolve_to_specialized_adapters(self) -> None:
        self.assertEqual(self.registry.resolve("wan3").adapter, "fal_wan3")
        self.assertEqual(self.registry.resolve("sync3").adapter, "fal_sync3")
        self.assertEqual(self.registry.resolve("flashvsr").adapter, "fal_flashvsr")
        swap = self.registry.resolve("wan3.0-video")
        self.assertEqual(swap.provider, "fal")
        self.assertEqual(swap.api_model, "alibaba/wan-3.0/reference-to-video")

    def test_unknown_model_uses_compatibility_adapter(self) -> None:
        model = self.registry.resolve("future-model")
        self.assertEqual(model.adapter, "fal_generic")
        self.assertFalse(model.registered)
        self.assertEqual(model.api_model, "future-model")

    def test_default_provider_is_fal(self) -> None:
        self.assertEqual(self.registry.default_provider, "fal")
        self.assertEqual(self.registry.default_model("fal"), "wan3.0-video")

    def test_provider_layer_uses_default_model_when_omitted(self) -> None:
        model = self.registry.resolve_for_provider("fal", None)
        self.assertEqual(model.id, "wan3.0-video")

    def test_provider_layer_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(VideoGenerationError, "未登记服务商"):
            self.registry.resolve_for_provider("unknown-provider", "wan3")


class DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_env = dict(os.environ)
        # Test credentials must not depend on the developer's installed .env.
        env_loader = patch("model_runner.credentials.load_env")
        env_loader.start()
        self.addCleanup(env_loader.stop)
        os.environ.pop("FAL_KEY", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_sync3_maps_video_and_audio_without_prompt(self) -> None:
        args = Namespace(
            prompt=None,
            provider=None,
            model="sync3",
            reference=["video=https://example.com/clip.mp4", "audio=https://example.com/voice.wav"],
            output=str(Path(tempfile.gettempdir()) / "sync3-dry-run-result.mp4"),
            duration=None,
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertEqual(result["adapter"], "fal_sync3")
        self.assertEqual(result["resolved_model"], "fal-ai/sync-lipsync/v3")
        self.assertEqual(result["request_payload"], {
            "video_url": "https://example.com/clip.mp4",
            "audio_url": "https://example.com/voice.wav",
        })

    def test_sync3_requires_exactly_one_video_and_one_audio(self) -> None:
        base = dict(prompt=None, provider=None, model="sync3",
                    output=str(Path(tempfile.gettempdir()) / "sync3-invalid.mp4"),
                    duration=None, dry_run=True)
        with self.assertRaisesRegex(VideoGenerationError, "一个视频和一段音频"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, reference=["video=https://example.com/clip.mp4"]))
        with self.assertRaisesRegex(VideoGenerationError, "不接受图片参考"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, reference=["video=https://example.com/clip.mp4",
                                   "audio=https://example.com/voice.wav",
                                   "image=https://example.com/p.png"]))

    def test_sync3_rejects_local_video_over_ten_minutes(self) -> None:
        import media_inspection
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "long.mp4"
            audio = Path(temp_dir) / "voice.wav"
            video.write_bytes(b"test")
            audio.write_bytes(b"test")
            args = Namespace(
                prompt=None, provider=None, model="sync3",
                reference=[str(video), str(audio)],
                output=str(Path(temp_dir) / "result.mp4"),
                duration=None, dry_run=True,
            )
            fake = media_inspection.VideoInfo(601.0, 1920, 1080, "16:9")
            with patch("media_inspection.probe_video", return_value=fake), \
                 patch("media_inspection.probe_audio_duration", return_value=60.0):
                with self.assertRaisesRegex(VideoGenerationError, "最长 600 秒"):
                    ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)

    def test_flashvsr_maps_video_with_fixed_upscale_options(self) -> None:
        args = Namespace(
            prompt=None,
            provider=None,
            model="flashvsr",
            reference=["video=https://example.com/clip.mp4"],
            output=str(Path(tempfile.gettempdir()) / "flashvsr-dry-run-result.mp4"),
            duration=None,
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertEqual(result["adapter"], "fal_flashvsr")
        self.assertEqual(result["resolved_model"], "fal-ai/flashvsr/upscale/video")
        self.assertEqual(result["request_payload"], {
            "video_url": "https://example.com/clip.mp4",
            "upscale_factor": 2,
            "acceleration": "regular",
            "color_fix": True,
            "preserve_audio": True,
        })

    def test_flashvsr_accepts_user_options(self) -> None:
        args = Namespace(
            prompt=None,
            provider=None,
            model="flashvsr",
            reference=["video=https://example.com/clip.mp4"],
            output=str(Path(tempfile.gettempdir()) / "flashvsr-options.mp4"),
            duration=None,
            dry_run=True,
            options={"upscale_factor": 4, "acceleration": "full",
                     "color_fix": False, "preserve_audio": False},
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertEqual(result["request_payload"], {
            "video_url": "https://example.com/clip.mp4",
            "upscale_factor": 4,
            "acceleration": "full",
            "color_fix": False,
            "preserve_audio": False,
        })

    def test_flashvsr_rejects_invalid_options(self) -> None:
        base = dict(prompt=None, provider=None, model="flashvsr",
                    reference=["video=https://example.com/clip.mp4"],
                    output=str(Path(tempfile.gettempdir()) / "flashvsr-bad-options.mp4"),
                    duration=None, dry_run=True)
        with self.assertRaisesRegex(VideoGenerationError, "1-4"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, options={"upscale_factor": 8}))
        with self.assertRaisesRegex(VideoGenerationError, "不支持的参数"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, options={"unknown_field": 1}))
        with self.assertRaisesRegex(VideoGenerationError, "regular / high / full"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, options={"acceleration": "turbo"}))

    def test_flashvsr_rejects_audio_and_second_video(self) -> None:
        base = dict(prompt=None, provider=None, model="flashvsr",
                    output=str(Path(tempfile.gettempdir()) / "flashvsr-invalid.mp4"),
                    duration=None, dry_run=True)
        with self.assertRaisesRegex(VideoGenerationError, "只接受视频"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, reference=["video=https://example.com/clip.mp4",
                                   "audio=https://example.com/voice.wav"]))
        with self.assertRaisesRegex(VideoGenerationError, "只接受一个视频"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(Namespace(
                **base, reference=["video=https://example.com/a.mp4",
                                   "video=https://example.com/b.mp4"]))

    def test_wan3_defaults_duration_and_maps_reference_fields(self) -> None:
        args = Namespace(
            prompt="原始提示词",
            provider=None,
            model="wan3",
            reference=["image=https://example.com/ref", "video=https://example.com/source"],
            output=str(Path(tempfile.gettempdir()) / "wan3-dry-run-result.mp4"),
            duration=None,
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertEqual(result["adapter"], "fal_wan3")
        payload = result["request_payload"]
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["reference_image_urls"], ["https://example.com/ref"])
        self.assertEqual(payload["reference_video_urls"], ["https://example.com/source"])
        self.assertFalse(payload["enable_prompt_expansion"])

    def test_output_specifications_reach_payload(self) -> None:
        args = Namespace(
            prompt="原始提示词",
            provider=None,
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

    def test_fal_key_is_reported_as_credential_source(self) -> None:
        os.environ["FAL_KEY"] = "fal-test-secret"
        args = Namespace(
            prompt="原始提示词",
            provider=None,
            model="wan3",
            reference=["image=https://example.com/ref"],
            output=str(Path(tempfile.gettempdir()) / "wan3-fal-key-result.mp4"),
            duration=5,
            dry_run=True,
        )
        result = ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)
        self.assertTrue(result["credential_configured"])
        self.assertEqual(result["credential_source"], "FAL_KEY")

    def test_t2v_rejects_references(self) -> None:
        args = Namespace(
            prompt="原始提示词",
            provider=None,
            model="wan3.0-t2v",
            reference=["image=https://example.com/ref"],
            output=str(Path(tempfile.gettempdir()) / "wan3-t2v-dry-run-result.mp4"),
            duration=5,
            dry_run=True,
        )
        with self.assertRaisesRegex(VideoGenerationError, "纯文本模型"):
            ModelRunner(SKILL_ROOT, credential_source="host-app").run(args)


class ReferenceTests(unittest.TestCase):
    def test_non_https_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(VideoGenerationError, "公网 HTTPS"):
            parse_reference("image=http://example.com/ref.png")


class FalDeliveryTests(unittest.TestCase):
    @staticmethod
    def request(output: Path) -> GenerationRequest:
        model = ModelSpec(
            id="test-model",
            api_model="fal-ai/test-model",
            aliases=(),
            provider="fal",
            adapter="fal_generic",
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
            provider="fal",
            api_base="https://queue.fal.run",
            api_key="fal-test-key",
            api_key_source="FAL_KEY",
            submit_path="",
        )

    class FakeResponse:
        def __init__(self, payload=None, content=b"", ok=True, status_code=200, text=""):
            self._payload = payload
            self.content = content
            self.ok = ok
            self.status_code = status_code
            self.text = text

        def json(self):
            return self._payload

    class FakeSession:
        """Serves fal queue endpoints in order: submit, status, result, download."""

        def __init__(self, *, result_payload=None, download_content=b"video-bytes", download_ok=True):
            self.result_payload = result_payload or {
                "video": {"url": "https://v3b.fal.media/files/result.mp4"}
            }
            self.download_content = download_content
            self.download_ok = download_ok
            self.posts = []
            self.gets = []

        def post(self, url, **kwargs):
            self.posts.append(url)
            return FalDeliveryTests.FakeResponse({"request_id": "req-123"})

        def head(self, url, **kwargs):
            from unittest.mock import Mock
            return Mock(status_code=405)

        def put(self, url, **kwargs):
            return FalDeliveryTests.FakeResponse()

        def get(self, url, **kwargs):
            self.gets.append(url)
            if url.endswith("/status"):
                return FalDeliveryTests.FakeResponse({"status": "COMPLETED"})
            if "/requests/" in url:
                return FalDeliveryTests.FakeResponse(self.result_payload)
            return FalDeliveryTests.FakeResponse(
                content=self.download_content, ok=self.download_ok,
                status_code=200 if self.download_ok else 500,
            )

    def test_submit_poll_result_download(self) -> None:
        session = self.FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.mp4"
            with patch("requests.Session", return_value=session):
                result = FalGenericAdapter().execute(self.request(output), self.credential())
            self.assertEqual(output.read_bytes(), b"video-bytes")

        self.assertTrue(result["local_downloaded"])
        self.assertEqual(result["delivery"], "local_file")
        self.assertEqual(result["task_id"], "req-123")
        self.assertEqual(result["video_url"], "https://v3b.fal.media/files/result.mp4")
        self.assertEqual(session.posts, ["https://queue.fal.run/fal-ai/test-model"])
        self.assertIn(
            "https://queue.fal.run/fal-ai/test-model/requests/req-123/status", session.gets
        )
        self.assertIn("https://queue.fal.run/fal-ai/test-model/requests/req-123", session.gets)

    def test_download_failure_returns_url_without_resubmitting(self) -> None:
        class FailingSession(FalDeliveryTests.FakeSession):
            def get(self, url, **kwargs):
                if "fal.media" in url:
                    raise ConnectionError("TLS connection closed")
                return super().get(url, **kwargs)

        session = FailingSession()
        with patch("requests.Session", return_value=session):
            result = FalGenericAdapter().execute(
                self.request(Path(tempfile.gettempdir()) / "unused-result.mp4"),
                self.credential(),
            )
        self.assertEqual(len(session.posts), 1)
        self.assertTrue(result["success"])
        self.assertFalse(result["local_downloaded"])
        self.assertEqual(result["delivery"], "url_fallback")
        self.assertIn("下载视频失败", result["download_error"])


if __name__ == "__main__":
    unittest.main()
