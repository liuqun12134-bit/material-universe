"""Direct speech jobs and legacy pairs, outside the video queue.

Each take remains an ordinary history row. Repeating a local request only returns
the persisted rows; continuing a take only queries/downloads an attempted request.
The caller owns one background worker so Tk stays responsive.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

_guard = threading.RLock()
_active_stores = set()
_ATTEMPTED = {"submitting", "submitted", "submission_unknown", "generated", "interrupted"}
_QUERYABLE = {"submitting", "submitted", "submission_unknown"}


def _scope(store):
    return str(store.root.resolve())


def batch_jobs(store, batch_id):
    jobs = []
    for job in store.jobs():
        config = job.get("config", {})
        if job.get("kind") == "speech" and (job.get("batch_id") or config.get("batch_id")) == batch_id:
            # Imported per-output records retain these fields in speech_config.
            job.setdefault("batch_id", config.get("batch_id"))
            job.setdefault("variant_index", config.get("variant_index", 0))
            jobs.append(job)
    return sorted(jobs, key=lambda job: job["variant_index"])


def latest_batch(store, connection_id=None):
    for job in store.jobs():
        config = job.get("config", {})
        identity = job.get("batch_id") or config.get("batch_id")
        if (job.get("kind") == "speech" and identity and
                (connection_id is None or config.get("connection_id") == connection_id)):
            return batch_jobs(store, identity)
    return []


def _record(job):
    filename = job.get("task_state_file") or str(job["config"]["output"]) + ".video-task.json"
    try:
        value = json.loads(Path(filename).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def can_resume(job):
    if job.get("status") in {"running", "queued", "cancelled"}:
        return False
    state = _record(job)
    if state.get("status") == "failed":
        return False
    if state.get("audio_url"):
        return True
    return bool(state.get("status") in _QUERYABLE and
                state.get("studio_project_id") and state.get("studio_block"))


def _notify(store, batch_id, callback):
    jobs = batch_jobs(store, batch_id)
    if callback:
        callback(copy.deepcopy(jobs))
    return jobs


def _redact(fields, credentials):
    encoded = json.dumps(fields, ensure_ascii=False)
    for key, value in credentials.items():
        if value and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET")):
            encoded = encoded.replace(str(value), "***")
    return json.loads(encoded)


def _existing(store, request_id, digest, count):
    row = store.db.execute("SELECT job_id, config_hash FROM submission_requests WHERE request_id=?",
                           (request_id,)).fetchone()
    if not row:
        return None
    if row[1] != digest:
        raise ValueError("同一次合成的文案或参数已改变，请重新预估后生成。")
    first = store.job(row[0])
    identity = first.get("batch_id") or first.get("config", {}).get("batch_id")
    jobs = batch_jobs(store, identity)
    if len(jobs) != count:
        raise ValueError("原合成记录不完整或已删除，请核实已有结果后新建一次合成。")
    return identity


def _create(store, config, request_id):
    if config.get("cost_approved") is not True:
        raise ValueError("请先确认本次合成费用。")
    single = config.get("variant_backend") == "tts"
    if single:
        from voice_service import validate_approved
        validate_approved(config)
    variants = [config] if single else config.get("variants")
    if not isinstance(variants, list) or len(variants) != (1 if single else 2):
        raise ValueError("请重新预估两个音频版本。")
    fields = ("text", "voice_id", "model", "speed", "volume_db", "audio_settings_mode")
    if any(any(variant.get(key) != config.get(key) for key in fields) for variant in variants):
        raise ValueError("两个版本的文案、音色或参数已改变，请重新预估。")
    if not single and (len({variant.get("studio_block") for variant in variants}) != 2 or
                       len({variant.get("studio_project_id") for variant in variants}) != 1):
        raise ValueError("两个版本的草稿信息不一致，请重新预估。")
    prices = [variant.get("fish_quote", {}).get("credits") for variant in variants]
    if (any(isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0 for price in prices)
            or config.get("fish_quote", {}).get("credits") != sum(prices)):
        raise ValueError("两个版本的合计报价不一致，请重新预估。")
    digest = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with store.lock, store.db:
        existing = _existing(store, request_id, digest, len(variants))
        if existing:
            return existing, False
        batch_id = uuid.uuid4().hex
        now = time.time()
        identities = []
        for index, variant in enumerate(variants, 1):
            snapshot = copy.deepcopy(variant)
            if (snapshot.get("variant_backend") != ("tts" if single else "studio") or
                    snapshot.get("connection_id") != config.get("connection_id")):
                raise ValueError("两个版本的服务或授权快照不一致，请重新预估。")
            snapshot = {key: value for key, value in snapshot.items() if not key.startswith("_")
                        and not any(word in key.lower() for word in ("key", "token", "secret", "credential"))}
            source = Path(config.get("output") or snapshot.get("output") or "")
            if source.suffix.lower() != ".wav":
                raise ValueError("声音结果须保存为 WAV 文件。")
            label = "合成音频" if single else "版本 " + ("A" if index == 1 else "B")
            suffix = batch_id[:12] if single else label.replace(" ", "") + "_" + batch_id[:12]
            snapshot.update(cost_approved=True, batch_id=batch_id, variant_index=index,
                            variant_label=label,
                            output=str(source.with_name(f"{source.stem}_{suffix}.wav")))
            identity = uuid.uuid4().hex
            job = {"id": identity, "kind": "speech", "batch_id": batch_id, "variant_index": index,
                   "name": f"{(snapshot.get('text', '').strip().splitlines() or ['声音合成'])[0][:40]} · {label}",
                   "config": snapshot, "created": now, "updated": now, "status": "queued",
                   "stage": "准备" + label, "asset_ids": [], "request_id": request_id,
                   "events": [], "result": {}, "error": ""}
            store.db.execute("INSERT INTO jobs VALUES (?, ?)", (identity, json.dumps(job, ensure_ascii=False)))
            identities.append(identity)
        store.db.execute("INSERT INTO submission_requests VALUES (?, ?, ?)", (request_id, identities[0], digest))
        return batch_id, True


def _run_one(store, job, credentials, callback, resume, run_variant):
    batch_id = job.get("batch_id") or job["config"]["batch_id"]
    identity = job["id"]

    def update(**fields):
        fields.setdefault("batch_id", batch_id)
        fields.setdefault("variant_index", job.get("variant_index", job["config"].get("variant_index")))
        store.update(identity, **_redact(fields, credentials))
        _notify(store, batch_id, callback)

    update(status="running", stage="正在继续原结果" if resume else "正在合成" + job["config"]["variant_label"],
           started=time.time(), finished=None, error="")
    try:
        result = run_variant(store.job(identity), credentials, update, resume=resume)
        update(status="success" if result.get("local_downloaded") else "attention", result=result,
               stage="音频已保存" if result.get("local_downloaded") else "已有结果，等待下载或处理",
               finished=time.time(), error=result.get("download_error", ""))
    except Exception as exc:
        state = _record(store.job(identity))
        attention = state.get("status") in _ATTEMPTED or bool(state.get("audio_url"))
        update(status="attention" if attention else "failed", error=str(exc), finished=time.time(),
               stage="请继续原结果或核实状态" if attention else "本版本未完成")


def run_batch(config, store, credentials, on_update=None, request_id=None, resume=False,
              run_variant=None, stop_requested=None):
    """Run each approved variant once; duplicate calls only return existing rows.

    `resume=True` never starts a queued/unsubmitted take. The UI normally uses
    `resume_job` to continue one chosen result instead.
    """
    if run_variant is None:
        if config.get("variant_backend") == "tts":
            from voice_service import run_job as run_variant
        else:
            from voice_pair_service import run_variant
    request_id = str(request_id or uuid.uuid4().hex)
    if not request_id or len(request_id) > 100:
        raise ValueError("合成请求编号无效。")
    scope = _scope(store)
    with _guard:
        if scope in _active_stores:
            raise ValueError("当前正在合成声音，请等待本次任务完成。")
        batch_id, created = _create(store, copy.deepcopy(config), request_id)
        if not created and not resume:
            return _notify(store, batch_id, on_update)
        _active_stores.add(scope)
    try:
        jobs = _notify(store, batch_id, on_update)
        for snapshot in jobs:
            job = store.job(snapshot["id"])
            if stop_requested and stop_requested():
                if job.get("status") == "queued":
                    store.update(job["id"], status="paused", stage="尚未提交，已停止", finished=time.time())
                continue
            if resume:
                if not can_resume(job):
                    continue
            elif job.get("status") != "queued":
                continue
            _run_one(store, job, credentials, on_update, resume, run_variant)
        return _notify(store, batch_id, on_update)
    finally:
        with _guard:
            _active_stores.discard(scope)


def resume_job(identity, store, credentials, on_update=None, run_variant=None):
    """Continue an attempted take without generating a new take or reroll."""
    scope = _scope(store)
    with _guard:
        if scope in _active_stores:
            raise ValueError("当前正在处理声音，请等待完成后再继续。")
        job = store.job(identity)
        backend = job.get("config", {}).get("variant_backend")
        if job.get("kind") != "speech" or backend not in {"studio", "tts"}:
            raise ValueError("此结果不是声音合成任务。")
        if run_variant is None:
            if backend == "tts":
                from voice_service import run_job as run_variant
            else:
                from voice_pair_service import run_variant
        batch_id = job.get("batch_id") or job["config"]["batch_id"]
        if job.get("status") == "success":
            return _notify(store, batch_id, on_update)
        if not can_resume(job):
            raise ValueError("此版本没有可继续的生成结果，请核实状态；不会重新提交合成。")
        _active_stores.add(scope)
    try:
        _run_one(store, job, credentials, on_update, True, run_variant)
        return _notify(store, batch_id, on_update)
    finally:
        with _guard:
            _active_stores.discard(scope)
