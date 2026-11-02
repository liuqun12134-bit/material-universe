"""Offline contracts for generic canvas creation and single-image recovery."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from canvas_backend import CanvasService
from desktop_tasks import DesktopStore, TaskManager, execute_job, input_paths
from model_runner.adapters.fal_flux_schnell import FalFluxSchnellAdapter
from model_runner.adapters.fal_base import FalQueueAdapter
from model_runner.adapters.fal_wan3 import FalWan3T2VAdapter
from model_runner.core import GenerationRequest
from model_runner.registry import ModelRegistry
from model_runner.runner import ModelRunner
from model_runner.task_state import TaskRecord


class GeneralCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg"):
            with tempfile.TemporaryDirectory() as directory:
                video = Path(directory) / "four_seconds.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=25:d=4", "-pix_fmt", "yuv420p", str(video)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                cls.video_bytes = video.read_bytes()
        else:
            cls.video_bytes = b"video"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "data")
        self.tasks = TaskManager(self.store, execute=lambda *_: {"local_downloaded": False})
        models = ModelRegistry(ROOT / "references").list_models()
        self.service = CanvasService(self.store, self.tasks, self.root, lambda: {}, models=models)
        self.image_file = self.root / "reference.png"
        Image.new("RGB", (8, 8), "blue").save(self.image_file)
        self.image = self.store.import_asset(self.image_file)
        video = self.root / "reference.mp4"
        video.write_bytes(self.video_bytes)
        self.video = self.store.import_asset(video)

    def tearDown(self):
        self.tasks.stop()
        self.tasks.wait_idle(3)
        self.store.db.close()
        self.temp.cleanup()

    def test_catalog_validation_keeps_exact_prompt_and_reference_order(self):
        self.assertEqual([m["id"] for m in self.service.general_models],
                         ["wan3.0-t2v", "wan3.0-video", "dashscope-wan3.0-video", "dashscope-wan3.0-video-prime",
                          "kaiyuncode-wan3.0-video", "kaiyuncode-wan3.0-video-prime", "kaiyuncode-seedance-2.5-tj",
                          "kaiyuncode-omni", "kaiyuncode-gemini-nano-banana-2.1"])
        prompt = "  原文第一行\n第二行  "
        draft = {"model": "wan3.0-video", "prompt": prompt,
                 "referenceAssetIds": [self.video["id"], self.image["id"]],
                 "parameters": {"duration": 7, "aspect_ratio": "16:9", "resolution": "720p"}}
        config = self.service._config("general", draft, uuid.uuid4().hex)
        self.assertEqual(config["prompt"], prompt)
        self.assertEqual(config["references"], [self.video["path"], self.image["path"]])
        self.assertTrue(config["output"].endswith(".mp4"))
        with self.assertRaises(ValueError):
            self.service._config("general", {**draft, "parameters": {"mystery": 1}}, uuid.uuid4().hex)
        video_only = self.service._config("general", {**draft, "referenceAssetIds": [self.video["id"]]}, uuid.uuid4().hex)
        self.assertEqual(video_only["model"], "wan3.0-video")
        with self.assertRaises(ValueError):
            self.service._config("general", {**draft, "model": "arbitrary/provider"}, uuid.uuid4().hex)
        with self.assertRaises(ValueError):
            self.service._config("general", {**draft, "model": "wan3.0-t2v"}, uuid.uuid4().hex)

    def test_image_config_and_result_media(self):
        with self.assertRaises(ValueError):
            self.service._config("general", {"model": "flux-schnell", "prompt": "A blue cup",
                "referenceAssetIds": [], "parameters": {"image_size": "square_hd"}}, uuid.uuid4().hex)
        identity = uuid.uuid4().hex
        output = self.root / "old_flux_result.png"
        output.parent.mkdir(exist_ok=True)
        Image.new("RGB", (16, 16), "green").save(output)
        self.store._put("jobs", {"id": identity, "kind": "general", "status": "success",
                                 "created": 0, "result": {"output": str(output), "output_kind": "image"}})
        self.assertTrue(self.service.media_path(f"/media/job/{identity}").samefile(output))
        self.assertTrue(self.service.media_path(f"/media/job-thumb/{identity}").is_file())
        self.assertEqual(self.service.result_asset(identity)["kind"], "image")

    def test_execution_bypasses_product_analysis_and_keeps_reference_order(self):
        config = self.service._config("general", {"model": "wan3.0-video", "prompt": "exact\nwords",
            "referenceAssetIds": [self.video["id"], self.image["id"]], "parameters": {}}, uuid.uuid4().hex)
        captured = []
        def run(args):
            captured.append(args)
            return {"output": config["output"], "local_downloaded": False}
        with patch("model_runner.runner.ModelRunner") as runner, patch("product_swap.ProductSwapWorkflow") as swap:
            runner.return_value.run.side_effect = run
            execute_job({"id": uuid.uuid4().hex, "kind": "general", "config": config}, {}, lambda **_: None, self.store)
            swap.assert_not_called()
        self.assertEqual(captured[0].prompt, "exact\nwords")
        self.assertEqual(captured[0].reference, config["references"])
        self.assertEqual(captured[0].model, "wan3.0-video")
        with self.assertRaises(ValueError):
            execute_job({"kind": "unknown", "config": {"output": str(self.root / "x.mp4")}},
                        {}, lambda **_: None, self.store)

    def test_flux_payload_and_get_only_recovery(self):
        adapter = FalFluxSchnellAdapter()
        spec = ModelRegistry(ROOT / "references").resolve("flux-schnell")
        output = self.root / "result.png"
        request = GenerationRequest("  exact\nimage  ", spec.id, spec, [], output, None,
                                    options={"image_size": "square_hd"})
        adapter.validate(request)
        self.assertEqual(adapter.remote_payload(request), {"prompt": "  exact\nimage  ",
            "image_size": "square_hd", "num_images": 1, "output_format": "png"})
        self.assertEqual(adapter.result_url({"images": [{"url": "https://example.test/one.png"}]}),
                         "https://example.test/one.png")
        record = TaskRecord(TaskRecord.path_for(output))
        record.start(request, "https://queue.fal.run")
        record.update(task_id="existing-task", status="submitted")
        request.task_record = record
        data = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(data, format="PNG")
        class Response:
            ok = True
            content = data.getvalue()
        class Session:
            def get(self, url, **kwargs):
                self.url = url
                return Response()
        session = Session()
        with patch("requests.Session", return_value=session), patch.object(adapter, "_poll", return_value="https://example.test/one.png"):
            from model_runner.credentials import Credential
            credential = Credential(provider="fal", api_key="mock", api_base="https://queue.fal.run",
                                    api_key_source="test", submit_path="")
            result = adapter.resume(request, credential)
        self.assertEqual(session.url, "https://example.test/one.png")
        self.assertEqual(result["output_kind"], "image")
        self.assertEqual(output.read_bytes(), data.getvalue())
        self.assertEqual(record.data["task_id"], "existing-task")

    def test_wan_text_video_thinking_is_explicit_and_business_default_unchanged(self):
        adapter = FalWan3T2VAdapter()
        spec = ModelRegistry(ROOT / "references").resolve("wan3.0-t2v")
        request = GenerationRequest("exact\ntext", spec.id, spec, [], self.root / "t2v.mp4", 5,
                                    options={"enable_thinking": True})
        adapter.validate(request)
        payload = adapter.remote_payload(request)
        self.assertEqual(payload["prompt"], "exact\ntext")
        self.assertIs(payload["enable_thinking"], True)
        self.assertIs(payload["enable_prompt_expansion"], False)
        request.options = {"enable_thinking": False}
        self.assertIs(adapter.remote_payload(request)["enable_thinking"], False)
        request.options = None
        self.assertNotIn("enable_thinking", adapter.remote_payload(request))

    def test_imported_record_restores_only_saved_general_draft(self):
        output = self.root / "general_imported.mp4"
        runner = ModelRunner(ROOT, credential_source="host-app", values={"FAL_KEY": "test-only"})
        exact_prompt = "  一行\n二行  "
        runner.record_metadata = {"general_config": {
            "model": "wan3.0-video", "prompt": exact_prompt,
            "references": [self.video["path"], self.image["path"]],
            "referenceAssetIds": [self.video["id"], self.image["id"]],
            "parameters": {"duration": 5, "aspect_ratio": "16:9", "resolution": "480p"},
            "output_kind": "video"}}
        state = TaskRecord(TaskRecord.path_for(output))
        from dataclasses import asdict
        state.update(version=1, status="submitted", output=str(output),
                     model=asdict(runner.registry.resolve("wan3.0-video")),
                     task_id="existing-request", general_config=runner.record_metadata["general_config"])
        identity = self.store.import_record(state.path)
        restored = self.store.job(identity)
        self.assertEqual(restored["task_id"], "existing-request")
        self.assertEqual(restored["config"]["prompt"], exact_prompt)
        self.assertEqual(restored["config"]["referenceAssetIds"], [self.video["id"], self.image["id"]])
        self.assertEqual(restored["config"]["reference_assets"], [self.video["path"], self.image["path"]])
        self.assertTrue(restored["config"]["reusable"])

    def test_family_route_defaults_and_false_t2v_payload(self):
        from canvas_catalog import resolve_route
        self.assertEqual(resolve_route("wan3", [])[1]["modelId"], "wan3.0-t2v")
        self.assertEqual(resolve_route("wan3", [self.video["id"]])[1]["modelId"], "wan3.0-video")
        draft = {"familyId": "wan3", "nodeType": "video", "model": "wan3.0-video", "prompt": " 原文 ", "referenceAssetIds": [], "parameters": {"enable_thinking": False, "enable_prompt_expansion": False}}
        config = self.service._config("general", draft, uuid.uuid4().hex)
        self.assertEqual(config["model"], "wan3.0-t2v")
        with patch("model_runner.runner.ModelRunner") as runner:
            execute_job({"kind": "general", "config": config}, {}, lambda **_: None, self.store)
            args = runner.return_value.run.call_args.args[0]
        self.assertEqual(args.options, {"enable_thinking": False, "enable_prompt_expansion": False})
        spec = ModelRegistry(ROOT / "references").resolve(args.model)
        request = GenerationRequest(args.prompt, args.model, spec, [], Path(args.output), args.duration, args.aspect_ratio, args.resolution, options=args.options)
        payload = FalWan3T2VAdapter().remote_payload(request)
        self.assertIs(payload["enable_thinking"], False)
        self.assertIs(payload["enable_prompt_expansion"], False)
        defaults = self.service._config("general", {**draft, "parameters": {}}, uuid.uuid4().hex)
        self.assertIs(defaults["parameters"]["enable_thinking"], True)
        self.assertIs(defaults["parameters"]["enable_prompt_expansion"], True)

    def test_frozen_canvas_preflight_uses_shared_skill_root(self):
        # PyInstaller flattens module __file__ into its temporary extraction
        # directory; the installed skill registry remains under SKILL_ROOT.
        installed_skill = self.root / "installed-skill"
        shutil.copytree(ROOT / "references", installed_skill / "references")
        frozen_module = self.root / "_MEI-test" / "canvas_backend.py"
        draft = {"familyId": "wan3", "nodeType": "video", "prompt": "中文预检原文",
                 "referenceAssetIds": [], "parameters": {"duration": 5, "resolution": "480p"}}
        with patch("canvas_backend.__file__", str(frozen_module)), \
                patch("model_runner.runner.SKILL_ROOT", installed_skill), \
                patch("requests.Session", side_effect=AssertionError("preflight must stay offline")), \
                patch.object(self.tasks, "submit", side_effect=AssertionError("preflight must not submit")):
            config = self.service._config("general", draft, uuid.uuid4().hex)
        self.assertEqual(config["model"], "wan3.0-t2v")
        self.assertEqual(config["prompt"], draft["prompt"])
        self.assertFalse(self.tasks.active)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires local media tools")
    def test_local_cost_seconds_reference_billing_and_shorter_sync_media(self):
        audio = self.root / "three_seconds.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b"\0\0" * (16000 * 3))
        audio_asset = self.store.import_asset(audio)
        config = {"familyId": "wan3", "nodeType": "video", "model": "wan3.0-t2v", "parameters": {"duration": 5, "resolution": "480p"}, "referenceAssetIds": []}
        def quote(change):
            return self.service.estimate({"config": {**config, **change}})
        with patch.object(self.tasks, "submit", side_effect=AssertionError("estimate must not schedule")), patch("requests.Session", side_effect=AssertionError("estimate must not use network")):
            self.assertEqual(quote({})["amount"], 0.25)
            self.assertEqual(quote({"parameters": {"duration": 5, "resolution": "720p"}})["amount"], 0.5)
            self.assertEqual(quote({"referenceAssetIds": [self.image["id"]]})["amount"], 0.25)
            reference_quote = quote({"referenceAssetIds": [self.video["id"]]})
            self.assertEqual(reference_quote["amount"], 0.45)
            self.assertAlmostEqual(reference_quote["referenceVideoSeconds"], 4)
            with patch("media_inspection.probe_video", side_effect=AssertionError("cached local metadata")):
                self.assertEqual(quote({"referenceAssetIds": [self.video["id"]]})["amount"], 0.45)
            sync = quote({"familyId": "sync3", "nodeType": "lipsync", "referenceAssetIds": [self.video["id"], audio_asset["id"]]})
            self.assertEqual(sync["status"], "unknown")
            self.assertEqual(quote({"familyId": "sync3", "referenceAssetIds": [self.video["id"]]})["status"], "unknown")
        self.service.duration_cache.clear()
        with patch("media_inspection.probe_frame_rate", return_value=15):
            self.assertEqual(quote({"referenceAssetIds": [self.video["id"]]})["status"], "unknown")
            with self.assertRaises(RuntimeError):
                self.service._config("general", {**config, "prompt": "test", "referenceAssetIds": [self.video["id"]]}, uuid.uuid4().hex)


if __name__ == "__main__":
    unittest.main()
