"""Offline Tk acceptance for material groups; never submits a provider task."""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))

import customtkinter as ctk
from desktop_tasks import DesktopStore
from video_gui import MaterialUniverseApp


def main():
    report = {}
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        root = ctk.CTk()
        root.withdraw()
        root.attributes("-alpha", 0)
        with patch.object(MaterialUniverseApp, "_ensure_tray"), patch.object(MaterialUniverseApp, "_restore_history"):
            app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
        app.workspace_output_dir_var.set(str(folder / "outputs"))
        try:
            first = folder / "a.mp4"; first.write_bytes(b"first")
            second = folder / "b.png"; second.write_bytes(b"second")
            third = folder / "c.wav"; third.write_bytes(b"third")
            group_a = app.desktop_store.create_group("产品A")
            group_b = app.desktop_store.create_group("产品B")
            asset_a = app.desktop_store.import_asset(first, group_id=group_a["id"])
            asset_b = app.desktop_store.import_asset(second, group_id=group_b["id"])
            app.desktop_store.import_asset(third)
            app._refresh_library()
            assert len(app.asset_tree.get_children()) == 3
            app._select_asset_group("产品B")
            app.asset_filter.set("图片")
            app.asset_search.set("b")
            app._refresh_library()
            assert app.asset_tree.get_children() == (asset_b["id"],)
            app.asset_search.set("absent")
            app._refresh_library()
            assert not app.asset_tree.get_children()
            app.asset_search.set("")
            app.asset_filter.set("全部素材")
            app._select_asset_group("产品A")
            assert app.asset_tree.get_children() == (asset_a["id"],)
            report["group_type_keyword_combined"] = True

            app._manage_asset_groups()
            window = next(child for child in root.winfo_children()
                          if isinstance(child, ctk.CTkToplevel) and child.title() == "管理素材分组")
            window.attributes("-alpha", 0)
            root.update()
            menu = next(child for child in window.winfo_children() if isinstance(child, ctk.CTkOptionMenu))
            buttons = [child for frame in window.winfo_children() if isinstance(frame, ctk.CTkFrame)
                       for child in frame.winfo_children() if isinstance(child, ctk.CTkButton)]
            menu.set("产品B")
            with patch("desktop_pages.ctk.CTkInputDialog") as dialog:
                dialog.return_value.get_input.return_value = "产品B改名"
                next(button for button in buttons if button.cget("text") == "重命名").invoke()
            assert menu.get() == "产品B改名"
            assert next(group for group in app.desktop_store.groups() if group["id"] == group_b["id"])["name"] == "产品B改名"
            assert app._asset_group_id == group_a["id"]
            window.destroy()
            report["manage_dialog_stable_selected_id"] = True

            app._select_asset_group("产品B改名")
            fresh = folder / "fresh.mp4"; fresh.write_bytes(b"new material")
            def delete_during_dialog(**_):
                app.desktop_store.delete_group(group_b["id"])
                return (str(fresh),)
            with patch("desktop_pages.filedialog.askopenfilenames", side_effect=delete_during_dialog), \
                    patch("video_gui.messagebox.showerror") as show_error:
                app._import_assets()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not show_error.called:
                    root.update(); time.sleep(.02)
                assert show_error.called
                assert "已删除" in str(show_error.call_args)
            assert not any(asset["name"] == fresh.name for asset in app.desktop_store.assets())
            assert app._asset_group_id is None
            report["dialog_target_deleted_reports_failure"] = True

            app.tasks.stop()
            app.canvas_service.stop()
            app.desktop_store.db.close()
            root.destroy()
            root = None
            reopened = DesktopStore(folder / "state")
            assert reopened.groups() == [{"id": group_a["id"], "name": "产品A", "created": group_a["created"]}]
            assert next(asset for asset in reopened.assets() if asset["id"] == asset_a["id"])["group_id"] == group_a["id"]
            assert "group_id" not in next(asset for asset in reopened.assets() if asset["id"] == asset_b["id"])
            reopened.db.close()
            report["groups_and_membership_survive_restart"] = True
        finally:
            if root is not None:
                app.tasks.stop(); app.tasks.wait_idle(); app.tasks.stop()
                app.canvas_service.stop(); app.desktop_store.db.close(); root.destroy()
    report["success"] = True
    output = ROOT / ".build/asset-groups-gui-1.6.14.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
