"""Offline official protocol, credential isolation, upload and recovery contracts."""
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_runner.runner import ModelRunner
from model_runner.core import VideoGenerationError
from model_runner.task_state import TaskRecord


def response(data=None, status=200, content=b"video-bytes"):
    result = Mock(ok=status < 300, status_code=status, content=content, text="test error")
    result.json.return_value = data or {}
    return result


class OfficialWan3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.output = self.folder / "result.mp4"
        self.values = {"DASHSCOPE_API_KEY": "official-test-key", "FAL_KEY": "fal-other-key"}
        self.runner = ModelRunner(ROOT, credential_source="host-app", values=self.values)
        self.args = Namespace(provider="dashscope", model="dashscope-wan3.0-video", prompt=" 原样提示词\n第二行 ",
                              reference=["image=https://example.com/ref.png"], output=str(self.output),
                              duration=5, aspect_ratio="9:16", resolution="720p", dry_run=False,
                              options={"enable_prompt_expansion": False, "enable_thinking": False})

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_uses_official_protocol_with_no_fal_fields_or_key(self):
        self.args.dry_run = True
        plan = self.runner.run(self.args)
        self.assertEqual(plan["provider"], "dashscope")
        payload = plan["request_payload"]
        self.assertEqual(payload["input"]["prompt"], self.args.prompt)
        self.assertEqual(payload["input"]["media"], [{"type": "reference_image", "url": "https://example.com/ref.png"}])
        self.assertEqual(payload["parameters"], {"duration": 5, "resolution": "720P", "ratio": "9:16", "prompt_extend": False})
        self.assertIn("dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis", plan["submit_endpoint"])
        self.assertNotIn("official-test-key", json.dumps(plan))
        self.args.options["enable_thinking"] = True
        with self.assertRaisesRegex(VideoGenerationError, "思考模式"):
            self.runner.run(self.args)

    def test_missing_own_key_and_foreign_base_fail_before_network_or_record(self):
        for values in ({"FAL_KEY": "fal-other-key"}, {**self.values, "DASHSCOPE_API_BASE": "https://evil.example.com/api/v1"}):
            with self.subTest(values=list(values)):
                runner = ModelRunner(ROOT, credential_source="host-app", values=values)
                with patch("requests.Session") as network, self.assertRaises(VideoGenerationError):
                    runner.run(self.args)
                network.assert_not_called()
                self.assertFalse(TaskRecord.path_for(self.output).exists())

    def test_submit_poll_download_and_cross_host_key_isolation(self):
        session = Mock()
        session.post.return_value = response({"output": {"task_id": "official-1"}})
        session.get.side_effect = [response({"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.example.com/video.mp4"}}), response()]
        with patch("requests.Session", return_value=session):
            result = self.runner.run(self.args)
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(self.output.read_bytes(), b"video-bytes")
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer official-test-key")
        self.assertEqual(session.post.call_args.kwargs["headers"]["X-DashScope-Async"], "enable")
        self.assertEqual(session.get.call_args.kwargs["headers"], {})
        self.assertEqual(json.loads(TaskRecord.path_for(self.output).read_text(encoding="utf-8"))["task_id"], "official-1")

    def test_unknown_submit_never_auto_reposts(self):
        session = Mock()
        session.post.side_effect = OSError("lost response")
        with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
            self.runner.run(self.args)
        record = TaskRecord.path_for(self.output)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "submission_unknown")
        with self.assertRaises(VideoGenerationError):
            self.runner.resume(record)
        session.post.assert_called_once()

    def test_resume_task_after_unknown_status_is_get_only(self):
        session = Mock()
        session.post.return_value = response({"output": {"task_id": "official-existing"}})
        session.get.return_value = response({"output": {"task_status": "UNKNOWN"}})
        with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
            self.runner.run(self.args)
        record = TaskRecord.path_for(self.output)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "interrupted")
        session.get.side_effect = [response({"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.example.com/result.mp4"}}), response()]
        with patch("requests.Session", return_value=session):
            result = self.runner.resume(record)
        self.assertTrue(result["resumed_existing_task"])
        session.post.assert_called_once()

    def test_local_upload_oss_does_not_receive_bearer_and_generation_resolves_oss(self):
        image = self.folder / "reference.png"
        Image.new("RGB", (240, 240), "red").save(image)
        self.args.reference = [f"image={image}"]
        session = Mock()
        policy = {"upload_host": "https://dashscope-upload.oss-cn-beijing.aliyuncs.com", "upload_dir": "prefix", "policy": "policy",
                  "signature": "signature", "oss_access_key_id": "temporary-id", "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true"}
        session.get.side_effect = [response({"data": policy}), response({"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.example.com/video.mp4"}}), response()]
        session.post.side_effect = [response(), response({"output": {"task_id": "local-task"}})]
        with patch("requests.Session", return_value=session):
            self.runner.run(self.args)
        upload, generation = session.post.call_args_list
        self.assertNotIn("headers", upload.kwargs)
        self.assertIn("file", upload.kwargs["files"])
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["model"], "wan3.0-video")
        self.assertTrue(generation.kwargs["json"]["input"]["media"][0]["url"].startswith("oss://prefix/"))
        self.assertEqual(generation.kwargs["headers"]["X-DashScope-OssResourceResolve"], "enable")

    def test_upload_failure_is_not_submitted_and_no_generation_post(self):
        image = self.folder / "reference.png"
        Image.new("RGB", (240, 240)).save(image)
        self.args.reference = [f"image={image}"]
        session = Mock()
        session.get.return_value = response({}, status=403)
        with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
            self.runner.run(self.args)
        self.assertEqual(json.loads(TaskRecord.path_for(self.output).read_text(encoding="utf-8"))["status"], "not_submitted")
        session.post.assert_not_called()

    def test_prime_upload_policy_uses_prime_model_and_preserves_remote_reference_order(self):
        self.args.dry_run = True
        self.args.model = "dashscope-wan3.0-video-prime"
        self.args.reference = ["video=https://example.com/video.mp4", "image=https://example.com/first.png", "audio=https://example.com/audio.wav"]
        payload = self.runner.run(self.args)["request_payload"]
        self.assertEqual(payload["model"], "wan3.0-video-prime")
        self.assertEqual([item["type"] for item in payload["input"]["media"]], ["reference_video", "reference_image", "reference_audio"])

    def test_local_constraints_stop_before_upload_and_combined_duration_limit(self):
        video = self.folder / "reference.mp4"
        video.write_bytes(b"local-video")
        self.args.reference = [f"video={video}"]
        self.args.duration = 20
        info = Mock(duration=11, width=1080, height=1920)
        with patch("media_inspection.probe_video", return_value=info), patch("media_inspection.probe_frame_rate", return_value=24), patch("requests.Session") as network:
            with self.assertRaisesRegex(VideoGenerationError, "加输出时长不能超过"):
                self.runner.run(self.args)
            network.assert_not_called()
        for width, height in ((240, 2160), (2160, 240)):
            with self.subTest(size=(width, height)), patch("media_inspection.probe_video", return_value=Mock(duration=5, width=width, height=height)), patch("media_inspection.probe_frame_rate", return_value=24), patch("requests.Session") as network:
                with self.assertRaisesRegex(VideoGenerationError, "宽高比"):
                    self.runner.run(self.args)
                network.assert_not_called()
        image = self.folder / "transparent.png"
        Image.new("RGBA", (240, 240)).save(image)
        self.args.reference = [f"image={image}"]
        self.args.duration = 5
        with patch("requests.Session") as network, self.assertRaisesRegex(VideoGenerationError, "透明"):
            self.runner.run(self.args)
        network.assert_not_called()

    def test_terminal_failure_does_not_resume_or_resubmit(self):
        session = Mock()
        session.post.return_value = response({"output": {"task_id": "failed-task"}})
        session.get.return_value = response({"output": {"task_status": "FAILED", "message": "invalid prompt"}})
        with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
            self.runner.run(self.args)
        record = TaskRecord.path_for(self.output)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "failed")
        with self.assertRaisesRegex(VideoGenerationError, "明确失败"):
            self.runner.resume(record)
        session.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
