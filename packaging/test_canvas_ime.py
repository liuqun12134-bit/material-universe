"""Headless Chromium IME regression against the real canvas and local persistence.

Uses Chromium's IME protocol, without sending keys to the user's desktop or
submitting generation tasks. Build the canvas before running this file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))


def main():
    from playwright.sync_api import sync_playwright
    from canvas_backend import CanvasService
    from desktop_tasks import DesktopStore, TaskManager
    from model_runner.registry import ModelRegistry

    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", type=Path, default=ROOT / "skills/video-product-swap/canvas/dist")
    options = parser.parse_args()
    folder = ROOT / ".build/canvas-integrated-20261003" / ("ime-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    store = DesktopStore(folder / "state")
    def no_generation(*_):
        raise AssertionError("IME validation must not generate media")
    tasks = TaskManager(store, execute=no_generation)
    models = ModelRegistry(ROOT / "skills/video-product-swap/references").list_models()
    service = CanvasService(store, tasks, options.assets_root, lambda: {}, models=models)
    canvas = service.canvases.create("输入法回归")
    family = next(item for item in service.model_families if item["nodeType"] == "video")
    config = {"familyId": family["id"], "nodeType": "video", "prompt": "把参考视频",
              "model": next(route["modelId"] for route in family["routes"] if route["when"] in ("no_references", "always")),
              "referenceAssetIds": [], "parameters": {item["key"]: item["default"] for item in family["parameters"]}}
    service.canvases.save(canvas["id"], {"revision": 0, "nodes": [
        {"id": "prompt", "type": "task", "selected": True, "position": {"x": 30, "y": 20}, "data": {"kind": "general", "config": config}},
        {"id": "note", "type": "note", "position": {"x": 560, "y": 20}, "data": {"text": "备注"}}
    ], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}})
    report = {"paid_tasks": False, "assets_root": str(options.assets_root), "passed": False}
    def saved_value(node_id):
        data = next(node for node in service.canvases.get(canvas["id"])["nodes"] if node["id"] == node_id)["data"]
        return data["config"]["prompt"] if node_id == "prompt" else data["text"]
    def await_saved(page, node_id, expected):
        deadline = time.monotonic() + 5
        while saved_value(node_id) != expected and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert saved_value(node_id) == expected, (node_id, saved_value(node_id), expected)
    try:
        url = service.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            try:
                page = browser.new_page(viewport={"width": 1500, "height": 920})
                page.add_init_script("window.hostCalls=[];window.pywebview={api:{host_action:a=>{window.hostCalls.push(a);return Promise.resolve()}}}")
                counts = {"polls": 0, "writes": 0}
                def count_requests(request):
                    if request.url.endswith("/api/state"):
                        counts["polls"] += 1
                    if request.method == "PUT" and "/api/canvases/" in request.url:
                        counts["writes"] += 1
                page.on("request", count_requests)
                page.goto(url)
                prompt = page.locator(".creation-prompt textarea")
                prompt.wait_for()
                page.wait_for_timeout(850)  # Settle initial node measurements/autosave.
                prompt.focus()
                prompt.evaluate("e=>{e.setSelectionRange(e.value.length,e.value.length);window.imeInput=e;window.imeEvents=[];for(const k of ['compositionstart','compositionend','input','blur'])e.addEventListener(k,v=>window.imeEvents.push({type:k,composing:v.isComposing,value:e.value}))}")
                cdp = page.context.new_cdp_session(page)
                start_counts = dict(counts)
                cdp.send("Input.imeSetComposition", {"text": "zhong", "selectionStart": 5, "selectionEnd": 5})
                page.wait_for_timeout(3850)
                evidence = page.evaluate("({value:imeInput.value,same:imeInput===document.querySelector('.creation-prompt textarea'),focused:document.activeElement===imeInput,events:imeEvents})")
                evidence.update({"polls": counts["polls"] - start_counts["polls"], "writes": counts["writes"] - start_counts["writes"], "saved": saved_value("prompt")})
                report["active_composition"] = evidence
                assert evidence["polls"] >= 3 and evidence["same"] and evidence["focused"]
                assert evidence["value"] == "把参考视频zhong"
                assert not any(item["type"] in ("compositionend", "blur") for item in evidence["events"])
                assert evidence["saved"] == "把参考视频", "Uncommitted pinyin escaped into the saved canvas"
                assert evidence["writes"] == 0, "Composition must not schedule autosave"
                page.evaluate("for(const opts of [{isComposing:true},{keyCode:229}])for(const key of ['Escape','F11','Delete','z'])window.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key,ctrlKey:key==='z',...opts}))")
                assert page.evaluate("hostCalls") == []
                cdp.send("Input.insertText", {"text": "中文"})
                await_saved(page, "prompt", "把参考视频中文")
                prompt.evaluate("e=>e.blur()")
                page.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'z',ctrlKey:true}))")
                await_saved(page, "prompt", "把参考视频")
                assert prompt.input_value() == "把参考视频"
                page.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'y',ctrlKey:true}))")
                await_saved(page, "prompt", "把参考视频中文")
                assert prompt.input_value() == "把参考视频中文"
                report["chinese_commit_and_single_undo_redo"] = True
                report["ime_keys_do_not_trigger_host_or_canvas_actions"] = True
                page.reload()
                prompt.wait_for()
                assert prompt.input_value() == "把参考视频中文"
                note = page.locator(".note-input")
                note.focus()
                note.evaluate("e=>e.setSelectionRange(e.value.length,e.value.length)")
                cdp.send("Input.imeSetComposition", {"text": "beizhu", "selectionStart": 6, "selectionEnd": 6})
                page.wait_for_timeout(1400)
                assert saved_value("note") == "备注"
                cdp.send("Input.insertText", {"text": "中文"})
                # Flush immediately; the normal 650ms autosave has not run yet.
                page.evaluate("window.canvasFlush()")
                assert saved_value("note") == "备注中文"
                report["note_composition_and_immediate_close_flush"] = True
                page.reload()
                note.wait_for()
                assert note.input_value() == "备注中文" and prompt.input_value() == "把参考视频中文"
                # Native blur-before-compositionend fallback, without synthetic
                # keyboard or paid actions. The DOM holds the final chosen text.
                note.focus()
                note.evaluate("e=>{e.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));e.value='关闭时已选中文';e.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true,data:'中文'}))}")
                page.evaluate("window.canvasFlush()")
                assert saved_value("note") == "关闭时已选中文"
                page.reload()
                note.wait_for()
                assert note.input_value() == "关闭时已选中文"
                assert not tasks.active and not tasks.pending and not store.jobs()
                report["reopen_persisted_and_blur_before_end_flush"] = True
                note.focus()
                note.evaluate("e=>e.setSelectionRange(0,e.value.length)")
                cdp.send("Input.imeSetComposition", {"text": "原生组合关闭中文", "selectionStart": 8, "selectionEnd": 8})
                page.evaluate("window.canvasFlush()")
                assert saved_value("note") == "原生组合关闭中文"
                page.reload()
                note.wait_for()
                assert note.input_value() == "原生组合关闭中文"
                report["real_composition_active_close_flush"] = True
                report["passed"] = True
            finally:
                browser.close()
    finally:
        service.stop()
        tasks.stop()
        tasks.wait_idle(3)
        store.db.close()
        (folder / "ime-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(folder / "ime-report.json"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
