import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from desktop_tasks import DesktopStore, TaskManager, execute_job
from desktop_pages import DesktopPages
from history_sync import refresh


class VoiceTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "store")
        self.manager = None
        self.config = {"text": "你好，欢迎回来。", "voice_id": "voice-a", "voice_name": "我的声音",
                       "model": "drama-3", "speed": 1.2, "volume": 80,
                       "output": str(self.root / "speech.wav"), "cost_approved": True}

    def tearDown(self):
        if self.manager:
            self.manager.stop()
            self.assertTrue(self.manager.wait_idle(5))
            self.manager.owner.close()
        self.store.db.close()
        self.temp.cleanup()

    def record(self, status="generated", audio_url="https://media.fish.audio/a.wav"):
        state = {"version": 1, "model": {"provider": "fish_audio", "adapter": "fish_speech"},
                 "status": status, "output": self.config["output"], "speech_config": dict(self.config)}
        if audio_url:
            state["audio_url"] = audio_url
        path = Path(self.config["output"] + ".video-task.json")
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def app(self, job):
        app = object.__new__(DesktopPages)
        app.root = Mock()
        app._selected_job = Mock(return_value=job)
        app.history_primary = Mock()
        app.history_action_more = Mock()
        return app

    def test_shared_queue_uses_frozen_speech_draft_and_deduplicates(self):
        release = threading.Event()
        seen = []

        def execute(job, *args):
            seen.append(job)
            release.wait(3)
            return {"local_downloaded": True, "output": job["config"]["output"], "output_kind": "audio"}

        self.manager = TaskManager(self.store, execute=execute)
        identity = self.manager.submit("speech", self.config, {}, request_id="same-request")
        self.assertEqual(identity, self.manager.submit("speech", self.config, {}, request_id="same-request"))
        self.config["text"] = "已经修改"
        self.config["speed"] = 2
        release.set()
        self.assertTrue(self.manager.wait_idle(5))
        job = self.store.job(identity)
        self.assertEqual(len(seen), 1)
        self.assertEqual(job["name"], "你好，欢迎回来。")
        self.assertEqual(job["config"]["speed"], 1.2)
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["stage"], "音频已保存")
        self.assertEqual(Path(job["config"]["output"]).suffix, ".wav")

    def test_execute_routes_to_voice_backend_with_resume_flag(self):
        stub = types.ModuleType("voice_service")
        stub.run_job = Mock(return_value={"local_downloaded": True})
        job = {"kind": "speech", "config": self.config}
        update = Mock()
        with patch.dict(sys.modules, {"voice_service": stub}):
            self.assertEqual(execute_job(job, {"FISH_AUTH_PATH": "auth"}, update, self.store, True),
                             {"local_downloaded": True})
        stub.run_job.assert_called_once_with(job, {"FISH_AUTH_PATH": "auth"}, update, resume=True)

    def test_studio_take_routes_to_independent_backend_and_same_page_recovery(self):
        stub = types.ModuleType("voice_pair_service")
        stub.run_variant = Mock(return_value={"local_downloaded": True})
        job = {"id": "pair-one", "kind": "speech", "config": {**self.config, "variant_backend": "studio"}}
        update = Mock()
        with patch.dict(sys.modules, {"voice_pair_service": stub}):
            execute_job(job, {}, update, self.store, True)
        stub.run_variant.assert_called_once_with(job, {}, update, resume=True)
        app = self.app(job)
        app._show_page = Mock()
        app._voice_resume_job_id = Mock()
        app.tasks = Mock()
        app._recover_selected()
        app._show_page.assert_called_once_with("voice")
        app._voice_resume_job_id.assert_called_once_with("pair-one")
        app.tasks.recover.assert_not_called()

    def test_audio_record_import_and_download_recovery_do_not_regenerate(self):
        path = self.record()
        identity = self.store.import_record(path)
        job = self.store.job(identity)
        self.assertEqual(job["kind"], "speech")
        self.assertEqual(job["config"], self.config)
        execute = Mock(return_value={"local_downloaded": True})
        self.manager = TaskManager(self.store, execute=execute)
        self.manager.recover(identity, {})
        self.assertTrue(self.manager.wait_idle(5))
        self.assertTrue(execute.call_args.args[-1])

    def test_submission_without_audio_url_cannot_be_resubmitted_by_recovery(self):
        path = self.record(status="submission_unknown", audio_url=None)
        identity = self.store.import_record(path)
        execute = Mock()
        self.manager = TaskManager(self.store, execute=execute)
        with self.assertRaisesRegex(ValueError, "没有可恢复"):
            self.manager.recover(identity, {})
        execute.assert_not_called()

    def test_refresh_never_polls_synchronous_speech_even_if_record_has_task_id(self):
        path = self.record(status="submitting", audio_url=None)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["task_id"] = "not-a-video-job"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.store.import_record(path)
        self.manager = TaskManager(self.store)
        with patch("history_sync.fetch_status") as fetch:
            result = refresh(self.manager, {})
        self.assertEqual(result, {"synced": 0, "active": 0, "cooldown": 0, "errors": []})
        fetch.assert_not_called()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), state)

    def test_history_offers_download_for_saved_audio_and_playback_after_success(self):
        path = self.record()
        identity = self.store.import_record(path)
        app = self.app(self.store.job(identity))
        app._history_selection()
        self.assertEqual(app.history_primary.configure.call_args.kwargs["text"], "下载结果")
        output = Path(self.config["output"])
        output.write_bytes(b"generated audio")
        app._selected_job.return_value.update(status="success", result={"output": str(output)})
        app._open_task_result = Mock()
        app._history_selection()
        options = app.history_primary.configure.call_args.kwargs
        self.assertEqual(options["text"], "播放音频")
        options["command"]()
        app._open_task_result.assert_called_once_with(str(output))

    def test_reuse_opens_voice_draft_without_submitting(self):
        app = self.app({"kind": "speech", "config": self.config})
        app._voice_reuse = Mock()
        app._show_page = Mock()
        app.tasks = Mock()
        app._reuse_selected()
        app._voice_reuse.assert_called_once_with(self.config)
        app._show_page.assert_called_once_with("voice")
        app.tasks.submit.assert_not_called()

    def test_save_audio_copies_generated_file_and_cancel_does_nothing(self):
        output = Path(self.config["output"])
        output.write_bytes(b"generated audio")
        target = self.root / "saved.wav"
        app = self.app({})
        with patch("desktop_pages.filedialog.asksaveasfilename", return_value=str(target)):
            app._save_task_result(str(output))
        self.assertEqual(target.read_bytes(), output.read_bytes())
        with patch("desktop_pages.filedialog.asksaveasfilename", return_value=""), patch("desktop_pages.shutil.copy2") as copy:
            app._save_task_result(str(output))
        copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
