import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from desktop_tasks import DesktopStore, TaskManager


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.01)
    raise AssertionError("timed out")


class DesktopTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DesktopStore(self.root / "library")
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
            wait_for(lambda: not manager.active)
            manager.owner.close()
        self.store.db.close()
        self.temp.cleanup()

    def manager(self, execute, limit=3):
        manager = TaskManager(self.store, execute=execute, limit=limit)
        self.managers.append(manager)
        return manager

    def config(self):
        return {"source": str(self.root / "source.mp4"), "output": str(self.root / "output.mp4"),
                "prompt": "保持原提示词", "options": {"value": 1}}

    def test_ten_shared_slots_queue_isolation_and_failure(self):
        release = threading.Event()
        seen, lock = [], threading.Lock()
        def execute(job, credentials, update, store, resume):
            with lock: seen.append((job, credentials))
            release.wait(4)
            if job["config"].get("test_failure"): raise RuntimeError("provider failed secret-1234")
            return {"local_downloaded": True, "output": job["config"]["output"]}
        manager = self.manager(execute, 10)
        config = self.config()
        credentials = {"FAL_KEY": "secret-1234"}
        ids = [manager.submit(("general", "image_audio")[i % 2], {**config, "test_failure": i % 3 == 2}, credentials) for i in range(13)]
        config["options"]["value"] = 99; credentials["FAL_KEY"] = "changed"
        wait_for(lambda: len(seen) == 10)
        self.assertEqual(len(manager.active), 10)
        self.assertEqual(sum(j["status"] == "queued" for j in self.store.jobs()), 3)
        self.assertEqual(len({j["config"]["output"] for j in self.store.jobs()}), 13)
        release.set(); wait_for(lambda: not manager.active and not manager.pending)
        self.assertEqual(len(seen), 13)
        self.assertTrue(all(j["config"]["options"]["value"] == 1 and c["FAL_KEY"] == "secret-1234" for j,c in seen))
        self.assertEqual(sum(j["status"] == "failed" for j in self.store.jobs()), 4)
        self.assertNotIn("secret-1234", json.dumps(self.store.jobs()))
        # A completed job must not prevent the next submission.
        manager.submit("general", self.config(), {})
        wait_for(lambda: not manager.active)
        self.assertEqual(len(self.store.jobs()), 14)

    def test_restart_does_not_submit_and_resume_flag_is_read_only(self):
        identity = "existing"
        output = self.root / "original.mp4"
        record_path = Path(str(output) + ".video-task.json")
        record_path.write_text(json.dumps({"task_id": "paid-task", "status": "submitted"}), encoding="utf-8")
        self.store._put("jobs", {"id": identity, "kind": "full", "name": "old", "created": time.time(),
                                "status": "running", "config": {"output": str(output)}})
        calls = []
        def execute(job, credentials, update, store, resume):
            calls.append(resume)
            return {"local_downloaded": True}
        manager = self.manager(execute)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.job(identity)["status"], "attention")
        manager.recover(identity, {})
        wait_for(lambda: not manager.active)
        self.assertEqual(calls, [True])

    def test_cancel_queued_without_executing(self):
        release = threading.Event()
        calls = []
        def execute(job, *args):
            calls.append(job["id"]); release.wait(4); return {"local_downloaded": True}
        manager = self.manager(execute, 1)
        first = manager.submit("general", self.config(), {})
        second = manager.submit("image_audio", self.config(), {})
        manager.cancel(second); release.set(); wait_for(lambda: not manager.active)
        self.assertEqual(calls, [first])
        self.assertEqual(self.store.job(second)["status"], "cancelled")

    def test_assets_survive_original_deletion_deduplicate_and_protect_references(self):
        original = self.root / "original.mp4"; original.write_bytes(b"test media")
        asset = self.store.import_asset(original)
        duplicate = self.root / "renamed.mp4"; duplicate.write_bytes(original.read_bytes())
        self.assertEqual(asset["id"], self.store.import_asset(duplicate)["id"])
        original.unlink()
        self.assertEqual(Path(asset["path"]).read_bytes(), b"test media")
        self.store._put("jobs", {"id": "owner", "created": time.time(), "status": "success", "asset_ids": [asset["id"]]})
        with self.assertRaises(ValueError): self.store.delete_asset(asset["id"])
        self.store.delete_job("owner")
        self.assertTrue(Path(asset["path"]).exists())
        self.store.delete_asset(asset["id"])
        self.assertTrue(duplicate.exists())

    def test_asset_groups_move_rename_delete_and_validate_batch_atomically(self):
        group = self.store.create_group("玫瑰护发精油")
        with self.assertRaisesRegex(ValueError, "同名"):
            self.store.create_group(" 玫瑰护发精油 ")
        with self.assertRaisesRegex(ValueError, "系统筛选"):
            self.store.create_group("未分组")
        first = self.root / "first.mp4"; first.write_bytes(b"first")
        second = self.root / "second.png"; second.write_bytes(b"second")
        a = self.store.import_asset(first)
        b = self.store.import_asset(second, group_id=group["id"])
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.store.move_assets([a["id"], "missing"], group["id"])
        self.assertNotIn("group_id", next(item for item in self.store.assets() if item["id"] == a["id"]))
        self.assertEqual(self.store.move_assets([a["id"], b["id"]], group["id"]), 2)
        self.store.rename_group(group["id"], "玫瑰精油")
        self.assertEqual(self.store.groups()[0]["name"], "玫瑰精油")
        self.assertEqual({item.get("group_id") for item in self.store.assets()}, {group["id"]})
        self.store.delete_group(group["id"])
        self.assertEqual(self.store.groups(), [])
        self.assertTrue(all("group_id" not in item and Path(item["path"]).is_file() for item in self.store.assets()))

    def test_duplicate_import_keeps_group_and_missing_copy_restores_old_metadata(self):
        group_a = self.store.create_group("产品A")
        group_b = self.store.create_group("产品B")
        source = self.root / "source.mp4"; source.write_bytes(b"same content")
        old = self.store.import_asset(source, group_id=group_a["id"])
        self.store.rename_asset(old["id"], "原名称")
        old = next(item for item in self.store.assets() if item["id"] == old["id"])
        duplicate = self.root / "new.mov"; duplicate.write_bytes(source.read_bytes())
        self.assertEqual(self.store.import_asset(duplicate, group_id=group_b["id"]), old)
        Path(old["path"]).unlink()
        self.assertEqual(self.store.import_asset(duplicate, group_id=group_b["id"]), old)
        self.assertTrue(Path(old["path"]).is_file())
        ungrouped = self.root / "ungrouped.mp4"; ungrouped.write_bytes(b"ungrouped")
        plain = self.store.import_asset(ungrouped)
        Path(plain["path"]).unlink()
        self.assertNotIn("group_id", self.store.import_asset(ungrouped, group_id=group_b["id"]))
        self.store.delete_group(group_b["id"])
        fresh = self.root / "fresh.mp4"; fresh.write_bytes(b"fresh")
        with self.assertRaisesRegex(ValueError, "已删除"):
            self.store.import_asset(fresh, group_id=group_b["id"])
        self.assertEqual(len(self.store.assets()), 2)

    def test_first_group_upgrade_backs_up_committed_wal_snapshot(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        connection = sqlite3.connect(legacy / "history.sqlite3")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE assets (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        body = json.dumps({"id": "old", "name": "旧素材", "created": 1})
        connection.execute("INSERT INTO assets VALUES (?, ?)", ("old", body))
        connection.commit()
        upgraded = DesktopStore(legacy)
        backups = list(legacy.glob("history.before-asset-groups-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        snapshot = sqlite3.connect(backups[0])
        try:
            self.assertEqual(snapshot.execute("SELECT body FROM assets WHERE id='old'").fetchone()[0], body)
            self.assertFalse(snapshot.execute("SELECT 1 FROM sqlite_master WHERE name='asset_groups'").fetchone())
        finally:
            snapshot.close()
        self.assertEqual(upgraded.groups(), [])
        upgraded.db.close(); connection.close()

    def test_analysis_failure_keeps_job_and_assets(self):
        video = self.root / "video.mp4"; video.write_bytes(b"video")
        image = self.root / "image.png"; image.write_bytes(b"image")
        video = Path(self.store.import_asset(video)["path"])
        image = Path(self.store.import_asset(image)["path"])
        from desktop_tasks import execute_job
        manager = self.manager(lambda *args: (_ for _ in ()).throw(RuntimeError("位置识别缺项")))
        config = {"source": str(video), "image": str(image), "output": str(self.root / "out.mp4"),
                  "relation": "same", "model": "wan3", "duration": 5, "aspect_ratio": "9:16",
                  "resolution": "480p", "vision_model": "test", "prompt_mode": "analyze"}
        with patch("product_swap.ProductSwapWorkflow.generate", side_effect=RuntimeError("位置识别缺项")):
            identity = manager.submit("general", config, {})
            wait_for(lambda: not manager.active)
        job = self.store.job(identity)
        self.assertEqual(job["status"], "failed")
        self.assertIn("位置识别", job["error"])
        self.assertEqual(len(job["asset_ids"]), 2)

    def test_second_manager_is_rejected(self):
        self.manager(lambda *args: {})
        with self.assertRaises(ValueError): TaskManager(self.store)

    def test_deleted_record_stays_deleted_on_auto_import(self):
        path = self.root / "old.video-task.json"
        path.write_text(json.dumps({"version": 1, "model": {"adapter": "fal_wan3"},
                                   "output": str(self.root / "old.mp4"), "status": "submitted"}), encoding="utf-8")
        identity = self.store.import_record(path)
        self.store.delete_job(identity)
        self.assertIsNone(self.store.import_record(path))
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(self.store.import_record(path, explicit=True), identity)

    def test_cancel_does_not_overwrite_completed_status(self):
        manager = self.manager(lambda *args: {"local_downloaded": True})
        identity = manager.submit("general", self.config(), {})
        wait_for(lambda: not manager.active)
        with self.assertRaises(ValueError): manager.cancel(identity)
        self.assertEqual(self.store.job(identity)["status"], "success")
