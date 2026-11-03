"""Fish voice library, cloning and single-speaker synthesis; no desktop dependencies.

Speech is submitted once. A persisted audio URL permits download/processing recovery,
but a lost synchronous response is never interpreted as permission to submit again.
"""
from __future__ import annotations

import copy
import json
import math
import mimetypes
import shutil
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import requests

from fish_mcp import ENDPOINT, FishClient, FishError, FishToolError, put_media
from image_audio_video import digest_file
from model_runner.core import GenerationRequest, ModelSpec
from model_runner.task_state import TaskRecord

MODEL = "drama-3"
MAX_TEXT_CHARS = 2000
MAX_AUDIO_BYTES = 100 * 1024 * 1024
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")
_sample_publish_lock = threading.Lock()
_voice_details_lock = threading.Lock()
_voice_details_cache = OrderedDict()
_VOICE_DETAILS_TTL = 60
_VOICE_DETAILS_CACHE_SIZE = 256


def _voice_details(client, identity):
    """The list endpoint omits user notes; details returns them as description."""
    key = (client.connection_id, identity)
    now = time.monotonic()
    with _voice_details_lock:
        cached = _voice_details_cache.get(key)
        if cached and now - cached[0] < _VOICE_DETAILS_TTL:
            _voice_details_cache.move_to_end(key)
            return dict(cached[1])
    details = client.call("get_voice", {"voice_id": identity})
    if not isinstance(details, dict) or str(details.get("id", "")) != identity:
        raise FishError("音色备注响应与所选音色不匹配。")
    with _voice_details_lock:
        _voice_details_cache[key] = (now, dict(details))
        _voice_details_cache.move_to_end(key)
        while len(_voice_details_cache) > _VOICE_DETAILS_CACHE_SIZE:
            _voice_details_cache.popitem(last=False)
    return details


def _https(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def list_voices(client, page=1, query=""):
    page = int(page)
    if page < 1:
        raise ValueError("音色页码必须从 1 开始。")
    # Fish's search_voices endpoint accepts at most 20 voices per page.
    args = {"self": True, "page": page, "page_size": 20, "sort_by": "created_at"}
    if str(query).strip():
        args["query"] = str(query).strip()
    payload = client.call("search_voices", args)
    rows = payload.get("voices")
    if not isinstance(rows, list):
        raise FishError("Fish Audio 没有返回有效的音色列表。")
    voices = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        details, unavailable = {}, False
        if "description" not in row:
            try:
                details = _voice_details(client, str(row["id"]))
            except (FishError, OSError, ValueError):
                # One unavailable/private detail must not hide the other voices.
                unavailable = True
        description = row.get("description", details.get("description", ""))
        description = description.strip() if isinstance(description, str) else ""
        sample_info = details.get("sample") if isinstance(details.get("sample"), dict) else {}
        sample = row.get("sample_audio") or row.get("sample_url") or sample_info.get("audio") or ""
        voices.append({**row, "id": str(row["id"]), "title": str(row.get("title") or "未命名音色"),
                       "description": description, "description_unavailable": unavailable,
                       "sample_url": sample if _https(sample) else ""})
    return {"voices": voices, "has_next": payload.get("has_next") is True,
            "page": page, "connection_id": client.connection_id}


def _media_tool(name):
    path = shutil.which(name)
    if not path:
        raise ValueError("缺少音频处理组件，请使用完整安装版。")
    return path


def _run_media(args, timeout=120):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("音频处理未完成，请检查文件或稍后继续下载。") from None
    if result.returncode:
        raise ValueError("音频文件不能完整解码或处理，请使用有效的音频文件。")
    return result


def _inspect_audio(path, full=True):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
        raise ValueError("音频文件不存在、为空或超过 100 MB。")
    result = _run_media([_media_tool("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
                         "-show_format", "-show_streams", "-of", "json", str(path)])
    try:
        data = json.loads(result.stdout)
        tracks = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        duration = float(data.get("format", {}).get("duration", tracks[0].get("duration", 0)))
    except (ValueError, IndexError, TypeError, AttributeError):
        raise ValueError("无法读取音频信息，请重新导出音频。") from None
    if len(tracks) != 1 or videos or not math.isfinite(duration) or duration <= 0:
        raise ValueError("请使用只有一条有效音轨的音频文件，不能使用视频文件。")
    if full:
        _run_media([_media_tool("ffmpeg"), "-v", "error", "-xerror", "-nostdin",
                    "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"])
    return {"path": str(path), "duration": duration, "size": path.stat().st_size,
            "sha256": digest_file(path), "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def inspect_clone_audio(path):
    if Path(path).suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError("声音样本支持 MP3、WAV、M4A、AAC、FLAC、OGG、Opus 音频。")
    info = _inspect_audio(path)
    if info["duration"] > 270:
        raise ValueError("声音样本总时长不能超过 270 秒，建议提供 10–60 秒清晰人声。")
    return info


def _upload_audio(client, info):
    slot = client.call("get_upload_url", {"filename": Path(info["path"]).name})
    if not _https(slot.get("upload_url")) or not _https(slot.get("file_url")):
        raise FishError("Fish Audio 没有提供有效的音频上传地址。")
    if digest_file(info["path"]) != info["sha256"]:
        raise ValueError("声音样本已变化，请重新选择。")
    put_media(slot["upload_url"], info["path"], info["mime"])
    if digest_file(info["path"]) != info["sha256"]:
        raise ValueError("上传期间声音样本已变化，请重新选择。")
    return slot["file_url"]


def _clone_result(payload):
    voice = payload.get("voice") if isinstance(payload.get("voice"), dict) else {}
    identity = payload.get("voice_id") or voice.get("id") or payload.get("id")
    status = payload.get("status") or ("created" if identity else "unknown")
    if identity and status in {"success", "completed", "succeeded"}:
        status = "created"
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    prompt = (payload.get("verification_prompt") or payload.get("prompt_sentence") or payload.get("prompt")
              or verification.get("prompt") or "")
    return {**payload, "status": status, "voice_id": identity,
            "verification_prompt": prompt, "instructions": payload.get("instructions") or ""}


def _clone_call(client, tool, args):
    try:
        payload = client.call(tool, args)
    except FishToolError as exc:
        if exc.payload.get("status") not in {"quality_warning", "verification_required"}:
            raise
        payload = exc.payload
    result = _clone_result(payload)
    # Pending/failed verification replies need not repeat the original session ID.
    # Retain it so the user can supply a new recording for the same attempt.
    if not result.get("preflight_id") and args.get("preflight_id"):
        result["preflight_id"] = args["preflight_id"]
    return result


def _visibility(visibility):
    if visibility not in {"private", "unlist"}:
        raise ValueError("请选择私有或不公开音色。")
    return visibility


def create_clone(client, paths, title, confirm_voice_rights, visibility="private",
                 preflight_id=None, proceed_despite_warnings=False):
    if confirm_voice_rights is not True:
        raise ValueError("请先确认声音属于本人，或已获得说话者的克隆授权。")
    args = {"confirm_voice_rights": True, "visibility": _visibility(visibility)}
    if str(title).strip():
        args["title"] = str(title).strip()
    if preflight_id:
        args["preflight_id"] = str(preflight_id)
        if proceed_despite_warnings:
            args["proceed_despite_warnings"] = True
    else:
        if proceed_despite_warnings:
            raise ValueError("没有待确认的质量提示，请重新创建音色。")
        if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 5:
            raise ValueError("请选择同一说话者的 1–5 段声音样本。")
        media = [inspect_clone_audio(path) for path in paths]
        if sum(info["duration"] for info in media) > 270:
            raise ValueError("声音样本总时长不能超过 270 秒，建议提供 10–60 秒清晰人声。")
        args["audio_urls"] = [_upload_audio(client, info) for info in media]
    return _clone_call(client, "create_voice_clone", args)


def verify_clone(client, preflight_id, recording_path=None, title="", visibility="private"):
    if not str(preflight_id or "").strip():
        raise ValueError("没有待验证的克隆会话。")
    args = {"preflight_id": str(preflight_id), "visibility": _visibility(visibility)}
    if str(title).strip():
        args["title"] = str(title).strip()
    if recording_path:
        args["recording_url"] = _upload_audio(client, inspect_clone_audio(recording_path))
    return _clone_call(client, "verify_voice_clone", args)


def speech_form_settings(config):
    """Map a saved form to current controls; never reinterpret old percent as dB."""
    migrated = config.get("audio_settings_mode") != "fish" or "volume_db" not in config

    def number(value, default):
        try:
            value = float(value) if not isinstance(value, bool) else default
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    speed = min(1.3, max(.7, number(config.get("speed", 1), 1)))
    if "volume_db" in config:
        volume = number(config["volume_db"], 0)
    else:
        percent = number(config.get("volume", 100), 100)
        volume = 20 * math.log10(percent / 100) if percent > 0 else -5
    return {"speed": round(speed, 2), "volume_db": round(min(5., max(-5., volume)), 1),
            "migrated": migrated}


def validate_speech_config(config):
    config = copy.deepcopy(config)
    text = str(config.get("text") or "").strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"请填写合成文案，单次最多 {MAX_TEXT_CHARS} 个字符。")
    identity = str(config.get("voice_id") or "").strip()
    if not identity:
        raise ValueError("请先选择一个音色。")
    if "volume" in config or config.get("audio_settings_mode", "fish") != "fish":
        raise ValueError("旧版语速和音量设置不能直接提交，请在声音合成页重新预估。")
    try:
        if isinstance(config.get("speed"), bool) or isinstance(config.get("volume_db"), bool):
            raise ValueError
        speed, volume_db = float(config.get("speed", 1)), float(config.get("volume_db", 0))
    except (ValueError, TypeError):
        raise ValueError("语速和音量须为有效数值。") from None
    if not math.isfinite(speed) or not .7 <= speed <= 1.3:
        raise ValueError("语速须为 0.7–1.3 倍，请调整后重新预估。")
    if not math.isfinite(volume_db) or not -5 <= volume_db <= 5:
        raise ValueError("音量须为 −5 至 +5 dB，请调整后重新预估。")
    output = str(config.get("output") or "")
    if not output or Path(output).suffix.lower() != ".wav":
        raise ValueError("声音合成输出须为 WAV 文件。")
    config.update(text=text, voice_id=identity, model=MODEL, provider="fish_audio",
                  output_kind="audio", speed=speed, volume_db=volume_db, audio_settings_mode="fish")
    return config


def speech_arguments(config):
    return {"text": config["text"], "voice_id": config["voice_id"], "model": MODEL}


def _validate_plain_tts_settings(config):
    # Plain MCP TTS has no numeric prosody fields; never silently drop them.
    if config["speed"] != 1 or config["volume_db"] != 0:
        raise ValueError("此合成接口不支持语速和音量参数，请在声音合成页重新预估。")


def _quote(client, args):
    payload = client.call("text_to_speech", {**args, "dry_run": True})
    credits = payload.get("estimated_credits")
    if (payload.get("dry_run") is not True or isinstance(credits, bool)
            or not isinstance(credits, (int, float)) or not math.isfinite(credits) or credits < 0):
        raise FishError("Fish Audio 未返回有效语音报价，未提交合成。")
    if payload.get("has_sufficient_balance") is not True or payload.get("can_generate") is False:
        raise FishError("当前积分不足或套餐不支持本次合成，请检查 Fish Audio 账户。")
    if payload.get("model", MODEL) != MODEL:
        raise FishError("Fish 返回的报价模型与 Drama 3 不一致，未提交合成。")
    return {"arguments": args, "credits": credits, "balance": payload.get("balance"),
            "quoted_model": payload.get("model"), "created": time.time()}


def prepare_speech(config, client):
    # A new direct request must not inherit an old Studio draft or approval.
    config = {key: value for key, value in config.items()
              if not key.startswith("studio_") and key not in
              {"variants", "cost_approved", "fish_quote", "batch_id", "variant_index", "variant_label"}}
    config = validate_speech_config(config)
    _validate_plain_tts_settings(config)
    # Verify that decoding and saving the returned audio can run before charging.
    _media_tool("ffmpeg"); _media_tool("ffprobe")
    if not client.connection_id:
        raise FishError("请先在设置中连接 Fish Audio。")
    quote = _quote(client, speech_arguments(config))
    quote.update(speed=config["speed"], volume_db=config["volume_db"], audio_settings_mode="fish")
    config.update(fish_quote=quote, connection_id=client.connection_id, variant_backend="tts")
    return config


def validate_approved(config):
    valid = validate_speech_config(config)
    _validate_plain_tts_settings(valid)
    quote = config.get("fish_quote", {})
    if config.get("cost_approved") is not True or not config.get("connection_id"):
        raise ValueError("请先查看并确认本次合成的积分报价。")
    if (quote.get("arguments") != speech_arguments(valid) or quote.get("speed") != valid["speed"]
            or quote.get("volume_db") != valid["volume_db"] or quote.get("audio_settings_mode") != "fish"):
        raise ValueError("文案、音色、语速或音量已改变，请重新预估。")
    credits = quote.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or not math.isfinite(credits) or credits < 0:
        raise ValueError("报价无效，请重新预估。")
    return valid


def _download(url, destination):
    if not _https(url):
        raise FishError("音频下载地址无效。")
    destination = Path(destination)
    try:
        # Do not forward the MCP authorization header to media storage.
        with requests.get(url, stream=True, timeout=(20, 120)) as response:
            if not response.ok:
                raise FishError(f"音频下载失败（HTTP {response.status_code}）。")
            size = 0
            with destination.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise FishError("音频下载超过 100 MB，已停止。")
                    output.write(chunk)
    except requests.RequestException:
        raise FishError("音频下载中断，可继续下载原结果。") from None


def download_sample(url, destination):
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _inspect_audio(destination)
        return destination
    # Multiple rapid preview requests may share one cache key. Each owns only its
    # temporary file; finishing/cancelling a request must not remove another's data.
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    try:
        _download(url, temporary)
        _inspect_audio(temporary)
        # Publish each immutable cache entry once. On Windows replacing a file
        # while another caller/ffplay is reading it can raise PermissionError.
        with _sample_publish_lock:
            if not destination.exists():
                temporary.replace(destination)
        _inspect_audio(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def download_result(record, progress=lambda _: None):
    output = Path(record.data["output"])
    if output.exists():
        raise FishError("输出文件已存在，已停止以免覆盖。")
    config = record.data["speech_config"]
    settings = ({"speed": config["speed"], "volume_db": config["volume_db"], "audio_settings_mode": "fish"}
                if config.get("audio_settings_mode") == "fish" else
                {"speed": 1., "volume_db": 0., "audio_settings_mode": "fish_original",
                 "legacy_requested_speed": config.get("speed", 1),
                 "legacy_requested_volume": config.get("volume", 100)})
    urls = record.data.get("audio_urls") or [record.data["audio_url"]]
    sources = [output.with_name(output.stem + f".source-{i}.part") for i in range(len(urls))]
    processed = output.with_name(output.stem + ".processed.wav")
    result = {"success": True, "provider": "fish_audio", "model": MODEL, "output_kind": "audio",
              "audio_url": record.data["audio_url"], "audio_urls": urls, "output": None, "local_downloaded": False,
              "voice_id": config["voice_id"], "voice_name": config.get("voice_name", ""),
              **settings,
              "credits_charged": record.data.get("credits_charged")}
    try:
        progress("正在下载合成音频")
        total_size = 0
        for url, source in zip(urls, sources):
            _download(url, source)
            total_size += source.stat().st_size
            if total_size > MAX_AUDIO_BYTES:
                raise FishError("合成音频总大小超过 100 MB，已停止下载。")
            _inspect_audio(source)
        progress("正在保存 Fish 原始音频为 WAV")
        command = [_media_tool("ffmpeg"), "-v", "error", "-xerror", "-nostdin", "-y"]
        for source in sources:
            command.extend(["-protocol_whitelist", "file,pipe", "-i", str(source)])
        if len(sources) == 1:
            command.extend(["-map", "0:a:0"])
        else:
            joined = "".join(f"[{i}:a:0]" for i in range(len(sources)))
            command.extend(["-filter_complex", f"{joined}concat=n={len(sources)}:v=0:a=1[out]",
                            "-map", "[out]"])
        _run_media([*command, "-vn", "-c:a", "pcm_s16le", str(processed)], timeout=300)
        info = _inspect_audio(processed)
        processed.replace(output)
        result.update(output=str(output), local_downloaded=True, duration=info["duration"])
    except (OSError, ValueError, FishError):
        result["download_error"] = "音频下载或保存未完成，可在使用记录继续下载原结果，不会再次合成或扣费。"
    finally:
        for source in sources:
            source.unlink(missing_ok=True)
        processed.unlink(missing_ok=True)
    return result


def _audio_url(payload):
    for key in ("audio_url", "url"):
        if _https(payload.get(key)):
            return payload[key]
    for key in ("audio", "media", "result"):
        if isinstance(payload.get(key), dict):
            found = _audio_url(payload[key])
            if found:
                return found
    return None


def _audio_urls(payload):
    """Fish may split longer speech into clips; never silently keep just one."""
    for key in ("audio_urls", "audios", "clips", "results", "segments", "outputs"):
        rows = payload.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        if len(rows) > 32:
            raise FishError("合成响应包含过多音频片段，请到 Fish Audio 核实原结果。")
        urls = [row if isinstance(row, str) and _https(row) else _audio_url(row)
                if isinstance(row, dict) else None for row in rows]
        if not all(urls):
            raise FishError("合成响应有音频片段缺少地址，请到 Fish Audio 核实原结果。")
        return urls
    for key in ("audio", "media", "result"):
        if isinstance(payload.get(key), dict):
            nested = _audio_urls(payload[key])
            if nested:
                return nested
    single = _audio_url(payload)
    return [single] if single else []


def run_job(job, credentials, update, resume=False, client_factory=FishClient):
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
                return record.finish(download_result(record, progress))
            raise FishError("原合成没有返回音频地址，请先到 Fish Audio 核实；不会自动再次合成。")
        config = validate_approved(config)
        if output.exists():
            raise FishError("输出文件已存在，请选择新的保存位置。")
        request = GenerationRequest(config["text"], MODEL,
                    ModelSpec(MODEL, MODEL, (), "fish_audio", "fish_speech", "单音色语音合成"), [], output, None)
        record.start(request, ENDPOINT)
        record.update(status="preparing", speech_config=config, actual_prompt=config["text"])
        try:
            with client_factory(credentials.get("FISH_AUTH_PATH"), config["connection_id"]) as client:
                # Recheck price/permissions after a queue delay, before the paid boundary.
                progress("正在核对语音报价与授权")
                fresh = _quote(client, speech_arguments(config))
                if fresh["credits"] != config["fish_quote"]["credits"]:
                    raise FishError("合成报价已变化，尚未合成；请重新预估并确认。")
                _media_tool("ffmpeg"); _media_tool("ffprobe")
                record.update(status="submitting")
                progress("Drama 3 正在合成声音")
                payload = client.call("text_to_speech", {**speech_arguments(config), "dry_run": False})
                urls = _audio_urls(payload)
                if not urls:
                    raise FishError("合成响应没有音频地址，请先到 Fish Audio 核实；不会自动再次合成。")
                record.update(status="generated", audio_url=urls[0], audio_urls=urls,
                              credits_charged=payload.get("credits_charged"))
            return record.finish(download_result(record, progress))
        except Exception as exc:
            status = ("generated" if record.data.get("audio_url") else "not_submitted"
                      if record.data.get("status") == "preparing" else "submission_unknown")
            record.update(status=status, error=str(exc))
            raise
