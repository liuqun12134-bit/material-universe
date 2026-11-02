"""Real Tk, media and shared queue acceptance. All network access is denied."""
import copy
import json
import os
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
from PIL import Image, ImageGrab
from video_gui import MaterialUniverseApp
from image_audio_video import MEDIA_FIELDS, IMAGE_FIELDS, generation_args, digest_file


def main():
    report = {}
    with tempfile.TemporaryDirectory() as temp, patch("requests.sessions.Session.request", side_effect=AssertionError("Network not allowed")):
        folder = Path(temp)
        root = ctk.CTk()
        root.withdraw()
        app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
        app.workspace_output_dir_var.set(str(folder / "output"))
        app.tasks.set_limit(2)
        gate = threading.Event()
        reached = []
        def execute(job, credentials, update, store, resume):
            reached.append(job)
            gate.wait(15)
            return {"local_downloaded": False, "output": None}
        app.tasks.execute = execute
        def pump_until(test, seconds=10):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if test(): return
                time.sleep(.01)
            raise AssertionError("UI timed out")
        try:
            root.geometry("1080x740")
            app._show_page("image_audio")
            assert app.fish_ratio.get() == "9:16" and app.fish_resolution.get() == "480p"
            with patch("image_audio_page.messagebox.showerror") as error:
                app._fish_submit()
                assert error.called and not app.desktop_store.jobs()
            for index, field in enumerate(IMAGE_FIELDS):
                path = folder / (field + ".png")
                Image.new("RGB", (480, 640), (index * 55 + 40, 120, 180)).save(path)
                app._fish_load(field, path)
            audio = folder / "音频测试.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                wav.writeframes(b"\0\0" * int(4.1 * 16000))
            app._fish_load("audio", audio)
            pump_until(lambda: len(app.fish_media) == 4)
            assert app.fish_duration.get() == "5"
            assert all(Path(app.fish_media[field]["path"]).parent == app.desktop_store.files for field in MEDIA_FIELDS)
            app.fish_audio_player.state_callback(True, False)
            assert app.fish_audio_play_label.get() == "暂停"
            app.fish_audio_player.state_callback(False, False)
            report["required_inputs_and_real_media_inspection"] = True
            app._handle_fish_event(("account", {"workspaces": [{"workspace_id": "offline-workspace", "workspace_name": "离线验收工作区", "team_name": "验收", "is_default": True}]}, None))
            app.fish_prompt.insert("1.0", "参考 @图1 的开场构图，保持 @图2 的人物和 @图3 的产品外观。\n跟随 @音频1 自然讲解并展示产品。")
            def quote():
                snapshot = app._fish_form()
                config = copy.deepcopy(snapshot)
                config.update(duration=int(config["duration"]), output=str(folder / "output" / "图音生视频.mp4"),
                              connection_id="offline", reference_assets=[config[f] for f in MEDIA_FIELDS],
                              media_hashes={f: digest_file(config[f]) for f in MEDIA_FIELDS})
                args = generation_args(config, [{"role": "audioRef" if f == "audio" else "ref", "object_key": f} for f in MEDIA_FIELDS])
                config["fish_quote"] = {"arguments": args, "credits": 10808, "pricing_version": "offline", "balance": 100000}
                return snapshot, config
            # Cancel is free; changing a form while a quote is in flight invalidates it.
            with patch("image_audio_page.messagebox.askyesno", return_value=False) as approve:
                app.fish_busy = True
                app._handle_fish_event(("quote", *quote(), None))
                assert approve.called and not app.desktop_store.jobs()
            snapshot, config = quote()
            app.fish_ratio.set("16:9")
            with patch("image_audio_page.messagebox.askyesno") as approve:
                app._handle_fish_event(("quote", snapshot, config, None))
                assert not approve.called and not app.desktop_store.jobs()
            app.fish_ratio.set("9:16")
            report["cancel_and_quote_invalidation"] = True
            # Three paid intents: two workers, one queue; form available immediately.
            with patch("image_audio_page.messagebox.askyesno", return_value=True), patch("image_audio_page.messagebox.showinfo"):
                for _ in range(3):
                    app.fish_busy = True
                    app._handle_fish_event(("quote", *quote(), None))
                    assert not app.fish_busy
                assert len(app.tasks.active) == 2 and len(app.tasks.pending) == 1
                assert len(app.desktop_store.jobs()) == 3
                before = len(app.desktop_store.jobs())
                app._fish_submit()  # queued double click is ignored by cooldown.
                assert len(app.desktop_store.jobs()) == before
            report["two_workers_one_queued_and_double_click_guard"] = True
            config = app.desktop_store.jobs()[0]["config"]
            app.fish_prompt.insert("end", "下一次改动")
            assert "下一次改动" not in app.desktop_store.jobs()[0]["config"]["prompt"]
            report["submitted_configuration_frozen"] = True
            root.attributes("-topmost", True)
            root.deiconify(); root.lift(); root.update()
            destination = ROOT / "发行版/维护资料/验收记录/1.9.0-创作改版"
            destination.mkdir(parents=True, exist_ok=True)
            def capture(name):
                root.update(); time.sleep(.2)
                x, y = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(destination / name)
            app.image_audio_page._parent_canvas.yview_moveto(0)
            capture("图音生视频-素材.png")
            app.image_audio_page._parent_canvas.yview_moveto(1)
            capture("图音生视频-参数.png")
            app._show_page("history"); root.update()
            assert app.history_tree.get_children()
            capture("图音生视频-队列.png")
            report["minimum_window_and_history"] = True
            (destination / "离线验收.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        finally:
            gate.set(); app.tasks.wait_idle(5)
            app._closed = True
            if app.tray: app.tray.stop()
            app.tasks.stop(); app.tasks.owner.close()
            app.fish_audio_player.stop()
            root.destroy()
            app.desktop_store.db.close()


if __name__ == "__main__": main()
