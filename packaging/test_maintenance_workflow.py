"""Real Tk migration/update acceptance, using isolated libraries and blocked HTTP."""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))


def run(output):
    import customtkinter as ctk
    from PIL import Image, ImageGrab
    from video_gui import MaterialUniverseApp
    from desktop_tasks import DesktopStore
    from library_transfer import LibraryTransfer
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=True)
    report = {}
    with tempfile.TemporaryDirectory() as folder, \
            patch("requests.sessions.Session.request", side_effect=AssertionError("No network during offline QA")), \
            patch.object(MaterialUniverseApp, "_ensure_tray"), \
            patch.object(MaterialUniverseApp, "_restore_history"), \
            patch("maintenance_page.messagebox.showerror") as errors:
        folder = Path(folder)
        source = DesktopStore(folder / "source")
        group = source.create_group("产品参考")
        image = folder / "测试产品.png"; Image.new("RGB", (96, 128), "#cabb8a").save(image)
        source.import_asset(image, group["id"])
        outgoing = LibraryTransfer(source); outgoing.recover()
        archive = folder / "migration.zip"; outgoing.export(archive)
        source.db.close()
        root = ctk.CTk()
        root.geometry("1080x740")
        app = MaterialUniverseApp(root, demo=True, data_root=folder / "destination")
        callback_errors = []
        root.report_callback_exception = lambda *exc: callback_errors.append(str(exc))
        def pump(condition=lambda: True):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                root.update(); time.sleep(.03)
                if condition():
                    assert not callback_errors, callback_errors
                    return
            raise AssertionError("UI timed out: " + str(callback_errors))
        try:
            app._show_page("api")
            pump(); pump()
            with patch("maintenance_page.filedialog.askopenfilename", return_value=str(archive)), \
                    patch("maintenance_page.messagebox.askyesno", return_value=True):
                app._import_libraries()
                pump(lambda: not app._maintenance_busy and len(app.desktop_store.assets()) == 1)
            assert len(app.desktop_store.groups()) == 1
            assert "导入完成" in app.library_status.get()
            report["preview_confirm_and_import"] = True
            export = folder / "roundtrip.zip"
            with patch("maintenance_page.filedialog.asksaveasfilename", return_value=str(export)):
                app._export_libraries(); pump(lambda: not app._maintenance_busy)
            assert export.is_file()
            report["export_buttons"] = True
            with patch("maintenance_page.check_update", return_value={"available": False, "message": "当前已是最新版本。"}):
                app._check_application_update(); pump(lambda: not app._maintenance_busy)
            assert "最新" in app.update_status.get()
            report["update_check_button"] = True
            with patch("maintenance_page.messagebox.showinfo") as info:
                app.tasks.active.add("offline-busy")
                app._import_libraries()
                info.assert_called_once()
                app.tasks.active.clear()
            report["active_task_blocks_migration"] = True
            root.attributes("-topmost", True); root.deiconify(); root.lift()
            for size in ("1080x740", "1440x920"):
                root.geometry(size)
                app.api_page._parent_canvas.yview_moveto(0)
                pump(); time.sleep(.25); pump()
                x, y = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(output / f"settings-{size}.png")
            errors.assert_not_called()
            report["success"] = True
        finally:
            app._closed = True
            app.tasks.stop(); app.fish_audio_player.stop(); app.voice_player.stop()
            if app.tray: app.tray.stop()
            root.destroy(); app.desktop_store.db.close()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else ROOT / ".build/maintenance-acceptance"), ensure_ascii=False))
