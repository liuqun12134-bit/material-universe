"""Official Alibaba Wan3 async protocol and temporary OSS media uploads."""
from __future__ import annotations

import math
import time
import uuid
from urllib.parse import quote, urlparse

from ..core import VideoGenerationError
from ..task_state import TaskFailed
from .base import ModelAdapter, response_json, same_origin
from .fal_base import queue_get

POLL_TIMEOUT = 1800
POLL_INTERVAL = 5


class DashScopeWan3Adapter(ModelAdapter):
    name = "dashscope_wan3"

    def validate_credential(self, credential):
        import re
        parsed = urlparse(credential.api_base)
        host = parsed.hostname or ""
        shared = {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com",
                  "cn-hongkong.dashscope.aliyuncs.com"}
        workspace = re.fullmatch(r"[a-zA-Z0-9-]+\.(cn-beijing|ap-southeast-1|ap-northeast-1|eu-central-1|us-east-1|cn-hongkong)\.maas\.aliyuncs\.com", host)
        if parsed.scheme != "https" or parsed.path.rstrip("/") != "/api/v1" or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port or (host not in shared and not workspace):
            raise VideoGenerationError("阿里官方服务地址无效，请使用北京 https://dashscope.aliyuncs.com/api/v1，或官方地域/业务空间的完整原生 API 地址。")

    def plan(self, request, credential):
        self.validate_credential(credential)
        return super().plan(request, credential)

    def validate(self, request):
        super().validate(request)
        request.duration = 5 if request.duration is None else request.duration
        if type(request.duration) is not int or not 2 <= request.duration <= 30:
            raise VideoGenerationError("阿里官方 Wan3 时长必须是 2–30 秒的整数。")
        options = request.options or {}
        if options.get("enable_thinking"):
            raise VideoGenerationError("阿里官方 Wan3 没有思考模式参数，请关闭思考模式。")
        for key in ("enable_prompt_expansion", "audio", "watermark"):
            if key in options and type(options[key]) is not bool:
                raise VideoGenerationError(f"{key} 必须是布尔值。")
        if "seed" in options and (type(options["seed"]) is not int or not -1 <= options["seed"] <= 2147483647):
            raise VideoGenerationError("阿里官方 seed 必须是 -1 或 0–2147483647 的整数。")
        videos, audios = 0.0, 0.0
        from media_inspection import probe_video, probe_frame_rate, probe_audio_duration
        from PIL import Image
        rules = {"image": ({".jpg", ".jpeg", ".png", ".webp"}, 20, 10),
                 "video": ({".mp4", ".mov"}, 100, 5), "audio": ({".wav", ".mp3"}, 15, 5)}
        for kind, (suffixes, megabytes, maximum) in rules.items():
            refs = [r for r in request.references if r.kind == kind]
            if len(refs) > maximum:
                raise VideoGenerationError(f"阿里官方 Wan3 最多支持 {maximum} 个{kind}参考。")
            for ref in refs:
                if ref.local_path is None:
                    continue
                path = ref.local_path
                if path.suffix.lower() not in suffixes or not 0 < path.stat().st_size <= megabytes * 1024 * 1024:
                    raise VideoGenerationError(f"阿里官方 Wan3 的{kind}素材格式或大小不符：{path.name}")
                try:
                    if kind == "image":
                        with Image.open(path) as image:
                            w, h = image.size
                            if min(w, h) < 240 or max(w, h) > 8000 or max(w / h, h / w) > 8:
                                raise VideoGenerationError("阿里官方参考图边长须为 240–8000 像素，宽高比不超过 8:1。")
                            if "A" in image.getbands() or "transparency" in image.info:
                                raise VideoGenerationError("阿里官方参考图不能带透明通道，请使用无透明底图片。")
                    elif kind == "video":
                        info = probe_video(path)
                        if not math.isfinite(info.duration) or not 1 <= info.duration <= 15.001:
                            raise VideoGenerationError("阿里官方参考视频单个时长须为 1–15 秒。")
                        if min(info.width, info.height) < 240 or max(info.width, info.height) > 4096 or max(info.width / info.height, info.height / info.width) > 8 or probe_frame_rate(path) < 16:
                            raise VideoGenerationError("阿里官方参考视频边长须为 240–4096 像素、宽高比不超过 8:1，帧率至少 16fps。")
                        videos += info.duration
                    else:
                        duration = probe_audio_duration(path)
                        if not math.isfinite(duration) or not 1 <= duration <= 15.001:
                            raise VideoGenerationError("阿里官方参考音频单个时长须为 1–15 秒。")
                        audios += duration
                except VideoGenerationError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    raise VideoGenerationError(f"阿里官方参考素材信息读取失败：{path.name}：{exc}") from exc
        if videos > 15.001 or audios > 15.001:
            raise VideoGenerationError("阿里官方参考视频或参考音频总时长不能超过 15 秒。")
        if videos + request.duration > 30.001:
            raise VideoGenerationError("阿里官方输入视频总时长加输出时长不能超过 30 秒。")

    def submit_endpoint(self, request, credential):
        return credential.api_base.rstrip("/") + "/services/aigc/video-generation/video-synthesis"

    def payload(self, request, url_for):
        options = request.options or {}
        parameters = {"duration": request.duration, "resolution": (request.resolution or "480p").upper(),
                      "ratio": request.aspect_ratio or "adaptive", "prompt_extend": options.get("enable_prompt_expansion", False)}
        for key in ("audio", "watermark", "seed"):
            if key in options:
                parameters[key] = options[key]
        return {"model": request.model.api_model,
                "input": {"prompt": request.prompt, "media": [{"type": "reference_" + ref.kind, "url": url_for(ref)} for ref in request.references]},
                "parameters": parameters}

    def remote_payload(self, request):
        return self.payload(request, lambda ref: ref.value if ref.local_path is None else "oss://pending/" + ref.local_path.name)

    def upload_file(self, session, credential, model, ref):
        # The documented temporary upload API uses the shared regional domain.
        host = urlparse(credential.api_base).hostname or ""
        if "ap-southeast-1" in host or host == "dashscope-intl.aliyuncs.com":
            upload_base = "https://dashscope-intl.aliyuncs.com/api/v1"
        elif "us-east-1" in host or host == "dashscope-us.aliyuncs.com":
            upload_base = "https://dashscope-us.aliyuncs.com/api/v1"
        elif "cn-hongkong" in host:
            upload_base = "https://cn-hongkong.dashscope.aliyuncs.com/api/v1"
        elif "cn-beijing" in host or host == "dashscope.aliyuncs.com":
            upload_base = "https://dashscope.aliyuncs.com/api/v1"
        else:
            raise VideoGenerationError("当前阿里地域的临时文件上传地址尚未登记，请使用已支持的地域或公网参考链接。")
        response = session.get(upload_base + "/uploads", params={"action": "getPolicy", "model": model},
                               headers={"Authorization": f"Bearer {credential.api_key}"}, timeout=60, allow_redirects=False)
        data = response_json(response, "申请阿里临时上传策略").get("data", {})
        upload_host = str(data.get("upload_host", ""))
        parsed = urlparse(upload_host)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".aliyuncs.com"):
            raise VideoGenerationError("阿里临时上传策略没有可信的 OSS 地址。")
        fields = {"OSSAccessKeyId": data.get("oss_access_key_id"), "Signature": data.get("signature"),
                  "policy": data.get("policy"), "x-oss-object-acl": data.get("x_oss_object_acl"),
                  "x-oss-forbid-overwrite": data.get("x_oss_forbid_overwrite")}
        if not all(fields.values()) or not data.get("upload_dir"):
            raise VideoGenerationError("阿里临时上传策略不完整。")
        key = str(data["upload_dir"]).rstrip("/") + "/" + uuid.uuid4().hex + ref.local_path.suffix.lower()
        fields.update(key=key, success_action_status="200")
        with ref.local_path.open("rb") as file:
            uploaded = session.post(upload_host, data=fields, files={"file": (ref.local_path.name, file, ref.mime_type)},
                                    timeout=(30, 300), allow_redirects=False)
        if uploaded.status_code != 200:
            raise VideoGenerationError(f"阿里临时文件上传失败（HTTP {uploaded.status_code}），尚未提交生成。")
        return "oss://" + key

    def execute(self, request, credential):
        self.validate_credential(credential)
        import requests
        session = requests.Session()
        if request.task_record:
            request.task_record.update(status="preparing")
        uploaded = {}
        for ref in request.references:
            if ref.local_path:
                if request.progress:
                    request.progress(f"上传阿里官方参考素材（{ref.kind}）")
                uploaded[id(ref)] = self.upload_file(session, credential, request.model.api_model, ref)
        payload = self.payload(request, lambda ref: uploaded[id(ref)] if ref.local_path else ref.value)
        headers = {"Authorization": f"Bearer {credential.api_key}", "X-DashScope-Async": "enable"}
        if uploaded:
            headers["X-DashScope-OssResourceResolve"] = "enable"
        if request.task_record:
            request.task_record.update(status="submitting")
        try:
            response = session.post(self.submit_endpoint(request, credential), headers=headers, json=payload,
                                    timeout=300, allow_redirects=False)
        except Exception as exc:
            raise VideoGenerationError("阿里官方提交响应未确认，程序不会自动重投，请在官方控制台核实。") from exc
        # An HTTP response rejecting input/auth clearly did not create a task.
        if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
            if request.task_record:
                request.task_record.update(status="preparing")
        result = response_json(response, "提交阿里官方视频任务")
        task_id = result.get("output", {}).get("task_id")
        if not task_id:
            raise VideoGenerationError("阿里官方提交响应没有任务编号，请先在控制台核实，程序不会自动重投。")
        request.task_record.update(task_id=str(task_id), status="submitted")
        return self.resume(request, credential, session=session)

    def resume(self, request, credential, session=None):
        self.validate_credential(credential)
        import requests
        session = session or requests.Session()
        state = request.task_record.data
        task_id = str(state.get("task_id") or "")
        if not task_id and not state.get("video_url"):
            raise VideoGenerationError("没有阿里原任务编号，禁止重新提交。")
        video_url = state.get("video_url")
        headers = {"Authorization": f"Bearer {credential.api_key}"}
        deadline = time.monotonic() + POLL_TIMEOUT
        while not video_url and time.monotonic() < deadline:
            endpoint = credential.api_base.rstrip("/") + "/tasks/" + quote(task_id, safe="")
            response = queue_get(session, endpoint, headers=headers, timeout=60, allow_redirects=False)
            payload = response_json(response, "查询阿里官方原任务")
            output = payload.get("output", {})
            status = str(output.get("task_status") or "").upper()
            if request.progress:
                request.progress(f"阿里官方任务：{status or '查询中'}")
            if status in {"FAILED", "CANCELED", "CANCELLED"}:
                raise TaskFailed(f"阿里官方任务已失败：{output.get('message') or output.get('code') or status}")
            if status == "UNKNOWN":
                raise VideoGenerationError("阿里官方任务状态 UNKNOWN，可能已过有效期，请到官方控制台核实原任务。")
            if status == "SUCCEEDED":
                video_url = output.get("video_url")
                if not video_url:
                    raise VideoGenerationError("阿里官方任务成功但未返回成片地址，可继续查询原任务。")
                request.task_record.update(video_url=video_url, status="generated")
                break
            if status not in {"PENDING", "RUNNING"}:
                raise VideoGenerationError(f"阿里官方返回未识别任务状态：{status}，请核实原任务。")
            time.sleep(POLL_INTERVAL)
        if not video_url:
            raise VideoGenerationError("阿里官方任务查询超时，可稍后继续原任务，禁止重新提交。")
        if urlparse(video_url).scheme != "https":
            raise VideoGenerationError("阿里官方成片地址必须使用 HTTPS。")
        return self.deliver(request, session, video_url, task_id, headers if same_origin(video_url, credential.api_base) else {})
