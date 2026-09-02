from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from ..core import Credential, ImagePreprocessorError, ModelSpec, image_data_uri, nested
from .base import ImageGenerationAdapter


GENERATION_PROMPT = """
Use the supplied product image as the sole identity reference. Create one square 2x2 multi-view studio contact sheet of exactly the same product: front view, three-quarter view, side view, and rear or alternate three-quarter view. Use a clean white or very light neutral seamless studio background with soft commercial lighting. Preserve the product geometry, cap shape and state, materials, colors, proportions, logo placement, label shape, and distinctive packaging details consistently across all four views. Remove every Chinese character and Chinese marketing sentence from the package, background, and watermarks. Preserve a clearly legible Latin brand name, Latin letters, numbers, and capacity markings only when they are visible in the reference. Do not invent claims or dense replacement copy; simplify uncertain small text instead of producing gibberish. Show no hands, people, props, scene elements, watermarks, borders, captions, or unrelated objects. Do not redesign the product.
""".strip()


def _validate_size(value: str) -> str:
    size = value.strip()
    if size in {"1K", "2K"}:
        return size
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", size)
    if not match:
        raise ImagePreprocessorError("Seedream size 必须是 1K、2K 或 768-2048 范围内的 WIDTHxHEIGHT。")
    width, height = int(match.group(1)), int(match.group(2))
    if not (768 <= width <= 2048 and 768 <= height <= 2048):
        raise ImagePreprocessorError("Seedream 自定义宽高必须在 768-2048 范围内。")
    return size


def _response_json(response: Any, action: str) -> dict[str, Any]:
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ImagePreprocessorError(f"{action}没有返回有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise ImagePreprocessorError(f"{action}返回格式错误。")
    return payload


def _task_id(payload: dict[str, Any]) -> str | None:
    value = nested(payload, ("task_id",), ("data", "task_id"), ("id",), ("data", "id"))
    return str(value) if value else None


def _status(payload: dict[str, Any]) -> str:
    value = nested(payload, ("status",), ("data", "status"), ("result", "status"))
    return str(value or "").strip().lower()


def _image_url(payload: dict[str, Any]) -> str | None:
    value = nested(
        payload,
        ("data", 0, "url"),
        ("data", "images", 0, "url"),
        ("result", "data", 0, "url"),
        ("result", "url"),
        ("image_url",),
        ("url",),
    )
    return str(value) if value else None


def _error_detail(payload: dict[str, Any]) -> str:
    value = nested(payload, ("error",), ("message",), ("data", "error"), ("data", "message"))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "未知错误")


def _same_origin(a: str, b: str) -> bool:
    left, right = urlparse(a), urlparse(b)
    return (left.scheme.lower(), left.netloc.lower()) == (right.scheme.lower(), right.netloc.lower())


def _actual_suffix(data: bytes, content_type: str) -> str:
    lowered = content_type.lower()
    if data.startswith(b"\x89PNG\r\n\x1a\n") or "image/png" in lowered:
        return ".png"
    if data.startswith(b"\xff\xd8\xff") or "image/jpeg" in lowered:
        return ".jpg"
    if (data.startswith(b"RIFF") and data[8:12] == b"WEBP") or "image/webp" in lowered:
        return ".webp"
    raise ImagePreprocessorError("下载结果不是可识别的 PNG、JPEG 或 WEBP 图片。")


def _state_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".seedream-task.json")


def _input_hash(image_path: Path) -> str:
    digest = hashlib.sha256()
    with image_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImagePreprocessorError(f"Seedream 任务状态文件无效：{path}") from exc
    if not isinstance(payload, dict):
        raise ImagePreprocessorError(f"Seedream 任务状态文件格式错误：{path}")
    return payload


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class SeedreamAsyncAdapter(ImageGenerationAdapter):
    name = "seedream-async"

    @staticmethod
    def _endpoint(credential: Credential) -> str:
        return urljoin(credential.api_base + "/", credential.submit_path)

    @staticmethod
    def _reference_value(image_path: Path, model: ModelSpec, include_data: bool) -> Any:
        options = model.options
        if options.get("reference_encoding") != "data_uri":
            raise ImagePreprocessorError("Seedream 适配器当前只实现 data_uri 参考图编码。")
        value = image_data_uri(image_path) if include_data else "<local-image-as-data-uri>"
        shape = options.get("reference_shape", "array")
        if shape == "array":
            return [value]
        if shape == "scalar":
            return value
        raise ImagePreprocessorError(f"不支持的 Seedream reference_shape：{shape}")

    def _payload(self, image_path: Path, model: ModelSpec, size: str, include_data: bool) -> dict[str, Any]:
        field = str(model.options.get("reference_field", "")).strip()
        if not field:
            raise ImagePreprocessorError("Seedream 模型注册缺少 reference_field。")
        return {
            "model": model.api_model,
            "prompt": GENERATION_PROMPT,
            "n": int(model.options.get("n", 1)),
            "size": _validate_size(size),
            field: self._reference_value(image_path, model, include_data),
        }

    def plan(
        self,
        image_path: Path,
        model: ModelSpec,
        credential: Credential,
        output: Path,
        size: str,
    ) -> dict[str, Any]:
        return {
            "model": model.api_model,
            "provider": model.provider,
            "adapter": self.name,
            "endpoint": self._endpoint(credential),
            "poll_endpoint_template": urljoin(credential.api_base + "/", credential.poll_path or ""),
            "credential_source": credential.api_key_source,
            "credential_configured": bool(credential.api_key),
            "request_payload": self._payload(image_path, model, size, include_data=False),
            "output": str(output),
            "reference_field_confirmed_by_supplied_doc": False,
        }

    def _submit(
        self, image_path: Path, model: ModelSpec, credential: Credential, size: str, timeout: int
    ) -> dict[str, Any]:
        if not credential.api_key:
            raise ImagePreprocessorError("Seedream 图片生成缺少 API Key。")
        request = urllib.request.Request(
            self._endpoint(credential),
            data=json.dumps(self._payload(image_path, model, size, include_data=True), ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(timeout, 300)) as response:
                return _response_json(response, "提交 Seedream 图片任务")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-1500:]
            raise ImagePreprocessorError(
                f"Seedream 提交失败（HTTP {exc.code}）：{detail}。"
                "用户提供的文档未说明参考图字段；当前注册为 image 数组 data URI，请先核对平台实际字段。"
            ) from exc
        except urllib.error.URLError as exc:
            raise ImagePreprocessorError(f"无法连接 Seedream 服务：{exc.reason}") from exc

    def _poll(self, task_id: str, credential: Credential, timeout: int) -> tuple[dict[str, Any], str]:
        if not credential.poll_path or not credential.api_key:
            raise ImagePreprocessorError("Seedream Provider 没有配置轮询路径或 API Key。")
        poll_url = urljoin(credential.api_base + "/", credential.poll_path.format(task_id=task_id))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            request = urllib.request.Request(
                poll_url,
                headers={"Authorization": f"Bearer {credential.api_key}", "Accept": "application/json"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = _response_json(response, "查询 Seedream 图片任务")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[-1500:]
                if exc.code in {429, 500, 502, 503, 504}:
                    print(
                        f"Seedream 轮询临时失败（HTTP {exc.code}），继续查询同一任务……",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(5)
                    continue
                raise ImagePreprocessorError(f"Seedream 查询失败（HTTP {exc.code}）：{detail}") from exc
            except urllib.error.URLError as exc:
                print(
                    f"Seedream 轮询连接临时中断，继续查询同一任务：{exc.reason}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(5)
                continue
            status = _status(payload)
            url = _image_url(payload)
            if url:
                return payload, url
            if status in {"failed", "failure", "error", "cancelled", "canceled"}:
                raise ImagePreprocessorError(f"Seedream 图片任务失败：{_error_detail(payload)}")
            print(f"Seedream 图片任务状态：{status or '未知'}", file=sys.stderr, flush=True)
            time.sleep(5)
        raise ImagePreprocessorError(f"等待 Seedream 图片任务超时（{timeout} 秒）。")

    @staticmethod
    def _download(url: str, output: Path, credential: Credential) -> Path:
        headers = {"Accept": "image/*"}
        if credential.api_key and _same_origin(url, credential.api_base):
            headers["Authorization"] = f"Bearer {credential.api_key}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise ImagePreprocessorError(f"下载 Seedream 图片失败（HTTP {exc.code}）。") from exc
        except urllib.error.URLError as exc:
            raise ImagePreprocessorError(f"下载 Seedream 图片失败：{exc.reason}") from exc
        if not data:
            raise ImagePreprocessorError("Seedream 图片下载结果为空。")
        suffix = _actual_suffix(data, content_type)
        actual_output = output if output.suffix.lower() == suffix else output.with_suffix(suffix)
        if actual_output.exists():
            raise ImagePreprocessorError(f"输出文件已存在：{actual_output}")
        actual_output.parent.mkdir(parents=True, exist_ok=True)
        actual_output.write_bytes(data)
        return actual_output

    def generate(
        self,
        image_path: Path,
        model: ModelSpec,
        credential: Credential,
        output: Path,
        size: str,
        timeout: int,
    ) -> dict[str, Any]:
        state_file = _state_path(output)
        state = _read_state(state_file)
        input_digest = _input_hash(image_path)
        resumed = False
        if state:
            if state.get("model") != model.api_model or state.get("input_sha256") != input_digest:
                raise ImagePreprocessorError(
                    f"Seedream 任务状态文件与当前模型或输入图不匹配：{state_file}"
                )
            saved_output = Path(str(state.get("actual_output") or output)).resolve()
            if state.get("status") == "completed" and saved_output.is_file():
                return {
                    "success": True,
                    "technical_completed": True,
                    "quality_passed": None,
                    "model": model.api_model,
                    "provider": model.provider,
                    "adapter": self.name,
                    "task_id": state.get("task_id"),
                    "image_url": state.get("image_url"),
                    "output": str(saved_output),
                    "status": "completed",
                    "resumed_existing_task": True,
                    "task_state_file": str(state_file),
                }
            task_id = str(state.get("task_id") or "").strip()
            if not task_id:
                raise ImagePreprocessorError(f"Seedream 任务状态文件缺少 task_id：{state_file}")
            url = str(state.get("image_url") or "").strip() or None
            final_payload = state
            resumed = True
            print(f"恢复 Seedream 任务：{task_id}", file=sys.stderr, flush=True)
        else:
            if output.exists():
                raise ImagePreprocessorError(f"输出文件已存在：{output}")
            submitted = self._submit(image_path, model, credential, size, timeout)
            task_id = _task_id(submitted)
            url = _image_url(submitted)
            final_payload = submitted
            if task_id:
                _write_state(
                    state_file,
                    {
                        "model": model.api_model,
                        "provider": model.provider,
                        "input": str(image_path),
                        "input_sha256": input_digest,
                        "requested_output": str(output),
                        "task_id": task_id,
                        "image_url": url,
                        "status": _status(submitted) or "submitted",
                    },
                )
                print(f"Seedream task_id：{task_id}", file=sys.stderr, flush=True)
        if not url:
            if not task_id:
                raise ImagePreprocessorError("Seedream 提交响应中没有 task_id 或图片 URL。")
            final_payload, url = self._poll(task_id, credential, timeout)
        actual_output = self._download(url, output, credential)
        _write_state(
            state_file,
            {
                "model": model.api_model,
                "provider": model.provider,
                "input": str(image_path),
                "input_sha256": input_digest,
                "requested_output": str(output),
                "actual_output": str(actual_output),
                "task_id": task_id,
                "image_url": url,
                "status": "completed",
            },
        )
        return {
            "success": True,
            "technical_completed": True,
            "quality_passed": None,
            "model": model.api_model,
            "provider": model.provider,
            "adapter": self.name,
            "task_id": task_id,
            "image_url": url,
            "output": str(actual_output),
            "status": _status(final_payload) or "completed",
            "resumed_existing_task": resumed,
            "task_state_file": str(state_file),
        }
