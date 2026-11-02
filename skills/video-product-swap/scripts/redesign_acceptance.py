"""Isolated real-Tk acceptance for 1.9; outbound HTTP is prohibited."""
import copy
import json
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import patch


def run(output_dir=None):
    import customtkinter as ctk
    from PIL import Image, ImageDraw, ImageGrab
    from video_gui import MaterialUniverseApp
    from director_prompt import read_rule, configuration
    from feature_policy import require_creation
    from image_audio_video import MEDIA_FIELDS, IMAGE_FIELDS
    result = {}
    # Shell notification-area availability is outside this isolated UI flow.
    # A tray failure dialog can close the test window and stop its event pump;
    # tray lifetime has its own tests.
    with tempfile.TemporaryDirectory() as folder, \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Network prohibited")), \
            patch.object(MaterialUniverseApp, "_ensure_tray"):
        folder = Path(folder)
        root = ctk.CTk()
        root.withdraw()
        if not output_dir:
            root.attributes("-alpha", 0)
            root.deiconify()
        app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
        app.workspace_output_dir_var.set(str(folder / "outputs"))
        errors = []
        import traceback
        root.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))
        def pump(test=lambda: True, seconds=8):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if test(): return
                time.sleep(.02)
            raise AssertionError("UI timeout: " + repr(errors))
        def capture(name):
            if output_dir:
                dest = Path(output_dir); dest.mkdir(parents=True, exist_ok=True)
                root.attributes("-topmost", True)
                root.deiconify(); root.lift(); root.update(); time.sleep(.2)
                x, y = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(dest / name)
        try:
            root.geometry("1080x740")
            assert app.current_page == "image_audio"
            assert not hasattr(app, "editor_nav") and not hasattr(app, "lipsync_nav") and not hasattr(app, "repair_nav")
            assert app.director_mode.get() == "请选择导演模式"
            assert app.director_generate_button.cget("state") == "disabled"
            assert "模式" in read_rule("SKILL.md")
            result["main_navigation_and_no_default_director"] = True
            app.prompt_key_var.set("legacy-key")
            try:
                configuration(app._credential_values())
            except ValueError:
                pass
            else:
                raise AssertionError("Director used legacy credentials")
            app.provider_vars["kaiyuncode"]["key_var"].set("offline-kaiyun-key")
            assert configuration(app._credential_values()) == ("https://kaiyuncode.com/v1", "offline-kaiyun-key", "gpt-6.1-sol")
            result["director_kaiyun_shared_account"] = True
            result["director_model"] = configuration(app._credential_values())[2]
            # Populate real local assets without accessing any user media.
            group = app.desktop_store.create_group("产品参考")
            imported = []
            for i in range(6):
                path = folder / f"参考图片-{i+1}.png"
                image = Image.new("RGB", (480, 640), (225 - i * 12, 220 + i * 3, 220))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((145, 190, 335, 420), radius=25, fill=(190, 155, 75))
                draw.rectangle((150, 160, 330, 210), fill=(225, 195, 110))
                image.save(path)
                imported.append(app.desktop_store.import_asset(path, group_id=group["id"]))
            audio = folder / "测试口播.wav"
            with wave.open(str(audio), "wb") as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
                out.writeframes(b"\0\0" * 4 * 16000)
            app.desktop_store.import_asset(audio)
            for field, asset in zip(IMAGE_FIELDS, imported): app._fish_load(field, asset["path"])
            app._fish_load("audio", audio)
            pump(lambda: len(app.fish_media) == 4)
            app._handle_fish_event(("account", {"workspaces": [{"workspace_id": "offline", "workspace_name": "电商创作", "team_name": "示例团队"}]}, None))
            app._open_library("first_frame")
            app.asset_tree.selection_set(imported[0]["id"])
            assert app.asset_use_button.cget("state") == "normal"
            capture("素材库-选择首帧.png")
            app._use_assets()
            pump(lambda: len(app.fish_media) == 4)
            assert app.current_page == "image_audio"
            app._open_library("audio")
            assert app.asset_filter.get() == "音频"
            assert app.asset_use_button.cget("state") == "disabled"
            app._cancel_asset_pick()
            result["library_role_selection_and_roundtrip"] = True
            # Drafting requires explicit selection and never triggers video generation.
            app._toggle_director()
            before = "用户自己的拍摄要求"
            app.fish_prompt.insert("1.0", before)
            app.director_mode.set("边讲边用")
            snapshot = app._director_snapshot()
            generated = {"prompt": "按 @图1 构图，@图2 人物，@图3 产品，保持 @音频1 完整，连续取用并涂抹。", "transcript": "示例转写", "mode": "use",
                         "visual_plan": "只展示和使用首帧主产品", "issues": ["其他产品仅口播，不新增画面"]}
            app._handle_director_event(("done", snapshot, generated, None))
            assert app.fish_prompt.get("1.0", "end").strip() == generated["prompt"]
            assert "1 条编排说明" in app.director_note.get()
            app._view_director(); pump()
            review = next(w for w in root.winfo_children() if isinstance(w, ctk.CTkToplevel))
            review_text = next(w for w in review.winfo_children() if isinstance(w, ctk.CTkTextbox)).get("1.0", "end")
            assert generated["visual_plan"] in review_text and generated["issues"][0] in review_text
            review.destroy()
            app._undo_director()
            assert app.fish_prompt.get("1.0", "end").strip() == before
            snapshot = app._director_snapshot()
            app.fish_prompt.insert("end", "新输入")
            app._handle_director_event(("done", snapshot, generated, None))
            assert "新输入" in app.fish_prompt.get("1.0", "end")
            app.director_mode.set("请选择导演模式")
            app.director_generate_button.configure(state="disabled")
            app.image_audio_page._parent_canvas.yview_moveto(1)
            pump()
            capture("图音生视频-智能提示词.png")
            assert not app.desktop_store.jobs()
            result["draft_undo_and_inflight_edit_protection"] = True
            app._show_page("api"); pump()
            assert app.fish_connect_button.winfo_ismapped()
            capture("设置-服务账户.png")
            app._show_page("image_audio"); pump()
            assert not app.fish_connect_button.winfo_ismapped()
            app.image_audio_page._parent_canvas.yview_moveto(0)
            capture("图音生视频-首页.png")
            result["fish_account_only_in_settings"] = True
            # Retired creations are rejected before dispatch, historical records are readable.
            for kind, config in [("full", {}), ("lipsync", {}), ("repair", {}), ("general", {"model": "kaiyuncode-omni_video_edit"})]:
                try: app.tasks.submit(kind, {**config, "output": str(folder / "unused.mp4")}, {})
                except ValueError: pass
                else: raise AssertionError("retired task accepted")
            now = time.time()
            for i, status in enumerate(("success", "attention", "failed", "cancelled")):
                job = {"id": f"history-{i}", "kind": "image_audio" if i != 3 else "lipsync", "name": ["人参霜 · 纯口播", "人参油 · 使用演示", "产品展示", "旧换口型任务"][i],
                       "status": status, "stage": ["成片已保存", "生成完成，待下载", "请检查服务连接", "已取消排队"][i], "created": now - i * 50,
                       "config": {"output": str(folder / f"result-{i}.mp4"), "model": "seedance-2.5", "prompt": generated["prompt"]}, "asset_ids": []}
                with app.desktop_store.lock, app.desktop_store.db:
                    app.desktop_store.db.execute("INSERT INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job, ensure_ascii=False)))
            app._show_page("history"); pump()
            assert len(app.history_tree.get_children()) == 4
            app.history_tree.selection_set("history-1")
            app._sync_history()
            pump(lambda: not app.history_sync_busy)
            assert app.history_tree.selection() == ("history-1",)
            capture("使用记录.png")
            app._task_detail(); pump()
            capture("使用记录-详情.png")
            app.history_state.set("已完成"); app._refresh_history()
            assert len(app.history_tree.get_children()) == 1
            result["retired_routes_history_and_refresh_selection"] = True
            app._browse_library(); app.asset_filter.set("全部素材"); app._refresh_library(); pump()
            app.asset_tree.selection_set(imported[0]["id"], imported[1]["id"])
            assert len(app._selected_assets()) == 2
            capture("素材库-浏览.png")
            assert not errors, errors
            result["minimum_window_and_tk_callbacks"] = True
            return result
        finally:
            app._closed = True
            if app.tray: app.tray.stop()
            app.canvas_service.stop()
            app.fish_audio_player.stop()
            app.tasks.stop(); app.tasks.wait_idle(5)
            app.asset_tree.pool.shutdown(wait=True, cancel_futures=True)
            app.desktop_store.db.close()
            root.destroy()
