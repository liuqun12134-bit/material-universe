"""Offline real-window acceptance of feedback, pending approval, editing and learning."""
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))
sys.path.insert(0, str(ROOT / "skills/video-product-swap/tests"))


def main():
    import customtkinter as ctk
    from PIL import Image, ImageGrab
    from video_gui import MaterialUniverseApp
    from assistant_learning import analyze_source
    from ai_image_assistant import generate_prompt
    from test_assistants import content

    scratch = ROOT / ".build/assistant-ui-temp"; scratch.mkdir(parents=True, exist_ok=True)
    output = ROOT / "output/assistant-acceptance"; output.mkdir(parents=True, exist_ok=True)
    errors, report = [], {}
    with tempfile.TemporaryDirectory(dir=scratch) as temp, \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Real network prohibited")), \
            patch.object(MaterialUniverseApp, "_ensure_tray"):
        folder = Path(temp)
        root = ctk.CTk(); root.withdraw()
        app = MaterialUniverseApp(root, data_root=folder / "data", demo=True)
        root.report_callback_exception = lambda *args: errors.append("".join(traceback.format_exception(*args)))
        root.geometry("1080x740+0+0")
        app.provider_vars["kaiyuncode"]["key_var"].set("offline-test")

        def pump(predicate=lambda: True, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if errors: raise AssertionError(errors)
                if predicate(): return
                time.sleep(.02)
            raise AssertionError("UI timeout: " + app.assistant_status.get())

        def capture(window, name):
            import ctypes
            window.geometry("+0+0"); window.deiconify(); window.lift(); pump()
            time.sleep(.25); pump()
            ImageGrab.grab(window=ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)).save(output / name)

        def close_children():
            for child in root.winfo_children():
                if child.winfo_class() == "Toplevel" and child is not app._assistant_window:
                    child.destroy()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        def button(window, text):
            return next(w for w in descendants(window) if isinstance(w, ctk.CTkButton) and w.cget("text") == text)

        try:
            root.deiconify(); app._show_page("api"); pump()
            assert app.ai_helper_toggle.cget("text") == "AI 美工"
            assert app.director_toggle_button.cget("text") == "AI 导演"
            assert set(app._assistant_badges) == {"artist", "director"}
            capture(root, "设置-两个AI助手.png")
            picture = folder / "reference.png"
            Image.new("RGB", (640, 480), "#D1B46A").save(picture)
            job = {"id": "image-task", "name": "产品换品", "kind": "ai_image", "status": "success", "stage": "图片已保存",
                   "created": time.time(), "updated": time.time(),
                   "config": {"model": "gpt-image-2.5-sunburst", "prompt": "保持原图照明，替换产品",
                              "parameters": {"resolution": "1K"}, "image_inputs": [{"path": str(picture)}]},
                   "result": {"asset_path": str(picture), "output": str(picture)}}
            app.desktop_store._put("jobs", job)
            app._show_page("ai_image"); app._ai_select_result(job["id"]); pump()
            assert app.ai_feedback_button.cget("state") == "normal"
            feedback = app._assistant_feedback(job)
            feedback.feedback_box.insert("1.0", "瓶身反光过强，与场景光线不匹配。")
            feedback.feedback_save(); pump(lambda: not app._assistant_busy)
            assert len(app.assistant_store.read()["sources"]) == 1
            assert not app.assistant_store.read()["entries"]
            report["feedback_off_saves_without_analysis"] = True

            panel = app._open_assistant("artist"); pump()
            app._assistant_auto_switch.toggle(); pump()
            assert app.assistant_store.enabled("artist")
            model = Mock(return_value={"note": "候选经验，修改效果尚未验证。", "suggestions": [{"kind": "case", **content()}]})
            def review(store, identity, credentials, progress, **kwargs):
                return analyze_source(store, identity, credentials, progress, ask_fn=model, **kwargs)
            with patch("assistant_page.analyze_source", side_effect=review):
                feedback = app._assistant_feedback(job)
                feedback.feedback_box.insert("1.0", "瓶身反光过强，但这次手指接触合理，请只改反光。")
                feedback.feedback_save(); pump(lambda: not app._assistant_busy)
            model.assert_called_once()
            entries = list(app.assistant_store.read()["entries"].values())
            assert len(entries) == 1 and entries[0]["draft"] and not entries[0]["active"]
            assert app.assistant_store.approved("artist") == []
            app._assistant_tabs.set("待确认"); capture(panel, "AI美工-待确认.png")
            report["feedback_on_creates_pending_only"] = True

            editor = app._assistant_edit("artist", entries[0]); pump()
            editor.assistant_fields["content"].delete("1.0", "end")
            editor.assistant_fields["content"].insert("1.0", "匹配场景的反光方向和强弱，保留已经认可的手指接触关系。")
            capture(editor, "AI美工-规则编辑.png")
            editor.assistant_save(False); pump()
            assert app.assistant_store.approved("artist") == []
            entry = app.assistant_store.read()["entries"][entries[0]["id"]]
            editor = app._assistant_edit("artist", entry); editor.assistant_save(True); pump()
            assert "保留已经认可" in app.assistant_store.approved("artist")[0]["content"]
            report["visible_edit_and_explicit_confirmation"] = True

            prompt_model = Mock(return_value={"prompt": "按原场景光照替换产品，保留握持接触。", "note": "依据当前需求处理。"})
            def prompt(*args, **kwargs): return generate_prompt(*args, **kwargs, ask_fn=prompt_model)
            app.ai_requirements.insert("1.0", "亮面瓶身换品，匹配原光线")
            with patch("ai_image_page.generate_prompt", side_effect=prompt):
                app._ai_generate_helper(); pump(lambda: not app.ai_image_busy)
            payload = json.loads(prompt_model.call_args.args[2][0]["text"])
            assert len(payload["已确认个人内容"]) == 1
            report["confirmed_content_reaches_real_prompt_builder"] = True
            active = app.assistant_store.read()["entries"][entry["id"]]
            app._assistant_decide(active, "disable"); pump()
            assert app.assistant_store.approved("artist") == []
            app._assistant_tabs.set("工作规则"); capture(panel, "AI美工-生效内容与停用.png")
            events = app.assistant_store.read()["history"]
            event = next(e for e in reversed(events) if e["action"] == "确认并启用")
            history = app._assistant_history(event); capture(history, "变更记录-前后对照.png"); history.destroy()
            report["disable_and_revision_comparison"] = True

            sources = list(app.assistant_store.read()["sources"].values())
            evidence = app._assistant_view_source(sources[-1]); pump()
            capture(evidence, "学习资料-原图结果与反馈.png"); evidence.destroy()
            panel = app._open_assistant("director"); pump()
            dialog = app._assistant_add_dialog("director"); pump()
            pictures = []
            for i in range(8):
                path = folder / f"learning-{i}.png"
                Image.new("RGB", (320, 240), (100 + i * 15, 130, 80)).save(path); pictures.append(str(path))
            with patch("assistant_page.filedialog.askopenfilenames", return_value=pictures):
                button(dialog, "选择素材").invoke()
            next(w for w in descendants(dialog) if isinstance(w, ctk.CTkEntry)).insert(0, "乳霜")
            next(w for w in descendants(dialog) if isinstance(w, ctk.CTkTextbox)).insert("1.0", "学习产品展示的起始状态")
            button(dialog, "保存学习资料").invoke(); pump(lambda: not app._assistant_busy)
            source = next(s for s in app.assistant_store.read()["sources"].values() if s["role"] == "director")
            assert len(source["media"]) == 8 and source["analysis"] == "new"
            close_children()
            evidence = app._assistant_view_source(source); pump()
            button(evidence, "下一页").invoke(); pump()
            assert any(isinstance(w, ctk.CTkLabel) and w.cget("text") == "学习图片 8" for w in descendants(evidence))
            capture(evidence, "AI导演-学习素材分页.png"); evidence.destroy()
            report["material_picker_save_and_evidence_pagination"] = True
            model = Mock(return_value={"note": "依据画面整理；未读取音频。", "suggestions": [{"kind": "demo", **content(title="产品展示起始条件", modes=["use"])}]})
            with patch("assistant_page.analyze_source", side_effect=review):
                app._assistant_analyze(source["id"]); pump(lambda: not app._assistant_busy)
            pending = next(e for e in app.assistant_store.read()["entries"].values() if e["role"] == "director")
            assert not app.assistant_store.approved("director")
            editor = app._assistant_edit("director", pending); editor.assistant_save(True); pump()
            assert not app.assistant_store.approved("director", mode="talk")
            assert len(app.assistant_store.approved("director", mode="use")) == 1
            app._assistant_tabs.set("工作规则"); capture(panel, "AI导演-演示与适用模式.png")
            report["director_learning_separate_scope_and_mode"] = True

            legacy = app._assistant_existing_library(); pump()
            legacy_text = next(w for w in descendants(legacy) if isinstance(w, ctk.CTkTextbox)).get("1.0", "end-1c")
            assert "品类：" in legacy_text and "限制：" in legacy_text
            capture(legacy, "AI导演-已有演示库.png")
            button(legacy, "为此补充个人规则").invoke(); pump()
            supplement = next(w for w in root.winfo_children() if hasattr(w, "assistant_fields"))
            assert "基于已有资料" in supplement.assistant_fields["evidence"].get("1.0", "end-1c")
            supplement.destroy(); legacy.destroy()
            report["existing_library_readable_and_supplementable"] = True

            for i in range(21): app.assistant_store.propose("director", "rule", content(title=f"分页规则 {i + 1}"))
            app._assistant_reload(); app._assistant_tabs.set("待确认"); pump()
            app._assistant_turn_page("待确认", 1); pump()
            assert "第 2 / 2 页" in app._assistant_page_labels["待确认"].cget("text")
            assert len(app._assistant_lists["待确认"].winfo_children()) == 1
            report["pending_list_pagination"] = True
            app._assistant_busy = True
            with patch("assistant_page.messagebox.showinfo"), patch("video_gui.messagebox.showinfo") as message:
                app._request_exit(); message.assert_called_once()
            app._assistant_busy = False
            assert not app._closed
            report["exit_waits_for_learning_save"] = True
            assert not errors
            report["no_tk_errors"] = True
        finally:
            app._closed = True
            app.ai_display.close(wait=True)
            app.tasks.stop(); app.tasks.wait_idle()
            app.fish_audio_player.stop(); app.voice_player.stop(); app.canvas_service.stop()
            root.destroy(); app.desktop_store.db.close()
    (output / "验收结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__": main()
