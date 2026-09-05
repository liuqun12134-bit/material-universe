from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from video_gui import EmbeddedVideoPlayer, MaterialUniverseApp, build_isolated_child_env


class EmbeddedVideoPlayerTests(unittest.TestCase):
    def make_player(self) -> EmbeddedVideoPlayer:
        player = object.__new__(EmbeddedVideoPlayer)
        player.process = Mock(pid=1234)
        player.process.poll.return_value = None
        player.paused = False
        player.state_callback = Mock()
        return player

    @patch("video_gui.set_process_suspended", return_value=True)
    def test_toggle_suspends_and_resumes_process(self, set_suspended: Mock) -> None:
        player = self.make_player()

        player.toggle()
        self.assertTrue(player.paused)
        set_suspended.assert_called_once_with(1234, True)
        player.state_callback.assert_called_once_with(True, True)

        set_suspended.reset_mock()
        player.state_callback.reset_mock()
        player.toggle()
        self.assertFalse(player.paused)
        set_suspended.assert_called_once_with(1234, False)
        player.state_callback.assert_called_once_with(True, False)

    @patch("video_gui.set_process_suspended", return_value=False)
    def test_toggle_keeps_state_when_windows_rejects_request(self, set_suspended: Mock) -> None:
        player = self.make_player()

        player.toggle()

        self.assertFalse(player.paused)
        set_suspended.assert_called_once_with(1234, True)
        player.state_callback.assert_not_called()


class ChildEnvironmentTests(unittest.TestCase):
    def test_empty_app_keys_block_skill_and_legacy_env_fallbacks(self) -> None:
        parent = {
            "DEEPSEEK_API_KEY": "prompt-skill-key",
            "GEMINI_RELAY_API_KEY": "retired-prompt-key",
            "GEMINI_API_KEY": "prompt-legacy-key",
            "KAIYUNCODE_API_KEY": "video-skill-key",
            "VIDEO_SWAP_API_KEY": "video-legacy-key",
            "VIDEO_SWAP_API_BASE": "https://legacy.example.com",
            "UNRELATED": "preserved",
        }
        app_values = {
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_API_BASE": "https://prompt.example.com",
            "KAIYUNCODE_API_KEY": "",
            "KAIYUNCODE_API_BASE": "https://video.example.com",
        }
        providers = {
            "kaiyuncode": {
                "legacy_api_base_env": "VIDEO_SWAP_API_BASE",
                "legacy_api_key_env": "VIDEO_SWAP_API_KEY",
            }
        }

        result = build_isolated_child_env(parent, app_values, providers)

        self.assertEqual(result["DEEPSEEK_API_KEY"], "")
        self.assertEqual(result["GEMINI_RELAY_API_KEY"], "")
        self.assertEqual(result["GEMINI_API_KEY"], "")
        self.assertEqual(result["KAIYUNCODE_API_KEY"], "")
        self.assertEqual(result["VIDEO_SWAP_API_KEY"], "")
        self.assertEqual(result["VIDEO_SWAP_API_BASE"], "")
        self.assertEqual(result["UNRELATED"], "preserved")

    def test_nonempty_app_keys_are_passed_without_skill_fallback(self) -> None:
        result = build_isolated_child_env(
            {"DEEPSEEK_API_KEY": "old", "VIDEO_SWAP_API_KEY": "legacy"},
            {"DEEPSEEK_API_KEY": "app-prompt", "KAIYUNCODE_API_KEY": "app-video"},
            {"kaiyuncode": {"legacy_api_key_env": "VIDEO_SWAP_API_KEY"}},
        )

        self.assertEqual(result["DEEPSEEK_API_KEY"], "app-prompt")
        self.assertEqual(result["KAIYUNCODE_API_KEY"], "app-video")
        self.assertEqual(result["VIDEO_SWAP_API_KEY"], "")


class AppCredentialGateTests(unittest.TestCase):
    def make_app(
        self,
        prompt_key: str,
        video_key: str,
        model: str = "wan3.0-video",
        provider: str = "kaiyuncode",
    ) -> MaterialUniverseApp:
        app = object.__new__(MaterialUniverseApp)
        app.root = Mock()
        app.prompt_key_var = Mock()
        app.prompt_key_var.get.return_value = prompt_key
        app.model_var = Mock()
        app.model_var.get.return_value = model
        app.model_map = {model: {"provider": provider}}
        video_key_var = Mock()
        video_key_var.get.return_value = video_key
        app.provider_vars = {provider: {"key_var": video_key_var}}
        return app

    @patch("video_gui.messagebox.showerror")
    def test_full_pipeline_is_blocked_when_app_keys_are_empty(self, showerror: Mock) -> None:
        app = self.make_app("", "")

        allowed = app._require_app_credentials(prompt=True, video=True)

        self.assertFalse(allowed)
        message = showerror.call_args.args[1]
        self.assertIn("提示词分析 API Key", message)
        self.assertIn("Wan3 视频参考 API Key", message)
        self.assertIn("不会自动使用 Skill 的 .env Key", message)

    @patch("video_gui.messagebox.showerror")
    def test_full_pipeline_accepts_app_owned_keys(self, showerror: Mock) -> None:
        app = self.make_app("app-prompt", "app-video")

        self.assertTrue(app._require_app_credentials(prompt=True, video=True))
        showerror.assert_not_called()

    @patch("video_gui.messagebox.showerror")
    def test_official_route_checks_dashscope_key(self, showerror: Mock) -> None:
        app = self.make_app(
            "app-prompt",
            "",
            model="wan-official-videoedit",
            provider="dashscope",
        )

        self.assertFalse(app._require_app_credentials(video=True))
        self.assertIn("Wan 官方", showerror.call_args.args[1])


class ProductCredentialImportTests(unittest.TestCase):
    @patch("video_gui.messagebox.showinfo")
    @patch("video_gui.read_env_values")
    def test_import_reads_only_product_owned_env(
        self,
        read_env_values: Mock,
        showinfo: Mock,
    ) -> None:
        read_env_values.return_value = {
            "DEEPSEEK_API_KEY": "product-key",
            "DEEPSEEK_API_BASE": "https://product.example.com",
            "DEEPSEEK_PROMPT_MODEL": "product-model",
        }
        app = object.__new__(MaterialUniverseApp)
        app.root = Mock()
        app.prompt_skill_root = Path("D:/other-prompt-skill")
        app.prompt_key_var = Mock()
        app.prompt_base_var = Mock()
        app.prompt_model_var = Mock()
        app.provider_vars = {}
        app.runner = Mock()
        app.runner.registry.providers = {}
        app._credential_values = Mock(return_value={})
        app._update_api_status = Mock()

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "system-key"}, clear=False):
            app._import_existing()

        read_env_values.assert_called_once_with(SKILL_ROOT / ".env")
        app.prompt_key_var.set.assert_called_once_with("product-key")
        app.prompt_base_var.set.assert_called_once_with("https://product.example.com")
        app.prompt_model_var.set.assert_called_once_with("product-model")
        app._update_api_status.assert_called_once_with()
        showinfo.assert_called_once()


class GuiResponsivenessTests(unittest.TestCase):
    def test_page_switch_unmaps_the_hidden_page(self) -> None:
        app = object.__new__(MaterialUniverseApp)
        app.current_page = None
        app.editor_nav = Mock()
        app.api_nav = Mock()
        app.edit_tab = Mock()
        app.api_tab = Mock()

        app._show_page("editor")

        app.api_tab.grid_remove.assert_called_once_with()
        app.edit_tab.grid.assert_called_once_with()
        app.edit_tab.tkraise.assert_called_once_with()
        self.assertEqual(app.current_page, "editor")

        app._show_page("editor")
        app.edit_tab.grid.assert_called_once_with()

    @patch("video_gui.threading.Thread")
    def test_source_inspection_starts_in_background(self, thread_class: Mock) -> None:
        app = object.__new__(MaterialUniverseApp)
        app.source_video_var = Mock()
        app.status_var = Mock()
        app.source_button = Mock()
        app.events = queue.Queue()

        app._load_source_video(Path("source.mp4"))

        app.source_video_var.set.assert_called_once_with("")
        app.source_button.configure.assert_called_once_with(state="disabled")
        thread_class.assert_called_once()
        self.assertTrue(thread_class.call_args.kwargs["daemon"])
        thread_class.return_value.start.assert_called_once_with()

    @patch("video_gui.image_thumbnail")
    def test_reference_worker_returns_through_event_queue(self, image_thumbnail: Mock) -> None:
        thumbnail = Mock()
        image_thumbnail.return_value = thumbnail
        app = object.__new__(MaterialUniverseApp)
        app.events = queue.Queue()
        path = Path("reference.png")

        app._inspect_reference_image("token", path)

        kind, payload = app.events.get_nowait()
        self.assertEqual(kind, "reference_loaded")
        self.assertEqual(payload["token"], "token")
        self.assertEqual(payload["path"], path)
        self.assertIs(payload["thumbnail"], thumbnail)


if __name__ == "__main__":
    unittest.main()
