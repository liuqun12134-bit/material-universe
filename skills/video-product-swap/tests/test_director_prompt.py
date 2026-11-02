"""Offline regressions for speech mentions versus required visuals."""
import copy
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import director_prompt as director


def issue(kind, message="人参霜只在口播中提及", evidence="首帧仅有人参油", resolution="保留原声，继续使用人参油"):
    return dict(kind=kind, message=message, evidence=evidence, resolution=resolution)


class KaiyunRoutingTests(unittest.TestCase):
    def test_normalizes_base_and_ignores_legacy_deepseek_credentials(self):
        for base in ("https://kaiyuncode.com", "https://kaiyuncode.com/", "https://kaiyuncode.com/v1/"):
            credentials = {"KAIYUNCODE_API_BASE": base, "KAIYUNCODE_API_KEY": "kaiyun-test",
                           "DEEPSEEK_API_BASE": "https://old.test", "DEEPSEEK_API_KEY": "old-test",
                           "DEEPSEEK_PROMPT_MODEL": "old-model"}
            self.assertEqual(director.configuration(credentials),
                             ("https://kaiyuncode.com/v1", "kaiyun-test", "gpt-6.1-sol"))

    def test_missing_kaiyun_key_never_falls_back_or_sends_request(self):
        with patch.object(director.requests, "post") as post:
            with self.assertRaisesRegex(ValueError, "Kaiyun"):
                director.ask({"DEEPSEEK_API_KEY": "old-test"}, "JSON", [])
            post.assert_not_called()

    def test_request_preserves_images_and_uses_kaiyun_model_and_key(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"prompt":"ok"}'}}]}
        content = [{"type": "text", "text": "JSON"},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,test"}}]
        with patch.object(director.requests, "post", return_value=response) as post:
            self.assertEqual(director.ask({"KAIYUNCODE_API_KEY": "kaiyun-test"}, "JSON", content), {"prompt": "ok"})
            post.assert_called_once()
            self.assertEqual(post.call_args.args[0], "https://kaiyuncode.com/v1/chat/completions")
            args = post.call_args.kwargs
            self.assertEqual(args["headers"]["Authorization"], "Bearer kaiyun-test")
            self.assertEqual(args["json"]["model"], "gpt-6.1-sol")
            self.assertEqual(args["json"]["reasoning_effort"], "medium")
            self.assertEqual(args["json"]["messages"][1]["content"], content)
            self.assertFalse(args["allow_redirects"])

    def test_service_failure_does_not_retry(self):
        with patch.object(director.requests, "post", return_value=Mock(status_code=403)) as post:
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                director.ask({"KAIYUNCODE_API_KEY": "kaiyun-test"}, "JSON", [])
            post.assert_called_once()


class DirectorPromptTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("No paid calls")))
        media = {field: {"path": field, "sha256": "same", "duration": 30} for field in director.MEDIA_FIELDS}
        self.snapshot = {"mode": "边讲边用", "requirements": "只展示人参油", "prompt": "", "media": media}
        self.credentials = {"KAIYUNCODE_API_BASE": "https://example.test", "KAIYUNCODE_API_KEY": "test"}
        self.stack.enter_context(patch.object(director, "inspect_image", side_effect=lambda path, field: media[field]))
        self.stack.enter_context(patch.object(director, "inspect_audio", return_value=media["audio"]))
        self.stack.enter_context(patch.object(director, "digest_file", return_value="same"))
        self.stack.enter_context(patch.object(director, "image_part", return_value={"type": "image_url"}))
        self.stack.enter_context(patch.object(director, "transcribe", return_value="这瓶人参油可以搭配人参霜，四瓶不过百，双十一活动，滋润保湿。"))
        self.ask = self.stack.enter_context(patch.object(director, "ask"))
        self.analysis = {"observations": "一瓶油；产品参考是同瓶四个角度。",
                         "visual_plan": "只展示并使用首帧一瓶油；面霜、四瓶优惠、活动只保留原声。",
                         "issues": [], "selected_ids": []}
        self.draft = {"issues": [], "prompt": "按 @图1 构图，@图2 人物、@图3 产品，保持 @音频1 完整。只展示并使用一瓶油。"}

    def run_generate(self):
        self.ask.side_effect = [self.analysis, self.draft]
        return director.generate(self.snapshot, self.credentials, None, None, lambda _: None)

    def test_incidental_product_quantity_promotion_and_claim_do_not_block(self):
        self.analysis["issues"] = [
            issue("speech_only"),
            issue("speech_only", "四瓶优惠仅口播", "首帧一瓶，参考图四个角度", "维持首帧一瓶"),
            issue("optional_visual", "缺活动截图", "用户未要求活动画面", "不新增价格牌"),
            issue("unverified_claim", "图片不能核实功效", "只有包装参考", "保留原声，不编造功效演示"),
        ]
        self.draft["issues"] = [copy.deepcopy(self.analysis["issues"][0])]
        result = self.run_generate()
        self.assertEqual(result["prompt"], self.draft["prompt"])
        self.assertEqual(len(result["issues"]), 4)
        self.assertEqual(result["visual_plan"], self.analysis["visual_plan"])
        self.assertEqual(self.ask.call_count, 2)
        # Both stages must receive the same boundary and carry forward the plan.
        for call in self.ask.call_args_list:
            self.assertIn(director.VISUAL_SCOPE_RULES, call.args[1])
            self.assertIn(director.ISSUE_SCHEMA, call.args[1])
        self.assertIn(self.analysis["visual_plan"], self.ask.call_args_list[1].args[2][0]["text"])

    def test_real_visual_conflicts_stop_before_drafting(self):
        for kind, message in (
            ("visual_conflict", "首帧是滴管瓶，产品参考是另一款泵瓶"),
            ("missing_required_visual", "用户明确要求展示面霜，但未提供面霜参考"),
            ("core_use_impossible", "指定取用动作依赖的结构无法辨认"),
        ):
            with self.subTest(kind=kind):
                self.ask.reset_mock()
                self.analysis["issues"] = [issue(kind, message, "具体画面或用户要求", "补充对应参考")]
                with self.assertRaisesRegex(ValueError, message):
                    self.run_generate()
                self.assertEqual(self.ask.call_count, 1)

    def test_drafting_uses_the_same_blocking_boundary(self):
        self.draft["issues"] = [issue("missing_required_visual", "必须展示面霜", "用户明确要求同时举起面霜", "补充面霜参考")]
        with self.assertRaisesRegex(ValueError, "必须展示面霜"):
            self.run_generate()
        self.assertEqual(self.ask.call_count, 2)

    def test_advice_discovered_during_drafting_is_returned(self):
        self.draft["issues"] = [issue("unverified_claim")]
        result = self.run_generate()
        self.assertEqual(len(result["issues"]), 1)
        self.assertTrue(result["prompt"])

    def test_malformed_or_legacy_review_is_not_silently_accepted(self):
        for bad in ({}, {"issues": "none"}, {"issues": ["缺参考"]},
                    {"issues": [issue("unknown")]},
                    {"issues": [issue(["visual_conflict"])]},
                    {"issues": [issue("visual_conflict", evidence="")]},
                    {"issues": [], "conflicts": ["旧格式冲突"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                director.review_issues(bad)

    def test_empty_draft_still_fails_without_retry(self):
        self.draft["prompt"] = ""
        with self.assertRaisesRegex(ValueError, "未返回可用完整提示词"):
            self.run_generate()
        self.assertEqual(self.ask.call_count, 2)

    def test_missing_plan_stops_before_drafting(self):
        del self.analysis["visual_plan"]
        with self.assertRaisesRegex(ValueError, "画面编排范围"):
            self.run_generate()
        self.assertEqual(self.ask.call_count, 1)

    def test_reference_binding_check_is_preserved(self):
        self.draft["prompt"] = "仅按 @图1 和 @音频1 生成"
        with self.assertRaisesRegex(ValueError, "完整绑定"):
            self.run_generate()


if __name__ == "__main__":
    unittest.main()
