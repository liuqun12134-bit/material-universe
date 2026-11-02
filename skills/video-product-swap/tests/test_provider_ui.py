"""Headless desktop provider controls and currency regression checks."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from video_gui import MaterialUniverseApp


class Value:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class ProviderControlTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(MaterialUniverseApp)
        app.model_map = {"wan3.0-video": {"provider": "fal", "adapter": "fal_wan3"}, "dashscope-wan3.0-video": {"provider": "dashscope", "adapter": "dashscope_wan3"}, "dashscope-wan3.0-video-prime": {"provider": "dashscope", "adapter": "dashscope_wan3"}}
        app.route_labels = {key: key for key in app.model_map}
        app.provider_names = {"fal": "fal", "dashscope": "阿里云官方"}
        app.provider_keys = {name: key for key, name in app.provider_names.items()}
        app.runner = SimpleNamespace(registry=SimpleNamespace(providers={"fal": {"default_model": "wan3.0-video"}, "dashscope": {"default_model": "dashscope-wan3.0-video", "default_api_base": "https://dashscope.aliyuncs.com/api/v1"}}))
        app.model_var, app.video_route_var, app.provider_var = Value("wan3.0-video"), Value(""), Value("fal")
        app.model_help_var, app.swap_cost_var = Value(""), Value("")
        app.resolution_var, app.duration_var, app.source_video_var = Value("720p"), Value("5"), Value("source.mp4")
        app.provider_vars = {"dashscope": {"base_var": Value("https://dashscope.aliyuncs.com/api/v1")}}
        app.video_info = SimpleNamespace(duration=4)
        app.enable_thinking_var = Value(True)
        app.enable_prompt_expansion_var = Value(True)
        app.prompt_mode_var = Value("使用已有提示词")
        app._feature_provider = "fal"
        app._provider_feature_values = {}
        app.thinking_checkbox = Mock()
        app.thinking_checkbox.winfo_manager.return_value = ""
        app.model_box, app.resolution_box, app.full_button = Mock(), Mock(), Mock()
        return app

    def test_switch_official_filters_models_hides_thinking_and_cny_quotes(self):
        app = self.make_app()
        app._select_provider("阿里云官方")
        self.assertEqual(app.model_var.get(), "dashscope-wan3.0-video")
        self.assertEqual(app.provider_var.get(), "阿里云官方")
        self.assertTrue(app.enable_thinking_var.get())  # Hidden switch retains the fal preference.
        app.thinking_checkbox.pack_forget.assert_called_once()
        self.assertEqual(app.model_box.configure.call_args.kwargs["values"], ["dashscope-wan3.0-video", "dashscope-wan3.0-video-prime"])
        self.assertIn("¥5.40", app.swap_cost_var.get())
        self.assertNotIn("$", app.swap_cost_var.get())
        app._select_video_route("dashscope-wan3.0-video-prime")
        self.assertIn("¥8.10", app.swap_cost_var.get())

    def test_provider_roundtrip_restores_each_providers_explicit_switches(self):
        app = self.make_app()
        app.enable_thinking_var.set(False)
        app.enable_prompt_expansion_var.set(False)
        app._select_provider("阿里云官方")
        self.assertFalse(app.enable_thinking_var.get())
        app.enable_prompt_expansion_var.set(True)
        app._select_provider("fal")
        self.assertFalse(app.enable_thinking_var.get())
        self.assertFalse(app.enable_prompt_expansion_var.get())
        app._select_provider("阿里云官方")
        self.assertTrue(app.enable_prompt_expansion_var.get())
        self.assertFalse(app.enable_thinking_var.get())

    def test_unknown_region_and_invalid_restored_resolution(self):
        app = self.make_app()
        app.model_var.set("dashscope-wan3.0-video")
        app.resolution_var.set("4k")
        app._update_model_help()
        self.assertEqual(app.resolution_var.get(), "480p")
        self.assertIn("¥2.70", app.swap_cost_var.get())
        app.provider_vars["dashscope"]["base_var"].set("https://dashscope-intl.aliyuncs.com/api/v1")
        app._update_swap_cost()
        self.assertIn("地域", app.swap_cost_var.get())
        self.assertIn("可继续生成", app.swap_cost_var.get())

if __name__ == "__main__": unittest.main()
