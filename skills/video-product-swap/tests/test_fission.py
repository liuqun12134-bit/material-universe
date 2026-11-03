import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from PIL import Image
from desktop_tasks import DesktopStore, TaskManager
from fission_store import FissionStore, branch_turns
from fission_service import (MODEL, RATIOS, inspect_image, image_inputs, prepare_quote,
                             validate_approved, run_job)
from fission_chat import context, chat
from fish_mcp import FishError
from model_runner.task_state import TaskRecord


def model():
    return {"model_id": MODEL, "prompt_max_chars": 32000, "capabilities": {"edit": {
        "max_inputs": 16, "inputs": {"ref": {"max_count": 16, "max_bytes": 20971520,
        "mime_types": ["image/png", "image/jpeg", "image/webp"]}}, "parameter_schema": {"properties": {
        "aspect_ratio": {"enum": list(RATIOS)}, "resolution": {"enum": ["1K", "2K", "4K"]},
        "quality": {"enum": ["low", "medium", "high", "xhigh", "max"]}}}}}}


class Fish:
    connection_id = "account-one"

    def __init__(self, *_):
        self.calls = []
        self.fail_submit = False
        self.fail_poll = False
        self.status = {"status": "completed", "media": {"url": "https://example.invalid/image", "object_key": "generated-key"}}

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def call(self, tool, args):
        self.calls.append((tool, copy.deepcopy(args)))
        if tool == "get_media_model": return model()
        if tool == "list_my_workspaces": return {"workspaces": [{"workspace_id": "workspace"}]}
        if tool == "create_media_upload": return {"upload_url": "https://example.invalid/upload", "object_key": "input-" + str(len(self.calls))}
        if tool == "estimate_image_edit": return {"can_generate": True, "credits": 636, "pricing_version": "test-price", "balance": 10000}
        if tool == "generate_image_edit":
            if self.fail_submit: raise FishError("lost submission response")
            return {"generation_id": "generation-one", "status": "queued", "poll_after_seconds": 20}
        if tool == "get_generation_status":
            if self.fail_poll: raise FishError("lost query response")
            return self.status
        raise AssertionError(tool)


class Download:
    def __init__(self, content): self.content = content
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def raise_for_status(self): pass
    def iter_content(self, _): yield self.content


class FissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DesktopStore(self.root / "data")
        self.sessions = FissionStore(self.store)
        self.session = self.sessions.create()
        self.sid = self.session["id"]
        for index, role in enumerate(("original", "product")):
            path = self.root / (role + ".png")
            Image.new("RGB", (480, 640), (160, 50 * index, 90)).save(path)
            asset = self.store.import_asset(path)
            self.sessions.reference(self.sid, role, {**inspect_image(asset["path"]), "name": asset["name"], "asset_id": asset["id"]})
        self.client = Fish()
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Real network prohibited"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def config(self):
        session = self.sessions.get(self.sid)
        return {"session_id": self.sid, "prompt": "只调整瓶子反光，人物不变", "mode": "refine", "locks": "保留人物",
                "model": MODEL, "parameters": {"aspect_ratio": "跟随底图", "resolution": "1K", "quality": "high"},
                "image_inputs": image_inputs(session, self.sessions), "workspace_id": "workspace",
                "output": str(self.root / "result.png")}

    def quote(self, config=None, client=None):
        with patch("fission_service.put_media"):
            result = prepare_quote(config or self.config(), self.store, client or self.client)
        result["cost_approved"] = True
        return result

    def png(self):
        out = io.BytesIO()
        Image.new("RGB", (450, 600), "navy").save(out, format="PNG")
        return out.getvalue()

    def run_job(self, config, resume=False):
        return run_job({"config": config}, {"FISH_AUTH_PATH": "test-auth"}, lambda **_: None,
                       resume, client_factory=lambda *_: self.client, sleep=lambda s: self.assertGreaterEqual(s, 20))

    def test_quote_is_read_only_and_cached_uploads_are_account_workspace_scoped(self):
        first = self.quote()
        self.assertEqual(first["parameters"]["aspect_ratio"], "3:4")
        count = sum(t == "create_media_upload" for t, _ in self.client.calls)
        self.assertEqual(count, 2)
        self.quote()
        self.assertEqual(sum(t == "create_media_upload" for t, _ in self.client.calls), count)
        other = Fish(); other.connection_id = "another-account"
        self.quote(client=other)
        self.assertEqual(sum(t == "create_media_upload" for t, _ in other.calls), 2)
        config = self.config(); config["workspace_id"] = "another-workspace"
        self.quote(config)
        self.assertEqual(sum(t == "create_media_upload" for t, _ in self.client.calls), 4)
        self.assertFalse(any(t.startswith("generate_") for t, _ in self.client.calls))
        self.assertNotIn("upload_url", str(list(self.store.db.execute("SELECT body FROM image_uploads"))))

    def test_frozen_prompt_parameters_references_and_approval(self):
        config = self.quote()
        validate_approved(config)
        for key, value in (("prompt", "different"), ("locks", "different"), ("mode", "restart"), ("cost_approved", False)):
            changed = {**config, key: value}
            with self.assertRaises(ValueError): validate_approved(changed)
        changed = copy.deepcopy(config); changed["parameters"]["quality"] = "low"
        with self.assertRaises(ValueError): validate_approved(changed)
        Path(config["image_inputs"][0]["path"]).write_bytes(self.png())
        with self.assertRaises(ValueError): validate_approved(config)

    def test_success_resume_and_generated_object_reused_without_upload(self):
        config = self.quote()
        with patch("fission_service.requests.get", return_value=Download(self.png())):
            result = self.run_job(config)
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(result["media_object_key"], "generated-key")
        self.assertEqual(result["workspace_id"], "workspace")
        before = len(self.client.calls)
        self.assertTrue(self.run_job(config, resume=True)["resumed_existing_task"])
        self.assertEqual(len(self.client.calls), before)
        new = self.config()
        new["image_inputs"].insert(0, {**inspect_image(result["output"]), "role": "base", "label": "V1",
            "remote": {"object_key": result["media_object_key"], "connection_id": result["connection_id"], "workspace_id": result["workspace_id"]}})
        prepared = self.quote(new)
        self.assertEqual(prepared["fish_quote"]["arguments"]["inputs"][0]["object_key"], "generated-key")
        self.assertEqual(sum(t == "create_media_upload" for t, _ in self.client.calls), 2)
        self.assertEqual(sum(t == "generate_image_edit" for t, _ in self.client.calls), 1)

    def test_submission_timeout_is_durable_and_never_resubmitted(self):
        config = self.quote(); self.client.fail_submit = True
        with self.assertRaises(FishError): self.run_job(config)
        state = TaskRecord(TaskRecord.path_for(Path(config["output"]))).read().data
        self.assertEqual(state["status"], "submission_unknown")
        self.assertTrue(state["idempotency_key"])
        with self.assertRaises(FishError): self.run_job(config, resume=True)
        with self.assertRaises(Exception): self.run_job(config)
        self.assertEqual(sum(t == "generate_image_edit" for t, _ in self.client.calls), 1)

    def test_query_and_download_failures_recover_original_job_only(self):
        config = self.quote(); self.client.fail_poll = True
        with self.assertRaises(FishError): self.run_job(config)
        self.client.fail_poll = False
        with patch("fission_service.requests.get", return_value=Download(b"not an image")):
            result = self.run_job(config, resume=True)
        self.assertFalse(result["local_downloaded"])
        self.assertFalse(Path(config["output"]).exists())
        count = len(self.client.calls)
        with patch("fission_service.requests.get", return_value=Download(self.png())):
            result = self.run_job(config, resume=True)
        self.assertTrue(result["local_downloaded"])
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(sum(t == "generate_image_edit" for t, _ in self.client.calls), 1)

    def test_failed_remote_job_is_not_retried(self):
        config = self.quote(); self.client.status = {"status": "failed"}
        with self.assertRaises(Exception): self.run_job(config)
        before = len(self.client.calls)
        with self.assertRaises(FishError): self.run_job(config, resume=True)
        self.assertEqual(len(self.client.calls), before)

    def test_frozen_quote_validates_live_model_before_any_upload(self):
        config = self.config(); config["parameters"]["quality"] = "unknown"
        with patch("fission_service.put_media") as put:
            with self.assertRaises(ValueError): prepare_quote(config, self.store, self.client)
        put.assert_not_called()
        self.assertFalse(any(t == "create_media_upload" for t, _ in self.client.calls))

    def turn(self, text):
        s = self.sessions.begin_turn(self.sid, text)
        return self.sessions.finish_turn(self.sid, s["cursor"], {"reply": "已整理", "prompt": text, "summary": text}, expected=s["revision"])

    def test_branching_reopens_without_sibling_feedback_and_restart_uses_originals(self):
        s = self.turn("人物已满意，只改瓶子")
        s = self.sessions.reserve(self.sid, self.quote(), s["revision"])
        v1 = s["versions"][-1]
        # A finished job is durable before its image becomes the next input.
        output = self.root / "v1.png"; output.write_bytes(self.png())
        asset = self.store.import_asset(output)
        self.store._put("jobs", {"id": "job-one", "kind": "image_fission", "created": 1, "status": "success",
                               "config": {}, "result": {"output": str(output), "asset_id": asset["id"], "asset_path": asset["path"]}})
        self.sessions.attach_job(self.sid, v1["id"], "job-one")
        self.sessions.select(self.sid, v1["id"])
        self.turn("分支 A：改成红衣")
        self.sessions.fields(self.sid, locks="另一分支的保留要求")
        self.sessions.reference(self.sid, "product", None)
        self.sessions.select(self.sid, v1["id"])
        s = self.turn("分支 B：保持衣服，只改瓶盖")
        self.assertNotIn("另一分支", s["locks"])
        self.assertEqual(len(s["references"]), 2)
        history = json.dumps(context(s, image_inputs(s, self.sessions)), ensure_ascii=False)
        self.assertIn("人物已满意", history)
        self.assertIn("分支 B", history)
        self.assertNotIn("分支 A", history)
        self.assertEqual(image_inputs(s, self.sessions)[0]["role"], "base")
        s = self.sessions.fields(self.sid, mode="restart")
        self.assertEqual([r["role"] for r in image_inputs(s, self.sessions)], ["original", "product"])
        other = DesktopStore(self.store.root)
        try: self.assertEqual(FissionStore(other).get(self.sid), s)
        finally: other.db.close()

    def test_slow_chat_keeps_new_input_and_does_not_overwrite_prompt(self):
        s = self.sessions.begin_turn(self.sid, "只改产品")
        self.sessions.fields(self.sid, input="还有一个要求", prompt="用户新修改稿")
        result = self.sessions.finish_turn(self.sid, s["cursor"], {"reply": "旧回答", "prompt": "旧修改稿", "summary": "摘要"}, expected=s["revision"])
        self.assertEqual(result["input"], "还有一个要求")
        self.assertEqual(result["prompt"], "用户新修改稿")
        self.assertEqual(result["turns"][-1]["reply"], "旧回答")

    def test_archived_sessions_do_not_prevent_asset_deletion(self):
        original = self.sessions.get(self.sid)["references"][0]
        self.turn("请按原图修改")
        self.sessions.reference(self.sid, "original", None)
        self.store.rename_asset(original["asset_id"], "可以重命名")
        self.store.delete_asset(original["asset_id"])
        self.assertFalse(Path(original["path"]).exists())

    def test_chat_receives_actual_images_and_summary_not_other_branch(self):
        for i in range(8): self.turn("修改意见 " + str(i))
        session = self.sessions.get(self.sid)
        refs = image_inputs(session, self.sessions)
        seen = []
        def ask_fn(credentials, system, content):
            seen.extend(content)
            return {"reply": "已看图", "prompt": "@图1 保持人物", "summary": "累计反馈"}
        reply = chat(session, refs, {}, ask_fn)
        self.assertEqual(reply["reply"], "已看图")
        self.assertEqual(sum(p["type"] == "image_url" for p in seen), 2)
        self.assertIn("较早上下文摘要", seen[0]["text"])
        self.assertIn("修改意见 1", seen[0]["text"])

    def test_duplicate_clicks_one_job_and_session_link_survives_lost_local_reply(self):
        manager = TaskManager(self.store, execute=lambda *a: {"local_downloaded": False})
        try:
            config = self.quote()
            s = self.sessions.get(self.sid)
            s = self.sessions.reserve(self.sid, config, s["revision"])
            v = s["versions"][-1]
            first = manager.submit("image_fission", config, {}, request_id=v["request_id"])
            second = manager.submit("image_fission", config, {}, request_id=v["request_id"])
            self.assertEqual(first, second)
            self.assertEqual(self.sessions.job_for(v)["id"], first)
            manager.wait_idle()
            self.store.delete_job(first)
        finally:
            manager.stop(); manager.wait_idle()

    def test_manual_status_refresh_keeps_remote_image_key_for_reuse(self):
        from history_sync import fetch_status
        with patch("history_sync.FishClient", return_value=self.client):
            fetched = fetch_status({"model": {"provider": "fish_audio", "adapter": "fish_image"}, "task_id": "generation-one"}, {})
        self.assertEqual(fetched[4]["media_object_key"], "generated-key")
        self.assertEqual(self.client.calls[0][0], "get_generation_status")

    def test_old_workspace_result_is_uploaded_once_to_new_workspace(self):
        config = self.config()
        config["image_inputs"][0]["remote"] = {"object_key": "old-key", "workspace_id": "old-workspace",
                                                   "connection_id": "account-one", "generation_id": "old-generation"}
        first = self.quote(config)
        second = self.quote(config)
        self.assertEqual(first["fish_quote"]["arguments"]["inputs"], second["fish_quote"]["arguments"]["inputs"])
        self.assertEqual(sum(t == "create_media_upload" for t, _ in self.client.calls), 2)


if __name__ == "__main__": unittest.main()
