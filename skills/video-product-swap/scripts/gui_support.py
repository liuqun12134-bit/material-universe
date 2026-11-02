from __future__ import annotations

from pathlib import Path
from typing import Iterable


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
    """fal 线路统一接受本地素材（适配器自行上传 fal 存储），不再需要公网图。"""
    return [
        normalize_reference("video", source_video),
        normalize_reference("image", local_reference_image),
    ]
