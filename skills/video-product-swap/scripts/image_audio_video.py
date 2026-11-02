"""Fixed Seedance 2.5 image/audio workflow, independent of the desktop UI."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

from fish_mcp import ENDPOINT, FishClient, FishError, put_media
from model_runner.core import GenerationRequest, ModelSpec
from model_runner.task_state import TaskRecord, TaskFailed

MODEL = "seedance-2.5"
IMAGE_FIELDS = ("first_frame", "person_image", "product_image")
MEDIA_FIELDS = (*IMAGE_FIELDS, "audio")
FIELD_LABELS = {"first_frame": "首帧图", "person_image": "人物多视角图",
                "product_image": "产品多视角图", "audio": "音频"}
RATIOS = ("9:16", "16:9", "1:1", "4:3", "3:4", "21:9")
RESOLUTIONS = ("480p", "720p", "1080p")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".mp3": "audio/mpeg", ".wav": "audio/wav"}
BINDING = ("素材用途：@图1 为开场首帧构图参考；@图2 为同一人物的多视角身份参考；"
           "@图3 为同一产品的多视角外观参考；@音频1 为完整口播音频。"
           "从 @图1 的构图与人物状态开始，保持人物、产品外观一致，并按 @音频1 的内容和节奏表演。\n\n")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path(value, label, limit):
    if not str(value or "").strip():
        raise ValueError(f"请上传{label}，此项必填。")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"找不到{label}，请重新选择。")
    if not 0 < path.stat().st_size <= limit:
        raise ValueError(f"{label}为空或超过 {limit // 1024 // 1024} MB。")
    return path


def inspect_image(value, label="参考图"):
    path = _path(value, label, 20 * 1024 * 1024)
    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}.get(path.suffix.lower())
    if not expected:
        raise ValueError(f"{label}仅支持 JPG、PNG、WebP。")
    try:
        with Image.open(path) as image:
            if image.format != expected:
                raise ValueError(f"{label}的真实格式与后缀不符，请正确导出，不能只修改扩展名。")
            width, height = image.size
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError(f"{label}须为单张静态图片。")
            if not (300 <= width <= 6000 and 300 <= height <= 6000 and .4 <= width / height <= 2.5):
                raise ValueError(f"{label}宽高须为 300–6000 像素，宽高比须为 0.4–2.5。")
            image.verify()
        with Image.open(path) as image:
            image.load()  # verify alone does not decode JPEG pixel data.
    except (OSError, Image.DecompressionBombError):
        raise ValueError(f"{label}损坏或无法完整解码，请重新导出。") from None
    return {"path": str(path), "mime": MIME[path.suffix.lower()], "size": path.stat().st_size,
            "width": width, "height": height, "sha256": digest_file(path)}


def _media_command(arguments, timeout=40):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("无法检查音频，请确认 FFmpeg 可用且文件没有损坏。") from None
    if result.returncode:
        raise ValueError("音频不能完整解码，可能损坏或只是修改了扩展名，请重新导出 MP3 / WAV。")
    return result


def audio_info_from_probe(payload, suffix):
    """Check container AND codec; a renamed M4A must never pass as MP3."""
    streams = payload.get("streams", [])
    tracks = [s for s in streams if s.get("codec_type") == "audio"]
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if len(tracks) != 1 or videos:
        raise ValueError("请上传只有一条音轨的 MP3 / WAV 音频文件，不能使用视频文件。")
    formats = set(payload.get("format", {}).get("format_name", "").split(","))
    codec = tracks[0].get("codec_name", "")
    if not ((suffix == ".mp3" and "mp3" in formats and codec == "mp3") or
            (suffix == ".wav" and "wav" in formats and codec.startswith("pcm_"))):
        raise ValueError("音频真实封装或编码与 MP3 / WAV 不符，请正确导出；修改文件后缀不能转换格式。")
    try:
        seconds = float(payload.get("format", {}).get("duration", tracks[0].get("duration", 0)))
    except (ValueError, TypeError):
        seconds = 0
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("无法确定音频真实时长。")
    if seconds > 30:
        raise ValueError(f"音频实际时长 {seconds:.3f} 秒，超过 30 秒上限，请裁剪后重新上传。")
    if seconds < 1.8:
        raise ValueError("音频过短：Fish Audio 要求参考音频至少 1.8 秒。")
    return {"duration": seconds, "default_duration": max(4, math.ceil(seconds)), "codec": codec}


def inspect_audio(value):
    path = _path(value, "音频", 15 * 1024 * 1024)
    if path.suffix.lower() not in (".mp3", ".wav"):
        raise ValueError("此模型音频参考仅支持 MP3 / WAV；MOV 是视频格式，M4A、AAC 等请先转为 MP3 / WAV。")
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise ValueError("缺少 FFmpeg 音频检查组件，请使用完整安装版。")
    result = _media_command([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise ValueError("无法读取音频信息，请重新导出。") from None
    info = audio_info_from_probe(payload, path.suffix.lower())
    _media_command([ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", str(path),
                    "-map", "0:a:0", "-f", "null", "-"])
    return {**info, "path": str(path), "mime": MIME[path.suffix.lower()],
            "size": path.stat().st_size, "sha256": digest_file(path)}


def validate_config(config):
    config = copy.deepcopy(config)
    media = {field: inspect_image(config.get(field), FIELD_LABELS[field]) for field in IMAGE_FIELDS}
    media["audio"] = inspect_audio(config.get("audio"))
    prompt = str(config.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("请填写视频提示词。")
    if len(BINDING + prompt) > 20000:
        raise ValueError("提示词过长，连同素材说明须在 20000 字符以内。")
    if config.get("aspect_ratio", "9:16") not in RATIOS:
        raise ValueError("请选择支持的画面比例。")
    if config.get("resolution", "480p") not in RESOLUTIONS:
        raise ValueError("请选择 480p、720p 或 1080p。")
    value = str(config.get("duration") or media["audio"]["default_duration"])
    if not value.isdigit() or not 4 <= int(value) <= 30:
        raise ValueError("生成秒数必须是 4–30 的整数。")
    if int(value) < media["audio"]["default_duration"]:
        raise ValueError(f"生成时长不能短于完整音频，请至少选择 {media['audio']['default_duration']} 秒。")
    config.update({field: media[field]["path"] for field in MEDIA_FIELDS})
    config.update(prompt=prompt, duration=int(value), aspect_ratio=config.get("aspect_ratio", "9:16"),
                  resolution=config.get("resolution", "480p"), audio_seconds=media["audio"]["duration"],
                  model=MODEL, provider="fish_audio")
    return config, media


def generation_args(config, inputs):
    return {"workspace_id": config["workspace_id"], "model_id": MODEL, "operation": "reference",
            "inputs": inputs, "prompt": BINDING + config["prompt"],
            "parameters": {"aspect_ratio": config["aspect_ratio"], "resolution": config["resolution"],
                           "duration": f"{config['duration']}s"}}


def validate_live_model(model, config, media):
    cap = model.get("capabilities", {}).get("reference", {})
    schema = cap.get("parameter_schema", {}).get("properties", {})
    args = generation_args(config, [])
    for key, value in args["parameters"].items():
        if value not in schema.get(key, {}).get("enum", []):
            raise FishError(f"Fish Audio 当前不支持所选{key}参数，请更新后再提交。")
    if model.get("model_id") != MODEL or len(args["prompt"]) > model.get("prompt_max_chars", 0):
        raise FishError("Fish Audio 模型或提示词限制已变化，已停止提交。")
    for role, fields in (("ref", IMAGE_FIELDS), ("audioRef", ("audio",))):
        spec = cap.get("inputs", {}).get(role, {})
        if spec.get("max_count", 0) < len(fields):
            raise FishError("Fish Audio 当前参考数量限制不支持此模块。")
        for field in fields:
            item = media[field]
            if item["mime"] not in spec.get("mime_types", []) or item["size"] > spec.get("max_bytes", 0):
                raise FishError(f"{FIELD_LABELS[field]}不满足 Fish Audio 当前格式或大小限制。")
            if role == "ref":
                bounds = spec.get("limits", {})
                w, h = item["width"], item["height"]
                if not (bounds.get("minWidthPixels", 0) <= w <= bounds.get("maxWidthPixels", math.inf) and
                        bounds.get("minHeightPixels", 0) <= h <= bounds.get("maxHeightPixels", math.inf) and
                        bounds.get("minAspectRatio", 0) <= w / h <= bounds.get("maxAspectRatio", math.inf)):
                    raise FishError(f"{FIELD_LABELS[field]}不满足 Fish Audio 当前尺寸限制。")
            else:
                bounds = spec.get("duration", {})
                if not bounds.get("min_sec", 0) <= item["duration"] <= min(bounds.get("max_sec", 30), bounds.get("max_total_sec", 30)):
                    raise FishError("音频不满足 Fish Audio 当前时长限制。")


def prepare_quote(config, store, client, progress=lambda _: None):
    progress("正在检查图片、音频真实格式和时长")
    config, media = validate_config(config)
    if not config.get("workspace_id"):
        raise ValueError("请先连接 Fish Audio 并选择工作区。")
    model = client.call("get_media_model", {"model_id": MODEL})
    validate_live_model(model, config, media)
    inputs, hashes, assets = [], {}, []
    for field in MEDIA_FIELDS:
        progress(f"保存并上传{FIELD_LABELS[field]}")
        asset = store.import_asset(config[field])
        path = asset["path"]
        if digest_file(path) != media[field]["sha256"]:
            raise ValueError("校验后素材发生变化，请重新选择并预估。")
        config[field] = path
        assets.append(asset["id"])
        hashes[field] = media[field]["sha256"]
        slot = client.call("create_media_upload", {"workspace_id": config["workspace_id"],
                           "file_name": Path(path).name, "file_size": media[field]["size"],
                           "content_type": media[field]["mime"]})
        if not slot.get("upload_url") or not slot.get("object_key"):
            raise FishError("Fish Audio 没有提供有效上传地址，尚未提交生成。")
        put_media(slot["upload_url"], path, media[field]["mime"])
        if digest_file(path) != hashes[field]:
            raise ValueError("上传期间素材发生变化，请重新预估。")
        inputs.append({"role": "audioRef" if field == "audio" else "ref", "object_key": slot["object_key"]})
    progress("正在取得 Fish Audio 实际积分报价")
    args = generation_args(config, inputs)
    quote = client.call("estimate_video_generation", args)
    if quote.get("can_generate") is not True:
        raise FishError("Fish Audio 当前不能生成：请检查套餐、可用积分和素材限制。")
    credits = quote.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or not math.isfinite(credits) or credits < 0 or not quote.get("pricing_version"):
        raise FishError("Fish Audio 未提供有效报价，已停止。")
    config.update(reference_assets=[config[field] for field in MEDIA_FIELDS],
                  media_hashes=hashes, connection_id=client.connection_id,
                  fish_quote={"arguments": args, "credits": credits, "pricing_version": quote["pricing_version"],
                              "balance": quote.get("balance"), "created": time.time()})
    return config


def validate_approved(config):
    if config.get("cost_approved") is not True:
        raise ValueError("请先查看并确认 Fish Audio 本次积分报价。")
    quote = config.get("fish_quote", {})
    args = quote.get("arguments", {})
    inputs = args.get("inputs", [])
    if len(inputs) != 4 or [x.get("role") for x in inputs] != ["ref", "ref", "ref", "audioRef"] or not all(x.get("object_key") for x in inputs):
        raise ValueError("参考素材未完整上传，请重新预估。")
    if args != generation_args(config, inputs):
        raise ValueError("参数与已确认报价不一致，请重新预估。")
    for field in MEDIA_FIELDS:
        if not config.get(field) or digest_file(config[field]) != config.get("media_hashes", {}).get(field):
            raise ValueError(f"{FIELD_LABELS[field]}与已确认素材不一致，请重新预估。")
    return args


def _terminal_media(payload):
    media = payload.get("media")
    if isinstance(media, dict) and media.get("url"):
        return media
    for key in ("results", "outputs", "result"):
        children = payload.get(key, [])
        if isinstance(children, dict):
            children = [children]
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    found = _terminal_media(child)
                    if found:
                        return found
    return None


def download_result(record, progress):
    output = Path(record.data["output"])
    if output.exists():
        raise FishError("输出文件已存在，已停止以免覆盖。")
    url = record.data["video_url"]
    result = {"success": True, "provider": "fish_audio", "model": MODEL,
              "task_id": record.data.get("task_id"), "video_url": url,
              "view_url": record.data.get("view_url"), "output_kind": "video",
              "local_downloaded": False, "output": None}
    temporary = output.with_name(output.stem + ".download.mp4")
    try:
        if urlparse(url).scheme != "https":
            raise FishError("成片下载地址无效。")
        progress("正在下载并校验成片")
        with requests.get(url, stream=True, timeout=(20, 120)) as response:
            if not response.ok:
                raise FishError(f"下载失败（HTTP {response.status_code}）。")
            size = 0
            with temporary.open("wb") as dest:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 2 * 1024 ** 3:
                        raise FishError("下载结果超过 2 GB，已停止。")
                    dest.write(chunk)
        from media_inspection import probe_video
        probe_video(temporary)
        temporary.replace(output)
        result.update(local_downloaded=True, output=str(output))
    except Exception:
        result["download_error"] = "成片下载或媒体校验失败，可在使用记录继续原任务或打开 Fish 历史页面；不会重新生成。"
    finally:
        temporary.unlink(missing_ok=True)
    return result


def run_job(job, credentials, update, resume=False, client_factory=FishClient, sleep=time.sleep):
    config = copy.deepcopy(job["config"])
    output = Path(config["output"])
    record = TaskRecord(Path(job.get("task_state_file") or TaskRecord.path_for(output)))
    record.on_update = lambda state, path: update(task_state_file=str(path), remote_status=state.get("status"), task_id=state.get("task_id"))
    progress = lambda message: update(stage=message)
    with record.lock():
        if resume:
            record.read()
            done = record.completed_result()
            if done:
                return done
            if record.data.get("status") == "failed":
                raise FishError("原任务已明确失败，不会自动重新生成。")
            if record.data.get("video_url"):
                return record.finish(download_result(record, progress))
            if not record.data.get("task_id"):
                raise FishError("没有服务商任务编号，请先到 Fish Audio 核实，不能自动重新提交。")
        else:
            args = validate_approved(config)
            if output.exists():
                raise FishError("输出文件已存在，请重新选择输出路径。")
            request = GenerationRequest(config["prompt"], MODEL,
                ModelSpec(MODEL, MODEL, (), "fish_audio", "fish_seedance", "三图一音频"), [], output,
                config["duration"], config["aspect_ratio"], config["resolution"])
            record.start(request, ENDPOINT)
            record.update(status="preparing", image_audio_config=config, actual_prompt=args["prompt"],
                          reference_count=4, idempotency_key=str(uuid.uuid4()))
        try:
            with client_factory(credentials.get("FISH_AUTH_PATH"), None if resume else config.get("connection_id")) as client:
                if not resume:
                    # Authorize/initialize before marking the paid submission boundary.
                    client.call("list_my_workspaces", {})
                    progress("正在提交 Fish Audio 生成任务")
                    quote = config["fish_quote"]
                    record.update(status="submitting")
                    payload = client.call("generate_video", {**args, "expected_credits": quote["credits"],
                                          "pricing_version": quote["pricing_version"],
                                          "idempotency_key": record.data["idempotency_key"]})
                    generation_id = payload.get("generation_id")
                    if not generation_id:
                        raise FishError("提交响应没有任务编号，请到 Fish Audio 核实；不会重新提交。")
                    record.update(status="submitted", task_id=generation_id, view_url=payload.get("view_url"))
                else:
                    payload = client.call("get_generation_status", {"generation_id": record.data["task_id"]})
                deadline = time.monotonic() + 6 * 3600
                while True:
                    status = str(payload.get("status", "")).lower()
                    if status in ("failed", "cancelled", "canceled", "rejected"):
                        raise TaskFailed("Fish Audio 已报告任务失败，请在 Fish 历史页面查看原因；不会重新生成。")
                    media = _terminal_media(payload)
                    if media and status in ("completed", "succeeded", "success", "finished"):
                        record.update(status="generated", video_url=media["url"],
                                      view_url=payload.get("view_url") or record.data.get("view_url"))
                        return record.finish(download_result(record, progress))
                    if status in ("completed", "succeeded", "success", "finished"):
                        raise FishError("生成完成，但响应没有可识别的成片地址，请查看 Fish 历史页面。")
                    if time.monotonic() > deadline:
                        raise FishError("等待时间较长，请稍后继续原任务。")
                    progress("Fish Audio 正在生成，可继续提交其他视频")
                    delay = max(1, float(payload.get("poll_after_seconds") or 60))
                    sleep(delay)
                    payload = client.call("get_generation_status", {"generation_id": record.data["task_id"]})
        except Exception as exc:
            status = ("failed" if isinstance(exc, TaskFailed) else "not_submitted" if record.data.get("status") == "preparing"
                      else "interrupted" if record.data.get("task_id") else "submission_unknown")
            record.update(status=status, error=str(exc))
            raise
