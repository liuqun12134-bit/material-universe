from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from video_gui import EmbeddedVideoPlayer, MaterialUniverseApp, build_isolated_child_env, toggle_video_pause


class EmbeddedVideoPlayerTests(unittest.TestCase):
    @patch("video_gui.ctypes.WinDLL", create=True)
    @patch("video_gui.os.name", "nt")
    def test_embedded_player_receives_nonblocking_key_press_and_release(self, win_dll: Mock) -> None:
        user32 = win_dll.return_value
        user32.PostMessageW.return_value = True
        self.assertTrue(toggle_video_pause("embedded-player", 4321))
        user32.FindWindowW.assert_not_called()
        self.assertEqual(user32.PostMessageW.call_count, 2)
        down, up = user32.PostMessageW.call_args_list
        self.assertEqual(down.args[:3], (4321, 0x0100, ord("P")))
        self.assertEqual(up.args[:3], (4321, 0x0101, ord("P")))
        user32.SendMessageW.assert_not_called()

    def make_player(self) -> EmbeddedVideoPlayer:
        player = object.__new__(EmbeddedVideoPlayer)
        player.process = Mock(pid=1234)
        player.process.poll.return_value = None
        player.paused = False
        player.window_title = "test-player-1234"
        player.hwnd = 4321
        player.state_callback = Mock()
        return player

    @patch("video_gui.set_process_suspended")
    @patch("video_gui.toggle_video_pause", return_value=True)
    def test_pause_keeps_embedded_window_thread_running(self, toggle_pause: Mock, suspend: Mock) -> None:
        player = self.make_player()

        player.toggle()
        self.assertTrue(player.paused)
        toggle_pause.assert_called_once_with("test-player-1234", 4321)
        suspend.assert_not_called()
        player.state_callback.assert_called_once_with(True, True)

        toggle_pause.reset_mock()
        player.state_callback.reset_mock()
        player.toggle()
        self.assertFalse(player.paused)
        toggle_pause.assert_called_once_with("test-player-1234", 4321)
        suspend.assert_not_called()
        player.state_callback.assert_called_once_with(True, False)

    @patch("video_gui.toggle_video_pause", return_value=False)
    def test_toggle_keeps_state_when_player_is_not_ready(self, toggle_pause: Mock) -> None:
        player = self.make_player()

        player.toggle()

        self.assertFalse(player.paused)
        toggle_pause.assert_called_once_with("test-player-1234", 4321)
        player.state_callback.assert_not_called()


class ChildEnvironmentTests(unittest.TestCase):
    def test_kaiyuncode_uses_only_explicit_desktop_credentials(self) -> None:
        providers = {"kaiyuncode": {"api_key_env": "KAIYUNCODE_API_KEY",
                                   "api_base_env": "KAIYUNCODE_API_BASE"}}
        inherited = {"KAIYUNCODE_API_KEY": "unrelated-key",
                     "KAIYUNCODE_API_BASE": "https://unrelated.example"}
        explicit = {"KAIYUNCODE_API_KEY": "desktop-test-key",
                    "KAIYUNCODE_API_BASE": "https://kaiyuncode.com"}
        configured = build_isolated_child_env(inherited, explicit, providers)
        self.assertEqual(configured["KAIYUNCODE_API_KEY"], explicit["KAIYUNCODE_API_KEY"])
        self.assertEqual(configured["KAIYUNCODE_API_BASE"], explicit["KAIYUNCODE_API_BASE"])
        empty = build_isolated_child_env(inherited, {}, providers)
        self.assertEqual(empty["KAIYUNCODE_API_KEY"], "")
        self.assertEqual(empty["KAIYUNCODE_API_BASE"], "")

    def test_empty_app_keys_block_skill_and_legacy_env_fallbacks(self) -> None:
        parent = {
            "DEEPSEEK_API_KEY": "prompt-skill-key",
            "GEMINI_RELAY_API_KEY": "retired-prompt-key",
            "GEMINI_API_KEY": "prompt-legacy-key",
            "FAL_KEY": "video-skill-key",
            "KAIYUNCODE_API_KEY": "retired-video-key",
            "VIDEO_SWAP_API_KEY": "video-legacy-key",
            "VIDEO_SWAP_API_BASE": "https://legacy.example.com",
            "DASHSCOPE_API_KEY": "retired-official-key",
            "UNRELATED": "preserved",
        }
        app_values = {
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_API_BASE": "https://prompt.example.com",
            "FAL_KEY": "",
            "FAL_API_BASE": "https://queue.fal.run",
        }
        providers = {"fal": {}}

        result = build_isolated_child_env(parent, app_values, providers)

        self.assertEqual(result["DEEPSEEK_API_KEY"], "")
        self.assertEqual(result["GEMINI_RELAY_API_KEY"], "")
        self.assertEqual(result["GEMINI_API_KEY"], "")
        self.assertEqual(result["FAL_KEY"], "")
        self.assertEqual(result["KAIYUNCODE_API_KEY"], "")
        self.assertEqual(result["VIDEO_SWAP_API_KEY"], "")
        self.assertEqual(result["VIDEO_SWAP_API_BASE"], "")
        self.assertEqual(result["DASHSCOPE_API_KEY"], "")
        self.assertEqual(result["UNRELATED"], "preserved")

    def test_nonempty_app_keys_are_passed_without_skill_fallback(self) -> None:
        result = build_isolated_child_env(
            {"DEEPSEEK_API_KEY": "old", "FAL_KEY": "legacy"},
            {"DEEPSEEK_API_KEY": "app-prompt", "FAL_KEY": "app-video"},
            {"fal": {}},
        )

        self.assertEqual(result["DEEPSEEK_API_KEY"], "app-prompt")
        self.assertEqual(result["FAL_KEY"], "app-video")


class AppCredentialGateTests(unittest.TestCase):
    def make_app(
        self,
        prompt_key: str,
        video_key: str,
        model: str = "wan3.0-video",
        provider: str = "fal",
    ) -> MaterialUniverseApp:
        app = object.__new__(MaterialUniverseApp)
        app.root = Mock()
        app.prompt_key_var = Mock()
        app.prompt_key_var.get.return_value = prompt_key
        app.model_var = Mock()
        app.model_var.get.return_value = model
        app.model_map = {model: {"provider": provider}}
        app.route_labels = {"wan3.0-video": "fal · Wan3 视频参考（换品）"}
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
        self.assertIn("Wan3 视频参考", message)
        self.assertIn("不会自动使用 Skill 的 .env Key", message)

    @patch("video_gui.messagebox.showerror")
    def test_full_pipeline_accepts_app_owned_keys(self, showerror: Mock) -> None:
        app = self.make_app("app-prompt", "app-video")

        self.assertTrue(app._require_app_credentials(prompt=True, video=True))
        showerror.assert_not_called()

    @patch("video_gui.messagebox.showerror")
    def test_unregistered_route_falls_back_to_generic_label(self, showerror: Mock) -> None:
        app = self.make_app("app-prompt", "", model="future-model", provider="fal")

        self.assertFalse(app._require_app_credentials(video=True))
        self.assertIn("视频服务 API Key", showerror.call_args.args[1])


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
        app.root = Mock()
        app.editor_nav = Mock()
        app.image_audio_nav = Mock()
        app.image_audio_tab = Mock()
        app.voice_nav = Mock()
        app.voice_tab = Mock()
        app.api_nav = Mock()
        app.lipsync_nav = Mock()
        app.repair_nav = Mock()
        app.history_nav = Mock()
        app.library_nav = Mock()
        app.canvas_nav = Mock()
        app.canvas_tab = Mock()
        app.canvas_window = Mock()
        app.edit_tab = Mock()
        app.api_tab = Mock()
        app.lipsync_tab = Mock()
        app.repair_tab = Mock()
        app.history_tab = Mock()
        app.library_tab = Mock()

        app.demo = True
        app.fish_workspaces = {}
        app._show_page("image_audio")
        app.api_tab.grid_remove.assert_called_once_with()
        app.voice_tab.grid_remove.assert_called_once_with()
        app.image_audio_tab.grid.assert_called_once_with(row=0, column=0, sticky="nsew")
        app.image_audio_tab.tkraise.assert_called_once_with()
        self.assertEqual(app.current_page, "image_audio")
        app._show_page("image_audio")
        app.image_audio_tab.grid.assert_called_once_with(row=0, column=0, sticky="nsew")
        app._show_page("api")
        app.image_audio_tab.grid_remove.assert_called()
        self.assertEqual(app.current_page, "api")
        app._show_page("repair")
        self.assertEqual(app.current_page, "api")

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
        app.desktop_store = Mock()
        app.desktop_store.import_asset.return_value = {"path": str(path)}

        app._inspect_reference_image("token", path)

        kind, payload = app.events.get_nowait()
        self.assertEqual(kind, "reference_loaded")
        self.assertEqual(payload["token"], "token")
        self.assertEqual(payload["path"], path)
        self.assertIs(payload["thumbnail"], thumbnail)


if __name__ == "__main__":
    unittest.main()
