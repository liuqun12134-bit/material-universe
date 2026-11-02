import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fish_mcp import FishAuth, FishClient, FishError, FishToolError, unwrap


class FishMcpTests(unittest.TestCase):
    def test_login_pkce_loopback_state_and_encrypted_result(self):
        import base64
        import hashlib
        import urllib.request
        import urllib.error
        from urllib.parse import parse_qs, urlparse, urlencode
        browser_args, callback_status = {}, []
        callback_thread = None
        class Registered:
            status_code = 201
            def json(self): return {"client_id": "registered-client"}
        def browser(url):
            nonlocal callback_thread
            browser_args.update({k: v[0] for k, v in parse_qs(urlparse(url).query).items()})
            def callback():
                redirect = browser_args["redirect_uri"]
                try:
                    urllib.request.urlopen(redirect + "?" + urlencode({"state": "wrong", "code": "bad"}), timeout=3)
                except urllib.error.HTTPError as exc:
                    callback_status.append(exc.code)
                    exc.close()
                with urllib.request.urlopen(redirect + "?" + urlencode({"state": browser_args["state"], "code": "good", "iss": "https://api.fish.audio"}), timeout=3) as response:
                    callback_status.append(response.status)
            callback_thread = threading.Thread(target=callback)
            callback_thread.start()
            return True
        with tempfile.TemporaryDirectory() as folder:
            auth = FishAuth(Path(folder) / "auth.dat")
            with patch("fish_mcp.requests.post", return_value=Registered()) as register, patch.object(FishAuth, "_token", return_value={"access_token": "private-secret", "refresh_token": "refresh", "expires_in": 3600}) as token:
                connection = auth.login(open_browser=browser, timeout=5)
                callback_thread.join()
                self.assertEqual(callback_status, [400, 200])
                token_args = token.call_args.args[0]
                self.assertEqual(token_args["code"], "good")
                expected = base64.urlsafe_b64encode(hashlib.sha256(token_args["code_verifier"].encode()).digest()).decode().rstrip("=")
                self.assertEqual(browser_args["code_challenge"], expected)
                self.assertEqual(browser_args["code_challenge_method"], "S256")
                self.assertEqual(register.call_args.kwargs["json"]["redirect_uris"], [browser_args["redirect_uri"]])
                self.assertTrue(browser_args["redirect_uri"].startswith("http://127.0.0.1:"))
                self.assertEqual(auth.load()["connection_id"], connection)
                self.assertNotIn(b"private-secret", auth.store.path.read_bytes())

    def test_structured_and_text_results(self):
        self.assertEqual(unwrap({"structuredContent": {"status": "estimated"}}), {"status": "estimated"})
        self.assertEqual(unwrap({"content": [{"type": "text", "text": '{"credits": 4}'}]}), {"credits": 4})
        with self.assertRaises(FishToolError): unwrap({"isError": True, "content": [{"type": "text", "text": "bad input"}]})
        with self.assertRaises(FishError): unwrap({"content": []})

    def test_oauth_encrypted_storage_and_refresh_serialization(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oauth.dat"
            auth = FishAuth(path)
            auth.save({"client_id": "test", "access_token": "expired-secret", "refresh_token": "refresh-secret",
                       "expires_at": "0", "connection_id": "one"})
            self.assertNotIn(b"refresh-secret", path.read_bytes())
            returned = []
            with patch.object(FishAuth, "_token", return_value={"access_token": "new-secret", "expires_in": 3600}) as refresh:
                threads = [threading.Thread(target=lambda: returned.append(FishAuth(path).token("one"))) for _ in range(10)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                self.assertEqual(refresh.call_count, 1)
                self.assertEqual(returned, ["new-secret"] * 10)
            with self.assertRaises(FishError): auth.token("different-connection")

    def test_json_and_sse_protocol_initialization(self):
        for sse in (False, True):
            calls = []
            class Response:
                status_code = 200
                headers = {"Content-Type": "text/event-stream" if sse else "application/json", "Mcp-Session-Id": "test-session"}
                def __init__(self, body): self.body = body
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def json(self): return self.body
                def iter_lines(self):
                    yield b": comment"
                    yield b""
                    yield b"data: " + json.dumps({"method": "notifications/progress", "params": {}}).encode()
                    yield b""
                    yield b"data: " + json.dumps(self.body).encode()
                    yield b""
            def post(url, **kwargs):
                calls.append(kwargs)
                body = kwargs["json"]
                result = {"protocolVersion": "2025-06-18"} if body["method"] == "initialize" else {"structuredContent": {"credits": 10}}
                return Response({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
            with patch.object(FishAuth, "load", return_value={"connection_id": "id"}), patch.object(FishAuth, "token", return_value="secret"):
                with FishClient() as client, patch.object(client.session, "post", side_effect=post):
                    self.assertEqual(client.call("estimate_video_generation", {}), {"credits": 10})
                    self.assertEqual(client.call("get_media_model", {}), {"credits": 10})
            self.assertEqual([c["json"]["method"] for c in calls], ["initialize", "notifications/initialized", "tools/call", "tools/call"])
            self.assertEqual(calls[-1]["headers"]["Mcp-Session-Id"], "test-session")

    def test_transport_error_is_never_replayed(self):
        import requests
        with patch.object(FishAuth, "load", return_value={"connection_id": "id"}), patch.object(FishAuth, "token", return_value="secret"):
            with FishClient() as client, patch.object(client.session, "post", side_effect=requests.Timeout("secret")) as post:
                client.initialized = True
                with self.assertRaises(FishError) as error:
                    client.call("generate_video", {})
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(post.call_count, 1)


if __name__ == "__main__": unittest.main()
