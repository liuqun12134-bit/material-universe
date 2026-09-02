from __future__ import annotations

from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


REFERENCE_PREFIXES = {"image", "video", "audio"}


def normalize_reference(kind: str, value: str) -> str:
    normalized_kind = kind.strip().lower()
    normalized_value = value.strip()
    if normalized_kind not in REFERENCE_PREFIXES:
        raise ValueError(f"不支持的参考类型：{kind}")
    if not normalized_value:
        raise ValueError("参考文件或 URL 不能为空。")
    return f"{normalized_kind}={normalized_value}"


def build_generate_command(
    python_executable: str,
    script_path: Path,
    *,
    prompt: str,
    model: str,
    references: Iterable[str],
    output: str,
    duration: int | None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    dry_run: bool,
) -> list[str]:
    command = [
        python_executable,
        str(script_path),
        "--prompt",
        prompt,
        "--model",
        model,
    ]
    for reference in references:
        command.extend(["--reference", reference])
    if output:
        command.extend(["--output", output])
    if duration is not None:
        command.extend(["--duration", str(duration)])
    if aspect_ratio:
        command.extend(["--aspect-ratio", aspect_ratio])
    if resolution:
        command.extend(["--resolution", resolution])
    if dry_run:
        command.append("--dry-run")
    return command


def build_prompt_command(
    python_executable: str,
    script_path: Path,
    *,
    video: str,
    reference_image: str,
    volume_relation: str,
    model: str | None = None,
) -> list[str]:
    command = [
        python_executable,
        str(script_path),
        "--video",
        video,
        "--reference-image",
        reference_image,
        "--volume-relation",
        volume_relation,
        "--credential-source",
        "host-app",
        "--json",
    ]
    if model:
        command.extend(["--model", model])
    return command


def references_for_swap(
    adapter: str,
    *,
    source_video: str,
    local_reference_image: str,
    public_reference_url: str = "",
) -> list[str]:
    normalized_adapter = adapter.strip().lower()
    public_url = public_reference_url.strip()
    if normalized_adapter == "omniflash":
        parsed = urlparse(public_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                "OmniFlash 需要参考图的公网 HTTPS 地址；程序不会擅自上传本地参考图。"
            )
        image_reference = public_url
    else:
        image_reference = local_reference_image
    return [
        normalize_reference("video", source_video),
        normalize_reference("image", image_reference),
    ]
