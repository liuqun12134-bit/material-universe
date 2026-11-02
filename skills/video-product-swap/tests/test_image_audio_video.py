import copy
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fish_mcp import FishError
from desktop_tasks import DesktopStore, TaskManager
from image_audio_video import (BINDING, IMAGE_FIELDS, MEDIA_FIELDS, audio_info_from_probe, digest_file,
                               generation_args, inspect_audio, prepare_quote, run_job, validate_config)
from PIL import Image


def model():
    return {"model_id": "seedance-2.5", "prompt_max_chars": 20000, "capabilities": {"reference": {
        "parameter_schema": {"properties": {"aspect_ratio": {"enum": ["9:16", "16:9"]},
            "resolution": {"enum": ["480p", "720p", "1080p"]}, "duration": {"enum": [f"{i}s" for i in range(4, 31)]}}},
        "inputs": {"ref": {"max_count": 30, "max_bytes": 20971520, "mime_types": ["image/png", "image/jpeg", "image/webp"]},
                   "audioRef": {"max_count": 10, "max_bytes": 15728640, "mime_types": ["audio/mpeg", "audio/wav"],
                                "duration": {"min_sec": 1.8, "max_sec": 30.2, "max_total_sec": 30.2}}}}}}


class FakeClient:
    connection_id = "offline-connection"

    def __init__(self, *_, **__):
        self.calls = []
        self.fail_submit = False
        self.fail_poll = False
        self.poll_status = {"status": "succeeded", "media": {"url": "https://cdn.fish.audio/test.mp4"}}

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_media_model": return model()
        if name == "create_media_upload": return {"upload_url": "https://upload.fish.audio/signed", "object_key": str(len(self.calls))}
        if name == "estimate_video_generation": return {"can_generate": True, "credits": 10808, "balance": 100000, "pricing_version": "test"}
        if name == "list_my_workspaces": return {"workspaces": [{"workspace_id": "workspace"}]}
        if name == "generate_video":
            if self.fail_submit: raise FishError("response lost")
            return {"generation_id": "remote-id", "status": "queued", "poll_after_seconds": 60, "view_url": "https://fish.audio/app/history/test"}
        if name == "get_generation_status":
            if self.fail_poll: raise FishError("poll interrupted")
            return self.poll_status
        raise AssertionError(name)


class ImageAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "library")
        self.config = {"prompt": "人物自然讲解，并展示产品。", "aspect_ratio": "9:16", "resolution": "480p",
                       "duration": "", "workspace_id": "workspace", "output": str(self.root / "result.mp4")}
        for index, field in enumerate(IMAGE_FIELDS):
            path = self.root / (field + ".png")
            Image.new("RGB", (400, 500), (index * 50, 80, 90)).save(path)
            self.config[field] = str(path)
        path = self.root / "voice.wav"
        self.make_wav(path, 4.1)
        self.config["audio"] = str(path)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def make_wav(self, path, seconds):
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
            f.writeframes(b"\x00\x00" * round(16000 * seconds))

    def approved(self):
        with patch("image_audio_video.put_media"):
            config = prepare_quote(self.config, self.store, FakeClient())
        config["cost_approved"] = True
        return config

    def test_rounding_and_bounds(self):
        for seconds, expected in ((4, 4), (24.1, 25), (29.9, 30), (30, 30), (2, 4)):
            payload = {"format": {"format_name": "mp3", "duration": str(seconds)},
                       "streams": [{"codec_type": "audio", "codec_name": "mp3"}]}
            self.assertEqual(audio_info_from_probe(payload, ".mp3")["default_duration"], expected)
        for seconds in (30.00001, 31, 0, -1, float("nan"), float("inf"), 1):
            with self.assertRaises(ValueError):
                audio_info_from_probe({"format": {"format_name": "wav", "duration": str(seconds)},
                    "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}]}, ".wav")

    def test_required_images_and_prompt_before_network(self):
        for field in (*MEDIA_FIELDS, "prompt"):
            config = {**self.config, field: ""}
            client = FakeClient()
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare_quote(config, self.store, client)
            self.assertEqual(client.calls, [])

    def test_real_format_decode_and_length(self):
        info = inspect_audio(self.config["audio"])
        self.assertAlmostEqual(info["duration"], 4.1, places=3)
        self.assertEqual(info["default_duration"], 5)
        fake = self.root / "renamed.mp3"
        shutil.copyfile(self.config["audio"], fake)
        with self.assertRaisesRegex(ValueError, "真实封装"):
            inspect_audio(fake)
        broken = self.root / "broken.wav"; broken.write_text("not audio")
        with self.assertRaises(ValueError): inspect_audio(broken)
        movie = self.root / "sound.mov"; movie.write_bytes(b"fake")
        with self.assertRaisesRegex(ValueError, "MOV"):
            inspect_audio(movie)
        self.make_wav(self.root / "long.wav", 30.01)
        with self.assertRaisesRegex(ValueError, "超过 30 秒"):
            inspect_audio(self.root / "long.wav")

    def test_renamed_m4a_rejected_and_valid_mp3_accepted(self):
        mp3 = self.root / "real.mp3"
        m4a = self.root / "original.m4a"
        for path, codec in ((mp3, "libmp3lame"), (m4a, "aac")):
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", self.config["audio"], "-c:a", codec, str(path)],
                           check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(inspect_audio(mp3)["codec"], "mp3")
        fake = self.root / "fake.mp3"; shutil.copyfile(m4a, fake)
        with self.assertRaisesRegex(ValueError, "真实封装"):
            inspect_audio(fake)

    def test_image_content_and_size_and_duration_rules(self):
        wrong = self.root / "wrong.jpg"; shutil.copyfile(self.config["first_frame"], wrong)
        with self.assertRaisesRegex(ValueError, "真实格式"):
            validate_config({**self.config, "first_frame": str(wrong)})
        tiny = self.root / "tiny.png"; Image.new("RGB", (100, 100)).save(tiny)
        with self.assertRaisesRegex(ValueError, "300"):
            validate_config({**self.config, "first_frame": str(tiny)})
        for duration in ("4", "4.5", "31", "nan"):
            with self.assertRaises(ValueError): validate_config({**self.config, "duration": duration})
        self.assertEqual(validate_config(self.config)[0]["duration"], 5)

    def test_quote_preserves_roles_order_prompt_and_upload_sizes(self):
        client = FakeClient()
        with patch("image_audio_video.put_media") as uploads:
            config = prepare_quote(self.config, self.store, client)
        args = config["fish_quote"]["arguments"]
        self.assertEqual(args["operation"], "reference")
        self.assertEqual(args["prompt"], BINDING + self.config["prompt"])
        self.assertEqual(args["parameters"], {"aspect_ratio": "9:16", "resolution": "480p", "duration": "5s"})
        self.assertEqual([r["role"] for r in args["inputs"]], ["ref", "ref", "ref", "audioRef"])
        self.assertEqual(uploads.call_count, 4)
        self.assertNotIn("generate_video", [name for name, _ in client.calls])
        for field in MEDIA_FIELDS:
            self.assertEqual(Path(config[field]).parent, self.store.files)
        for _, args in [call for call in client.calls if call[0] == "create_media_upload"]:
            self.assertEqual(args["file_size"], (self.store.files / args["file_name"]).stat().st_size)

    def test_live_constraints_checked_before_upload(self):
        client = FakeClient()
        original = client.call
        def call(name, args):
            result = original(name, args)
            if name == "get_media_model": result["capabilities"]["reference"]["inputs"]["audioRef"]["max_count"] = 0
            return result
        client.call = call
        with self.assertRaises(FishError): prepare_quote(self.config, self.store, client)
        self.assertEqual([name for name, _ in client.calls], ["get_media_model"])

    def test_frozen_quote_and_approval_are_required(self):
        config = self.approved()
        for change in ({"cost_approved": False}, {"prompt": "changed"}, {"duration": 30}):
            client = FakeClient()
            with self.assertRaises(ValueError):
                run_job({"config": {**config, **change}}, {}, lambda **_: None, client_factory=lambda *_: client)
            self.assertEqual(client.calls, [])

    def test_single_submit_same_quote_then_resume_only_query(self):
        config = self.approved()
        client = FakeClient(); client.fail_poll = True
        job = {"config": config}
        with self.assertRaises(FishError):
            run_job(job, {}, lambda **_: None, client_factory=lambda *_: client, sleep=lambda _: None)
        state = json.loads(Path(config["output"] + ".video-task.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["task_id"], "remote-id")
        submitted = next(args for name, args in client.calls if name == "generate_video")
        self.assertEqual(submitted["expected_credits"], 10808)
        self.assertEqual(submitted["pricing_version"], "test")
        self.assertEqual(submitted["inputs"], config["fish_quote"]["arguments"]["inputs"])
        other = FakeClient()
        with patch("image_audio_video.download_result", return_value={"local_downloaded": False, "output": None, "view_url": "https://fish.audio/history"}):
            result = run_job(job, {}, lambda **_: None, resume=True, client_factory=lambda *_: other)
        self.assertEqual([name for name, _ in other.calls], ["get_generation_status"])
        self.assertFalse(result["local_downloaded"])

    def test_unknown_submission_never_resubmits(self):
        config = self.approved(); client = FakeClient(); client.fail_submit = True
        job = {"config": config}
        with self.assertRaises(FishError):
            run_job(job, {}, lambda **_: None, client_factory=lambda *_: client)
        state = json.loads(Path(config["output"] + ".video-task.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "submission_unknown")
        with self.assertRaises(FishError):
            run_job(job, {}, lambda **_: None, resume=True, client_factory=lambda *_: client)
        self.assertEqual([name for name, _ in client.calls].count("generate_video"), 1)

    def test_completed_video_download_verification_and_completed_resume(self):
        video = self.root / "remote.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x480:r=24",
                        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                       check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = video.read_bytes()
        class Response:
            ok = True
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def iter_content(self, *_): yield data
        config = self.approved(); client = FakeClient(); delays = []
        with patch("image_audio_video.requests.get", return_value=Response()):
            result = run_job({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client, sleep=delays.append)
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(Path(result["output"]).read_bytes(), data)
        self.assertEqual(delays, [60])
        state = json.loads(Path(config["output"] + ".video-task.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        for field in MEDIA_FIELDS: Path(config[field]).unlink()
        with patch("image_audio_video.FishClient", side_effect=AssertionError("No network needed")):
            resumed = run_job({"config": config}, {}, lambda **_: None, resume=True)
        self.assertTrue(resumed["resumed_existing_task"])

    def test_html_result_is_not_delivered_as_video(self):
        class Response:
            ok = True
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def iter_content(self, *_): yield b"<html>not a video</html>"
        config = self.approved(); client = FakeClient()
        with patch("image_audio_video.requests.get", return_value=Response()):
            result = run_job({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client, sleep=lambda _: None)
        self.assertFalse(result["local_downloaded"])
        self.assertFalse(Path(config["output"]).exists())
        state = json.loads(Path(config["output"] + ".video-task.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "generated")
        self.assertEqual(state["task_id"], "remote-id")

    def test_shared_queue_independent_configs_and_request_dedup(self):
        seen, gate = [], threading.Event()
        def execute(job, credentials, update, store, resume):
            seen.append(job)
            gate.wait(5)
            return {"local_downloaded": False}
        manager = TaskManager(self.store, execute=execute, limit=2)
        try:
            config = self.approved()
            first = manager.submit("image_audio", config, {}, request_id="first")
            self.assertEqual(manager.submit("image_audio", config, {}, request_id="first"), first)
            second = manager.submit("general", {"output": str(self.root / "other.mp4")}, {})
            third = manager.submit("image_audio", config, {}, request_id="third")
            self.assertEqual(len(manager.active), 2)
            self.assertEqual(len(manager.pending), 1)
            config["prompt"] = "edited"
            self.assertNotEqual(self.store.job(first)["config"]["prompt"], "edited")
            self.assertEqual(len({self.store.job(i)["config"]["output"] for i in (first, second, third)}), 3)
            self.assertEqual(self.store.job(first)["kind"], "image_audio")
            self.assertEqual(len(self.store.job(first)["asset_ids"]), 4)
        finally:
            gate.set(); manager.wait_idle(5); manager.stop(); manager.owner.close()

    def test_history_import_keeps_module_and_configuration(self):
        config = self.approved(); client = FakeClient(); client.fail_poll = True
        with self.assertRaises(FishError):
            run_job({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client, sleep=lambda _: None)
        identity = self.store.import_record(config["output"] + ".video-task.json")
        job = self.store.job(identity)
        self.assertEqual(job["kind"], "image_audio")
        self.assertEqual(job["config"]["prompt"], config["prompt"])
        self.assertEqual(job["config"]["first_frame"], config["first_frame"])


if __name__ == "__main__": unittest.main()
