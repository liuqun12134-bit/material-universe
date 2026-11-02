"""Fish's remote MCP client. OAuth belongs to this app, never to a chat host.

One HTTP session per worker; encrypted token refresh is serialized across workers.
No tool call is automatically replayed, especially a generation submission.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from secure_credentials import SecureCredentialStore, default_store_path

ENDPOINT = "https://api.fish.audio/mcp"
ISSUER = "https://api.fish.audio"
PROTOCOL = "2025-06-18"
_auth_lock = threading.RLock()
_login_lock = threading.Lock()


class FishError(RuntimeError):
    pass


class FishToolError(FishError):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(safe_message(payload))


def safe_message(payload):
    """Never persist tokens, signed upload URLs or arbitrary transport exceptions."""
    if isinstance(payload, dict):
        for key in ("message", "error_description", "error", "detail", "instructions"):
            value = payload.get(key)
            if isinstance(value, dict):
                return safe_message(value)
            if isinstance(value, str) and value:
                # Fish errors may contain URLs; keep them out of history/logs.
                import re
                return re.sub(r"https?://\S+", "[服务地址]", value)[:800]
        return "Fish Audio 拒绝了请求，请检查账户权限、积分和素材参数。"
    return "Fish Audio 返回了无法识别的错误。"


def auth_path():
    return default_store_path().with_name("fish-oauth.dat")


class FishAuth:
    def __init__(self, path=None):
        self.store = SecureCredentialStore(Path(path) if path else auth_path())

    def load(self):
        with _auth_lock:
            return self.store.load()

    def connected(self):
        return bool(self.load().get("access_token"))

    def disconnect(self):
        with _auth_lock:
            self.store.clear()

    def save(self, data):
        # Atomic replacement also survives application shutdown during refresh.
        with _auth_lock:
            temporary = self.store.path.with_suffix(".pending")
            SecureCredentialStore(temporary).save({k: str(v) for k, v in data.items()})
            temporary.replace(self.store.path)

    @staticmethod
    def _token(data):
        try:
            r = requests.post(ISSUER + "/oauth/token", data=data, timeout=30, allow_redirects=False)
            if r.status_code != 200:
                raise FishError("Fish Audio 登录已失效，请重新连接账户。")
            result = r.json()
            if not result.get("access_token") or str(result.get("token_type", "Bearer")).lower() != "bearer":
                raise FishError("Fish Audio 授权响应无效。")
            return result
        except (requests.RequestException, ValueError):
            raise FishError("无法完成 Fish Audio 授权，请检查网络后重新连接。") from None

    def token(self, connection_id=None):
        with _auth_lock:
            saved = self.load()
            if connection_id and saved.get("connection_id") != connection_id:
                raise FishError("Fish Audio 连接已切换，请重新预估并确认；已有任务请使用原授权连接。")
            if not saved.get("access_token"):
                raise FishError("请先连接 Fish Audio 账户。")
            if float(saved.get("expires_at", 0)) <= time.time() + 90:
                if not saved.get("refresh_token"):
                    raise FishError("Fish Audio 登录已过期，请重新连接。")
                fresh = self._token({"grant_type": "refresh_token", "client_id": saved["client_id"],
                                     "refresh_token": saved["refresh_token"], "resource": ENDPOINT})
                saved.update(access_token=fresh["access_token"],
                             refresh_token=fresh.get("refresh_token", saved["refresh_token"]),
                             expires_at=str(time.time() + float(fresh.get("expires_in", 3600))))
                self.save(saved)
            return saved["access_token"]

    def login(self, open_browser=webbrowser.open, timeout=240):
        if not _login_lock.acquire(blocking=False):
            raise FishError("已有 Fish Audio 登录窗口，请先完成该次登录。")
        try:
            return self._login(open_browser, timeout)
        finally:
            _login_lock.release()

    def _login(self, open_browser, timeout):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        result = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                args = parse_qs(parsed.query)
                valid = (parsed.path == "/callback" and
                         secrets.compare_digest(args.get("state", [""])[0], state) and
                         args.get("iss", [ISSUER])[0] == ISSUER)
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if valid:
                    result.update(code=args.get("code", [""])[0], error=args.get("error", [""])[0])
                    self.wfile.write("授权流程已结束，请回到素材万象查看连接结果。".encode())
                else:
                    self.wfile.write(b"Invalid authorization callback.")

        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 0.5
            redirect = f"http://127.0.0.1:{server.server_port}/callback"
            try:
                response = requests.post(ISSUER + "/oauth/register", json={
                    "client_name": "素材万象", "redirect_uris": [redirect],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none",
                }, timeout=30, allow_redirects=False)
                if response.status_code not in (200, 201):
                    raise FishError(f"Fish Audio 客户端注册失败（HTTP {response.status_code}）。")
                client_id = response.json()["client_id"]
            except (requests.RequestException, ValueError, KeyError):
                raise FishError("无法连接 Fish Audio 登录服务。") from None
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            url = "https://fish.audio/oauth/authorize?" + urlencode({
                "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
                "scope": "mcp", "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "resource": ENDPOINT,
            })
            if not open_browser(url):
                raise FishError("无法打开默认浏览器，请设置默认浏览器后重试。")
            deadline = time.monotonic() + timeout
            while not result and time.monotonic() < deadline:
                server.handle_request()
            if not result.get("code") or result.get("error"):
                raise FishError("未完成 Fish Audio 授权（取消或超时），请重新连接。")
            tokens = self._token({"grant_type": "authorization_code", "code": result["code"],
                                  "client_id": client_id, "redirect_uri": redirect,
                                  "code_verifier": verifier, "resource": ENDPOINT})
        saved = {"client_id": client_id, "access_token": tokens["access_token"],
                 "refresh_token": tokens.get("refresh_token", ""),
                 "expires_at": str(time.time() + float(tokens.get("expires_in", 3600))),
                 "connection_id": uuid.uuid4().hex}
        self.save(saved)
        return saved["connection_id"]


def unwrap(result):
    if isinstance(result.get("structuredContent"), dict):
        value = result["structuredContent"]
    else:
        value = None
        for block in result.get("content", []):
            if block.get("type") != "text":
                continue
            try:
                value = json.loads(block["text"])
                break
            except (ValueError, KeyError):
                if result.get("isError"):
                    raise FishToolError({"message": block.get("text", "")}) from None
    if not isinstance(value, dict):
        raise FishError("Fish Audio 返回格式无法识别，已停止。")
    if result.get("isError") or value.get("error") or value.get("status") == "error":
        raise FishToolError(value)
    return value


class FishClient:
    def __init__(self, path=None, connection_id=None):
        self.auth = FishAuth(path)
        self.connection_id = connection_id or self.auth.load().get("connection_id")
        self.session = requests.Session()
        self.session_id = None
        self.protocol = PROTOCOL
        self.initialized = False

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _rpc(self, method, params=None, notification=False):
        identity = uuid.uuid4().hex
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = identity
        if params is not None:
            body["params"] = params
        headers = {"Authorization": "Bearer " + self.auth.token(self.connection_id),
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            with self.session.post(ENDPOINT, json=body, headers=headers, timeout=(20, 120),
                                   stream=True, allow_redirects=False) as response:
                if response.status_code in (401, 403):
                    raise FishError("Fish Audio 授权或账户权限不足，请重新连接并检查套餐。")
                if response.status_code not in (200, 202, 204):
                    raise FishError(f"Fish Audio 请求失败（HTTP {response.status_code}），未自动重试。")
                self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
                if notification:
                    return {}
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    data, payload, total = [], None, 0
                    for raw in response.iter_lines():
                        total += len(raw)
                        if total > 8 * 1024 * 1024:
                            raise FishError("Fish Audio 响应过大。")
                        line = raw.decode("utf-8")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            event = json.loads("\n".join(data)); data = []
                            if event.get("id") == identity:
                                payload = event
                                break
                else:
                    payload = response.json()
                if not isinstance(payload, dict) or payload.get("id") != identity:
                    raise FishError("Fish Audio 响应不完整，未自动重试。")
                if payload.get("error"):
                    raise FishToolError(payload["error"])
                return payload["result"]
        except (requests.RequestException, ValueError, KeyError, UnicodeDecodeError):
            raise FishError("Fish Audio 网络中断或响应无效，未自动重试。") from None

    def call(self, name, arguments):
        if not self.initialized:
            info = self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                              "clientInfo": {"name": "MaterialUniverse", "version": "1.9.0"}})
            self.protocol = info.get("protocolVersion", PROTOCOL)
            self._rpc("notifications/initialized", notification=True)
            self.initialized = True
        return unwrap(self._rpc("tools/call", {"name": name, "arguments": arguments}))


def put_media(url, path, content_type):
    if urlparse(url).scheme != "https":
        raise FishError("Fish Audio 上传地址无效。")
    try:
        with Path(path).open("rb") as stream:
            response = requests.put(url, data=stream, headers={"Content-Type": content_type},
                                    timeout=(20, 120), allow_redirects=False)
        if not response.ok:
            raise FishError(f"素材上传失败（HTTP {response.status_code}），尚未提交生成。")
    except requests.RequestException:
        raise FishError("素材上传中断，尚未提交生成。") from None
