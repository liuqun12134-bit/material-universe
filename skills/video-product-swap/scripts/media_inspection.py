from __future__ import annotations

import io
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


STANDARD_RATIOS: tuple[tuple[str, float], ...] = (
    ("9:16", 9 / 16),
    ("16:9", 16 / 9),
    ("1:1", 1.0),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
)


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    aspect_ratio: str

    @property
    def duration_seconds(self) -> int:
        return max(1, int(math.floor(self.duration + 0.5)))

    @property
    def summary(self) -> str:
        return f"{self.duration:.1f} 秒 · {self.width} × {self.height} · {self.aspect_ratio}"


def classify_aspect_ratio(width: int, height: int, tolerance: float = 0.035) -> str:
    if width <= 0 or height <= 0:
        raise ValueError("视频宽高必须大于 0。")
    actual = width / height
    closest_name, closest_value = min(STANDARD_RATIOS, key=lambda item: abs(actual - item[1]))
    relative_error = abs(actual - closest_value) / closest_value
    return closest_name if relative_error <= tolerance else "9:16"


def _run(command: list[str], description: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少 {command[0]}，无法{description}。") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"{description}失败：{detail}")
    return result


def probe_video(path: Path) -> VideoInfo:
    if not path.is_file():
        raise FileNotFoundError(f"找不到视频：{path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("缺少 ffprobe，无法识别视频信息。")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_tags=rotate:stream_side_data=rotation:format=duration",
            "-of",
            "json",
            str(path),
        ],
        "读取视频信息",
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        stream = payload["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(payload["format"]["duration"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法解析视频的时长或宽高。") from exc
    rotation = 0
    try:
        rotation = int(stream.get("tags", {}).get("rotate", 0))
    except (TypeError, ValueError):
        pass
    for side_data in stream.get("side_data_list", []):
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                rotation = int(side_data["rotation"])
            except (TypeError, ValueError):
                pass
    if abs(rotation) % 180 == 90:
        width, height = height, width
    if duration <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("视频时长或宽高无效。")
    return VideoInfo(duration, width, height, classify_aspect_ratio(width, height))


def video_poster(path: Path, size: tuple[int, int]) -> Image.Image:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg，无法生成视频预览。")
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.1",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        "生成视频预览图",
    )
    try:
        image = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    except Exception as exc:
        raise RuntimeError("无法读取视频预览图。") from exc
    return fit_preview(image, size, "#111111")


def image_thumbnail(path: Path, size: tuple[int, int]) -> Image.Image:
    if not path.is_file():
        raise FileNotFoundError(f"找不到图片：{path}")
    try:
        image = Image.open(path).convert("RGB")
    except Exception as exc:
        raise RuntimeError("无法读取产品参考图。") from exc
    return fit_preview(image, size, "#F2F2F7")


def fit_preview(image: Image.Image, size: tuple[int, int], background: str) -> Image.Image:
    fitted = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, background)
    x = (size[0] - fitted.width) // 2
    y = (size[1] - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas
