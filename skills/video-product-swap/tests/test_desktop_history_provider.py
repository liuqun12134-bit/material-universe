import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from video_gui import MaterialUniverseApp


class Value:
    def __init__(self, value):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class HistoryProviderTests(unittest.TestCase):
    def app(self, current, saved, provider):
        app = object.__new__(MaterialUniverseApp)
        app.root = Mock()
        app.model_map = {"wan3.0-video": {"provider": "fal", "adapter": "fal_wan3"},
                         "dashscope-wan3.0-video-prime": {"provider": "dashscope", "adapter": "dashscope_wan3"}}
        app.route_labels = {model: model for model in app.model_map}
        app.model_var = Value(current)
        config = {"source": "source.mp4", "image": "image.png", "model": saved, "provider": provider,
                  "enable_prompt_expansion": False, "enable_thinking": False, "base_prompt": "exact history prompt"}
        app._selected_job = Mock(return_value={"kind": "full", "config": config})
        app._prepare_next_submission = Mock(return_value=True)
        app._update_model_help = Mock(side_effect=lambda: self.assertEqual(app.model_var.get(), saved))
        for method in ("_load_source_video", "_load_reference_image", "_show_page"):
            setattr(app, method, Mock())
        for name in ("relation_text", "reuse_prompt_box", "status_var"):
            setattr(app, name, Mock())
        for name in ("remove_subtitles_var", "enable_prompt_expansion_var", "enable_thinking_var", "prompt_mode_var", "resolution_var"):
            setattr(app, name, Value(True))
        return app

    def test_retired_history_cannot_restore_as_new_paid_draft(self):
        for current, saved, provider in (("wan3.0-video", "dashscope-wan3.0-video-prime", "dashscope"),
                                         ("dashscope-wan3.0-video-prime", "wan3.0-video", "fal")):
            with self.subTest(saved=saved), patch("desktop_pages.messagebox.showinfo") as info:
                app = self.app(current, saved, provider)
                app._reuse_selected()
                self.assertEqual(app.model_var.get(), current)
                app._update_model_help.assert_not_called()
                app._prepare_next_submission.assert_not_called()
                info.assert_called_once()

    def test_unavailable_or_mismatched_history_model_cannot_keep_current_paid_route(self):
        for model, provider in (("retired-model", "dashscope"), ("dashscope-wan3.0-video-prime", "fal")):
            with self.subTest(model=model), patch("desktop_pages.messagebox.showinfo"), patch("desktop_pages.messagebox.showerror"):
                app = self.app("wan3.0-video", model, provider)
                app._reuse_selected()
                app._prepare_next_submission.assert_not_called()
                app._load_source_video.assert_not_called()
                self.assertEqual(app.model_var.get(), "wan3.0-video")


if __name__ == "__main__":
    unittest.main()
