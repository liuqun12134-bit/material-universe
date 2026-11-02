"""Headless canvas acceptance; local mock execution, no paid calls or uploads."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))


def main():
    from playwright.sync_api import sync_playwright, expect
    from canvas_backend import CanvasService
    from canvas_demo import _make_media, mock_execute
    from desktop_tasks import DesktopStore, TaskManager
    from model_runner.registry import ModelRegistry

    folder = ROOT / ".build" / ("kaiyun-canvas-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    video_path, image_path, _ = _make_media(folder / "media")
    audio_path = folder / "media" / "seedance-voice.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio_path)], check=True, capture_output=True)
    store = DesktopStore(folder / "state")
    tasks = TaskManager(store, execute=mock_execute)
    credentials = {"KAIYUNCODE_API_KEY": "offline-test-only",
                   "WORKSPACE_OUTPUT_DIR": str(folder / "output")}
    service = CanvasService(store, tasks, ROOT / "skills/video-product-swap/canvas/dist",
                            lambda: credentials,
                            models=ModelRegistry(ROOT / "skills/video-product-swap/references").list_models())
    store.import_asset(video_path)
    store.import_asset(image_path)
    store.import_asset(audio_path)
    image_canvas = service.canvases.create("生图验收")
    video_canvas = service.canvases.create("视频验收")
    errors = []
    report = {"passed": False, "paid_calls": 0, "public_uploads": 0}

    def saved_config(canvas_id):
        nodes = service.canvases.get(canvas_id)["nodes"]
        return next(node["data"]["config"] for node in nodes if node["type"] == "task")

    def flush(page):
        page.locator(".creation-prompt textarea").evaluate_all("(xs)=>xs.forEach(x=>x.blur())")
        # canvasFlush is the native close handshake and intentionally locks editing.
        page.wait_for_timeout(800)
        expect(page.locator(".save-state")).to_have_text("已保存")

    try:
        url = service.start()
        # requests is used by provider adapters; any accidental real call fails.
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel="msedge")
            try:
                page = browser.new_page(viewport={"width": 1800, "height": 1200}, accept_downloads=True)
                page.set_default_timeout(10000)
                def field(label):
                    return page.locator(".creation-controls label").filter(
                        has_text=re.compile("^" + re.escape(label))).locator("select")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url.split("/?")[0].split("#")[0].rstrip("/") + "/") else route.abort())
                page.goto(url)
                selector = page.locator(".canvas-select select")
                selector.select_option(image_canvas["id"])
                pane = page.locator(".react-flow__pane")
                pane.click(button="right", position={"x": 300, "y": 100})
                page.get_by_role("menuitem", name="新增生图节点").click()
                expect(page.locator(".fixed-model")).to_have_text("Nano Banana 2.1")
                field("尺寸").select_option("2K")
                field("比例").select_option("4:3")
                page.locator(".creation-prompt textarea").fill("保留原始提示词，生成产品白底图")
                flush(page)
                config = saved_config(image_canvas["id"])
                assert config["parameters"]["image_size"] == "2K"
                assert config["parameters"]["aspect_ratio"] == "4:3"
                page.reload()
                expect(field("尺寸")).to_have_value("2K")
                page.get_by_role("button", name="开始生成", exact=True).click()
                expect(page.locator(".image-result")).to_be_visible(timeout=15000)
                job = store.jobs()[0]
                assert job["config"]["model"] == "kaiyuncode-gemini-nano-banana-2.1"
                assert job["config"]["prompt"] == "保留原始提示词，生成产品白底图"
                assert Path(job["result"]["output"]).suffix == ".png"
                page.locator(".image-result").click()
                expect(page.get_by_role("dialog", name="生成图片预览")).to_be_visible()
                assert page.get_by_role("dialog").locator("img").evaluate("(x)=>x.complete && x.naturalWidth > 0")
                page.keyboard.press("Escape")
                expect(page.get_by_role("dialog", name="生成图片预览")).to_have_count(0)
                page.get_by_role("button", name="作为参考继续创作").click()
                expect(page.locator(".reference-tile")).to_have_count(1)
                flush(page)
                draft = next(n for n in service.canvases.get(image_canvas["id"])["nodes"]
                             if n["type"] == "task" and not n["data"].get("requestId"))
                refs = draft["data"]["config"]["referenceAssetIds"]
                assert len(refs) == 1
                generated = next(asset for asset in store.assets() if asset["id"] == refs[0])
                assert Path(generated["path"]).read_bytes() == Path(job["result"]["output"]).read_bytes()
                report["image_create_generate_preview_reuse_and_persistence"] = True

                selector.select_option(video_canvas["id"])
                pane.click(button="right", position={"x": 300, "y": 100})
                page.get_by_role("menuitem", name="新增视频节点").click()
                field("服务商").select_option("kaiyuncode")
                field("型号").select_option("kaiyuncode-wan3.0-video-prime")
                flush(page)
                assert saved_config(video_canvas["id"])["model"] == "kaiyuncode-wan3.0-video-prime"
                expect(page.get_by_text("思考模式", exact=True)).to_have_count(0)
                field("清晰度").select_option("1080p")
                page.get_by_role("button", name="＋ 添加参考", exact=True).click()
                for path in (image_path, video_path, audio_path):
                    page.locator(".picker-asset").filter(has_text=path.name).click()
                page.get_by_role("button", name="完成", exact=True).click()
                expect(page.locator(".reference-tile")).to_have_count(3)
                field("创作模型").select_option("seedance-kaiyuncode")
                expect(field("创作模型")).to_have_value("seedance-kaiyuncode")
                expect(field("清晰度")).to_have_value("720p")
                expect(field("清晰度").locator("option")).to_have_count(1)
                expect(page.locator(".reference-tile")).to_have_count(3)
                field("时长（秒）").select_option("30")
                page.locator(".creation-prompt textarea").fill("保留图片、视频和音频参考，生成多参视频")
                flush(page)
                seed_config = saved_config(video_canvas["id"])
                assert seed_config["parameters"]["duration"] == 30
                assert seed_config["parameters"]["resolution"] == "720p"
                assert len(seed_config["referenceAssetIds"]) == 3
                page.reload()
                expect(field("创作模型")).to_have_value("seedance-kaiyuncode")
                expect(page.locator(".reference-tile")).to_have_count(3)
                page.screenshot(path=str(folder / "seedance-multi-reference-720p.png"), full_page=True)
                page.get_by_role("button", name="开始生成", exact=True).click()
                expect(page.get_by_role("button", name="作为参考继续创作")).to_be_visible(timeout=15000)
                seed_job = next(job for job in store.jobs() if job["config"]["model"] == "kaiyuncode-seedance-2.5-tj")
                assert seed_job["status"] == "success"
                assert seed_job["config"]["referenceAssetIds"] == seed_config["referenceAssetIds"]
                assert seed_job["config"]["parameters"]["resolution"] == "720p"
                report["seedance_mixed_references_switch_reload_and_mock_generation"] = True
                selector.select_option(image_canvas["id"])
                video_canvas = service.canvases.create("Omni 验收")
                expect(selector.locator(f'option[value="{video_canvas["id"]}"]')).to_have_count(1)
                selector.select_option(video_canvas["id"])
                pane.click(button="right", position={"x": 300, "y": 100})
                page.get_by_role("menuitem", name="新增视频节点").click()
                field("服务商").select_option("kaiyuncode")
                field("创作模型").select_option("omni-kaiyuncode")
                field("时长（秒）").select_option("10")
                flush(page)
                assert saved_config(video_canvas["id"])["parameters"]["duration"] == 10
                expect(field("清晰度")).to_have_count(0)
                report["wan_prime_seedance_omni_parameters"] = True

                field("创作模型").select_option("omni-edit-kaiyuncode")
                expect(page.get_by_text("点击生成后，本地参考图会上传", exact=False)).to_be_visible()
                expect(field("时长（秒）")).to_have_count(0)
                page.get_by_role("button", name="＋ 添加参考", exact=True).click()
                page.locator(".picker-asset").filter(has_text=image_path.name).click()
                page.locator(".picker-asset").filter(has_text=video_path.name).click()
                page.get_by_role("button", name="完成", exact=True).click()
                expect(page.locator(".reference-tile")).to_have_count(2)
                page.locator(".creation-prompt textarea").fill("把原视频中的产品替换成参考图产品")
                flush(page)
                config = saved_config(video_canvas["id"])
                assert len(config["referenceAssetIds"]) == 2
                assert set(config["parameters"]) == {"aspect_ratio"}
                page.screenshot(path=str(folder / "omni-edit.png"), full_page=True)
                page.get_by_role("button", name="开始生成", exact=True).click()
                expect(page.get_by_role("button", name="作为参考继续创作")).to_be_visible(timeout=15000)
                edit_job = next(job for job in store.jobs() if job["config"]["model"] == "kaiyuncode-omni_video_edit")
                assert edit_job["status"] == "success"
                assert len(edit_job["config"]["references"]) == 2
                report["omni_edit_local_references_notice_and_mock_generation"] = True
                assert not errors, errors
                report["passed"] = True
            except Exception:
                page.screenshot(path=str(folder / "failure.png"), full_page=True)
                raise
            finally:
                browser.close()
    finally:
        service.stop()
        tasks.stop()
        tasks.wait_idle(5)
        store.db.close()
        report["browser_errors"] = errors
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "report.json")


if __name__ == "__main__":
    main()
