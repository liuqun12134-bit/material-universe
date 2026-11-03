"""User-owned assistant knowledge. Drafts never participate in prompt generation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path

from personal_library import atomic_json, library_lock, relative_file

ROLES = {"artist": "AI 美工", "director": "AI 导演"}
KINDS = {"rule": "工作规则", "case": "任务经验", "demo": "产品演示"}
CONTENT_FIELDS = ("title", "category", "content", "applicability", "exceptions", "evidence", "limitations", "modes")
FOLDER = "assistant-knowledge"


def empty_state():
    return {"schema_version": 1, "settings": {r: {"enabled": False} for r in ROLES},
            "sources": {}, "entries": {}, "history": []}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clean_content(value):
    if not isinstance(value, dict): raise ValueError("学习内容格式无效。")
    result = {}
    for key in CONTENT_FIELDS[:-1]:
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > (6000 if key == "content" else 1600):
            raise ValueError("学习内容过长或格式不正确。")
        result[key] = item.strip()
    if not all(result[k] for k in ("title", "content", "applicability")):
        raise ValueError("请填写名称、具体做法和适用条件。")
    modes = value.get("modes", ["talk", "use"])
    if not isinstance(modes, list) or not modes or any(not isinstance(m, str) or m not in {"talk", "use"} for m in modes):
        raise ValueError("请选择有效的导演适用模式。")
    result["modes"] = sorted(set(modes))
    return result


def validate_state(state, folder):
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or set(state.get("settings", {})) != set(ROLES)
            or any(not isinstance(v, dict) or type(v.get("enabled")) is not bool for v in state["settings"].values())
            or not isinstance(state.get("sources"), dict) or not isinstance(state.get("entries"), dict)
            or not isinstance(state.get("history"), list)):
        raise ValueError("助手资料格式无效，原文件已保留。")
    def identity_valid(value):
        return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value)
    def time_valid(value):
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    for identity, source in state["sources"].items():
        if (not identity_valid(identity) or not isinstance(source, dict) or source.get("id") != identity
                or source.get("role") not in ROLES or not isinstance(source.get("media"), list)
                or len(source["media"]) > 120 or source.get("kind") not in {"image", "video", "feedback"}
                or any(not isinstance(source.get(k), str) for k in ("title", "request", "category", "fingerprint"))
                or not time_valid(source.get("created")) or source.get("analysis") not in {"new", "working", "done", "error"}
                or not isinstance(source.get("context"), dict)):
            raise ValueError("学习资料索引无效。")
        for media in source["media"]:
            if not isinstance(media, dict) or not isinstance(media.get("label"), str): raise ValueError("学习证据格式无效。")
            path = relative_file(folder, media.get("path"))
            if path.suffix.lower() not in {".jpg", ".png"}:
                raise ValueError("学习资料只接受图片证据。")
    def check_entry(identity, entry):
        if (not identity_valid(identity) or not isinstance(entry, dict) or entry.get("id") != identity
                or entry.get("role") not in ROLES or entry.get("kind") not in KINDS
                or type(entry.get("revision")) is not int or entry["revision"] < 1 or type(entry.get("enabled")) is not bool
                or not all(time_valid(entry.get(k)) for k in ("created", "updated"))
                or entry.get("status") not in {"pending", "active", "disabled", "ignored"}):
            raise ValueError("助手规则索引无效。")
        if entry.get("source_id") and (entry["source_id"] not in state["sources"]
                                      or state["sources"][entry["source_id"]]["role"] != entry["role"]):
            raise ValueError("助手规则缺少来源资料。")
        for key in ("draft", "active"):
            if entry.get(key) is not None: clean_content(entry[key])
        if entry["enabled"] and entry.get("active") is None: raise ValueError("未确认内容不能启用。")
        if entry["status"] == "pending" and entry.get("draft") is None: raise ValueError("待确认内容缺失。")
    for identity, entry in state["entries"].items(): check_entry(identity, entry)
    event_ids = set()
    for event in state["history"]:
        if (not isinstance(event, dict) or event.get("role") not in ROLES or not identity_valid(event.get("id"))
                or event["id"] in event_ids or not time_valid(event.get("time"))
                or not isinstance(event.get("action"), str)):
            raise ValueError("助手修改历史无效。")
        event_ids.add(event["id"])
        if event.get("entry_id"):
            current = state["entries"].get(event["entry_id"])
            if not current or current["role"] != event["role"]: raise ValueError("助手修改历史与规则不匹配。")
            for key in ("before", "after"):
                if event.get(key) is not None:
                    check_entry(event["entry_id"], event[key])
                    if event[key]["role"] != event["role"]: raise ValueError("助手历史类型不匹配。")
    return state


class AssistantStore:
    def __init__(self, library_root):
        self.library_root = Path(library_root)
        self.root = self.library_root / FOLDER
        self.path = self.root / "state.json"

    def read(self):
        if not self.path.exists(): return empty_state()
        if self.path.stat().st_size > 32 * 1024 * 1024: raise ValueError("助手资料过大，请先整理历史资料。")
        try: state = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError): raise ValueError("助手资料无法读取，原文件已保留，请从备份恢复。") from None
        try: return validate_state(state, self.root)
        except (KeyError, TypeError, OverflowError):
            raise ValueError("助手资料格式无效，原文件已保留。") from None

    def _change(self, mutate):
        with library_lock(self.library_root):
            state = self.read()
            result = mutate(state)
            validate_state(state, self.root)
            atomic_json(self.path, state)
            return copy.deepcopy(result)

    def enabled(self, role):
        return self.read()["settings"][role]["enabled"]

    def enable(self, role, enabled):
        if role not in ROLES or type(enabled) is not bool: raise ValueError("助手设置无效。")
        def mutate(state):
            state["settings"][role]["enabled"] = enabled
            state["history"].append({"id": uuid.uuid4().hex, "role": role, "action": "开启自进化" if enabled else "关闭自进化", "time": time.time()})
        self._change(mutate)

    def add_source(self, role, source, media):
        """Copy only prepared evidence images. No caller paths or credentials enter JSON."""
        if role not in ROLES: raise ValueError("助手类型无效。")
        source = copy.deepcopy(source)
        key = fingerprint({"role": role, "kind": source["kind"], "context": source.get("context", {}),
                           "request": source["request"], "category": source.get("category", ""),
                           "digest": source.get("digest", "")})
        with library_lock(self.library_root):
            state = self.read()
            previous = next((s for s in state["sources"].values() if s.get("fingerprint") == key), None)
            if previous: return copy.deepcopy(previous), False
            identity = uuid.uuid4().hex
            directory = self.root / "assets" / identity
            directory.mkdir(parents=True)
            saved = []
            try:
                for i, item in enumerate(media):
                    original = Path(item["path"])
                    if original.suffix.lower() not in {".jpg", ".png"}: raise ValueError("资料图片格式无效。")
                    relative = f"assets/{identity}/{i:03d}{original.suffix.lower()}"
                    shutil.copy2(original, self.root / relative)
                    saved.append({k: item[k] for k in ("label", "seconds") if k in item} | {"path": relative})
                source.update(id=identity, role=role, fingerprint=key, media=saved, created=time.time(), analysis="new", error="")
                state["sources"][identity] = source
                atomic_json(self.path, validate_state(state, self.root))
            except BaseException:
                # Only this newly allocated evidence directory is removed.
                if directory.resolve().is_relative_to(self.root.resolve()): shutil.rmtree(directory)
                raise
            return copy.deepcopy(source), True

    def source_status(self, identity, status, error=""):
        def mutate(state):
            state["sources"][identity].update(analysis=status, error=str(error)[:600])
        self._change(mutate)

    @staticmethod
    def _event(state, entry, action, before):
        state["history"].append({"id": uuid.uuid4().hex, "entry_id": entry["id"], "role": entry["role"],
            "time": time.time(), "action": action, "before": before, "after": copy.deepcopy(entry)})

    @staticmethod
    def _entry(state, identity, revision):
        entry = state["entries"][identity]
        if entry["revision"] != revision: raise ValueError("这条内容已被修改，请刷新后重新检查。")
        return entry

    def _propose(self, state, role, kind, content, source_id=None, identity=None, revision=None):
        content = clean_content(content)
        if role not in ROLES or kind not in KINDS: raise ValueError("建议类型无效。")
        if source_id and (source_id not in state["sources"] or state["sources"][source_id]["role"] != role):
            raise ValueError("建议与来源不匹配。")
        if identity:
            entry = self._entry(state, identity, revision)
            if entry["role"] != role: raise ValueError("助手类型不匹配。")
            before = copy.deepcopy(entry)
            # Retain the latest source while the full previous evidence stays in history.
            if source_id: entry["source_id"] = source_id
        else:
            for existing in state["entries"].values():
                if existing["role"] == role and existing.get("source_id") == source_id and (existing.get("draft") == content or existing.get("active") == content):
                    return existing
            entry = {"id": uuid.uuid4().hex, "role": role, "kind": kind, "source_id": source_id,
                     "active": None, "enabled": False, "revision": 0, "created": time.time()}
            before = None
        entry.update(draft=content, status="pending", revision=entry["revision"] + 1, updated=time.time())
        state["entries"][entry["id"]] = entry
        self._event(state, entry, "提出建议" if before is None else "修改为待确认", before)
        return entry

    def propose(self, role, kind, content, source_id=None, identity=None, revision=None):
        return self._change(lambda state: self._propose(state, role, kind, content, source_id, identity, revision))

    def finish_analysis(self, source_id, items, revisions, automatic=False):
        def mutate(state):
            source = state["sources"][source_id]; role = source["role"]
            if automatic and not state["settings"][role]["enabled"]:
                source["analysis"] = "new"
                return False
            for kind, content, identity in items:
                self._propose(state, role, kind, content, source_id, identity, revisions.get(identity))
            source.update(analysis="done", error="")
            return True
        return self._change(mutate)

    def decide(self, identity, revision, action):
        if action not in {"approve", "ignore", "disable", "enable"}: raise ValueError("未知操作。")
        def mutate(state):
            entry = self._entry(state, identity, revision)
            before = copy.deepcopy(entry)
            if action == "approve":
                if entry.get("draft") is None: raise ValueError("这条内容没有待确认修改。")
                entry.update(active=clean_content(entry["draft"]), draft=None, enabled=True, status="active")
            elif action == "ignore":
                entry["draft"] = None
                entry["status"] = "active" if entry["enabled"] else "disabled" if entry["active"] else "ignored"
            else:
                if not entry.get("active"): raise ValueError("请先确认这条内容。")
                entry["enabled"] = action == "enable"
                entry["status"] = "pending" if entry.get("draft") else "active" if entry["enabled"] else "disabled"
            entry.update(revision=entry["revision"] + 1, updated=time.time())
            self._event(state, entry, {"approve": "确认并启用", "ignore": "忽略建议", "disable": "停用", "enable": "重新启用"}[action], before)
            return entry
        return self._change(mutate)

    def restore(self, event_id):
        def mutate(state):
            event = next(e for e in state["history"] if e["id"] == event_id)
            previous = event.get("before") or {}
            content = previous.get("active") if event["action"] == "确认并启用" else previous.get("draft") or previous.get("active")
            if not content: raise ValueError("这个变更没有可恢复的旧内容。")
            entry = state["entries"][event["entry_id"]]
            before = copy.deepcopy(entry)
            entry.update(draft=clean_content(content), status="pending", revision=entry["revision"] + 1, updated=time.time())
            self._event(state, entry, "恢复旧版本为待确认", before)
            return entry
        return self._change(mutate)

    def approved(self, role, query="", mode=None):
        entries = [e for e in self.read()["entries"].values() if e["role"] == role and e.get("enabled") and e.get("active")
                   and (mode is None or mode in e["active"]["modes"])]
        # Bound the prompt; the model must still check each entry's applicability.
        words = set(re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2}", query.lower()))
        def rank(entry):
            text = json.dumps(entry["active"], ensure_ascii=False).lower()
            return (sum(word in text for word in words), entry.get("updated", 0))
        rules = sorted((e for e in entries if e["kind"] == "rule"), key=rank, reverse=True)[:8]
        cases = sorted((e for e in entries if e["kind"] != "rule"), key=rank, reverse=True)[:4]
        return [{"id": e["id"], "kind": e["kind"], **copy.deepcopy(e["active"])} for e in rules + cases]


def prompt_knowledge(root, role, query="", mode=None):
    return AssistantStore(root).approved(role, query, mode) if root else []


def portable_files(root):
    store = AssistantStore(root)
    if not store.path.exists(): return {}
    state = store.read()
    files = {f"{FOLDER}/state.json": store.path}
    for source in state["sources"].values():
        for media in source["media"]:
            files[f"{FOLDER}/{media['path']}"] = relative_file(store.root, media["path"])
    return files


def merge_knowledge(source_root, destination_root):
    """Merge staged data, preserving local settings and conflicting revisions."""
    source, destination = AssistantStore(source_root), AssistantStore(destination_root)
    if not source.path.exists(): return 0
    incoming, state = source.read(), destination.read()
    if not destination.path.exists(): state["settings"] = copy.deepcopy(incoming["settings"])
    sources, entries, added = {}, {}, 0
    for identity, original in incoming["sources"].items():
        duplicate = next((s for s in state["sources"].values() if s["fingerprint"] == original["fingerprint"]), None)
        if duplicate:
            sources[identity] = duplicate["id"]; continue
        target_id = identity if identity not in state["sources"] else uuid.uuid4().hex
        sources[identity] = target_id
        item = copy.deepcopy(original); item["id"] = target_id
        for media in item["media"]:
            old = relative_file(source.root, media["path"])
            media["path"] = f"assets/{target_id}/{old.name}"
            target = destination.root / media["path"]; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, target)
        state["sources"][target_id] = item; added += 1
    for identity, original in incoming["entries"].items():
        item = copy.deepcopy(original)
        if item.get("source_id"): item["source_id"] = sources[item["source_id"]]
        previous = state["entries"].get(identity)
        if previous == item:
            entries[identity] = identity; continue
        if previous:
            # A deterministic ID makes repeated imports idempotent.
            imported_id = fingerprint(item)[:32]
            item["id"] = imported_id
            if imported_id in state["entries"]:
                entries[identity] = imported_id; continue
            candidate = item.get("draft") or item.get("active")
            item.update(draft=candidate, active=None, enabled=False, status="pending" if candidate else "ignored")
        entries[identity] = item["id"]
        state["entries"][item["id"]] = item; added += 1
    known_events = {e["id"] for e in state["history"]}
    for event in incoming["history"]:
        if event["id"] in known_events: continue
        event = copy.deepcopy(event)
        if event.get("entry_id"):
            event["entry_id"] = entries[event["entry_id"]]
            for key in ("before", "after"):
                if event.get(key):
                    event[key]["id"] = event["entry_id"]
                    if event[key].get("source_id"): event[key]["source_id"] = sources[event[key]["source_id"]]
        state["history"].append(event)
    atomic_json(destination.path, validate_state(state, destination.root))
    return added
