"""Submission acknowledgement releases the form while preserving task identity."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from video_gui import MaterialUniverseApp


class SubmissionDialogTests(unittest.TestCase):
    def app(self):
        app = object.__new__(MaterialUniverseApp)
        app.root = Mock()
        app.tasks = Mock()
        app.tasks.submit.return_value = "persisted-task"
        app.prompt_model_var = Mock()
        app.prompt_model_var.get.return_value = "vision-model"
        app.status_var = Mock()
        app._submission_drafts = {kind: app._new_submission_draft() for kind in ("full", "lipsync", "repair")}
        app._submission_buttons = {kind: Mock() for kind in app._submission_drafts}
        app._submission_status = {kind: Mock() for kind in app._submission_drafts}
        app._set_submission_progress = Mock()
        app._refresh_history = Mock()
        app._credential_values = Mock(return_value={})
        return app

    def test_confirm_releases_each_module_and_old_events_cannot_overwrite_form(self):
        for kind in ("full", "lipsync", "repair"):
            with self.subTest(kind=kind):
                app = self.app()
                old = app._submission_drafts[kind]
                def acknowledge(*args, **kwargs):
                    self.assertEqual(old["job_id"], "persisted-task")
                    self.assertTrue(old["lock"].locked())
                    self.assertTrue(app._handle_existing_submission(kind))
                    app._start_worker(kind, {"prompt": "reentrant click"})
                with patch("video_gui.messagebox.showinfo", side_effect=acknowledge) as dialog:
                    app._start_worker(kind, {"prompt": "original"})
                dialog.assert_called_once()
                new = app._submission_drafts[kind]
                self.assertNotEqual(new["request_id"], old["request_id"])
                self.assertIsNone(new["job_id"])
                self.assertEqual(old["config"]["prompt"], "original")
                app.tasks.submit.assert_called_once()
                self.assertTrue(app._handle_existing_submission(kind))  # Double-click guard.
                app._submission_buttons[kind].reset_mock()
                app._refresh_submission_feedback()
                app._submission_buttons[kind].configure.assert_not_called()
                new["not_before"] = 0
                self.assertFalse(app._handle_existing_submission(kind))
                with patch("video_gui.messagebox.showinfo"):
                    app._start_worker(kind, {"prompt": "second"})
                self.assertEqual(app.tasks.submit.call_count, 2)
                self.assertNotEqual(app.tasks.submit.call_args_list[0].kwargs["request_id"],
                                    app.tasks.submit.call_args_list[1].kwargs["request_id"])

    def test_uncertain_local_submit_retains_frozen_request_and_retry(self):
        app = self.app()
        old = app._submission_drafts["full"]
        app.tasks.submit.side_effect = OSError("lost response")
        with patch("video_gui.messagebox.showerror"), patch("video_gui.messagebox.showinfo") as dialog:
            app._start_worker("full", {"prompt": "original"})
        dialog.assert_not_called()
        self.assertIs(app._submission_drafts["full"], old)
        app.tasks.submit.side_effect = None
        with patch("video_gui.messagebox.showinfo"):
            self.assertTrue(app._handle_existing_submission("full"))
        self.assertEqual(app.tasks.submit.call_args.kwargs["request_id"], old["request_id"])
        self.assertEqual(app.tasks.submit.call_args.args[1]["prompt"], "original")

    def test_official_submission_ignores_hidden_thinking_but_preserves_fal_choice(self):
        app = self.app()
        app._handle_existing_submission = Mock(return_value=False)
        app._require_app_credentials = Mock(return_value=True)
        app.prompt_mode_var = Mock()
        app.prompt_mode_var.get.return_value = "使用已有提示词"
        app.source_video_var = Mock()
        app.source_video_var.get.return_value = ""
        app._validate_common = Mock(return_value={"provider": "dashscope", "model": "dashscope-wan3.0-video"})
        app.reuse_prompt_box = Mock()
        app.reuse_prompt_box.get.return_value = "original prompt"
        app.extra_reference_images = []
        app.remove_subtitles_var = Mock()
        app.remove_subtitles_var.get.return_value = False
        app.enable_prompt_expansion_var = Mock()
        app.enable_prompt_expansion_var.get.return_value = False
        app.enable_thinking_var = Mock()
        app.enable_thinking_var.get.return_value = True
        app._start_worker = Mock()
        app._start_full_pipeline()
        self.assertEqual(app._start_worker.call_args.args[1]["provider"], "dashscope")
        self.assertFalse(app._start_worker.call_args.args[1]["enable_thinking"])
        app.enable_thinking_var.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
