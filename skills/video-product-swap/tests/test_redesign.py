import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from desktop_tasks import DesktopStore, TaskManager
from feature_policy import require_creation
from director_prompt import generate, transcribe, read_rule
from canvas_catalog import GENERAL_MODELS, MODEL_FAMILIES
from history_sync import refresh


class RedesignTests(unittest.TestCase):
    def test_retired_routes_not_offered_or_submitted(self):
        self.assertFalse({"sync3", "omni-edit-kaiyuncode"} & {f["id"] for f in MODEL_FAMILIES})
        for kind, config in [("full", {}), ("repair", {}), ("lipsync", {}), ("general", {"model": "kaiyuncode-omni_video_edit"})]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                require_creation(kind, config)
        require_creation("image_audio", {"model": "seedance-2.5"})

    def test_explicit_mode_required_before_network(self):
        with patch("director_prompt.requests.post") as post, patch("director_prompt.transcribe") as stt:
            for mode in (None, "", "请选择导演模式"):
                with self.assertRaisesRegex(ValueError, "先选择"):
                    generate({"mode": mode}, {}, None, None, lambda _: None)
            post.assert_not_called()
            stt.assert_not_called()

    def test_invalid_kaiyun_config_does_not_bill_transcription(self):
        with patch("director_prompt.transcribe") as stt:
            with self.assertRaisesRegex(ValueError, "Kaiyun"):
                generate({"mode": "纯口播"}, {}, None, None, lambda _: None)
            stt.assert_not_called()

    def test_fish_transcription_cached_and_never_retried(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock()
        client.call.side_effect = [{"upload_url": "https://upload.fish.audio/file", "file_url": "https://fish.audio/audio.wav"}, {"text": "今天展示这款面霜。"}]
        with tempfile.TemporaryDirectory() as folder, patch("director_prompt.put_media") as upload:
            info = {"sha256": "abc123", "path": str(Path(folder) / "audio.wav")}
            text = transcribe(info, None, Path(folder) / "cache", lambda _: None, lambda _: client)
            again = transcribe(info, None, Path(folder) / "cache", lambda _: None, lambda _: client)
            self.assertEqual(text, again)
            self.assertEqual(client.call.call_count, 2)
            self.assertEqual(client.call.call_args.args[0], "speech_to_text")
            self.assertEqual(client.call.call_args.args[1]["model"], "flash")
            upload.assert_called_once()

    def test_missing_transcript_is_not_cached(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client); client.__exit__ = Mock()
        client.call.side_effect = [{"upload_url": "https://x", "file_url": "https://y"}, {}]
        with tempfile.TemporaryDirectory() as folder, patch("director_prompt.put_media"):
            with self.assertRaises(ValueError):
                transcribe({"sha256": "hash", "path": "audio.wav"}, None, folder, lambda _: None, lambda _: client)
            self.assertFalse(list(Path(folder).glob("*.json")))
            self.assertEqual(client.call.call_count, 2)

    def test_portable_rule_package_and_path_boundary(self):
        self.assertIn("纯口播", read_rule("SKILL.md"))
        self.assertTrue(read_rule("references/mode-use.md"))
        with self.assertRaises(ValueError):
            read_rule("../scripts/video_gui.py")

    def test_refresh_queries_only_and_preserves_remote_id(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DesktopStore(Path(folder) / "store")
            manager = TaskManager(store)
            try:
                path = Path(folder) / "video.mp4.video-task.json"
                state = {"version": 1, "status": "submitted", "task_id": "original-id", "output": str(Path(folder) / "video.mp4"),
                         "model": {"provider": "fish_audio"}}
                path.write_text(json.dumps(state))
                job = {"id": "task", "kind": "image_audio", "name": "示例", "status": "attention", "created": 1,
                       "config": {"output": state["output"]}, "task_state_file": str(path)}
                with store.lock, store.db:
                    store.db.execute("INSERT INTO jobs VALUES (?, ?)", ("task", json.dumps(job)))
                with patch("history_sync.fetch_status", return_value=("running", None, None, 60)) as fetch:
                    counts = refresh(manager, {})
                    self.assertEqual(counts["synced"], 1)
                    self.assertEqual(refresh(manager, {})["cooldown"], 1)
                    fetch.assert_called_once()
                self.assertEqual(json.loads(path.read_text())["task_id"], "original-id")
                self.assertFalse(manager.pending)
                self.assertFalse(manager.active)
                self.assertFalse(Path(state["output"]).exists())
                with patch("history_sync.fetch_status") as fetch:
                    manager.active.add("task")
                    self.assertEqual(refresh(manager, {})["active"], 1)
                    fetch.assert_not_called()
                    manager.active.clear()
            finally:
                manager.stop(); store.db.close()


if __name__ == "__main__": unittest.main()
