"""Stress image switching without network or user data; record layout and responsiveness."""
import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))


def main():
    import customtkinter as ctk
    from PIL import Image, ImageDraw, ImageGrab
    from video_gui import MaterialUniverseApp
    from fission_service import inspect_image
    from ai_image_models import DEFAULT_MODEL
    output = ROOT / "output/ai-image-navigation"
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".build/ai-image-test-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    errors, samples, decode_threads, layout_events = [], [], [], []
    with tempfile.TemporaryDirectory(dir=scratch) as temp, \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Network prohibited")), \
            patch.object(MaterialUniverseApp, "_ensure_tray"):
        folder = Path(temp)
        paths = []
        for i, size in enumerate([(4096, 2304), (2304, 4096), (3072, 3072), (4096, 1024),
                                  (1024, 4096), (3000, 2000), (2000, 3000), (4000, 2600)]):
            path = folder / f"offline-{i}.png"
            source = Image.new("RGB", size, (60 + i * 20, 110, 170))
            ImageDraw.Draw(source).rectangle((size[0] // 5, size[1] // 5, size[0] * 4 // 5, size[1] * 4 // 5), outline="white", width=30)
            source.save(path); paths.append(path)
        root = ctk.CTk(); root.withdraw()
        app = MaterialUniverseApp(root, data_root=folder / "data", demo=True)
        root.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))
        root.geometry("1440x900+0+0")
        left = app.ai_form_panel._parent_frame.master
        right = app.ai_preview.master
        body = right.master

        def layout():
            return {key: [w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height()]
                    for key, w in (("body", body), ("left", left), ("right", right), ("preview", app.ai_preview))}

        def pump(seconds=.15):
            stop = time.monotonic() + seconds
            while time.monotonic() < stop:
                root.update(); time.sleep(.005)
            if errors: raise AssertionError(errors)

        def sample(label, action):
            before = time.perf_counter(); action(); elapsed = time.perf_counter() - before
            pump(.15)
            samples.append({"label": label, "callback_ms": round(elapsed * 1000, 2), "layout": layout()})

        def ready():
            stop = time.monotonic() + 10
            while time.monotonic() < stop:
                pump(.03)
                if not app._ai_preview_pending and app._ai_preview_image is not None: return
            raise AssertionError("preview did not finish")

        try:
            refs = []
            for i, path in enumerate(paths):
                asset = app.desktop_store.import_asset(path)
                refs.append({**inspect_image(asset["path"]), "asset_id": asset["id"], "name": asset["name"]})
                app.desktop_store._put("jobs", {"id": f"result-{i}", "name": f"测试图片 {i}", "created": time.time() + i,
                    "updated": time.time(), "kind": "ai_image", "status": "success", "stage": "图片已保存",
                    "config": {"model": DEFAULT_MODEL}, "result": {"asset_id": asset["id"], "asset_path": asset["path"]}})
            app.ai_image_store.add_references(refs[:3])
            app.ai_image_store.fields(prompt="保持人物、场景和自然光线。" * 100, selected_job="result-0")
            root.deiconify(); app._show_page("ai_image"); pump(.5); ready()
            baseline = layout()
            card_widgets = {key: card["panel"] for key, card in app._ai_result_cards.items()}
            right.bind("<Configure>", lambda _: layout_events.append(layout()), add=True)
            real_open = Image.open
            def observed_open(*args, **kwargs):
                decode_threads.append(threading.current_thread().name)
                return real_open(*args, **kwargs)
            with patch("PIL.Image.open", side_effect=observed_open):
                for i in range(8): sample(f"result-{i}", lambda i=i: app._ai_select_result(f"result-{i}"))
                for i in range(3): sample(f"reference-{i}", lambda i=i: app._ai_preview_reference(i))
                for i in (7, 0, 3, 5, 1, 6): sample(f"again-{i}", lambda i=i: app._ai_select_result(f"result-{i}"))
                for i in range(8): app._ai_select_result(f"result-{i}")
                ready()
                assert app._ai_selected_job()["id"] == "result-7"
                image_id = app.ai_preview.find_all()[0]
                pixel = root.tk.call(app.ai_preview.itemcget(image_id, "image"), "get", 0, 0)
                assert tuple(pixel) == (200, 110, 170), "late render replaced the newest selection"
                # A transient unreadable file must not leave this selection stuck.
                selected_path = Path(app._ai_selected_path())
                original_bytes = selected_path.read_bytes()
                selected_path.write_bytes(b"incomplete image")
                app._ai_select_result("result-7")
                deadline = time.monotonic() + 10
                while app._ai_preview_pending and time.monotonic() < deadline: pump(.03)
                assert not app._ai_preview_pending and app._ai_preview_image is None
                assert not app._ai_preview_available
                selected_path.write_bytes(original_bytes)
                app._ai_select_result("result-7"); ready()
                assert app._ai_preview_available and app.ai_zoom_buttons["in"].cget("state") == "normal"
            # Long task messages cannot push zoom controls out of the fixed header.
            job = app.desktop_store.job("result-0")
            job["stage"] = "图片已保存，详细说明。" * 30
            app.desktop_store._put("jobs", job)
            app._ai_select_result("result-0"); ready()
            header = app.ai_preview_title.master
            for button in app.ai_zoom_buttons.values():
                assert button.winfo_ismapped()
                assert button.winfo_rooty() + button.winfo_height() <= header.winfo_rooty() + header.winfo_height()
                assert button._text_label.winfo_ismapped(), "zoom symbol disappeared after redraw"
                assert button._text_label.winfo_width() >= button._text_label.winfo_reqwidth(), "zoom symbol was clipped"
            app._ai_select_result("result-7"); ready()
            assert all(app._ai_result_cards[key]["panel"] is widget for key, widget in card_widgets.items()), "selection rebuilt result cards"
            pump(.5)
            import ctypes
            ImageGrab.grab(window=ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)).save(output / "连续切图.png")
            report = {"baseline": baseline, "samples": samples, "main_thread_image_opens": decode_threads.count("MainThread"),
                      "max_callback_ms": max(s["callback_ms"] for s in samples), "layout_events": layout_events,
                      "latest_selection_wins": True, "cards_reused": True, "read_failure_retry": True,
                      "long_status_keeps_zoom_visible": True, "no_tk_errors": not errors}
            (output / "验收结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
            print(json.dumps(report, ensure_ascii=False))
            assert all(s["layout"] == baseline for s in samples), "image selection changed layout"
            assert all(s == baseline for s in layout_events), "transient layout resize"
            assert not decode_threads.count("MainThread"), "image decoding blocks the UI thread"
            assert report["max_callback_ms"] < 150, "image selection blocks the UI"
        finally:
            app._closed = True
            if hasattr(app, "ai_display"): app.ai_display.close(wait=True)
            app.tasks.stop(); app.tasks.wait_idle()
            app.fish_audio_player.stop(); app.voice_player.stop(); app.canvas_service.stop()
            root.destroy(); app.desktop_store.db.close()


if __name__ == "__main__": main()
