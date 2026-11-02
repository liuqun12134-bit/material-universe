"""Upload disconnect recovery must not truncate files or retry paid generation."""
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_runner.adapters.fal_base import FalQueueAdapter
from model_runner.core import VideoGenerationError
from model_runner.credentials import Credential
from model_runner.runner import ModelRunner
from model_runner.task_state import TaskRecord


def response(code=200, payload=None):
    result = Mock(status_code=code, ok=code < 400)
    result.json.return_value = payload
    return result


class UploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "source.mp4"
        self.content = bytes(range(256)) * 100
        self.path.write_bytes(self.content)
        self.credential = Credential("fal", "https://queue.fal.run", "fake-key", "test", "")
        self.session = Mock()
        self.session.post.return_value = response(payload={
            "upload_url": "https://storage.example/same-object?signature=private",
            "file_url": "https://storage.example/same-object",
        })
        self.sleep = patch("model_runner.adapters.fal_base.time.sleep").start()
        self.addCleanup(patch.stopall)

    def upload(self):
        return FalQueueAdapter().upload_file(self.session, self.credential, self.path, "video/mp4")

    def test_partial_reset_rewinds_and_reuses_same_object(self):
        bodies = []
        def put(url, *, data, **kwargs):
            if not bodies:
                bodies.append(data.read(17))
                raise requests.ConnectionError(ConnectionResetError(10054, "reset"))
            bodies.append(data.read())
            return response()
        self.session.put.side_effect = put
        self.assertEqual(self.upload(), "https://storage.example/same-object")
        self.assertEqual(bodies, [self.content[:17], self.content])
        self.session.post.assert_called_once()
        self.assertEqual(len({call.args[0] for call in self.session.put.call_args_list}), 1)

    def test_ssl_exhaustion_is_bounded_and_hides_signed_url(self):
        self.session.put.side_effect = requests.exceptions.SSLError("signature=private")
        with self.assertRaisesRegex(VideoGenerationError, "已尝试 3 次") as error:
            self.upload()
        self.assertNotIn("signature", str(error.exception))
        self.assertEqual(self.session.put.call_count, 3)

    def test_transient_http_is_retried_but_403_is_not(self):
        self.session.put.side_effect = [response(503), response()]
        self.upload()
        self.assertEqual(self.session.put.call_count, 2)
        self.session.put.reset_mock()
        self.session.put.side_effect = [response(403)]
        with self.assertRaisesRegex(VideoGenerationError, "HTTP 403"):
            self.upload()
        self.session.put.assert_called_once()

    def test_allocation_disconnect_retries_without_repeating_successful_put(self):
        ok = self.session.post.return_value
        self.session.post.side_effect = [requests.Timeout(), ok]
        self.session.put.return_value = response()
        self.upload()
        self.assertEqual(self.session.post.call_count, 2)
        self.session.put.assert_called_once()

    def test_failed_upload_never_reaches_generation_endpoint(self):
        image = self.path.with_suffix(".png")
        image.write_bytes(b"image")
        output = self.path.parent / "result.mp4"
        args = Namespace(prompt="unchanged", model="wan3", provider="fal",
                         reference=[f"video={self.path}", f"image={image}"],
                         output=str(output), duration=5, aspect_ratio="9:16",
                         resolution="480p", dry_run=False)
        runner = ModelRunner(ROOT, credential_source="host-app", values={"FAL_KEY": "fake-key"})
        self.session.put.side_effect = requests.ConnectionError("reset")
        with patch("requests.Session", return_value=self.session):
            with self.assertRaises(VideoGenerationError):
                runner.run(args)
        self.session.post.assert_called_once()
        self.assertIn("storage/upload/initiate", self.session.post.call_args.args[0])
        state = json.loads(TaskRecord.path_for(output).read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "not_submitted")
        self.assertNotIn("task_id", state)


if __name__ == "__main__":
    unittest.main()
