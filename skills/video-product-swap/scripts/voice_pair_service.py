"""Two independently generated Drama 3 takes through Fish Story Studio.

Plain TTS has a short identical-request cache and no force/seed argument. Studio's
documented force operation produces a fresh take without changing the user's text.
Preparation edits a free draft; each approved block is generated at most once.
"""
from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from pathlib import Path

from fish_mcp import ENDPOINT, FishClient, FishError
from model_runner.core import GenerationRequest, ModelSpec
from model_runner.task_state import TaskRecord
from voice_service import (MODEL, _audio_urls, _https, _media_tool, download_result,
                           validate_speech_config)

PROJECT_NAME = "素材万象声音合成"
_project_lock = threading.Lock()


def _shared_project(client, config):
    """Discover in the active account on every preparation, including after restart.

    Never turn a failed/incomplete listing into a create, and never replay a
    create whose response was lost. The next preparation discovers it remotely.
    """
    with _project_lock:
        for page in range(1, 1001):
            listing = client.call("studio_list_projects", {"page": page, "page_size": 100})
            rows = listing.get("projects")
            if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
                    or not isinstance(listing.get("has_next"), bool)
                    or (listing["has_next"] and not rows)):
                raise FishError("无法完整核对 Fish 项目列表，尚未新建项目，请稍后重试。")
            for row in rows:
                if row.get("name") == PROJECT_NAME:
                    project = _project_id(row)
                    # Only the backend for NEW blocks is changed. Project audio
                    # settings could dirty old chapters; voice/settings are explicit per block.
                    client.call("studio_update_project", {"project_id": project, "default_backend": MODEL})
                    return project
            if not listing["has_next"]:
                break
        else:
            raise FishError("Fish 项目列表过长，未能完整核对，尚未新建项目。")
        project = _project_id(client.call("studio_create_project", {
            "name": PROJECT_NAME, "default_voice_id": config["voice_id"], "chapters": []}))
        client.call("studio_update_project", {"project_id": project,
                    "default_backend": MODEL, "scene_preset": "off"})
        return project


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _id(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _project_id(payload):
    result = _id(payload, "project_id", "id")
    if not result and isinstance(payload.get("project"), dict):
        result = _id(payload["project"], "project_id", "id")
    if not result:
        raise FishError("Fish 没有返回草稿编号，已停止；请在 Fish Story Studio 核实草稿。")
    return result


def _chapter_id(client, project, payload, title):
    result = _id(payload, "anchor", "chapter_id", "id")
    if not result and isinstance(payload.get("chapter"), dict):
        result = _id(payload["chapter"], "anchor", "chapter_id", "id")
    if result:
        return result
    overview = client.call("studio_project", {"project_id": project})
    chapters = overview.get("chapters")
    if isinstance(chapters, list):
        matches = [c for c in chapters if isinstance(c, dict) and c.get("title") == title]
        if len(matches) == 1:
            result = _id(matches[0], "anchor", "chapter_id", "id")
    if not result:
        raise FishError("无法确认双版本草稿章节，尚未合成，请重新准备。")
    return result


def _blocks(payload):
    rows = payload.get("blocks")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise FishError("Fish 没有返回有效的文案块，已停止以免生成错误内容。")
    return rows


def _anchor(block):
    identity = _id(block, "anchor", "id", "block_id")
    if not identity:
        raise FishError("文案块缺少编号，尚未合成。")
    return identity


def _voice_id(block):
    value = block.get("voice_id", block.get("voice"))
    if isinstance(value, dict):
        value = value.get("id", value.get("voice_id"))
    return value


def _resolve_voices(client, project, rows, expected_voice):
    """Studio reads return roster names, not voice IDs; resolve unambiguously."""
    if all(_voice_id(row) == expected_voice for row in rows):
        return rows
    overview = client.call("studio_project", {"project_id": project})
    roster = overview.get("roster")
    if not isinstance(roster, list):
        raise FishError("Fish 草稿缺少音色对应关系，尚未合成。")
    names = {}
    for member in roster:
        if isinstance(member, dict) and member.get("name") and member.get("voice_id"):
            names.setdefault(member["name"], set()).add(member["voice_id"])
    resolved = []
    for row in rows:
        current = _voice_id(row)
        if current == expected_voice:
            resolved.append(row)
            continue
        identities = names.get(current, set())
        if len(identities) != 1:
            raise FishError("草稿音色名称无法唯一对应到所选音色，尚未合成。")
        resolved.append({**row, "voice_id": next(iter(identities))})
    return resolved


def _check_block(block, config, *, revision=False):
    if block.get("text") != config["text"] or _voice_id(block) != config["voice_id"]:
        raise FishError("Fish 草稿的文案或音色已改变，请重新预估后合成。")
    backend = block.get("backend", block.get("model"))
    if backend is not None and backend != MODEL:
        raise FishError("Fish 草稿型号与 Drama 3 不一致，已停止合成。")
    if revision and config.get("studio_revision") != block.get("rev"):
        raise FishError("Fish 草稿在确认费用后发生变化，请重新预估。")


def _audio_settings(config):
    # Story Studio uses flat keys, unlike REST TTS's nested prosody object.
    # Keep these on each block so later project defaults cannot change a take.
    return {"speed": config["speed"], "volume": config["volume_db"]}


def _arguments(config):
    return {"project_id": config["studio_project_id"], "blocks": [config["studio_block"]],
            "force": True, "only_dirty": False}


def _quote(client, config):
    args = _arguments(config)
    payload = client.call("studio_generate", {**args, "dry_run": True})
    # The docs name credits for Studio quotes; estimated_credits is accepted for
    # compatibility with Fish's other quote tools. Unknown shapes fail closed.
    credits = payload.get("credits", payload.get("estimated_credits"))
    if (not _number(credits) or payload.get("dry_run") is False
            or payload.get("blocks_to_generate") != 1 or payload.get("cached", 0) != 0):
        raise FishError("Fish 未返回可核对的双版本报价，尚未提交任何合成。")
    if payload.get("has_sufficient_balance") is False or payload.get("can_generate") is False:
        raise FishError("当前积分不足或套餐不支持此次合成。")
    return {"credits": credits, "balance": payload.get("balance"), "arguments": args, "text": config["text"],
            "voice_id": config["voice_id"], "model": MODEL, "speed": config["speed"],
            "volume_db": config["volume_db"], "audio_settings_mode": config["audio_settings_mode"],
            "studio_audio_settings": _audio_settings(config), "created": time.time()}


def prepare_pair(config, client, progress=lambda _: None):
    """Add a task chapter to a shared free draft and quote only its two blocks."""
    config = validate_speech_config(config)
    config.pop("cost_approved", None)
    _media_tool("ffmpeg"); _media_tool("ffprobe")
    if not client.connection_id:
        raise FishError("请先连接 Fish Audio 账户。")
    client.call("studio_help", {"topic": "quickstart"})
    progress("正在准备两个版本的文案")
    project = _shared_project(client, config)
    chapter_title = "口播-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    chapter = _chapter_id(client, project,
        client.call("studio_create_chapter", {"project_id": project, "title": chapter_title}), chapter_title)
    written = client.call("studio_write_blocks", {"project_id": project, "ops": [
        {"append_to": chapter, "text": config["text"], "voice": config["voice_id"], "kind": "text",
         "audio_settings": _audio_settings(config)},
        {"append_to": chapter, "text": config["text"], "voice": config["voice_id"], "kind": "text",
         "audio_settings": _audio_settings(config)},
    ]})
    operations = written.get("results", [])
    if (len(operations) != 2 or any(not isinstance(op, dict) or op.get("status") != "ok"
            or not op.get("anchor") or not op.get("rev") for op in operations)):
        raise FishError("双版本文案或语速音量未完整写入 Fish 草稿，尚未合成。")
    accepted_revisions = {op["anchor"]: op["rev"] for op in operations}
    rows = _resolve_voices(client, project, _blocks(client.call("studio_read_blocks", {
        "project_id": project, "chapter": chapter, "include_audio": True, "limit": 3})), config["voice_id"])
    if len(rows) != 2 or len({_anchor(row) for row in rows}) != 2:
        raise FishError("Fish 草稿未返回两个独立文案块，尚未合成。")
    # MCP block reads omit audio_settings. Its revision hashes these settings;
    # bind the write acknowledgement to the read, then freeze it through billing.
    if any(accepted_revisions.get(_anchor(row)) != row.get("rev") for row in rows):
        raise FishError("Fish 草稿的语速、音量或文案发生变化，请重新预估。")
    variants = []
    progress("正在预估两个版本的积分")
    for index, block in enumerate(rows, 1):
        _check_block(block, config)
        if not isinstance(block.get("rev"), str) or not block["rev"]:
            raise FishError("文案块缺少版本信息，尚未合成。")
        output = Path(config["output"])
        label = "A" if index == 1 else "B"
        variant = {**copy.deepcopy(config), "connection_id": client.connection_id,
                   "output": str(output.with_name(output.stem + "_" + label + ".wav")),
                   "variant_backend": "studio", "variant_index": index,
                   "variant_label": "版本 " + label, "studio_project_id": project,
                    "studio_block": _anchor(block), "studio_revision": block["rev"],
                    "studio_audio_settings": _audio_settings(config)}
        variant["fish_quote"] = _quote(client, variant)
        variants.append(variant)
    balance_info = client.call("get_credit_balance", {})
    package = balance_info.get("package_balance")
    extra = balance_info.get("package_extra_balance", 0)
    if not _number(package) or not _number(extra):
        raise FishError("无法核对 Fish 可用积分，尚未提交合成。")
    # Prefer the effective balance reported by Studio when it is lower than the
    # account-wide package counters (which can include separate credit buckets).
    balance = min([package + extra] + [v["fish_quote"]["balance"] for v in variants
                                      if _number(v["fish_quote"].get("balance"))])
    prices = [v["fish_quote"]["credits"] for v in variants]
    if sum(prices) > balance:
        raise FishError("可用积分不足以生成两个版本，请补充积分后重新预估。")
    return {**config, "connection_id": client.connection_id, "studio_project_id": project,
            "variants": variants, "fish_quote": {"credits": sum(prices), "version_credits": prices,
                       "balance": balance, "created": time.time()}}


def validate_variant(config):
    valid = validate_speech_config(config)
    for key in ("connection_id", "studio_project_id", "studio_block", "studio_revision"):
        if not isinstance(valid.get(key), str) or not valid[key]:
            raise ValueError("双版本草稿信息不完整，请重新预估。")
    if valid.get("cost_approved") is not True:
        raise ValueError("请先确认两个版本的合成费用。")
    quote = valid.get("fish_quote", {})
    if not _number(quote.get("credits")) or quote.get("arguments") != _arguments(valid):
        raise ValueError("音频版本报价不匹配，请重新预估。")
    if any(quote.get(key) != valid[key] for key in ("text", "voice_id", "model", "speed", "volume_db", "audio_settings_mode")):
        raise ValueError("文案、音色或参数已改变，请重新预估。")
    if (valid.get("studio_audio_settings") != _audio_settings(valid)
            or quote.get("studio_audio_settings") != _audio_settings(valid)):
        raise ValueError("Fish 语速或音量设置与报价不一致，请重新预估。")
    return valid


def _read_block(client, config):
    rows = _resolve_voices(client, config["studio_project_id"], _blocks(client.call("studio_read_blocks", {
        "project_id": config["studio_project_id"], "blocks": [config["studio_block"]],
        "include_audio": True})), config["voice_id"])
    if len(rows) != 1:
        raise FishError("无法确认原音频版本，已停止；不会重新合成。")
    anchor = _anchor(rows[0])
    expected = config["studio_block"]
    if anchor != expected and not anchor.startswith(expected) and not expected.startswith(anchor):
        raise FishError("服务返回了其他音频版本，已停止。")
    _check_block(rows[0], config, revision=config.get("audio_settings_mode") == "fish")
    return rows[0]


def _urls(block):
    urls = _audio_urls(block)
    if not urls and _https(block.get("audio")):
        urls = [block["audio"]]
    return urls


def _collect_audio(client, record, progress, sleep, max_wait):
    config = record.data["speech_config"]
    deadline = time.monotonic() + max_wait
    while True:
        block = _read_block(client, config)
        urls = _urls(block)
        status = block.get("status", block.get("state", ""))
        if status in {"failed", "error", "cancelled"}:
            record.update(status="failed")
            raise FishError("Fish 报告此音频生成失败，已保留另一个版本；不会自动重新合成。")
        previous = record.data.get("previous_audio_urls", [])
        if urls and block.get("dirty") is not True and urls != previous:
            record.update(status="generated", audio_url=urls[0], audio_urls=urls,
                          studio_take_id=block.get("take_id"))
            return
        if time.monotonic() >= deadline:
            raise FishError("此版本仍在等待结果，可继续查看原任务；不会再次提交合成。")
        progress("正在等待原音频结果")
        delay = block.get("poll_after_seconds", 5)
        delay = min(20, max(1, delay)) if _number(delay) else 5
        sleep(min(delay, max(0, deadline - time.monotonic())))


def _finish_audio(record, progress):
    result = download_result(record, progress)
    config = record.data["speech_config"]
    result.update(variant_index=config.get("variant_index"), variant_label=config.get("variant_label"),
                  studio_project_id=config["studio_project_id"], studio_block=config["studio_block"])
    return record.finish(result)


def run_variant(job, credentials, update, resume=False, client_factory=FishClient,
                sleep=time.sleep, max_wait=120):
    """Generate one approved block once; resume only reads or downloads it."""
    config = copy.deepcopy(job["config"])
    output = Path(config["output"])
    record = TaskRecord(Path(job.get("task_state_file") or TaskRecord.path_for(output)))
    record.on_update = lambda state, path: update(task_state_file=str(path), remote_status=state.get("status"))
    progress = lambda message: update(stage=message)
    with record.lock():
        if resume:
            record.read()
            done = record.completed_result()
            if done:
                return done
            if record.data.get("audio_url"):
                return _finish_audio(record, progress)
            if record.data.get("status") not in {"submitting", "submitted", "submission_unknown"}:
                raise FishError("此版本没有可查询的已提交请求；不会自动再次合成。")
            config = record.data["speech_config"]
            with client_factory(credentials.get("FISH_AUTH_PATH"), config["connection_id"]) as client:
                client.call("studio_help", {"topic": "quickstart"})
                _collect_audio(client, record, progress, sleep, max_wait)
            return _finish_audio(record, progress)
        config = validate_variant(config)
        if output.exists():
            raise FishError("输出文件已存在，请使用新的保存位置。")
        request = GenerationRequest(config["text"], MODEL,
                    ModelSpec(MODEL, MODEL, (), "fish_audio", "fish_speech", "双版本语音合成"), [], output, None)
        record.start(request, ENDPOINT)
        record.update(status="preparing", speech_config=config, actual_prompt=config["text"],
                      studio_project_id=config["studio_project_id"], studio_block=config["studio_block"])
        try:
            with client_factory(credentials.get("FISH_AUTH_PATH"), config["connection_id"]) as client:
                client.call("studio_help", {"topic": "quickstart"})
                block = _read_block(client, config)
                _check_block(block, config, revision=True)
                progress("正在核对本版本的费用与文案")
                fresh = _quote(client, config)
                if fresh["credits"] != config["fish_quote"]["credits"]:
                    raise FishError("此版本报价已变化，尚未合成；请重新预估并确认。")
                _media_tool("ffmpeg"); _media_tool("ffprobe")
                record.update(status="submitting", previous_audio_urls=_urls(block))
                progress(config.get("variant_label", "音频") + " 正在生成")
                payload = client.call("studio_generate", {**_arguments(config), "dry_run": False})
                if payload.get("status") in {"failed", "error", "cancelled"}:
                    record.update(status="failed")
                    raise FishError("Fish 报告此版本生成失败；不会自动再次合成。")
                record.update(status="submitted", credits_charged=payload.get("credits_charged"))
                # Always re-read the explicitly scoped block. This also handles
                # remaining_anchors without issuing a second paid generation.
                _collect_audio(client, record, progress, sleep, max_wait)
            return _finish_audio(record, progress)
        except Exception as exc:
            state = record.data.get("status")
            if state not in {"failed", "generated"}:
                state = "not_submitted" if state == "preparing" else (
                    "submitted" if state == "submitted" else "submission_unknown")
            record.update(status=state, error=str(exc))
            raise
