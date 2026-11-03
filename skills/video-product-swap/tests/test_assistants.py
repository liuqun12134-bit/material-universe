"""Approval boundaries, learning evidence, prompt integration and portable data."""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from assistant_store import AssistantStore, merge_knowledge, prompt_knowledge
from assistant_learning import add_feedback, add_material, analyze_source, sample_video
from ai_image_assistant import generate_prompt
from desktop_tasks import DesktopStore
from library_transfer import LibraryTransfer, PreparedBundle


def content(title="亮面瓶身融合", **changes):
    return {"title": title, "category": "护肤品", "content": "依据当前场景匹配反光方向与强弱。",
            "applicability": "真实场景中的亮面瓶体换品", "exceptions": "明确要求重新布光时另行处理",
            "evidence": "用户指出本次瓶身反光过强", "limitations": "尚未验证新的提示词是否改善结果",
            "modes": ["talk", "use"], **changes}


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AssistantStore(self.root / "personal")
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Real network prohibited"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.picture = self.root / "reference.png"
        Image.new("RGB", (480, 640), "#A59A78").save(self.picture)
        self.credentials = {"KAIYUNCODE_API_KEY": "offline-test"}

    def source(self, role="artist"):
        return add_material(self.store, role, [self.picture], "学习反光与接触阴影", "护肤品")[0]

    def approve(self, role="artist", **kwargs):
        entry = self.store.propose(role, "rule", content(**kwargs))
        return self.store.decide(entry["id"], entry["revision"], "approve")

    def job(self, **extra):
        return {"id": "offline-job", "kind": "ai_image", "name": "换品测试", "status": "success",
                "config": {"model": "test-image-model", "prompt": "保留光线，替换产品", "api_key": "must-not-copy",
                           "parameters": {"resolution": "1K", "secret": "must-not-copy"},
                           "image_inputs": [{"path": str(self.picture)}]},
                "result": {"asset_path": str(self.picture), "video_url": "https://example.invalid/private?token=secret"}, **extra}

    def test_read_has_no_side_effect_and_roles_default_off(self):
        self.assertFalse(self.store.enabled("artist")); self.assertFalse(self.store.enabled("director"))
        self.assertFalse(self.store.root.exists())

    def test_unapproved_and_ignored_content_never_enters_prompts(self):
        entry = self.store.propose("artist", "rule", content())
        self.assertEqual(self.store.approved("artist"), [])
        self.store.decide(entry["id"], entry["revision"], "ignore")
        self.assertEqual(self.store.approved("artist"), [])

    def test_confirmation_scope_and_stop_learning_do_not_disable_existing_rules(self):
        self.approve(); self.approve("director", modes=["use"])
        self.store.enable("artist", True); self.store.enable("artist", False)
        self.assertEqual(len(self.store.approved("artist")), 1)
        self.assertEqual(self.store.approved("director", mode="talk"), [])
        self.assertEqual(len(self.store.approved("director", mode="use")), 1)
        self.assertNotIn("user_confirmed_effective", self.store.path.read_text(encoding="utf8"))

    def test_draft_edit_keeps_old_active_content_until_confirmation(self):
        entry = self.approve()
        changed = self.store.propose("artist", "rule", content(content="使用修订后的要求"), identity=entry["id"], revision=entry["revision"])
        self.assertIn("反光", self.store.approved("artist")[0]["content"])
        self.store.decide(changed["id"], changed["revision"], "approve")
        self.assertEqual(self.store.approved("artist")[0]["content"], "使用修订后的要求")

    def test_stale_editor_cannot_overwrite_new_revision(self):
        entry = self.approve()
        self.store.propose("artist", "rule", content(title="新版"), identity=entry["id"], revision=entry["revision"])
        with self.assertRaisesRegex(ValueError, "已被修改"):
            self.store.decide(entry["id"], entry["revision"], "approve")
        self.assertEqual(self.store.read()["entries"][entry["id"]]["draft"]["title"], "新版")

    def test_disable_and_restore_keep_version_history(self):
        entry = self.approve()
        changed = self.store.propose("artist", "rule", content(title="第二版"), identity=entry["id"], revision=entry["revision"])
        changed = self.store.decide(changed["id"], changed["revision"], "approve")
        event = self.store.read()["history"][-1]
        disabled = self.store.decide(changed["id"], changed["revision"], "disable")
        self.assertEqual(self.store.approved("artist"), [])
        restored = self.store.restore(event["id"])
        self.assertEqual(restored["draft"]["title"], "亮面瓶身融合")
        self.assertEqual(self.store.approved("artist"), [])
        self.store.decide(restored["id"], restored["revision"], "approve")
        self.assertEqual(self.store.approved("artist")[0]["title"], "亮面瓶身融合")
        self.assertGreater(len(self.store.read()["history"]), disabled["revision"])

    def test_feedback_is_explicit_durable_and_excludes_credentials_paths_and_urls(self):
        with self.assertRaises(ValueError): add_feedback(self.store, self.job(), "  ")
        self.assertFalse(self.store.path.exists())
        source, added = add_feedback(self.store, self.job(), "瓶身反光太强，手指接触合理")
        self.assertTrue(added); self.assertEqual(len(source["media"]), 2)
        text = self.store.path.read_text(encoding="utf8")
        for secret in ("must-not-copy", "token=secret", str(self.picture).replace("\\", "\\\\")):
            self.assertNotIn(secret, text)
        second, added = add_feedback(self.store, self.job(), "瓶身反光太强，手指接触合理")
        self.assertFalse(added); self.assertEqual(second["id"], source["id"])
        self.picture.unlink()
        self.assertEqual(len(self.store.read()["sources"]), 1)

    def test_feedback_rejects_replaced_reference_and_result_but_keeps_text(self):
        job = self.job()
        expected = hashlib.sha256(self.picture.read_bytes()).hexdigest()
        job["config"]["image_inputs"][0]["sha256"] = expected
        job["result"]["asset_id"] = expected
        Image.new("RGB", (480, 640), "red").save(self.picture)
        source, _ = add_feedback(self.store, job, "原来的瓶子反光过强")
        self.assertEqual(source["media"], [])
        self.assertEqual(len(source["context"]["limitations"]), 2)
        self.assertTrue(all("不一致" in item for item in source["context"]["limitations"]))
        self.assertEqual(source["request"], "原来的瓶子反光过强")

    def test_feedback_keeps_valid_evidence_but_skips_corrupt_result(self):
        job = self.job()
        job["config"]["image_inputs"][0]["sha256"] = hashlib.sha256(self.picture.read_bytes()).hexdigest()
        broken = self.root / "broken.png"; broken.write_bytes(b"invalid image")
        job["result"]["asset_path"] = str(broken)
        source, _ = add_feedback(self.store, job, "产品文字变形")
        self.assertEqual([m["label"] for m in source["media"]], ["参考图 1"])
        self.assertIn("无法完整读取", source["context"]["limitations"][0])

    def test_feedback_skips_files_changed_while_preparing_evidence(self):
        import assistant_learning
        original = assistant_learning._picture
        def changing_picture(path, target):
            original(path, target)
            Image.new("RGB", (640, 480), "blue").save(path)
        job = self.job(); job["result"] = {}
        with patch("assistant_learning._picture", side_effect=changing_picture):
            source, _ = add_feedback(self.store, job, "请记住原图产品的反光")
        self.assertEqual(source["media"], [])
        self.assertIn("读取期间发生变化", source["context"]["limitations"][0])

    def test_director_feedback_checks_frozen_reference_hashes(self):
        job = self.job(kind="image_audio")
        job["config"].update(first_frame=str(self.picture), media_hashes={"first_frame": "0" * 64})
        job["result"] = {}
        source, _ = add_feedback(self.store, job, "这次握瓶姿势不合理")
        self.assertEqual(source["media"], [])
        self.assertIn("首帧与本次任务记录不一致", source["context"]["limitations"][0])

    def test_auto_off_never_calls_model_and_manual_learning_still_works(self):
        source = self.source(); model = Mock(return_value={"note": "候选", "suggestions": [{"kind": "case", **content()}]})
        analyze_source(self.store, source["id"], {}, automatic=True, ask_fn=model)
        model.assert_not_called()
        result = analyze_source(self.store, source["id"], self.credentials, ask_fn=model)
        self.assertEqual(result["count"], 1); self.assertEqual(self.store.approved("artist"), [])
        self.assertEqual(next(iter(self.store.read()["entries"].values()))["status"], "pending")

    def test_auto_on_still_needs_confirmation_and_repeated_analysis_is_not_paid_again(self):
        self.store.enable("artist", True)
        source = self.source(); model = Mock(return_value={"note": "待验证", "suggestions": [{"kind": "case", **content()}]})
        analyze_source(self.store, source["id"], self.credentials, automatic=True, ask_fn=model)
        self.assertEqual(self.store.approved("artist"), [])
        analyze_source(self.store, source["id"], self.credentials, automatic=True, ask_fn=model)
        model.assert_called_once()

    def test_switching_off_during_request_preserves_feedback_without_new_suggestions(self):
        self.store.enable("artist", True); source = self.source()
        def respond(*_):
            self.store.enable("artist", False)
            return {"note": "候选", "suggestions": [{"kind": "rule", **content()}]}
        result = analyze_source(self.store, source["id"], self.credentials, automatic=True, ask_fn=respond)
        self.assertEqual(result["count"], 0); self.assertFalse(self.store.read()["entries"])
        self.assertIn(source["id"], self.store.read()["sources"])

    def test_malformed_suggestions_are_rejected_as_a_batch(self):
        source = self.source()
        model = Mock(return_value={"note": "bad", "suggestions": [{"kind": "case", **content()}, {"kind": "rule"}]})
        with self.assertRaises(ValueError): analyze_source(self.store, source["id"], self.credentials, ask_fn=model)
        self.assertFalse(self.store.read()["entries"])
        self.assertEqual(self.store.read()["sources"][source["id"]]["analysis"], "error")

    def test_stale_batch_does_not_partially_save_earlier_suggestion(self):
        entry = self.approve(); source = self.source()
        def respond(*_):
            self.store.propose("artist", "rule", content(title="用户刚改的版本"), identity=entry["id"], revision=entry["revision"])
            return {"note": "候选", "suggestions": [{"kind": "case", **content(title="新建议")},
                    {"kind": "rule", "entry_id": entry["id"], **content(title="迟到建议")}]}
        with self.assertRaisesRegex(ValueError, "已被修改"):
            analyze_source(self.store, source["id"], self.credentials, ask_fn=respond)
        self.assertEqual(len(self.store.read()["entries"]), 1)

    def test_empty_review_creates_no_experience(self):
        source = self.source()
        result = analyze_source(self.store, source["id"], self.credentials, ask_fn=lambda *_: {"note": "没有具体的新反馈", "suggestions": []})
        self.assertEqual(result["count"], 0); self.assertFalse(self.store.read()["entries"])

    def test_service_error_keeps_source_and_does_not_retry(self):
        source = self.source(); model = Mock(side_effect=ValueError("offline failure"))
        with self.assertRaisesRegex(ValueError, "offline failure"):
            analyze_source(self.store, source["id"], self.credentials, ask_fn=model)
        model.assert_called_once(); self.assertIn(source["id"], self.store.read()["sources"])

    def test_image_prompt_receives_only_approved_enabled_artist_rules(self):
        pending = self.store.propose("artist", "rule", content(title="待确认的内容"))
        self.approve(title="可以使用的内容"); self.approve("director", title="另一个助手的内容")
        snapshot = {"references": [], "prompt": "", "assistant": {"request": "生成亮面瓶体图片"}}
        model = Mock(return_value={"prompt": "一张静物图片", "note": ""})
        generate_prompt(snapshot, {}, ask_fn=model, personal_root=self.store.library_root)
        payload = json.loads(model.call_args.args[2][0]["text"])
        self.assertEqual([v["title"] for v in payload["已确认个人内容"]], ["可以使用的内容"])
        self.assertNotIn(pending["id"], str(payload["已确认个人内容"]))

    def test_evidence_path_traversal_and_corrupt_state_fail_without_clearing_data(self):
        source = self.source(); state = self.store.read()
        state["sources"][source["id"]]["media"][0]["path"] = "../reference.png"
        self.store.path.write_text(json.dumps(state), encoding="utf8")
        original = self.store.path.read_bytes()
        with self.assertRaises(ValueError): self.store.read()
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_director_prompt_receives_approved_mode_specific_knowledge(self):
        import director_prompt as director
        self.approve("director", title="只用于使用导演", modes=["use"])
        self.approve("director", title="只用于纯口播", modes=["talk"])
        self.approve("artist", title="美工规则")
        self.store.propose("director", "demo", content(title="尚未确认的演示"))
        media = {field: {"path": str(self.picture), "sha256": "same", "duration": 12} for field in director.MEDIA_FIELDS}
        snapshot = {"mode": "边讲边用", "requirements": "演示乳霜", "prompt": "", "media": media}
        responses = [{"observations": "首帧与产品参考一致", "visual_plan": "展示手持产品", "issues": [], "selected_ids": []},
                     {"issues": [], "prompt": "使用 @图1 构图、@图2 人物、@图3 产品，保留 @音频1。"}]
        with patch.object(director, "inspect_image", side_effect=lambda path, field: media[field]), \
             patch.object(director, "inspect_audio", return_value=media["audio"]), \
             patch.object(director, "digest_file", return_value="same"), \
             patch.object(director, "transcribe", return_value="这款乳霜"), \
             patch.object(director, "ask", side_effect=responses) as model:
            director.generate(snapshot, self.credentials, None, self.root, lambda _: None, personal_root=self.store.library_root)
        payload = json.loads(model.call_args_list[0].args[2][0]["text"])
        self.assertEqual([r["title"] for r in payload["approved_assistant_knowledge"]], ["只用于使用导演"])
        self.assertIn("只用于使用导演", model.call_args_list[1].args[2][0]["text"])

    def test_round_trip_migration_keeps_pending_active_evidence_and_history(self):
        desktop = DesktopStore(self.root / "desktop"); self.addCleanup(desktop.db.close)
        source = self.source(); self.store.enable("artist", True)
        self.approve(); self.store.propose("director", "demo", content(title="待确认演示"))
        transfer = LibraryTransfer(desktop, self.store.library_root)
        archive = self.root / "backup.zip"; transfer.export(archive, assets=False, skills=True)
        destination = LibraryTransfer(desktop, self.root / "restored")
        destination.recover()
        with tempfile.TemporaryDirectory(dir=self.root) as scratch:
            prepared = PreparedBundle(archive, scratch)
            try: destination.import_bundle(prepared)
            finally: prepared.close()
        restored = AssistantStore(destination.root)
        self.assertEqual(restored.read(), self.store.read())
        self.assertTrue(restored.enabled("artist"))
        self.assertEqual(len(restored.approved("artist")), 1)
        self.assertEqual(restored.approved("director"), [])
        self.assertTrue((restored.root / source["media"][0]["path"]).is_file())

    def test_conflicting_import_keeps_local_rule_and_imports_other_revision_pending(self):
        entry = self.approve()
        other = AssistantStore(self.root / "other")
        merge_knowledge(self.store.library_root, other.library_root)
        candidate = other.propose("artist", "rule", content(title="另一个版本"), identity=entry["id"], revision=entry["revision"])
        other.decide(candidate["id"], candidate["revision"], "approve")
        merge_knowledge(other.library_root, self.store.library_root)
        self.assertEqual([v["title"] for v in self.store.approved("artist")], ["亮面瓶身融合"])
        self.assertEqual(len(self.store.read()["entries"]), 2)
        merge_knowledge(other.library_root, self.store.library_root)
        self.assertEqual(len(self.store.read()["entries"]), 2)

    def test_ignored_conflicting_import_stays_ignored_and_is_idempotent(self):
        entry = self.store.propose("artist", "rule", content())
        other = AssistantStore(self.root / "other")
        merge_knowledge(self.store.library_root, other.library_root)
        other.decide(entry["id"], entry["revision"], "ignore")
        self.store.decide(entry["id"], entry["revision"], "approve")
        merge_knowledge(other.library_root, self.store.library_root)
        merge_knowledge(other.library_root, self.store.library_root)
        entries = list(self.store.read()["entries"].values())
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(e["status"] == "ignored" for e in entries), 1)
        self.assertEqual(len(self.store.approved("artist")), 1)

    def test_malformed_import_history_is_rejected_before_changing_destination(self):
        self.approve()
        other = AssistantStore(self.root / "other")
        merge_knowledge(self.store.library_root, other.library_root)
        original = self.store.path.read_bytes()
        state = other.read(); state["history"][0]["after"]["role"] = "director"
        other.path.write_text(json.dumps(state), encoding="utf8")
        with self.assertRaises(ValueError): merge_knowledge(other.library_root, self.store.library_root)
        self.assertEqual(self.store.path.read_bytes(), original)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "media tools required")
    def test_video_learning_samples_real_frames_and_does_not_claim_audio_analysis(self):
        video = self.root / "test.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                        "-t", "3", "-pix_fmt", "yuv420p", str(video)], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info, frames = sample_video(video, self.root / "frames")
        self.assertAlmostEqual(info["duration"], 3, delta=.2)
        self.assertEqual(frames[0]["seconds"], 0)
        self.assertGreaterEqual(frames[-1]["seconds"], 2.5)
        self.assertIn("未分析音频", info["observation"])
        source, _ = add_material(self.store, "director", [video], "学习取用过程", "乳霜")
        self.assertEqual(source["kind"], "video")
        self.assertTrue(all((self.store.root / m["path"]).is_file() for m in source["media"]))


if __name__ == "__main__": unittest.main()
