"""KaiyunCode public video and OpenAI Images protocols.

No submission retries. Local references go directly to the selected provider,
except Omni Edit images, which use the disclosed 24-hour temporary host.
"""
from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import replace
from contextlib import ExitStack
from urllib.parse import quote, urljoin, urlparse

from ..core import VideoGenerationError, nested, result_url
from ..task_state import TaskFailed
from .base import ModelAdapter, FAILURE_STATES, SUCCESS_STATES, response_json, same_origin, save_download

IMAGE_RATIOS = ("1:1", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4",
                "4:5", "5:4", "21:9", "4:1", "1:4", "8:1", "1:8")
POLL_TIMEOUT = 1800
POLL_INTERVAL = 5


def api_root(credential):
    parsed = urlparse(credential.api_base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in {"", "/v1"}):
        raise VideoGenerationError("KaiyunCode 地址须为 HTTPS 站点地址，可带 /v1。")
    return credential.api_base.rstrip("/").removesuffix("/v1") + "/v1"


def validate_references(request, limits, sizes=None):
    from PIL import Image
    sizes = sizes or {"image": 10, "video": 100, "audio": 15}
    formats = {"image": {".png", ".jpg", ".jpeg", ".webp"},
               "video": {".mp4", ".mov"}, "audio": {".mp3", ".wav"}}
    for kind, maximum in limits.items():
        refs = [ref for ref in request.references if ref.kind == kind]
        if len(refs) > maximum:
            raise VideoGenerationError(f"当前模型最多支持 {maximum} 个 {kind} 参考。")
        for ref in refs:
            if ref.local_path is None:
                if urlparse(ref.value).scheme != "https":
                    raise VideoGenerationError("参考地址必须使用 HTTPS。")
                continue
            path = ref.local_path
            if path.suffix.lower() not in formats[kind] or not 0 < path.stat().st_size <= sizes[kind] * 1024**2:
                raise VideoGenerationError(f"参考文件格式或大小不符（{kind} ≤ {sizes[kind]} MB）：{path.name}")
            if kind == "image":
                try:
                    with Image.open(path) as image:
                        image.verify()
                except (OSError, ValueError) as exc:
                    raise VideoGenerationError(f"参考图片无法读取：{path.name}") from exc
            else:
                from media_inspection import probe_video, probe_audio_duration
                try:
                    probe_video(path) if kind == "video" else probe_audio_duration(path)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise VideoGenerationError(f"参考素材无法读取：{path.name}") from exc
    if any(ref.kind not in limits for ref in request.references):
        raise VideoGenerationError("不支持的参考素材类型。")


class KaiyunVideoAdapter(ModelAdapter):
    name = "kaiyuncode_video"
    remote_fields = {"image": "images", "video": "reference_video_urls", "audio": "audio_urls"}

    def validate_credential(self, credential):
        api_root(credential)

    def submit_endpoint(self, request, credential):
        return api_root(credential) + "/videos"

    def upload_field(self, reference):
        return {"image": "images", "video": "videos", "audio": "audio"}[reference.kind]

    def remote_payload(self, request):
        payload = self.build_payload(request)
        for kind, field in self.remote_fields.items():
            values = [ref.value for ref in request.references if ref.kind == kind and ref.local_path is None]
            if values:
                payload[field] = values
        return payload

    def post(self, request, credential, session):
        payload = self.remote_payload(request)
        headers = {"Authorization": f"Bearer {credential.api_key}"}
        local = [ref for ref in request.references if ref.local_path is not None]
        with ExitStack() as stack:
            files = [(self.upload_field(ref), (ref.local_path.name, stack.enter_context(ref.local_path.open("rb")), ref.mime_type))
                     for ref in local]
            fields = []
            for key, value in payload.items():
                if isinstance(value, list) and all(isinstance(item, str) for item in value):
                    fields.extend((key + "[]", item) for item in value)
                else:
                    fields.append((key, json.dumps(value, ensure_ascii=False) if isinstance(value, (bool, dict, list)) else str(value)))
            # Opening local files and preparing the request cannot create a task.
            request.task_record.update(status="submitting")
            return session.post(self.submit_endpoint(request, credential), headers=headers,
                                **({"data": fields, "files": files} if files else {"json": payload}),
                                timeout=(30, 600), allow_redirects=False)

    def execute(self, request, credential):
        import requests
        self.validate_credential(credential)
        request.task_record.update(status="preparing")
        session = requests.Session()
        if request.progress:
            request.progress("提交 KaiyunCode 生成任务")
        response = self.post(request, credential, session)
        if 400 <= response.status_code < 500 and response.status_code not in {408, 409, 429}:
            request.task_record.update(status="preparing")
        payload = response_json(response, "提交 KaiyunCode 任务")
        task_id = nested(payload, ("id",), ("task_id",), ("data", "id"), ("data", "task_id"))
        poll_url = nested(payload, ("poll_url",), ("data", "poll_url"))
        # Persist the ID before inspecting other fields.
        if task_id:
            request.task_record.update(task_id=str(task_id), status="submitted")
        if poll_url:
            poll_url = urljoin(api_root(credential) + "/", str(poll_url))
            self.check_url(poll_url, credential, polling=True)
            request.task_record.update(poll_url=poll_url, status="submitted")
        status = str(nested(payload, ("status",), ("data", "status")) or "").lower()
        if status in FAILURE_STATES:
            raise TaskFailed(f"KaiyunCode 任务失败：{payload.get('error') or payload.get('message') or status}")
        if status in SUCCESS_STATES and (result_url(payload) or payload.get("url")):
            request.task_record.update(video_url=result_url(payload) or payload["url"], status="generated")
        if not any(request.task_record.data.get(key) for key in ("task_id", "poll_url", "video_url")):
            raise VideoGenerationError("提交响应没有任务编号，请在平台核实是否受理；不会自动重投。")
        return self.resume(request, credential, session=session)

    @staticmethod
    def check_url(url, credential, *, polling=False):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise VideoGenerationError("结果或查询地址必须是有效 HTTPS 地址。")
        if polling and not same_origin(url, credential.api_base):
            raise VideoGenerationError("任务查询地址必须与提交服务同源。")

    def download_response(self, session, url, credential):
        # Follow CDN redirects without ever forwarding the provider key off-origin.
        for _ in range(5):
            self.check_url(url, credential)
            headers = {"Authorization": f"Bearer {credential.api_key}"} if credential.api_key and same_origin(url, credential.api_base) else {}
            response = session.get(url, headers=headers, timeout=300, allow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            if not location:
                break
            url = urljoin(url, location)
        raise VideoGenerationError("结果下载跳转无效，可稍后继续原任务。")

    def resume(self, request, credential, session=None, refresh_expired=True):
        import requests
        self.validate_credential(credential)
        session = session or requests.Session()
        state = request.task_record.data
        task_id = str(state.get("task_id") or "")
        endpoint = state.get("poll_url") or (api_root(credential) + "/videos/" + quote(task_id, safe="") if task_id else None)
        video_url = state.get("video_url")
        if not endpoint and not video_url:
            raise VideoGenerationError("没有原任务编号，禁止重新提交。")
        if endpoint:
            self.check_url(endpoint, credential, polling=True)
        headers = {"Authorization": f"Bearer {credential.api_key}"} if credential.api_key else {}
        deadline = time.monotonic() + POLL_TIMEOUT
        while not video_url and time.monotonic() < deadline:
            response = session.get(endpoint, headers=headers, timeout=60, allow_redirects=False)
            payload = response_json(response, "查询 KaiyunCode 原任务")
            status = str(nested(payload, ("status",), ("data", "status"), ("result", "status")) or "").lower()
            if request.progress:
                request.progress(f"KaiyunCode 任务：{status or '查询中'}")
            if status in FAILURE_STATES:
                raise TaskFailed(f"KaiyunCode 任务失败：{payload.get('error') or payload.get('message') or status}")
            if status in SUCCESS_STATES:
                video_url = result_url(payload) or nested(payload, ("url",), ("data", "url"))
                if not video_url:
                    raise VideoGenerationError("任务已完成但没有结果地址，可稍后继续查询原任务。")
                request.task_record.update(video_url=video_url, status="generated")
                break
            if status not in {"pending", "queued", "running", "processing", "submitted", "in_progress", "created"}:
                raise VideoGenerationError(f"未识别的任务状态：{status or '空'}；请核对原任务。")
            time.sleep(POLL_INTERVAL)
        if not video_url:
            raise VideoGenerationError("查询超时，可稍后继续原任务。")
        self.check_url(video_url, credential)
        if request.progress:
            request.progress("下载生成视频")
        response = self.download_response(session, video_url, credential)
        if response.status_code in {401, 403, 404, 410} and endpoint and credential.api_key and refresh_expired:
            request.task_record.update(video_url=None, status="submitted")
            return self.resume(request, credential, session=session, refresh_expired=False)
        if not response.ok or not response.content:
            raise VideoGenerationError(f"视频下载失败（HTTP {response.status_code}），可继续原任务。")
        from media_inspection import probe_video
        temporary = request.output.with_name(request.output.name + ".part")
        try:
            request.output.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(response.content)
            probe_video(temporary)
            if request.output.exists():
                raise FileExistsError(f"输出文件已存在：{request.output}")
            temporary.replace(request.output)
        except (OSError, RuntimeError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise VideoGenerationError(f"生成结果保存或视频校验失败：{exc}；可继续原任务。") from exc
        return {"success": True, "model": request.model.api_model, "provider": request.model.provider,
                "adapter": self.name, "output_kind": "video", "output": str(request.output),
                "video_url": video_url, "task_id": task_id or None, "technical_completed": True,
                "quality_passed": None, "local_downloaded": True, "delivery": "local_file",
                "reference_count": len(request.references)}


class KaiyunWan3Adapter(KaiyunVideoAdapter):
    name = "kaiyuncode_wan3"

    def validate(self, request):
        super().validate(request)
        request.duration = 5 if request.duration is None else request.duration
        if type(request.duration) is not int or not 2 <= request.duration <= 30:
            raise VideoGenerationError("KaiyunCode Wan3 时长须为 2–30 秒整数。")
        options = request.options or {}
        if set(options) - {"enable_prompt_expansion", "enable_thinking"}:
            raise VideoGenerationError("包含 KaiyunCode Wan3 不支持的参数。")
        if options.get("enable_thinking"):
            raise VideoGenerationError("KaiyunCode Wan3 不提供思考模式，请关闭。")
        if "enable_prompt_expansion" in options and type(options["enable_prompt_expansion"]) is not bool:
            raise VideoGenerationError("提示词扩写须为布尔值。")
        validate_references(request, {"image": 10, "video": 5, "audio": 5})
        if request.output.suffix.lower() != ".mp4":
            raise VideoGenerationError("视频输出须为 MP4。")

    def build_payload(self, request):
        return {"model": request.model.api_model, "prompt": request.prompt,
                "duration": request.duration, "resolution": (request.resolution or "720p").upper(),
                "ratio": request.aspect_ratio or "adaptive",
                "prompt_extend": (request.options or {}).get("enable_prompt_expansion", False)}


class KaiyunOmniAdapter(KaiyunVideoAdapter):
    name = "kaiyuncode_omni"
    remote_fields = {"image": "image_urls"}

    def validate(self, request):
        super().validate(request)
        request.duration = 8 if request.duration is None else request.duration
        if type(request.duration) is not int or request.duration not in {4, 6, 8, 10}:
            raise VideoGenerationError("Omni 时长仅支持 4、6、8、10 秒。")
        if request.aspect_ratio not in {None, "16:9", "9:16"} or request.resolution is not None or request.options:
            raise VideoGenerationError("Omni 仅支持 16:9 / 9:16，不接受清晰度或额外参数。")
        validate_references(request, {"image": 0, "video": 0, "audio": 0})
        if request.output.suffix.lower() != ".mp4":
            raise VideoGenerationError("视频输出须为 MP4。")

    def build_payload(self, request):
        return {"model": request.model.api_model, "prompt": request.prompt,
                "duration": str(request.duration), "aspect_ratio": request.aspect_ratio or "16:9"}


class KaiyunSeedanceAdapter(KaiyunVideoAdapter):
    name = "kaiyuncode_seedance"
    remote_fields = {"image": "images", "video": "videos", "audio": "audios"}

    def reference_payload(self, request, *, preview=False):
        payload = self.build_payload(request)
        for ref in request.references:
            value = ref.value
            if ref.local_path is not None:
                value = (f"<local-data-url:{ref.kind}>" if preview else
                         f"data:{ref.mime_type};base64," + base64.b64encode(ref.local_path.read_bytes()).decode("ascii"))
            payload.setdefault(self.remote_fields[ref.kind], []).append(value)
        return payload

    def plan(self, request, credential):
        plan = super().plan(request, credential)
        plan.update(transport="multipart-data-url-text", request_payload=self.reference_payload(request, preview=True),
                    local_uploads=[], local_encodings=[{"kind": ref.kind, "field": self.remote_fields[ref.kind]}
                                                      for ref in request.references if ref.local_path is not None])
        return plan

    def post(self, request, credential, session):
        # Match the provider canvas: JSON arrays are text parts with no file names.
        # Read failures happen before marking the paid submission as started.
        payload = self.reference_payload(request)
        endpoint = self.submit_endpoint(request, credential)
        request.task_record.update(status="submitting", submission_protocol={
            "adapter": self.name, "version": 1, "transport": "multipart-data-url-text",
            "endpoint_path": urlparse(endpoint).path,
            "field_counts": {field: len(payload.get(field, [])) for field in self.remote_fields.values()},
        })
        fields = [(key, (None, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict, bool)) else str(value)))
                  for key, value in payload.items()]
        response = session.post(endpoint, headers={"Authorization": f"Bearer {credential.api_key}"},
                                files=fields, timeout=(30, 600), allow_redirects=False)
        if response.status_code == 400 and "unexpected field" in (response.text or "").lower():
            request.task_record.update(status="preparing")
            raise VideoGenerationError("上传字段或数量与服务不兼容；" +
                                       f"提交 KaiyunCode 任务失败（HTTP 400）：{(response.text or '')[:1000]}")
        return response

    def validate(self, request):
        super().validate(request)
        request.duration = 4 if request.duration is None else request.duration
        if type(request.duration) is not int or request.duration not in {4, 5, 6, 8, 10, 12, 15, 20, 25, 30}:
            raise VideoGenerationError("Seedance 2.5 TJ 时长仅支持 4/5/6/8/10/12/15/20/25/30 秒。")
        if request.resolution not in {None, "720p"}:
            raise VideoGenerationError("Seedance 2.5 TJ 特价模型仅支持 720p 清晰度。")
        if request.aspect_ratio not in {None, "16:9", "9:16", "1:1"} or request.options:
            raise VideoGenerationError("Seedance 2.5 TJ 仅支持 16:9 / 9:16 / 1:1，不支持额外选项。")
        validate_references(request, {"image": 30, "video": 10, "audio": 10})
        for ref in request.references:
            if ref.local_path and ref.kind in {"video", "audio"}:
                from media_inspection import probe_video, probe_audio_duration
                duration = probe_video(ref.local_path).duration if ref.kind == "video" else probe_audio_duration(ref.local_path)
                if not 2 <= duration <= 14:
                    raise VideoGenerationError(f"Seedance 2.5 TJ 的每个 {ref.kind} 参考时长须为 2–14 秒。")
        if request.output.suffix.lower() != ".mp4":
            raise VideoGenerationError("视频输出须为 MP4。")

    def build_payload(self, request):
        return {"model": request.model.api_model, "prompt": request.prompt, "seconds": str(request.duration),
                "aspect_ratio": request.aspect_ratio or "16:9", "resolution": request.resolution or "720p"}


class KaiyunOmniEditAdapter(KaiyunVideoAdapter):
    name = "kaiyuncode_omni_edit"

    def image_urls(self, request):
        raw = (request.options or {}).get("reference_image_urls", "")
        if not isinstance(raw, str):
            raise VideoGenerationError("参考图片链接须为文本，每行一个。")
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        urls += [ref.value if ref.local_path is None else
                 "https://litter.catbox.moe/pending-upload-" + str(index) + ref.local_path.suffix.lower()
                 for index, ref in enumerate(request.references) if ref.kind == "image"]
        if len(urls) > 5:
            raise VideoGenerationError("Omni 视频编辑最多接受 5 张公网参考图。")
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise VideoGenerationError("Omni 参考图须填写公网 HTTPS 图片链接，每行一个。")
        return urls

    def validate(self, request):
        super().validate(request)
        if request.duration is not None or request.resolution is not None or request.aspect_ratio not in {None, "16:9", "9:16"}:
            raise VideoGenerationError("Omni 视频编辑时长跟随原视频，仅可选择 16:9 / 9:16 比例。")
        if set(request.options or {}) - {"reference_image_urls"}:
            raise VideoGenerationError("Omni 视频编辑包含不支持的参数。")
        validate_references(request, {"image": 5, "video": 1, "audio": 0})
        videos = [ref for ref in request.references if ref.kind == "video"]
        if len(videos) != 1:
            raise VideoGenerationError("Omni 视频编辑需要且仅接受一个原视频。")
        video = videos[0]
        if video.local_path:
            from media_inspection import probe_video
            if video.local_path.suffix.lower() != ".mp4" or probe_video(video.local_path).duration > 30.001:
                raise VideoGenerationError("Omni 视频编辑原视频须为 MP4，最长 30 秒。")
        self.image_urls(request)
        if request.output.suffix.lower() != ".mp4":
            raise VideoGenerationError("视频输出须为 MP4。")

    def upload_field(self, reference):
        return "video" if reference.kind == "video" else "fileToUpload"

    def plan(self, request, credential):
        plan = super().plan(request, credential)
        for upload in plan["local_uploads"]:
            if upload["kind"] == "image":
                upload.update(destination="Litterbox", public=True, retention="24h",
                              endpoint="https://litterbox.catbox.moe/resources/internals/api.php")
        return plan

    def post(self, request, credential, session):
        # Only this explicitly selected editing workflow needs external hosting.
        # Upload before marking the paid video request as submitting.
        references = []
        uploads = []
        for ref in request.references:
            if ref.kind != "image" or ref.local_path is None:
                references.append(ref)
                continue
            if request.progress:
                request.progress("临时公开上传 Omni 参考图（Litterbox，24小时）")
            with ref.local_path.open("rb") as file:
                response = session.post("https://litterbox.catbox.moe/resources/internals/api.php",
                                        data={"reqtype": "fileupload", "time": "24h"},
                                        files={"fileToUpload": ("reference" + ref.local_path.suffix.lower(), file, ref.mime_type)},
                                        timeout=(30, 180), allow_redirects=False)
            url = (response.text or "").strip()
            parsed = urlparse(url)
            if (response.status_code != 200 or parsed.scheme != "https"
                    or parsed.hostname != "litter.catbox.moe" or parsed.username or parsed.password
                    or parsed.port or parsed.query or parsed.fragment or not parsed.path.strip("/")):
                raise VideoGenerationError("临时图片上传失败，尚未提交 Omni 生成。")
            references.append(replace(ref, value=url, local_path=None))
            uploads.append({"provider": "litterbox", "url": url, "expires_at": time.time() + 86400})
            request.task_record.update(temporary_reference_uploads=list(uploads))
        return super().post(replace(request, references=references), credential, session)

    def remote_payload(self, request):
        content = [{"type": "text", "text": request.prompt}]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in self.image_urls(request))
        payload = {"model": request.model.api_model, "messages": [{"role": "user", "content": content}]}
        video = next(ref for ref in request.references if ref.kind == "video")
        if video.local_path is None:
            payload["video_url"] = video.value
        if request.aspect_ratio:
            payload["generationConfig"] = {"videoConfig": {"aspectRatio": request.aspect_ratio}}
        return payload


class KaiyunImageAdapter(KaiyunVideoAdapter):
    name = "kaiyuncode_image"
    remote_fields = {"image": "image_urls"}

    def submit_endpoint(self, request, credential):
        return api_root(credential) + "/images/generations"

    def upload_field(self, reference):
        return "image"

    def validate(self, request):
        if not request.prompt.strip():
            raise VideoGenerationError("提示词不能为空。")
        if request.duration is not None or request.resolution is not None:
            raise VideoGenerationError("生图不接受视频时长或视频清晰度。")
        if request.aspect_ratio not in {None, *IMAGE_RATIOS}:
            raise VideoGenerationError("不支持的图片比例。")
        options = request.options or {}
        if set(options) - {"image_size"} or options.get("image_size", "1K") not in {"1K", "2K", "4K"}:
            raise VideoGenerationError("图片尺寸仅支持 1K、2K、4K。")
        validate_references(request, {"image": 16, "video": 0, "audio": 0})
        if request.output.suffix.lower() != ".png":
            raise VideoGenerationError("图片输出须为 PNG。")

    def build_payload(self, request):
        return {"model": request.model.api_model, "prompt": request.prompt, "n": 1,
                "image_size": (request.options or {}).get("image_size", "1K"),
                "aspect_ratio": request.aspect_ratio or "1:1"}

    def execute(self, request, credential):
        import requests
        self.validate_credential(credential)
        request.task_record.update(status="preparing")
        session = requests.Session()
        if request.progress:
            request.progress("KaiyunCode 正在生成图片")
        response = self.post(request, credential, session)
        if 400 <= response.status_code < 500 and response.status_code not in {408, 409, 429}:
            request.task_record.update(status="preparing")
        payload = response_json(response, "生成 KaiyunCode 图片")
        url = nested(payload, ("data", 0, "url"))
        encoded = nested(payload, ("data", 0, "b64_json"))
        if url:
            self.check_url(str(url), credential)
            request.task_record.update(video_url=str(url), status="generated")
            return self.resume(request, credential, session=session)
        if encoded:
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise VideoGenerationError("平台返回的图片编码无效；不会重复生成。") from exc
            # Persist a received result before decoding/saving so failures are recoverable.
            request.task_record.update(image_base64=encoded, status="generated", poll_url=None)
            return self.save_image(request, content)
        raise VideoGenerationError("平台未返回图片；请核对本次调用记录，不会自动重复生成。")

    def save_image(self, request, content):
        from PIL import Image
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                output = io.BytesIO()
                image.save(output, format="PNG")
        except (OSError, ValueError) as exc:
            raise VideoGenerationError("返回内容不是有效图片；不会重复生成。") from exc
        save_download(request, output.getvalue())
        result = {"success": True, "model": request.model.api_model, "provider": request.model.provider,
                  "adapter": self.name, "output_kind": "image", "output": str(request.output),
                  "video_url": request.task_record.data.get("video_url"), "task_id": None,
                  "technical_completed": True, "quality_passed": None, "local_downloaded": True,
                  "delivery": "local_file", "reference_count": len(request.references)}
        return result

    def resume(self, request, credential, session=None):
        import requests
        state = request.task_record.data
        if state.get("image_base64"):
            return self.save_image(request, base64.b64decode(state["image_base64"], validate=True))
        url = state.get("video_url")
        if not url:
            raise VideoGenerationError("图片接口没有可查询的任务编号，请到平台核对；禁止重新生成。")
        self.check_url(url, credential)
        response = self.download_response(session or requests.Session(), url, credential)
        if not response.ok or not response.content or 300 <= response.status_code < 400:
            raise VideoGenerationError("图片下载失败，可继续下载原结果。")
        return self.save_image(request, response.content)
