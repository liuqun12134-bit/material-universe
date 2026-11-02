"""Local canvas state and authenticated bridge to the one desktop task manager."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from PIL import Image


CANVAS_ID = re.compile(r"^[a-f0-9]{32}$")
MAX_BODY = 2 * 1024 * 1024
MAX_UPLOAD = 1024 * 1024 * 1024

from canvas_catalog import GENERAL_MODELS, ZERO_REFS, available_families, resolve_route, runner_options


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _matches_submitted_general(draft, job):
    """Check a restored card against the original request without re-running current model defaults."""
    if job.get("kind") != "general" or not isinstance(draft, dict):
        return False
    submitted = job.get("config") or {}
    if "canvas_draft" in submitted:
        return draft == submitted["canvas_draft"]
    if any(draft.get(key) != submitted.get(key) for key in ("model", "prompt", "referenceAssetIds")):
        return False
    if draft.get("provider") and draft["provider"] != submitted.get("provider", "fal"):
        return False
    supplied = draft.get("parameters") or {}
    if not isinstance(supplied, dict):
        return False
    defaults = ({"duration": 5, "aspect_ratio": "9:16", "resolution": "480p",
                 "enable_thinking": False} if draft.get("model") in {"wan3.0-video", "wan3.0-t2v"}
                else {"image_size": "landscape_4_3"} if draft.get("model") == "flux-schnell" else {})
    saved = submitted.get("parameters") or {}
    if set(supplied) - (set(saved) | set(defaults)):
        return False
    return all(supplied.get(key, defaults.get(key)) == saved.get(key, defaults.get(key))
               for key in set(saved) | set(supplied))


class CanvasStore:
    def __init__(self, desktop_store, submission_guard=None):
        self.store = desktop_store
        self.submission_guard = submission_guard

    def input_ids(self, nodes, edges, target_id):
        by_id = {node["id"]: node for node in nodes}
        pairs = []
        seen_orders = set()
        for edge in edges:
            data = edge.get("data") or {}
            if not isinstance(data, dict) or data.get("kind") != "input" or edge.get("target") != target_id:
                continue
            source = by_id[edge["source"]]
            target = by_id[target_id]
            order = data.get("order")
            if (source["type"] != "asset" or target["type"] != "task"
                    or target["data"].get("kind") != "general"
                    or data.get("assetId") != source["data"].get("assetId")
                    or type(order) is not int or order < 0 or order in seen_orders):
                raise ValueError("输入连线必须由素材卡指向通用任务，且顺序不能重复。")
            seen_orders.add(order)
            pairs.append((order, data["assetId"]))
        ordered = []
        seen_assets = set()
        for _, asset_id in sorted(pairs):
            if asset_id not in seen_assets:
                ordered.append(asset_id)
                seen_assets.add(asset_id)
        return ordered

    @staticmethod
    def input_signature(edges, target_id):
        return sorted((edge.get("id"), edge.get("source"), edge.get("target"),
                       edge["data"].get("assetId"), edge["data"].get("order"))
                      for edge in edges if (edge.get("data") or {}).get("kind") == "input"
                      and edge.get("target") == target_id)

    def validate_inputs(self, nodes, edges, previous=None, restored_legacy=None):
        by_id = {node["id"]: node for node in nodes}
        assets = {asset["id"]: asset for asset in self.store.assets()}
        for edge in edges:
            data = edge.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError("连线数据格式无效。")
            if data.get("kind") == "input":
                if (set(data) != {"kind", "assetId", "order"}
                        or not isinstance(edge.get("id"), str) or not edge["id"]):
                    raise ValueError("输入连线字段无效。")
                target = by_id[edge["target"]]
                source = by_id[edge["source"]]
                if source["type"] != "asset" or target["type"] != "task" or target["data"].get("kind") != "general":
                    raise ValueError("输入连线只能从素材卡指向通用任务卡。")
                if data.get("assetId") not in assets:
                    raise ValueError("输入连线引用的素材不存在。")
        for node in nodes:
            data = node["data"]
            if node["type"] != "task" or data.get("kind") != "general":
                continue
            config = data.get("config")
            if not isinstance(config, dict) or not isinstance(config.get("model", ""), str):
                raise ValueError("通用任务配置格式无效。")
            if (not isinstance(config.get("prompt", ""), str) or len(config.get("prompt", "")) > 10000
                    or not isinstance(config.get("parameters", {}), dict)):
                raise ValueError("通用任务提示词或参数格式无效。")
            ids = config.get("referenceAssetIds", [])
            if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) for i in ids):
                raise ValueError("参考素材列表无效。")
            from_edges = self.input_ids(nodes, edges, node["id"])
            old = next((item for item in (previous or {}).get("nodes", []) if item["id"] == node["id"]), None)
            old_inputs = [edge for edge in (previous or {}).get("edges", [])
                          if (edge.get("data") or {}).get("kind") == "input" and edge.get("target") == node["id"]]
            legacy_frozen = bool(old and old["data"].get("requestId") and not old_inputs
                                 and old["data"].get("config", {}).get("referenceAssetIds", []) == ids and not from_edges)
            legacy_frozen = legacy_frozen or bool(node["id"] in (restored_legacy or set()) and not from_edges)
            if ids != from_edges or len(ids) != len(set(ids)):
                if not legacy_frozen:
                    raise ValueError("输入连线与参考素材顺序不一致。")
            model_id = config.get("model")
            model = next((m for m in GENERAL_MODELS if m["id"] == model_id), None)
            if any(i not in assets for i in ids) and not legacy_frozen:
                raise ValueError("参考素材不存在。")
            limits = model["referenceLimits"] if model else {}
            if config.get("familyId") and not (data.get("requestId") or data.get("jobId")):
                family, route = resolve_route(config["familyId"], ids, model_id=config.get("model"))
                if config.get("nodeType") != family["nodeType"] or config["model"] != route["modelId"]:
                    raise ValueError("草稿类型或路线与模型目录不一致。")
                limits = family["referenceLimits"]
            if model and not legacy_frozen:
                counts = {kind: sum(assets[i]["kind"] == kind for i in ids) for kind in ZERO_REFS}
                if any(counts[kind] > bounds["max"] for kind, bounds in limits.items()):
                    raise ValueError("参考素材超过所选模型上限。")

    def list(self):
        with self.store.lock:
            rows = [json.loads(row[0]) for row in self.store.db.execute("SELECT body FROM canvases")]
        return sorted(({"id": c["id"], "name": c["name"], "updated": c["updated"]} for c in rows),
                      key=lambda c: c["updated"], reverse=True)

    def create(self, name="新画布"):
        name = str(name).strip()[:80] or "新画布"
        now = time.time()
        canvas = {"id": uuid.uuid4().hex, "name": name, "created": now, "updated": now,
                  "revision": 0, "nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO canvases VALUES (?, ?)", (canvas["id"], _json(canvas).decode()))
        return canvas

    def get(self, identity):
        if not CANVAS_ID.fullmatch(str(identity)):
            raise ValueError("画布编号无效。")
        with self.store.lock:
            row = self.store.db.execute("SELECT body FROM canvases WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("画布不存在。")
        return json.loads(row[0])

    def save(self, identity, payload):
        nodes, edges, viewport = payload.get("nodes"), payload.get("edges"), payload.get("viewport")
        if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(viewport, dict):
            raise ValueError("画布内容格式无效。")
        if len(nodes) > 1000 or len(edges) > 2000:
            raise ValueError("画布卡片或连线过多。")
        allowed = {"asset", "task", "note", "group", "result"}
        ids = set()
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") not in allowed or not isinstance(node.get("data"), dict):
                raise ValueError("卡片格式无效。")
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id or len(node_id) > 100 or node_id in ids:
                raise ValueError("卡片编号无效或重复。")
            ids.add(node_id)
            position = node.get("position")
            if not isinstance(position, dict) or not all(isinstance(position.get(k), (int, float)) for k in ("x", "y")):
                raise ValueError("卡片位置无效。")
        for edge in edges:
            if not isinstance(edge, dict) or edge.get("source") not in ids or edge.get("target") not in ids:
                raise ValueError("来源连线引用了不存在的卡片。")
        if not all(isinstance(viewport.get(k), (int, float)) for k in ("x", "y", "zoom")):
            raise ValueError("画布视角无效。")
        if not 0.05 <= viewport["zoom"] <= 5:
            raise ValueError("画布缩放超出范围。")
        with (self.submission_guard or nullcontext()), self.store.lock, self.store.db:
            canvas = self.get(identity)
            if payload.get("revision") != canvas["revision"]:
                raise RuntimeError("画布已在另一窗口更新，请重新打开后再编辑。")
            old_nodes = {node["id"]: node for node in canvas["nodes"]}
            new_nodes = {node["id"]: node for node in nodes}
            restored_legacy = set()
            for node in nodes:
                if (node["type"] == "task" and node["data"].get("kind") == "general"
                        and node["data"].get("requestId") and node["id"] not in old_nodes):
                    request_id = node["data"]["requestId"]
                    mapping = self.store.db.execute("SELECT job_id FROM submission_requests WHERE request_id=?",
                                                    (request_id,)).fetchone()
                    if not mapping:
                        raise ValueError("请先保存通用任务草稿，再固定请求编号。")
                    if any(old["data"].get("requestId") == request_id for old in old_nodes.values()) or sum(
                            new["type"] == "task" and new["data"].get("requestId") == request_id for new in nodes) != 1:
                        raise ValueError("同一请求编号不能重复放入画布。")
                    job = self.store.job(mapping[0])
                    if (node["data"].get("jobId") not in (None, job["id"])
                            or not _matches_submitted_general(node["data"].get("config"), job)):
                        raise ValueError("恢复卡片与已提交任务的冻结配置不一致。")
                    if "canvas_draft" not in job.get("config", {}):
                        restored_legacy.add(node["id"])
            self.validate_inputs(nodes, edges, canvas, restored_legacy)
            for node_id, old in old_nodes.items():
                old_data = old["data"]
                if old["type"] != "task" or old_data.get("kind") != "general" or not old_data.get("requestId"):
                    continue
                new = new_nodes.get(node_id)
                if new is None:
                    continue  # Removing a card does not cancel its task.
                new_data = new["data"]
                request_id = old_data["requestId"]
                mapped = self.store.db.execute("SELECT job_id FROM submission_requests WHERE request_id=?",
                                               (request_id,)).fetchone()
                same_request = new_data.get("requestId") == request_id
                if mapped and (new["type"] != old["type"] or new_data.get("kind") != "general"
                               or not same_request or new_data.get("config") != old_data.get("config")):
                    raise ValueError("已提交任务配置已冻结，请复制为新草稿。")
                if not mapped and new_data.get("requestId") not in (None, request_id):
                    raise ValueError("请先清除未提交的旧请求编号。")
                if not mapped and same_request and new_data.get("config") != old_data.get("config"):
                    raise ValueError("请先清除未提交的请求编号再修改配置。")
                if mapped or same_request:
                    if self.input_signature(canvas["edges"], node_id) != self.input_signature(edges, node_id):
                        raise ValueError("已固定请求的输入连线不能修改。")
            canvas.update(nodes=nodes, edges=edges, viewport=viewport, updated=time.time(),
                          revision=canvas["revision"] + 1)
            self.store.db.execute("UPDATE canvases SET body=? WHERE id=?", (_json(canvas).decode(), identity))
        return canvas

    def rename(self, identity, name):
        with self.store.lock, self.store.db:
            canvas = self.get(identity)
            canvas["name"] = str(name).strip()[:80] or canvas["name"]
            canvas["updated"] = time.time()
            self.store.db.execute("UPDATE canvases SET body=? WHERE id=?", (_json(canvas).decode(), identity))
        return canvas


class CanvasService:
    def __init__(self, store, tasks, assets_root, credential_provider, models=None, demo=False):
        self.store, self.tasks = store, tasks
        self.canvases = CanvasStore(store, tasks.guard)
        self.assets_root = Path(assets_root).resolve()
        self.credential_provider = credential_provider
        self.models = models or []
        self.general_models = [model for model in GENERAL_MODELS if
            any(item.get("model") == model["id"] and item.get("adapter") == model["adapter"]
                for item in self.models)]
        self.model_families = available_families(self.general_models)
        self.duration_cache = {}
        self.duration_lock = threading.Lock()
        self.demo = demo
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.thread = None
        self.host_action = None
        self.thumb_dir = store.root / "画布封面"
        self.thumb_dir.mkdir(exist_ok=True)
        self.debug_http_log = os.environ.get("MATERIAL_UNIVERSE_CANVAS_DEBUG_HTTP_LOG")
        self.debug_log_lock = threading.Lock()

    def start(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status, body, mime="application/json", extra=None):
                self.response_status = status
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _auth(self):
                supplied = self.headers.get("X-Canvas-Token", "")
                if not supplied and urlsplit(self.path).path.startswith("/media/"):
                    supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
                return secrets.compare_digest(supplied, service.token)

            def _body(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY:
                    raise ValueError("请求内容过大。")
                return json.loads(self.rfile.read(length) or b"{}")

            def _handle(self):
                route = unquote(urlsplit(self.path).path)
                if self.command == "GET" and (route == "/" or route.startswith("/assets/")):
                    relative = "index.html" if route == "/" else route.removeprefix("/")
                    target = (service.assets_root / relative).resolve()
                    if not target.is_file() or not target.is_relative_to(service.assets_root):
                        return self._send(404, b"Not found", "text/plain")
                    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(200, target.read_bytes(), mime)
                if not self._auth():
                    return self._send(403, _json({"error": "本机画布连接未授权。"}))
                if self.command == "GET" and route == "/api/bootstrap":
                    canvases = service.canvases.list()
                    if not canvases:
                        canvases = [service.canvases.create()]
                    return self._send(200, _json({"canvases": canvases, "assets": service.store.assets(),
                                                  "groups": service.store.groups(),
                                                  "jobs": service.store.jobs(), "models": service.models,
                                                  "generalModels": service.general_models,
                                                  "modelFamilies": service.model_families,
                                                  "demo": service.demo}))
                if self.command == "GET" and route == "/api/state":
                    return self._send(200, _json({"assets": service.store.assets(), "groups": service.store.groups(),
                                                  "jobs": service.store.jobs(),
                                                  "canvases": service.canvases.list()}))
                if self.command == "GET" and route.startswith("/api/canvases/"):
                    return self._send(200, _json(service.canvases.get(route.rsplit("/", 1)[-1])))
                if self.command == "POST" and route == "/api/canvases":
                    return self._send(201, _json(service.canvases.create(self._body().get("name", "新画布"))))
                if self.command == "PUT" and route.startswith("/api/canvases/"):
                    return self._send(200, _json(service.canvases.save(route.rsplit("/", 1)[-1], self._body())))
                if self.command == "POST" and route.endswith("/rename") and route.startswith("/api/canvases/"):
                    return self._send(200, _json(service.canvases.rename(route.split("/")[3], self._body().get("name", ""))))
                if self.command == "POST" and route == "/api/assets/import":
                    body = self._body()
                    paths = body.get("paths", [])
                    if not isinstance(paths, list) or len(paths) > 30:
                        raise ValueError("请选择不超过 30 个素材。")
                    group_id = body.get("group_id")
                    if group_id is not None and not isinstance(group_id, str):
                        raise ValueError("导入目标分组无效。")
                    imported = []
                    for path in paths:
                        try:
                            imported.append(service.store.import_asset(path, group_id=group_id))
                        except Exception as exc:
                            raise ValueError(f"{Path(path).name} 导入失败：{exc}；此前成功的 {len(imported)} 个素材已保留在素材库。") from exc
                    return self._send(200, _json(imported))
                if self.command == "POST" and route == "/api/assets/upload":
                    return self._send(201, _json(self._upload()))
                if self.command == "POST" and route == "/api/jobs/estimate":
                    return self._send(200, _json(service.estimate(self._body())))
                if self.command == "POST" and route == "/api/host/action":
                    action = self._body().get("action")
                    if action not in {"fullscreen", "leave_fullscreen"} or not service.host_action:
                        raise ValueError("主窗口操作不可用。")
                    service.host_action(action)
                    return self._send(200, _json({"ok": True}))
                if self.command == "POST" and route == "/api/jobs/validate":
                    return self._send(200, _json({"valid": True, "submitted": False,
                                                  "config": service.validate(self._body())}))
                if self.command == "POST" and route == "/api/jobs":
                    return self._send(200, _json(service.submit(self._body())))
                if self.command == "GET" and route.startswith("/api/jobs/") and route.endswith("/download-info"):
                    path = service.media_path(f"/media/job/{route.split('/')[3]}")
                    return self._send(200, _json({"path": str(path), "name": path.name}))
                if self.command == "POST" and route.startswith("/api/jobs/") and route.endswith("/recover"):
                    return self._send(200, _json(service.recover(route.split("/")[3])))
                if self.command == "POST" and route.startswith("/api/jobs/") and route.endswith("/cancel"):
                    identity = route.split("/")[3]
                    service.tasks.cancel(identity)
                    return self._send(200, _json(service.store.job(identity)))
                if self.command == "POST" and route.startswith("/api/jobs/") and route.endswith("/asset"):
                    return self._send(200, _json(service.result_asset(route.split("/")[3])))
                if self.command == "GET" and route.startswith("/media/"):
                    path = service.media_path(route)
                    return self._media(path, attachment=route.startswith("/media/job/") and
                                       parse_qs(urlsplit(self.path).query).get("download") == ["1"])
                return self._send(404, _json({"error": "接口不存在。"}))

            def _upload(self):
                query = parse_qs(urlsplit(self.path).query)
                name = query.get("name", [""])[0]
                group_id = query.get("group_id", [None])[0]
                name = name.replace("\\", "/").rsplit("/", 1)[-1]
                if not name or len(name) > 255:
                    raise ValueError("上传文件名无效。")
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_UPLOAD:
                    raise ValueError("上传文件为空或超过 1 GB。")
                suffix = Path(name).suffix.lower()
                from model_runner.core import EXTENSIONS
                if not any(suffix in extensions for extensions in EXTENSIONS.values()):
                    raise ValueError("不支持的素材格式。")
                temporary = service.store.root / f".canvas-upload-{uuid.uuid4().hex}{suffix}"
                try:
                    with temporary.open("wb") as target:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("上传内容不完整。")
                            target.write(chunk)
                            remaining -= len(chunk)
                    asset = service.store.import_asset(temporary, group_id=group_id)
                    if asset["name"] == temporary.name:
                        service.store.rename_asset(asset["id"], name)
                        asset["name"] = name
                    return asset
                finally:
                    temporary.unlink(missing_ok=True)

            def _media(self, path, attachment=False):
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                size = path.stat().st_size
                start, end = 0, size - 1
                requested = self.headers.get("Range", "")
                if requested:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                    if not match:
                        return self._send(416, b"", mime, {"Content-Range": f"bytes */{size}"})
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                    if start >= size or end < start:
                        return self._send(416, b"", mime, {"Content-Range": f"bytes */{size}"})
                length = end - start + 1
                self.response_status = 206 if requested else 200
                self.send_response(self.response_status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if attachment:
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
                if requested:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with path.open("rb") as source:
                        source.seek(start)
                        while length:
                            chunk = source.read(min(length, 1024 * 1024))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            length -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                self._guarded()

            def do_POST(self):
                self._guarded()

            def do_PUT(self):
                self._guarded()

            def _guarded(self):
                started = time.monotonic()
                try:
                    self._handle()
                except Exception as exc:
                    from model_runner.core import VideoGenerationError
                    if isinstance(exc, VideoGenerationError):
                        return self._send(400, _json({"error": str(exc), "code": "validation_failed", "submitted": False}))
                    if isinstance(exc, RuntimeError):
                        return self._send(409, _json({"error": str(exc), "code": "conflict", "submitted": False}))
                    if isinstance(exc, (ValueError, KeyError, TypeError, OSError)):
                        return self._send(400, _json({"error": str(exc), "code": "validation_failed", "submitted": False}))
                    raise
                finally:
                    if service.debug_http_log:
                        with service.debug_log_lock, open(service.debug_http_log, "a", encoding="utf-8") as log:
                            log.write(f"{self.command} {urlsplit(self.path).path} {getattr(self, 'response_status', '?')} "
                                      f"{time.monotonic() - started:.3f}s\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/#{self.token}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def _asset(self, identity, kind=None):
        asset = next((item for item in self.store.assets() if item["id"] == identity), None)
        if not asset or (kind and asset["kind"] != kind):
            raise ValueError("所选素材不存在或类型不符。")
        path = Path(asset["path"]).resolve()
        if not path.is_file() or not path.is_relative_to(self.store.files.resolve()):
            raise ValueError("素材文件已丢失。")
        return asset

    def _config(self, kind, incoming, request_id, credentials=None):
        from feature_policy import require_creation
        require_creation(kind, incoming)
        if not isinstance(incoming, dict):
            raise ValueError("任务配置格式无效。")
        if not CANVAS_ID.fullmatch(str(request_id)):
            raise ValueError("生成请求编号无效。")
        snapshot = self.credential_provider() if credentials is None else credentials
        configured = snapshot.get("WORKSPACE_OUTPUT_DIR", "").strip()
        folder = Path(configured).expanduser().resolve() if configured else self.store.root / "画布成片"
        folder.mkdir(parents=True, exist_ok=True)
        if kind == "general":
            return self._general_config(incoming, request_id, folder)
        output = str(folder / f"{kind}_{request_id}.mp4")
        if kind == "full":
            source = self._asset(incoming.get("sourceAssetId"), "video")
            image = self._asset(incoming.get("imageAssetId"), "image")
            extras = incoming.get("extraImageAssetIds", [])
            if not isinstance(extras, list) or len(extras) > 9:
                raise ValueError("额外参考图最多 9 张。")
            relation = str(incoming.get("relation", ""))
            prompt = str(incoming.get("prompt", ""))
            remove_subtitles = incoming.get("removeSubtitles", False)
            if not isinstance(remove_subtitles, bool):
                raise ValueError("去字幕开关格式无效。")
            enable_prompt_expansion = incoming.get("enablePromptExpansion", True)
            enable_thinking = incoming.get("enableThinking", True)
            if type(enable_prompt_expansion) is not bool or type(enable_thinking) is not bool:
                raise ValueError("提示词扩写和思考模式开关格式无效。")
            mode = incoming.get("promptMode", "analyze")
            if mode not in {"analyze", "reuse"} or (mode == "analyze" and not relation.strip()) or (mode == "reuse" and not prompt.strip()):
                raise ValueError("请填写尺寸关系，或选择已有提示词并填写内容。")
            if len(relation) > 300:
                raise ValueError("尺寸关系不能超过 300 字。")
            model = str(incoming.get("model", "wan3.0-video"))
            if model not in {m["model"] for m in self.models if m.get("adapter") in {"fal_wan3", "dashscope_wan3", "kaiyuncode_wan3"}}:
                raise ValueError("请选择可用于换品的模型。")
            model_info = next(m for m in self.models if m["model"] == model)
            provider = str(incoming.get("provider") or model_info["provider"])
            self._resolve_registered(model, provider, model_info["adapter"])
            if provider in {"dashscope", "kaiyuncode"}:
                enable_thinking = False
            raw_duration = incoming.get("duration", 5)
            if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)) or int(raw_duration) != raw_duration:
                raise ValueError("输出时长必须是整数秒。")
            duration = int(raw_duration)
            if not 2 <= duration <= 30:
                raise ValueError("输出时长需为 2 到 30 秒。")
            ratio = str(incoming.get("aspectRatio", "9:16"))
            resolution = str(incoming.get("resolution", "480p"))
            if ratio not in {"9:16", "16:9", "1:1", "4:3", "3:4"} or resolution not in {"480p", "720p", "1080p"}:
                raise ValueError("输出比例或清晰度无效。")
            if model_info["adapter"] == "fal_wan3":
                from model_runner.adapters.fal_wan3 import WAN3_LOCAL_RULES
                for asset in [source, image, *(self._asset(i, "image") for i in extras)]:
                    extensions, max_bytes, _ = WAN3_LOCAL_RULES[asset["kind"]]
                    if Path(asset["path"]).suffix.lower() not in extensions or asset["size"] > max_bytes:
                        raise ValueError(f"{asset['name']} 不符合 Wan3 参考素材的格式或大小限制。")
            config = {"source": source["path"], "image": image["path"], "extra_images": [self._asset(i, "image")["path"] for i in extras],
                    "relation": relation, "prompt": prompt, "base_prompt": prompt if mode == "reuse" else "",
                    "prompt_mode": mode, "remove_subtitles": remove_subtitles,
                    "enable_prompt_expansion": enable_prompt_expansion,
                    "enable_thinking": enable_thinking, "model": model, "provider": provider,
                    "duration": duration, "aspect_ratio": ratio,
                    "resolution": resolution,
                    "vision_model": str(incoming.get("visionModel", "deepseek-v4-flash-vision-exp")), "output": output}
            self._validate_adapter(kind, config)
            return config
        if kind == "lipsync":
            video = self._asset(incoming.get("videoAssetId"), "video")
            audio = self._asset(incoming.get("audioAssetId"), "audio")
            from model_runner.adapters.fal_sync3 import SYNC3_AUDIO_EXTENSIONS, SYNC3_VIDEO_EXTENSIONS
            if Path(video["path"]).suffix.lower() not in SYNC3_VIDEO_EXTENSIONS or Path(audio["path"]).suffix.lower() not in SYNC3_AUDIO_EXTENSIONS:
                raise ValueError("换口型素材格式不支持。")
            config = {"video": video["path"], "audio": audio["path"], "output": output}
            self._validate_adapter(kind, config)
            return config
        if kind == "repair":
            options = incoming.get("options") or {}
            if not isinstance(options, dict):
                raise ValueError("画质参数无效。")
            factor = float(options.get("upscale_factor", 2))
            acceleration = str(options.get("acceleration", "regular"))
            if not 1 <= factor <= 4 or acceleration not in {"regular", "high", "full"}:
                raise ValueError("画质修复参数无效。")
            video = self._asset(incoming.get("videoAssetId"), "video")
            if Path(video["path"]).suffix.lower() not in {".mp4", ".mov", ".webm"}:
                raise ValueError("画质修复视频仅支持 MP4 / MOV / WebM。")
            config = {"video": video["path"],
                    "options": {"upscale_factor": factor,
                                "acceleration": acceleration,
                                "color_fix": options.get("color_fix", True),
                                "preserve_audio": options.get("preserve_audio", True)}, "output": output}
            self._validate_adapter(kind, config)
            return config
        raise ValueError("不支持的任务类型。")

    def _general_config(self, incoming, request_id, folder):
        model_id = incoming.get("model")
        if incoming.get("familyId"):
            family, route = resolve_route(incoming["familyId"], incoming.get("referenceAssetIds", []), self.model_families, model_id=incoming.get("model"))
            if incoming.get("nodeType") != family["nodeType"]:
                raise ValueError("节点类型与创作模型不一致。")
            model_id = route["modelId"]
        model = next((item for item in self.general_models if item["id"] == model_id), None)
        if model is None:
            raise ValueError("请选择画布支持的通用生成模型。")
        provider = str(incoming.get("provider") or model["provider"])
        self._resolve_registered(model_id, provider, model["adapter"])
        prompt = incoming.get("prompt", "")
        if not isinstance(prompt, str) or (model["promptRequired"] and not prompt.strip()) or len(prompt) > 10000:
            raise ValueError("请填写不超过 10000 字的提示词。")
        ids = incoming.get("referenceAssetIds", [])
        if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) for i in ids):
            raise ValueError("参考素材列表无效。")
        assets = [self._asset(identity) for identity in ids]
        counts = {kind: sum(a["kind"] == kind for a in assets) for kind in ZERO_REFS}
        for kind, bounds in model["referenceLimits"].items():
            if not bounds["min"] <= counts[kind] <= bounds["max"]:
                raise ValueError(f"{model['label']} 的{kind}参考数量需要 {bounds['min']}–{bounds['max']} 个。")
        raw_parameters = incoming.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raise ValueError("模型参数格式无效。")
        definitions = {item["key"]: item for item in model["parameters"]}
        if set(raw_parameters) - set(definitions):
            raise ValueError("包含该模型不支持的参数。")
        parameters = {}
        for key, spec in definitions.items():
            value = raw_parameters.get(key, spec["default"])
            if spec["type"] == "integer":
                if isinstance(value, bool) or not isinstance(value, int) or not spec["min"] <= value <= spec["max"]:
                    raise ValueError(f"{spec['label']}不在允许范围内。")
            elif spec["type"] == "boolean":
                if type(value) is not bool:
                    raise ValueError(f"{spec['label']}必须开启或关闭。")
            elif spec["type"] == "string":
                if not isinstance(value, str) or len(value) > 16000:
                    raise ValueError(f"{spec['label']}格式无效或过长。")
            elif value not in spec["enum"]:
                raise ValueError(f"{spec['label']}不在允许选项内。")
            parameters[key] = value
        suffix = ".png" if model["outputKind"] == "image" else ".mp4"
        config = {"model": model_id, "provider": provider, "familyId": model["familyId"], "nodeType": model["nodeType"], "prompt": prompt, "referenceAssetIds": ids,
                  "references": [asset["path"] for asset in assets],
                  "parameters": parameters, "output_kind": model["outputKind"],
                  "output": str(folder / f"general_{request_id}{suffix}")}
        self._validate_general_adapter(model, config)
        return config

    def _media_metadata(self, asset):
        from media_inspection import probe_audio_duration, probe_frame_rate, probe_video
        path = Path(asset["path"])
        stat = path.stat()
        key = (str(path), asset["kind"], stat.st_mtime_ns, stat.st_size)
        with self.duration_lock:
            if key not in self.duration_cache:
                if asset["kind"] == "video":
                    value = {"duration": probe_video(path).duration, "fps": probe_frame_rate(path)}
                else:
                    value = {"duration": probe_audio_duration(path)}
                if len(self.duration_cache) > 512:
                    self.duration_cache.clear()
                self.duration_cache[key] = value
            return self.duration_cache[key]

    def estimate(self, payload):
        """Read-only local quote using the selected endpoint region; no provider requests."""
        import math
        try:
            config = payload.get("config") or {}
            ids = config.get("referenceAssetIds", [])
            if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(value, str) for value in ids):
                raise ValueError("参考素材列表无效。")
            model = next((item for item in self.general_models if item["id"] == config.get("model")), None)
            family_id = config.get("familyId") or (model or {}).get("familyId")
            family, route = resolve_route(family_id, ids, self.model_families, model_id=config.get("model") if model and model.get("familyId") == family_id else None)
            price = route.get("price")
            if not price:
                raise ValueError("该模型价格暂未核实。")
            if family.get("provider") == "dashscope":
                base = str(self.credential_provider().get("DASHSCOPE_API_BASE") or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
                host = urlsplit(base).hostname or ""
                if host != "dashscope.aliyuncs.com" and not host.endswith(".cn-beijing.maas.aliyuncs.com"):
                    raise ValueError("当前阿里云地域价格暂未核实，可继续生成；以服务商实际计费为准。")
            assets = [self._asset(identity) for identity in ids]
            for kind, bounds in family["referenceLimits"].items():
                if sum(asset["kind"] == kind for asset in assets) > bounds["max"]:
                    raise ValueError("参考数量超过模型上限。")
            output_seconds = None
            reference_seconds = 0.0
            if price["durationBasis"] == "shorter_media":
                videos = [asset for asset in assets if asset["kind"] == "video"]
                audios = [asset for asset in assets if asset["kind"] == "audio"]
                if len(videos) != 1 or len(audios) != 1:
                    raise ValueError("接入一个视频和一个音频后显示费用。")
                video_seconds = self._media_metadata(videos[0])["duration"]
                audio_seconds = self._media_metadata(audios[0])["duration"]
                billable = min(video_seconds, audio_seconds)
                rate = price["rate"]
                amount = billable / 60 * rate
            else:
                params = config.get("parameters") or {}
                output_seconds = params.get("duration", 5)
                if type(output_seconds) is not int or not 2 <= output_seconds <= 30:
                    raise ValueError("生成时长须为 2–30 秒的整数。")
                rate = price.get("rateByResolution", {}).get(params.get("resolution", "480p"))
                if rate is None:
                    raise ValueError("该清晰度价格暂未核实。")
                if price["durationBasis"] == "output_plus_reference_video":
                    constraints = route.get("mediaConstraints", {})
                    audio_seconds = 0.0
                    for asset in assets:
                        if asset["kind"] == "video":
                            info = self._media_metadata(asset)
                            minimum_fps = constraints.get("videoMinFps", 0)
                            if info["fps"] < minimum_fps:
                                raise ValueError(f"参考视频帧率至少需要 {minimum_fps}fps。")
                            reference_seconds += info["duration"]
                        elif asset["kind"] == "audio":
                            audio_seconds += self._media_metadata(asset)["duration"]
                    video_limit = constraints.get("videoTotalMaxSeconds", float("inf"))
                    audio_limit = constraints.get("audioTotalMaxSeconds", float("inf"))
                    if reference_seconds > video_limit + 0.001:
                        raise ValueError(f"参考视频总时长不能超过 {video_limit} 秒。")
                    if audio_seconds > audio_limit + 0.001:
                        raise ValueError(f"参考音频总时长不能超过 {audio_limit} 秒。")
                billable = output_seconds + reference_seconds
                amount = billable * rate
            if not math.isfinite(amount) or billable <= 0:
                raise ValueError("无法读取有效计费时长。")
            return {"status": "known", "amount": round(amount, 6), "billableSeconds": billable,
                    "outputSeconds": output_seconds, "referenceVideoSeconds": reference_seconds,
                    "unitRate": rate, "model": route["modelId"], **price}
        except (ValueError, TypeError, RuntimeError, OSError, KeyError) as exc:
            return {"status": "unknown", "reason": str(exc) or "费用信息暂不可用。"}

    def _resolve_registered(self, model_id, provider, adapter):
        from model_runner.registry import ModelRegistry
        from model_runner.runner import SKILL_ROOT
        registered = ModelRegistry(SKILL_ROOT / "references").resolve_for_provider(provider, model_id)
        if not registered.registered or registered.adapter != adapter:
            raise ValueError("模型登记与画布配置不一致。")
        return registered

    def _validate_general_adapter(self, model, config):
        from model_runner.adapters import create_adapter
        from model_runner.core import GenerationRequest, Reference
        registered = self._resolve_registered(model["id"], config["provider"], model["adapter"])
        refs = [Reference(asset["kind"], asset["path"], Path(asset["path"]))
                for identity in config["referenceAssetIds"] for asset in [self._asset(identity)]]
        params = config["parameters"]
        request = GenerationRequest(config["prompt"], model["id"], registered, refs,
                                    Path(config["output"]), params.get("duration"),
                                    params.get("aspect_ratio"), params.get("resolution"),
                                    options=({"strict_media": True} if model["adapter"] == "fal_sync3"
                                             else runner_options(model["id"], params)))
        create_adapter(model["adapter"]).validate(request)

    def _validate_adapter(self, kind, config):
        """Run the same local model validation as paid execution, without a key or request."""
        from model_runner.adapters import create_adapter
        from model_runner.core import GenerationRequest, Reference
        adapter_name = {"full": "fal_wan3", "lipsync": "fal_sync3", "repair": "fal_flashvsr"}[kind]
        model_name = config.get("model") or {"lipsync": "sync_lipsync", "repair": "flashvsr_upscale"}[kind]
        model_info = next((item for item in self.models if item.get("model") == model_name), None)
        if model_info is None:
            # Demo fixtures may supply only the switcher model; other adapters are fixed.
            model_info = {"model": model_name, "adapter": adapter_name, "provider": "fal"}
        adapter_name = model_info["adapter"]
        spec = self._resolve_registered(model_name, config.get("provider") or model_info["provider"], adapter_name)
        refs = []
        for field, media_kind in (("source", "video"), ("image", "image"), ("video", "video"), ("audio", "audio")):
            if config.get(field):
                path = Path(config[field])
                refs.append(Reference(media_kind, str(path), path))
        refs.extend(Reference("image", str(path), Path(path)) for path in config.get("extra_images", []))
        request = GenerationRequest(prompt=config.get("prompt") if config.get("prompt_mode") == "reuse" else "待分析提示词",
                                    requested_model=model_name, model=spec, references=refs,
                                    output=Path(config["output"]), duration=config.get("duration"),
                                    aspect_ratio=config.get("aspect_ratio"), resolution=config.get("resolution"),
                                    options=({"enable_prompt_expansion": config["enable_prompt_expansion"],
                                             **({"enable_thinking": config["enable_thinking"]} if spec.provider == "fal" else {})}
                                             if kind == "full" else config.get("options")))
        create_adapter(adapter_name).validate(request)

    def validate(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("任务配置格式无效。")
        canvas_id = payload.get("canvasId")
        if canvas_id:
            self.canvases.get(canvas_id)
        return self._config(payload.get("kind"), payload.get("config"), payload.get("requestId") or uuid.uuid4().hex)

    def _matches_legacy_draft(self, draft, job):
        if not isinstance(draft, dict):
            return False
        saved = job.get("config") or {}
        if "canvas_draft" in saved:
            return draft == saved["canvas_draft"]
        if job.get("kind") == "general":
            return _matches_submitted_general(draft, job)
        assets = {item["id"]: item["path"] for item in self.store.assets()}
        kind = job.get("kind")
        path_fields = {"full": {"sourceAssetId": "source", "imageAssetId": "image"},
                       "lipsync": {"videoAssetId": "video", "audioAssetId": "audio"},
                       "repair": {"videoAssetId": "video"}}.get(kind)
        if not path_fields or any(assets.get(draft.get(key)) != saved.get(field) for key, field in path_fields.items()):
            return False
        if kind == "lipsync":
            return not (set(draft) - set(path_fields))
        if kind == "repair":
            defaults = {"upscale_factor": 2, "acceleration": "regular", "color_fix": True, "preserve_audio": True}
            return not (set(draft) - {*path_fields, "options"}) and {**defaults, **draft.get("options", {})} == {**defaults, **saved.get("options", {})}
        fields = {"relation": ("relation", ""), "prompt": ("prompt", ""), "promptMode": ("prompt_mode", "analyze"),
                  "removeSubtitles": ("remove_subtitles", False), "enablePromptExpansion": ("enable_prompt_expansion", True),
                  "enableThinking": ("enable_thinking", True), "model": ("model", "wan3.0-video"), "provider": ("provider", "fal"),
                  "duration": ("duration", 5), "aspectRatio": ("aspect_ratio", "9:16"), "resolution": ("resolution", "480p"),
                  "visionModel": ("vision_model", "deepseek-v4-flash-vision-exp")}
        if set(draft) - {*path_fields, *fields, "extraImageAssetIds"}:
            return False
        if [assets.get(identity) for identity in draft.get("extraImageAssetIds", [])] != saved.get("extra_images", []):
            return False
        return all(draft.get(key, default) == saved.get(field, default) for key, (field, default) in fields.items())

    def submit(self, payload):
        with self.tasks.guard:
            canvas = self.canvases.get(payload.get("canvasId"))
            node = next((n for n in canvas["nodes"] if n["id"] == payload.get("nodeId") and n["type"] == "task"), None)
            if not node:
                raise ValueError("请先保存任务卡片。")
            data = node["data"]
            request_id = data.get("requestId")
            if not request_id or request_id != payload.get("requestId"):
                raise ValueError("请先保存本次生成编号。")
            kind = data.get("kind")
            with self.store.lock:
                existing = self.store.db.execute("SELECT job_id FROM submission_requests WHERE request_id=?",
                                                 (request_id,)).fetchone()
            if existing:
                job = self.store.job(existing[0])
                if job.get("kind") != kind or not self._matches_legacy_draft(data.get("config"), job):
                    raise ValueError("同一请求编号的配置已改变，请复制为新草稿。")
                return job
            self.canvases.validate_inputs(canvas["nodes"], canvas["edges"], canvas)
            credentials = self.credential_provider()
            config = self._config(kind, data.get("config") or {}, request_id, credentials)
            if config.get("provider") in {"dashscope", "kaiyuncode"} and not self.demo:
                from model_runner.credentials import CredentialManager
                from model_runner.registry import ModelRegistry
                from model_runner.runner import SKILL_ROOT
                registry = ModelRegistry(SKILL_ROOT / "references")
                credential = CredentialManager(SKILL_ROOT, registry.providers,
                                               values=credentials).resolve(config["provider"])
                from model_runner.adapters import create_adapter
                create_adapter(registry.resolve(config["model"]).adapter).validate_credential(credential)
            if kind == "general":
                quote = self.estimate({"config": {**(data.get("config") or {}), "model": config["model"]}})
                model = next(item for item in self.general_models if item["id"] == config["model"])
                if quote["status"] != "known" and not model.get("pricingUnverified"):
                    raise ValueError(f"生成前无法估算费用：{quote['reason']}")
                config["cost_estimate"] = {**quote, "estimatedAt": time.time()}
            config["canvas_draft"] = json.loads(json.dumps(data.get("config") or {}, ensure_ascii=False))
            identity = self.tasks.submit(kind, config, credentials, request_id=request_id)
            return self.store.job(identity)

    def recover(self, identity):
        job = self.store.job(identity)
        path = Path(job.get("task_state_file") or (job["config"]["output"] + ".video-task.json"))
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        if not any(record.get(k) for k in ("task_id", "video_url", "poll_url", "image_base64")) and not (
            job.get("status") == "paused" and record.get("status") in (None, "not_submitted")
        ):
            raise ValueError("没有可查询或下载的原任务编号；提交结果不明时请先向服务商核实。")
        self.tasks.recover(identity, self.credential_provider())
        return self.store.job(identity)

    def result_asset(self, identity):
        job = self.store.job(identity)
        path = Path(job.get("result", {}).get("output") or "")
        if not path.is_file() or job.get("status") != "success":
            raise ValueError("成片尚未保存，无法作为下游素材。")
        return self.store.import_asset(path)

    def media_path(self, route):
        parts = route.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "media":
            raise ValueError("媒体地址无效。")
        _, kind, identity = parts
        if kind == "asset":
            return Path(self._asset(identity)["path"])
        if kind == "job":
            job = self.store.job(identity)
            path = Path(job.get("result", {}).get("output") or "").resolve()
            if not path.is_file() or job.get("status") != "success" or path.suffix.lower() not in {".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("本地成片尚未保存。")
            return path
        if kind == "thumb":
            asset = self._asset(identity)
            source = Path(asset["path"])
            if asset["kind"] == "image":
                target = self.thumb_dir / f"{identity}.jpg"
                if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
                    with Image.open(source) as image:
                        image.thumbnail((480, 320))
                        image.convert("RGB").save(target, "JPEG", quality=83)
                return target
            if asset["kind"] != "video":
                raise ValueError("该素材没有封面。")
            target = self.thumb_dir / f"{identity}.jpg"
            if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(source),
                                "-frames:v", "1", "-vf", "scale=480:-2", str(target)],
                               timeout=20, check=True, capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return target
        if kind == "job-thumb":
            source = self.media_path(f"/media/job/{identity}")
            target = self.thumb_dir / f"job-{identity}.jpg"
            if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
                if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    with Image.open(source) as image:
                        image.thumbnail((480, 320))
                        image.convert("RGB").save(target, "JPEG", quality=83)
                else:
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(source),
                                    "-frames:v", "1", "-vf", "scale=480:-2", str(target)],
                                   timeout=20, check=True, capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return target
        raise ValueError("媒体类型无效。")
