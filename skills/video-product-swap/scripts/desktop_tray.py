"""Windows notification-area lifetime; all UI actions go through the Tk queue."""
from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path

from PIL import Image


class DesktopTray:
    def __init__(self, events, image_path: Path):
        self.events, self.image_path = events, image_path
        self.icon = None
        self.thread = None
        self.ready = threading.Event()
        self.stopping = threading.Event()

    @property
    def is_ready(self):
        return self.ready.is_set() and not self.stopping.is_set() and bool(self.thread and self.thread.is_alive())

    def open_window(self, *_args):
        self.events.put(("tray_open", self))

    def exit_app(self, *_args):
        self.events.put(("tray_exit", self))

    def start(self):
        self.thread = threading.Thread(target=self._run, name="material-universe-tray", daemon=True)
        self.thread.start()

    def _create_icon(self, image):
        import pystray
        icon_class = pystray.Icon
        if sys.platform == "win32":
            from pystray._win32 import Icon as WindowsIcon
            from pystray._util import win32
            owner = self

            class CheckedWindowsIcon(WindowsIcon):
                def _show(self):
                    # pystray 0.19 ignores the shell's BOOL result. Do not hide
                    # the only window unless Windows accepted NIM_ADD.
                    self._assert_icon_handle()
                    accepted = win32.Shell_NotifyIcon(win32.NIM_ADD, win32.NOTIFYICONDATAW(
                        cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd, hID=id(self),
                        uFlags=win32.NIF_MESSAGE | win32.NIF_ICON | win32.NIF_TIP,
                        uCallbackMessage=win32.WM_NOTIFY, hIcon=self._icon_handle, szTip=self.title))
                    if not accepted:
                        owner.ready.clear()
                        owner.events.put(("tray_failed", (owner, "Windows 未能建立系统托盘图标。")))
                        raise OSError("Windows 未能建立系统托盘图标。")

            icon_class = CheckedWindowsIcon
        return icon_class("MaterialUniverse", image, "素材万象", menu=pystray.Menu(
            pystray.MenuItem("打开素材万象", self.open_window, default=True),
            pystray.MenuItem("退出软件", self.exit_app)))

    def _on_ready(self, icon):
        try:
            if self.stopping.is_set():
                icon.stop()
                return
            icon.visible = True
            if self.stopping.is_set():
                icon.visible = False
                icon.stop()
                return
            self.ready.set()
            self.events.put(("tray_ready", self))
        except Exception as exc:
            self.events.put(("tray_failed", (self, str(exc))))
            icon.stop()

    def _run(self):
        error = "系统托盘已停止，请重新关闭窗口以重试。"
        try:
            with Image.open(self.image_path) as source:
                image = source.convert("RGBA").resize((64, 64))
            self.icon = self._create_icon(image)
            if self.stopping.is_set():
                return
            self.icon.run(setup=self._on_ready)
        except Exception as exc:
            error = str(exc)
        finally:
            self.ready.clear()
            if not self.stopping.is_set():
                self.events.put(("tray_failed", (self, error)))

    def stop(self):
        self.stopping.set()
        self.ready.clear()
        if self.icon is not None:
            try:
                self.icon.visible = False
                self.icon.stop()
            except Exception:
                pass
        if self.thread and self.thread is not threading.current_thread() and self.thread.is_alive():
            try:
                self.thread.join(timeout=2)
            except RuntimeError:
                pass
