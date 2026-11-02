"""Offline library interaction acceptance using an invisible real Tk window."""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))
import customtkinter as ctk
from video_gui import MaterialUniverseApp


def main():
    report = {"network_called": False}
    with tempfile.TemporaryDirectory() as directory, patch("requests.Session", side_effect=AssertionError("network forbidden")):
        folder = Path(directory)
        root = ctk.CTk()
        root.withdraw(); root.attributes("-alpha", 0)
        with patch.object(MaterialUniverseApp, "_ensure_tray"), patch.object(MaterialUniverseApp, "_restore_history"):
            app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
        app.workspace_output_dir_var.set(str(folder / "outputs"))
        original_toplevel = ctk.CTkToplevel
        def invisible_window(*args, **kwargs):
            window = original_toplevel(*args, **kwargs)
            window.attributes("-alpha", 0)
            return window
        def choose(assets, target=None):
            app.asset_tree.selection_remove(*app.asset_tree.selection())
            if target: app.asset_use_var.set(target)
            app.asset_tree.selection_set([asset["id"] for asset in assets])
            app._refresh_asset_selection()
        def enabled(button): return button.cget("state") == "normal"
        def windows():
            return [child for child in root.winfo_children() if isinstance(child, original_toplevel)]
        def pump_until(condition):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                root.update()
                if condition(): return
                time.sleep(.02)
            raise AssertionError("library event timed out")
        try:
            app._show_page("library")
            root.geometry("1080x740"); root.deiconify(); root.update()
            assert "空" in app.asset_empty_var.get()
            assert not any(enabled(button) for button in (app.asset_use_button, app.asset_preview_button,
                       app.asset_move_button, app.asset_more_button))
            group = app.desktop_store.create_group("产品A")
            other = app.desktop_store.create_group("产品B")
            long_group = app.desktop_store.create_group("很长的产品系列名称" * 7)
            def asset(name, group_id=None):
                path = folder / name; path.write_bytes(name.encode())
                return app.desktop_store.import_asset(path, group_id=group_id)
            videos = [asset(f"video-{i}.mp4", group["id"]) for i in range(2)]
            images = [asset(f"image-{i}.png", group["id"]) for i in range(11)]
            audio = asset("voice.wav", other["id"])
            audio_other = asset("voice-other.wav", other["id"])
            app._refresh_library(); app._select_asset_group(long_group["name"]); root.update()
            assert app.asset_tree.column("name", "width") > app.asset_tree.column("group", "width")
            assert not app.asset_tree.column("kind", "stretch")
            for widget in (app.asset_group_menu, app.asset_group_manage_button, app.asset_type_menu,
                           app.asset_search_entry, app.asset_clear_search_button, app.asset_import_button,
                           app.asset_use_button, app.asset_preview_button, app.asset_move_button, app.asset_more_button):
                assert widget.winfo_width() > 25
                assert widget.winfo_rootx() >= root.winfo_rootx()
                assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
                assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
            assert app.asset_tree.winfo_height() > 150
            assert app.asset_group_menu.winfo_width() == 150
            ctk.set_widget_scaling(1.25); ctk.set_window_scaling(1.25)
            root.geometry("1080x740"); root.update()
            for widget in (app.asset_search_entry, app.asset_import_button, app.asset_use_button,
                           app.asset_preview_button, app.asset_move_button, app.asset_more_button):
                assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
                assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
            assert app.asset_tree.winfo_height() > 150
            ctk.set_widget_scaling(1); ctk.set_window_scaling(1)
            root.geometry("1080x740"); root.update()
            app._select_asset_group("全部分组")
            report["minimum_window_layout_100_and_125_percent_fixed_columns"] = True

            for target in ("换品原视频", "换口型视频", "画质修复视频"):
                choose([videos[0]], target)
                assert enabled(app.asset_use_button) and enabled(app.asset_preview_button)
                choose(videos)
                assert not enabled(app.asset_use_button) and not enabled(app.asset_preview_button)
                assert enabled(app.asset_move_button) and app.asset_more_menu.entrycget(0, "state") == "disabled"
            choose([audio], "换口型音频"); assert enabled(app.asset_use_button)
            choose([audio, audio_other]); assert not enabled(app.asset_use_button)
            choose(images[:1], "换品产品图"); assert enabled(app.asset_use_button)
            choose(images[:10]); assert enabled(app.asset_use_button)
            choose(images); assert not enabled(app.asset_use_button)
            choose([videos[0], images[0]]); assert not enabled(app.asset_use_button)
            with patch.object(app, "_load_reference_image") as load, patch("desktop_pages.messagebox.showinfo"):
                app._use_assets(); load.assert_not_called()
                choose(images[:10]); app._use_assets()
                assert load.call_count == 1 and len(app.extra_reference_images) == 9
            for target, source, loader, page in (
                    ("换品原视频", videos[0], "_load_source_video", "editor"),
                    ("换口型视频", videos[0], "_load_lipsync_video", "lipsync"),
                    ("换口型音频", audio, "_load_lipsync_audio", "lipsync"),
                    ("画质修复视频", videos[0], "_load_repair_video", "repair")):
                with patch.object(app, loader) as load, patch.object(app, "_show_page") as show:
                    choose([source], target); app._use_assets()
                    load.assert_called_once_with(Path(source["path"]))
                    show.assert_called_once_with(page)
            with patch("desktop_pages.os.startfile") as preview, patch("desktop_pages.ctk.CTkInputDialog") as rename:
                choose(videos)
                app._preview_asset(); app._rename_asset()
                preview.assert_not_called(); rename.assert_not_called()
                choose(videos[:1]); app._preview_asset(); preview.assert_called_once()
                rename.return_value.get_input.return_value = None
                app._rename_asset()
                assert app.desktop_store.assets()[0]["name"]
            report["type_count_compatibility_and_multi_selection_guards"] = True

            app._select_asset_group("产品A")
            app.asset_search.set("old-search")
            app._open_library("换口型视频")
            assert app.asset_use_var.get() == "换口型视频" and app.asset_filter.get() == "视频"
            assert not app.asset_search.get() and not app.asset_tree.selection()
            assert app._asset_group_id == group["id"]
            choose(videos[:1]); app._refresh_library()
            assert app.asset_use_var.get() == "换口型视频"
            app.asset_search.set("video-1")
            assert app.asset_tree.get_children() == (videos[1]["id"],)
            assert not app.asset_tree.selection() and not enabled(app.asset_use_button)
            app.asset_search.set("absent")
            assert "没有匹配" in app.asset_empty_var.get()
            app.asset_clear_search_button.invoke()
            assert len(app.asset_tree.get_children()) == 2
            report["entry_context_combined_filters_clear_search_hidden_selection"] = True

            app.asset_target_var.set("新增 1 个素材，1 个已存在并保留原分组")
            app._refresh_library(); choose(videos[:1]); app._refresh_library()
            assert "保留原分组" in app.asset_target_var.get()
            with patch("desktop_pages.Menu.tk_popup") as popup:
                app._show_asset_more(); popup.assert_called_once()
            assert app.asset_more_menu.entrycget(0, "state") == "normal"
            assert app.asset_more_menu.entrycget(2, "state") == "normal"
            report["import_status_survives_refresh_and_menu_states"] = True

            with patch("desktop_pages.ctk.CTkToplevel", side_effect=invisible_window):
                choose(videos)
                app._move_selected_assets(); root.update_idletasks()
                window = windows()[-1]
                menu = next(child for child in window.winfo_children() if isinstance(child, ctk.CTkOptionMenu))
                menu.set("产品B")
                next(child for child in window.winfo_children() if isinstance(child, ctk.CTkButton)).invoke()
                assert not app.asset_tree.selection()
                assert all(a.get("group_id") == other["id"] for a in app.desktop_store.assets() if a["id"] in {v["id"] for v in videos})
            app.asset_filter.set("全部素材"); app._select_asset_group("全部分组")
            choose(videos[:1])
            with patch("desktop_pages.ctk.CTkInputDialog") as dialog:
                dialog.return_value.get_input.return_value = "新名称"
                app._rename_asset()
            assert next(a for a in app.desktop_store.assets() if a["id"] == videos[0]["id"])["name"] == "新名称"
            choose(videos)
            with patch("desktop_pages.messagebox.askyesno", return_value=False): app._delete_asset()
            assert all(app.asset_tree.exists(v["id"]) for v in videos)
            original_delete = app.desktop_store.delete_asset
            def delete_with_reference_guard(identity):
                if identity == videos[1]["id"]: raise ValueError("素材正在被任务引用")
                return original_delete(identity)
            with patch.object(app.desktop_store, "delete_asset", side_effect=delete_with_reference_guard), \
                    patch("desktop_pages.messagebox.askyesno", return_value=True), patch("desktop_pages.messagebox.showerror") as error:
                app._delete_asset()
                assert "已删除 1 个，未删除 1 个" in error.call_args.args[1]
            assert not app.asset_tree.exists(videos[0]["id"]) and app.asset_tree.exists(videos[1]["id"])
            assert (folder / "video-0.mp4").exists()
            report["move_batch_rename_cancel_and_partial_delete_reporting"] = True

            fresh = folder / "import.png"; fresh.write_bytes(b"fresh import")
            app._select_asset_group("产品A")
            def pick_and_change_filter(**_):
                app._select_asset_group("产品B")
                return (str(fresh),)
            with patch("desktop_pages.filedialog.askopenfilenames", side_effect=pick_and_change_filter):
                app._import_assets()
                pump_until(lambda: "新增 1" in app.asset_target_var.get())
            imported = next(a for a in app.desktop_store.assets() if a["name"] == fresh.name)
            assert imported["group_id"] == group["id"]
            app._select_asset_group("全部分组")
            app._import_paths([str(fresh)], group_id=other["id"])
            pump_until(lambda: "已存在" in app.asset_target_var.get())
            assert next(a for a in app.desktop_store.assets() if a["id"] == imported["id"])["group_id"] == group["id"]
            ungrouped = folder / "ungrouped.wav"; ungrouped.write_bytes(b"new ungrouped audio")
            with patch("desktop_pages.filedialog.askopenfilenames", return_value=(str(ungrouped),)):
                app._import_assets()
                pump_until(lambda: any(a["name"] == ungrouped.name for a in app.desktop_store.assets()))
            assert next(a for a in app.desktop_store.assets() if a["name"] == ungrouped.name).get("group_id") is None
            pump_until(lambda: "新增 1" in app.asset_target_var.get())
            report["frozen_import_target_and_duplicate_group_preserved"] = True
            report["success"] = True
        finally:
            app.tasks.stop(); app.tasks.wait_idle(); app.tasks.stop()
            app.canvas_service.stop(); app.desktop_store.db.close(); root.destroy()
    output = ROOT / ".build/library-workflow-1.6.14.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__": main()
