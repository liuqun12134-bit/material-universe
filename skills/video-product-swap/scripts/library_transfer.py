"""Portable library archives and recoverable, non-destructive imports.

Archives contain data only. Credentials, job submissions and executable Skill
code are never exported/imported. The SQLite backup API includes committed WAL.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from app_version import VERSION
from model_runner.core import EXTENSIONS
from personal_library import (RULES, atomic_json, collect, default_root, digest,
                              entry_files, library_lock, merge, reindex)

FORMAT = "material-universe-libraries"
MAX_JSON = 32 * 1024 * 1024
MAX_FILES = 200000
MAX_BYTES = 1024 ** 4  # 1 TiB; actual imports also require enough free disk.


def safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("迁移包包含无效路径。")
    parts = name.split("/")
    if any(p in {"", ".", ".."} or p[-1:] in {" ", "."} or any(ord(c) < 32 or c in '<>"|?*' for c in p)
           or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", p, re.I) for p in parts):
        raise ValueError("迁移包包含不安全路径。")
    return name


def json_file(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError("迁移包中的索引过大。")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def skill_files(root):
    root = Path(root)
    files = {}
    for rows in collect(root).values():
        for path, record in rows:
            for _, attachment in entry_files(path, record).items():
                relative = attachment.relative_to(root).as_posix()
                if attachment.suffix.lower() in {".py", ".pyw", ".exe", ".dll", ".ps1", ".bat", ".cmd", ".lnk", ".key", ".pem"} or attachment.name.startswith(".env"):
                    raise ValueError(f"库附件不能包含程序或凭据：{relative}")
                files[relative] = attachment
    for path in (root / "learning").rglob("*.json"):
        relative = path.relative_to(root).as_posix()
        safe_name(relative)
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("学习记录位置无效。")
        files[relative] = path
    from assistant_store import portable_files
    files.update(portable_files(root))
    return files


def copy_skill(source, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for relative, path in skill_files(source).items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    reindex(destination)


def copy_personal_directory(source, destination):
    """Internal snapshots retain extra personal files, not just exported attachments."""
    source, destination = Path(source), Path(destination)
    for path in source.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("个人库包含指向外部的链接，请先整理后再迁移。")
    shutil.copytree(source, destination, dirs_exist_ok=True)


class PreparedBundle:
    """Validate once into an isolated folder; confirmation imports these exact bytes."""
    def __init__(self, archive, temporary_parent=None):
        self.temporary = tempfile.TemporaryDirectory(prefix="mu-import-", dir=temporary_parent)
        self.root = Path(self.temporary.name)
        try:
            self._read(Path(archive))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.temporary.cleanup()

    def _read(self, path):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or any(i.is_dir() for i in entries):
                raise ValueError("迁移包目录结构无效。")
            names = [safe_name(i.filename) for i in entries]
            if len({n.casefold() for n in names}) != len(names):
                raise ValueError("迁移包含有重名文件。")
            if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > MAX_JSON:
                raise ValueError("不是有效的素材万象迁移包。")
            self.manifest = manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != FORMAT or manifest.get("format_version") != 1:
                raise ValueError("不支持此迁移包版本，请更新软件后重试。")
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != set(names) - {"manifest.json"}:
                raise ValueError("迁移包文件清单不完整。")
            total = sum(i.file_size for i in entries)
            if total > MAX_BYTES or total + 128 * 1024 ** 2 > shutil.disk_usage(self.root).free:
                raise ValueError("空间不足，无法完整检查迁移包。")
            for info in entries:
                if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise ValueError("不接受链接或加密的迁移包文件。")
                if info.filename == "manifest.json":
                    continue
                expected = files[info.filename]
                if not isinstance(expected, dict) or expected.get("size") != info.file_size or not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("sha256", ""))):
                    raise ValueError("迁移包校验信息无效。")
                target = self.root / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                checksum = hashlib.sha256()
                with archive.open(info) as src, target.open("xb") as dst:
                    while chunk := src.read(1024 * 1024):
                        checksum.update(chunk); dst.write(chunk)
                if checksum.hexdigest() != expected["sha256"]:
                    raise ValueError(f"迁移包文件损坏：{info.filename}")
        self._validate()

    def _validate(self):
        manifest = self.manifest
        if not isinstance(manifest.get("assets"), list) or not isinstance(manifest.get("groups"), list) or type(manifest.get("skill_included")) is not bool:
            raise ValueError("迁移包内容格式无效。")
        groups, assets, allowed = set(), set(), set()
        from desktop_tasks import DesktopStore
        group_names = set()
        for group in manifest["groups"]:
            if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"] or group["id"] in groups:
                raise ValueError("迁移包分组无效。")
            _, key = DesktopStore._group_name(group.get("name"))
            if key in group_names:
                raise ValueError("迁移包分组重名。")
            group_names.add(key); groups.add(group["id"])
        for asset in manifest["assets"]:
            if not isinstance(asset, dict):
                raise ValueError("迁移包素材无效。")
            identity, relative = asset.get("id"), asset.get("file")
            if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity) or identity in assets:
                raise ValueError("迁移包素材编号无效。")
            safe_name(relative)
            suffix = PurePosixPath(relative).suffix.lower()
            if (relative != f"assets/{identity}{suffix}" or suffix not in EXTENSIONS.get(asset.get("kind"), set())
                    or relative not in manifest["files"] or manifest["files"][relative]["sha256"] != identity
                    or not isinstance(asset.get("name"), str) or not asset["name"].strip()
                    or len(asset["name"]) > 1000 or (asset.get("group_id") is not None and asset["group_id"] not in groups)):
                raise ValueError("迁移包素材引用或内容校验不符。")
            assets.add(identity); allowed.add(relative)
        if manifest["skill_included"]:
            expected = skill_files(self.root / "skill")
            allowed.update("skill/" + name for name in expected)
            # Validate state JSON, collision handling and derived indexes without touching user data.
            with tempfile.TemporaryDirectory(dir=self.root) as folder:
                merge(self.root / "skill", Path(folder))
        if set(manifest["files"]) != allowed:
            raise ValueError("迁移包包含未声明的库文件。")


class LibraryTransfer:
    def __init__(self, store, personal_root=None):
        self.store = store
        self.root = Path(personal_root) if personal_root is not None else default_root(store.root)
        self.root = self.root.resolve()
        self.backups = store.root / "backups"
        self.journal = store.root / "library-import.pending.json"
        from model_runner.runner import SKILL_ROOT
        if (store.root.is_relative_to(self.root) or self.root.is_relative_to(store.files)
                or self.root.is_relative_to(self.backups) or self.root.is_relative_to(SKILL_ROOT.resolve())
                or (getattr(sys, "frozen", False) and self.root.is_relative_to(Path(sys.executable).resolve().parent))):
            raise ValueError("个人库必须使用独立目录，不能放在程序、素材文件或备份目录内。")

    def recover(self):
        with self.store.lock, library_lock(self.root):
            if self.journal.exists():
                note = json_file(self.journal)
                name = note.get("backup", "")
                if not re.fullmatch(r"import-[a-f0-9]{32}", name) or note.get("library_root") != str(self.root):
                    raise ValueError("未完成迁移的恢复位置不符，请保留备份并检查个人库路径。")
                backup = self.backups / name
                if note.get("committed") is not True:
                    self._restore(backup)
                self.journal.unlink()
            self.root.mkdir(parents=True, exist_ok=True)
            reindex(self.root)

    def _database_backup(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as destination:
            self.store.db.backup(destination)

    def _restore(self, backup):
        self.store.db.rollback()
        if (backup / "history.sqlite3").is_file():
            with closing(sqlite3.connect(backup / "history.sqlite3")) as source:
                source.backup(self.store.db)
        if (backup / "skill-before").is_dir():
            if self.root.exists():
                self.root.rename(self.root.with_name(self.root.name + ".incomplete-" + uuid.uuid4().hex))
            (backup / "skill-before").rename(self.root)

    def export(self, target, assets=True, skills=True, skill_source=None):
        if not assets and not skills:
            raise ValueError("请至少选择一种库。")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        with self.store.lock, library_lock(self.root):
            source = Path(skill_source) if skill_source is not None else self.root
            manifest = {"format": FORMAT, "format_version": 1, "app_version": VERSION,
                        "created": time.time(), "assets": [], "groups": self.store.groups() if assets else [],
                        "skill_included": skills, "files": {}}
            files = {}
            if assets:
                for item in self.store.assets():
                    path = Path(item["path"])
                    if not path.is_file():
                        raise ValueError(f"素材缺失，未生成迁移包：{item['name']}")
                    relative = f"assets/{item['id']}{path.suffix.lower()}"
                    manifest["assets"].append({k: item[k] for k in ("id", "name", "kind", "created", "group_id") if k in item} | {"file": relative})
                    files[relative] = path
            if skills:
                files.update({"skill/" + name: path for name, path in skill_files(source).items()})
            try:
                with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
                    for relative, path in files.items():
                        safe_name(relative)
                        checksum, size = hashlib.sha256(), 0
                        with path.open("rb") as src, archive.open(relative, "w", force_zip64=True) as dst:
                            while chunk := src.read(1024 * 1024):
                                checksum.update(chunk); size += len(chunk); dst.write(chunk)
                        manifest["files"][relative] = {"sha256": checksum.hexdigest(), "size": size}
                        if relative.startswith("assets/"):
                            expected = next(a["id"] for a in manifest["assets"] if a["file"] == relative)
                            if checksum.hexdigest() != expected:
                                raise ValueError("素材内容已变化，未导出不一致的库。")
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, allow_nan=False))
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return {"assets": len(manifest["assets"]), "files": len(files), "path": str(target)}

    def preview(self, prepared):
        with self.store.lock, library_lock(self.root):
            existing = {a["id"]: a for a in self.store.assets()}
            assets = prepared.manifest["assets"]
            with tempfile.TemporaryDirectory(prefix="mu-preview-") as folder:
                copy_skill(self.root, Path(folder))
                stats = merge(prepared.root / "skill", Path(folder)) if prepared.manifest["skill_included"] else {"added": 0, "duplicates": 0, "conflicts": 0, "learning_added": 0}
            return {"assets_added": sum(a["id"] not in existing for a in assets),
                    "assets_duplicate": sum(a["id"] in existing for a in assets),
                    "assets_repaired": sum(a["id"] in existing and not Path(existing[a["id"]]["path"]).is_file() for a in assets),
                    "skills": stats}

    def import_bundle(self, prepared):
        self.backups.mkdir(parents=True, exist_ok=True)
        with self.store.lock, library_lock(self.root):
            if self.journal.exists():
                raise ValueError("上次迁移尚未恢复，请重启软件后重试。")
            # Check staged bytes again after the preview and before touching the library.
            for relative, expected in prepared.manifest["files"].items():
                if digest(prepared.root / relative) != expected["sha256"]:
                    raise ValueError("检查后的迁移文件已改变，请重新选择迁移包。")
            backup = self.backups / ("import-" + uuid.uuid4().hex)
            backup.mkdir()
            self._database_backup(backup / "history.sqlite3")
            # Stage beside the library, allowing custom library and database drives to differ.
            stage = self.root.with_name(self.root.name + ".staging-" + uuid.uuid4().hex)
            copy_personal_directory(self.root, stage)
            stats = merge(prepared.root / "skill", stage) if prepared.manifest["skill_included"] else {}
            copy_personal_directory(self.root, backup / "skill-before")
            atomic_json(self.journal, {"backup": backup.name, "library_root": str(self.root), "committed": False})
            try:
                existing = {a["id"]: a for a in self.store.assets()}
                groups = {g["name"].casefold(): g for g in self.store.groups()}
                group_map = {}
                added, repaired = 0, 0
                with self.store.db:
                    for group in prepared.manifest["groups"]:
                        name, key = self.store._group_name(group["name"])
                        if key not in groups:
                            identity = uuid.uuid4().hex
                            self.store.db.execute("INSERT INTO asset_groups VALUES (?, ?, ?, ?)", (identity, name, key, time.time()))
                            groups[key] = {"id": identity}
                        group_map[group["id"]] = groups[key]["id"]
                    for item in prepared.manifest["assets"]:
                        old = existing.get(item["id"])
                        if old and Path(old["path"]).is_file():
                            if digest(old["path"]) != item["id"]:
                                raise ValueError(f"已有素材内容异常，请先检查：{old['name']}")
                            continue
                        source = prepared.root / item["file"]
                        target = self.store.files / source.name
                        if target.exists() and digest(target) != item["id"]:
                            raise ValueError("素材目标文件内容冲突，已保留原文件。")
                        if not target.exists():
                            temp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
                            try:
                                shutil.copy2(source, temp); os.replace(temp, target)
                            finally:
                                temp.unlink(missing_ok=True)
                        created = item.get("created")
                        if not isinstance(created, (int, float)) or not math.isfinite(created):
                            created = time.time()
                        value = copy.deepcopy(old) if old else {"id": item["id"], "name": item["name"], "kind": item["kind"], "created": created}
                        value["path"] = str(target.resolve())
                        value["size"] = target.stat().st_size
                        if not old and item.get("group_id"):
                            value["group_id"] = group_map[item["group_id"]]
                        self.store.db.execute("INSERT OR REPLACE INTO assets VALUES (?, ?)", (value["id"], json.dumps(value, ensure_ascii=False)))
                        added += old is None; repaired += old is not None
                    # No original records are destroyed: the old directory is kept in the backup.
                    previous = self.root.with_name(self.root.name + ".previous-" + uuid.uuid4().hex)
                    if self.root.exists():
                        self.root.rename(previous)
                    stage.rename(self.root)
                atomic_json(self.journal, {"backup": backup.name, "library_root": str(self.root), "committed": True})
                self.journal.unlink()
                # The verified full snapshot is already in backup/skill-before.
                if (previous.exists() and previous.resolve().parent == self.root.parent
                        and previous.name.startswith(self.root.name + ".previous-") and not previous.is_symlink()):
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        pass  # Retain an extra snapshot if another reader still has a file open.
                return {"assets_added": added, "assets_repaired": repaired, "skills": stats, "backup": str(backup)}
            except BaseException:
                self._restore(backup)
                self.journal.unlink(missing_ok=True)
                raise

    def import_skill_directory(self, source, include_learning=False):
        """Copy an existing Skill's records; its rules/code/credentials remain untouched."""
        source = Path(source).resolve()
        if source == self.root:
            raise ValueError("这已经是当前个人库。")
        with tempfile.TemporaryDirectory(prefix="mu-skill-import-") as folder:
            folder = Path(folder)
            staged = folder / "skill"
            copy_skill(source, staged)
            if include_learning:
                config_path = source / "learning-config.json"
                config = json_file(config_path) if config_path.exists() else {}
                learning = Path(os.environ.get("SKILL_LEARNING_HOME") or Path.home() / ".local/share/skill-learning")
                for scope in config.get("scopes", []):
                    if not isinstance(scope, str) or not re.fullmatch(r"[a-z0-9-]+", scope):
                        raise ValueError("Skill 学习范围无效。")
                    path = learning / scope / "state.json"
                    if path.is_file():
                        atomic_json(staged / "learning" / scope / "state.json", json_file(path))
            archive = folder / "skill.mu-library.zip"
            self.export(archive, assets=False, skills=True, skill_source=staged)
            return PreparedBundle(archive)

    def migrate_legacy(self, sources):
        """One-time additive migration before the task manager starts."""
        marker = self.store.root / "personal-library-migrations.json"
        done = json_file(marker) if marker.exists() else {"sources": []}
        for source in sources:
            source = Path(source).resolve()
            key = str(source)
            if key in done["sources"] or source == self.root or not source.is_dir():
                continue
            count = sum(len(v) for v in collect(source).values())
            if not count:
                continue
            prepared = self.import_skill_directory(source)
            try:
                self.import_bundle(prepared)
            finally:
                prepared.close()
            done["sources"].append(key)
            atomic_json(marker, done)
