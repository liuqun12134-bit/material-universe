"""Offline canvas contracts: one paid request, durable references and local media."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from canvas_backend import CanvasService
from canvas_catalog import GENERAL_MODELS, available_families
from desktop_tasks import DesktopStore, TaskManager
from canvas_window import save_download_result


def wait_for(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


class CanvasBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "library")
        self.assets_root = self.root / "dist"
        self.assets_root.mkdir()
        (self.assets_root / "index.html").write_text("canvas", encoding="utf-8")
        video_file = self.root / "source.mp4"
        video_file.write_bytes(b"1234567890")
        image_file = self.root / "product.png"
        Image.new("RGB", (8, 8), "red").save(image_file)
        self.video = self.store.import_asset(video_file)
        self.image = self.store.import_asset(image_file)
        self.executions = []
        self.release = threading.Event()

        def execute(job, credentials, update, store, resume):
            self.executions.append((job["id"], resume))
            self.release.wait(2)
            return {"output": job["config"]["output"], "local_downloaded": True}

        self.tasks = TaskManager(self.store, execute=execute, limit=2)
        self.service = CanvasService(self.store, self.tasks, self.assets_root, lambda: {},
                                     models=[{"model": "wan3.0-video", "adapter": "fal_wan3", "provider": "fal"}])
        self.canvas = self.service.canvases.create()
        self.request_id = uuid.uuid4().hex
        self.config = {"sourceAssetId": self.video["id"], "imageAssetId": self.image["id"],
                       "promptMode": "reuse", "prompt": "  用户原样提示词\n", "duration": 5,
                       "model": "wan3.0-video", "aspectRatio": "16:9", "resolution": "480p",
                       "referenceAssetIds": [self.image["id"]], "parameters": {"duration": 5, "resolution": "480p"}}
        self.node = {"id": "task-1", "type": "task", "position": {"x": 4, "y": 9},
                     "data": {"kind": "general", "requestId": self.request_id, "config": self.config}}
        self.save_task(self.canvas, self.node)

    def save_task(self, canvas, node):
        import copy
        draft = copy.deepcopy(node)
        request_id = draft["data"].pop("requestId")
        nodes = [draft]
        edges = []
        for index, identity in enumerate(draft["data"]["config"].get("referenceAssetIds", [])):
            nodes.append({"id": identity, "type": "asset", "position": {"x": 0, "y": 0}, "data": {"assetId": identity}})
            edges.append({"id": "input-" + identity, "source": identity, "target": draft["id"], "data": {"kind": "input", "assetId": identity, "order": index}})
        saved = self.service.canvases.save(canvas["id"], {"nodes": nodes, "edges": edges, "viewport": canvas["viewport"], "revision": canvas["revision"]})
        draft["data"]["requestId"] = request_id
        self.service.canvases.save(canvas["id"], {"nodes": nodes, "edges": edges, "viewport": canvas["viewport"], "revision": saved["revision"]})

    def tearDown(self):
        self.release.set()
        self.tasks.stop()
        self.tasks.wait_idle(4)
        self.tasks.stop()  # Release the Windows instance lock after any active workers finish.
        self.service.stop()
        self.store.db.close()
        self.temp.cleanup()

    def payload(self):
        return {"canvasId": self.canvas["id"], "nodeId": self.node["id"], "requestId": self.request_id}

    def test_native_and_browser_result_downloads_are_authorized_and_exact(self):
        job = self.service.submit(self.payload())
        self.release.set()
        wait_for(lambda: not self.tasks.active)
        source = Path(job["config"]["output"])
        source.parent.mkdir(parents=True, exist_ok=True)
        url = self.service.start()
        base = url.split('/#')[0]
        info_url = f"{base}/api/jobs/{job['id']}/download-info"
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(info_url, auth=False)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        for suffix in (".mp4", ".png"):
            source = source.with_suffix(suffix)
            source.write_bytes(b"exact-result-bytes")
            self.store.update(job["id"], result={"output": str(source), "local_downloaded": True})
            target = self.root / ("saved" + suffix)
            result = save_download_result(url, job["id"], lambda name: (str(target),))
            self.assertFalse(result["cancelled"])
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(save_download_result(url, job["id"], lambda _: None), {"cancelled": True})
            with self.request(f"{base}/media/job/{job['id']}?download=1") as response:
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertEqual(response.read(), source.read_bytes())
            source.unlink()
            with self.assertRaises(ValueError):
                save_download_result(url, job["id"], lambda _: self.fail("must not prompt for missing result"))
        with self.assertRaises(ValueError):
            save_download_result(url, "../../secret", lambda _: self.fail("must not prompt"))

    def request(self, url, method="GET", body=None, auth=True, headers=None):
        request_headers = dict(headers or {})
        if auth:
            request_headers["X-Canvas-Token"] = self.service.token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        return urllib.request.urlopen(request, timeout=5)

    def test_concurrent_idempotency_and_restart_mapping(self):
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(self.service.submit(self.payload())["id"])) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(set(answers)), 1)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.store.job(answers[0])["config"]["prompt"], "  用户原样提示词\n")
        self.release.set()
        wait_for(lambda: not self.tasks.active)
        self.tasks.stop()
        self.store.db.close()
        self.store = DesktopStore(self.root / "library")
        self.tasks = TaskManager(self.store, execute=lambda *_: self.fail("replayed paid call"))
        self.service.store, self.service.tasks = self.store, self.tasks
        self.service.canvases.store = self.store
        self.assertEqual(self.service.submit(self.payload())["id"], answers[0])

    def test_system_output_directory_changes_only_new_jobs_and_uses_one_snapshot(self):
        first_folder = self.root / "configured" / "first"
        second_folder = self.root / "configured" / "second"
        credentials = {"WORKSPACE_OUTPUT_DIR": str(first_folder), "FAL_KEY": "offline-test"}
        self.service.credential_provider = Mock(return_value=credentials)
        with patch.object(self.tasks, "submit", wraps=self.tasks.submit) as submit:
            first = self.service.submit(self.payload())
            self.assertIs(submit.call_args.args[2], credentials)
        self.service.credential_provider.assert_called_once_with()
        self.assertEqual(Path(first["config"]["output"]).parent, first_folder.resolve())
        self.assertTrue(first_folder.is_dir())
        self.assertEqual(first["config"]["canvas_draft"], self.config)
        credentials = {"WORKSPACE_OUTPUT_DIR": str(second_folder)}
        self.service.credential_provider = Mock(return_value=credentials)
        restored = self.service.submit(self.payload())
        self.assertEqual(restored["id"], first["id"])
        self.assertEqual(restored["config"]["output"], first["config"]["output"])
        self.service.credential_provider.assert_not_called()
        self.assertFalse(second_folder.exists())
        canvas = self.service.canvases.create()
        request_id = uuid.uuid4().hex
        node = {**self.node, "data": {**self.node["data"], "requestId": request_id}}
        self.save_task(canvas, node)
        second = self.service.submit({"canvasId": canvas["id"], "nodeId": node["id"], "requestId": request_id})
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(Path(second["config"]["output"]).parent, second_folder.resolve())
        self.service.credential_provider.assert_called_once_with()

    def test_all_kinds_restore_original_job_without_current_credentials_or_revalidation(self):
        audio_file = self.root / "speech.wav"
        audio_file.write_bytes(b"offline audio fixture")
        audio = self.store.import_asset(audio_file)
        self.service.general_models = [m for m in GENERAL_MODELS if m["id"] == "wan3.0-t2v"]
        self.service.model_families = available_families(self.service.general_models)
        drafts = {"full": {k: v for k, v in self.config.items() if k not in {"referenceAssetIds", "parameters"}},
            "lipsync": {"videoAssetId": self.video["id"], "audioAssetId": audio["id"]},
            "repair": {"videoAssetId": self.video["id"], "options": {"upscale_factor": 3}},
            "general": {"model": "wan3.0-t2v", "prompt": "exact original prompt", "referenceAssetIds": [],
                "parameters": {"enable_thinking": True, "enable_prompt_expansion": True}}}
        for legacy in (False, True):
            for kind, draft in drafts.items():
                with self.subTest(kind=kind, legacy=legacy):
                    canvas = self.service.canvases.create()
                    request_id = uuid.uuid4().hex
                    node = {"id": "task", "type": "task", "position": {"x": 0, "y": 0},
                        "data": {"kind": kind, "config": draft}}
                    self.service.canvases.save(canvas["id"], {"nodes": [node], "edges": [],
                        "viewport": canvas["viewport"], "revision": 0})
                    node["data"]["requestId"] = request_id
                    self.service.canvases.save(canvas["id"], {"nodes": [node], "edges": [],
                        "viewport": canvas["viewport"], "revision": 1})
                    payload = {"canvasId": canvas["id"], "nodeId": "task", "requestId": request_id}
                    self.service.credential_provider = lambda: {"WORKSPACE_OUTPUT_DIR": str(self.root / "original")}
                    # Seed a pre-upgrade accepted job; restore below runs the real retirement policy.
                    with patch("feature_policy.require_creation"), patch.object(self.service, "_validate_adapter"), patch.object(self.service, "_validate_general_adapter"):
                        if legacy:
                            config = self.service._config(kind, draft, request_id)
                            identity = self.tasks.submit(kind, config, {}, request_id=request_id)
                            original = self.store.job(identity)
                        else:
                            original = self.service.submit(payload)
                            self.assertEqual(original["config"]["canvas_draft"], draft)
                    with patch.object(self.service, "credential_provider", side_effect=AssertionError("current credentials unavailable")), \
                            patch.object(self.service, "_config", side_effect=AssertionError("must preserve original output")), \
                            patch.object(self.tasks, "submit", side_effect=AssertionError("must not submit twice")):
                        restored = self.service.submit(payload)
                        self.assertEqual(restored["id"], original["id"])
                        self.assertEqual(restored["config"]["output"], original["config"]["output"])
                        changed = self.service.canvases.get(canvas["id"])
                        changed_draft = changed["nodes"][0]["data"]["config"]
                        if kind in {"full", "general"}:
                            changed_draft["prompt"] = "changed prompt"
                        elif kind == "lipsync":
                            changed_draft["audioAssetId"] = self.video["id"]
                        else:
                            changed_draft["options"] = {"upscale_factor": 4}
                        with patch.object(self.service.canvases, "get", return_value=changed):
                            with self.assertRaisesRegex(ValueError, "配置已改变"):
                                self.service.submit(payload)

    def test_retired_swap_options_cannot_create_new_tasks(self):
        for change in ({}, {"removeSubtitles": True}, {"enableThinking": False}):
            with self.assertRaisesRegex(ValueError, "已下线"):
                self.service._config("full", {**self.config, **change}, self.request_id)

    def test_revision_and_references_protect_assets_and_jobs(self):
        with self.assertRaises(RuntimeError):
            self.service.canvases.save(self.canvas["id"], {"nodes": [], "edges": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1}, "revision": 0})
        canvas = self.service.canvases.get(self.canvas["id"])
        self.service.canvases.save(self.canvas["id"], {"nodes": canvas["nodes"], "edges": canvas["edges"],
            "viewport": canvas["viewport"], "revision": canvas["revision"]})
        with self.assertRaises(ValueError):
            self.store.delete_asset(self.image["id"])
        job = self.service.submit(self.payload())
        self.release.set()
        wait_for(lambda: not self.tasks.active)
        with self.assertRaises(ValueError):
            self.store.delete_job(job["id"])

    def test_http_auth_range_upload_and_validation(self):
        url = self.service.start().split("/#")[0]
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(url + "/media/asset/" + self.video["id"], auth=False)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        with self.request(url + "/media/asset/" + self.video["id"] + "?token=" + self.service.token,
                          auth=False, headers={"Range": "bytes=2-5"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"3456")
        with self.request(url + "/api/jobs/validate", "POST", {**self.payload(), "kind": "general", "config": self.config}) as response:
            self.assertTrue(json.load(response)["valid"])
        bad = {**self.config, "parameters": {"duration": 2.5}}
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(url + "/api/jobs/validate", "POST", {**self.payload(), "kind": "general", "config": bad})
        self.assertEqual(json.load(denied.exception)["submitted"], False)
        denied.exception.close()
        upload = urllib.request.Request(url + "/api/assets/upload?name=drop.mp4", data=b"media",
            headers={"X-Canvas-Token": self.service.token}, method="POST")
        with urllib.request.urlopen(upload, timeout=5) as response:
            asset = json.load(response)
        self.assertEqual(asset["name"], "drop.mp4")
        self.assertEqual(Path(asset["path"]).read_bytes(), b"media")

    def test_http_asset_groups_and_import_target_snapshot(self):
        group = self.store.create_group("口播素材")
        url = self.service.start().split("/#")[0]
        with self.request(url + "/api/bootstrap") as response:
            self.assertEqual(json.load(response)["groups"][0]["id"], group["id"])
        with self.request(url + "/api/state") as response:
            self.assertEqual(json.load(response)["groups"][0]["name"], "口播素材")
        upload = urllib.request.Request(url + "/api/assets/upload?name=new.mp4&group_id=" + group["id"],
            data=b"new content", headers={"X-Canvas-Token": self.service.token}, method="POST")
        with urllib.request.urlopen(upload, timeout=5) as response:
            asset = json.load(response)
        self.assertEqual(asset["group_id"], group["id"])
        with self.request(url + "/api/assets/import", "POST",
                          {"paths": [asset["path"]], "group_id": None}) as response:
            self.assertEqual(json.load(response)[0]["group_id"], group["id"])
        self.store.delete_group(group["id"])
        stale = urllib.request.Request(url + "/api/assets/upload?name=stale.mp4&group_id=" + group["id"],
            data=b"different", headers={"X-Canvas-Token": self.service.token}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(stale, timeout=5)
        self.assertIn("已删除", json.load(denied.exception)["error"])
        denied.exception.close()
        self.assertEqual(len(self.store.assets()), 3)

    def test_paused_local_can_start_but_submission_unknown_cannot(self):
        identity = self.service.submit(self.payload())["id"]
        self.release.set()
        wait_for(lambda: not self.tasks.active)
        self.store.update(identity, status="paused")
        self.service.recover(identity)
        wait_for(lambda: not self.tasks.active)
        self.assertEqual(len(self.executions), 2)
        record = Path(self.store.job(identity)["config"]["output"] + ".video-task.json")
        record.parent.mkdir(exist_ok=True)
        record.write_text(json.dumps({"status": "submission_unknown"}), encoding="utf-8")
        self.store.update(identity, status="paused")
        with self.assertRaises(ValueError):
            self.service.recover(identity)


if __name__ == "__main__":
    unittest.main()
