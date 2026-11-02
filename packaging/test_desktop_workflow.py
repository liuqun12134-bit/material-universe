"""Real Tk/media/SQLite integration, with only paid provider execution replaced."""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))
import customtkinter as ctk
from PIL import Image
from video_gui import MaterialUniverseApp
from desktop_tasks import DesktopStore, TaskManager
from model_runner.adapters.fal_base import FalQueueAdapter


def main():
    report = {"network_called": False}
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        video, pic, audio = folder / "source.mp4", folder / "product.png", folder / "audio.wav"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x288:r=24",
                        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        Image.new("RGB", (160, 160), "white").save(pic)
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b"\0" * 64000)
        root = ctk.CTk(); root.withdraw(); root.attributes("-alpha", 0)
        app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
        # Exercise the production scheduler/provider path with isolated credentials.
        from desktop_tasks import execute_job
        app.tasks.execute = execute_job
        # Avoid importing unrelated existing outputs or recovering real tasks during this test.
        app.workspace_output_dir_var.set(str(folder / "outputs"))
        app.provider_vars["fal"]["key_var"].set("offline-test-key")
        app.tasks.set_limit(10)
        app.prompt_mode_var.set("使用已有提示词")
        exact_prompt = "只换产品，其他保持不变。\n第二行保留。"
        app.reuse_prompt_box.insert("1.0", exact_prompt)
        app._load_source_video(video); app._load_reference_image(pic)
        app._load_lipsync_video(video); app._load_lipsync_audio(audio); app._load_repair_video(video)
        def pump_until(test, seconds=12):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if test(): return
                time.sleep(.02)
            raise AssertionError("GUI integration timed out")
        pump_until(lambda: app.source_video_var.get() and app.reference_image_var.get() and
                   app._lipsync_audio_seconds is not None and app._lipsync_video_seconds is not None and
                   app._repair_video_info is not None)
        # Selection already saved managed copies, so queued inputs survive moves of originals.
        assert Path(app.source_video_var.get()).parent == app.desktop_store.files
        pic.unlink(); audio.unlink()
        report["inputs_saved_before_submit"] = True
        gate = threading.Event()
        reached = []
        lock = threading.Lock()
        def fake_provider(adapter, request, credential):
            with lock: reached.append((request.model.adapter, request.prompt, str(request.output)))
            request.task_record.update(task_id="offline-" + request.output.stem, status="submitted")
            request.progress("本地验收：模拟生成中")
            gate.wait(8)
            if request.model.adapter == "fal_flashvsr":
                from model_runner.task_state import TaskFailed
                raise TaskFailed("模拟服务商失败")
            request.output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(video, request.output)
            return {"local_downloaded": True, "output": str(request.output), "video_url": "https://example.invalid/offline.mp4"}
        try:
            with patch.object(FalQueueAdapter, "execute", fake_provider), patch("requests.Session") as network, patch("video_gui.messagebox.showinfo") as acknowledgement:
                for i in range(12):
                    mode = ("full", "lipsync", "repair")[i % 3]
                    app._submission_drafts[mode]["not_before"] = 0
                    start = (app._start_full_pipeline, app._start_lipsync, app._start_repair)[i % 3]
                    start()
                    start(); start()  # Rapid repeats only locate the same task.
                    assert len(app.desktop_store.jobs()) == i + 1
                pump_until(lambda: len(reached) == 10)
                assert len(app.tasks.active) == 10 and len(app.tasks.pending) == 2
                report["ten_concurrent_two_queued"] = True
                report["three_modules_duplicate_submit_blocked"] = True
                assert acknowledgement.call_count == 12
                report["confirm_dialog_releases_form_for_concurrent_submit"] = True
                # Editing the form cannot change existing job snapshots.
                app.reuse_prompt_box.delete("1.0", "end"); app.reuse_prompt_box.insert("1.0", "下一次提示词")
                root.deiconify(); root.update()
                app._on_close()
                pump_until(lambda: app.tray and app.tray.is_ready and root.state() == "withdrawn")
                assert app.tray.icon.visible and not app.tasks.stopping
                assert len(app.tasks.active) == 10
                gate.set(); pump_until(lambda: not app.tasks.active and not app.tasks.pending)
                opener = threading.Thread(target=app.tray.open_window)
                opener.start(); opener.join()
                assert root.state() == "withdrawn"  # Background callback only enqueues.
                pump_until(lambda: root.state() == "normal")
                app._refresh_submission_feedback()
                assert "使用记录" in app._submission_status["repair"].get()
                pump_until(lambda: app._submission_buttons["full"].cget("state") == "normal")
                assert app._submission_buttons["full"].cget("text") == "一键开始换品"
                assert app._submission_buttons["repair"].cget("text") == "开始修复画质"
                report["tray_hide_keeps_tasks_running_and_queue_restore"] = True
                jobs = app.desktop_store.jobs()
                assert len(jobs) == 12
                assert sum(j["status"] == "success" for j in jobs) == 8
                assert sum(j["status"] == "failed" for j in jobs) == 4
                assert all(prompt == exact_prompt for kind, prompt, _ in reached if kind == "fal_wan3")
                assert len({output for _, _, output in reached}) == 12
                assert len(app.desktop_store.assets()) == 3
                # The same main button creates a new task after completion, with no preparation button.
                previous_request = app._submission_drafts["full"]["request_id"]
                app._submission_drafts["full"]["not_before"] = 0
                app.reuse_prompt_box.delete("1.0", "end"); app.reuse_prompt_box.insert("1.0", exact_prompt)
                app.full_button.invoke(); pump_until(lambda: not app.tasks.active)
                assert len(app.desktop_store.jobs()) == 13
                assert app._submission_drafts["full"]["request_id"] != previous_request
                report["same_button_generates_again_after_completion"] = True
                # History reuse still only fills the form.
                job = next(j for j in jobs if j["kind"] == "full")
                app._refresh_history(); app.history_tree.selection_set(job["id"]); app._reuse_selected()
                pump_until(lambda: app.source_video_var.get() and app.reference_image_var.get())
                assert app.reuse_prompt_box.get("1.0", "end-1c") == exact_prompt
                assert len(app.desktop_store.jobs()) == 13
                # A lost local submit response retains frozen config/output/request ID.
                app._prepare_next_submission("full")
                original_submit = app.tasks.submit
                def commit_then_lose_response(*args, **kwargs):
                    identity = original_submit(*args, **kwargs)
                    # A reentrant activation during synchronous submit must be ignored.
                    app._start_full_pipeline()
                    raise OSError("模拟本地提交响应丢失")
                with patch.object(app.tasks, "submit", commit_then_lose_response), patch("video_gui.messagebox.showerror"):
                    app._start_full_pipeline()
                draft = app._submission_drafts["full"]
                frozen = json.loads(json.dumps(draft["config"]))
                request_id = draft["request_id"]
                app.source_video_var.set("")
                app.provider_vars["fal"]["key_var"].set("")
                app.prompt_model_var.set("edited-model")
                app._start_full_pipeline(); pump_until(lambda: not app.tasks.active)
                assert len(app.desktop_store.jobs()) == 14
                assert draft["request_id"] == request_id and draft["config"] == frozen
                app.history_filter.set("画质修复"); app.history_search.set("cannot-find-this")
                app._submission_drafts["full"] = draft  # Inspect original identity retained in history.
                app._view_submission("full")
                assert app.history_filter.get() == "全部功能" and app.history_search.get() == ""
                assert app.history_tree.selection() == (draft["job_id"],)
                report["uncertain_submit_retries_same_request_and_snapshot"] = True
                # An uncertain provider result routes to the original task, never creates a new one.
                app.desktop_store.update(draft["job_id"], status="attention", stage="需要核实或继续原任务")
                draft["submitted_at"] -= 2
                app._refresh_submission_feedback()
                assert app.full_button.cget("text") == "查看任务"
                app.full_button.invoke()
                assert len(app.desktop_store.jobs()) == 14
                assert app.history_tree.selection() == (draft["job_id"],)
                app.desktop_store.update(draft["job_id"], status="success", stage="成片已保存")
                report["uncertain_remote_result_keeps_original_task"] = True
                # Deleting a finished record keeps event polling alive and permits a fresh click later.
                app.history_tree.selection_set(draft["job_id"])
                with patch("desktop_pages.messagebox.askyesno", return_value=True):
                    app._delete_selected()
                assert "记录已删除" in app._submission_status["full"].get()
                app.events.put(("tasks_changed", None)); app.events.put(("status", "删除后事件仍处理"))
                pump_until(lambda: app.status_var.get() == "删除后事件仍处理")
                draft["submitted_at"] -= 2
                assert app._handle_existing_submission("full") is False
                assert len(app.desktop_store.jobs()) == 13
                report["deleted_record_allows_new_draft_and_events_alive"] = True
                # Explicit exit: cancellation and failed canvas save cannot stop tasks.
                app.provider_vars["fal"]["key_var"].set("offline-test-key")
                app.tasks.set_limit(1); gate.clear()
                for _ in range(2):
                    app._prepare_next_submission("lipsync"); app._start_lipsync()
                pump_until(lambda: len(app.tasks.active) == 1 and len(app.tasks.pending) == 1)
                queued_id = app.tasks.pending[0][0]
                with patch("video_gui.messagebox.askyesno", return_value=False), patch.object(app.canvas_window, "request_stop") as canvas_stop:
                    app._on_close()
                    app.tray.exit_app()
                    pump_until(lambda: root.state() == "normal")
                    canvas_stop.assert_not_called()
                assert not app.tasks.stopping and app.tray.is_ready and app.tray.icon.visible
                with patch("video_gui.messagebox.askyesno", return_value=True), patch("video_gui.messagebox.showerror"), patch.object(app, "_show_page"), patch.object(app.canvas_window, "request_stop", side_effect=lambda callback: callback(False)):
                    app._request_exit()
                assert not app.tasks.stopping and root.winfo_exists()
                assert app.tray.is_ready and app.tray.icon.visible and not app._exit_in_progress
                with patch("video_gui.messagebox.askyesno", return_value=True), patch.object(app.canvas_window, "request_stop", side_effect=lambda callback: callback(True)), patch.object(root, "destroy") as destroy:
                    app._request_exit(); destroy.assert_called_once()
                assert app.tasks.stopping and not app.tasks.pending
                assert not app.tray.is_ready and not app.tray.icon.visible and not app.tray.thread.is_alive()
                report["tray_cancel_save_failure_retains_icon_and_exit_removes_it"] = True
                assert app.desktop_store.job(queued_id)["status"] == "paused"
                gate.set(); assert app.tasks.wait_idle(8)
                report["exit_cancel_save_guard_and_pending_persistence"] = True
                network.assert_not_called()
                report.update(three_modules=True, error_isolation=True, immutable_prompt=True,
                              library_deduplicated=True, reuse_without_restart=True)
            app.tasks.stop(); app.desktop_store.db.close(); root.destroy()
            reopened = DesktopStore(folder / "state")
            manager = TaskManager(reopened, execute=lambda *args: (_ for _ in ()).throw(AssertionError("unexpected restart")))
            assert len(reopened.jobs()) == 15 and len(reopened.assets()) == 3
            assert not manager.active
            manager.stop(); reopened.db.close()
            report["records_survive_restart"] = True
            report["success"] = True
        finally:
            gate.set()
            if app.tray:
                app.tray.stop()
            if not report.get("success"):
                app.tasks.stop()
                app.tasks.wait_idle(10)
                app.tasks.stop()
                app.desktop_store.db.close()
                root.destroy()
        (ROOT / ".build/task-ui-integration.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
