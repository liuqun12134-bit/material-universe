#!/usr/bin/env python3
"""Local, opt-in skill learning store. No network or generation (Python 3.10+)."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, value):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".learning-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fresh():
    return {"schema_version": 1, "enabled": False, "records": {}, "history": []}


def load(path):
    if not path.exists():
        return fresh()
    state = read(path)
    require(state.get("schema_version") == 1 and type(state.get("enabled")) is bool
            and isinstance(state.get("records"), dict) and isinstance(state.get("history"), list),
            "Invalid state; preserve file and repair from history, do not reset it")
    return state


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise Invalid("Store busy; retry after the other writer finishes. Do not delete a live lock.") from exc
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def texts(value, label, nonempty=False):
    require(isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(x, str) and x.strip() for x in value), label + ": expected text list")


def confirmed(evidence):
    return evidence["source"] == "user_feedback" and evidence["outcome"] in {"improved", "succeeded"}


def validate(data, state, origin):
    allowed = {"id", "kind", "title", "topic", "rule", "applicability", "context", "evidence",
               "unknowns", "related_ids", "source_ids"}
    require(isinstance(data, dict) and not set(data) - allowed, "Unknown record fields")
    for k in ("id", "kind", "title", "topic", "rule"):
        require(isinstance(data.get(k), str) and data[k].strip(), k + ": required")
    require(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", data["id"]), "Invalid ID")
    require(data["kind"] in {"experience", "strategy"}, "Invalid library kind")
    require(data["topic"] in {"identity", "product", "wardrobe", "scene", "composition", "usage",
                              "continuity", "prompt", "audio"}, "Topic must describe task craft, not permissions or budgets")
    for k in ("applicability", "unknowns", "related_ids", "source_ids"):
        texts(data.get(k), k, k == "applicability")
    context = data.get("context")
    require(isinstance(context, dict), "context required")
    for k in ("task_id", "model", "settings", "changes", "observations"):
        require(k in context, "context." + k + " required")
    require(isinstance(context["task_id"], str) and context["task_id"].strip(), "task_id required")
    require(isinstance(context["model"], str) and context["model"].strip(), "model: use unknown if unavailable")
    require(isinstance(context["settings"], dict), "settings must be an object")
    texts(context["changes"], "changes"); texts(context["observations"], "observations")
    evidence = data.get("evidence")
    require(isinstance(evidence, list) and evidence, "At least one evidence item required")
    for e in evidence:
        require(isinstance(e, dict), "Invalid evidence")
        for k in ("task_id", "summary"):
            require(isinstance(e.get(k), str) and e[k].strip(), "evidence." + k + " required")
        require(e.get("source") in {"model_self_check", "media_review", "user_feedback", "user_instruction"}, "Invalid evidence source")
        require(e.get("artifact_type") in {"image", "video", "prompt", "rule"}, "Invalid evidence artifact")
        require(e.get("outcome") in {"unverified", "failed", "improved", "succeeded"}, "Invalid outcome")
        texts(e.get("scope"), "evidence.scope", True)
        require(e["artifact_type"] != "prompt" or e["outcome"] not in {"improved", "succeeded"}
                or all(x not in " ".join(e["scope"]).lower() for x in ("lip-sync", "口型", "成片连续")),
                "A prompt alone cannot validate video quality")
    serialized = json.dumps(data, ensure_ascii=False)
    require(not re.search(r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|[?&](?:X-Amz-Signature|sig|token)=", serialized, re.I),
            "Remove credentials and signed URLs before recording")
    if data["kind"] == "strategy" and origin == "auto":
        tasks = set()
        require(data["source_ids"], "Automatic strategy needs supporting experience IDs")
        for rid in data["source_ids"]:
            source = state["records"].get(rid)
            require(source and source["active"] and source["kind"] == "experience"
                    and source["status"] == "user_confirmed_effective", "Strategy source is not active user-confirmed experience")
            tasks.update(e["task_id"] for e in source["evidence"] if confirmed(e))
        require(len(tasks) >= 2, "Automatic promotion needs two independent user-confirmed tasks; one case stays experience")
    result = copy.deepcopy(data)
    feedback = [e for e in evidence if e["source"] == "user_feedback"]
    result["status"] = "user_confirmed_effective" if feedback and confirmed(feedback[-1]) else "unconfirmed"
    result["active"] = True
    return result


def fingerprint(d):
    fields = [d["kind"], d["rule"].strip(), sorted(x.strip() for x in d["applicability"])]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def usable(record, state):
    if not record["active"]:
        return False
    if record["kind"] == "strategy" and record["origin"] == "auto":
        sources = [state["records"].get(rid) for rid in record["source_ids"]]
        return bool(sources) and all(r and r["active"] and r["status"] == "user_confirmed_effective" for r in sources)
    return True


def event(state, action, rid, before, after, reason):
    state["history"].append({"revision": len(state["history"]) + 1, "time": now(), "action": action,
                             "id": rid, "before": copy.deepcopy(before), "after": copy.deepcopy(after), "reason": reason})


def execute(path, args):
    command = args.command
    state = load(path)
    if command == "status":
        return {"enabled": state["enabled"], "path": str(path), "records": len(state["records"])}
    if command == "list":
        rows = [r for r in state["records"].values() if (usable(r, state) or args.include_retracted)
                and (not args.kind or r["kind"] == args.kind)
                and (not args.query or args.query.casefold() in json.dumps(r, ensure_ascii=False).casefold())]
        return sorted(rows, key=lambda r: r["revision"], reverse=True)[:args.limit]
    if command in {"show", "history"}:
        if command == "history":
            return [e for e in state["history"] if e["id"] == args.id]
        require(args.id in state["records"], "Unknown record ID")
        return state["records"][args.id]
    # No-write path matters: disabled automatic recording must not create directories or history.
    if command == "record" and args.origin == "auto" and not state["enabled"]:
        return {"written": False, "reason": "self_optimization_off"}
    with locked(path):
        state = load(path)
        if command in {"enable", "disable"}:
            value = command == "enable"
            if value == state["enabled"]:
                return {"written": False, "enabled": value}
            event(state, command, None, state["enabled"], value, "explicit user setting")
            state["enabled"] = value
        elif command == "record":
            if args.origin == "auto" and not state["enabled"]:
                return {"written": False, "reason": "self_optimization_off"}
            candidate = validate(read(args.file), state, args.origin)
            rid = candidate["id"]; old = state["records"].get(rid)
            for existing in state["records"].values():
                if existing["id"] != rid and existing["active"] and fingerprint(existing) == fingerprint(candidate):
                    return {"written": False, "reason": "duplicate", "existing_id": existing["id"], "revision": existing["revision"]}
            if old and all(old.get(k) == v for k, v in candidate.items()):
                return {"written": False, "reason": "unchanged", "id": rid}
            if old:
                require(args.expected_revision == old["revision"], "Changed record requires current --expected-revision; merge history first")
                require(old["kind"] == candidate["kind"], "Promote with a new strategy ID, preserve experience")
                require(old["active"] or args.origin == "manual", "Automatic learning cannot reactivate a retracted record")
            candidate["revision"] = len(state["history"]) + 1
            candidate["origin"] = args.origin
            event(state, "record", rid, old, candidate, args.origin)
            state["records"][rid] = candidate
        else:
            require(args.id in state["records"], "Unknown record ID")
            old = state["records"][args.id]; updated = copy.deepcopy(old)
            if command == "retract":
                updated["active"] = False
            elif command == "restore":
                prior = [e for e in state["history"] if e["id"] == args.id and e["revision"] == args.revision]
                require(prior and isinstance(prior[0]["after"], dict), "No restorable record at that revision")
                updated = copy.deepcopy(prior[0]["after"])
            updated["revision"] = len(state["history"]) + 1
            event(state, command, args.id, old, updated, args.reason)
            state["records"][args.id] = updated
        atomic(path, state)
        return {"written": True, "enabled": state["enabled"], "revision": len(state["history"]), "path": str(path)}


def main():
    config = read(Path(__file__).resolve().parents[1] / "learning-config.json")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scope", choices=config["scopes"], default=config.get("default_scope"))
    p.add_argument("--data-root", type=Path, help="Override persistent root, primarily for isolated tests")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("status", "enable", "disable"):
        sub.add_parser(name)
    q = sub.add_parser("list"); q.add_argument("--kind", choices=["experience", "strategy"])
    q.add_argument("--query"); q.add_argument("--limit", type=int, default=20); q.add_argument("--include-retracted", action="store_true")
    for name in ("show", "history", "retract", "restore"):
        q = sub.add_parser(name); q.add_argument("id")
        if name in {"retract", "restore"}:
            q.add_argument("--reason", required=True)
        if name == "restore":
            q.add_argument("--revision", type=int, required=True)
    q = sub.add_parser("record"); q.add_argument("--file", type=Path, required=True)
    q.add_argument("--origin", choices=["auto", "manual"], required=True); q.add_argument("--expected-revision", type=int)
    args = p.parse_args()
    try:
        require(args.scope, "Select director mode with --scope")
        base = args.data_root or Path(os.environ.get("SKILL_LEARNING_HOME", str(Path.home() / ".local/share/skill-learning")))
        path = base.expanduser().resolve() / args.scope / "state.json"
        installed = Path(__file__).resolve().parents[1]
        require(not path.is_relative_to(installed), "Learning data must live outside the installed skill")
        print(json.dumps(execute(path, args), ensure_ascii=False, allow_nan=False))
        return 0
    except (Invalid, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
