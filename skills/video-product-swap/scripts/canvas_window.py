"""WebView2 renderer embedded in a Tk HWND; the parent owns the sole task manager."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox
from urllib.parse import urlsplit


def save_download_result(url, identity, choose_destination):
    """Export only a persisted successful result chosen through the native dialog."""
    import json
    import shutil
    import tempfile
    from urllib.request import Request, urlopen
    from canvas_backend import CANVAS_ID
    if not isinstance(identity, str) or not CANVAS_ID.fullmatch(identity):
        raise ValueError("任务编号无效。")
    parsed = urlsplit(url)
    request = Request(f"{parsed.scheme}://{parsed.netloc}/api/jobs/{identity}/download-info",
                      headers={"X-Canvas-Token": parsed.fragment})
    try:
        with urlopen(request, timeout=15) as response:
            info = json.load(response)
    except Exception as exc:
        if hasattr(exc, "close"):
            exc.close()
        raise ValueError("本地生成结果不可用，请确认文件仍存在。") from exc
    source = Path(info["path"])
    if not source.is_file():
        raise ValueError("本地生成结果文件已移动或删除。")
    selected = choose_destination(source.name)
    if not selected:
        return {"cancelled": True}
    target = Path(selected[0] if isinstance(selected, (tuple, list)) else selected)
    if not target.suffix:
        target = target.with_suffix(source.suffix)
    if source.resolve() != target.resolve():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".material-save-", delete=False) as handle:
                temporary = Path(handle.name)
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return {"cancelled": False, "path": str(target)}


def window_title(url):
    return f"素材万象 · 创作画布 · {urlsplit(url).port}"


def descendant_process_ids(parent, relationships=None):
    """Include the real renderer behind Windows Python/venv launcher processes."""
    if relationships is None:
        if os.name != "nt":
            return {parent}
        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel.Process32FirstW.restype = kernel.Process32NextW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return {parent}
        relationships = []
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                relationships.append((entry.th32ProcessID, entry.th32ParentProcessID))
                found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
    allowed = {parent}
    while True:
        expanded = allowed | {pid for pid, ancestor in relationships if ancestor in allowed}
        if expanded == allowed:
            return allowed
        allowed = expanded


def run_child(url, embedded=False):
    stack_path = os.environ.get("MATERIAL_UNIVERSE_CANVAS_DEBUG_STACK_LOG")
    stack_log = open(stack_path, "w", encoding="utf-8") if stack_path else None
    if stack_log:
        import faulthandler
        faulthandler.dump_traceback_later(12, repeat=True, file=stack_log)
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("缺少画布窗口组件 pywebview，请安装桌面依赖后重试。") from exc

    class Dialogs:
        def __init__(self):
            self._window = None
            self._on_close_completed = None

        def close_completed(self, result=None):
            if self._on_close_completed:
                self._on_close_completed(result)

        def download_result(self, identity):
            try:
                return save_download_result(url, identity, lambda name: self._window.create_file_dialog(
                    webview.FileDialog.SAVE, save_filename=name))
            except Exception as exc:
                return {"error": str(exc)}

        def host_action(self, action):
            import json
            from urllib.request import Request, urlopen
            parsed = urlsplit(url)
            endpoint = f"{parsed.scheme}://{parsed.netloc}/api/host/action"
            request = Request(endpoint, json.dumps({"action": action}).encode(),
                              {"X-Canvas-Token": parsed.fragment, "Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)

    dialogs = Dialogs()
    dialogs._window = webview.create_window(window_title(url), url, js_api=dialogs,
                                           width=1500, height=920, min_size=(1, 1) if embedded else (900, 650),
                                           hidden=embedded, frameless=embedded, easy_drag=not embedded,
                                           background_color="#191a1f")
    if embedded:
        def lock_page_zoom():
            # pywebview enables native Ctrl+wheel zoom even with zoomable=False.
            # Only React Flow's viewport should zoom inside this fixed host.
            from System import Action
            def apply():
                view = dialogs._window.native.webview
                view.CoreWebView2.Settings.IsZoomControlEnabled = False
                view.ZoomFactor = 1.0
            dialogs._window.native.Invoke(Action(apply))
        dialogs._window.events.loaded += lock_page_zoom
    state = {"closing": False, "saved": False}
    state_lock = threading.Lock()

    def flush_and_close():
        def finish_on_worker(error=None):
            if error:
                try:
                    dialogs._window.run_js("alert('画布保存失败，请重试关闭。')")
                except Exception:
                    pass
            else:
                dialogs._window.destroy()

        def completed(value):
            with state_lock:
                if state["saved"]:
                    return
                if isinstance(value, dict) and value.get("error"):
                    state["closing"] = False
                    threading.Thread(target=finish_on_worker, args=(value["error"],), daemon=True).start()
                    return
                state["saved"] = True
            threading.Thread(target=finish_on_worker, daemon=True).start()

        try:
            dialogs._on_close_completed = completed
            if not dialogs._window.events._pywebviewready.wait(15):
                raise RuntimeError("画布窗口尚未加载完成")
            # run_js executes the script directly. evaluate_js wraps it in eval,
            # which the canvas Content Security Policy intentionally blocks.
            dialogs._window.run_js(
                "Promise.resolve(window.canvasFlush ? window.canvasFlush() : true)"
                ".then(() => window.pywebview.api.close_completed({ok:true}),"
                "e => window.pywebview.api.close_completed({error:String(e)}))"
            )
        except Exception:
            with state_lock:
                state["closing"] = False

    def on_closing():
        with state_lock:
            if state["saved"]:
                return None
            if not state["closing"]:
                state["closing"] = True
                threading.Thread(target=flush_and_close, daemon=True).start()
        return False

    dialogs._window.events.closing += on_closing
    try:
        try:
            webview.start(gui="edgechromium", debug=False)
        except Exception as exc:
            raise RuntimeError("WebView2 画布窗口启动失败。请确认 Microsoft Edge WebView2 Runtime 已安装。"
                               f"原始错误：{exc}") from exc
    finally:
        if stack_log:
            faulthandler.cancel_dump_traceback_later()
            stack_log.close()
    return 0


def win32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "IsWindow": ((wintypes.HWND,), wintypes.BOOL),
        "IsWindowVisible": ((wintypes.HWND,), wintypes.BOOL),
        "GetParent": ((wintypes.HWND,), wintypes.HWND),
        "SetParent": ((wintypes.HWND, wintypes.HWND), wintypes.HWND),
        "GetWindowLongW": ((wintypes.HWND, ctypes.c_int), ctypes.c_long),
        "SetWindowLongW": ((wintypes.HWND, ctypes.c_int, ctypes.c_long), ctypes.c_long),
        "GetWindowTextLengthW": ((wintypes.HWND,), ctypes.c_int),
        "GetWindowTextW": ((wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
        "GetWindowThreadProcessId": ((wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
        "GetClientRect": ((wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
        "MoveWindow": ((wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL), wintypes.BOOL),
        "SetWindowPos": ((wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT), wintypes.BOOL),
        "ShowWindow": ((wintypes.HWND, ctypes.c_int), wintypes.BOOL),
        "SetForegroundWindow": ((wintypes.HWND,), wintypes.BOOL),
        "PostMessageW": ((wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM), wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(user32, name)
        function.argtypes, function.restype = args, result
    return user32


class CanvasWindowManager:
    def __init__(self, root, service, on_closed=None):
        self.root, self.service, self.on_closed = root, service, on_closed
        self.child = None
        self.url = None
        self.host = None
        self.hwnd = None
        self.visible = False
        self.stopping = False
        self._resize_timer = None
        self._close_callback = None
        self.error_log = service.store.root / "画布窗口错误.log"

    def open(self, host=None):
        if host is not None and self.host is None:
            self.host = host
            host.bind("<Configure>", self._schedule_resize, add="+")
        self.visible = True
        if self.child and self.child.poll() is None:
            self._show_embedded()
            return
        try:
            import webview  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("缺少画布组件 pywebview，请安装桌面依赖后重试。") from exc
        if self.url is None or self.service.server is None:
            self.url = self.service.start()
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).with_name("launch_gui.pyw")))
        command += ["--canvas-child", self.url, str(self.error_log)]
        if self.host is not None:
            command.append("--canvas-embedded")
        self.stopping = False
        self.hwnd = None
        with self.error_log.open("wb") as log:
            self.child = subprocess.Popen(command, close_fds=True, stdout=log, stderr=log,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.root.after(100, self._watch)

    def hide(self):
        self.visible = False
        if self.hwnd and os.name == "nt":
            win32().ShowWindow(self.hwnd, 0)

    def _window_handle(self):
        if os.name != "nt" or not self.url:
            return None
        user32 = win32()
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        user32.EnumWindows.restype = wintypes.BOOL
        found = []
        title = window_title(self.url)
        allowed_pids = descendant_process_ids(self.child.pid) if self.child else set()
        def inspect(hwnd, _):
            process = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
            if self.child and process.value not in allowed_pids:
                return True
            size = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(size + 1)
            user32.GetWindowTextW(hwnd, buffer, size + 1)
            if buffer.value == title:
                found.append(hwnd)
                return False
            return True
        user32.EnumWindows(callback_type(inspect), 0)
        return found[0] if found else None

    def _attach(self):
        if not self.host or self.hwnd:
            return
        hwnd = self._window_handle()
        if not hwnd:
            return
        user32 = win32()
        host_hwnd = self.host.winfo_id()
        style = user32.GetWindowLongW(hwnd, -16) & 0xffffffff
        style = (style & ~0x80cf0000) | 0x40000000  # remove popup/chrome; add WS_CHILD
        user32.SetWindowLongW(hwnd, -16, style)
        ctypes.set_last_error(0)
        user32.SetParent(hwnd, host_hwnd)
        if user32.GetParent(hwnd) != host_hwnd:
            raise ctypes.WinError(ctypes.get_last_error() or 1)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37)  # FRAMECHANGED, no move/size/activate
        self.hwnd = hwnd
        self._show_embedded()

    def _show_embedded(self):
        if self.host and not self.hwnd:
            return  # Never show the renderer until SetParent has completed.
        hwnd = self.hwnd or self._window_handle()
        if not hwnd:
            return
        user32 = win32()
        if self.host:
            self.resize()
            user32.ShowWindow(hwnd, 5 if self.visible else 0)
        else:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)

    def _schedule_resize(self, _event=None):
        if self._resize_timer:
            self.root.after_cancel(self._resize_timer)
        self._resize_timer = self.root.after(30, self.resize)

    def resize(self):
        self._resize_timer = None
        if not self.host or not self.hwnd:
            return
        user32 = win32()
        rect = wintypes.RECT()
        if user32.GetClientRect(self.host.winfo_id(), ctypes.byref(rect)):
            user32.MoveWindow(self.hwnd, 0, 0, max(1, rect.right), max(1, rect.bottom), True)

    def _watch(self):
        if self.child and self.child.poll() is None:
            if self.host and not self.hwnd:
                try:
                    self._attach()
                except Exception as exc:
                    self.visible = False
                    self.stopping = True  # Report this failed launch once.
                    self.child.terminate()
                    messagebox.showerror("画布嵌入失败", str(exc), parent=self.root)
            self.root.after(100 if self.host and not self.hwnd else 700, self._watch)
            return
        self.hwnd = None
        if self.child and self.child.returncode and not self.stopping:
            detail = self.error_log.read_text(encoding="utf-8", errors="replace")[-1800:]
            messagebox.showerror("画布加载失败", detail or "画布组件已退出，请确认 WebView2 Runtime 可用。", parent=self.root)
        if self.on_closed and not self.stopping:
            self.on_closed()

    def request_stop(self, completed):
        """Keep Tk pumping while the embedded renderer flushes and destroys its HWND."""
        if self._close_callback:
            return
        self.stopping = True
        self._close_callback = completed
        self._close_deadline = time.monotonic() + 18
        if self.child and self.child.poll() is None:
            hwnd = self.hwnd or self._window_handle()
            if hwnd:
                win32().PostMessageW(hwnd, 0x0010, 0, 0)
            else:
                self.child.terminate()
        self.root.after(30, self._poll_stop)

    def _poll_stop(self):
        if self.child and self.child.poll() is None:
            if time.monotonic() < self._close_deadline:
                self.root.after(50, self._poll_stop)
                return
            self.stopping = False
            result = False
        else:
            self.hwnd = None
            self.service.stop()
            result = True
        callback, self._close_callback = self._close_callback, None
        if callback:
            callback(result)

    def stop(self):
        # Compatibility for teardown helpers outside mainloop; normal app exit is async.
        result = []
        self.request_stop(result.append)
        while not result:
            self.root.update()
            time.sleep(.01)
        return result[0]
