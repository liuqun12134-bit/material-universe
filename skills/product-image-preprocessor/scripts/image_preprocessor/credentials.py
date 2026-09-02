from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .core import Credential, ImagePreprocessorError


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


class CredentialManager:
    def __init__(self, skill_root: Path, providers: dict[str, Any], source: str) -> None:
        if source not in {"skill-env", "host-app"}:
            raise ImagePreprocessorError(f"未知凭据来源：{source}")
        self.providers = providers
        self.source = source
        self.values = dict(os.environ) if source == "host-app" else read_env_file(skill_root / ".env")
        self._resolved: list[Credential] = []

    def resolve(self, provider: str, require_key: bool = True) -> Credential:
        try:
            spec = self.providers[provider]
        except KeyError as exc:
            raise ImagePreprocessorError(f"未登记服务商：{provider}") from exc
        base_name = str(spec["api_base_env"])
        key_name = str(spec["api_key_env"])
        base = (self.values.get(base_name, "").strip() or str(spec["default_api_base"])).rstrip("/")
        key = self.values.get(key_name, "").strip() or None
        if not base.lower().startswith("https://"):
            raise ImagePreprocessorError(f"服务商 {provider} 的 API Base 必须使用 HTTPS。")
        if require_key and not key:
            origin = "宿主应用" if self.source == "host-app" else "Skill 自己的 .env"
            raise ImagePreprocessorError(
                f"{origin}没有配置 {key_name}；不会回退到 Windows、Codex 或其他 Skill 的 Key。"
            )
        credential = Credential(
            provider=provider,
            api_base=base,
            api_key=key,
            api_key_source=key_name if key else None,
            submit_path=str(spec["submit_path"]),
            poll_path=str(spec["poll_path"]) if spec.get("poll_path") else None,
        )
        self._resolved.append(credential)
        return credential

    def redact(self, text: str) -> str:
        for credential in self._resolved:
            if credential.api_key:
                text = text.replace(credential.api_key, "***")
        return text
