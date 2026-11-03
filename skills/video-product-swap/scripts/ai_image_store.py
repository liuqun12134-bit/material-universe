"""One durable image form; generation history belongs to the existing job store."""
import copy
import hashlib
import json
import re
import time

from ai_image_models import DEFAULT_MODEL, DEFAULT_PARAMETERS

REFERENCE_TOKEN = re.compile(r"(@图|参考图|图)(\d+)(?!\d|[A-Za-z]|[:：]\d)")
REMOVED_REFERENCE = "【参考图已移除】"
LEGACY_ROLE_LABELS = {"original": "母图", "person": "人物参考", "product": "产品参考", "clothing": "服装参考", "scene": "场景参考"}


def assistant_request(form):
    """Turn an older unfinished helper form into visible, editable requirements."""
    options = form.get("assistant", {})
    if "request" in options: return options["request"]
    parts = [options.get("changes", "").strip()]
    if options.get("preserve", "").strip(): parts.append("需要保留：" + options["preserve"].strip())
    if not any(parts): return ""  # An automatically chosen mother is not a user request.
    if options.get("direction"): parts.append("修改方向：" + options["direction"])
    numbers = {r["asset_id"]: f"@图{i}" for i, r in enumerate(form["references"], 1)}
    roles = [numbers[v] + "作为" + LEGACY_ROLE_LABELS[k] for k, v in options.get("roles", {}).items()
             if v in numbers and k in LEGACY_ROLE_LABELS]
    if roles: parts.append("参考说明：" + "；".join(roles))
    return "\n".join(p for p in parts if p)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def generation_snapshot(form):
    return copy.deepcopy({key: form[key] for key in ("model", "parameters", "prompt", "references")})


def helper_snapshot(form):
    return {**generation_snapshot(form), "assistant": {"request": assistant_request(form)}}


def empty_draft():
    return {"model": DEFAULT_MODEL, "parameters": copy.deepcopy(DEFAULT_PARAMETERS),
            "prompt": "", "references": [], "assistant": {"request": ""},
            "helper_result": None, "undo_prompt": None, "pending_submission": None}


def validate_references(prompt, count):
    if REMOVED_REFERENCE in prompt:
        raise ValueError("提示词中有已移除的参考图，请修改标记处后再生成。")
    missing = sorted({int(m[2]) for m in REFERENCE_TOKEN.finditer(prompt) if not 1 <= int(m[2]) <= count})
    if missing:
        raise ValueError("提示词引用了不存在的图片：" + "、".join("@图" + str(n) for n in missing))


def remove_number(prompt, number):
    def replace(match):
        n = int(match[2])
        if n == number: return REMOVED_REFERENCE
        return match[1] + str(n - 1 if n > number else n)
    return REFERENCE_TOKEN.sub(replace, prompt)


class AIImageStore:
    def __init__(self, store):
        self.store = store

    def get(self):
        with self.store.lock:
            row = self.store.db.execute("SELECT body FROM image_workspace WHERE id='current'").fetchone()
            if row:
                form = json.loads(row[0])
                if "request" not in form.get("assistant", {}):
                    form["assistant"] = {"request": assistant_request(form)}
                    self.store._put("image_workspace", form)
                return form
            form = {"id": "current", "revision": 0, "updated": time.time(),
                    "selected_job": None, **empty_draft()}
            self.store._put("image_workspace", form)
            return form

    def change(self, mutate):
        with self.store.lock:
            form = self.get()
            mutate(form)
            form.update(revision=form["revision"] + 1, updated=time.time())
            self.store._put("image_workspace", form)
            return form

    def fields(self, **fields):
        allowed = {"model", "parameters", "prompt", "assistant", "helper_result", "undo_prompt", "selected_job", "pending_submission"}
        if not set(fields) <= allowed: raise ValueError("生图草稿字段无效。")
        return self.change(lambda f: f.update(copy.deepcopy(fields)))

    def reset_after_submission(self, job_id):
        """Start a blank draft atomically while keeping the submitted result selected."""
        return self.change(lambda f: f.update(empty_draft(), selected_job=job_id))

    def add_references(self, references):
        def mutate(form):
            known = {r["asset_id"] for r in form["references"]}
            for ref in references:
                if ref["asset_id"] not in known:
                    form["references"].append(copy.deepcopy(ref)); known.add(ref["asset_id"])
        return self.change(mutate)

    def remove_reference(self, index):
        def mutate(form):
            form["references"].pop(index)
            form["prompt"] = remove_number(form["prompt"], index + 1)
            form["assistant"]["request"] = remove_number(form["assistant"]["request"], index + 1)
            form["undo_prompt"] = None  # Old reference numbers must not re-enter the prompt.
        return self.change(mutate)

    def helper_finished(self, snapshot, result):
        applied = False
        def mutate(form):
            nonlocal applied
            form["helper_result"] = {**copy.deepcopy(result), "snapshot": copy.deepcopy(snapshot)}
            if result["prompt"].strip() and fingerprint(helper_snapshot(form)) == fingerprint(snapshot):
                form["undo_prompt"] = {"before": form["prompt"], "after": result["prompt"]}
                form["prompt"] = result["prompt"]
                applied = True
        form = self.change(mutate)
        return form, applied

    def undo(self):
        def mutate(form):
            undo = form.get("undo_prompt")
            if not undo or form["prompt"] != undo["after"]:
                raise ValueError("提示词已被手动修改，不能覆盖；可在生成稿中查看内容。")
            form["prompt"] = undo["before"]; form["undo_prompt"] = None
        return self.change(mutate)

    def pending_job(self):
        pending = self.get().get("pending_submission")
        if not pending: return None
        with self.store.lock:
            row = self.store.db.execute("SELECT job_id FROM submission_requests WHERE request_id=?", (pending["request_id"],)).fetchone()
        return self.store.job(row[0]) if row else None

    def model_cache(self):
        with self.store.lock:
            return {row[0]: json.loads(row[1]) for row in self.store.db.execute("SELECT id, body FROM image_models")}

    def save_model(self, model):
        if not model.get("model_id") or not isinstance(model.get("capabilities"), dict):
            raise ValueError("模型规格不完整。")
        self.store._put("image_models", {**copy.deepcopy(model), "id": model["model_id"]})
