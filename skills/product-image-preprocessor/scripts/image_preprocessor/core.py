from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ImagePreprocessorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    api_model: str
    aliases: tuple[str, ...]
    capability: str
    provider: str
    adapter: str
    input_summary: str
    options: dict[str, Any]


@dataclass(frozen=True)
class Credential:
    provider: str
    api_base: str
    api_key: str | None
    api_key_source: str | None
    submit_path: str
    poll_path: str | None


REQUIRED_ANALYSIS_FLAGS = (
    "clean_studio_background",
    "no_chinese_text",
    "multi_view",
    "product_complete_and_clear",
    "cross_view_consistent",
)


def validate_input_image(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ImagePreprocessorError(f"找不到产品图片：{resolved}")
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ImagePreprocessorError("产品图片只支持 PNG、JPEG 或 WEBP。")
    size = resolved.stat().st_size
    if size <= 0:
        raise ImagePreprocessorError("产品图片为空。")
    if size > 20 * 1024 * 1024:
        raise ImagePreprocessorError("产品图片超过 20 MB，请先压缩。")
    return resolved


def image_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def normalize_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in REQUIRED_ANALYSIS_FLAGS:
        value = payload.get(key)
        if not isinstance(value, bool):
            raise ImagePreprocessorError(f"视觉模型结果缺少布尔字段：{key}")
        normalized[key] = value

    view_count = payload.get("view_count", 0)
    if not isinstance(view_count, int) or isinstance(view_count, bool) or view_count < 0:
        raise ImagePreprocessorError("视觉模型返回的 view_count 无效。")
    normalized["view_count"] = view_count

    reasons = payload.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise ImagePreprocessorError("视觉模型返回的 reasons 无效。")
    normalized["reasons"] = [item.strip() for item in reasons if item.strip()]

    chinese = payload.get("visible_chinese_text", [])
    if not isinstance(chinese, list) or not all(isinstance(item, str) for item in chinese):
        raise ImagePreprocessorError("视觉模型返回的 visible_chinese_text 无效。")
    normalized["visible_chinese_text"] = [item.strip() for item in chinese if item.strip()]

    normalized["passed"] = all(normalized[key] for key in REQUIRED_ANALYSIS_FLAGS)
    if normalized["multi_view"] and view_count < 3:
        normalized["multi_view"] = False
        normalized["passed"] = False
        normalized["reasons"].append("有效产品视角少于 3 个。")
    return normalized


def nested(payload: Any, *paths: tuple[Any, ...]) -> Any:
    for path in paths:
        value = payload
        for key in path:
            if isinstance(key, int):
                if not isinstance(value, list) or key < 0 or key >= len(value):
                    value = None
                    break
                value = value[key]
            elif not isinstance(value, dict) or key not in value:
                value = None
                break
            else:
                value = value[key]
        if value is not None:
            return value
    return None
