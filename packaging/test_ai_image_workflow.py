"""Real Tk acceptance of single-form AI images, with fake providers and isolated data."""
import copy
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))
sys.path.insert(0, str(ROOT / "skills/video-product-swap/tests"))


def main():
    import customtkinter as ctk
    from PIL import Image, ImageDraw, ImageGrab
    from video_gui import MaterialUniverseApp
    from desktop_tasks import execute_job, DesktopStore
    from ai_image_models import DEFAULT_MODEL, DEFAULT_PARAMETERS, MODEL_LABELS
    from ai_image_store import AIImageStore
    from fission_service import run_job
    from test_ai_image import Fish, Download
    output = ROOT / "output/ai-image-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".build/ai-image-test-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    report = {}
    with tempfile.TemporaryDirectory(dir=scratch) as temp, \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Network prohibited")), \
            patch.object(MaterialUniverseApp, "_ensure_tray"):
        folder = Path(temp)
        root = ctk.CTk(); root.withdraw()
        app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
        app.workspace_output_dir_var.set(str(folder / "outputs"))
        app.provider_vars["kaiyuncode"]["key_var"].set("offline-placeholder")
        errors = []
        root.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

        def pump(predicate=lambda: True, seconds=10):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if errors: raise AssertionError("Tk callback: " + repr(errors))
                if predicate(): return
                time.sleep(.02)
            raise AssertionError("UI timeout: " + app.ai_status.get() + repr(app.desktop_store.jobs()))

        def capture(name):
            root.deiconify(); root.update(); time.sleep(.25); root.update()
            if app._ai_preview_path: wait_preview()
            import ctypes
            hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
            ImageGrab.grab(window=hwnd).save(output / name)

        def assert_visible(widget):
            assert widget.winfo_ismapped(), "not mapped"
            assert widget.winfo_rootx() >= root.winfo_rootx(), "left overflow"
            assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width(), "right overflow"
            assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height(), "bottom overflow"

        def set_prompt(text):
            app.ai_prompt.delete("1.0", "end"); app.ai_prompt.insert("1.0", text); app._ai_save()

        def wait_preview():
            pump(lambda: not app._ai_preview_pending and app._ai_preview_image is not None)

        def assert_blank_draft():
            form = app.ai_image_store.get()
            assert form["model"] == DEFAULT_MODEL and form["parameters"] == DEFAULT_PARAMETERS
            assert not form["references"] and not form["prompt"] and form["assistant"] == {"request": ""}
            assert form["helper_result"] is None and form["undo_prompt"] is None and form["pending_submission"] is None
            assert app.ai_prompt.get("1.0", "end-1c") == "" and app.ai_requirements.get("1.0", "end-1c") == ""
            assert not app.ai_helper_panel.winfo_manager() and app._ai_preview_ref is None
            assert app.ai_helper_undo.cget("state") == app.ai_helper_view.cget("state") == "disabled"
            other = DesktopStore(app.desktop_store.root)
            try: assert AIImageStore(other).get() == form
            finally: other.db.close()

        try:
            root.geometry("1080x740+0+0"); root.deiconify(); app._show_page("ai_image"); pump()
            assert app.ai_image_nav.cget("text") == "AI 生图"
            assert not hasattr(app, "fission_tab") and not hasattr(app, "fission_store")
            assert app.ai_image_store.get()["parameters"] == DEFAULT_PARAMETERS
            assert not app.ai_helper_panel.winfo_manager()
            assert app.ai_ref_section.winfo_y() < app.ai_prompt.winfo_y()
            assert app.ai_helper_toggle.cget("text") == "AI 美工"
            assert not hasattr(app, "ai_role_vars") and not hasattr(app, "ai_direction")
            report["references_above_prompt_and_simplified_helper"] = True
            report["single_form_defaults_no_conversations_or_projects"] = True
            app.fish_workspaces = {"离线演示工作区": "workspace"}; app.fish_workspace.set("离线演示工作区")

            mother = folder / "原始母图.png"
            image = Image.new("RGB", (480, 640), "#DFE9E9"); draw = ImageDraw.Draw(image)
            draw.rectangle((20, 25, 155, 560), fill="#C9D7D5")
            draw.ellipse((160, 60, 325, 260), fill="#CDB19C")
            draw.rounded_rectangle((100, 270, 390, 640), 60, fill="#74897D")
            draw.rounded_rectangle((270, 355, 350, 490), 12, fill="#AC7F31")
            draw.rectangle((276, 335, 344, 365), fill="#D4BF88"); image.save(mother)
            product = folder / "产品参考.png"
            image = Image.new("RGB", (480, 640), "#F7F7F5"); draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((150, 210, 330, 500), 24, fill="#B7842A")
            draw.rectangle((162, 150, 318, 230), fill="#DBCA98"); image.save(product)
            generated = folder / "generated.png"
            with Image.open(mother) as source: image = source.copy()
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((272, 355, 348, 488), 10, fill="#BD954C")
            draw.line((282, 372, 282, 473), fill="#E3CF9F", width=5)
            image.resize((2048, 3072), Image.Resampling.LANCZOS).save(generated)

            fish = Fish(); app.tasks.execute = execute_job
            actual_run = lambda job, credentials, update, resume=False: run_job(job, credentials, update,
                resume, client_factory=lambda *_: fish, sleep=lambda _: None)
            with patch("ai_image_page.FishClient", return_value=fish), patch("fission_service.put_media"), \
                    patch("fission_service.run_job", side_effect=actual_run), \
                    patch("fission_service.requests.get", return_value=Download(generated.read_bytes())):
                app._ai_refresh_models(catalog=True); pump(lambda: not app.ai_model_busy)
                assert len(app._ai_catalog_ids) == 8
                assert DEFAULT_MODEL in app.ai_image_store.model_cache()
                report["live_catalog_and_cached_capabilities"] = True
                set_prompt("窗边的护肤品静物，柔和自然光。")
                with patch("ai_image_page.messagebox.askyesno", return_value=False):
                    app._ai_quote(); pump(lambda: not app.ai_image_busy)
                assert not app.desktop_store.jobs()
                assert not any(t.startswith("generate_") for t, _ in fish.calls)
                assert app.ai_image_store.get()["prompt"] == "窗边的护肤品静物，柔和自然光。"
                report["cancel_quote_no_generation"] = True
                with patch("ai_image_page.messagebox.askyesno") as confirm:
                    app._ai_quote(); set_prompt("修改后的提示词"); pump(lambda: not app.ai_image_busy)
                    confirm.assert_not_called()
                assert not app.desktop_store.jobs()
                report["stale_quote_rejected"] = True
                with patch("ai_image_page.messagebox.askyesno", return_value=True):
                    app._ai_quote(); app._ai_quote()
                    pump(lambda: not app.ai_image_busy and bool(app.desktop_store.jobs()) and app.desktop_store.jobs()[0]["status"] == "success")
                app.tasks.wait_idle(); pump()
                assert sum(t == "generate_image" for t, _ in fish.calls) == 1
                assert not any(t == "create_media_upload" for t, _ in fish.calls)
                assert app._ai_selected_path() and Path(app._ai_selected_path()).is_file()
                assert_blank_draft()
                assert app.desktop_store.jobs()[0]["config"]["prompt"] == "修改后的提示词"
                report["text_generation_once_and_inline_result"] = True

                app._ai_add_paths([mother, product]); pump(lambda: not app.ai_image_busy)
                assert len(app.ai_image_store.get()["references"]) == 2
                set_prompt("保留 @图1 的人物、衣服和构图，按 @图2 替换产品，匹配照明与接触阴影。")
                app.ai_model_var.set(MODEL_LABELS["nano-banana-2-lite"]); app._ai_model_changed()
                assert app.ai_parameter_menus["resolution"].cget("state") == "disabled"
                assert app.ai_image_store.get()["parameters"] == {"aspect_ratio": "9:16"}
                prompt = app.ai_prompt.get("1.0", "end-1c")
                app._ai_reset_parameters()
                assert app.ai_image_store.get()["parameters"] == DEFAULT_PARAMETERS
                assert app.ai_prompt.get("1.0", "end-1c") == prompt
                assert len(app.ai_image_store.get()["references"]) == 2
                report["multi_reference_import_and_model_controls_preserve_inputs"] = True

                app._ai_toggle_helper(); pump()
                app.ai_requirements.insert("1.0", "把手里的产品换成图2，保留人物和动作，修正瓶身反光与手指接触阴影")
                capture("智能提示词-图片裂变-1080x740.png")
                assert_visible(app.ai_requirements); assert_visible(app.ai_helper_generate)
                answer = {"prompt": "保持 @图1 人物、服装和构图，按 @图2 调整瓶身，匹配画面照明与接触阴影。", "note": "重点检查瓶身边缘。"}
                with patch("ai_image_page.generate_prompt", return_value=answer):
                    app._ai_generate_helper(); pump(lambda: not app.ai_image_busy)
                assert app.ai_prompt.get("1.0", "end-1c") == answer["prompt"]
                app._ai_undo_helper(); assert app.ai_prompt.get("1.0", "end-1c") == prompt
                window = app._ai_view_helper(); pump(); window.destroy()
                with patch("ai_image_page.generate_prompt", return_value=answer):
                    app._ai_generate_helper(); set_prompt("处理期间的新要求"); pump(lambda: not app.ai_image_busy)
                assert app.ai_prompt.get("1.0", "end-1c") == "处理期间的新要求"
                set_prompt(answer["prompt"]); app._ai_toggle_helper(); pump()
                report["optional_helper_fill_undo_view_and_late_result"] = True

                app.ai_model_var.set(MODEL_LABELS["nano-banana-2-lite"]); app._ai_model_changed()
                app.ai_parameter_vars["aspect_ratio"].set("16:9"); app._ai_save()
                assert app.ai_image_store.get()["model"] != DEFAULT_MODEL
                app._ai_toggle_helper()  # A successful submission also collapses an open helper.
                app._ai_not_before = 0
                # A rejected local enqueue keeps the entire draft and approved request.
                original = copy.deepcopy(app.ai_image_store.get())
                with patch("ai_image_page.messagebox.askyesno", return_value=True), \
                        patch.object(app.tasks, "submit", side_effect=OSError("local store unavailable")):
                    app._ai_quote(); pump(lambda: not app.ai_image_busy)
                pending = app.ai_image_store.get()["pending_submission"]
                assert pending and len(app.desktop_store.jobs()) == 1
                assert all(app.ai_image_store.get()[k] == original[k] for k in ("prompt", "references", "assistant", "helper_result", "undo_prompt"))
                with patch("ai_image_page.messagebox.askyesno", return_value=True):
                    submit = app.tasks.submit
                    def lost_local_reply(*args, **kwargs):
                        submit(*args, **kwargs); raise RuntimeError("lost local callback")
                    with patch.object(app.tasks, "submit", side_effect=lost_local_reply):
                        app._ai_quote()
                        pump(lambda: not app.ai_image_busy and len(app.desktop_store.jobs()) == 2 and all(j["status"] == "success" for j in app.desktop_store.jobs()))
                app.tasks.wait_idle(); pump()
                assert sum(t == "generate_image_edit" for t, _ in fish.calls) == 1
                assert_blank_draft()
                selected_job = app._ai_selected_job()
                assert selected_job["request_id"] == pending["request_id"]
                assert len(selected_job["config"]["image_inputs"]) == 2
                assert selected_job["config"]["prompt"] == answer["prompt"]
                assert all(Path(r["path"]).is_file() for r in selected_job["config"]["image_inputs"])
                # Restart/page recovery finds the persisted task and performs the same reset.
                app._ai_reuse_selected()
                app.ai_image_store.fields(pending_submission=pending)
                retired_generation = app._ai_form_generation
                app._ai_on_show()
                assert_blank_draft()
                settled = app.ai_image_store.get()
                with patch("ai_image_page.messagebox.askyesno") as confirm:
                    for kind, result in (("helper", answer), ("references", {"references": original["references"], "errors": []}), ("quote", pending["config"])):
                        app._handle_ai_image_event((kind, None, result, None, retired_generation))
                    confirm.assert_not_called()
                assert app.ai_image_store.get() == settled
                assert len(app.desktop_store.jobs()) == 2
                report["reset_persisted_draft_and_helpers_keep_submitted_config_and_results"] = True
                report["failed_enqueue_preserves_draft_recovery_resets_and_rejects_old_events"] = True
                report["reference_generation_lost_local_reply_resolves_original_job"] = True
                app.ai_form_panel._parent_canvas.yview_moveto(0); pump()
                capture("AI生图-1080x740.png")
                for widget in (app.ai_generate_button, *app.ai_result_buttons.values()): assert_visible(widget)
                app.ai_form_panel._parent_canvas.yview_moveto(1); pump()
                capture("AI生图-参数-1080x740.png")
                for widget in (app.ai_model_menu, *app.ai_parameter_menus.values()): assert_visible(widget)
                root.geometry("1440x900+0+0"); pump(); time.sleep(.2); pump()
                app.ai_form_panel._parent_canvas.yview_moveto(0); pump()
                capture("AI生图-1440x900.png")
                selected = app.ai_image_store.get()["selected_job"]
                for _ in range(4):
                    app._ai_select_result(selected); pump(); time.sleep(.2); pump()
                root.geometry("1080x740+0+0"); pump(); time.sleep(.5); pump()
                wait_preview()
                for widget in (app.ai_preview, *app.ai_result_buttons.values(), app.ai_results): assert_visible(widget)
                report["large_image_selection_then_window_shrink"] = True
                fitted = app._ai_preview_image.width(), app._ai_preview_image.height()
                viewport = app.ai_preview.winfo_width(), app.ai_preview.winfo_height()
                app.ai_zoom_buttons["in"].invoke(); wait_preview()
                zoomed = app._ai_preview_image.width(), app._ai_preview_image.height()
                assert zoomed[0] > fitted[0] and zoomed[1] > fitted[1]
                app.ai_zoom_buttons["out"].invoke(); wait_preview()
                assert (app._ai_preview_image.width(), app._ai_preview_image.height()) == fitted
                for _ in range(3): app.ai_zoom_buttons["in"].invoke()
                app.ai_preview.scan_mark(20, 20); app.ai_preview.scan_dragto(100, 100, gain=1)
                app.ai_zoom_buttons["fit"].invoke(); wait_preview()
                assert (app._ai_preview_image.width(), app._ai_preview_image.height()) == fitted
                assert app.ai_preview.xview() == (0.0, 1.0) and app.ai_preview.yview() == (0.0, 1.0)
                assert (app.ai_preview.winfo_width(), app.ai_preview.winfo_height()) == viewport
                app.ai_zoom_buttons["out"].invoke(); wait_preview()
                assert app._ai_preview_image.width() < fitted[0]
                app._ai_select_result(selected); wait_preview()
                assert app._ai_preview_zoom == 1.0
                assert (app._ai_preview_image.width(), app._ai_preview_image.height()) == fitted
                report["zoom_out_in_fit_and_recent_selection_reset"] = True
                report["minimum_and_large_window_layout"] = True
                viewer = app._ai_compare(); pump(); assert viewer.winfo_exists(); viewer.destroy()
                report["comparison_viewer"] = True
                exported = folder / "另存.png"
                with patch("ai_image_page.filedialog.asksaveasfilename", return_value=str(exported)):
                    app._ai_export()
                assert exported.read_bytes() == Path(app._ai_selected_path()).read_bytes()
                report["export_selected_image"] = True
                app._ai_use_as_reference(); pump(lambda: not app.ai_image_busy)
                assert len(app.ai_image_store.get()["references"]) == 1
                assert app.ai_image_store.get()["references"][-1]["remote"]["object_key"] == "generated-key"
                app._ai_remove_reference(0)
                assert not app.ai_image_store.get()["references"]
                app._ai_reuse_selected()
                app._ai_preview_reference(0); app._ai_reuse_selected()
                assert len(app.ai_image_store.get()["references"]) == 2
                assert app.ai_image_store.get()["prompt"] == answer["prompt"]
                assert app._ai_preview_ref is None
                assert app._ai_selected_path() == app._ai_result_path(app._ai_selected_job())
                report["result_reference_and_reuse_parameters"] = True
                app._ai_use_as_frame(); pump(lambda: "first_frame" in app.fish_media)
                assert app.current_page == "image_audio" and Path(app.fish_media["first_frame"]["path"]).is_file()
                assert app.director_toggle_button.cget("text") == "AI 导演"
                app.image_audio_page._parent_canvas.yview_moveto(.55); pump()
                capture("智能提示词-seedance导演-1080x740.png")
                assert_visible(app.director_toggle_button)
                report["send_selected_image_to_video_first_frame"] = True

            app._show_page("ai_image"); set_prompt("关闭后继续的草稿")
            app._show_page("history"); app._show_page("ai_image")
            assert app.ai_prompt.get("1.0", "end-1c") == "关闭后继续的草稿"
            app._open_library("ai_image")
            assets = app.desktop_store.assets()[:2]
            app.asset_tree.selection_set(*(a["id"] for a in assets)); app._refresh_asset_selection()
            assert app.asset_use_button.cget("text") == "添加为参考图"
            assert app.asset_use_button.cget("state") == "normal"
            app._use_assets(); pump(lambda: not app.ai_image_busy)
            assert app.current_page == "ai_image"
            app._open_library("ai_image"); app._cancel_asset_pick(); assert app.current_page == "ai_image"
            app._ai_show_history(); assert app.history_filter.get() == "AI 生图"
            assert len(app.history_tree.get_children()) == 2
            report["saved_draft_multi_library_selection_and_history"] = True
            legacy = copy.deepcopy(app.desktop_store.jobs()[0]["config"])
            legacy.pop("workflow"); legacy.update(mode="restart", locks="保留光线", prompt="调整旧图")
            for i, ref in enumerate(legacy["image_inputs"]): ref.update(role="original" if i == 0 else "product", label="母图" if i == 0 else "产品参考")
            app._ai_reuse_config(legacy)
            assert "保留光线" in app.ai_prompt.get("1.0", "end-1c")
            assert app.current_page == "ai_image"
            other = DesktopStore(app.desktop_store.root)
            try:
                assert AIImageStore(other).get() == app.ai_image_store.get()
                assert other.db.execute("SELECT count(*) FROM image_sessions").fetchone()[0] == 0
            finally: other.db.close()
            report["legacy_reuse_and_single_form_persistence"] = True
            assert not errors, errors
            report["no_tk_errors"] = True
        finally:
            app._closed = True
            app.ai_display.close(wait=True)
            app.tasks.stop(); app.tasks.wait_idle()
            app.fish_audio_player.stop(); app.voice_player.stop()
            app.canvas_service.stop()
            root.destroy(); app.desktop_store.db.close()
    (output / "验收结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__": main()
