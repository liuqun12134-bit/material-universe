import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from desktop_tasks import DesktopStore
from voice_batch import can_resume, latest_batch, resume_job, run_batch


class VoiceBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "store")
        common = {"text": "同一段文案", "voice_id": "mine", "voice_name": "我的声音",
                  "model": "drama-3", "speed": 1.1, "volume_db": -1, "audio_settings_mode": "fish", "connection_id": "account-one",
                  "variant_backend": "studio", "studio_project_id": "project",
                  "output": str(self.root / "声音.wav"), "fish_quote": {"credits": 18}}
        self.config = {**common, "cost_approved": True, "fish_quote": {"credits": 36},
                       "variants": [{**common, "studio_block": "a"}, {**common, "studio_block": "b"}]}
        self.calls = []

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def successful(self, job, credentials, update, resume=False):
        self.calls.append((job["variant_index"], resume))
        output = Path(job["config"]["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"take-" + str(job["variant_index"]).encode())
        return {"output": str(output), "local_downloaded": True, "duration": 12,
                "output_kind": "audio", "audio_url": "https://example.test/" + job["id"] + ".wav"}

    def record(self, job, status="submission_unknown", audio_url=None):
        path = Path(job["config"]["output"] + ".video-task.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"status": status, "studio_project_id": "project", "studio_block": job["config"]["studio_block"]}
        if audio_url:
            state["audio_url"] = audio_url
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_two_variants_run_sequentially_and_publish_first_result_immediately(self):
        snapshots = []
        def execute(job, credentials, update, resume=False):
            if job["variant_index"] == 2:
                self.assertEqual(latest_batch(self.store)[0]["status"], "success")
                self.assertTrue(any(items[0]["status"] == "success" and items[1]["status"] == "queued"
                                    for items in snapshots))
            return self.successful(job, credentials, update, resume)
        jobs = run_batch(self.config, self.store, {}, snapshots.append, "two-takes", run_variant=execute)
        self.assertEqual(self.calls, [(1, False), (2, False)])
        self.assertEqual([job["status"] for job in jobs], ["success", "success"])
        self.assertNotEqual(jobs[0]["config"]["output"], jobs[1]["config"]["output"])
        self.assertEqual(jobs[0]["batch_id"], jobs[1]["batch_id"])
        self.assertEqual(self.config["variants"][0]["output"], str(self.root / "声音.wav"))

    def test_duplicate_request_returns_existing_pair_without_generating_again(self):
        jobs = run_batch(self.config, self.store, {}, request_id="same", run_variant=self.successful)
        again = run_batch(self.config, self.store, {}, request_id="same", run_variant=self.successful)
        self.assertEqual([j["id"] for j in jobs], [j["id"] for j in again])
        self.assertEqual(len(self.calls), 2)
        changed = copy.deepcopy(self.config)
        changed["variants"][0]["text"] = "改过"
        with self.assertRaisesRegex(ValueError, "改变"):
            run_batch(changed, self.store, {}, request_id="same", run_variant=self.successful)
        self.assertEqual(len(self.store.jobs()), 2)

    def test_no_rows_or_requests_before_approval_and_invalid_pair_rolls_back(self):
        execute = Mock()
        with self.assertRaisesRegex(ValueError, "确认"):
            run_batch({**self.config, "cost_approved": False}, self.store, {}, run_variant=execute)
        invalid = copy.deepcopy(self.config)
        invalid["variants"][1]["connection_id"] = "different"
        with self.assertRaisesRegex(ValueError, "不一致"):
            run_batch(invalid, self.store, {}, run_variant=execute)
        self.assertEqual(self.store.jobs(), [])
        execute.assert_not_called()
        for changed in ({**self.config, "fish_quote": {"credits": 18}},
                        {**self.config, "text": "未确认的新文案"},
                        {**self.config, "volume_db": -3}, {**self.config, "speed": .9}):
            with self.assertRaises(ValueError):
                run_batch(changed, self.store, {}, run_variant=execute)
        self.assertEqual(self.store.jobs(), [])

    def test_partial_failure_keeps_first_and_continuation_only_recovers_second(self):
        def execute(job, credentials, update, resume=False):
            if job["variant_index"] == 1:
                return self.successful(job, credentials, update, resume)
            path = self.record(job)
            update(task_state_file=str(path))
            raise RuntimeError("响应尚未完成")
        jobs = run_batch(self.config, self.store, {}, request_id="partial", run_variant=execute)
        self.assertEqual([job["status"] for job in jobs], ["success", "attention"])
        self.assertTrue(Path(jobs[0]["result"]["output"]).is_file())
        self.assertTrue(can_resume(jobs[1]))
        resumed = resume_job(jobs[1]["id"], self.store, {}, run_variant=self.successful)
        self.assertEqual(self.calls, [(1, False), (2, True)])
        self.assertEqual([job["status"] for job in resumed], ["success", "success"])
        resume_job(jobs[1]["id"], self.store, {}, run_variant=Mock(side_effect=AssertionError("must not generate")))

    def test_unsubmitted_take_cannot_resume_even_with_prepared_studio_identifiers(self):
        def execute(job, credentials, update, resume=False):
            self.record(job, status="not_submitted")
            raise RuntimeError("quote changed")
        jobs = run_batch(self.config, self.store, {}, run_variant=execute)
        self.assertEqual([job["status"] for job in jobs], ["failed", "failed"])
        for job in jobs:
            self.assertFalse(can_resume(job))
            with self.assertRaisesRegex(ValueError, "没有可继续"):
                resume_job(job["id"], self.store, {}, run_variant=Mock(side_effect=AssertionError("must not submit")))

    def test_second_take_failure_does_not_hide_successful_result(self):
        def execute(job, credentials, update, resume=False):
            if job["variant_index"] == 2:
                raise RuntimeError("配额不足")
            return self.successful(job, credentials, update, resume)
        jobs = run_batch(self.config, self.store, {}, request_id="budget", run_variant=execute)
        self.assertEqual([job["status"] for job in jobs], ["success", "failed"])
        self.assertEqual(latest_batch(self.store, "account-one"), jobs)
        self.assertEqual(latest_batch(self.store, "another-account"), [])
        self.assertEqual(run_batch(self.config, self.store, {}, request_id="budget", run_variant=execute), jobs)
        self.assertEqual(self.calls, [(1, False)])

    def test_stop_between_takes_prevents_second_submission(self):
        stop = threading.Event()
        def execute(job, credentials, update, resume=False):
            result = self.successful(job, credentials, update, resume)
            stop.set()
            return result
        jobs = run_batch(self.config, self.store, {}, request_id="stop", run_variant=execute,
                         stop_requested=stop.is_set)
        self.assertEqual(self.calls, [(1, False)])
        self.assertEqual([job["status"] for job in jobs], ["success", "paused"])
        self.assertFalse(can_resume(jobs[1]))
        run_batch(self.config, self.store, {}, request_id="stop", run_variant=execute)
        self.assertEqual(self.calls, [(1, False)])

    def test_credentials_redacted_from_progress_and_errors(self):
        def execute(job, credentials, update, resume=False):
            update(stage="error secret-token")
            raise RuntimeError("secret-token")
        jobs = run_batch(self.config, self.store, {"ACCESS_TOKEN": "secret-token"}, run_variant=execute)
        self.assertNotIn("secret-token", json.dumps(jobs))

    def test_real_pair_backend_contract_from_quote_to_two_persisted_results(self):
        import voice_pair_service as pair
        from test_voice_pair_service import StudioClient, finish_download
        client = StudioClient()
        form = {key: self.config[key] for key in ("text", "voice_id", "voice_name", "model", "speed", "volume_db", "output")}
        with patch.object(pair, "_media_tool", return_value="ffmpeg"), \
                patch.object(pair, "download_result", side_effect=finish_download):
            prepared = pair.prepare_pair(form, client)
            prepared["cost_approved"] = True
            def execute(job, credentials, update, resume=False):
                return pair.run_variant(job, credentials, update, resume=resume,
                                        client_factory=lambda *_: client, max_wait=0)
            jobs = run_batch(prepared, self.store, {}, request_id="integrated", run_variant=execute)
        self.assertEqual([job["status"] for job in jobs], ["success", "success"])
        self.assertEqual(len(client.paid), 2)
        self.assertEqual([args["blocks"] for args in client.paid], [["1"], ["2"]])
        self.assertNotEqual(jobs[0]["result"]["audio_url"], jobs[1]["result"]["audio_url"])
        self.assertTrue(all(Path(job["task_state_file"]).is_file() for job in jobs))
        restored = DesktopStore(self.root / "restored-store")
        try:
            for job in jobs:
                restored.import_record(job["task_state_file"])
            recovered = latest_batch(restored, "app-connection")
            self.assertEqual([job["variant_index"] for job in recovered], [1, 2])
            self.assertEqual([job["status"] for job in recovered], ["success", "success"])
        finally:
            restored.db.close()


if __name__ == "__main__":
    unittest.main()
