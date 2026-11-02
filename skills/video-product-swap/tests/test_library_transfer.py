import copy
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from desktop_tasks import DesktopStore
from library_transfer import LibraryTransfer, PreparedBundle, safe_name
from personal_library import RULES, atomic_json, catalog, collect, reindex


class LibraryTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = DesktopStore(self.base / "old-user")
        self.target = DesktopStore(self.base / "new-user")
        self.addCleanup(self.source.db.close); self.addCleanup(self.target.db.close)
        self.outgoing = LibraryTransfer(self.source)
        self.incoming = LibraryTransfer(self.target)
        self.outgoing.recover(); self.incoming.recover()
        self.archive = self.base / "library.zip"

    def asset(self, store, name="原图.png", group=None, content=b"actual file contents"):
        path = self.base / name; path.write_bytes(content)
        return store.import_asset(path, group_id=group)

    def demo(self, root, title="个人动作案例"):
        identity = "demo-method-sheet-mask-01"
        destination = root / "demo-library/entries" / identity
        shutil.copytree(RULES / "demo-library/entries" / identity, destination, dirs_exist_ok=True)
        path = destination / "record.json"
        record = json.loads(path.read_text(encoding="utf-8")); record["title"] = title
        atomic_json(path, record); reindex(root)
        return path

    def prepared(self):
        self.outgoing.export(self.archive)
        prepared = PreparedBundle(self.archive)
        self.addCleanup(prepared.close)
        return prepared

    def test_round_trip_rewrites_paths_preserves_groups_and_never_exports_credentials(self):
        group = self.source.create_group("产品 A")
        source_asset = self.asset(self.source, group=group["id"])
        self.demo(self.outgoing.root)
        (self.source.root / "credentials.dat").write_bytes(b"do-not-export")
        prepared = self.prepared()
        self.assertNotIn(str(self.source.root), json.dumps(prepared.manifest))
        self.assertFalse(any("credentials" in name for name in prepared.manifest["files"]))
        preview = self.incoming.preview(prepared)
        self.assertEqual(preview["assets_added"], 1)
        self.assertEqual(len(self.target.assets()), 0)
        result = self.incoming.import_bundle(prepared)
        asset = self.target.assets()[0]
        self.assertEqual(asset["id"], source_asset["id"])
        self.assertTrue(Path(asset["path"]).is_relative_to(self.target.root))
        self.assertEqual(Path(asset["path"]).read_bytes(), b"actual file contents")
        self.assertEqual(self.target.groups()[0]["name"], "产品 A")
        self.assertEqual(asset["group_id"], self.target.groups()[0]["id"])
        self.assertEqual(asset["size"], source_asset["size"])
        self.assertEqual(asset["created"], source_asset["created"])
        self.assertTrue((Path(result["backup"]) / "history.sqlite3").is_file())
        self.assertEqual(len(collect(self.incoming.root)["demo"]), 1)

    def test_duplicate_retains_name_and_group_and_missing_copy_can_be_repaired(self):
        self.asset(self.source)
        group = self.target.create_group("保留分组")
        previous = self.asset(self.target, "已有名称.png", group["id"])
        prepared = self.prepared()
        self.incoming.import_bundle(prepared)
        self.assertEqual(self.target.assets()[0], previous)
        Path(previous["path"]).unlink()
        self.assertEqual(self.incoming.preview(prepared)["assets_repaired"], 1)
        self.incoming.import_bundle(prepared)
        self.assertEqual(self.target.assets()[0]["name"], "已有名称.png")
        self.assertEqual(self.target.assets()[0]["group_id"], group["id"])
        self.assertTrue(Path(previous["path"]).exists())

    def test_conflicting_skill_preserves_both_and_second_import_is_idempotent(self):
        self.demo(self.outgoing.root, "新经验")
        path = self.demo(self.incoming.root, "原经验")
        original = path.read_bytes()
        prepared = self.prepared()
        preview = self.incoming.preview(prepared)
        self.assertEqual(preview["skills"]["conflicts"], 1)
        self.incoming.import_bundle(prepared)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(collect(self.incoming.root)["demo"]), 2)
        self.incoming.import_bundle(prepared)
        self.assertEqual(len(collect(self.incoming.root)["demo"]), 2)

    def test_personal_case_is_used_in_director_catalog(self):
        self.demo(self.incoming.root, "保留我的修订")
        entries = catalog(self.incoming.root, "use")
        selected = [e for e, _ in entries if e["id"] == "demo-method-sheet-mask-01"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["title"], "保留我的修订")
        self.assertEqual(selected[0]["origin"], "个人")

    def test_missing_attachment_aborts_export_without_overwriting_previous_backup(self):
        path = self.demo(self.outgoing.root)
        (path.parent / "source-notes.md").unlink()
        self.archive.write_bytes(b"previous backup")
        with self.assertRaises(ValueError):
            self.outgoing.export(self.archive)
        self.assertEqual(self.archive.read_bytes(), b"previous backup")

    def test_corrupt_archive_and_path_traversal_are_rejected_before_writes(self):
        self.asset(self.source)
        self.outgoing.export(self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        asset_name = next(n for n in contents if n.startswith("assets/"))
        contents[asset_name] = b"tampered"
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, value in contents.items(): archive.writestr(name, value)
        with self.assertRaises(ValueError): PreparedBundle(self.archive)
        with zipfile.ZipFile(self.archive, "a") as archive: archive.writestr("../outside", b"bad")
        with self.assertRaises(ValueError): PreparedBundle(self.archive)
        self.assertEqual(self.target.assets(), [])
        self.assertFalse((self.base / "outside").exists())

    def test_case_collisions_and_windows_special_paths_are_rejected(self):
        for value in ("../x", "C:/x", "/x", "x\\y", "CON.txt", "a/../x", "a./x", "a//x", "x:stream", "a/NUL"):
            with self.subTest(value=value), self.assertRaises(ValueError): safe_name(value)

    def test_failed_import_rolls_back_database_and_personal_cases(self):
        self.asset(self.source)
        self.demo(self.outgoing.root, "新案例")
        self.demo(self.incoming.root, "原案例")
        prepared = self.prepared()
        original_replace = Path.rename
        def fail_stage(path, target):
            if ".staging-" in path.name: raise OSError("模拟磁盘失败")
            return original_replace(path, target)
        with patch.object(Path, "rename", fail_stage), self.assertRaises(OSError):
            self.incoming.import_bundle(prepared)
        self.assertEqual(self.target.assets(), [])
        self.assertEqual(collect(self.incoming.root)["demo"][0][1]["title"], "原案例")
        self.assertFalse(self.incoming.journal.exists())

    def test_interrupted_import_recovers_committed_wal_and_old_cases(self):
        existing = self.asset(self.target, "保留.png")
        self.demo(self.incoming.root, "原案例")
        backup = self.incoming.backups / ("import-" + "a" * 32)
        backup.mkdir(parents=True)
        self.incoming._database_backup(backup / "history.sqlite3")
        shutil.copytree(self.incoming.root, backup / "skill-before")
        self.asset(self.target, "中断新增.png", content=b"new")
        self.demo(self.incoming.root, "中断修改")
        atomic_json(self.incoming.journal, {"backup": backup.name, "library_root": str(self.incoming.root), "committed": False})
        self.incoming.recover()
        self.assertEqual([a["id"] for a in self.target.assets()], [existing["id"]])
        self.assertEqual(collect(self.incoming.root)["demo"][0][1]["title"], "原案例")

    def test_learning_conflict_keeps_both_states_and_original_switch(self):
        source = {"schema_version": 1, "enabled": True, "records": {"new": {}}, "history": []}
        target = {"schema_version": 1, "enabled": False, "records": {"old": {}}, "history": []}
        relative = Path("learning/seedance25-director/state.json")
        atomic_json(self.outgoing.root / relative, source)
        atomic_json(self.incoming.root / relative, target)
        self.incoming.import_bundle(self.prepared())
        self.assertEqual(json.loads((self.incoming.root / relative).read_text(encoding="utf-8")), target)
        self.assertEqual(len(list((self.incoming.root / "learning/imported").glob("*.json"))), 1)

    def test_legacy_migration_preserves_original_and_is_once_per_source(self):
        legacy = self.base / "old-install/director-skill"
        self.demo(legacy)
        self.incoming.migrate_legacy([legacy])
        self.incoming.migrate_legacy([legacy])
        self.assertEqual(len(collect(self.incoming.root)["demo"]), 1)
        self.assertEqual(len(collect(legacy)["demo"]), 1)

    def test_extra_personal_notes_are_retained_and_invalid_root_is_rejected(self):
        notes = self.incoming.root / "我的整理.md"
        notes.write_text("do not lose", encoding="utf-8")
        self.incoming.import_bundle(self.prepared())
        self.assertEqual(notes.read_text(encoding="utf-8"), "do not lose")
        with self.assertRaises(ValueError): LibraryTransfer(self.target, self.target.root)
        with self.assertRaises(ValueError): LibraryTransfer(self.target, self.target.files / "personal")

    def test_experience_references_follow_imported_conflict_copy(self):
        self.demo(self.outgoing.root, "新演示")
        self.demo(self.incoming.root, "旧演示")
        record = json.loads((RULES / "templates/experience-record.json").read_text(encoding="utf-8"))
        record.update(category="面膜", summary="一次未确认的经验", created_on="2026-11-02", updated_on="2026-11-02",
                      applicability=["面膜"], related_demo_ids=["demo-method-sheet-mask-01"])
        record["context"].update(first_frame_state="手持面膜", problem="需保持连续")
        record["attempts"][0]["date"] = "2026-11-02"
        path = self.outgoing.root / "experience-library/entries/experience-new-case/record.json"
        atomic_json(path, record); reindex(self.outgoing.root)
        prepared = self.prepared()
        self.incoming.import_bundle(prepared)
        experience = collect(self.incoming.root)["experience"][0][1]
        target_id = experience["related_demo_ids"][0]
        self.assertIn("-import-", target_id)
        demo = {d["id"]: d for _, d in collect(self.incoming.root)["demo"]}[target_id]
        self.assertEqual(demo["title"], "新演示")
        preview = self.incoming.preview(prepared)
        self.assertEqual(preview["skills"]["added"], 0)
        self.assertEqual(preview["skills"]["conflicts"], 0)

    def test_update_backup_and_post_start_check_leave_task_identity_unchanged(self):
        from app_updates import backup_for_update, verify_after_update
        from app_version import VERSION
        from contextlib import closing
        import sqlite3
        self.asset(self.target)
        self.demo(self.incoming.root)
        self.target._put("jobs", {"id": "paid-existing", "created": 0, "status": "attention", "task_id": "provider-existing-id"})
        before = self.target.jobs()
        result = backup_for_update(self.incoming, VERSION)
        with closing(sqlite3.connect(Path(result["backup"]) / "history.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
        self.assertIn("已检查", verify_after_update(self.incoming))
        self.assertEqual(self.target.jobs(), before)


if __name__ == "__main__": unittest.main()
