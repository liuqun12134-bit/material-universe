"""Provider selection and quote contracts, with no network or paid execution."""
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canvas_backend import CanvasService
from canvas_catalog import runner_options
from desktop_tasks import DesktopStore, TaskManager
from model_runner.registry import ModelRegistry


class CanvasProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DesktopStore(Path(self.temp.name) / "data")
        self.tasks = TaskManager(self.store, execute=lambda *_: {"local_downloaded": False})
        self.service = CanvasService(self.store, self.tasks, self.temp.name, lambda: {}, models=ModelRegistry(ROOT / "references").list_models())

    def tearDown(self):
        self.tasks.stop()
        self.tasks.wait_idle(3)
        self.store.db.close()
        self.temp.cleanup()

    def draft(self, prime=False):
        return {"familyId": "wan3-dashscope", "nodeType": "video", "model": "dashscope-wan3.0-video" + ("-prime" if prime else ""), "provider": "dashscope", "prompt": "  exact prompt\nsecond line  ", "referenceAssetIds": [], "parameters": {"duration": 5, "resolution": "720p", "enable_prompt_expansion": False}}

    def saved_submission(self, draft):
        canvas = self.service.canvases.create("Provider test")
        node_id, request_id = uuid.uuid4().hex, uuid.uuid4().hex
        node = {"id": node_id, "type": "task", "position": {"x": 0, "y": 0}, "data": {"kind": "general", "config": draft}}
        contents = {"nodes": [node], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}, "revision": 0}
        self.service.canvases.save(canvas["id"], contents)
        node["data"]["requestId"] = request_id
        self.service.canvases.save(canvas["id"], {**contents, "revision": 1})
        return {"canvasId": canvas["id"], "nodeId": node_id, "requestId": request_id}

    def test_official_model_provider_and_prime_survive_local_config(self):
        with patch("requests.Session", side_effect=AssertionError("local configuration must stay offline")):
            draft = self.draft(prime=True)
            config = self.service._config("general", draft, uuid.uuid4().hex)
            self.assertEqual(config["provider"], "dashscope")
            self.assertEqual(config["model"], "dashscope-wan3.0-video-prime")
            self.assertEqual(config["prompt"], draft["prompt"])
            self.assertNotIn("enable_thinking", config["parameters"])
            self.assertEqual(runner_options(config["model"], config["parameters"]), {"enable_prompt_expansion": False})
            with self.assertRaises(RuntimeError):
                self.service._config("general", {**draft, "provider": "fal"}, uuid.uuid4().hex)
            with self.assertRaises(ValueError):
                self.service._config("general", {**draft, "model": "wan3.0-video"}, uuid.uuid4().hex)
            with self.assertRaises(ValueError):
                self.service._config("general", {**draft, "parameters": {"enable_thinking": True}}, uuid.uuid4().hex)

    def test_beijing_quotes_are_cny_and_other_region_is_unknown(self):
        standard = self.service.estimate({"config": self.draft()})
        prime = self.service.estimate({"config": self.draft(True)})
        self.assertEqual(standard["currency"], "CNY")
        self.assertEqual(standard["amount"], 3)
        self.assertEqual(prime["amount"], 4.5)
        self.assertEqual(standard["durationBasis"], "output_plus_reference_video")
        self.service.credential_provider = lambda: {"DASHSCOPE_API_BASE": "https://dashscope-intl.aliyuncs.com/api/v1"}
        quote = self.service.estimate({"config": self.draft()})
        self.assertEqual(quote["status"], "unknown")
        self.assertIn("地域", quote["reason"])

    def test_official_missing_key_or_wrong_host_never_queues(self):
        payload = self.saved_submission(self.draft())
        with patch.object(self.tasks, "submit", side_effect=AssertionError("must not enqueue")):
            with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
                self.service.submit(payload)
            self.service.credential_provider = lambda: {"DASHSCOPE_API_KEY": "fake", "DASHSCOPE_API_BASE": "https://queue.fal.run"}
            with self.assertRaisesRegex(RuntimeError, "阿里官方服务地址"):
                self.service.submit(payload)

    def test_unverified_region_can_submit_with_frozen_provider_snapshot(self):
        self.service.credential_provider = lambda: {"DASHSCOPE_API_KEY": "fake", "DASHSCOPE_API_BASE": "https://dashscope-intl.aliyuncs.com/api/v1"}
        payload = self.saved_submission(self.draft(prime=True))
        with patch("requests.Session", side_effect=AssertionError("mock task must stay offline")):
            job = self.service.submit(payload)
            self.assertEqual(job["config"]["provider"], "dashscope")
            self.assertEqual(job["config"]["model"], "dashscope-wan3.0-video-prime")
            self.assertEqual(job["config"]["cost_estimate"]["status"], "unknown")
            self.service.credential_provider = lambda: {"FAL_KEY": "new-fal-key"}
            self.assertEqual(self.service.submit(payload)["id"], job["id"])
            self.assertEqual(self.store.job(job["id"])["config"]["canvas_draft"]["provider"], "dashscope")

if __name__ == "__main__":
    unittest.main()
