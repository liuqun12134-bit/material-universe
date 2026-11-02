"""Durable desktop jobs and managed assets. Skills remain usable without this layer."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from argparse import Namespace
from pathlib import Path

from model_runner.core import EXTENSIONS

KINDS = {"full": "视频换品", "image_audio": "图音生视频", "speech": "声音合成", "lipsync": "换口型", "repair": "画质修复", "general": "通用创作"}
ACTIVE = {"queued", "running"}
LABELS = {"queued": "等待执行", "running": "执行中", "success": "成功",
          "failed": "失败", "attention": "需要处理", "paused": "待启动", "cancelled": "已取消"}


def input_paths(config):
    return ([config[k] for k in ("source", "image", "video", "audio", "first_frame", "person_image", "product_image") if config.get(k)]
            + list(config.get("extra_images", [])) + list(config.get("references", []))
            + list(config.get("reference_assets", [])))


def canvas_asset_references(node):
    data = node.get("data", {})
    config = data.get("config", {})
    single = (data.get("assetId"), config.get("sourceAssetId"), config.get("imageAssetId"),
              config.get("videoAssetId"), config.get("audioAssetId"))
    return {value for value in (*single, *(data.get("assetIds") or []),
                                *(config.get("extraImageAssetIds") or []),
                                *(config.get("referenceAssetIds") or [])) if isinstance(value, str)}


class DesktopStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / "素材"
        self.files.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        db_path = self.root / "history.sqlite3"
        existed = db_path.is_file()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        if (existed and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'").fetchone()
                and not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_groups'").fetchone()):
            # SQLite's backup API includes committed WAL pages; copying only the DB file does not.
            backup = self.root / f"history.before-asset-groups-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
            destination = sqlite3.connect(backup)
            try:
                self.db.backup(destination)
            finally:
                destination.close()
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS asset_groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, created REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS hidden_records (path TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS canvases (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS submission_requests (request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, config_hash TEXT NOT NULL)")
        self.db.commit()

    def _put(self, table, data):
        with self.lock, self.db:
            self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)",
                            (data["id"], json.dumps(data, ensure_ascii=False)))

    def jobs(self):
        with self.lock:
            return sorted((json.loads(row[0]) for row in self.db.execute("SELECT body FROM jobs")),
                          key=lambda j: j["created"], reverse=True)

    def import_record(self, path, explicit=False):
        path = Path(path).resolve()
        with self.lock, self.db:
            if explicit:
                self.db.execute("DELETE FROM hidden_records WHERE path=?", (str(path),))
            elif self.db.execute("SELECT path FROM hidden_records WHERE path=?", (str(path),)).fetchone():
                return None
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not state.get("output") or not isinstance(state.get("model"), dict):
            raise ValueError("不是有效的创作任务记录。")
        identity = hashlib.sha256(str(path).encode()).hexdigest()[:32]
        if any(j["id"] == identity or j.get("task_state_file") == str(path) for j in self.jobs()):
            return next(j["id"] for j in self.jobs() if j["id"] == identity or j.get("task_state_file") == str(path))
        adapter = state["model"].get("adapter", "")
        kind = ("general" if isinstance(state.get("general_config"), dict)
                and adapter in {"fal_wan3", "fal_wan3_t2v", "fal_sync3", "fal_flux_schnell", "dashscope_wan3", "kaiyuncode_wan3", "kaiyuncode_seedance", "kaiyuncode_omni", "kaiyuncode_image", "kaiyuncode_omni_edit"}
                else "lipsync" if "sync" in adapter else "repair" if "flashvsr" in adapter
                else "general" if adapter in {"fal_wan3_t2v", "fal_flux_schnell", "kaiyuncode_seedance", "kaiyuncode_omni", "kaiyuncode_image", "kaiyuncode_omni_edit"}
                or (adapter in {"fal_wan3", "dashscope_wan3", "kaiyuncode_wan3"} and Path(state["output"]).name.startswith("general_"))
                else "full")
        if adapter == "fish_seedance":
            kind = "image_audio"
        elif adapter == "fish_speech":
            kind = "speech"
        result = state.get("result", {})
        success = state.get("status") == "completed" and Path(state["output"]).is_file()
        config = {"output": state["output"]}
        if kind == "image_audio":
            config.update(state.get("image_audio_config", {}))
        elif kind == "speech":
            config.update(state.get("speech_config", {}))
            config["output"] = state["output"]
        if kind == "full" and isinstance(state.get("swap_config"), dict):
            config.update(state["swap_config"])
        if kind == "general":
            snapshot = state.get("general_config")
            if isinstance(snapshot, dict):
                references = snapshot.get("references", [])
                reference_ids = snapshot.get("referenceAssetIds", [])
                assets_by_id = {asset["id"]: asset for asset in self.assets()}
                config.update({"model": snapshot.get("model", state["model"].get("id", "")),
                               "provider": snapshot.get("provider", state["model"].get("provider", "fal")),
                               "prompt": snapshot.get("prompt", ""),
                               "reference_assets": references,
                               "parameters": snapshot.get("parameters", {}),
                               "output_kind": snapshot.get("output_kind", "image" if Path(state["output"]).suffix.lower() == ".png" else "video"),
                               "reusable": True})
                if (isinstance(references, list) and isinstance(reference_ids, list)
                        and len(references) == len(reference_ids)
                        and all(assets_by_id.get(identity, {}).get("path") == source
                                for identity, source in zip(reference_ids, references))):
                    config["referenceAssetIds"] = reference_ids
            else:
                # Old task records did not retain the prompt or source paths.
                config.update({"model": state["model"].get("id", ""), "prompt": "",
                               "reference_assets": [],
                               "parameters": {key: state[key] for key in ("duration", "aspect_ratio", "resolution")
                                              if state.get(key) is not None},
                               "output_kind": "image" if Path(state["output"]).suffix.lower() == ".png" else "video",
                               "reusable": False,
                               "reuse_warning": "旧任务记录未保存提示词和参考素材，请新建草稿重新填写。"})
        self._put("jobs", {"id": identity, "kind": kind, "name": Path(state["output"]).name,
            "created": path.stat().st_mtime, "updated": time.time(), "finished": time.time(),
            "status": "success" if success else "attention", "stage": "已导入旧记录", "config": config,
            "asset_ids": list(config.get("referenceAssetIds", [])),
            "task_state_file": str(path), "task_id": state.get("task_id"),
            "result": result, "error": state.get("error", ""),
            "actual_prompt": state.get("actual_prompt", "")})
        return identity

    def job(self, identity):
        with self.lock:
            row = self.db.execute("SELECT body FROM jobs WHERE id=?", (identity,)).fetchone()
            if not row:
                raise ValueError("任务记录不存在。")
            return json.loads(row[0])

    def update(self, identity, **fields):
        with self.lock:
            job = self.job(identity)
            if fields.get("stage") and fields["stage"] != job.get("stage"):
                job["events"] = (job.get("events", []) + [{"time": time.time(), "stage": fields["stage"]}])[-100:]
            job.update(fields, updated=time.time())
            self._put("jobs", job)
            return job

    def assets(self):
        with self.lock:
            return sorted((json.loads(row[0]) for row in self.db.execute("SELECT body FROM assets")),
                          key=lambda a: a["created"], reverse=True)

    def groups(self):
        with self.lock:
            return [{"id": identity, "name": name, "created": created}
                    for identity, name, created in self.db.execute(
                        "SELECT id, name, created FROM asset_groups ORDER BY created, id")]

    @staticmethod
    def _group_name(name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("请输入分组名称。")
        value = name.strip()
        if len(value) > 80:
            raise ValueError("分组名称最多 80 个字符。")
        if value.casefold() in {"全部分组", "未分组"}:
            raise ValueError("此名称用于系统筛选，请换一个分组名称。")
        return value, value.casefold()

    def create_group(self, name):
        value, key = self._group_name(name)
        group = {"id": uuid.uuid4().hex, "name": value, "created": time.time()}
        with self.lock, self.db:
            try:
                self.db.execute("INSERT INTO asset_groups VALUES (?, ?, ?, ?)",
                                (group["id"], value, key, group["created"]))
            except sqlite3.IntegrityError as exc:
                raise ValueError("同名分组已存在。") from exc
        return group

    def rename_group(self, identity, name):
        value, key = self._group_name(name)
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM asset_groups WHERE id=?", (identity,)).fetchone():
                raise ValueError("分组已不存在。")
            try:
                self.db.execute("UPDATE asset_groups SET name=?, name_key=? WHERE id=?",
                                (value, key, identity))
            except sqlite3.IntegrityError as exc:
                raise ValueError("同名分组已存在。") from exc

    def delete_group(self, identity):
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM asset_groups WHERE id=?", (identity,)).fetchone():
                raise ValueError("分组已不存在。")
            for asset_id, body in self.db.execute("SELECT id, body FROM assets"):
                asset = json.loads(body)
                if asset.get("group_id") == identity:
                    asset.pop("group_id", None)
                    self.db.execute("UPDATE assets SET body=? WHERE id=?",
                                    (json.dumps(asset, ensure_ascii=False), asset_id))
            self.db.execute("DELETE FROM asset_groups WHERE id=?", (identity,))

    def move_assets(self, identities, group_id=None):
        ids = list(dict.fromkeys(identities))
        if not ids:
            raise ValueError("请先选择素材。")
        with self.lock, self.db:
            if group_id is not None and not self.db.execute(
                    "SELECT 1 FROM asset_groups WHERE id=?", (group_id,)).fetchone():
                raise ValueError("目标分组已不存在。")
            assets = {}
            for identity in ids:
                row = self.db.execute("SELECT body FROM assets WHERE id=?", (identity,)).fetchone()
                if not row:
                    raise ValueError("所选素材已不存在，请刷新后重试。")
                assets[identity] = json.loads(row[0])
            for identity, asset in assets.items():
                if group_id is None:
                    asset.pop("group_id", None)
                else:
                    asset["group_id"] = group_id
                self.db.execute("UPDATE assets SET body=? WHERE id=?",
                                (json.dumps(asset, ensure_ascii=False), identity))
        return len(ids)

    def import_asset(self, source, group_id=None):
        source = Path(source).resolve()
        kind = next((k for k, suffixes in EXTENSIONS.items() if source.suffix.lower() in suffixes), None)
        if not kind:
            raise ValueError(f"不支持的素材类型：{source.suffix}")
        temporary = self.files / (uuid.uuid4().hex + ".tmp")
        digest = hashlib.sha256()
        try:
            with source.open("rb") as src, temporary.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    digest.update(chunk)
                    dst.write(chunk)
            identity = digest.hexdigest()
            with self.lock, self.db:
                if group_id is not None and not self.db.execute(
                        "SELECT 1 FROM asset_groups WHERE id=?", (group_id,)).fetchone():
                    raise ValueError("导入目标分组已删除，请重新选择分组后导入。")
                old = next((a for a in self.assets() if a["id"] == identity), None)
                target = self.files / (identity + source.suffix.lower())
                if old and Path(old["path"]).is_file():
                    return old
                if old:
                    target = Path(old["path"]).resolve()
                    if target.parent != self.files.resolve():
                        raise ValueError("已有素材路径不在素材库中，无法恢复副本。")
                temporary.replace(target)
                asset = old or {"id": identity, "name": source.name, "kind": kind,
                                "path": str(target), "size": target.stat().st_size, "created": time.time()}
                if not old:
                    if group_id is not None:
                        asset["group_id"] = group_id
                    self.db.execute("INSERT INTO assets VALUES (?, ?)",
                                    (identity, json.dumps(asset, ensure_ascii=False)))
                return asset
        finally:
            temporary.unlink(missing_ok=True)

    def rename_asset(self, identity, name):
        with self.lock:
            asset = next(a for a in self.assets() if a["id"] == identity)
            asset["name"] = name.strip() or asset["name"]
            self._put("assets", asset)

    def delete_asset(self, identity):
        with self.lock:
            if any(j["status"] in ACTIVE for j in self.jobs()):
                raise ValueError("任务正在准备或使用素材，请等任务结束后删除素材。")
            asset = next(a for a in self.assets() if a["id"] == identity)
            if any(identity in j.get("asset_ids", []) or asset["path"] in input_paths(j.get("config", {}))
                   for j in self.jobs()):
                raise ValueError("素材被使用记录引用，请先删除相关记录；删除记录不会删除成片。")
            for (body,) in self.db.execute("SELECT body FROM canvases"):
                canvas = json.loads(body)
                if any(identity in canvas_asset_references(node) for node in canvas.get("nodes", [])):
                    raise ValueError("素材仍被创作画布引用，请先移除相关卡片。")
            path = Path(asset["path"]).resolve()
            if path.parent != self.files.resolve():
                raise ValueError("素材路径不在素材库中。")
            path.unlink(missing_ok=True)
            with self.db:
                self.db.execute("DELETE FROM assets WHERE id=?", (identity,))

    def delete_job(self, identity):
        with self.lock:
            job = self.job(identity)
            if job["status"] in ACTIVE:
                raise ValueError("请先等待任务结束或取消排队。")
            for (body,) in self.db.execute("SELECT body FROM canvases"):
                if any(node.get("data", {}).get("jobId") == identity or
                       (job.get("request_id") and node.get("data", {}).get("requestId") == job["request_id"])
                       for node in json.loads(body).get("nodes", [])):
                    raise ValueError("任务仍被创作画布引用，请先移除相关卡片。")
            with self.db:
                path = job.get("task_state_file") or (job.get("config", {}).get("output", "") + ".video-task.json")
                self.db.execute("INSERT OR IGNORE INTO hidden_records VALUES (?)", (str(Path(path).resolve()),))
                self.db.execute("DELETE FROM jobs WHERE id=?", (identity,))


class TaskManager:
    def __init__(self, store, execute=None, limit=3, changed=None):
        self.store, self.execute = store, execute or execute_job
        self.limit = max(1, min(10, int(limit)))
        self.changed = changed or (lambda: None)
        self.guard = threading.RLock()
        self.pending, self.active = [], set()
        self.workers = set()
        self.refreshing = set()
        self.stopping = False
        # Only one desktop process may dispatch this database's tasks.
        self.owner = (store.root / "desktop.lock").open("a+b")
        self.owner.write(b"0"); self.owner.flush(); self.owner.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise ValueError("素材万象已在运行，请使用已打开的窗口。") from None
        for job in store.jobs():
            if job["status"] in ACTIVE:
                store.update(job["id"], status="paused" if job["status"] == "queued" else "attention",
                             stage="软件已关闭，等待检查并恢复", finished=time.time())

    def set_limit(self, limit):
        with self.guard:
            self.limit = max(1, min(10, int(limit)))
            self._dispatch()

    def submit(self, kind, config, credentials, request_id=None):
        if kind not in KINDS:
            raise ValueError("不支持的任务类型。")
        request_id = str(request_id or uuid.uuid4().hex)
        if not request_id or len(request_id) > 100:
            raise ValueError("请求编号无效。")
        digest = hashlib.sha256(json.dumps([kind, config], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        identity = uuid.uuid4().hex
        config = copy.deepcopy(config)
        # A submission owns its output and input snapshot. Never persist credentials.
        config = {k: v for k, v in config.items() if not k.startswith("_") and not any(
            word in k.lower() for word in ("key", "token", "secret", "credential"))}
        output = Path(config["output"])
        config["output"] = str(output.with_name(output.stem + "_" + identity[:12] + output.suffix))
        job = {"id": identity, "kind": kind, "name": Path(config.get("source") or config.get("video") or "任务").name,
               "config": config, "created": time.time(), "updated": time.time(), "status": "queued",
               "stage": "等待执行", "asset_ids": [], "request_id": request_id}
        source = config.get("source") or config.get("video")
        job["name"] = next((a["name"] for a in self.store.assets() if a["path"] == source), job["name"])
        if kind in {"general", "image_audio"}:
            job["name"] = (config.get("prompt", "").strip().splitlines() or ["通用创作"])[0][:50]
        elif kind == "speech":
            job["name"] = (config.get("text", "").strip().splitlines() or ["声音合成"])[0][:50]
        job["asset_ids"] = [a["id"] for a in self.store.assets() if a["path"] in input_paths(config)]
        with self.guard:
            if self.stopping:
                raise ValueError("程序正在关闭。")
            with self.store.lock, self.store.db:
                existing = self.store.db.execute(
                    "SELECT job_id, config_hash FROM submission_requests WHERE request_id=?", (request_id,)
                ).fetchone()
                if existing:
                    if existing[1] != digest:
                        raise ValueError("同一请求编号的参数已改变，请新建草稿后再次生成。")
                    if not self.store.db.execute("SELECT 1 FROM jobs WHERE id=?", (existing[0],)).fetchone():
                        raise ValueError("原请求记录已删除；请新建草稿，再明确发起一次生成。")
                    return existing[0]
                from feature_policy import require_creation
                require_creation(kind, config)
                self.store.db.execute(
                    "INSERT INTO jobs VALUES (?, ?)", (identity, json.dumps(job, ensure_ascii=False))
                )
                self.store.db.execute(
                    "INSERT INTO submission_requests VALUES (?, ?, ?)", (request_id, identity, digest)
                )
            self.pending.append((identity, dict(credentials), False))
            self._dispatch()
        self.changed()
        return identity

    def recover(self, identity, credentials):
        with self.guard:
            job = self.store.job(identity)
            if identity in self.refreshing:
                raise ValueError("正在同步此任务，请稍后继续。")
            if job["status"] in ACTIVE or identity in self.active:
                raise ValueError("该任务已在执行或排队。")
            record_path = job.get("task_state_file") or (job["config"]["output"] + ".video-task.json")
            record = json.loads(Path(record_path).read_text(encoding="utf-8")) if Path(record_path).is_file() else {}
            has_remote = any(record.get(k) for k in ("task_id", "video_url", "audio_url", "poll_url", "image_base64"))
            local_unsubmitted = job["status"] == "paused" and record.get("status") in (None, "not_submitted")
            if job["kind"] == "speech" and job.get("config", {}).get("variant_backend") == "studio":
                from voice_batch import can_resume
                has_remote = can_resume(job)
                local_unsubmitted = False  # A prepared Studio block is not a submitted take.
            if not has_remote:
                from feature_policy import require_creation
                require_creation(job["kind"], job["config"])
            if not has_remote and not local_unsubmitted:
                raise ValueError("没有可恢复的远程任务。请使用配置再生成；提交结果不明时先到服务商核实。")
            self.store.update(identity, status="queued", stage="等待恢复" if has_remote else "等待执行", finished=None)
            self.pending.append((identity, dict(credentials), has_remote))
            self._dispatch()
        self.changed()

    def cancel(self, identity):
        with self.guard:
            if identity in self.active:
                raise ValueError("任务已经开始执行；取消本地等待不能撤销服务商任务。")
            if self.store.job(identity)["status"] not in {"queued", "paused"}:
                raise ValueError("只有尚未开始的排队任务可以取消。")
            self.pending = [item for item in self.pending if item[0] != identity]
            self.store.update(identity, status="cancelled", stage="已取消排队", finished=time.time())
        self.changed()

    def _dispatch(self):
        while not self.stopping and self.pending and len(self.active) < self.limit:
            identity, credentials, resume = self.pending.pop(0)
            self.active.add(identity)
            self.store.update(identity, status="running", stage="准备恢复" if resume else "准备素材", started=time.time(), finished=None)
            worker = threading.Thread(target=self._run, args=(identity, credentials, resume), daemon=True)
            self.workers.add(worker)
            worker.start()

    def _run(self, identity, credentials, resume):
        def update(**fields):
            # Redact before anything reaches the persistent database or UI.
            text = json.dumps(fields, ensure_ascii=False)
            for key, value in credentials.items():
                if value and any(x in key.upper() for x in ("KEY", "TOKEN", "SECRET")):
                    text = text.replace(str(value), "***")
            self.store.update(identity, **json.loads(text))
            self.changed()
        try:
            job = self.store.job(identity)
            result = self.execute(job, credentials, update, self.store, resume)
            update(status="success" if result.get("local_downloaded") else "attention",
                   stage=("音频已保存" if job["kind"] == "speech" else "成片已保存") if result.get("local_downloaded") else "生成完成，待下载",
                   result=result, finished=time.time(), error="")
        except Exception as exc:
            job = self.store.job(identity)
            record_path = job.get("task_state_file") or (job["config"]["output"] + ".video-task.json")
            state = {}
            try:
                state = json.loads(Path(record_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            attention = state.get("status") in {"submission_unknown", "interrupted", "submitted", "generated", "submitting"}
            update(status="attention" if attention else "failed", error=str(exc),
                   stage="需要核实或继续原任务" if attention else "失败：" + job.get("stage", "执行"), finished=time.time())
        finally:
            with self.guard:
                self.active.discard(identity)
                self.workers.discard(threading.current_thread())
                self._dispatch()
                if self.stopping and not self.active:
                    self.owner.close()
            self.changed()

    def stop(self):
        with self.guard:
            self.stopping = True
            for identity, _, _ in self.pending:
                self.store.update(identity, status="paused", stage="待启动")
            self.pending.clear()
            if not self.active:
                self.owner.close()

    def wait_idle(self, timeout=5):
        """Give local workers time to finish before a caller closes SQLite."""
        deadline = time.monotonic() + timeout
        while True:
            with self.guard:
                workers = list(self.workers)
            if not workers:
                return True
            for worker in workers:
                worker.join(max(0, min(0.2, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                return False


def execute_job(job, credentials, update, store, resume=False):
    from model_runner.runner import ModelRunner
    from product_swap import ProductSwapWorkflow
    from model_runner.task_state import TaskRecord

    config = copy.deepcopy(job["config"])
    if job["kind"] == "speech":
        if config.get("variant_backend") == "studio":
            from voice_pair_service import run_variant
            return run_variant(job, credentials, update, resume=resume)
        from voice_service import run_job
        return run_job(job, credentials, update, resume=resume)
    if job["kind"] == "image_audio":
        from image_audio_video import run_job
        update(actual_prompt=config.get("fish_quote", {}).get("arguments", {}).get("prompt", ""))
        return run_job(job, credentials, update, resume)
    runner = ModelRunner(credential_source="host-app", values=credentials)
    runner.progress = lambda text: update(stage=text)
    runner.task_update = lambda state, path: update(task_state_file=str(path),
                                                   remote_status=state.get("status"), task_id=state.get("task_id"))
    if resume:
        return runner.resume(Path(job.get("task_state_file") or TaskRecord.path_for(Path(config["output"]))))
    if job["kind"] == "general":
        references = []
        asset_ids = []
        original_references = list(config["references"])
        for source in original_references:
            asset = store.import_asset(source)
            references.append(asset["path"])
            asset_ids.append(asset["id"])
            config["references"] = list(references) + original_references[len(references):]
            update(config=config, asset_ids=list(asset_ids))
        from canvas_catalog import runner_options
        params = config["parameters"]
        runner.record_metadata = {"general_config": {
            "model": config["model"], "provider": config.get("provider", runner.registry.resolve(config["model"]).provider), "prompt": config["prompt"],
            "references": list(config["references"]), "parameters": dict(params),
            "referenceAssetIds": list(config.get("referenceAssetIds", [])),
            "output_kind": config["output_kind"]}}
        return runner.run(Namespace(prompt=config["prompt"], provider=config.get("provider", runner.registry.resolve(config["model"]).provider), model=config["model"],
                                    reference=references, output=config["output"],
                                    duration=params.get("duration"), aspect_ratio=params.get("aspect_ratio"),
                                    resolution=params.get("resolution"),
                                    options=({"image_size": params["image_size"]} if config["model"] == "flux-schnell"
                                             else runner_options(config["model"], params)),
                                    dry_run=False))
    asset_ids = []
    for field in ("source", "image", "video", "audio"):
        if config.get(field):
            asset = store.import_asset(config[field])
            config[field] = asset["path"]
            asset_ids.append(asset["id"])
            update(config=config, asset_ids=list(asset_ids))
    images = []
    for source in config.get("extra_images", []):
        asset = store.import_asset(source)
        images.append(asset["path"]); asset_ids.append(asset["id"])
    config["extra_images"] = images
    update(config=config, asset_ids=asset_ids)
    if job["kind"] == "full":
        runner.registry.resolve_for_provider(config.get("provider", runner.registry.resolve(config["model"]).provider), config["model"])
        def prepared(final_prompt, base_prompt):
            config["base_prompt"] = base_prompt
            runner.record_metadata = {"swap_config": dict(config), "actual_prompt": final_prompt}
            update(config=dict(config), actual_prompt=final_prompt)

        return ProductSwapWorkflow(runner).generate(
            config["source"], config["image"], config.get("relation", ""), config["output"],
            model=config["model"], duration=int(config["duration"]), aspect_ratio=config["aspect_ratio"],
            resolution=config["resolution"], vision_model=config["vision_model"], max_source_seconds=None,
            progress=runner.progress, prompt_override=(config.get("base_prompt") or config.get("prompt")) if config.get("prompt_mode") == "reuse" else None,
            prepared_callback=prepared, extra_images=images,
            remove_subtitles=config.get("remove_subtitles", False),
            enable_prompt_expansion=config.get("enable_prompt_expansion", True),
            enable_thinking=config.get("enable_thinking", True))
    if job["kind"] not in {"lipsync", "repair"}:
        raise ValueError("不支持的任务类型。")
    refs = [f"video={config['video']}"]
    if job["kind"] == "lipsync":
        refs.append(f"audio={config['audio']}")
    return runner.run(Namespace(prompt="", provider=None, model="sync3" if job["kind"] == "lipsync" else "flashvsr",
                                reference=refs, output=config["output"], duration=None, aspect_ratio=None,
                                resolution=None, options=config.get("options"), dry_run=False))
