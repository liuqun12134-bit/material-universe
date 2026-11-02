"""Offline regressions for paid-task recovery and retired feature boundaries."""
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_runner.core import GenerationRequest, ModelSpec, VideoGenerationError
from model_runner.runner import ModelRunner
from model_runner.task_state import TaskRecord


class Response:
    def __init__(self, payload=None, status=200, content=b"recovered-video"):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.text = "temporary error" if status >= 400 else ""
        self.content = content

    def json(self):
        return self.payload

    def close(self):
        pass


class RecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = ModelRunner(ROOT, values={"FAL_KEY": "fake-fal-key"})

    def record(self, name, provider="fal", adapter="fal_wan3", base="https://queue.fal.run", **fields):
        model = ModelSpec("old-model", "owner/app/path", (), provider, adapter, "test")
        output = self.root / (name + ".mp4")
        record = TaskRecord(TaskRecord.path_for(output))
        record.start(GenerationRequest("", "old-model", model, [], output, 5), base)
        record.update(task_id="existing-paid-task", status="interrupted", **fields)
        return record

    def test_result_http_errors_remain_resumable_without_a_second_submission(self):
        for status in (401, 403, 404, 408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                record = self.record(str(status))
                session = Mock()
                session.get.side_effect = [Response({"status": "COMPLETED"})] + [Response(status=status) for _ in range(3)]
                with patch("requests.Session", return_value=session), patch("model_runner.adapters.fal_base.time.sleep"):
                    with self.assertRaisesRegex(VideoGenerationError, f"HTTP {status}"):
                        self.runner.resume(record.path)
                    record.read()
                    self.assertEqual(record.data["status"], "interrupted")
                    self.assertEqual(record.data["task_id"], "existing-paid-task")
                    session.get.side_effect = [Response({"status": "COMPLETED"}),
                                              Response({"video": {"url": "https://media.example/result.mp4"}}), Response()]
                    result = self.runner.resume(record.path)
                self.assertTrue(result["local_downloaded"])
                self.assertEqual(Path(result["output"]).read_bytes(), b"recovered-video")
                session.post.assert_not_called()
                session.put.assert_not_called()

    def test_pre_fix_misclassified_http_failure_can_be_recovered(self):
        record = self.record("misclassified")
        record.update(status="failed", error="视频任务失败（HTTP 503）：unavailable")
        session = Mock()
        session.get.side_effect = [Response({"status": "COMPLETED"}),
                                  Response({"video": {"url": "https://media.example/out.mp4"}}), Response()]
        with patch("requests.Session", return_value=session):
            result = self.runner.resume(record.path)
        self.assertTrue(result["local_downloaded"])
        session.post.assert_not_called()

    def test_explicit_terminal_status_stays_failed(self):
        record = self.record("terminal")
        session = Mock()
        session.get.side_effect = [Response({"status": "COMPLETED"}), Response({"status": "failed", "error": "rejected"})]
        with patch("requests.Session", return_value=session):
            with self.assertRaises(VideoGenerationError):
                self.runner.resume(record.path)
            with self.assertRaisesRegex(VideoGenerationError, "明确失败"):
                self.runner.resume(record.path)
        self.assertEqual(session.get.call_count, 2)
        record.read()
        self.assertEqual(record.data["failure_kind"], "terminal_task")

    def test_old_public_video_url_downloads_without_any_key(self):
        for provider, adapter, base in (("kaiyuncode", "wan3", "https://kaiyuncode.com"),
                                        ("dashscope", "dashscope_videoedit", "https://dashscope.aliyuncs.com/api/v1")):
            with self.subTest(provider=provider):
                record = self.record(provider, provider, adapter, base, video_url="https://media.example/old.mp4")
                session = Mock()
                session.get.return_value = Response()
                with patch("requests.Session", return_value=session):
                    result = ModelRunner(ROOT, values={}).resume(record.path)
                self.assertTrue(result["local_downloaded"])
                session.get.assert_called_once_with("https://media.example/old.mp4", headers={}, timeout=300)
                session.post.assert_not_called()

    def test_old_providers_query_original_service_with_only_their_own_key(self):
        cases = (("kaiyuncode", "wan3", "https://kaiyuncode.com", "KAIYUNCODE_API_KEY",
                  {"status": "completed", "video_url": "https://media.example/old.mp4"}, "/v1/videos/existing-paid-task"),
                 ("dashscope", "dashscope_videoedit", "https://dashscope.aliyuncs.com/api/v1", "DASHSCOPE_API_KEY",
                  {"output": {"task_status": "SUCCEEDED", "video_url": "https://media.example/old.mp4"}}, "/tasks/existing-paid-task"))
        for provider, adapter, base, key, payload, path in cases:
            with self.subTest(provider=provider):
                record = self.record(provider, provider, adapter, base)
                session = Mock()
                session.get.side_effect = [Response(payload), Response()]
                with patch("requests.Session", return_value=session):
                    result = ModelRunner(ROOT, values={key: "old-provider-key", "FAL_KEY": "unrelated-key"}).resume(record.path)
                self.assertTrue(result["local_downloaded"])
                first = session.get.call_args_list[0]
                self.assertEqual(first.args[0], base + path)
                self.assertEqual(first.kwargs["headers"], {"Authorization": "Bearer old-provider-key"})
                self.assertEqual(session.get.call_args.kwargs["headers"], {})
                session.post.assert_not_called()

    def test_old_task_does_not_borrow_fal_credentials(self):
        record = self.record("no-key", "kaiyuncode", "wan3", "https://kaiyuncode.com")
        with patch("requests.Session") as network:
            with self.assertRaisesRegex(VideoGenerationError, "KAIYUNCODE_API_KEY"):
                self.runner.resume(record.path)
        network.assert_not_called()

    def test_foreign_old_poll_url_is_rejected_without_sending_key(self):
        record = self.record("foreign", "kaiyuncode", "wan3", "https://kaiyuncode.com", poll_url="https://foreign.example/task")
        session = Mock()
        with patch("requests.Session", return_value=session):
            with self.assertRaisesRegex(VideoGenerationError, "同源"):
                ModelRunner(ROOT, values={"KAIYUNCODE_API_KEY": "old-key"}).resume(record.path)
        session.get.assert_not_called()

    def test_removed_fal_omni_task_can_finish_without_restoring_generation(self):
        record = self.record("omni", adapter="fal_omniflash")
        session = Mock()
        session.get.side_effect = [Response({"status": "COMPLETED"}),
                                  Response({"video": {"url": "https://media.example/out.mp4"}}), Response()]
        with patch("requests.Session", return_value=session):
            self.assertTrue(self.runner.resume(record.path)["local_downloaded"])
        session.post.assert_not_called()

    def test_removed_omni_names_never_fall_back_to_generic_submission(self):
        self.assertFalse(any("omni" in m["model"] and m["provider"] == "fal" for m in self.runner.list_models()))
        for name in ("omniflash", "omni-flash", "fal_omniflash", "google/gemini-omni-flash/v1.1/edit"):
            with self.subTest(name=name), patch("requests.Session") as network:
                args = Namespace(model=name, prompt="test", reference=[], output=str(self.root / "new.mp4"), duration=None, dry_run=False)
                with self.assertRaisesRegex(VideoGenerationError, "已移除"):
                    self.runner.run(args)
                network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
