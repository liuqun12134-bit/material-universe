"""Offline contracts for the single-form image workflow and paid-request boundary."""
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
from ai_image_models import (BUILTIN_MODELS, DEFAULT_MODEL, DEFAULT_PARAMETERS, FOLLOW,
                             normalize_parameters, resolve_parameters, reference_limit)
from ai_image_store import AIImageStore, helper_snapshot, validate_references, REMOVED_REFERENCE
from ai_image_assistant import generate_prompt
from desktop_tasks import DesktopStore, TaskManager, execute_job
from fish_mcp import FishError
from fission_service import inspect_image, prepare_quote, validate_approved, run_job, actual_prompt
from model_runner.task_state import TaskRecord


class Fish:
    connection_id = "offline-account"

    def __init__(self, *_):
        self.calls = []
        self.fail_submit = self.fail_poll = False
        self.models = copy.deepcopy(BUILTIN_MODELS)
        self.status = {"status": "completed", "media": {"url": "https://example.invalid/image", "object_key": "generated-key"}}

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def call(self, tool, args):
        self.calls.append((tool, copy.deepcopy(args)))
        if tool == "list_media_models":
            return {"models": [{"model_id": k, "capabilities": list(v["capabilities"])} for k, v in self.models.items()]}
        if tool == "get_media_model": return self.models[args["model_id"]]
        if tool == "list_my_workspaces": return {"workspaces": [{"workspace_id": "workspace"}]}
        if tool == "create_media_upload": return {"upload_url": "https://example.invalid/upload", "object_key": "input-" + str(len(self.calls))}
        if tool in {"estimate_image_edit", "estimate_image_generation"}:
            return {"can_generate": True, "credits": 636, "pricing_version": "offline-price", "balance": 10000}
        if tool in {"generate_image_edit", "generate_image"}:
            if self.fail_submit: raise FishError("lost submission response")
            return {"generation_id": "generation-" + str(len(self.calls)), "status": "queued", "poll_after_seconds": 20}
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


class AIImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DesktopStore(self.root / "data")
        self.form = AIImageStore(self.store)
        self.client = Fish()
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Real network prohibited"))
        self.network.start()

    def tearDown(self):
        self.network.stop(); self.store.db.close(); self.tmp.cleanup()

    def references(self, count=2):
        refs = []
        for i in range(count):
            path = self.root / f"ref-{i}.png"
            Image.new("RGB", (480, 640), (i * 11, 100, 160)).save(path)
            asset = self.store.import_asset(path)
            refs.append({**inspect_image(asset["path"]), "asset_id": asset["id"], "name": asset["name"], "label": f"@图{i+1}"})
        return refs

    def config(self, refs=None):
        return {"workflow": "ai_image", "prompt": "清晨窗边的一杯咖啡", "model": DEFAULT_MODEL,
                "parameters": copy.deepcopy(DEFAULT_PARAMETERS), "image_inputs": refs or [],
                "workspace_id": "workspace", "output": str(self.root / "result.png")}

    def quote(self, config=None):
        with patch("fission_service.put_media"):
            result = prepare_quote(config or self.config(), self.store, self.client)
        result["cost_approved"] = True
        return result

    def png(self):
        data = io.BytesIO(); Image.new("RGB", (450, 600), "navy").save(data, format="PNG")
        return data.getvalue()

    def run_job(self, config, resume=False):
        return run_job({"config": config}, {}, lambda **_: None, resume,
                       client_factory=lambda *_: self.client, sleep=lambda _: None)

    def test_defaults_and_singleton_persist_without_any_sessions(self):
        self.assertEqual(self.form.get()["parameters"], {"aspect_ratio": "9:16", "resolution": "1K", "quality": "medium"})
        self.form.fields(prompt="保存草稿", model=DEFAULT_MODEL)
        self.form.add_references(self.references())
        other = DesktopStore(self.store.root)
        try:
            self.assertEqual(AIImageStore(other).get(), self.form.get())
            self.assertEqual(other.db.execute("SELECT count(*) FROM image_sessions").fetchone()[0], 0)
            self.assertEqual(other.db.execute("SELECT count(*) FROM image_workspace").fetchone()[0], 1)
        finally: other.db.close()

    def test_text_generation_never_uploads_and_calls_count_one(self):
        config = self.quote()
        self.assertEqual(config["fish_quote"]["arguments"]["prompt"], config["prompt"])
        self.assertEqual(config["fish_quote"]["arguments"]["count"], 1)
        self.assertNotIn("inputs", config["fish_quote"]["arguments"])
        with patch("fission_service.requests.get", return_value=Download(self.png())):
            result = self.run_job(config)
        self.assertTrue(result["local_downloaded"])
        self.assertFalse(any(t in {"create_media_upload", "estimate_image_edit", "generate_image_edit"} for t, _ in self.client.calls))
        calls = [a for t, a in self.client.calls if t == "generate_image"]
        self.assertEqual(len(calls), 1); self.assertEqual(calls[0]["parameters"]["quality"], "medium")
        self.assertEqual(calls[0]["expected_credits"], 636)
        self.assertTrue(calls[0]["idempotency_key"])
        before = len(self.client.calls)
        self.assertTrue(self.run_job(config, True)["resumed_existing_task"])
        self.assertEqual(before, len(self.client.calls))

    def test_eight_references_preserve_order_without_commerce_rules(self):
        refs = self.references(8); config = self.config(refs)
        config["prompt"] = "用 @图8 的配色和 @图1 的构图画风景"
        first = self.quote(config)
        args = first["fish_quote"]["arguments"]
        self.assertEqual(len(args["inputs"]), 8); self.assertNotIn("count", args)
        self.assertTrue(args["prompt"].endswith(config["prompt"]))
        self.assertNotIn("人物身份", args["prompt"])
        self.assertEqual(first["reference_assets"], [r["path"] for r in refs])
        self.assertEqual(args, self.quote(config)["fish_quote"]["arguments"])
        self.assertEqual(sum(t == "create_media_upload" for t, _ in self.client.calls), 8)
        with patch("fission_service.requests.get", return_value=Download(self.png())):
            self.run_job(first)
        self.assertEqual(sum(t == "generate_image_edit" for t, _ in self.client.calls), 1)

    def test_reference_order_and_configuration_changes_invalidate_quote(self):
        config = self.quote(self.config(self.references()))
        changed = copy.deepcopy(config); changed["image_inputs"].reverse()
        with self.assertRaisesRegex(ValueError, "顺序"): validate_approved(changed)
        for key, value in (("prompt", "变了"), ("model", "nano-banana"), ("cost_approved", False)):
            with self.subTest(key=key), self.assertRaises(ValueError): validate_approved({**config, key: value})
        text = self.quote(); text["fish_quote"]["arguments"]["count"] = 2
        with self.assertRaises(ValueError): validate_approved(text)

    def test_model_options_support_only_real_parameters_and_limits(self):
        refs = self.references()
        for model in BUILTIN_MODELS.values():
            for has_refs in (False, True):
                with self.subTest(model=model["model_id"], edit=has_refs):
                    params, _ = normalize_parameters(model, DEFAULT_PARAMETERS, has_refs)
                    config = self.config(refs if has_refs else [])
                    config.update(model=model["model_id"], parameters=params)
                    self.quote(config)
        lite = BUILTIN_MODELS["nano-banana-2-lite"]
        params, changes = normalize_parameters(lite, DEFAULT_PARAMETERS, True)
        self.assertEqual(params, {"aspect_ratio": "9:16"}); self.assertEqual(len(changes), 2)
        self.assertEqual(reference_limit(lite), 14)
        params, _ = normalize_parameters(BUILTIN_MODELS["seedream-5-0"], DEFAULT_PARAMETERS, False)
        self.assertEqual(params, {"aspect_ratio": "9:16", "resolution": "2K"})

    def test_excess_references_and_changed_schema_block_before_upload(self):
        refs = self.references(3)
        self.client.models[DEFAULT_MODEL]["capabilities"]["edit"]["max_inputs"] = 2
        with self.assertRaises(ValueError): self.quote(self.config(refs))
        config = self.config(); config["parameters"]["quality"] = "not-supported"
        with self.assertRaises(ValueError): self.quote(config)
        self.assertFalse(any(t == "create_media_upload" for t, _ in self.client.calls))

    def test_follow_image_uses_auto_or_closest_supported_ratio(self):
        refs = self.references(1)
        self.assertEqual(resolve_parameters({"aspect_ratio": FOLLOW}, BUILTIN_MODELS[DEFAULT_MODEL], refs)["aspect_ratio"], "3:4")
        self.assertEqual(resolve_parameters({"aspect_ratio": FOLLOW}, BUILTIN_MODELS["nano-banana"], refs)["aspect_ratio"], "auto")
        with self.assertRaises(ValueError): resolve_parameters({"aspect_ratio": FOLLOW}, BUILTIN_MODELS[DEFAULT_MODEL], [])

    def test_deleted_reference_numbers_are_rewritten_and_block_until_corrected(self):
        refs = self.references(11); self.form.add_references(refs)
        self.form.fields(prompt="@图1 + @图2 + @图10 + 参考图11", assistant={"request": "换成图2，保留图10构图"})
        form = self.form.remove_reference(1)
        self.assertEqual(form["prompt"], "@图1 + " + REMOVED_REFERENCE + " + @图9 + 参考图10")
        self.assertEqual(form["assistant"]["request"], "换成" + REMOVED_REFERENCE + "，保留图9构图")
        with self.assertRaises(ValueError): validate_references(form["prompt"], 10)
        with self.assertRaises(ValueError): validate_references("@图11", 10)
        validate_references("@图1 @图9 参考图10", 10)
        validate_references("构图3:2，地图3D风格", 0)
        with self.assertRaises(ValueError): validate_references("图2：保留动作", 1)

    def test_optional_helper_sees_images_and_can_fill_and_undo(self):
        refs = self.references(); self.form.add_references(refs); self.form.fields(prompt="原提示词")
        self.form.fields(assistant={"request": "换成图2的产品，保留人物和动作，光线自然"})
        snapshot = helper_snapshot(self.form.get()); seen = []
        def ask_fn(credentials, system, content):
            seen.extend(content); return {"prompt": "保持 @图1 的人物，替换成 @图2 产品。", "note": "已整理"}
        result = generate_prompt(snapshot, {}, ask_fn=ask_fn)
        self.assertEqual(sum(p["type"] == "image_url" for p in seen), 2)
        payload = json.loads(seen[0]["text"])
        self.assertEqual(payload["想要的效果"], "换成图2的产品，保留人物和动作，光线自然")
        self.assertEqual(payload["参考图编号"], ["@图1", "@图2"])
        self.assertNotIn("修改方向", payload)
        self.assertEqual(set(snapshot["assistant"]), {"request"})
        _, applied = self.form.helper_finished(snapshot, result)
        self.assertTrue(applied); self.assertEqual(self.form.get()["prompt"], result["prompt"])
        self.form.undo(); self.assertEqual(self.form.get()["prompt"], "原提示词")

    def test_late_helper_or_manually_edited_prompt_is_never_overwritten(self):
        self.form.add_references(self.references()); snapshot = helper_snapshot(self.form.get())
        self.form.fields(prompt="新要求")
        _, applied = self.form.helper_finished(snapshot, {"prompt": "旧生成稿", "note": ""})
        self.assertFalse(applied); self.assertEqual(self.form.get()["prompt"], "新要求")
        self.assertEqual(self.form.get()["helper_result"]["prompt"], "旧生成稿")
        snapshot = helper_snapshot(self.form.get())
        self.form.helper_finished(snapshot, {"prompt": "新生成稿", "note": ""})
        self.form.fields(prompt="手动调整")
        with self.assertRaises(ValueError): self.form.undo()

    def test_helper_rejects_empty_requirements_or_nonexistent_reference(self):
        form = self.form.get()
        with self.assertRaises(ValueError): generate_prompt(helper_snapshot(form), {})
        self.form.add_references(self.references(1)); form = self.form.get()
        form["assistant"]["request"] = "用图2"
        with self.assertRaises(ValueError): generate_prompt(helper_snapshot(form), {})

    def test_helper_can_write_text_only_prompt_without_mandatory_image_roles(self):
        form = self.form.fields(assistant={"request": "温暖的咖啡馆，清晨阳光"})
        seen = []
        def ask_fn(credentials, system, content):
            seen.extend(content)
            return {"prompt": "清晨阳光洒进温暖的咖啡馆，木质桌面、自然色彩。", "note": "已整理"}
        result = generate_prompt(helper_snapshot(form), {}, ask_fn=ask_fn)
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(seen[0]["text"])["参考图编号"], [])
        self.assertIn("咖啡馆", result["prompt"])

    def test_old_helper_draft_is_migrated_to_visible_requirements_once(self):
        refs = self.references(); self.form.add_references(refs)
        form = self.form.get()
        form["assistant"] = {"changes": "修瓶盖", "preserve": "保留手势", "direction": "局部调整",
                             "roles": {"original": refs[0]["asset_id"], "product": refs[1]["asset_id"]}}
        self.store._put("image_workspace", form)
        migrated = self.form.get()
        self.assertEqual(set(migrated["assistant"]), {"request"})
        request = migrated["assistant"]["request"]
        for text in ("修瓶盖", "保留手势", "局部调整", "@图1作为母图", "@图2作为产品参考"):
            self.assertIn(text, request)
        self.assertEqual(self.form.get(), migrated)

    def test_pending_request_resolves_same_job_after_lost_reply_and_restart(self):
        manager = TaskManager(self.store, execute=lambda *a: {"local_downloaded": False})
        config = self.quote()
        self.form.fields(pending_submission={"request_id": "same-request", "config": config})
        try:
            identity = manager.submit("ai_image", config, {}, request_id="same-request")
            self.assertEqual(manager.submit("ai_image", config, {}, request_id="same-request"), identity)
            self.assertEqual(AIImageStore(self.store).pending_job()["id"], identity)
            manager.wait_idle()
            with self.assertRaisesRegex(ValueError, "核对"): self.store.delete_job(identity)
            self.form.fields(pending_submission=None)
            self.store.delete_job(identity)
        finally: manager.stop(); manager.wait_idle()

    def test_unknown_paid_submission_never_repeats(self):
        config = self.quote(); self.client.fail_submit = True
        with self.assertRaises(FishError): self.run_job(config)
        state = TaskRecord(TaskRecord.path_for(Path(config["output"]))).read().data
        self.assertEqual(state["status"], "submission_unknown")
        self.assertIn("ai_image_config", state)
        with self.assertRaises(FishError): self.run_job(config, True)
        with self.assertRaises(Exception): self.run_job(config)
        self.assertEqual(sum(t == "generate_image" for t, _ in self.client.calls), 1)

    def test_known_job_recovery_polls_then_downloads_without_generating(self):
        config = self.quote(); self.client.fail_poll = True
        with self.assertRaises(FishError): self.run_job(config)
        self.client.fail_poll = False
        with patch("fission_service.requests.get", return_value=Download(b"invalid")):
            self.assertFalse(self.run_job(config, True)["local_downloaded"])
        before = len(self.client.calls)
        with patch("fission_service.requests.get", return_value=Download(self.png())):
            self.assertTrue(self.run_job(config, True)["local_downloaded"])
        self.assertEqual(len(self.client.calls), before)
        self.assertEqual(sum(t == "generate_image" for t, _ in self.client.calls), 1)

    def test_completed_job_import_and_execute_cache_result(self):
        config = self.quote()
        actual = lambda job, credentials, update, resume=False: run_job(job, credentials, update, resume,
                         client_factory=lambda *_: self.client, sleep=lambda _: None)
        with patch("fission_service.run_job", side_effect=actual), patch("fission_service.requests.get", return_value=Download(self.png())):
            result = execute_job({"kind": "ai_image", "config": config}, {}, lambda **_: None, self.store)
        self.assertTrue(Path(result["asset_path"]).is_file())
        identity = self.store.import_record(TaskRecord.path_for(Path(config["output"])))
        imported = self.store.job(identity)
        self.assertEqual(imported["kind"], "ai_image"); self.assertEqual(imported["config"]["prompt"], config["prompt"])
        new = self.config([{**inspect_image(result["asset_path"]), "label": "@图1"}])
        quote = self.quote(new)
        self.assertEqual(quote["fish_quote"]["arguments"]["inputs"][0]["object_key"], "generated-key")
        self.assertFalse(any(t == "create_media_upload" for t, _ in self.client.calls))

    def test_current_form_and_pending_inputs_are_protected_but_archives_are_not_active(self):
        refs = self.references(); self.form.add_references(refs)
        with self.assertRaisesRegex(ValueError, "表单"): self.store.delete_asset(refs[0]["asset_id"])
        self.form.remove_reference(0)
        self.form.fields(pending_submission={"request_id": "unsent", "config": self.config([refs[0]])})
        with self.assertRaisesRegex(ValueError, "表单"): self.store.delete_asset(refs[0]["asset_id"])
        self.form.fields(pending_submission=None)
        self.store._put("image_sessions", {"id": "archived", "references": refs})
        self.store.delete_asset(refs[0]["asset_id"])
        self.assertTrue(self.store.db.execute("SELECT 1 FROM image_sessions").fetchone())


if __name__ == "__main__": unittest.main()
