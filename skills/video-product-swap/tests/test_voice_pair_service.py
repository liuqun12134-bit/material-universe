import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import voice_pair_service as pair
from fish_mcp import FishError
from model_runner.task_state import TaskRecord


class StudioClient:
    connection_id = "app-connection"

    def __init__(self):
        self.calls = []
        self.backend = "s2.1-pro"
        self.blocks = []
        self.price = 6
        self.balance = 100
        self.paid = []
        self.lose_response = False
        self.remaining = False

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "studio_help": return {"skill": "documented workflow"}
        if name == "studio_create_project": return {"project_id": "project"}
        if name == "studio_update_project":
            self.backend = args["default_backend"]
            return {"status": "ok"}
        if name == "studio_create_chapter": return {"anchor": "chapter"}
        if name == "studio_write_blocks":
            for index, op in enumerate(args["ops"], 1):
                self.blocks.append({"anchor": str(index), "rev": "rev" + str(index), "text": op["text"],
                                    "voice": op["voice"], "backend": self.backend, "dirty": True,
                                    "audio_settings": copy.deepcopy(op["audio_settings"])})
            return {"results": [{"status": "ok", "anchor": b["anchor"], "rev": b["rev"]}
                                for b in self.blocks]}
        if name == "studio_read_blocks":
            return {"blocks": [copy.deepcopy(b) for b in self.blocks
                               if not args.get("blocks") or b["anchor"] in args["blocks"]]}
        if name == "studio_generate":
            if args["dry_run"]:
                return {"dry_run": True, "credits": self.price, "blocks_to_generate": 1}
            self.paid.append(copy.deepcopy(args))
            block = next(b for b in self.blocks if b["anchor"] == args["blocks"][0])
            if not self.remaining:
                block.update(audio_url="https://example.test/take-" + block["anchor"] + ".mp3", dirty=False)
            if self.lose_response:
                raise FishError("生成响应中断")
            return {"remaining_anchors": args["blocks"] if self.remaining else [], "credits_charged": self.price}
        if name == "get_credit_balance":
            return {"package_balance": self.balance, "package_extra_balance": 0}
        raise AssertionError("Unexpected tool: " + name)


def finish_download(record, progress):
    output = Path(record.data["output"])
    output.write_bytes(record.data["audio_url"].encode())
    return {"success": True, "output_kind": "audio", "local_downloaded": True,
            "output": str(output), "audio_url": record.data["audio_url"], "duration": 2.0}


class VoicePairServiceTests(unittest.TestCase):
    def config(self, directory):
        return {"text": "你好", "voice_id": "my-voice", "voice_name": "我的声音", "speed": 1., "volume_db": 0.,
                "output": str(Path(directory) / "speech.wav")}

    def prepare(self, directory, client):
        with patch.object(pair, "_media_tool", return_value="ffmpeg"):
            result = pair.prepare_pair(self.config(directory), client)
        result["cost_approved"] = True
        for variant in result["variants"]:
            variant["cost_approved"] = True
        return result

    def test_actual_free_studio_protocol_fixture_resolves_names_and_exact_quotes(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/fish_studio_pair_protocol.json").read_text(encoding="utf-8"))
        client = StudioClient()
        responses = {"studio_create_project": "created", "studio_update_project": "updated",
                     "studio_create_chapter": "chapter", "studio_write_blocks": "written",
                     "studio_read_blocks": "read", "studio_project": "project",
                     "studio_generate": "quote", "get_credit_balance": "balance"}
        calls = []
        def call(name, args):
            calls.append((name, args))
            if name == "studio_help": return {"skill": "manual"}
            if name == "studio_generate": self.assertTrue(args["dry_run"])
            return copy.deepcopy(fixture[responses[name]])
        client.call = call
        with tempfile.TemporaryDirectory() as directory, patch.object(pair, "_media_tool", return_value="ffmpeg"):
            config = {**self.config(directory), "text": "这是一段双版本接口验证文案。",
                      "voice_id": "fixture-voice", "voice_name": "示例音色"}
            result = pair.prepare_pair(config, client)
        self.assertEqual(result["fish_quote"]["credits"], 84)
        self.assertEqual(result["fish_quote"]["version_credits"], [42, 42])
        self.assertEqual(result["fish_quote"]["balance"], 1000)
        self.assertEqual([v["studio_block"] for v in result["variants"]], ["block-a", "block-b"])
        self.assertTrue(any(name == "studio_project" for name, _ in calls))
        # A duplicated display name must not silently choose the other voice.
        fixture["project"]["roster"].append({"name": "示例音色", "voice_id": "wrong-voice"})
        with tempfile.TemporaryDirectory() as directory, patch.object(pair, "_media_tool", return_value="ffmpeg"):
            with self.assertRaisesRegex(FishError, "唯一"):
                pair.prepare_pair({**config, "output": str(Path(directory) / "out.wav")}, client)

    def test_precise_two_variant_quotes_preserve_text_voice_and_set_drama_before_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient()
            result = self.prepare(directory, client)
            self.assertEqual(result["fish_quote"]["credits"], 12)
            self.assertEqual(result["fish_quote"]["version_credits"], [6, 6])
            self.assertFalse(client.paid)
            self.assertEqual([b["backend"] for b in client.blocks], ["drama-3", "drama-3"])
            self.assertEqual([b["text"] for b in client.blocks], ["你好", "你好"])
            self.assertEqual([b["voice"] for b in client.blocks], ["my-voice", "my-voice"])
            variants = result["variants"]
            self.assertNotEqual(variants[0]["studio_block"], variants[1]["studio_block"])
            self.assertNotEqual(variants[0]["output"], variants[1]["output"])
            self.assertTrue(all(v["fish_quote"]["arguments"]["force"] for v in variants))
            self.assertEqual(client.calls[0][0], "studio_help")

    def test_nondefault_speed_and_decibels_are_sent_to_each_fish_block(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pair, "_media_tool", return_value="ffmpeg"):
            client = StudioClient()
            result = pair.prepare_pair({**self.config(directory), "speed": .9, "volume_db": -3}, client)
            settings = {"speed": .9, "volume": -3}
            update = next(args for name, args in client.calls if name == "studio_update_project")
            self.assertEqual(update["audio_settings"], settings)
            self.assertEqual([b["audio_settings"] for b in client.blocks], [settings, settings])
            for variant in result["variants"]:
                self.assertEqual(variant["studio_audio_settings"], settings)
                self.assertEqual(variant["fish_quote"]["studio_audio_settings"], settings)
                self.assertEqual(variant["audio_settings_mode"], "fish")
                variant["cost_approved"] = True
                self.assertEqual(pair.validate_variant(variant)["volume_db"], -3)
            self.assertFalse(client.paid)

    def test_settings_write_not_acknowledged_or_changed_before_quote_stops(self):
        for change in ("missing_ack", "changed_revision"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                client = StudioClient()
                original = client.call
                def changed(name, args):
                    response = original(name, args)
                    if name == "studio_write_blocks":
                        if change == "missing_ack":
                            return {"results": []}
                        client.blocks[0]["rev"] = "settings-changed"
                    return response
                client.call = changed
                with patch.object(pair, "_media_tool", return_value="ffmpeg"), self.assertRaises(FishError):
                    pair.prepare_pair(self.config(directory), client)
                self.assertFalse(any(name == "studio_generate" for name, _ in client.calls))

    def test_quote_requires_balance_for_both_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient(); client.balance = 10
            with self.assertRaisesRegex(FishError, "两个版本"):
                self.prepare(directory, client)
            self.assertFalse(client.paid)

    def test_unknown_quote_shape_stops_before_paid_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient()
            original = client.call
            def changed(name, args):
                if name == "studio_generate":
                    return {"unexpected_price": 6}
                return original(name, args)
            client.call = changed
            with self.assertRaisesRegex(FishError, "报价"):
                self.prepare(directory, client)
            self.assertFalse(client.paid)

    def test_two_independent_forced_takes_are_saved_with_separate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient()
            variants = self.prepare(directory, client)["variants"]
            results = []
            with patch.object(pair, "_media_tool", return_value="ffmpeg"), patch.object(pair, "download_result", side_effect=finish_download):
                for variant in variants:
                    results.append(pair.run_variant({"config": variant}, {}, lambda **_: None,
                        client_factory=lambda *_: client, max_wait=0))
            self.assertEqual(len(client.paid), 2)
            self.assertTrue(all(args["force"] and not args["only_dirty"] for args in client.paid))
            self.assertEqual([args["blocks"] for args in client.paid], [["1"], ["2"]])
            self.assertNotEqual(results[0]["audio_url"], results[1]["audio_url"])
            self.assertNotEqual(results[0]["task_state_file"], results[1]["task_state_file"])
            self.assertTrue(all(r["local_downloaded"] for r in results))

    def test_changed_remote_draft_or_price_never_generates(self):
        for change in ("revision", "text", "price", "settings"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                client = StudioClient()
                config = self.prepare(directory, client)["variants"][0]
                if change == "revision": client.blocks[0]["rev"] = "changed"
                if change == "text": client.blocks[0]["text"] = "改动文案"
                if change == "price": client.price = 9
                if change == "settings":
                    client.blocks[0]["audio_settings"]["volume"] = 3
                    client.blocks[0]["rev"] = "audio-settings-changed"
                with patch.object(pair, "_media_tool", return_value="ffmpeg"), self.assertRaises(FishError):
                    pair.run_variant({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client)
                self.assertFalse(client.paid)
                state = json.loads(TaskRecord.path_for(Path(config["output"])).read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "not_submitted")

    def test_changed_local_controls_invalidate_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepare(directory, StudioClient())["variants"][0]
            for key, value in (("text", "改了"), ("voice_id", "other"), ("speed", 1.2), ("volume_db", -3),
                               ("studio_audio_settings", {"speed": 1, "volume": 4}), ("studio_block", "other")):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    pair.validate_variant({**config, key: value})

    def test_lost_generation_response_recovers_by_reading_block_only(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient()
            config = self.prepare(directory, client)["variants"][0]
            client.lose_response = True
            with patch.object(pair, "_media_tool", return_value="ffmpeg"), self.assertRaisesRegex(FishError, "中断"):
                pair.run_variant({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client)
            self.assertEqual(len(client.paid), 1)
            state = json.loads(TaskRecord.path_for(Path(config["output"])).read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "submission_unknown")
            self.assertEqual(state["studio_block"], "1")
            start = len(client.calls)
            with patch.object(pair, "download_result", side_effect=finish_download):
                result = pair.run_variant({"config": config}, {}, lambda **_: None, resume=True,
                                          client_factory=lambda *_: client, max_wait=0)
            self.assertTrue(result["local_downloaded"])
            self.assertFalse(any(name == "studio_generate" for name, _ in client.calls[start:]))
            self.assertEqual(len(client.paid), 1)

    def test_remaining_blocks_are_never_submitted_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient()
            config = self.prepare(directory, client)["variants"][0]
            client.remaining = True
            with patch.object(pair, "_media_tool", return_value="ffmpeg"), self.assertRaisesRegex(FishError, "等待"):
                pair.run_variant({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client, max_wait=0)
            with self.assertRaisesRegex(FishError, "等待"):
                pair.run_variant({"config": config}, {}, lambda **_: None, resume=True,
                                 client_factory=lambda *_: client, max_wait=0)
            self.assertEqual(len(client.paid), 1)

    def test_existing_audio_download_resumes_without_server_or_paid_call(self):
        with tempfile.TemporaryDirectory() as directory:
            client = StudioClient()
            config = self.prepare(directory, client)["variants"][0]
            with patch.object(pair, "_media_tool", return_value="ffmpeg"), patch.object(pair, "download_result",
                    return_value={"success": True, "output": None, "local_downloaded": False}):
                result = pair.run_variant({"config": config}, {}, lambda **_: None, client_factory=lambda *_: client)
            self.assertFalse(result["local_downloaded"])
            with patch.object(pair, "download_result", side_effect=finish_download):
                result = pair.run_variant({"config": config}, {}, lambda **_: None, resume=True,
                    client_factory=lambda *_: self.fail("Downloaded URL must not reauthorize or generate"))
            self.assertTrue(result["local_downloaded"])
            self.assertEqual(len(client.paid), 1)


if __name__ == "__main__":
    unittest.main()
