from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = SKILL_ROOT / "素材万象启动错误.log"


def show_startup_error(error: BaseException) -> None:
    detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    LOG_PATH.write_text(
        f"时间：{datetime.now().isoformat(timespec='seconds')}\n\n{detail}",
        encoding="utf-8",
    )
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


try:
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    import video_gui

    exit_code = video_gui.main()
except BaseException as exc:
    show_startup_error(exc)
    exit_code = 1

raise SystemExit(exit_code)
