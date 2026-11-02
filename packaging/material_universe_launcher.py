from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path


FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIDEO_SKILL_ROOT = (
    BUNDLE_ROOT / "skills" / "video-product-swap"
    if FROZEN
    else PROJECT_ROOT / "skills" / "video-product-swap"
)
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
APP_DATA_ROOT = LOCAL_APP_DATA / "MaterialUniverse"
WORKSPACE_ROOT = Path.home() / "Documents" / "素材万象" if FROZEN else PROJECT_ROOT
LOG_PATH = APP_DATA_ROOT / "素材万象启动错误.log"


def _configure_runtime() -> None:
    # Windowed executables have no console streams; GUI dependencies may still
    # write warnings when optional fonts or native integrations are unavailable.
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
    os.environ["MATERIAL_UNIVERSE_VIDEO_SKILL_ROOT"] = str(VIDEO_SKILL_ROOT)
    os.environ["MATERIAL_UNIVERSE_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    os.environ["PYTHONUTF8"] = "1"

    video_scripts = VIDEO_SKILL_ROOT / "scripts"
    for path in (video_scripts,):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)

    ffmpeg_dir = BUNDLE_ROOT / "ffmpeg"
    if ffmpeg_dir.is_dir():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")


def _show_startup_error(error: BaseException) -> None:
    detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    try:
        APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(
            f"时间：{datetime.now().isoformat(timespec='seconds')}\n\n{detail}",
            encoding="utf-8",
        )
    except OSError:
        pass
    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        messagebox.showerror(
            "素材万象启动失败",
            f"{error}\n\n错误详情已保存到：\n{LOG_PATH}",
            parent=root,
        )
        root.destroy()
    except Exception:
        pass


def _portable_self_test(output_path: Path) -> int:
    from redesign_acceptance import run
    from app_version import VERSION
    report = {"version": VERSION}
    code = 0
    try:
        for name in ("ffmpeg", "ffprobe", "ffplay"):
            command = shutil.which(name)
            if not command:
                raise RuntimeError(f"Missing bundled tool: {name}")
            probe = subprocess.run([command, "-version"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if probe.returncode:
                raise RuntimeError(f"Bundled tool failed: {name}")
        report["bundled_media_tools"] = True
        assert (VIDEO_SKILL_ROOT / "canvas/dist/index.html").is_file()
        report.update(run())
        report["success"] = True
    except Exception:
        report["success"] = False
        report["error"] = traceback.format_exc()
        code = 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


def _voice_self_test(output_dir: Path) -> int:
    from voice_acceptance import run
    from app_version import VERSION
    report = {"version": VERSION}
    code = 0
    try:
        report.update(run(output_dir))
        report["success"] = True
    except Exception:
        report["success"] = False
        report["error"] = traceback.format_exc()
        code = 1
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "声音成品验收.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


def main() -> int:
    _configure_runtime()
    if len(sys.argv) > 1 and sys.argv[1] == "--maintenance":
        from maintenance import main as maintenance_main
        return maintenance_main(sys.argv[2:])
    if "--desktop-demo" in sys.argv:
        from canvas_demo import run_desktop_demo
        data_root = sys.argv[sys.argv.index("--desktop-demo-data-root") + 1] if "--desktop-demo-data-root" in sys.argv else None
        return run_desktop_demo(data_root)
    if len(sys.argv) >= 3 and sys.argv[1] == "--canvas-child":
        from canvas_window import run_child
        return run_child(sys.argv[2], embedded="--canvas-embedded" in sys.argv)
    if "--canvas-demo" in sys.argv:
        from canvas_demo import run_demo
        data_root = sys.argv[sys.argv.index("--canvas-demo-data-root") + 1] if "--canvas-demo-data-root" in sys.argv else None
        return run_demo(data_root, browser_only="--canvas-demo-browser-only" in sys.argv)
    if len(sys.argv) == 3 and sys.argv[1] == "--portable-self-test":
        return _portable_self_test(Path(sys.argv[2]).expanduser().resolve())
    if len(sys.argv) == 3 and sys.argv[1] == "--voice-self-test":
        return _voice_self_test(Path(sys.argv[2]).expanduser().resolve())

    import video_gui

    return int(video_gui.main() or 0)


try:
    exit_code = main()
except BaseException as exc:
    if "--canvas-child" in sys.argv:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(sys.argv) > 3:
            Path(sys.argv[3]).write_text(detail, encoding="utf-8")
        elif sys.stderr:
            sys.stderr.write(detail)
    else:
        _show_startup_error(exc)
    exit_code = 1

raise SystemExit(exit_code)
