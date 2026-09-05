from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"},
    "video": {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"},
}


class VideoGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Reference:
    kind: str
    value: str
    local_path: Path | None

    @property
    def source_type(self) -> str:
        return "local_file" if self.local_path is not None else "public_url"

    @property
    def mime_type(self) -> str:
        if self.local_path is None:
            return "application/octet-stream"
        return mimetypes.guess_type(self.local_path.name)[0] or "application/octet-stream"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    api_model: str
    aliases: tuple[str, ...]
    provider: str
    adapter: str
    input_summary: str
    registered: bool = True


@dataclass
class GenerationRequest:
    prompt: str
    requested_model: str
    model: ModelSpec
    references: list[Reference]
    output: Path
    duration: int | None
    aspect_ratio: str | None = None
    resolution: str | None = None
    task_record: Any = None


def infer_kind(value: str) -> tuple[str, str]:
    for kind in EXTENSIONS:
        prefix = f"{kind}="
        if value.lower().startswith(prefix):
            return kind, value[len(prefix) :].strip()
    parsed = urlparse(value)
    candidate = parsed.path if parsed.scheme else value
    suffix = Path(candidate).suffix.lower()
    for kind, extensions in EXTENSIONS.items():
        if suffix in extensions:
            return kind, value
    raise VideoGenerationError(
        f"无法识别参考类型：{value}。请使用 image=、video= 或 audio= 前缀。"
    )


def parse_reference(value: str) -> Reference:
    kind, target = infer_kind(value.strip())
    parsed = urlparse(target)
    is_windows_path = len(target) >= 3 and target[1] == ":" and target[2] in {"\\", "/"}
    if parsed.scheme and not is_windows_path:
        if parsed.scheme != "https" or not parsed.netloc:
            raise VideoGenerationError(f"参考 URL 必须是公网 HTTPS 地址：{target}")
        return Reference(kind, target, None)
    path = Path(target).expanduser().resolve()
    if not path.is_file():
        raise VideoGenerationError(f"参考文件不存在：{path}")
    return Reference(kind, str(path), path)


def default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (Path.cwd() / "output" / f"video_{stamp}.mp4").resolve()


def nested(payload: dict[str, Any], *paths: tuple[Any, ...]) -> Any:
    for path in paths:
        value: Any = payload
        for key in path:
            if isinstance(key, int):
                if not isinstance(value, list) or key >= len(value):
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


def result_url(payload: dict[str, Any]) -> str | None:
    value = nested(
        payload,
        ("video_url",),
        ("media_url",),
        ("result_url",),
        ("data", "video_url"),
        ("data", "media_url"),
        ("data", "result_url"),
        ("result", "video_url"),
        ("result", "url"),
        ("metadata", "url"),
        ("results", 0, "url"),
    )
    return str(value) if value else None
