import copy
import io
import json
import math
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import voice_service as voice
from fish_mcp import FishError, FishToolError
from model_runner.task_state import TaskRecord


def sine_wav(seconds=2, amplitude=7000, sample_rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * n / sample_rate)))
                                    for n in range(int(seconds * sample_rate))))
    return buffer.getvalue()


class Client:
    connection_id = "connection-one"
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def call(self, tool, args):
        self.calls.append((tool, copy.deepcopy(args)))
        if self.handler:
            return self.handler(tool, args)
        if tool == "text_to_speech" and args.get("dry_run"):
            return {"dry_run": True, "estimated_credits": 6, "has_sufficient_balance": True, "balance": 100}
        return {"audio_url": "https://example.test/audio.wav", "credits_charged": 6}


class VoiceServiceTests(unittest.TestCase):
    def config(self, directory):
        return {"text": "你好", "voice_id": "mine", "voice_name": "我的声音", "speed": 1., "volume_db": 0.,
                "audio_settings_mode": "fish",
                "output": str(Path(directory) / "speech.wav")}

    def prepared(self, directory):
        with patch.object(voice, "_media_tool", return_value="ffmpeg"):
            config = voice.prepare_speech(self.config(directory), Client())
        config["cost_approved"] = True
        return config

    def test_my_voices_pagination_and_sample_mapping(self):
        def search(tool, args):
            if tool == "get_voice":
                return {"id": args["voice_id"], "description": "自然、平淡的小场景口播"}
            # Match the provider's validation so an oversized page fails here.
            if not 1 <= args["page_size"] <= 20:
                raise FishError("page_size must be between 1 and 20")
            return {"voices": [{"id": "a", "title": "私有音色", "sample_audio": "https://example.test/a.mp3"}], "has_next": args["page"] < 3}
        client = Client(search)
        result = voice.list_voices(client, page=2, query=" 私有 ")
        self.assertEqual(client.calls[0], ("search_voices", {"self": True, "page": 2, "page_size": 20,
                                                          "sort_by": "created_at", "query": "私有"}))
        self.assertEqual(result["voices"][0]["sample_url"], "https://example.test/a.mp3")
        self.assertEqual(result["voices"][0]["description"], "自然、平淡的小场景口播")
        self.assertTrue(result["has_next"])
        last_page = voice.list_voices(client, page=3, query="私有")
        self.assertFalse(last_page["has_next"])
        self.assertEqual([args["page"] for tool, args in client.calls if tool == "search_voices"], [2, 3])

    def test_voice_description_cache_is_bounded_and_scoped_to_connection(self):
        voice._voice_details_cache.clear()
        def handler(tool, args):
            if tool == "search_voices":
                return {"voices": [{"id": "notes", "title": "同名音色"}], "has_next": False}
            return {"id": "notes", "description": "音色备注", "sample": {"audio": "https://example.test/sample.mp3"}}
        first = Client(handler)
        result = voice.list_voices(first)
        self.assertEqual(result["voices"][0]["description"], "音色备注")
        self.assertEqual(result["voices"][0]["sample_url"], "https://example.test/sample.mp3")
        voice.list_voices(first)
        self.assertEqual(sum(tool == "get_voice" for tool, _ in first.calls), 1)
        second = Client(handler); second.connection_id = "other-team"
        voice.list_voices(second)
        self.assertEqual(sum(tool == "get_voice" for tool, _ in second.calls), 1)
        with patch.object(voice, "_VOICE_DETAILS_CACHE_SIZE", 1):
            third = Client(handler); third.connection_id = "third-team"
            voice.list_voices(third)
        self.assertEqual(len(voice._voice_details_cache), 1)
        voice._voice_details_cache.clear()

    def test_one_unavailable_description_does_not_hide_voice_list(self):
        voice._voice_details_cache.clear()
        def handler(tool, args):
            if tool == "search_voices":
                return {"voices": [{"id": "bad", "title": "已有音色"}, {"id": "good", "title": "另一个音色"}]}
            if args["voice_id"] == "bad":
                raise FishError("音色详情暂时不可用")
            return {"id": "good", "description": "热情讲解"}
        result = voice.list_voices(Client(handler))
        self.assertEqual(len(result["voices"]), 2)
        self.assertTrue(result["voices"][0]["description_unavailable"])
        self.assertEqual(result["voices"][1]["description"], "热情讲解")
        voice._voice_details_cache.clear()

    def test_clone_rights_and_combined_duration_checked_before_upload(self):
        client = Client()
        with self.assertRaisesRegex(ValueError, "授权"):
            voice.create_clone(client, ["sample.wav"], "mine", False)
        self.assertEqual(client.calls, [])
        with patch.object(voice, "inspect_clone_audio", return_value={"duration": 140}):
            with self.assertRaisesRegex(ValueError, "270"):
                voice.create_clone(client, ["a", "b"], "mine", True)
        self.assertEqual(client.calls, [])

    def test_clone_warning_continuation_does_not_reupload(self):
        warning = {"status": "quality_warning", "preflight_id": "preflight", "instructions": "样本有噪声"}
        client = Client(lambda *_: warning)
        with patch.object(voice, "inspect_clone_audio", return_value={"duration": 20}), \
                patch.object(voice, "_upload_audio", return_value="https://example.test/sample.wav") as upload:
            result = voice.create_clone(client, ["a.wav"], "mine", True)
            self.assertEqual(result["status"], "quality_warning")
            voice.create_clone(client, [], "mine", True, preflight_id="preflight", proceed_despite_warnings=True)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(client.calls[1][1], {"confirm_voice_rights": True, "visibility": "private", "title": "mine",
                                            "preflight_id": "preflight", "proceed_despite_warnings": True})

    def test_clone_verification_normalization_and_upload(self):
        def handler(tool, args):
            if tool == "create_voice_clone":
                raise FishToolError({"status": "verification_required", "preflight_id": "p",
                                     "prompt_sentence": "这是我的声音", "instructions": "请朗读"})
            return {"status": "success", "voice": {"id": "created"}}
        client = Client(handler)
        result = voice.create_clone(client, [], "mine", True, preflight_id="p")
        self.assertEqual(result["verification_prompt"], "这是我的声音")
        with patch.object(voice, "inspect_clone_audio", return_value={"duration": 3}), \
                patch.object(voice, "_upload_audio", return_value="https://example.test/verify.wav"):
            result = voice.verify_clone(client, "p", "verify.wav", "mine")
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["voice_id"], "created")
        self.assertEqual(client.calls[-1][1]["recording_url"], "https://example.test/verify.wav")
        client.handler = lambda *_: {"status": "verification_required", "instructions": "请重新录制"}
        self.assertEqual(voice.verify_clone(client, "p")["preflight_id"], "p")

    def test_quote_fixed_model_and_no_fabricated_speed_parameters(self):
        client = Client()
        with tempfile.TemporaryDirectory() as directory, patch.object(voice, "_media_tool", return_value="media"):
            config = self.config(directory)
            config["model"] = "wrong-model"
            result = voice.prepare_speech(config, client)
            self.assertEqual(client.calls, [("text_to_speech", {"text": "你好", "voice_id": "mine", "model": "drama-3", "dry_run": True})])
            self.assertEqual(result["fish_quote"]["credits"], 6)
            self.assertEqual(result["model"], "drama-3")
            self.assertEqual(result["connection_id"], client.connection_id)
            self.assertNotIn("cost_approved", result)

    def test_invalid_or_insufficient_quote_never_accepted(self):
        for payload in ({"dry_run": True, "estimated_credits": 5, "has_sufficient_balance": False},
                        {"dry_run": True, "estimated_credits": float("nan"), "has_sufficient_balance": True},
                        {"dry_run": False, "estimated_credits": 5, "has_sufficient_balance": True}):
            with tempfile.TemporaryDirectory() as directory, patch.object(voice, "_media_tool", return_value="media"):
                with self.assertRaises(FishError):
                    voice.prepare_speech(self.config(directory), Client(lambda *_: payload))

    def test_approved_parameters_cannot_change_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepared(directory)
            for field, value in (("text", "改了文案"), ("voice_id", "other"), ("speed", 1.2), ("volume_db", -3)):
                modified = {**config, field: value}
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "重新预估"):
                    voice.validate_approved(modified)
            for field, value in (("speed", float("nan")), ("speed", .69), ("speed", 1.31),
                                 ("volume_db", -5.1), ("volume_db", 5.1), ("volume_db", float("inf")),
                                 ("speed", True), ("volume_db", False)):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    voice.validate_speech_config({**config, field: value})

    def test_current_settings_boundaries_and_legacy_submission_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            for speed, volume in ((.7, -5), (1.3, 5), (1, 0)):
                checked = voice.validate_speech_config({**config, "speed": speed, "volume_db": volume})
                self.assertEqual((checked["speed"], checked["volume_db"]), (speed, volume))
                self.assertEqual(checked["audio_settings_mode"], "fish")
            for legacy in ({**config, "volume": 100}, {**config, "audio_settings_mode": "local"}):
                client = Client()
                with self.assertRaisesRegex(ValueError, "旧版.*重新预估"):
                    voice.prepare_speech(legacy, client)
                self.assertEqual(client.calls, [])

    def test_plain_tts_rejects_unsupported_controls_without_a_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            for fields in ({"speed": .9}, {"volume_db": -3}):
                client = Client()
                with self.assertRaisesRegex(ValueError, "不支持语速和音量"):
                    voice.prepare_speech({**self.config(directory), **fields}, client)
                self.assertEqual(client.calls, [])

    def test_saved_controls_migrate_percent_to_db_only_for_form_reuse(self):
        legacy = {"speed": .5, "volume": 80}
        mapped = voice.speech_form_settings(legacy)
        self.assertEqual(mapped, {"speed": .7, "volume_db": -1.9, "migrated": True})
        self.assertEqual(legacy, {"speed": .5, "volume": 80})
        self.assertEqual(voice.speech_form_settings({"speed": 2, "volume": 0}),
                         {"speed": 1.3, "volume_db": -5., "migrated": True})
        self.assertEqual(voice.speech_form_settings({"speed": .9, "volume_db": -3, "audio_settings_mode": "fish"}),
                         {"speed": .9, "volume_db": -3., "migrated": False})

    def test_quote_change_stops_before_paid_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepared(directory)
            client = Client(lambda *_: {"dry_run": True, "estimated_credits": 8, "has_sufficient_balance": True})
            with self.assertRaisesRegex(FishError, "报价已变化"):
                voice.run_job({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client)
            self.assertTrue(all(args.get("dry_run") for _, args in client.calls))
            state = json.loads(TaskRecord.path_for(Path(config["output"])).read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "not_submitted")

    def test_connection_snapshot_and_lost_response_never_resubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepared(directory)
            client = Client()
            def handler(tool, args):
                if args.get("dry_run"):
                    return {"dry_run": True, "estimated_credits": 6, "has_sufficient_balance": True}
                raise FishError("网络中断")
            client.handler = handler
            factory_args = []
            def factory(*args):
                factory_args.append(args)
                return client
            with patch.object(voice, "_media_tool", return_value="ffmpeg"), self.assertRaises(FishError):
                voice.run_job({"config": config}, {"FISH_AUTH_PATH": "own-app-auth"}, lambda **_: None, client_factory=factory)
            self.assertEqual(factory_args, [("own-app-auth", "connection-one")])
            self.assertEqual(sum(args.get("dry_run") is False for _, args in client.calls), 1)
            with self.assertRaisesRegex(FishError, "不会自动再次合成"):
                voice.run_job({"config": config}, {}, lambda **_: None, resume=True,
                              client_factory=lambda *_: self.fail("Recovery must not contact TTS"))
            state = json.loads(TaskRecord.path_for(Path(config["output"])).read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "submission_unknown")

    def test_download_failure_keeps_result_and_resume_uses_only_url(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepared(directory)
            client = Client()
            with patch.object(voice, "_media_tool", return_value="ffmpeg"), \
                    patch.object(voice, "_download", side_effect=FishError("download interrupted")):
                result = voice.run_job({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client)
            self.assertFalse(result["local_downloaded"])
            state = json.loads(Path(result["task_state_file"]).read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "generated")
            self.assertEqual(state["audio_url"], "https://example.test/audio.wav")
            self.assertEqual(state["model"]["adapter"], "fish_speech")
            def recover(record, progress):
                Path(record.data["output"]).write_bytes(b"existing generated audio")
                return {**result, "output": record.data["output"], "local_downloaded": True}
            with patch.object(voice, "download_result", side_effect=recover):
                resumed = voice.run_job({"config": config}, {}, lambda **_: None, resume=True,
                                       client_factory=lambda *_: self.fail("Recovery must not authorize or generate"))
            self.assertTrue(resumed["local_downloaded"])
            again = voice.run_job({"config": config}, {}, lambda **_: None, resume=True)
            self.assertTrue(again["resumed_existing_task"])
            Path(config["output"]).write_bytes(b"user edit")
            with self.assertRaisesRegex(Exception, "已被修改"):
                voice.run_job({"config": config}, {}, lambda **_: None, resume=True)

    def test_simultaneous_preview_downloads_own_their_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "same-cache-key.mp3"
            barrier = threading.Barrier(2)
            paths, errors, results = [], [], []
            def download(url, path):
                paths.append(path)
                Path(path).write_bytes(b"same-audio")
                barrier.wait(timeout=3)
            def inspect(path):
                self.assertEqual(Path(path).read_bytes(), b"same-audio")
            def preview():
                try:
                    results.append(voice.download_sample("https://example.test/sample.mp3", target))
                except Exception as error:
                    errors.append(error)
            with patch.object(voice, "_download", side_effect=download), patch.object(voice, "_inspect_audio", side_effect=inspect):
                workers = [threading.Thread(target=preview) for _ in range(2)]
                for worker in workers: worker.start()
                for worker in workers: worker.join(timeout=5)
            self.assertFalse(errors)
            # Windows CI can expose TEMP via an 8.3 alias; the service resolves it.
            self.assertEqual(results, [target.resolve(), target.resolve()])
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(target.read_bytes(), b"same-audio")
            self.assertFalse(list(Path(directory).glob("*.part")))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg needed")
    def test_fish_audio_duration_and_samples_are_unchanged_by_local_save(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            config.update(speed=.9, volume_db=-3.)
            record = TaskRecord(Path(directory) / "record.json")
            record.data = {"output": config["output"], "speech_config": config,
                           "audio_url": "https://example.test/actual.wav"}
            original = sine_wav()
            with wave.open(io.BytesIO(original), "rb") as source:
                original_raw = source.readframes(source.getnframes())
            with patch.object(voice, "_download", side_effect=lambda url, path: Path(path).write_bytes(original)), \
                    patch.object(voice, "_run_media", wraps=voice._run_media) as commands:
                result = voice.download_result(record)
            self.assertTrue(result["local_downloaded"], result)
            with wave.open(config["output"], "rb") as generated:
                duration = generated.getnframes() / generated.getframerate()
                raw = generated.readframes(generated.getnframes())
            self.assertEqual(duration, 2.)
            self.assertEqual(raw, original_raw)
            self.assertEqual((result["speed"], result["volume_db"], result["audio_settings_mode"]), (.9, -3., "fish"))
            for call in commands.call_args_list:
                command = " ".join(str(arg) for arg in call.args[0])
                self.assertNotIn("atempo", command)
                self.assertNotIn("volume=", command)
            self.assertFalse(list(Path(directory).glob("*.part")))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg needed")
    def test_legacy_download_recovery_uses_original_audio_and_labels_actual_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepared(directory)
            with patch.object(voice, "_download", side_effect=FishError("interrupted")):
                incomplete = voice.run_job({"config": config}, {}, lambda **_: None, client_factory=lambda *_: Client())
            state_path = Path(incomplete["task_state_file"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            legacy_config = state["speech_config"]
            legacy_config.pop("audio_settings_mode")
            legacy_config.pop("volume_db")
            legacy_config.update(speed=.9, volume=80.)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with patch.object(voice, "_download", side_effect=lambda url, path: Path(path).write_bytes(sine_wav())):
                result = voice.run_job({"config": legacy_config}, {}, lambda **_: None, resume=True,
                                       client_factory=lambda *_: self.fail("Legacy recovery must never generate"))
            self.assertTrue(result["local_downloaded"])
            self.assertEqual((result["speed"], result["volume_db"], result["audio_settings_mode"]), (1., 0., "fish_original"))
            self.assertEqual((result["legacy_requested_speed"], result["legacy_requested_volume"]), (.9, 80.))
            self.assertEqual(result["duration"], 2.)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg needed")
    def test_all_returned_clips_are_saved_and_combined(self):
        urls = ["https://example.test/part-one.wav", "https://example.test/part-two.wav"]
        self.assertEqual(voice._audio_urls({"clips": [{"audio_url": value} for value in urls]}), urls)
        with self.assertRaises(FishError):
            voice._audio_urls({"clips": [{"audio_url": urls[0]}, {"error": "missing"}]})
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            record = TaskRecord(Path(directory) / "record.json")
            record.data = {"output": config["output"], "speech_config": config,
                           "audio_url": urls[0], "audio_urls": urls}
            with patch.object(voice, "_download", side_effect=lambda url, path: Path(path).write_bytes(sine_wav(seconds=1))) as fetch:
                result = voice.download_result(record)
            self.assertTrue(result["local_downloaded"], result)
            self.assertEqual(fetch.call_count, 2)
            self.assertAlmostEqual(result["duration"], 2., delta=.05)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg needed")
    def test_audio_inspection_rejects_video_and_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.wav"
            path.write_bytes(sine_wav(seconds=1))
            self.assertAlmostEqual(voice.inspect_clone_audio(path)["duration"], 1., delta=.01)
            path.write_bytes(b"not an audio file")
            with self.assertRaises(ValueError): voice.inspect_clone_audio(path)


if __name__ == "__main__":
    unittest.main()
