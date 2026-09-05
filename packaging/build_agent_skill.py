"""Build an allowlisted, self-contained Agent Skill ZIP without local secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


PROJECT = Path(__file__).resolve().parents[1]
SKILL_NAME = "video-product-swap"


def build(project: Path, output: Path, version: str, repository: str) -> dict:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Version must have the form 1.0.0")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must have the form owner/repo")
    source = project / "skills" / SKILL_NAME
    files: dict[str, bytes] = {}
    origins: dict[str, dict] = {}

    def include(relative: str, original: Path | None = None) -> None:
        original = original or source / relative
        if original.is_symlink():
            raise ValueError(f"Symlinks are not supported: {relative}")
        data = original.read_bytes().replace(b"\r\n", b"\n")
        files[relative] = data
        origins[relative] = {
            "source": original.relative_to(project).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    for name in (
        "SKILL.md", "agents/openai.yaml", "requirements-agent.txt", ".env.example",
        "references/installation.md", "references/models.json", "references/providers.json",
        "scripts/generate_video.py", "scripts/product_swap.py", "scripts/media_inspection.py",
        "scripts/prompt_engine.py",
    ):
        include(name)
    for original in sorted((source / "scripts/model_runner").rglob("*.py")):
        include(original.relative_to(source).as_posix(), original)

    # Reject accidentally embedded credentials in every distributed text file.
    secret_patterns = (
        rb"\bsk-[A-Za-z0-9_-]{16,}",
        rb"gh[pousr]_[A-Za-z0-9]{20,}",
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    )
    for name, data in files.items():
        if any(re.search(pattern, data) for pattern in secret_patterns):
            raise ValueError(f"Possible credential in {name}; package was not written")
    for line in files[".env.example"].decode("utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip().endswith("_KEY") and value.strip():
                raise ValueError("Example contains a non-empty API key")

    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "skills/video-product-swap", "packaging/build_agent_skill.py"], cwd=project, capture_output=True, text=True, check=True).stdout.strip()
    manifest = {
        "name": SKILL_NAME, "version": version, "repository": repository,
        "source_commit": revision, "source_dirty": bool(dirty),
        "bundled_prompt_engine": True,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        "source_files": origins,
    }
    files["package-manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{SKILL_NAME}-{version}.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            info = ZipInfo(f"{SKILL_NAME}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = output / f"{archive.name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    tag = f"skill-{SKILL_NAME}-v{version}"
    url = f"https://github.com/{repository}/releases/download/{tag}/{archive.name}"
    instruction = (
        f"请安装这个 Skill：{url}\n"
        f"SHA-256：{digest}\n"
        "下载并校验后，将压缩包内的 video-product-swap 文件夹安装到你使用的 Skills 目录，"
        "读取 SKILL.md 和 references/installation.md，安装依赖并完成不付费的安装检查。"
        "需要 API Key 时指导我在本地配置；已有同名 Skill 时先备份并保留 .env。"
        "本次只安装，不生成视频。\n"
    )
    (output / "安装指令.txt").write_text(instruction, encoding="utf-8")
    notes = (
        "视频生成与视频换品 Skill 独立安装包。换品所需的提示词引擎已包含在同一个 Skill 中。\n\n"
        "本版统一桌面与 Agent 换品流程，配置按调用隔离，并增加 --resume 原任务查询与下载恢复。"
        "独立 Skill 默认仅读取自身 .env，旧版依赖系统环境或同级提示词配置的用户需按安装说明迁移。\n\n"
        "支持通用视频生成及 DeepSeek 分析后调用 Wan3 换品；需要能读取 SKILL.md 并执行本地 Python 的 Agent。"
        "Python 3.10+；视频换品需要 FFmpeg/ffprobe；使用者自行配置 DeepSeek、Kaiyuncode 或所选官方线路的凭据。\n\n"
        "包内不含真实 API Key、桌面程序或用户素材。安装检查不发起付费请求。"
        "源码快照及文件校验记录位于包内 package-manifest.json；本发布以 ZIP 内代码为准。\n\n"
        f"### 复制给 Agent\n\n```text\n{instruction}```\n"
    )
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    return {"archive": str(archive), "sha256": digest, "files": len(files), "url": url, "tag": tag}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--output", type=Path, default=PROJECT / "发行版/Agent-Skill")
    parser.add_argument("--version", default="1.2.0")
    parser.add_argument("--repository", default="liuqun12134-bit/material-universe")
    args = parser.parse_args()
    print(json.dumps(build(args.project.resolve(), args.output.resolve(), args.version, args.repository), ensure_ascii=False, indent=2))
