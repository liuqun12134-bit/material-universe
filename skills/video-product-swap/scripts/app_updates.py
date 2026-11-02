"""Download verified stable GitHub releases; never execute downloaded source code."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

from app_version import REPOSITORY, VERSION
from personal_library import atomic_json, collect, digest, library_lock
from library_transfer import copy_skill

API = f"https://api.github.com/repos/{REPOSITORY}"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
MAX_INSTALLER = 3 * 1024 ** 3


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("更新版本号无效。")
    return tuple(map(int, value.split(".")))


def _get(session, url, *, stream=False):
    response = session.get(url, timeout=(15, 90), stream=stream,
                           headers={"User-Agent": f"MaterialUniverse/{VERSION}", "Accept": "application/vnd.github+json"},
                           allow_redirects=True)
    host = urlparse(response.url).hostname if isinstance(response.url, str) else ""
    if host not in {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        response.close()
        raise ValueError("更新下载被重定向到未知站点，已停止。")
    return response


def _json_response(response, max_size=2 * 1024 ** 2):
    response.raise_for_status()
    data = bytearray()
    for chunk in response.iter_content(65536):
        data.extend(chunk)
        if len(data) > max_size:
            raise ValueError("更新信息异常过大。")
    return json.loads(data)


def check_update(current=VERSION, session=None):
    session = session or requests.Session()
    try:
        with _get(session, API + "/releases/latest", stream=True) as response:
            if response.status_code == 404:
                return {"available": False, "message": "尚未发布可下载的正式版本。"}
            release = _json_response(response)
        tag = release.get("tag_name", "")
        version = tag.removeprefix("v")
        if release.get("draft") or release.get("prerelease") or tag != "v" + version:
            raise ValueError("不是可用的正式更新。")
        if version_tuple(version) <= version_tuple(current):
            return {"available": False, "version": version, "message": "当前已是最新版本。"}
        assets = {item["name"]: item for item in release.get("assets", [])}
        manifest_asset = assets.get("update.json")
        manifest_url = f"{RELEASES_URL}/download/{tag}/update.json"
        if not manifest_asset or manifest_asset.get("browser_download_url") != manifest_url:
            raise ValueError("此版本没有经过校验的更新清单，请到发布页查看。")
        with _get(session, manifest_url, stream=True) as response:
            manifest = _json_response(response)
        validate_manifest(manifest, version)
        installer = manifest["installer"]
        asset = assets.get(installer["name"], {})
        url = f"{RELEASES_URL}/download/{tag}/{installer['name']}"
        if asset.get("browser_download_url") != url or asset.get("size") != installer["size"]:
            raise ValueError("发布文件与更新清单不一致。")
        return {"available": True, "version": version, "manifest": manifest, "download_url": url,
                "page": f"{RELEASES_URL}/tag/{tag}", "notes": str(release.get("body") or "")[:6000]}
    except (requests.RequestException, json.JSONDecodeError) as exc:
        raise ValueError("暂时无法读取 GitHub 更新，请检查网络后重试。") from exc


def validate_manifest(manifest, version=None):
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ValueError("不支持此更新清单。")
    actual = manifest.get("version")
    version_tuple(actual)
    if version is not None and version != actual:
        raise ValueError("更新版本与发布标签不一致。")
    item = manifest.get("installer", {})
    if (manifest.get("repository") != REPOSITORY or manifest.get("data_format") != 1
            or item.get("name") != f"material-universe-setup-{actual}.exe"
            or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))
            or type(item.get("size")) is not int or not 0 < item["size"] <= MAX_INSTALLER):
        raise ValueError("更新文件信息或数据兼容版本无效。")
    return manifest


def download_update(update, directory, session=None, progress=lambda _: None):
    manifest = validate_manifest(update["manifest"], update["version"])
    if version_tuple(manifest["version"]) <= version_tuple(VERSION):
        raise ValueError("自动更新不会降级程序。")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    item = manifest["installer"]
    target = directory / item["name"]
    url = f"{RELEASES_URL}/download/v{manifest['version']}/{item['name']}"
    if update.get("download_url") != url:
        raise ValueError("更新来源无效。")
    if target.exists() and target.stat().st_size == item["size"] and digest(target) == item["sha256"]:
        return target
    if shutil.disk_usage(directory).free < item["size"] + 128 * 1024 ** 2:
        raise ValueError("空间不足，无法下载更新。")
    temporary = directory / (item["name"] + "." + uuid.uuid4().hex + ".part")
    try:
        with _get(session or requests.Session(), url, stream=True) as response:
            response.raise_for_status()
            size = 0
            last_percent = -1
            with temporary.open("xb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > item["size"]:
                        raise ValueError("更新文件大小异常。")
                    stream.write(chunk)
                    percent = size * 100 // item["size"]
                    if percent != last_percent:
                        progress(f"正在下载更新… {percent}%"); last_percent = percent
        if size != item["size"] or digest(temporary) != item["sha256"]:
            raise ValueError("更新文件校验失败，请重新下载。")
        os.replace(temporary, target)
        atomic_json(directory / "downloaded-update.json", manifest)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def backup_for_update(transfer, version):
    version_tuple(version)
    store = transfer.store
    with store.lock, library_lock(transfer.root):
        backup = transfer.backups / ("update-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        transfer._database_backup(backup / "history.sqlite3")
        copy_skill(transfer.root, backup / "skill")
        counts = {"assets": len(store.assets()), "jobs": len(store.jobs()),
                  "skills": sum(len(v) for v in collect(transfer.root).values())}
        note = {"from_version": VERSION, "to_version": version, "backup": str(backup),
                "data_root": str(store.root), "library_root": str(transfer.root), "counts": counts,
                "created": time.time(), "checked": False}
        atomic_json(backup / "backup.json", note)
        atomic_json(store.root / "updates" / "pending-update.json", note)
        return note


def verify_after_update(transfer):
    path = transfer.store.root / "updates" / "pending-update.json"
    if not path.exists():
        return ""
    note = json.loads(path.read_text(encoding="utf-8"))
    if note.get("checked") or note.get("to_version") != VERSION:
        return ""
    with transfer.store.lock:
        check = transfer.store.db.execute("PRAGMA quick_check").fetchone()[0]
        counts = {"assets": len(transfer.store.assets()), "jobs": len(transfer.store.jobs()),
                  "skills": sum(len(v) for v in collect(transfer.root).values())}
        if check != "ok" or any(counts[k] < note["counts"][k] for k in counts):
            return "升级后数据检查未通过，请保留当前数据，并使用升级前备份检查恢复：" + note["backup"]
    note["checked"] = True
    atomic_json(path, note)
    return "更新完成，素材、记录和个人 Skill 库已检查。"


def start_installer(installer, manifest, data_root, parent_pid=None):
    """Launch an independent helper, which waits for this process before installing."""
    validate_manifest(manifest)
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise ValueError("源码运行时请使用下载好的安装程序；自动覆盖更新只适用于安装版。")
    install_dir = Path(sys.executable).resolve().parent
    if not (install_dir / "unins000.exe").is_file():
        raise ValueError("当前是便携版，请运行下载的安装程序建立安装版，以后可直接覆盖更新。")
    installer = Path(installer).resolve()
    if installer.stat().st_size != manifest["installer"]["size"] or digest(installer) != manifest["installer"]["sha256"]:
        raise ValueError("更新文件已变化，请重新下载。")
    directory = Path(data_root) / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    from model_runner.runner import SKILL_ROOT
    helper_source = SKILL_ROOT / "update-helper.ps1"
    if not helper_source.is_file():
        raise ValueError("更新助手缺失，请重新下载安装程序。")
    helper = directory / "update-helper.ps1"
    shutil.copy2(helper_source, helper)
    ticket = directory / ("install-" + uuid.uuid4().hex + ".json")
    atomic_json(ticket, {"parent_pid": parent_pid or os.getpid(), "installer": str(installer),
                         "sha256": manifest["installer"]["sha256"], "install_dir": str(install_dir),
                         "version": manifest["version"], "data_root": str(Path(data_root).resolve())})
    powershell = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    return subprocess.Popen([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(helper), "-Ticket", str(ticket)],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
