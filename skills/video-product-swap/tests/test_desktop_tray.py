import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from desktop_tray import DesktopTray
from video_gui import MaterialUniverseApp, LOGO_ICO


class TraySafetyTests(unittest.TestCase):
    def app(self):
        app = object.__new__(MaterialUniverseApp)
        app.root = Mock()
        app.events = queue.Queue()
        app.status_var = Mock()
        app.tray = None
        app._closed = False
        app._exit_in_progress = False
        app._tray_hide_pending = False
        return app

    def test_close_waits_for_confirmed_ready(self):
        app = self.app()
        tray = Mock(is_ready=False)
        with patch("video_gui.DesktopTray", return_value=tray):
            app._on_close()
        tray.start.assert_called_once()
        app.root.withdraw.assert_not_called()
        self.assertTrue(app._tray_hide_pending)
        tray.is_ready = True
        app.events.put(("tray_ready", tray))
        app._drain_events()
        app.root.withdraw.assert_called_once()

    def test_failed_attempt_keeps_window_and_late_events_cannot_hide(self):
        app = self.app()
        old, new = Mock(is_ready=False), Mock(is_ready=False)
        app.tray = old
        with patch("video_gui.messagebox.askyesno", return_value=False):
            app._tray_start_timeout(old)
        old.stop.assert_called_once()
        app.root.deiconify.assert_called_once()
        app.root.withdraw.assert_not_called()
        self.assertIsNone(app.tray)
        with patch("video_gui.DesktopTray", return_value=new):
            app._on_close()
        app.events.put(("tray_ready", old))
        app.events.put(("tray_failed", (old, "late failure")))
        with patch("video_gui.messagebox.askyesno") as dialog:
            app._drain_events()
            dialog.assert_not_called()
        self.assertIs(app.tray, new)
        app.root.withdraw.assert_not_called()

    def test_failure_dialog_can_exit_using_guarded_path(self):
        app = self.app()
        app.tray = tray = Mock()
        app._request_exit = Mock()
        with patch("video_gui.messagebox.askyesno", return_value=True):
            app._tray_failed(tray, "failure")
        app._request_exit.assert_called_once()

    def test_thread_start_failure_leaves_window_available(self):
        app = self.app()
        with patch("desktop_tray.threading.Thread.start", side_effect=RuntimeError("thread unavailable")), patch("video_gui.messagebox.askyesno", return_value=False):
            app._on_close()
        self.assertIsNone(app.tray)
        app.root.deiconify.assert_called_once()
        app.root.withdraw.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows shell result")
    def test_shell_rejecting_nim_add_cannot_mark_ready(self):
        from PIL import Image
        from pystray._util import win32
        tray = DesktopTray(queue.Queue(), LOGO_ICO)
        icon = tray._create_icon(Image.new("RGBA", (64, 64), "white"))
        icon.stop = Mock()
        with patch.object(icon, "_assert_icon_handle"), patch.object(win32, "Shell_NotifyIcon", return_value=False):
            tray._on_ready(icon)
        self.assertFalse(tray.ready.is_set())
        event, (attempt, error) = tray.events.get_nowait()
        self.assertEqual(event, "tray_failed")
        self.assertIs(attempt, tray)
        self.assertIn("Windows", error)
        icon.stop.assert_called_once()
        icon._unregister_class(icon._atom)

    @unittest.skipUnless(sys.platform == "win32", "Windows Explorer restart")
    def test_explorer_reregistration_failure_recovers_hidden_window(self):
        from PIL import Image
        from pystray._util import win32
        app = self.app()
        tray = app.tray = DesktopTray(app.events, LOGO_ICO)
        tray.icon = icon = tray._create_icon(Image.new("RGBA", (64, 64), "white"))
        icon.stop = Mock()
        icon._visible = True
        tray.ready.set()
        with patch.object(icon, "_assert_icon_handle"), patch.object(win32, "Shell_NotifyIcon", return_value=False):
            with self.assertRaises(OSError):
                icon._on_taskbarcreated(0, 0)
            self.assertFalse(tray.ready.is_set())
            with patch("video_gui.messagebox.askyesno", return_value=False):
                app._drain_events()
        self.assertIsNone(app.tray)
        app.root.deiconify.assert_called_once()
        self.assertFalse(icon.visible)
        icon._unregister_class(icon._atom)

    def test_worker_callbacks_only_queue_actions(self):
        tray = DesktopTray(queue.Queue(), LOGO_ICO)
        worker = threading.Thread(target=lambda: (tray.open_window(), tray.exit_app()))
        worker.start(); worker.join()
        self.assertEqual(tray.events.get_nowait(), ("tray_open", tray))
        self.assertEqual(tray.events.get_nowait(), ("tray_exit", tray))

    def test_exit_reentrancy_restores_guard_on_cancel_and_save_failure(self):
        app = self.app()
        app.tasks = Mock(active={"job"}, pending=[])
        app.canvas_window = Mock()
        app.tray = Mock(is_ready=True)
        def cancel(*_args, **_kwargs):
            app._request_exit()  # Nested tray exit while confirmation is open.
            return False
        with patch("video_gui.messagebox.askyesno", side_effect=cancel) as dialog:
            app._request_exit()
            dialog.assert_called_once()
        app.tasks.stop.assert_not_called()
        app.tray.stop.assert_not_called()
        self.assertFalse(app._exit_in_progress)
        app._show_page = Mock()
        app.canvas_window.request_stop.side_effect = lambda done: done(False)
        with patch("video_gui.messagebox.askyesno", return_value=True), patch("video_gui.messagebox.showerror"):
            app._request_exit()
        app.tray.stop.assert_not_called()
        self.assertFalse(app._exit_in_progress)

    def test_cleanup_failure_does_not_prevent_other_stops_or_destroy(self):
        app = self.app()
        app.tasks = Mock(active=set(), pending=[])
        app.canvas_window = Mock()
        app.canvas_window.request_stop.side_effect = lambda done: done(True)
        app.tray = Mock()
        app.video_player = Mock()

        app.lipsync_video_player = Mock()
        app.lipsync_audio_player = Mock()
        app.fish_audio_player = Mock()
        app.voice_player = Mock()
        app.fish_audio_player.stop.side_effect = OSError("player cleanup failed")
        app.repair_video_player = Mock()
        app._request_exit()
        app.tray.stop.assert_called_once()
        app.tasks.stop.assert_called_once()
        app.fish_audio_player.stop.assert_called_once()
        app.voice_player.stop.assert_called_once()
        app.root.destroy.assert_called_once()
        self.assertTrue(app._closed)
        self.assertEqual(app._shutdown_errors, ["player cleanup failed"])


if __name__ == "__main__":
    unittest.main()
