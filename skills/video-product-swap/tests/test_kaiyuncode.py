"""Offline wire contracts and recovery tests; never calls paid services."""
import base64
import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canvas_backend import CanvasService
from canvas_catalog import MODEL_FAMILIES, runner_options
from desktop_tasks import DesktopStore, TaskManager, execute_job
from model_runner.runner import ModelRunner
from model_runner.task_state import TaskRecord
from model_runner.core import VideoGenerationError
from product_swap import ProductSwapWorkflow


class Response:
    def __init__(self, data=None, content=b"", status=200, headers=None):
        self.data, self.content, self.status_code = data, content, status
        self.ok = 200 <= status < 400
        self.text = json.dumps(data or {})
        self.headers = headers or {}

    def json(self):
        return self.data


class KaiyunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        path = Path(cls.media.name) / "source.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=256x256:r=25:d=2",
                        "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
        cls.video = path.read_bytes()
        buf = io.BytesIO()
        Image.new("RGB", (256, 256), "green").save(buf, format="JPEG")
        cls.jpeg = buf.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.runner = ModelRunner(ROOT, credential_source="host-app", values={"KAIYUNCODE_API_KEY": "test-secret"})
        self.source = self.folder / "source.mp4"
        self.source.write_bytes(self.video)
        self.image = self.folder / "ref.jpg"
        self.image.write_bytes(self.jpeg)

    def args(self, model="wan3.0-video", **extra):
        values = dict(model=model, provider="kaiyuncode", prompt="  exact prompt\n第二行  ", reference=[],
                      output=str(self.folder / ("out.png" if "banana" in model else "out.mp4")),
                      duration=None, aspect_ratio=None, resolution=None, options=None, dry_run=True)
        values.update(extra)
        return Namespace(**values)

    def test_registry_provider_identity_and_all_six_models(self):
        self.assertEqual(len([m for m in self.runner.list_models() if m["provider"] == "kaiyuncode"]), 6)
        for model in ("wan3.0-video", "wan3.0-video-prime"):
            selected = self.runner.registry.resolve_for_provider("kaiyuncode", model)
            self.assertEqual(selected.api_model, model)
            self.assertEqual(selected.provider, "kaiyuncode")
        self.assertEqual(self.runner.registry.resolve("wan3.0-video").provider, "fal")
        self.assertEqual(self.runner.registry.resolve("omni_video_edit").provider, "kaiyuncode")

    def test_wan_multipart_plan_and_provider_specific_options(self):
        args = self.args(reference=[str(self.source), str(self.image), "image=https://media.example/ref.png"],
                         duration=6, aspect_ratio="9:16", resolution="480p",
                         options={"enable_prompt_expansion": False, "enable_thinking": False})
        with patch("requests.Session", side_effect=AssertionError("offline")):
            plan = self.runner.run(args)
        self.assertEqual(plan["request_payload"], {"model": "wan3.0-video", "prompt": args.prompt, "duration": 6,
                                                 "resolution": "480P", "ratio": "9:16", "prompt_extend": False,
                                                 "images": ["https://media.example/ref.png"]})
        self.assertEqual([item["field"] for item in plan["local_uploads"]], ["videos", "images"])
        with self.assertRaises(VideoGenerationError):
            self.runner.run(self.args(options={"enable_thinking": True}))

    def test_seedance_and_omni_exact_wire_parameters(self):
        seed = self.runner.run(self.args("seedance-2.5-tj", duration=30, resolution="720p", aspect_ratio="1:1"))
        self.assertEqual(seed["request_payload"]["seconds"], "30")
        self.assertEqual(seed["request_payload"]["resolution"], "720p")
        self.assertEqual(self.runner.run(self.args("seedance-2.5-tj"))["request_payload"]["resolution"], "720p")
        self.assertNotIn("duration", seed["request_payload"])
        omni = self.runner.run(self.args("omni", duration=6, aspect_ratio="9:16"))
        self.assertEqual(omni["request_payload"]["duration"], "6")
        self.assertNotIn("resolution", omni["request_payload"])
        for args in (self.args("omni", duration=5), self.args("seedance-2.5-tj", duration=7),
                     self.args("seedance-2.5-tj", resolution="480p"),
                     self.args("seedance-2.5-tj", resolution="1080p"),
                     self.args("omni", reference=[str(self.image)])):
            with self.subTest(model=args.model), self.assertRaises(VideoGenerationError):
                self.runner.run(args)

    def test_multipart_single_submit_then_only_gets_and_valid_video(self):
        session = Mock()
        session.post.return_value = Response({"id": "task-123", "poll_url": "/v1/videos/task-123"})
        session.get.side_effect = [Response({"status": "completed", "url": "https://cdn.example/result.mp4"}),
                                   Response(content=self.video)]
        args = self.args(reference=[str(self.source), str(self.image)], dry_run=False)
        with patch("requests.Session", return_value=session):
            result = self.runner.run(args)
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(session.post.call_count, 1)
        fields = session.post.call_args.kwargs["data"]
        self.assertIn(("model", "wan3.0-video"), fields)
        self.assertEqual([name for name, _ in session.post.call_args.kwargs["files"]], ["videos", "images"])
        self.assertEqual(session.get.call_args.kwargs["headers"], {})
        state = json.loads(TaskRecord.path_for(Path(args.output)).read_text(encoding="utf-8"))
        self.assertEqual(state["task_id"], "task-123")
        self.assertEqual(state["model"]["provider"], "kaiyuncode")
        self.assertNotIn("test-secret", json.dumps(state))

    def test_seedance_mixed_references_use_seedance_fields_and_single_submission(self):
        audio = self.folder / "voice.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)], check=True, capture_output=True)
        refs = [str(self.image), str(self.source), str(audio),
                "image=https://media.example/ref.png", "video=https://media.example/motion.mp4", "audio=https://media.example/voice.mp3"]
        plan = self.runner.run(self.args("seedance-2.5-tj", reference=refs))
        self.assertEqual(plan["local_uploads"], [])
        self.assertEqual(plan["transport"], "multipart-data-url-text")
        self.assertEqual(plan["request_payload"]["videos"], ["<local-data-url:video>", "https://media.example/motion.mp4"])
        self.assertEqual(plan["request_payload"]["audios"], ["<local-data-url:audio>", "https://media.example/voice.mp3"])
        self.assertNotIn("reference_video_urls", plan["request_payload"])
        session = Mock()
        session.post.return_value = Response({"id": "seedance-task"})
        session.get.side_effect = [Response({"status": "completed", "url": "https://cdn.example/result.mp4"}), Response(content=self.video)]
        with patch("requests.Session", return_value=session):
            result = self.runner.run(self.args("seedance-2.5-tj", reference=refs, dry_run=False))
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(session.post.call_count, 1)
        submitted = session.post.call_args.kwargs
        fields = {key: json.loads(value[1]) if key in {"images", "videos", "audios"} else value[1]
                  for key, value in submitted["files"]}
        self.assertEqual(fields["model"], "seedance-2.5-tj")
        self.assertEqual(fields["resolution"], "720p")
        self.assertEqual(fields["videos"][1], "https://media.example/motion.mp4")
        self.assertEqual(fields["audios"][1], "https://media.example/voice.mp3")
        self.assertTrue(all(value[0] is None for _, value in submitted["files"]))
        self.assertNotIn("json", submitted)
        self.assertNotIn("data", submitted)
        for field, path in (("images", self.image), ("videos", self.source), ("audios", audio)):
            prefix, encoded = fields[field][0].split(",", 1)
            self.assertTrue(prefix.startswith("data:"))
            self.assertTrue(prefix.endswith(";base64"))
            self.assertEqual(base64.b64decode(encoded), path.read_bytes())
        state = json.loads(TaskRecord.path_for(Path(result["output"])).read_text(encoding="utf-8"))
        self.assertEqual(state["submission_protocol"]["field_counts"], {"images": 2, "videos": 2, "audios": 2})
        self.assertNotIn("base64,", json.dumps(state))
        self.assertNotIn("base64,", json.dumps(plan))

    def test_seedance_three_png_three_wav_text_parts_and_rejection_safety(self):
        images, audios = [], []
        for index in range(3):
            image = self.folder / f"ref{index}.png"
            Image.new("RGB", (256, 256), (index * 50, 10, 30)).save(image)
            audio = self.folder / f"voice{index}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={400 + index * 100}:duration=2", str(audio)], check=True, capture_output=True)
            images.append(image)
            audios.append(audio)
        for failure in (400, "timeout"):
            args = self.args("seedance-2.5-tj", reference=[str(p) for p in images + audios],
                             output=str(self.folder / f"seed-{failure}.mp4"), dry_run=False)
            session = Mock()
            if failure == 400:
                session.post.return_value = Response({"code": 1, "msg": "上传失败：Unexpected field"}, status=400)
            else:
                session.post.side_effect = TimeoutError("response lost")
            with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError) as raised:
                self.runner.run(args)
            if failure == 400:
                self.assertIn("上传字段或数量与服务不兼容", str(raised.exception))
                self.assertIn("Unexpected field", str(raised.exception))
            self.assertEqual(session.post.call_count, 1)
            session.get.assert_not_called()
            submitted = session.post.call_args.kwargs
            self.assertTrue(all(value[0] is None for _, value in submitted["files"]))
            self.assertNotIn("json", submitted)
            self.assertNotIn("data", submitted)
            arrays = {key: json.loads(value[1]) for key, value in submitted["files"] if key in {"images", "audios"}}
            import requests
            wire = requests.Request("POST", "https://example.invalid/v1/videos", files=submitted["files"]).prepare()
            self.assertTrue(wire.headers["Content-Type"].startswith("multipart/form-data; boundary="))
            self.assertNotIn(b"filename=", wire.body)
            self.assertIn(b'name="images"', wire.body)
            self.assertIn(b'name="audios"', wire.body)
            self.assertNotIn(b'name="image"', wire.body)
            self.assertNotIn(b'name="audio"', wire.body)
            for field, paths, mime in (("images", images, "image/png"), ("audios", audios, "audio/wav")):
                self.assertEqual(len(arrays[field]), 3)
                for value, path in zip(arrays[field], paths):
                    self.assertTrue(value.startswith("data:" + mime + ";base64,"))
                    self.assertEqual(base64.b64decode(value.split(",", 1)[1]), path.read_bytes())
            state_file = TaskRecord.path_for(Path(args.output))
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "not_submitted" if failure == 400 else "submission_unknown")
            self.assertNotIn("base64,", json.dumps(state))
            with patch("requests.Session", side_effect=AssertionError("must not resubmit")), self.assertRaises(VideoGenerationError):
                self.runner.resume(state_file)

    def test_seedance_limits_and_bad_resolution_stop_before_network(self):
        with patch("requests.Session", side_effect=AssertionError("must not submit")):
            for refs in ([str(self.image)] * 31, [str(self.source)] * 11,
                         ["image=https://media.example/ref.png"] * 31):
                with self.subTest(refs=len(refs)), self.assertRaises(VideoGenerationError):
                    self.runner.run(self.args("seedance-2.5-tj", reference=refs, dry_run=False))
            for resolution in ("480p", "1080p"):
                with self.subTest(resolution=resolution), self.assertRaisesRegex(VideoGenerationError, "仅支持 720p"):
                    self.runner.run(self.args("seedance-2.5-tj", resolution=resolution, dry_run=False))
            from types import SimpleNamespace
            for duration in (1.9, 14.1):
                with patch("media_inspection.probe_video", return_value=SimpleNamespace(duration=duration)), self.assertRaisesRegex(VideoGenerationError, "2–14"):
                    self.runner.run(self.args("seedance-2.5-tj", reference=[str(self.source)], dry_run=False))
            for duration in (2, 14):
                with patch("media_inspection.probe_video", return_value=SimpleNamespace(duration=duration)):
                    self.runner.run(self.args("seedance-2.5-tj", reference=[str(self.source)]))

    def test_timeouts_and_auth_rejection_never_resubmit(self):
        for status in ("timeout", 401):
            args = self.args("omni", output=str(self.folder / f"{status}.mp4"), dry_run=False)
            session = Mock()
            if status == "timeout":
                session.post.side_effect = TimeoutError("response lost")
            else:
                session.post.return_value = Response({"error": "bad key"}, status=status)
            with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
                self.runner.run(args)
            state_file = TaskRecord.path_for(Path(args.output))
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "submission_unknown" if status == "timeout" else "not_submitted")
            with patch("requests.Session", side_effect=AssertionError("must not send")), self.assertRaises(VideoGenerationError):
                self.runner.resume(state_file)
            self.assertEqual(session.post.call_count, 1)

    def test_failed_query_and_html_download_preserve_original_id(self):
        for terminal in (True, False):
            args = self.args("omni", output=str(self.folder / f"{terminal}.mp4"), dry_run=False)
            session = Mock()
            session.post.return_value = Response({"id": "original"})
            session.get.side_effect = ([Response({"status": "failed", "error": "rejected"})] if terminal else
                                       [Response({"status": "completed", "video_url": "https://cdn.example/out"}),
                                        Response(content=b"<html>gateway error</html>")])
            with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
                self.runner.run(args)
            self.assertFalse(Path(args.output).exists())
            state = json.loads(TaskRecord.path_for(Path(args.output)).read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed" if terminal else "interrupted")
            self.assertEqual(state["task_id"], "original")

    def test_expired_video_url_refreshes_only_original_task(self):
        args = self.args("omni", dry_run=False)
        session = Mock()
        session.post.return_value = Response({"id": "original"})
        session.get.side_effect = [Response({"status": "completed", "video_url": "https://cdn.example/old"}),
                                   Response(status=403),
                                   Response({"status": "completed", "video_url": "https://cdn.example/new"}),
                                   Response(content=self.video)]
        with patch("requests.Session", return_value=session):
            self.runner.run(args)
        self.assertEqual(session.post.call_count, 1)
        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertEqual(urls.count("https://kaiyuncode.com/v1/videos/original"), 2)

    def test_foreign_poll_url_rejected_but_id_saved(self):
        args = self.args("omni", dry_run=False)
        session = Mock()
        session.post.return_value = Response({"id": "saved", "poll_url": "https://foreign.example/poll"})
        with patch("requests.Session", return_value=session), self.assertRaisesRegex(VideoGenerationError, "同源"):
            self.runner.run(args)
        session.get.assert_not_called()
        state = json.loads(TaskRecord.path_for(Path(args.output)).read_text(encoding="utf-8"))
        self.assertEqual(state["task_id"], "saved")
        self.assertNotIn("poll_url", state)

    def test_nano_reference_image_payload_and_actual_png_output(self):
        args = self.args("gemini-nano-banana-2.1", reference=[str(self.image)],
                         aspect_ratio="8:1", options={"image_size": "4K"}, dry_run=False)
        session = Mock()
        session.post.return_value = Response({"data": [{"url": "https://cdn.example/image.jpg"}]})
        session.get.return_value = Response(content=self.jpeg)
        with patch("requests.Session", return_value=session):
            result = self.runner.run(args)
        self.assertTrue(session.post.call_args.args[0].endswith("/v1/images/generations"))
        self.assertEqual([k for k, _ in session.post.call_args.kwargs["files"]], ["image"])
        self.assertIn(("image_size", "4K"), session.post.call_args.kwargs["data"])
        with Image.open(result["output"]) as image:
            self.assertEqual(image.format, "PNG")
        self.assertEqual(result["output_kind"], "image")

    def test_image_download_failure_resumes_get_only(self):
        args = self.args("gemini-nano-banana-2.1", dry_run=False)
        session = Mock()
        session.post.return_value = Response({"data": [{"url": "https://cdn.example/image"}]})
        session.get.return_value = Response(status=503)
        with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
            self.runner.run(args)
        recovered = Mock()
        recovered.get.return_value = Response(content=self.jpeg)
        with patch("requests.Session", return_value=recovered):
            result = ModelRunner(ROOT, values={}).resume(TaskRecord.path_for(Path(args.output)))
        recovered.post.assert_not_called()
        self.assertTrue(result["local_downloaded"])

    def test_base64_image_save_failure_is_locally_recoverable(self):
        args = self.args("gemini-nano-banana-2.1", dry_run=False)
        session = Mock()
        session.post.return_value = Response({"data": [{"b64_json": base64.b64encode(self.jpeg).decode()}]})
        with patch("requests.Session", return_value=session), patch("model_runner.adapters.kaiyuncode.save_download", side_effect=OSError("disk full")), self.assertRaises(VideoGenerationError):
            self.runner.run(args)
        path = TaskRecord.path_for(Path(args.output))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "interrupted")
        with patch("requests.Session", side_effect=AssertionError("local recovery")):
            result = ModelRunner(ROOT, values={}).resume(path)
        self.assertTrue(result["local_downloaded"])
        self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["image_base64"])

    def test_omni_edit_local_video_and_remote_images_messages(self):
        urls = "https://cdn.example/one.png\nhttps://cdn.example/two.jpg"
        args = self.args("omni_video_edit", reference=[str(self.source)],
                         aspect_ratio="16:9", options={"reference_image_urls": urls})
        plan = self.runner.run(args)
        payload = plan["request_payload"]
        self.assertEqual([part["type"] for part in payload["messages"][0]["content"]], ["text", "image_url", "image_url"])
        self.assertEqual(payload["messages"][0]["content"][0]["text"], args.prompt)
        self.assertEqual(plan["local_uploads"][0]["field"], "video")
        self.assertNotIn("duration", payload)
        self.assertNotIn("video_url", payload)
        session = Mock()
        session.post.return_value = Response({"id": "edit"})
        session.get.side_effect = [Response({"status": "completed", "url": "https://cdn.example/edit.mp4"}), Response(content=self.video)]
        args.dry_run = False
        with patch("requests.Session", return_value=session):
            self.runner.run(args)
        fields = dict(session.post.call_args.kwargs["data"])
        self.assertEqual(json.loads(fields["messages"]), payload["messages"])
        for index, bad in enumerate((self.args("omni_video_edit", reference=[]),
                    self.args("omni_video_edit", reference=[str(self.source)] + [str(self.image)] * 6),
                    self.args("omni_video_edit", reference=[str(self.source)], duration=8),
                    self.args("omni_video_edit", reference=[str(self.source)], options={"reference_image_urls": "file:///secret.png"}))):
            bad.output = str(self.folder / f"invalid-edit-{index}.mp4")
            with self.assertRaises(VideoGenerationError):
                self.runner.run(bad)

    def test_swap_prime_preserves_route_and_does_not_analyze_in_dry_run(self):
        with patch("requests.Session", side_effect=AssertionError("offline")):
            plan = ProductSwapWorkflow(self.runner).generate(self.source, self.image, "一样大", self.folder / "swap.mp4",
                        model="kaiyuncode-wan3.0-video-prime", dry_run=True)
        self.assertEqual(plan["resolved_model"], "wan3.0-video-prime")
        self.assertFalse(plan["enable_thinking"])
        self.assertFalse(plan["analysis_performed"])
        self.assertIsNone(plan["request_payload"]["prompt"])

    def test_omni_local_images_temporarily_hosted_without_key_then_generate_once(self):
        args = self.args("omni_video_edit", reference=[str(self.source), str(self.image)])
        with patch("requests.Session", side_effect=AssertionError("dry run must not upload")):
            plan = self.runner.run(args)
        self.assertEqual(plan["local_uploads"][1]["retention"], "24h")
        uploaded = Response()
        uploaded.text = "https://litter.catbox.moe/reference.jpg\n"
        session = Mock()
        session.post.side_effect = [uploaded, Response({"id": "original"})]
        session.get.side_effect = [Response({"status": "completed", "video_url": "https://cdn.example/edit.mp4"}), Response(content=self.video)]
        args.dry_run = False
        with patch("requests.Session", return_value=session):
            self.runner.run(args)
        upload, submit = session.post.call_args_list
        self.assertNotIn("headers", upload.kwargs)
        self.assertEqual(upload.kwargs["data"]["time"], "24h")
        self.assertTrue(submit.args[0].endswith("/v1/videos"))
        fields = dict(submit.kwargs["data"])
        self.assertEqual(json.loads(fields["messages"])[0]["content"][1]["image_url"]["url"],
                         "https://litter.catbox.moe/reference.jpg")
        self.assertEqual([name for name, _ in submit.kwargs["files"]], ["video"])
        self.assertEqual(session.post.call_count, 2)

    def test_omni_upload_failure_never_submits_paid_generation(self):
        args = self.args("omni_video_edit", reference=[str(self.source), str(self.image)], dry_run=False)
        session = Mock()
        session.post.return_value = Response(status=503)
        with patch("requests.Session", return_value=session), self.assertRaises(VideoGenerationError):
            self.runner.run(args)
        self.assertEqual(session.post.call_count, 1)
        self.assertIn("litterbox.catbox.moe", session.post.call_args.args[0])
        state = json.loads(TaskRecord.path_for(Path(args.output)).read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "not_submitted")
        session.get.assert_not_called()

    def test_canvas_models_image_references_and_execution_options(self):
        store = DesktopStore(self.folder / "data")
        tasks = TaskManager(store, execute=lambda *_: {"local_downloaded": False})
        self.addCleanup(store.db.close)
        self.addCleanup(lambda: tasks.wait_idle(3))
        self.addCleanup(tasks.stop)
        service = CanvasService(store, tasks, self.folder, lambda: {}, models=self.runner.list_models())
        image = store.import_asset(self.image)
        draft = {"familyId": "nano-banana-kaiyuncode", "nodeType": "image",
                 "model": "kaiyuncode-gemini-nano-banana-2.1", "provider": "kaiyuncode",
                 "prompt": "  保留原文  ", "referenceAssetIds": [image["id"]],
                 "parameters": {"image_size": "2K", "aspect_ratio": "4:1"}}
        config = service._config("general", draft, uuid.uuid4().hex)
        self.assertEqual(config["output_kind"], "image")
        self.assertEqual(config["prompt"], draft["prompt"])
        self.assertEqual(runner_options(config["model"], config["parameters"]), {"image_size": "2K"})
        with patch.object(ModelRunner, "run", return_value={}) as run:
            execute_job({"kind": "general", "config": config}, {}, lambda **_: None, store)
        self.assertEqual(run.call_args.args[0].options, {"image_size": "2K"})
        self.assertEqual(run.call_args.args[0].aspect_ratio, "4:1")
        self.assertNotIn("omni-edit-kaiyuncode", {f["id"] for f in MODEL_FAMILIES})
        with self.assertRaisesRegex(ValueError, "已下线"):
            service._config("general", {"model": "kaiyuncode-omni_video_edit"}, uuid.uuid4().hex)
        for bad in ({**draft, "parameters": {"duration": 5}}, {**draft, "provider": "fal"}):
            with self.assertRaises((ValueError, VideoGenerationError)):
                service._config("general", bad, uuid.uuid4().hex)

    def test_canvas_seedance_30_images_save_and_preflight_without_twenty_ref_cap(self):
        store = DesktopStore(self.folder / "data")
        tasks = TaskManager(store, execute=lambda *_: {})
        self.addCleanup(store.db.close)
        self.addCleanup(lambda: tasks.wait_idle(3))
        self.addCleanup(tasks.stop)
        service = CanvasService(store, tasks, self.folder, lambda: {}, models=self.runner.list_models())
        assets = []
        for i in range(31):
            path = self.folder / f"image-{i}.png"
            Image.new("RGB", (16, 16), (i, 0, 0)).save(path)
            assets.append(store.import_asset(path))
        ids = [asset["id"] for asset in assets[:30]]
        draft = {"familyId": "seedance-kaiyuncode", "nodeType": "video", "model": "kaiyuncode-seedance-2.5-tj",
                 "provider": "kaiyuncode", "prompt": "  exact prompt  ", "referenceAssetIds": ids, "parameters": {"resolution": "720p"}}
        canvas = service.canvases.create("30 references")
        nodes = [{"id": "task", "type": "task", "position": {"x": 0, "y": 0}, "data": {"kind": "general", "config": draft}}]
        nodes += [{"id": a["id"], "type": "asset", "position": {"x": 0, "y": 0}, "data": {"assetId": a["id"]}} for a in assets[:30]]
        edges = [{"id": str(i), "source": asset_id, "target": "task", "data": {"kind": "input", "assetId": asset_id, "order": i}} for i, asset_id in enumerate(ids)]
        service.canvases.save(canvas["id"], {"revision": 0, "nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 1}})
        with patch("requests.Session", side_effect=AssertionError("preflight must stay offline")):
            config = service._config("general", draft, uuid.uuid4().hex)
        self.assertEqual(config["referenceAssetIds"], ids)
        self.assertEqual(config["prompt"], draft["prompt"])
        for invalid in ({**draft, "referenceAssetIds": [a["id"] for a in assets]},
                        {**draft, "parameters": {"resolution": "480p"}}, {**draft, "parameters": {"resolution": "1080p"}}):
            with self.assertRaises(ValueError):
                service._config("general", invalid, uuid.uuid4().hex)


if __name__ == "__main__":
    unittest.main()
