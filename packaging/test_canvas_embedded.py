"""Real Tk + WebView2 integration. Isolated state, no paid execution or browser substitute."""
from __future__ import annotations
import ctypes
import json
import os
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))


def renderer():
    import webview
    from canvas_window import run_child
    folder = Path(os.environ["MU_NATIVE_TEST_FOLDER"])
    create = webview.create_window
    saved_names = set()
    def save_dialog(window, *args, **kwargs):
        name = kwargs.get("save_filename", "result.mp4")
        if name in saved_names:
            return None
        saved_names.add(name)
        return (str(folder / ("download-" + name)),)
    def create_test_window(*args, **kwargs):
        window = create(*args, **kwargs)
        def record_result(value):
            temporary = folder / ("response-" + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(folder / "response.json")
        def native_state():
            from System import Func, Object
            def inspect_native():
                form, view = window.native, window.native.webview
                return json.dumps({"form": [form.Left, form.Top, form.ClientSize.Width, form.ClientSize.Height],
                    "webview": [view.Left, view.Top, view.Width, view.Height],
                    "page_zoom": view.ZoomFactor, "page_zoom_enabled": view.CoreWebView2.Settings.IsZoomControlEnabled})
            return json.loads(str(window.native.Invoke(Func[Object](inspect_native))))
        def browser_input(method, parameters):
            from System import Func, Object
            window.native.Invoke(Func[Object](lambda: window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(method, json.dumps(parameters))))
            return True
        window.expose(record_result, native_state, browser_input)
        def loaded():
            (folder / "ready").write_text("loaded", encoding="utf-8")
            def commands():
                last = None
                while True:
                    try:
                        command = json.loads((folder / "command.json").read_text(encoding="utf-8"))
                        if command["seq"] != last:
                            last = command["seq"]
                            window.run_js(command["javascript"])
                    except (OSError, json.JSONDecodeError):
                        pass
                    time.sleep(.04)
            threading.Thread(target=commands, daemon=True).start()
        window.events.loaded += loaded
        return window
    with patch.object(webview, "create_window", create_test_window), \
            patch.object(webview.window.Window, "create_file_dialog", save_dialog):
        return run_child(sys.argv[2], embedded=True)


def main():
    import customtkinter as ctk
    import subprocess
    from PIL import Image, ImageGrab
    from canvas_window import win32
    from video_gui import MaterialUniverseApp
    folder = ROOT / ".build/canvas-integrated-20261003" / ("native-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    os.environ["MU_NATIVE_TEST_FOLDER"] = str(folder)
    user32 = win32()
    root = ctk.CTk()
    root.attributes("-alpha", 0)
    with patch.object(MaterialUniverseApp, "_ensure_tray", lambda self: None), patch.object(MaterialUniverseApp, "_restore_history", lambda self: None):
        app = MaterialUniverseApp(root, data_root=folder / "state", demo=True)
    root.title("素材万象 · 原生嵌入离线验收")
    app.workspace_output_dir_var.set(str(folder / "configured-outputs"))
    report = {"paid_tasks": False, "single_task_manager": app.canvas_service.tasks is app.tasks}
    # Real local results exercise the React download button and native bridge.
    import shutil
    image_result = folder / "验收图片.png"
    shutil.copyfile(ROOT / "skills/video-product-swap/assets/material-universe-logo.png", image_result)
    video_result = folder / "验收视频.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=240x320:r=24", "-t", "1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(video_result)], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    canvas = app.canvas_service.canvases.create("下载验收")
    result_nodes = []
    for index, source in enumerate((video_result, image_result)):
        identity = uuid.uuid4().hex
        app.desktop_store._put("jobs", {"id": identity, "kind": "general", "name": source.name,
            "created": time.time(), "status": "success", "stage": "离线成片", "config": {"output": str(source)},
            "result": {"output": str(source), "local_downloaded": True,
                       "output_kind": "video" if index == 0 else "image"}})
        result_nodes.append({"id": "result-" + identity, "type": "result", "position": {"x": 900 + index * 360, "y": 30},
                             "data": {"jobId": identity}})
    app.canvas_service.canvases.save(canvas["id"], {**canvas, "nodes": result_nodes})
    def pump(test, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            if test():
                return
            time.sleep(.02)
        raise AssertionError("Native integration timed out")
    real_popen = subprocess.Popen
    def child_command(command, *args, **kwargs):
        if "--canvas-child" in command:
            command = [command[0], str(Path(__file__).resolve()), *command[2:]]
        return real_popen(command, *args, **kwargs)
    serial = 0
    def javascript(code):
        nonlocal serial
        serial += 1
        answer = folder / "response.json"
        answer.unlink(missing_ok=True)
        (folder / "command.json").write_text(json.dumps({"seq": serial, "javascript": "(()=>{" + code + "})()"}, ensure_ascii=False), encoding="utf-8")
        pump(answer.exists)
        return json.loads(answer.read_text(encoding="utf-8"))
    inspect = "window.pywebview.api.record_result({note:document.querySelector('.note-input')?.value,viewport:document.querySelector('.react-flow__viewport')?.style.transform,canvas:document.querySelector('.canvas-select select')?.value,width:innerWidth,height:innerHeight,toolbar:[...document.querySelectorAll('.toolbar button')].map(x=>x.textContent),chrome:['.topbar','.toolbar','.library'].map(s=>{const r=document.querySelector(s).getBoundingClientRect();return [r.x,r.y,r.width,r.height]}),dpr:devicePixelRatio})"
    def dimensions(hwnd):
        rect = wintypes.RECT()
        assert user32.GetClientRect(hwnd, ctypes.byref(rect))
        return [rect.right, rect.bottom]
    def actual_bounds(hwnd, parent):
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        rect, host_rect = wintypes.RECT(), wintypes.RECT()
        assert user32.GetWindowRect(hwnd, ctypes.byref(rect))
        assert user32.GetWindowRect(parent, ctypes.byref(host_rect))
        return [rect.left - host_rect.left, rect.top - host_rect.top, rect.right - rect.left, rect.bottom - rect.top]
    try:
        with patch("canvas_window.subprocess.Popen", child_command):
            app._open_canvas()
            # Opening again during startup must never show a separate top-level renderer.
            app._open_canvas()
            pump(lambda: app.canvas_window.hwnd and (folder / "ready").exists(), 35)
        manager = app.canvas_window
        hwnd, pid, url = manager.hwnd, manager.child.pid, manager.url
        host = app.canvas_host.winfo_id()
        assert user32.GetParent(hwnd) == host
        style = user32.GetWindowLongW(hwnd, -16) & 0xffffffff
        assert style & 0x40000000 and not (style & 0x80000000)
        pump(lambda: dimensions(hwnd) == dimensions(host))
        report.update({"renderer_pid": pid, "renderer_hwnd": hwnd, "host_hwnd": host, "actual_parent_hwnd": user32.GetParent(hwnd), "WS_CHILD": True, "WS_POPUP": False, "initial_size": dimensions(hwnd)})
        print("Native child HWND parent/style/size verified", flush=True)
        native_before = javascript("window.pywebview.api.native_state().then(window.pywebview.api.record_result)")
        native_before["actual_bounds"] = actual_bounds(hwnd, host)
        assert not native_before["page_zoom_enabled"] and native_before["page_zoom"] == 1
        assert native_before["webview"] == [0, 0, *dimensions(host)]
        javascript("window.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:1,buttons:4,clientX:500,clientY:300,screenX:screenX+500,screenY:screenY+300,view:window}));window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,button:1,buttons:4,clientX:580,clientY:350,screenX:screenX+580,screenY:screenY+350,view:window}));window.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,button:1,clientX:580,clientY:350,view:window}));setTimeout(()=>window.pywebview.api.native_state().then(window.pywebview.api.record_result),250)")
        native_after = javascript("window.pywebview.api.native_state().then(window.pywebview.api.record_result)")
        native_after["actual_bounds"] = actual_bounds(hwnd, host)
        report["pan_native_before"] = native_before
        report["pan_native_after"] = native_after
        print(json.dumps({"native_before": native_before, "native_after": native_after}), flush=True)
        assert native_before["actual_bounds"] == native_after["actual_bounds"], "Canvas pan moved the embedded native window"
        pump(lambda: bool(app.canvas_service.canvases.list()))
        first = javascript(inspect)
        assert first["canvas"] and not any("视频节点" in text or "对口型节点" in text for text in first["toolbar"])
        for index, source in enumerate((video_result, image_result)):
            javascript("const b=[...document.querySelectorAll('.react-flow__node-result button')].filter(x=>x.textContent==='下载')[" + str(index) + "];if(!b)throw new Error('result download button absent');b.click();setTimeout(()=>window.pywebview.api.record_result({ok:true}),300)")
            destination = folder / ("download-" + source.name)
            pump(destination.exists)
            assert destination.read_bytes() == source.read_bytes()
            # The same button handles native cancel and remains available.
            javascript("const b=[...document.querySelectorAll('.react-flow__node-result button')].filter(x=>x.textContent==='下载')[" + str(index) + "];b.click();setTimeout(()=>window.pywebview.api.record_result({ready:!b.disabled}),300)")
            assert destination.read_bytes() == source.read_bytes()
        report["native_video_and_image_download_buttons"] = True
        report["native_download_cancel_preserves_original"] = True
        # Genuine Chromium input exercises wheel defaults, unlike an untrusted DOM wheel.
        chrome_before = first["chrome"]
        for modifiers in (0, 2):  # ordinary wheel, then Ctrl+wheel
            wheel_before = javascript(inspect)
            javascript("const r=document.querySelector('.react-flow__pane').getBoundingClientRect();window.pywebview.api.browser_input('Input.dispatchMouseEvent',{type:'mouseWheel',x:r.x+200,y:r.y+200,deltaX:0,deltaY:-100,modifiers:" + str(modifiers) + "}).then(()=>setTimeout(()=>window.pywebview.api.record_result({ok:true}),250))")
            wheel_after = javascript(inspect)
            assert wheel_before["viewport"] != wheel_after["viewport"], f"Wheel modifiers={modifiers} did not zoom the canvas"
            assert wheel_after["chrome"] == chrome_before
        zoomed = javascript(inspect)
        assert zoomed["viewport"] != first["viewport"]
        assert zoomed["chrome"] == chrome_before and zoomed["dpr"] == first["dpr"]
        javascript("window.pywebview.api.browser_input('Input.dispatchKeyEvent',{type:'keyDown',modifiers:2,key:'+',code:'Equal',windowsVirtualKeyCode:187}).then(()=>window.pywebview.api.browser_input('Input.dispatchKeyEvent',{type:'keyUp',modifiers:2,key:'+',code:'Equal',windowsVirtualKeyCode:187})).then(()=>window.pywebview.api.record_result({ok:true}))")
        native_zoomed = javascript("window.pywebview.api.native_state().then(window.pywebview.api.record_result)")
        assert native_zoomed["page_zoom"] == 1 and not native_zoomed["page_zoom_enabled"]
        assert actual_bounds(hwnd, host) == [0, 0, *dimensions(host)]
        report["wheel_and_ctrl_wheel_zoom_only_flow"] = True
        report["browser_zoom_keys_keep_zoom_one"] = True
        javascript("const p=document.querySelector('.react-flow__pane');p.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:innerWidth-210,clientY:220}));setTimeout(()=>{[...document.querySelectorAll('[role=menuitem]')].find(x=>x.textContent.includes('文字备注')).click();setTimeout(()=>window.pywebview.api.record_result({ok:true}),150)},150)")
        def change_note(text):
            encoded = json.dumps(text, ensure_ascii=False)
            return javascript("const n=document.querySelector('.note-input');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(n," + encoded + ");n.dispatchEvent(new Event('input',{bubbles:true}));window.pywebview.api.record_result({ok:true})")
        change_note("原生窗口切页保持内容")
        before_pan = javascript(inspect)
        javascript("const p=document.querySelector('.react-flow__pane');p.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:1,buttons:4,clientX:700,clientY:500,view:window}));window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,button:1,buttons:4,clientX:770,clientY:550,view:window}));window.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,button:1,clientX:770,clientY:550,view:window}));setTimeout(()=>window.pywebview.api.record_result({ok:true}),250)")
        after_pan = javascript(inspect)
        assert before_pan["viewport"] != after_pan["viewport"], "Middle drag did not pan the canvas"
        assert after_pan["chrome"] == before_pan["chrome"]
        assert actual_bounds(hwnd, host) == [0, 0, *dimensions(host)]
        report["middle_drag_moves_flow_only"] = True
        javascript("document.querySelector('.react-flow__controls-fitview').click();setTimeout(()=>window.pywebview.api.record_result({ok:true}),350)")
        node_drag = javascript("const n=document.querySelector('.react-flow__node-note');const before=n.style.transform;const title=n.querySelector('.eyebrow');const r=title.getBoundingClientRect();const x=r.left+30,y=r.top+r.height/2;const target=document.elementFromPoint(x,y)?.className;title.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0,buttons:1,clientX:x,clientY:y,view:window}));setTimeout(()=>{window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,button:0,buttons:1,clientX:x+20,clientY:y+15,view:window}));setTimeout(()=>{window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,button:0,buttons:1,clientX:x+45,clientY:y+35,view:window}));window.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,button:0,clientX:x+45,clientY:y+35,view:window}));setTimeout(()=>window.pywebview.api.record_result({before,after:document.querySelector('.react-flow__node-note').style.transform,x,y,target}),200)},80)},80)")
        print(json.dumps({"node_drag": node_drag}), flush=True)
        assert node_drag["before"] != node_drag["after"], "Node drag did not move the node"
        assert actual_bounds(hwnd, host) == [0, 0, *dimensions(host)]
        report["node_drag_keeps_native_origin"] = True
        report["node_drag_positions"] = node_drag
        javascript("document.querySelector('.react-flow__pane').dispatchEvent(new WheelEvent('wheel',{bubbles:true,clientX:500,clientY:350,deltaY:-100}));setTimeout(()=>window.pywebview.api.record_result({ok:true}),400)")
        pump(lambda: app.canvas_service.canvases.get(first["canvas"])["nodes"])
        before = javascript(inspect)
        assert before["note"] == "原生窗口切页保持内容"
        app._show_page("history")
        root.update()
        assert not user32.IsWindowVisible(hwnd)
        app._open_canvas()
        root.update()
        print(json.dumps({"canvas_page": app.current_page, "manager_visible": manager.visible, "child_visible": bool(user32.IsWindowVisible(hwnd)), "host_visible": bool(user32.IsWindowVisible(host)), "host_mapped": app.canvas_host.winfo_ismapped(), "child_style": hex(user32.GetWindowLongW(hwnd, -16) & 0xffffffff), "parent": user32.GetParent(hwnd)}), flush=True)
        pump(lambda: user32.IsWindowVisible(hwnd), 3)
        after = javascript(inspect)
        assert (manager.hwnd, manager.child.pid, manager.url) == (hwnd, pid, url)
        assert (after["note"], after["viewport"], after["canvas"]) == (before["note"], before["viewport"], before["canvas"])
        report["switch_keeps_pid_hwnd_url_dom_viewport"] = True
        root.geometry("1360x900")
        pump(lambda: dimensions(hwnd) == dimensions(host) and dimensions(hwnd) != report["initial_size"])
        report["resized_size"] = dimensions(hwnd)
        javascript("window.pywebview.api.host_action('fullscreen').then(()=>window.pywebview.api.record_result({ok:true}))")
        pump(lambda: app._fullscreen and dimensions(hwnd) == dimensions(host))
        report["fullscreen_size"] = dimensions(hwnd)
        javascript("window.pywebview.api.host_action('leave_fullscreen').then(()=>window.pywebview.api.record_result({ok:true}))")
        pump(lambda: not app._fullscreen and dimensions(hwnd) == dimensions(host))
        report["fullscreen_and_escape_host_bridge"] = True
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        rect = wintypes.RECT()
        user32.GetWindowRect(root.winfo_id(), ctypes.byref(rect))
        screenshot = folder / "embedded-window.png"
        try:
            if float(root.attributes("-alpha")) > 0:
                ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(screenshot)
                report["screenshot_method"] = "ImageGrab"
            else:
                report["screenshot_limitation"] = "透明验收窗口不采集桌面画面。"
        except OSError as capture_error:
            # A disconnected desktop can reject screen capture. Try native capture
            # once, but never let screenshot support skip the actual close test.
            try:
                gdi = ctypes.WinDLL("gdi32", use_last_error=True)
                for name in ("CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject"):
                    getattr(gdi, name).restype = wintypes.HANDLE
                gdi.CreateCompatibleDC.argtypes = (wintypes.HDC,)
                gdi.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
                gdi.SelectObject.argtypes = (wintypes.HDC, wintypes.HANDLE)
                gdi.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)
                gdi.DeleteObject.argtypes = (wintypes.HANDLE,)
                gdi.DeleteDC.argtypes = (wintypes.HDC,)
                user32.GetDC.argtypes = (wintypes.HWND,)
                user32.GetDC.restype = wintypes.HDC
                user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
                user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
                width, height = rect.right - rect.left, rect.bottom - rect.top
                dc = user32.GetDC(root.winfo_id())
                memory_dc = gdi.CreateCompatibleDC(dc)
                bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
                old_bitmap = gdi.SelectObject(memory_dc, bitmap)
                try:
                    assert user32.PrintWindow(root.winfo_id(), memory_dc, 2), "PrintWindow failed"
                    header = ctypes.create_string_buffer(40)
                    import struct
                    header.raw = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
                    pixels = ctypes.create_string_buffer(width * height * 4)
                    assert gdi.GetDIBits(memory_dc, bitmap, 0, height, pixels, header, 0) == height
                    Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX").save(screenshot)
                    report["screenshot_method"] = "PrintWindow"
                finally:
                    gdi.SelectObject(memory_dc, old_bitmap)
                    gdi.DeleteObject(bitmap)
                    gdi.DeleteDC(memory_dc)
                    user32.ReleaseDC(root.winfo_id(), dc)
            except Exception as native_capture_error:
                report["screenshot_limitation"] = f"{capture_error}; {native_capture_error}"
        # Exit immediately with a dirty edit, before the normal 650ms autosave fires.
        change_note("退出前未自动保存的内容也已落盘")
        beats = []
        def heartbeat():
            beats.append(time.monotonic())
            if not app._closed:
                root.after(30, heartbeat)
        root.after(0, heartbeat)
        app._request_exit()
        pump(lambda: app._closed and manager.child.poll() is not None, 25)
        saved = app.canvas_service.canvases.get(first["canvas"])
        assert next(node for node in saved["nodes"] if node["type"] == "note")["data"]["text"] == "退出前未自动保存的内容也已落盘"
        assert manager.child.returncode == 0 and app.canvas_service.server is None
        assert not app.tasks.active and not app.tasks.pending
        report.update({"close_flush_saved": True, "renderer_exit_code": 0, "server_stopped": True, "tk_pumped_during_close": len(beats), "native_screenshot": str(screenshot) if screenshot.exists() else None, "passed": True})
        print("Native switching/fullscreen/dirty-close flush verified", flush=True)
    finally:
        if app.canvas_window.child and app.canvas_window.child.poll() is None:
            app.canvas_window.child.terminate()
            app.canvas_window.child.wait(timeout=5)
        app.canvas_service.stop()
        app.tasks.stop()
        app.tasks.wait_idle(3)
        app.desktop_store.db.close()
        try:
            root.destroy()
        except Exception:
            pass
        (folder / "native-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(folder / "native-report.json"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(renderer() if "--canvas-child" in sys.argv else main())
