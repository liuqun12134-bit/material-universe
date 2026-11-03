"""Fish image generation/editing, including recovery of older fission tasks."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

from fish_mcp import ENDPOINT, FishClient, FishError, put_media
from image_audio_video import digest_file, _terminal_media
from model_runner.core import GenerationRequest, ModelSpec
from model_runner.task_state import TaskRecord, TaskFailed

MODEL = "gpt-image-2.5-sunburst"
RATIOS = ("3:1", "21:9", "2:1", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16", "1:2", "1:3")
MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def inspect_image(value):
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not 0 < path.stat().st_size <= 20 * 1024 * 1024:
        raise ValueError("请使用存在且不超过 20 MB 的图片。")
    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}.get(path.suffix.lower())
    try:
        with Image.open(path) as source:
            fmt, (w, h) = source.format, source.size
            if not expected or fmt != expected or getattr(source, "n_frames", 1) != 1:
                raise ValueError("仅支持真实格式对应后缀的静态 JPG、PNG、WebP 图片。")
            if w * h > 40_000_000:
                raise ValueError("参考图片超过 4000 万像素，请缩小后导入。")
            source.verify()
        with Image.open(path) as source:
            source.load()
            w, h = ImageOps.exif_transpose(source).size
    except (OSError, Image.DecompressionBombError):
        raise ValueError("图片无法完整解码，请重新导出。") from None
    return {"path": str(path), "sha256": digest_file(path), "mime": MIMES[fmt],
            "size": path.stat().st_size, "width": w, "height": h}


def image_inputs(session, sessions):
    refs = copy.deepcopy(session["references"])
    if not any(r["role"] == "original" for r in refs):
        raise ValueError("请先添加母图。人物、产品、服装和场景参考可选。")
    if session.get("selected") and session["mode"] == "refine":
        version = next(v for v in session["versions"] if v["id"] == session["selected"])
        job = sessions.job_for(version)
        result = job.get("result", {}) if job else {}
        path = result.get("asset_path") or result.get("output")
        if not path or not Path(path).is_file():
            raise ValueError("此版本尚无可用图片，请先继续原任务下载，或选择从原图重做。")
        refs.insert(0, {**inspect_image(path), "asset_id": result.get("asset_id"), "role": "base",
                        "label": f"微调底图 V{version['number']}",
                        "remote": {"object_key": result.get("media_object_key"),
                                   "generation_id": result.get("task_id"),
                                   "workspace_id": result.get("workspace_id"),
                                   "connection_id": result.get("connection_id")}})
    return refs


def effective_parameters(parameters, first):
    parameters = copy.deepcopy(parameters)
    if parameters.get("aspect_ratio") == "跟随底图":
        ratio = first["width"] / first["height"]
        parameters["aspect_ratio"] = min(RATIOS, key=lambda r: abs(math.log(ratio / (int(r.split(":")[0]) / int(r.split(":")[1])))))
    return parameters


def actual_prompt(config):
    if config.get("workflow") == "ai_image":
        prompt = config["prompt"].strip()
        if config["image_inputs"]:
            names = "、".join(f"@图{i}" for i in range(1, len(config["image_inputs"]) + 1))
            return "本次参考图按输入顺序编号为 " + names + "。\n" + prompt
        return prompt
    lines = ["请输出一张独立的图片。以下编号严格对应本次输入图片顺序："]
    for i, ref in enumerate(config["image_inputs"], 1):
        authority = {"original": "构图、镜位、姿势和未要求替换的元素依据",
                     "person": "人物身份依据；发型、妆容、表情按用户要求",
                     "product": "产品结构、比例、材质、颜色与标识依据；多视角不等于多件产品",
                     "clothing": "仅提供服装依据", "scene": "仅提供场景依据",
                     "base": "本轮局部微调底图；身份和商品真实性继续以原始参考为准"}[ref["role"]]
        lines.append(f"@图{i}：{ref['label']}，{authority}。")
    if config["mode"] == "restart":
        lines.append("本轮从原始母图重新生成。未提供旧生成图，不以失败版本作为底图。")
    else:
        lines.append("只修改本轮要求的部分，尽量保留已经满意的部分。")
    if config.get("locks"):
        lines.append("用户明确要求保留的内容：\n" + config["locks"])
    lines.append("本轮完整修改要求：\n" + config["prompt"])
    return "\n".join(lines)


def generation_args(config, inputs):
    args = {"workspace_id": config["workspace_id"], "model_id": config.get("model", MODEL),
            "prompt": actual_prompt(config), "parameters": copy.deepcopy(config["parameters"])}
    if config.get("workflow") == "ai_image" and not config["image_inputs"]:
        args["count"] = 1
    else:
        args["inputs"] = copy.deepcopy(inputs)
    return args


def is_text_generation(config):
    return config.get("workflow") == "ai_image" and not config["image_inputs"]


def upload_cache_id(connection, workspace, digest):
    return hashlib.sha256(json.dumps([connection, workspace, digest]).encode()).hexdigest()


def cache_result(store, result, asset):
    if result.get("media_object_key") and result.get("connection_id") and result.get("workspace_id"):
        identity = upload_cache_id(result["connection_id"], result["workspace_id"], asset["id"])
        store._put("image_uploads", {"id": identity, "object_key": result["media_object_key"],
                   "workspace_id": result["workspace_id"], "connection_id": result["connection_id"]})


def validate_model(model, config, media):
    cap = model.get("capabilities", {}).get("t2i" if is_text_generation(config) else "edit", {})
    spec = cap.get("inputs", {}).get("ref", {})
    if not cap or model.get("model_id") != config["model"]:
        raise ValueError("当前模型不支持本次生图方式，请刷新模型规格或切换模型。")
    if is_text_generation(config):
        if cap.get("max_count", 0) < 1:
            raise ValueError("当前模型不能生成图片，请切换模型。")
    elif (len(media) > min(cap.get("max_inputs", 0), spec.get("max_count", 0))
          or set(cap.get("required_roles", [])) - {"ref"}):
        raise ValueError("当前模型不支持本次参考输入，请减少参考或切换模型。")
    if len(actual_prompt(config)) > model.get("prompt_max_chars", 0):
        raise ValueError("本轮要求过长，请缩短修改稿。")
    properties = cap.get("parameter_schema", {}).get("properties", {})
    for key, value in config["parameters"].items():
        if value not in properties.get(key, {}).get("enum", []):
            raise ValueError(f"当前模型不支持所选 {key} 参数，请重新选择。")
    for item in media:
        if item["mime"] not in spec.get("mime_types", []) or item["size"] > spec.get("max_bytes", 0):
            raise ValueError("参考图不满足当前 Fish 模型的格式或大小限制。")
        b, w, h = spec.get("limits", {}), item["width"], item["height"]
        if not (b.get("minWidthPixels", 0) <= w <= b.get("maxWidthPixels", math.inf)
                and b.get("minHeightPixels", 0) <= h <= b.get("maxHeightPixels", math.inf)
                and b.get("minAspectRatio", 0) <= w / h <= b.get("maxAspectRatio", math.inf)
                and w * h <= b.get("maxPixelCount", math.inf)):
            raise ValueError("参考图不满足当前 Fish 模型的尺寸限制。")


def prepare_quote(config, store, client, progress=lambda _: None):
    config = copy.deepcopy(config)
    if not config.get("prompt", "").strip() or not config.get("workspace_id"):
        raise ValueError("请填写提示词，并在设置中连接 Fish 工作区。")
    if not config.get("image_inputs") and config.get("workflow") != "ai_image":
        raise ValueError("请先选择母图。")
    config.setdefault("image_inputs", [])
    if config.get("workflow") == "ai_image":
        from ai_image_store import validate_references
        validate_references(config["prompt"], len(config["image_inputs"]))
    config.setdefault("model", MODEL)
    media = [inspect_image(r["path"]) for r in config["image_inputs"]]
    for ref, info in zip(config["image_inputs"], media):
        if ref.get("sha256") != info["sha256"]:
            raise ValueError("参考图片已被修改，请重新添加后预估。")
    model = client.call("get_media_model", {"model_id": config["model"]})
    if config.get("workflow") == "ai_image":
        from ai_image_models import resolve_parameters
        config["parameters"] = resolve_parameters(config["parameters"], model, media)
    else:
        config["parameters"] = effective_parameters(config["parameters"], media[0])
    validate_model(model, config, media)
    inputs = []
    for ref, info in zip(config["image_inputs"], media):
        progress("准备参考 · " + ref["label"])
        cache_id = upload_cache_id(client.connection_id, config["workspace_id"], info["sha256"])
        with store.lock:
            row = store.db.execute("SELECT body FROM image_uploads WHERE id=?", (cache_id,)).fetchone()
        cached = json.loads(row[0]) if row else {}
        remote = cached or ref.get("remote") or {}
        key = remote.get("object_key") if (remote.get("workspace_id") == config["workspace_id"]
                                           and remote.get("connection_id") == client.connection_id) else None
        if not key and remote.get("generation_id") and remote.get("workspace_id") == config["workspace_id"]:
            payload = client.call("get_generation_status", {"generation_id": remote["generation_id"]})
            original = _terminal_media(payload)
            key = original.get("object_key") if original else None
            if not key:
                raise FishError("未取得原生成图片的引用，请查看 Fish 任务状态；不会重复上传同一结果。")
        if not key:
            slot = client.call("create_media_upload", {"workspace_id": config["workspace_id"],
                "file_name": Path(info["path"]).name, "file_size": info["size"], "content_type": info["mime"]})
            if not slot.get("upload_url") or not slot.get("object_key"):
                raise FishError("未取得有效上传地址，尚未提交生成。")
            put_media(slot["upload_url"], info["path"], info["mime"])
            key = slot["object_key"]
        if digest_file(info["path"]) != info["sha256"]:
            raise ValueError("准备期间图片发生变化，请重新添加。")
        store._put("image_uploads", {"id": cache_id, "object_key": key,
                   "workspace_id": config["workspace_id"], "connection_id": client.connection_id})
        inputs.append({"object_key": key, "role": "ref"})
    progress("正在读取本次生图费用")
    args = generation_args(config, inputs)
    quote = client.call("estimate_image_generation" if is_text_generation(config) else "estimate_image_edit", args)
    credits = quote.get("credits")
    if quote.get("can_generate") is not True or isinstance(credits, bool) or not isinstance(credits, (int, float)) or not math.isfinite(credits) or credits < 0 or not quote.get("pricing_version"):
        raise FishError("未取得可用报价，请检查积分、套餐或当前模型限制。")
    config.update(connection_id=client.connection_id, reference_assets=[r["path"] for r in config["image_inputs"]],
                  output_kind="image", provider="fish_audio", fish_quote={"arguments": args,
                  "reference_sha256": [r["sha256"] for r in config["image_inputs"]],
                  "credits": credits, "balance": quote.get("balance"), "pricing_version": quote["pricing_version"]})
    return config


def validate_approved(config):
    if config.get("cost_approved") is not True:
        raise ValueError("请先确认本次生图费用。")
    quote = config.get("fish_quote", {})
    args = quote.get("arguments", {})
    inputs = args.get("inputs", [])
    if (not inputs and not is_text_generation(config)) or len(inputs) != len(config["image_inputs"]) or any(i.get("role") != "ref" or not i.get("object_key") for i in inputs):
        raise ValueError("报价中的参考图不完整。")
    if args != generation_args(config, inputs):
        raise ValueError("图片、参数或修改稿已改变，请重新预估费用。")
    if (config.get("workflow") == "ai_image" or "reference_sha256" in quote) and quote.get("reference_sha256") != [r["sha256"] for r in config["image_inputs"]]:
        raise ValueError("参考图片或顺序已改变，请重新预估费用。")
    for ref in config["image_inputs"]:
        if digest_file(ref["path"]) != ref["sha256"]:
            raise ValueError("参考图与已确认的内容不一致，请重新添加。")
    return args


def download_result(record, progress):
    state = record.data
    output = Path(state["output"])
    config = state.get("ai_image_config") or state["image_fission_config"]
    result = {"success": True, "provider": "fish_audio", "model": config["model"], "output_kind": "image",
              "task_id": state.get("task_id"), "view_url": state.get("view_url"), "video_url": state["video_url"],
              "media_object_key": state.get("media_object_key"), "workspace_id": config["workspace_id"],
              "connection_id": config.get("connection_id"), "local_downloaded": False, "output": None}
    temporary = output.with_name(output.name + ".download")
    converted = output.with_name(output.name + ".verified")
    try:
        if output.exists():
            raise ValueError("目标图片已存在，请保留原文件后再恢复。")
        if urlparse(state["video_url"]).scheme != "https":
            raise ValueError("图片下载地址无效。")
        progress("正在下载并验证图片")
        with requests.get(state["video_url"], stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as dest:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 150 * 1024 * 1024:
                        raise ValueError("结果文件过大。")
                    dest.write(chunk)
        with Image.open(temporary) as source:
            if source.format not in MIMES or getattr(source, "n_frames", 1) != 1:
                raise ValueError("下载结果不是静态图片。")
            source.load()
            ImageOps.exif_transpose(source).save(converted, format="PNG")
        converted.replace(output)
        result.update(local_downloaded=True, output=str(output))
    except Exception:
        result["download_error"] = "图片下载或校验未完成。可继续原任务下载，或打开 Fish 历史页面；不会重新生成。"
    finally:
        temporary.unlink(missing_ok=True)
        converted.unlink(missing_ok=True)
    return result


def run_job(job, credentials, update, resume=False, client_factory=FishClient, sleep=time.sleep):
    config = copy.deepcopy(job["config"])
    output = Path(config["output"])
    record = TaskRecord(Path(job.get("task_state_file") or TaskRecord.path_for(output)))
    record.on_update = lambda state, path: update(task_state_file=str(path), remote_status=state.get("status"), task_id=state.get("task_id"))
    progress = lambda text: update(stage=text)
    with record.lock():
        if resume:
            record.read()
            done = record.completed_result()
            if done:
                return done
            if record.data.get("status") == "failed":
                raise FishError("原任务已明确失败，不会自动重提。")
            if record.data.get("video_url"):
                return record.finish(download_result(record, progress))
            if not record.data.get("task_id"):
                raise FishError("提交结果不明，没有任务编号，请先到 Fish 核实。")
        else:
            args = validate_approved(config)
            if output.exists():
                raise ValueError("输出文件已存在，不能覆盖。")
            request = GenerationRequest(config["prompt"], config["model"], ModelSpec(config["model"], config["model"], (),
                "fish_audio", "fish_image", "AI 生图"), [], output, None,
                config["parameters"].get("aspect_ratio"), config["parameters"].get("resolution"))
            record.start(request, ENDPOINT)
            snapshot_key = "ai_image_config" if config.get("workflow") == "ai_image" else "image_fission_config"
            record.update(status="preparing", **{snapshot_key: config}, actual_prompt=args["prompt"],
                          reference_count=len(config["image_inputs"]), idempotency_key=str(uuid.uuid4()))
            update(actual_prompt=args["prompt"])
        try:
            with client_factory(credentials.get("FISH_AUTH_PATH"), None if resume else config["connection_id"]) as client:
                if not resume:
                    client.call("list_my_workspaces", {})
                    progress("正在提交生图任务")
                    record.update(status="submitting")
                    quote = config["fish_quote"]
                    tool = "generate_image" if is_text_generation(config) else "generate_image_edit"
                    payload = client.call(tool, {**args, "expected_credits": quote["credits"],
                        "pricing_version": quote["pricing_version"], "idempotency_key": record.data["idempotency_key"]})
                    if not payload.get("generation_id"):
                        raise FishError("响应没有生成编号，请到 Fish 核实；不会重复提交。")
                    record.update(status="submitted", task_id=payload["generation_id"], view_url=payload.get("view_url"))
                else:
                    payload = client.call("get_generation_status", {"generation_id": record.data["task_id"]})
                deadline = time.monotonic() + 6 * 3600
                while True:
                    status = str(payload.get("status", "")).lower()
                    if status in {"failed", "cancelled", "canceled", "rejected"}:
                        raise TaskFailed("Fish 已报告生成失败，请查看平台记录，不会自动重提。")
                    media = _terminal_media(payload)
                    if status in {"completed", "succeeded", "success", "finished"}:
                        if not media:
                            raise FishError("生成完成但没有可下载图片，请查看 Fish 历史页面。")
                        record.update(status="generated", video_url=media["url"], media_object_key=media.get("object_key"),
                                      view_url=payload.get("view_url") or record.data.get("view_url"))
                        return record.finish(download_result(record, progress))
                    if time.monotonic() > deadline:
                        raise FishError("等待较久，请稍后继续原任务。")
                    progress("Fish 正在生成图片，可切换页面继续工作")
                    delay = float(payload.get("poll_after_seconds") or 20)
                    sleep(max(20, delay) if math.isfinite(delay) else 20)
                    payload = client.call("get_generation_status", {"generation_id": record.data["task_id"]})
        except Exception as exc:
            status = ("failed" if isinstance(exc, TaskFailed) else "not_submitted" if record.data.get("status") == "preparing"
                      else "interrupted" if record.data.get("task_id") else "submission_unknown")
            record.update(status=status, error=str(exc))
            raise
