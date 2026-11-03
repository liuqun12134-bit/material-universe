"""Persistent image conversations and immutable generation branches."""
from __future__ import annotations

import copy
import json
import time
import uuid

ROLES = {"original": "母图", "person": "人物参考", "product": "产品参考", "clothing": "服装参考", "scene": "场景参考"}
MODES = {"refine": "继续微调", "restart": "从原图重做"}


class FissionStore:
    def __init__(self, store):
        self.store = store

    def sessions(self):
        with self.store.lock:
            return sorted((json.loads(row[0]) for row in self.store.db.execute("SELECT body FROM image_sessions")),
                          key=lambda s: s["updated"], reverse=True)

    def get(self, identity):
        with self.store.lock:
            row = self.store.db.execute("SELECT body FROM image_sessions WHERE id=?", (identity,)).fetchone()
            if not row:
                raise ValueError("图片会话不存在。")
            return json.loads(row[0])

    def create(self, title="新的图片创作"):
        session = {"id": uuid.uuid4().hex, "title": title, "created": time.time(), "updated": time.time(),
                   "revision": 0, "references": [], "turns": [], "versions": [], "selected": None,
                   "cursor": None, "mode": "refine", "locks": "", "prompt": "", "input": "",
                   "parameters": {"aspect_ratio": "跟随底图", "resolution": "1K", "quality": "high"}}
        self.store._put("image_sessions", session)
        return session

    def change(self, identity, mutate, expected=None):
        with self.store.lock:
            session = self.get(identity)
            if expected is not None and session["revision"] != expected:
                raise ValueError("会话已改变，旧结果已停止应用，请根据当前素材重新操作。")
            mutate(session)
            session.update(revision=session["revision"] + 1, updated=time.time())
            self.store._put("image_sessions", session)
            return session

    def fields(self, identity, **fields):
        allowed = {"title", "locks", "prompt", "input", "mode", "parameters"}
        if not set(fields) <= allowed:
            raise ValueError("不支持的会话字段。")
        if "mode" in fields and fields["mode"] not in MODES:
            raise ValueError("请选择改图方式。")
        return self.change(identity, lambda s: s.update(copy.deepcopy(fields)))

    def reference(self, identity, role, reference):
        if role not in ROLES:
            raise ValueError("参考角色无效。")
        def mutate(s):
            s["references"] = [r for r in s["references"] if r["role"] != role]
            if reference:
                s["references"].append({**copy.deepcopy(reference), "role": role, "label": ROLES[role]})
            s["references"].sort(key=lambda r: list(ROLES).index(r["role"]))
            # A prompt's image numbers belong to its exact input list.
            s["prompt"] = ""
        return self.change(identity, mutate)

    def select(self, identity, version_id):
        def mutate(s):
            version = next((v for v in s["versions"] if v["id"] == version_id), None)
            if version_id and not version:
                raise ValueError("图片版本不存在。")
            s.update(selected=version_id, cursor=version.get("turn_id") if version else None,
                     prompt="", mode="refine")
            if version:
                s["references"] = copy.deepcopy(version.get("references", [r for r in version["config"]["image_inputs"] if r["role"] != "base"]))
                s["locks"] = version["locks"]
                s["parameters"] = copy.deepcopy(version.get("parameters", version["config"]["parameters"]))
        return self.change(identity, mutate)

    def begin_turn(self, identity, text):
        if not text.strip() or len(text) > 12000:
            raise ValueError("请输入 1–12000 字符的修改意见。")
        def mutate(s):
            turn = {"id": uuid.uuid4().hex, "parent": s["cursor"], "base_version": s["selected"],
                    "user": text.strip(), "reply": "", "status": "pending", "created": time.time(),
                    "references": copy.deepcopy(s["references"]), "locks": s["locks"], "mode": s["mode"]}
            s["turns"].append(turn)
            s.update(cursor=turn["id"], input="", prompt="")
        return self.change(identity, mutate)

    def finish_turn(self, identity, turn_id, result=None, error=None, expected=None):
        # Always retain responses, even if the user selected another branch meanwhile.
        def mutate(s):
            turn = next(t for t in s["turns"] if t["id"] == turn_id)
            turn.update(status="failed" if error else "complete", error=error,
                        reply=result.get("reply", "") if result else "",
                        summary=result.get("summary", "") if result else "",
                        prompt=result.get("prompt", "") if result else "")
            if not error and s["cursor"] == turn_id and (expected is None or s["revision"] == expected):
                s["prompt"] = result.get("prompt", "")
        return self.change(identity, mutate)

    def reserve(self, identity, config, expected):
        def mutate(s):
            version = {"id": uuid.uuid4().hex, "request_id": uuid.uuid4().hex,
                       "number": len(s["versions"]) + 1, "created": time.time(),
                       "parent": s["selected"] if s["mode"] == "refine" else None,
                       "context_version": s["selected"], "turn_id": s["cursor"],
                       "mode": s["mode"], "locks": s["locks"],
                       "references": copy.deepcopy(s["references"]), "parameters": copy.deepcopy(s["parameters"]),
                       "config": copy.deepcopy(config), "job_id": None}
            s["versions"].append(version)
        return self.change(identity, mutate, expected)

    def attach_job(self, identity, version_id, job_id):
        def mutate(s):
            next(v for v in s["versions"] if v["id"] == version_id)["job_id"] = job_id
        return self.change(identity, mutate)

    def job_for(self, version):
        identity = version.get("job_id")
        if not identity:
            with self.store.lock:
                row = self.store.db.execute("SELECT job_id FROM submission_requests WHERE request_id=?",
                                            (version["request_id"],)).fetchone()
                identity = row[0] if row else None
        if identity:
            try:
                return self.store.job(identity)
            except ValueError:
                pass
        return None


def branch_turns(session):
    by_id = {t["id"]: t for t in session["turns"]}
    cursor, seen, result = session.get("cursor"), set(), []
    while cursor and cursor not in seen:
        seen.add(cursor)
        turn = by_id.get(cursor)
        if not turn:
            break
        result.append(turn)
        cursor = turn.get("parent")
    return list(reversed(result))


def referenced_assets(session):
    """Protect original inputs, including superseded references still used by a branch."""
    refs = list(session.get("references", []))
    for turn in session.get("turns", []):
        refs.extend(turn.get("references", []))
    for version in session.get("versions", []):
        refs.extend(version.get("config", {}).get("image_inputs", []))
    return {r["asset_id"] for r in refs if r.get("asset_id")}
