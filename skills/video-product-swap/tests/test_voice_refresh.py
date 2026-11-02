"""An account check completing on the voice page should start its initial load."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from image_audio_page import ImageAudioPage


class VoiceRefreshTests(unittest.TestCase):
    def app(self, page="voice"):
        return SimpleNamespace(
            current_page=page, fish_login_busy=True,
            fish_connect_button=Mock(), fish_account_status=Mock(),
            fish_workspace_menu=Mock(), fish_workspace=Mock(get=Mock(return_value="")),
            _saved_fish_workspace=Mock(return_value="workspace-one"),
            _fish_workspace_changed=Mock(), _voice_on_show=Mock(),
        )

    def test_successful_account_check_loads_visible_voice_page(self):
        app = self.app()
        result = {"workspaces": [{"workspace_id": "workspace-one", "workspace_name": "Default",
                                  "team_name": "Team", "is_default": True}]}
        ImageAudioPage._handle_fish_event(app, ("account", result, None))
        self.assertFalse(app.fish_login_busy)
        app._fish_workspace_changed.assert_called_once()
        app._voice_on_show.assert_called_once()

    def test_failed_or_empty_account_does_not_retry_voice_loading(self):
        for result, error in ((None, "授权检查失败"), ({"workspaces": []}, None)):
            with self.subTest(error=error):
                app = self.app()
                ImageAudioPage._handle_fish_event(app, ("account", result, error))
                self.assertFalse(app.fish_login_busy)
                app._voice_on_show.assert_not_called()

    def test_account_check_does_not_load_hidden_voice_page(self):
        app = self.app("image_audio")
        result = {"workspaces": [{"workspace_id": "workspace-one", "workspace_name": "Default",
                                  "team_name": "Team"}]}
        ImageAudioPage._handle_fish_event(app, ("account", result, None))
        app._voice_on_show.assert_not_called()


if __name__ == "__main__":
    unittest.main()
