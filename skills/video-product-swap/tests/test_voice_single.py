"""Direct TTS must produce one history row without any Studio operation."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import voice_service as voice
from desktop_tasks import DesktopStore
from fish_mcp import FishError
from voice_batch import can_resume, latest_batch, resume_job, run_batch
from test_voice_service import Client


class SingleSpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "store")
        self.client = Client()
        self.config = {"text": "一条口播", "voice_id": "mine", "voice_name": "我的声音",
                       "speed": 1., "volume_db": 0., "output": str(self.root / "speech.wav")}
        self.media = patch.object(voice, "_media_tool", return_value="ffmpeg")
        self.media.start()
        self.download = patch.object(voice, "download_result", side_effect=self.finish)
        self.download.start()

    def tearDown(self):
        self.download.stop()
        self.media.stop()
        self.store.db.close()
        self.temp.cleanup()

    def finish(self, record, progress):
        output = Path(record.data["output"])
        output.write_bytes(b"original-audio")
        return {"output": str(output), "local_downloaded": True, "output_kind": "audio",
                "audio_url": record.data["audio_url"]}

    def prepare(self):
        config = voice.prepare_speech(self.config, self.client)
        config["cost_approved"] = True
        return config

    def run_direct(self, config, request_id="one"):
        # Exercise the default dispatch, not an injected fake runner.
        with patch.object(voice, "FishClient", return_value=self.client):
            # run_job's default is bound at import; wrap only its client factory.
            original = voice.run_job
            def run(job, credentials, update, resume=False):
                return original(job, credentials, update, resume, client_factory=lambda *_: self.client)
            with patch.object(voice, "run_job", side_effect=run):
                return run_batch(config, self.store, {}, request_id=request_id)

    def test_single_generation_explicit_drama3_one_record_and_no_studio(self):
        config = self.prepare()
        jobs = self.run_direct(config)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "success")
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(latest_batch(self.store, "connection-one"), jobs)
        paid = [args for name, args in self.client.calls if args.get("dry_run") is False]
        self.assertEqual(paid, [{"text": "一条口播", "voice_id": "mine", "model": "drama-3", "dry_run": False}])
        self.assertTrue(all(name == "text_to_speech" for name, _ in self.client.calls))
        self.assertNotIn("studio_project_id", jobs[0]["config"])
        self.assertNotIn("版本A", jobs[0]["config"]["output"])
        restored = DesktopStore(self.root / "restored")
        try:
            restored.import_record(jobs[0]["task_state_file"])
            self.assertEqual(len(latest_batch(restored, "connection-one")), 1)
        finally:
            restored.db.close()

    def test_duplicate_click_reuses_record_and_next_request_stays_single(self):
        config = self.prepare()
        jobs = self.run_direct(config)
        again = self.run_direct(config)
        self.assertEqual(jobs, again)
        self.assertEqual(sum(args.get("dry_run") is False for _, args in self.client.calls), 1)
        next_job = self.run_direct(config, request_id="two")
        self.assertEqual(len(next_job), 1)
        self.assertNotEqual(next_job[0]["config"]["output"], jobs[0]["config"]["output"])

    def test_cancel_or_changed_quote_does_not_create_rows_or_generate(self):
        config = self.prepare()
        for change in ({"cost_approved": False}, {"text": "文案变化"}, {"speed": .9}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_direct({**config, **change})
        self.assertEqual(self.store.jobs(), [])
        self.assertTrue(all(args.get("dry_run") for _, args in self.client.calls))

    def test_failed_download_resumes_only_the_saved_audio(self):
        config = self.prepare()
        with patch.object(voice, "download_result", side_effect=FishError("下载中断")):
            jobs = self.run_direct(config)
        self.assertEqual(jobs[0]["status"], "attention")
        self.assertTrue(can_resume(jobs[0]))
        calls = copy.deepcopy(self.client.calls)
        recovered = resume_job(jobs[0]["id"], self.store, {})
        self.assertEqual(recovered[0]["status"], "success")
        self.assertEqual(self.client.calls, calls)

    def test_lost_paid_response_is_never_replayed(self):
        config = self.prepare()
        def handler(name, args):
            if args.get("dry_run"):
                return {"dry_run": True, "estimated_credits": 6, "has_sufficient_balance": True}
            raise FishError("响应中断")
        self.client.handler = handler
        jobs = self.run_direct(config)
        self.assertFalse(can_resume(jobs[0]))
        self.run_direct(config)
        with self.assertRaisesRegex(ValueError, "没有可继续"):
            resume_job(jobs[0]["id"], self.store, {})
        self.assertEqual(sum(args.get("dry_run") is False for _, args in self.client.calls), 1)

    def test_new_quote_drops_old_studio_draft_and_approval(self):
        config = voice.prepare_speech({**self.config, "studio_project_id": "old", "studio_block": "old",
                                      "variants": [{}, {}], "cost_approved": True,
                                      "variant_backend": "studio"}, self.client)
        self.assertEqual(config["variant_backend"], "tts")
        self.assertNotIn("cost_approved", config)
        self.assertNotIn("variants", config)
        self.assertFalse(any(key.startswith("studio_") for key in config))

    def test_provider_quote_cannot_silently_switch_models(self):
        self.client.handler = lambda *_: {"dry_run": True, "estimated_credits": 6,
                                          "has_sufficient_balance": True, "model": "s2.1-pro"}
        with self.assertRaisesRegex(FishError, "报价模型"):
            self.prepare()
        self.assertTrue(all(args.get("dry_run") for _, args in self.client.calls))


if __name__ == "__main__":
    unittest.main()
