from __future__ import annotations

import json
import os
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
PROMPT_SKILL_ROOT = (
    BUNDLE_ROOT / "skills" / "ai-video-swap-prompt-generator"
    if FROZEN
    else PROJECT_ROOT / "skills" / "ai-video-swap-prompt-generator"
)
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
APP_DATA_ROOT = LOCAL_APP_DATA / "MaterialUniverse"
WORKSPACE_ROOT = Path.home() / "Documents" / "素材万象" if FROZEN else PROJECT_ROOT
LOG_PATH = APP_DATA_ROOT / "素材万象启动错误.log"


def _configure_runtime() -> None:
    os.environ["MATERIAL_UNIVERSE_VIDEO_SKILL_ROOT"] = str(VIDEO_SKILL_ROOT)
    os.environ["MATERIAL_UNIVERSE_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    os.environ["AI_VIDEO_PROMPT_SKILL_ROOT"] = str(PROMPT_SKILL_ROOT)
    os.environ["PYTHONUTF8"] = "1"

    video_scripts = VIDEO_SKILL_ROOT / "scripts"
    prompt_scripts = PROMPT_SKILL_ROOT / "scripts"
    for path in (prompt_scripts, video_scripts):
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
    report: dict[str, object] = {
        "success": False,
        "frozen": FROZEN,
        "bundle_root": str(BUNDLE_ROOT),
        "workspace_root": str(WORKSPACE_ROOT),
    }
    root = None
    try:
        from PIL import Image
        import customtkinter as ctk
        import dashscope
        import generate_swap_prompt
        import generate_video
        from portable_runtime import run_internal_command
        import video_gui

        tools: dict[str, str] = {}
        for name in ("ffmpeg", "ffprobe", "ffplay"):
            executable = shutil.which(name)
            if not executable:
                raise RuntimeError(f"没有找到内置的 {name}。")
            tools[name] = executable
        probe = subprocess.run(
            [tools["ffprobe"], "-version"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError("内置 FFprobe 无法运行。")

        with Image.open(VIDEO_SKILL_ROOT / "assets" / "material-universe-logo.png") as image:
            logo_size = image.size
        with Image.open(VIDEO_SKILL_ROOT / "assets" / "material-universe.ico") as image:
            icon_format = image.format

        root = ctk.CTk()
        root.withdraw()
        app = video_gui.MaterialUniverseApp(root)
        root.update_idletasks()

        internal_command = [
            sys.executable,
            str(VIDEO_SKILL_ROOT / "scripts" / "generate_video.py"),
            "--list-models",
        ]
        internal_code, internal_stdout, internal_stderr = run_internal_command(
            internal_command,
            VIDEO_SKILL_ROOT,
            dict(os.environ),
        )
        internal_payload = json.loads(internal_stdout)
        if internal_code != 0 or not internal_payload.get("success"):
            raise RuntimeError(
                internal_payload.get("error")
                or internal_stderr
                or "内置模型入口无法运行。"
            )

        report.update(
            {
                "success": True,
                "tools": tools,
                "logo_size": logo_size,
                "icon_format": icon_format,
                "models": [item["model"] for item in app.models],
                "dashscope_version": getattr(dashscope, "__version__", "unknown"),
                "internal_entries": [
                    generate_swap_prompt.__name__,
                    generate_video.__name__,
                ],
                "internal_model_count": len(internal_payload.get("models", [])),
            }
        )
        return_code = 0
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        return_code = 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return return_code


def main() -> int:
    _configure_runtime()
    if len(sys.argv) == 3 and sys.argv[1] == "--portable-self-test":
        return _portable_self_test(Path(sys.argv[2]).expanduser().resolve())

    import video_gui

    return int(video_gui.main() or 0)


try:
    exit_code = main()
except BaseException as exc:
    _show_startup_error(exc)
    exit_code = 1

raise SystemExit(exit_code)
