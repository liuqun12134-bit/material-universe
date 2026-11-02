"""Real-Tk voice acceptance with isolated storage and prohibited network calls."""
import copy
import json
import tempfile
import time
import traceback
import wave
from pathlib import Path
from unittest.mock import Mock, patch


def run(output_dir=None):
    import customtkinter as ctk
    from PIL import ImageGrab
    from video_gui import MaterialUniverseApp

    result = {}
    scratch = Path.cwd() / "output/voice-acceptance-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as folder, \
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
        app._voice_connection = Mock(return_value="offline-connection")
        app.voice_connection_id = "offline-connection"
        app.fish_account_status.set("Fish Audio 已连接 · 离线演示团队")
        errors = []
        root.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

        def pump(predicate=lambda: True, seconds=5):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(.02)
            raise AssertionError("UI timeout: " + repr(errors))

        def capture(name, window=None):
            if not output_dir:
                return
            destination = Path(output_dir)
            destination.mkdir(parents=True, exist_ok=True)
            window = window or root
            window.attributes("-topmost", True)
            window.deiconify(); window.lift(); root.update(); time.sleep(.2)
            x, y = window.winfo_rootx(), window.winfo_rooty()
            ImageGrab.grab((x, y, x + window.winfo_width(), y + window.winfo_height())).save(destination / name)

        def snapshot():
            form = app._voice_form()
            prepared = {**copy.deepcopy(form), "output": str(folder / "outputs/声音合成.wav"),
                        "connection_id": "offline-connection", "fish_quote": {
                            "credits": 90, "version_credits": [45, 45], "balance": 1000, "created": time.time()},
                        "variants": []}
            for index, block in enumerate(("block-a", "block-b"), 1):
                prepared["variants"].append({**copy.deepcopy(form), "output": str(folder / f"outputs/版本-{index}.wav"),
                    "connection_id": "offline-connection", "variant_backend": "studio", "variant_index": index,
                    "variant_label": f"版本 {'AB'[index - 1]}", "studio_project_id": "offline-project",
                    "studio_block": block, "studio_revision": "revision-one", "fish_quote": {
                        "credits": 45, "arguments": {"project_id": "offline-project", "blocks": [block],
                            "force": True, "only_dirty": False}, "text": form["text"], "voice_id": form["voice_id"],
                        "model": "drama-3", "speed": form["speed"], "volume_db": form["volume_db"],
                        "audio_settings_mode": "fish", "created": time.time()}})
            return form, prepared

        def quote_event(form, prepared, error=None):
            app.voice_busy = True
            app.voice_submit_button.configure(state="disabled")
            app._handle_voice_event(("quote", (form,), prepared, error))

        voices = [
            {"id": "offline-voice-one", "title": "我的日常口播", "sample_url": "https://example.invalid/one.mp3",
             "description": "室内小场景，口齿清晰，平淡语气的日常口播，适合产品介绍。"},
            {"id": "offline-voice-two", "title": "我的日常口播", "sample_url": "",
             "description": "语速稍慢，带一点轻松笑意；适合浴室场景里的自然分享。"},
            {"id": "offline-voice-three", "title": "轻柔讲解", "sample_url": "https://example.invalid/three.mp3",
             "description": "声音轻柔，停顿自然，适合护肤品使用步骤的细致讲解。"},
            {"id": "offline-voice-four", "title": "活力推荐", "sample_url": "https://example.invalid/four.mp3",
             "description": "明亮、有活力，语气热情但不过分夸张。"},
        ]
        try:
            root.geometry("1080x740")
            root.deiconify()
            app._show_page("voice"); pump()
            assert app.current_page == "voice"
            assert app.voice_view.get() == "我的声音"
            assert app.voice_library_panel.winfo_ismapped()
            assert not app.voice_speech_panel.winfo_ismapped()
            assert app.voice_tab_select.cget("values") == ["我的声音", "声音合成"]
            assert not hasattr(app, "voice_model_menu")
            app._handle_voice_event(("list", (app.voice_list_token, 1),
                                    {"voices": voices, "has_next": True}, None))
            pump()
            assert len(app.voice_rows) == 4 and len(app.voice_choices) == 4
            assert app.voice_previous.cget("state") == "disabled"
            assert app.voice_next.cget("state") == "normal"
            assert not app.voice_selection.get() in app.voice_choices
            assert [label.cget("text") for label in app.voice_description_labels] == [voice["description"] for voice in voices]
            for geometry in ("1080x740", "1440x900"):
                root.geometry(geometry); pump(); app._voice_layout_changed(); pump()
                for label in app.voice_description_labels:
                    assert label.winfo_height() >= label.winfo_reqheight() - 2
                    assert label.winfo_width() >= label.winfo_reqwidth() - 2
                capture(f"我的声音-{geometry}.png")
            result["complete_voice_descriptions_at_two_window_sizes"] = True
            app._voice_select(voices[1])
            pump(lambda: app.voice_speech_panel.winfo_ismapped() and not app.voice_library_panel.winfo_ismapped())
            assert app.voice_view.get() == "声音合成"
            assert app.voice_speech_panel.winfo_ismapped()
            assert not app.voice_library_panel.winfo_ismapped()
            assert app._voice_form()["voice_id"] == voices[1]["id"]
            assert app.voice_selected_description.get() == voices[1]["description"]
            result["two_tabs_existing_voices_paging_and_unambiguous_selection"] = True

            app.voice_text.insert("1.0", "今天用温柔、自然的语气，讲解这款产品。")
            app._voice_update_char_count()
            assert app.voice_speed.get() == 1.0 and app.voice_volume.get() == 0.0
            for name, low, high in (("语速", .7, 1.3), ("音量", -5., 5.)):
                slider = app.voice_control_sliders[name]
                assert slider.cget("from_") == low and slider.cget("to") == high
                slider.set(low - 1)
                assert slider.get() == low
                slider.set(high + 1)
                assert slider.get() == high
            app._voice_reset_controls()
            assert app.voice_speed.get() == 1.0 and app.voice_volume.get() == 0.0
            result["fish_control_ranges_and_reset_defaults"] = True
            app.voice_speed.set(1.25); app.voice_volume.set(-2)
            form, prepared = snapshot()
            assert form["model"] == "drama-3" and form["speed"] == 1.25 and form["volume_db"] == -2
            assert form["audio_settings_mode"] == "fish" and "volume" not in form
            with patch.object(app, "_voice_worker") as worker:
                app._voice_submit()
                assert worker.call_args.args[0] == "quote"
                assert worker.call_args.args[2] == form
                assert app.voice_submit_button.cget("state") == "disabled"
            with patch("voice_page.messagebox.askyesno", return_value=False) as question, \
                    patch.object(app.tasks, "submit") as submit:
                quote_event(form, copy.deepcopy(prepared))
                question.assert_called_once(); submit.assert_not_called()
                assert "-2.0 dB" in question.call_args.args[1]
            assert not app.voice_busy and app.voice_submit_button.cget("state") == "normal"
            assert "取消" in app.voice_status.get()
            result["fixed_drama3_adjustable_controls_and_cancelled_quote"] = True

            app.voice_text.insert("end", "新增一句。")
            app._voice_update_char_count()
            with patch("voice_page.messagebox.askyesno") as question, patch.object(app.tasks, "submit") as submit:
                quote_event(form, copy.deepcopy(prepared))
                question.assert_not_called(); submit.assert_not_called()
            assert "改变" in app.voice_status.get()
            with patch("voice_page.messagebox.showerror"):
                quote_event(None, None, "离线测试：连接不可用")
            assert not app.voice_busy and app.voice_submit_button.cget("state") == "normal"
            result["inflight_form_change_and_quote_error_do_not_submit"] = True

            form, prepared = snapshot()
            with patch("voice_page.messagebox.askyesno", return_value=True), \
                    patch.object(app.tasks, "submit") as submit, patch.object(app, "_voice_worker") as worker:
                quote_event(form, copy.deepcopy(prepared))
                submit.assert_not_called()
                worker.assert_called_once()
                assert worker.call_args.args[0] == "batch_done"
                metadata = tuple(worker.call_args.args[2:])
                assert app.voice_busy
                app._voice_submit()
                worker.assert_called_once()
            result["one_pair_worker_no_video_queue_and_double_click_guard"] = True
            jobs = []
            for index, variant in enumerate(prepared["variants"], 1):
                output = folder / f"版本-{'AB'[index - 1]}.wav"
                with wave.open(str(output), "wb") as audio:
                    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                    audio.writeframes(b"\0\0" * (index * 16000))
                jobs.append({"id": f"voice-pair-{index}", "batch_id": "offline-batch", "variant_index": index,
                    "kind": "speech", "name": f"版本 {'AB'[index - 1]}", "status": "success", "stage": "音频已保存",
                    "created": time.time(), "asset_ids": [], "config": {**variant, "cost_approved": True},
                    "result": {"output": str(output), "output_kind": "audio", "duration": index,
                        "local_downloaded": True, "audio_url": f"https://example.invalid/result-{index}.wav"}})
            partial = copy.deepcopy(jobs)
            partial[1].update(status="failed", stage="版本 B 尚未完成", error="服务连接中断，未自动重新生成", result={})
            app._handle_voice_event(("batch_progress", metadata, partial, None)); pump()
            assert app.voice_result_widgets[0]["play"].cget("state") == "normal"
            assert app.voice_result_widgets[1]["play"].cget("state") == "disabled"
            assert app.voice_result_widgets[0]["save"].cget("state") == "normal"
            app._handle_voice_event(("batch_done", metadata, partial, None)); pump()
            assert not app.voice_busy and app.voice_result_jobs[0]["result"]["local_downloaded"]
            capture("声音合成-部分失败.png")
            result["partial_failure_keeps_successful_version_available"] = True
            for job in jobs:
                app.desktop_store._put("jobs", job)
            app._voice_render_results(jobs); pump()
            for widgets in app.voice_result_widgets:
                assert widgets["play"].cget("state") == "normal"
                assert widgets["save"].cget("state") == "normal" and widgets["library"].cget("state") == "normal"
            with patch.object(app.voice_player, "load") as load, patch.object(app.voice_player, "start") as start:
                app.voice_result_widgets[0]["play"].invoke()
                load.assert_called_once_with(Path(jobs[0]["result"]["output"])); start.assert_called_once()
            with patch.object(app, "_save_task_result") as save, patch.object(app.desktop_store, "import_asset") as library:
                app.voice_result_widgets[0]["save"].invoke()
                app.voice_result_widgets[1]["library"].invoke()
                save.assert_called_once_with(jobs[0]["result"]["output"])
                library.assert_called_once_with(jobs[1]["result"]["output"])
            result["two_inline_players_save_and_import_actions"] = True
            long_voice = {**voices[2], "description": "这段音色适合安静室内的产品讲解，声音轻柔，停顿自然，重点词语清楚。" * 3}
            root.geometry("1080x740"); pump()
            app._voice_select(long_voice); pump()
            assert app.voice_selected_description.get() == long_voice["description"]
            assert app.voice_submit_button.winfo_rooty() + app.voice_submit_button.winfo_height() <= root.winfo_rooty() + root.winfo_height() + 2
            assert app.voice_text.winfo_height() >= 60
            capture("声音合成-长备注.png")
            app._voice_select(voices[1]); pump()
            result["long_selected_voice_note_keeps_editor_usable"] = True
            for geometry in ("1080x740", "1440x900"):
                root.geometry(geometry); pump(); app._voice_layout_changed(); pump()
                for widget in [app.voice_submit_button] + [w[key] for w in app.voice_result_widgets for key in ("play", "save", "library")]:
                    assert widget.winfo_ismapped()
                    assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height() + 2
                    assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width() + 2
                assert app.voice_text.winfo_height() >= 60
                capture(f"声音合成-{geometry}.png")
            result["controls_and_both_results_visible_without_page_scrolling"] = True

            app._voice_open_clone(); pump()
            assert app.voice_clone_dialog.winfo_exists()
            assert not app.voice_clone_rights.get()
            with patch("voice_page.messagebox.showerror") as warning, patch.object(app, "_voice_worker") as worker:
                app._voice_create_clone()
                warning.assert_called_once(); worker.assert_not_called()
            app.voice_clone_title.set("新建口播音色")
            app.voice_clone_files = [str(folder / "仅测试引用.wav")]
            app.voice_clone_rights.set(True)
            app.voice_clone_info.set("我的口播录音.wav")
            capture("创建新音色.png", app.voice_clone_dialog)
            with patch.object(app, "_voice_worker") as worker:
                app._voice_create_clone()
                assert worker.call_args.args[0] == "clone"
            with patch("voice_page.messagebox.askyesno", return_value=False):
                app._handle_voice_event(("clone", (), {"status": "quality_warning", "preflight_id": "offline-preflight",
                    "instructions": "样本存在背景噪声，请检查后继续。"}, None))
            assert app.voice_clone_preflight == "offline-preflight" and not app.voice_clone_busy
            app._handle_voice_event(("clone", (), {"status": "verification_required", "preflight_id": "offline-preflight",
                "verification_prompt": "这是一段仅用于测试的验证句。"}, None))
            pump()
            assert "仅用于测试" in app.voice_verify_prompt.get("1.0", "end")
            assert app.voice_clone_submit.cget("state") == "disabled"
            assert app.voice_verification.winfo_manager() == "pack"
            app._handle_voice_event(("clone", (), None, "验证录音上传失败"))
            assert app.voice_clone_preflight == "offline-preflight"
            assert app.voice_clone_submit.cget("state") == "disabled"
            with patch.object(app, "_voice_worker") as worker:
                app._voice_create_clone()
                worker.assert_not_called()
                app._voice_refresh_verification()
                assert worker.call_args.args[0] == "clone"
            app._handle_voice_event(("clone", (), {"status": "verification_required",
                "verification_prompt": "这是一段仅用于测试的验证句。"}, None))
            assert app.voice_clone_preflight == "offline-preflight"
            result["verification_retry_preserves_session_and_does_not_clone_again"] = True
            capture("创建新音色-本人验证.png", app.voice_clone_dialog)
            app._voice_close_clone()
            assert app.voice_clone_dialog is None
            result["clone_rights_quality_warning_and_speaker_verification"] = True

            old_token = app.voice_list_token
            app._voice_reset_account()
            app._handle_voice_event(("list", (old_token, 2), {"voices": voices, "has_next": False}, None))
            assert not app.voice_rows and not app.voice_choices
            assert app.voice_selection.get() == "请选择音色" and not app.voice_loaded
            result["account_change_discards_stale_voices"] = True

            token = app.voice_sample_token = "pending-sample"
            with patch.object(app.voice_player, "start") as start:
                app._voice_stop_preview()
                app._handle_voice_event(("sample", (token,), str(folder / "sample.mp3"), None))
                start.assert_not_called()
            token = app.voice_sample_token = "failed-player"
            with patch.object(app.voice_player, "start", side_effect=OSError("missing player")):
                app._handle_voice_event(("sample", (token,), str(folder / "sample.mp3"), None))
            assert "无法播放" in app.voice_status.get()
            result["stopped_preview_and_player_failure_do_not_restart_or_break_events"] = True

            # A stored audio result must remain playable/saveable/reusable without network.
            output = folder / "历史声音.wav"
            with wave.open(str(output), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)
            job = {"id": "voice-history", "kind": "speech", "name": "示例口播音频", "status": "success",
                   "stage": "音频已保存", "created": time.time(), "asset_ids": [], "config": prepared,
                   "result": {"output": str(output), "output_kind": "audio", "local_downloaded": True}}
            app.desktop_store._put("jobs", job)
            app._show_page("history"); pump()
            app.history_tree.selection_set(job["id"]); app._history_selection()
            assert app.history_primary.cget("text") == "播放音频"
            app._task_detail(); pump()
            def labels_in(widget):
                labels = [widget.cget("text")] if isinstance(widget, ctk.CTkLabel) else []
                return labels + [text for child in widget.winfo_children() for text in labels_in(child)]
            assert any("-2.0 dB" in text for text in labels_in(app.history_drawer))
            capture("声音合成-使用记录.png")
            app._reuse_selected(); pump()
            assert app.current_page == "voice" and app.voice_view.get() == "声音合成"
            assert app._voice_form()["text"] == prepared["text"]
            assert app._voice_form()["voice_id"] == prepared["voice_id"]
            assert app._voice_form()["speed"] == prepared["speed"]
            assert app._voice_form()["volume_db"] == prepared["volume_db"]
            assert len(app.desktop_store.jobs()) == 3
            result["audio_history_details_and_reuse_preserve_voice_and_controls"] = True
            legacy = {"text": "旧版文案", "voice_id": prepared["voice_id"], "speed": 2., "volume": 200.}
            app._voice_reuse(legacy); pump()
            assert app._voice_form()["speed"] == 1.3 and app._voice_form()["volume_db"] == 5.
            assert "旧版" in app.voice_status.get() and "核对" in app.voice_status.get()
            assert legacy["speed"] == 2. and legacy["volume"] == 200.
            result["legacy_reuse_converts_volume_and_clamps_without_changing_history"] = True
            resumed = copy.deepcopy(jobs[0])
            resumed["config"].update(speed=.9, volume=80.)
            resumed["config"].pop("audio_settings_mode", None)
            resumed["config"].pop("volume_db", None)
            resumed["result"].update(speed=1., volume_db=0., audio_settings_mode="fish_original",
                                     legacy_requested_speed=.9, legacy_requested_volume=80.)
            app._voice_render_results([resumed]); pump()
            assert "原始音频" in app.voice_result_widgets[0]["note"].cget("text")
            with patch.object(app, "_selected_job", return_value=resumed):
                app._task_detail(); pump()
            details = labels_in(app.history_drawer)
            assert any("语速 1.0×" in text and "+0.0 dB" in text and "原始音频" in text for text in details)
            assert not any("语速 0.9×" in text for text in details)
            result["recovered_legacy_audio_displays_actual_original_settings"] = True
            assert not errors, errors
            result["minimum_window_and_tk_callbacks"] = True
            return result
        finally:
            app._closed = True
            if app.tray:
                app.tray.stop()
            app.canvas_service.stop()
            app.fish_audio_player.stop(); app.voice_player.stop()
            app.tasks.stop(); app.tasks.wait_idle(5)
            app.asset_tree.pool.shutdown(wait=True, cancel_futures=True)
            app.desktop_store.db.close()
            root.destroy()
