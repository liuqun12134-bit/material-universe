"""User-owned director records; executable rules always come from the application."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath

from model_runner.runner import SKILL_ROOT

KINDS = ("demo", "experience")
RULES = SKILL_ROOT / ("director-rules" if (SKILL_ROOT / "director-rules").is_dir() else "director-skill")


def default_root(data_root):
    override = os.environ.get("MATERIAL_UNIVERSE_LIBRARY_ROOT", "").strip()
    return Path(override).expanduser().resolve() if override else Path(data_root).resolve() / "Skill附属库"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path, value):
    import uuid
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def library_lock(root):
    """An OS lock releases on a crash, unlike a persistent exclusive-create file."""
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    path = root.parent / ("." + root.name + ".lock")
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("个人库正在由另一个操作使用，请稍后重试。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@lru_cache(maxsize=1)
def schema():
    # Never load code from an imported library or migration archive.
    spec = importlib.util.spec_from_file_location("bundled_director_schema", RULES / "scripts" / "library.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relative_file(base, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("库附件路径无效。")
    parts = PurePosixPath(relative).parts
    if relative.startswith("/") or not parts or any(p in {".", ".."} for p in parts):
        raise ValueError("库附件不能越出条目目录。")
    path = Path(base).joinpath(*parts)
    if not path.resolve().is_relative_to(Path(base).resolve()) or path.is_symlink() or not path.is_file():
        raise ValueError(f"库附件缺失或位置无效：{relative}")
    return path


def entry_files(path, record):
    names = {"record.json"}
    if record["record_type"] == "demo":
        names.update(record["provenance"].get(k) for k in ("source_asset", "notes_path"))
        names.update(f["path"] for f in record.get("keyframes", []))
    else:
        names.update(record.get("attachments", []))
        names.update(a.get("prompt_path") for a in record.get("attempts", []))
    return {name: relative_file(path.parent, name) for name in sorted(n for n in names if n)}


def collect(root):
    root = Path(root)
    records = schema().collect(root)
    for rows in records.values():
        for path, record in rows:
            entry_files(path, record)
    return records


def reindex(root):
    root = Path(root)
    records = collect(root)
    for kind, rows in records.items():
        atomic_json(root / f"{kind}-library" / "index.json", schema().index_data(root, kind, rows))
    return records


def signature(path, record):
    value = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode())
    for name, attachment in entry_files(path, record).items():
        if name != "record.json":
            value.update(name.encode()); value.update(digest(attachment).encode())
    return value.hexdigest()


def merge(source, destination):
    """Merge into a staged directory. Keep both revisions on an ID conflict."""
    source, destination = Path(source), Path(destination)
    incoming = collect(source)
    existing = {d["id"]: (p, d) for rows in collect(destination).values() for p, d in rows}
    remap, stats = {}, {"added": 0, "duplicates": 0, "conflicts": 0, "learning_added": 0}
    for kind in KINDS:  # demos first, so experiences can retain their references
        for path, original in incoming[kind]:
            record = copy.deepcopy(original)
            if kind == "experience":
                record["related_demo_ids"] = [remap.get(i, i) for i in record["related_demo_ids"]]
            original_id = record["id"]
            identity = original_id
            previous = existing.get(identity)
            if previous and signature(*previous) != signature(path, record):
                stats["conflicts"] += 1
                identity = original_id + "-import-" + signature(path, record)[:12]
            remap[original_id] = identity
            record["id"] = identity
            previous = existing.get(identity)
            if previous:
                if signature(*previous) != signature(path, record):
                    raise ValueError(f"导入副本也有冲突，请先处理条目：{identity}")
                stats["duplicates"] += 1
                continue
            target = destination / f"{kind}-library" / "entries" / identity
            target.mkdir(parents=True)
            for name, attachment in entry_files(path, original).items():
                if name == "record.json":
                    continue
                dest = target / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(attachment, dest)
            atomic_json(target / "record.json", record)
            existing[identity] = (target / "record.json", record)
            stats["added"] += 1
    for path in sorted((source / "learning").rglob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(state, dict) or state.get("schema_version") != 1 or type(state.get("enabled")) is not bool
                or not isinstance(state.get("records"), dict) or not isinstance(state.get("history"), list)):
            raise ValueError("个人学习记录格式无效。")
        relative = path.relative_to(source)
        target = destination / relative
        if target.exists() and json.loads(target.read_text(encoding="utf-8-sig")) != state:
            target = destination / "learning" / "imported" / (path.parent.name + "-" + digest(path)[:12] + ".json")
        if not target.exists():
            atomic_json(target, state)
            stats["learning_added"] += 1
    reindex(destination)
    return stats


def catalog(personal_root=None, mode=None):
    """Personal revisions override bundled entries with the same ID."""
    merged = {}
    roots = [(RULES, "内置")]
    if personal_root is not None and Path(personal_root).is_dir():
        roots.append((Path(personal_root), "个人"))
    for root, origin in roots:
        for kind, records in collect(root).items():
            index = schema().index_data(root, kind, records)
            by_id = {record["id"]: path for path, record in records}
            for entry in index["entries"]:
                merged[entry["id"]] = ({**entry, "library": f"{kind}-library", "origin": origin}, by_id[entry["id"]])
    return [value for value in merged.values() if mode is None or mode in value[0]["modes"]]
