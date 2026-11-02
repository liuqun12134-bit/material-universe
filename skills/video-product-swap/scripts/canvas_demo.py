"""Isolated, offline canvas acceptance scene for source and packaged app."""
from __future__ import annotations

import math
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

from PIL import Image, ImageDraw

from canvas_backend import CanvasStore


DEMO_STOP = threading.Event()


def mock_execute(job, _credentials, update, _store, _resume=False):
    steps = ["本地演示：准备素材", "本地演示：上传完成", "本地演示：服务商生成中", "本地演示：下载成片"]
    delay = float(job["config"].get("demo_delay", 0.25))
    for step in steps:
        if DEMO_STOP.is_set():
            raise RuntimeError("本地演示已关闭")
        update(stage=step)
        if DEMO_STOP.wait(delay):
            raise RuntimeError("本地演示已关闭")
    config = job["config"]
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    if job["kind"] == "general":
        desired = config["output_kind"]
        source_asset = next((asset for asset in _store.assets() if asset["kind"] == desired), None)
        if source_asset is None:
            raise ValueError("本地演示缺少对应的示例媒体。")
        if desired == "image":
            with Image.open(source_asset["path"]) as source_image:
                source_image.save(output, format="PNG")
        else:
            shutil.copy2(source_asset["path"], output)
        return {"output": str(output), "output_kind": desired, "local_downloaded": True}
    source = Path(config.get("source") or config.get("video"))
    shutil.copy2(source, output)
    return {"output": str(output), "local_downloaded": True}


def _make_media(root):
    root.mkdir(parents=True, exist_ok=True)
    image = root / "演示产品图.png"
    canvas = Image.new("RGB", (840, 560), "#e8e0d5")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((305, 90, 535, 485), radius=45, fill="#a76743", outline="#694127", width=5)
    draw.rounded_rectangle((330, 65, 510, 150), radius=16, fill="#d8b292", outline="#805738", width=5)
    draw.ellipse((365, 220, 475, 330), fill="#efe8d7")
    canvas.save(image)
    audio = root / "演示配音.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(22050)
        for index in range(22050 * 4):
            stream.writeframesraw(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * index / 22050))))
    video = root / "演示视频.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100",
                    "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(video)],
                   check=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return video, image, audio


def seed_demo(app, root):
    raw_video, raw_image, raw_audio = _make_media(root / "示例文件")
    store = app.desktop_store
    video, image, audio = (store.import_asset(path) for path in (raw_video, raw_image, raw_audio))
    result_path = root / "画布成片" / "演示已完成.mp4"
    result_path.parent.mkdir(exist_ok=True)
    shutil.copy2(raw_video, result_path)
    success_request = uuid.uuid4().hex
    success_id = uuid.uuid4().hex
    now = time.time()
    store._put("jobs", {"id": success_id, "kind": "full", "name": "演示视频.mp4", "request_id": success_request,
                        "created": now - 180, "updated": now - 100, "finished": now - 100, "status": "success",
                        "stage": "成片已保存", "task_id": "offline-demo-completed", "actual_prompt": "保留原场景与镜头，只替换展示商品。",
                        "config": {"source": video["path"], "image": image["path"], "relation": "新产品与原产品外形尺寸相近。",
                                   "model": "wan3.0-video", "duration": 4, "aspect_ratio": "16:9", "resolution": "480p",
                                   "prompt_mode": "reuse", "prompt": "保留原场景与镜头，只替换展示商品。", "output": str(result_path)},
                        "asset_ids": [video["id"], image["id"]], "result": {"output": str(result_path), "local_downloaded": True},
                        "events": [{"time": now - 170, "stage": "本地演示：上传完成"},
                                   {"time": now - 120, "stage": "本地演示：下载成片"}]})
    running_request = uuid.uuid4().hex
    running_id = app.tasks.submit("lipsync", {"video": video["path"], "audio": audio["path"],
                                               "output": str(root / "画布成片" / "换口型演示.mp4"),
                                               "demo_delay": 45}, {}, request_id=running_request)
    canvases = CanvasStore(store)
    demo = canvases.create("示例创作流程")
    def node(identity, kind, x, y, data):
        return {"id": identity, "type": kind, "position": {"x": x, "y": y}, "data": data}
    nodes = [node("demo-video", "asset", 70, 80, {"assetId": video["id"]}),
             node("demo-image", "asset", 70, 395, {"assetId": image["id"]}),
             node("demo-audio", "asset", 70, 750, {"assetId": audio["id"]}),
             node("demo-success", "task", 440, 75, {"kind": "full", "requestId": success_request,
                 "config": {"sourceAssetId": video["id"], "imageAssetId": image["id"],
                            "promptMode": "reuse", "prompt": "保留原场景与镜头，只替换展示商品。", "duration": 4}}),
             node("demo-result", "result", 880, 85, {"jobId": success_id, "title": "演示已完成成片"}),
             node("demo-running", "task", 440, 550, {"kind": "lipsync", "requestId": running_request,
                 "config": {"videoAssetId": video["id"], "audioAssetId": audio["id"]}}),
             node("demo-draft", "task", 1300, 85, {"kind": "repair", "config": {"videoAssetId": video["id"]}}),
             node("demo-note", "note", 890, 570, {"text": "这是完全离线的演示画布。\n点击成功任务的按钮，可以复用配置或从成片创建下游草稿。"})]
    edges = [{"id": uuid.uuid4().hex, "source": source, "target": target, "type": "smoothstep"}
             for source, target in (("demo-video", "demo-success"), ("demo-image", "demo-success"),
                                    ("demo-success", "demo-result"), ("demo-video", "demo-running"),
                                    ("demo-audio", "demo-running"), ("demo-result", "demo-draft"))]
    canvases.save(demo["id"], {"revision": 0, "nodes": nodes, "edges": edges,
                                "viewport": {"x": 15, "y": 20, "zoom": 0.82}})


def run_desktop_demo(data_root=None):
    """Visible desktop acceptance, isolated from credentials and existing jobs."""
    import customtkinter as ctk
    from contextlib import nullcontext
    from video_gui import MaterialUniverseApp
    context = nullcontext(str(Path(data_root).resolve())) if data_root else tempfile.TemporaryDirectory(prefix="material-universe-desktop-demo-")
    with context as temporary:
        demo_root = Path(temporary)
        demo_root.mkdir(parents=True, exist_ok=True)
        DEMO_STOP.clear()
        root = ctk.CTk()
        app = MaterialUniverseApp(root, data_root=demo_root / "任务与素材", demo=True)
        app.canvas_window.on_closed = None
        root.title("素材万象 · 离线桌面验收（不收费）")
        app.workspace_output_dir_var.set(str(demo_root / "桌面成片"))
        app.tasks.execute = lambda job, credentials, update, store, resume=False: mock_execute(
            {**job, "config": {**job["config"], "demo_delay": 5}}, credentials, update, store, resume)
        video, image, audio = _make_media(demo_root / "示例文件")
        app.provider_vars["fal"]["key_var"].set("offline-demo-key")
        app.prompt_mode_var.set("使用已有提示词")
        app.reuse_prompt_box.insert("1.0", "保留原场景与镜头，只替换展示商品。")
        app._load_source_video(video); app._load_reference_image(image)
        app._load_lipsync_video(video); app._load_lipsync_audio(audio); app._load_repair_video(video)
        app.status_var.set("离线桌面验收 · 所有任务均为本地模拟，不调用服务商、不收费")
        try:
            root.mainloop()
        finally:
            DEMO_STOP.set()
            if app.tray:
                app.tray.stop()
            app.tasks.stop()
            app.canvas_service.stop()
            if app.tasks.wait_idle(5):
                app.desktop_store.db.close()
    return 0


def run_demo(data_root=None, browser_only=False):
    import customtkinter as ctk
    from video_gui import MaterialUniverseApp

    from contextlib import nullcontext
    context = nullcontext(str(Path(data_root).resolve())) if data_root else tempfile.TemporaryDirectory(prefix="material-universe-canvas-demo-")
    with context as temporary:
        demo_root = Path(temporary)
        demo_root.mkdir(parents=True, exist_ok=True)
        DEMO_STOP.clear()
        root = ctk.CTk()
        app = MaterialUniverseApp(root, data_root=demo_root / "任务与素材", demo=True)
        app.workspace_output_dir_var.set(str(demo_root / "画布成片"))
        if not CanvasStore(app.desktop_store).list():
            seed_demo(app, demo_root)
        root.withdraw()
        connection_file = demo_root / "demo-connection.json"
        try:
            if browser_only:
                url = app.canvas_service.start()
                connection_file.write_text(json.dumps({"url": url, "pid": os.getpid()}, ensure_ascii=False), encoding="utf-8")
                print(f"本地演示画布：{url}", flush=True)
                while not DEMO_STOP.is_set():
                    root.update()
                    time.sleep(0.05)
            else:
                root.deiconify()
                root.after(450, app._open_canvas)
                root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            connection_file.unlink(missing_ok=True)
            DEMO_STOP.set()
            if app.tray:
                app.tray.stop()
            app.tasks.stop()
            app.canvas_service.stop()
            if app.tasks.wait_idle(5):
                app.desktop_store.db.close()
    return 0
