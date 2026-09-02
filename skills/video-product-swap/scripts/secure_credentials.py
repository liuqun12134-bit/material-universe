from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Mapping


APP_FOLDER = "素材万象"
LEGACY_APP_FOLDER = "AI视频无痕换品工作台"
MAGIC = b"AIVSWAP1\n"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialStoreError(RuntimeError):
    pass


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStoreError("API Key 安全存储只支持 Windows。")
    input_blob, input_buffer = _blob(data)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        ctypes.c_wchar_p("素材万象"),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise CredentialStoreError(f"无法加密 API Key（Windows 错误 {ctypes.get_last_error()}）。")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStoreError("API Key 安全存储只支持 Windows。")
    input_blob, input_buffer = _blob(data)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise CredentialStoreError(f"无法解密 API Key（Windows 错误 {ctypes.get_last_error()}）。")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def default_store_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / APP_FOLDER / "credentials.dat"


def legacy_store_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / LEGACY_APP_FOLDER / "credentials.dat"


class SecureCredentialStore:
    """Small DPAPI-backed store. Secrets can only be decrypted by this Windows user."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_store_path()

    def load(self) -> dict[str, str]:
        if self.path == default_store_path() and not self.path.is_file() and legacy_store_path().is_file():
            legacy_values = SecureCredentialStore(legacy_store_path()).load()
            self.save(legacy_values)
        if not self.path.is_file():
            return {}
        raw = self.path.read_bytes()
        if not raw.startswith(MAGIC):
            raise CredentialStoreError("API Key 存储文件格式无法识别。")
        try:
            payload = json.loads(_unprotect(raw[len(MAGIC) :]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("API Key 存储文件已损坏。") from exc
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ):
            raise CredentialStoreError("API Key 存储内容格式错误。")
        return payload

    def save(self, values: Mapping[str, str]) -> None:
        cleaned = {str(key): str(value).strip() for key, value in values.items() if str(value).strip()}
        plaintext = json.dumps(cleaned, ensure_ascii=False, sort_keys=True).encode("utf-8")
        encrypted = MAGIC + _protect(plaintext)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)

    def clear(self) -> None:
        if self.path.is_file():
            self.path.unlink()


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def redact(text: str, values: Mapping[str, str]) -> str:
    redacted = text
    for key, value in values.items():
        if "KEY" in key.upper() and value:
            redacted = redacted.replace(value, "***")
    return redacted
