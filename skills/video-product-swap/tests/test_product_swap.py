from __future__ import annotations

import contextlib
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from generate_video import build_parser, main
from media_inspection import VideoInfo, probe_video
from model_runner.adapters.fal_base import FalQueueAdapter
from model_runner.core import GenerationRequest, VideoGenerationError
from model_runner.runner import ModelRunner
from product_swap import ProductSwapWorkflow, SUBTITLE_PREFIX
import prompt_engine


class ProductSwapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.mp4"
        self.image = self.root / "product.png"
        self.output = self.root / "result.mp4"
        self.source.write_bytes(b"video")
        self.image.write_bytes(b"image")
        self.env = patch.dict(os.environ, {"FAL_KEY": "test-video-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runner = ModelRunner(SKILL_ROOT, credential_source="host-app")
        self.workflow = ProductSwapWorkflow(self.runner)
        self.engine = prompt_engine
        self.analysis = {
            "placements": ["in_hand", "on_table"], "other_location_zh": "",
            "volume_relation_zh": "参考图产品的高度约为原视频产品的三分之二，宽度一致",
        }
        self.args = build_parser().parse_args([
            "--mode", "product-swap", "--video", str(self.source),
            "--reference-image", str(self.image), "--volume-relation", "高度约三分之二，宽度一致",
            "--output", str(self.output), "--credential-source", "host-app",
        ])

    def run_workflow(self, duration=8.43, ratio="16:9", analysis=None):
        with patch("product_swap.probe_video", return_value=VideoInfo(duration, 1920, 1080, ratio)), \
             patch.object(self.runner.credentials, "vision", return_value=("test-vision-key", "https://api.deepseek.com", "ignored-model")), \
             patch.object(self.engine, "analyze_video_and_relation", return_value=analysis or self.analysis) as vision, \
             patch.object(FalQueueAdapter, "execute", autospec=True, return_value={"success": True, "technical_completed": True}) as generate:
            result = self.workflow.run(self.args)
        return result, vision, generate

    def test_analysis_and_automatic_parameters_reach_one_video_submission(self):
        result, vision, generate = self.run_workflow()
        vision.assert_called_once()
        self.assertEqual(vision.call_args.args[2], "deepseek-v4-flash-vision-exp")
        self.assertNotIn(self.image, vision.call_args.args)
        generate.assert_called_once()
        request = generate.call_args.args[1]
        payload = generate.call_args.args[0].build_payload(request)
        self.assertEqual(payload, {
            "model": "alibaba/wan-3.0/reference-to-video", "prompt": result["prompt"],
            "duration": 9, "aspect_ratio": "16:9", "resolution": "480p",
        })
        self.assertIn("人物手中和桌面上", result["prompt"])
        self.assertEqual(result["source_duration"], 8.43)
        self.assertEqual([ref.local_path for ref in request.references], [self.source, self.image])
        self.assertEqual(request.options, {"enable_prompt_expansion": True, "enable_thinking": True})

    def test_swap_switches_are_independent_in_submitted_request(self):
        self.args.enable_prompt_expansion = False
        self.args.enable_thinking = True
        _, _, generate = self.run_workflow()
        self.assertEqual(generate.call_args.args[1].options,
                         {"enable_prompt_expansion": False, "enable_thinking": True})
        self.args.enable_prompt_expansion = True
        self.args.enable_thinking = False
        self.args.output = str(self.root / "second-result.mp4")
        _, _, generate = self.run_workflow()
        self.assertEqual(generate.call_args.args[1].options,
                         {"enable_prompt_expansion": True, "enable_thinking": False})

    def test_removed_or_non_swap_models_stop_before_analysis(self):
        for model in ("omniflash", "omni_video_edit", "google/gemini-omni-flash/v1.1/edit", "sync3", "wan3.0-t2v"):
            with self.subTest(model=model), patch("product_swap.probe_video") as probe, patch.object(self.engine, "analyze_video_and_relation") as vision:
                with self.assertRaises(VideoGenerationError):
                    self.workflow.generate(self.source, self.image, "大小一致", self.output, model=model)
                probe.assert_not_called()
                vision.assert_not_called()

    def test_uncertain_location_continues_once(self):
        uncertain = {**self.analysis, "placements": ["uncertain"]}
        result, vision, generate = self.run_workflow(analysis=uncertain)
        generate.assert_called_once()
        self.assertIn("位置不明确处", result["prompt"])
        self.assertTrue(any("位置识别不确定" in item for item in result["warnings"]))

    def test_remove_subtitles_prepares_and_submits_identical_prompt(self):
        self.args.remove_subtitles = True
        result, _, generate = self.run_workflow()
        submitted = generate.call_args.args[1].prompt
        self.assertEqual(submitted, result["prompt"])
        self.assertEqual(submitted, SUBTITLE_PREFIX + result["base_prompt"])
        self.assertTrue(result["remove_subtitles"])

    def test_reusing_exact_prompt_skips_vision_and_preserves_extra_images(self):
        extra = self.root / "extra.png"
        extra.write_bytes(b"image2")
        prompt = "  保持产品比例。\n不要改写这段提示词。  "
        with patch("product_swap.probe_video", return_value=VideoInfo(5, 1920, 1080, "16:9")), \
             patch.object(self.runner.credentials, "vision") as credential, \
             patch.object(self.engine, "analyze_video_and_relation") as analysis, \
             patch.object(FalQueueAdapter, "execute", autospec=True, return_value={"success": True}) as generate:
            result = self.workflow.generate(self.source, self.image, "", self.output,
                                            prompt_override=prompt, extra_images=[str(extra)])
        credential.assert_not_called(); analysis.assert_not_called()
        self.assertEqual(result["prompt"], prompt)
        request = generate.call_args.args[1]
        self.assertEqual(request.prompt, prompt)
        self.assertEqual(len(request.references), 3)

    def test_reuse_prefix_is_idempotent_and_off_preserves_original(self):
        base = "  保留原场景。  "
        for enabled, input_prompt, expected in ((True, base, SUBTITLE_PREFIX + base),
                                                 (True, SUBTITLE_PREFIX + base, SUBTITLE_PREFIX + base),
                                                 (False, SUBTITLE_PREFIX + base, SUBTITLE_PREFIX + base)):
            with self.subTest(enabled=enabled, input_prompt=input_prompt), \
                 patch("product_swap.probe_video", return_value=VideoInfo(5, 1920, 1080, "16:9")), \
                 patch.object(FalQueueAdapter, "execute", autospec=True, return_value={"success": True}) as generate:
                prepared = []
                result = self.workflow.generate(self.source, self.image, "", self.root / f"result-{enabled}-{len(prepared)}-{len(input_prompt)}.mp4",
                    prompt_override=input_prompt, remove_subtitles=enabled,
                    prepared_callback=lambda actual, original: prepared.append((actual, original)))
                self.assertEqual(result["prompt"], expected)
                self.assertEqual(generate.call_args.args[1].prompt, expected)
                self.assertEqual(prepared[0][0], expected)
                self.assertEqual(result["base_prompt"], base if enabled else input_prompt)

    def test_cli_rejects_remove_subtitles_outside_product_swap(self):
        for flags in (["--mode", "general", "--remove-subtitles"],
                      ["--resume", str(self.output) + ".video-task.json", "--remove-subtitles"]):
            output = io.StringIO()
            with self.subTest(flags=flags), patch.object(sys, "argv", ["generate_video.py", *flags]), \
                 contextlib.redirect_stdout(output), patch.object(ModelRunner, "resume") as resume:
                self.assertEqual(main(), 1)
                resume.assert_not_called()
            self.assertIn("remove-subtitles" if "general" in flags else "--resume", output.getvalue())

    def test_over_limit_rejected_before_any_model_or_prompt_loading(self):
        with patch("product_swap.probe_video", return_value=VideoInfo(15.001, 1080, 1920, "9:16")), \
             patch("prompt_engine.analyze_video_and_relation") as engine, \
             patch.object(self.runner, "run") as video:
            with self.assertRaisesRegex(VideoGenerationError, "超过 15 秒"):
                self.workflow.run(self.args)
        engine.assert_not_called()
        video.assert_not_called()

    def test_exactly_fifteen_seconds_is_accepted(self):
        result, _, generate = self.run_workflow(duration=15)
        generate.assert_called_once()
        self.assertEqual(result["output_parameters"]["duration"], 15)

    def test_short_clip_respects_two_second_api_minimum(self):
        result, _, generate = self.run_workflow(duration=0.8)
        generate.assert_called_once()
        self.assertEqual(result["output_parameters"]["duration"], 2)
        self.assertTrue(result["warnings"])

    def test_dry_run_calls_neither_model_and_returns_pending_prompt(self):
        self.args.dry_run = True
        result, vision, generate = self.run_workflow()
        vision.assert_not_called()
        generate.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["prompt_pending"])
        self.assertIsNone(result["request_payload"]["prompt"])
        self.assertEqual(result["request_payload"]["duration"], 9)

    def test_fixed_mode_rejects_overrides_before_analysis(self):
        for field, value in (("prompt", "manual"), ("model", "wan-official"),
                             ("duration", 6), ("aspect_ratio", "1:1"), ("resolution", "1080p"),
                             ("reference", ["https://example.com/a.png"])):
            with self.subTest(field=field), patch("product_swap.probe_video") as probe:
                original = getattr(self.args, field)
                setattr(self.args, field, value)
                with self.assertRaisesRegex(VideoGenerationError, "通用|general"):
                    self.workflow.run(self.args)
                setattr(self.args, field, original)
                probe.assert_not_called()

    def test_missing_relation_is_not_invented(self):
        self.args.volume_relation = " "
        with patch("product_swap.probe_video") as probe:
            with self.assertRaisesRegex(VideoGenerationError, "volume-relation"):
                self.workflow.run(self.args)
        probe.assert_not_called()

    def test_existing_output_rejected_before_vision(self):
        self.output.write_bytes(b"existing")
        with patch("product_swap.probe_video", return_value=VideoInfo(8, 1080, 1920, "9:16")), \
             patch("prompt_engine.analyze_video_and_relation") as engine:
            with self.assertRaisesRegex(VideoGenerationError, "输出文件已存在"):
                self.workflow.run(self.args)
        engine.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_missing_video_key_stops_before_paid_analysis(self):
        with patch.dict(self.runner.credentials.values, {"FAL_KEY": ""}), \
             patch("product_swap.probe_video", return_value=VideoInfo(8, 1080, 1920, "9:16")), \
             patch.object(self.engine, "analyze_video_and_relation") as vision:
            with self.assertRaisesRegex(VideoGenerationError, "FAL_KEY"):
                self.workflow.run(self.args)
        vision.assert_not_called()

    def test_vision_failure_stops_and_redacts_key(self):
        with patch("product_swap.probe_video", return_value=VideoInfo(8, 1080, 1920, "9:16")), \
             patch.object(self.runner.credentials, "vision", return_value=("test-vision-key", "https://api.deepseek.com", "unused")), \
             patch.object(self.engine, "analyze_video_and_relation", side_effect=RuntimeError("bad test-vision-key")), \
             patch.object(FalQueueAdapter, "execute") as generate:
            with self.assertRaisesRegex(VideoGenerationError, r"bad \*\*\*"):
                self.workflow.run(self.args)
        generate.assert_not_called()

    def test_host_mode_never_loads_skill_env(self):
        with patch("model_runner.credentials.load_env") as load:
            ModelRunner(SKILL_ROOT, credential_source="host-app")
        load.assert_not_called()

    def test_general_mode_keeps_explicit_parameters_without_vision(self):
        argv = ["generate_video.py", "--mode", "general", "--prompt", "原始提示词",
                "--model", "wan3", "--reference", str(self.image), "--duration", "12",
                "--aspect-ratio", "1:1", "--resolution", "720p", "--output", str(self.output),
                "--dry-run", "--credential-source", "host-app"]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
             patch("prompt_engine.analyze_video_and_relation") as load:
            self.assertEqual(main(), 0)
        load.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["request_payload"], {
            "prompt": "原始提示词", "duration": 12,
            "aspect_ratio": "1:1", "resolution": "720p",
            "reference_image_urls": [f"fal-storage://{self.image.name}"],
            "enable_prompt_expansion": False,
        })

    def test_older_requests_may_omit_optional_output_specs(self):
        request = GenerationRequest(
            prompt="原始提示词", requested_model="wan3",
            model=self.runner.registry.resolve("wan3"), references=[],
            output=self.output, duration=5,
        )
        self.assertIsNone(request.aspect_ratio)
        self.assertIsNone(request.resolution)


class PromptAnalysisTests(unittest.TestCase):
    def test_invalid_placements_have_distinct_errors(self):
        relation = "参考图产品比原视频产品矮"
        for placements, expected in ((None, "缺少 placements"), ("in_hand", "placements 必须是数组"),
                                     ([], "placements 是空数组"), (["in_hand", "wrong"], "不允许的位置分类")):
            with self.subTest(placements=placements):
                payload = {"volume_relation_zh": relation}
                if placements is not None:
                    payload["placements"] = placements
                with self.assertRaisesRegex(RuntimeError, expected):
                    prompt_engine.parse_analysis_response(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(prompt_engine.parse_analysis_response(json.dumps({
            "placements": ["in_hand"], "volume_relation_zh": relation,
        }, ensure_ascii=False))["placements"], ["in_hand"])
        with self.assertRaisesRegex(RuntimeError, "返回字段：product_location") as error:
            prompt_engine.parse_analysis_response(json.dumps({
                "product_location": "secret-value", "bad.key": "ignored",
            }))
        self.assertNotIn("secret-value", str(error.exception))
        with self.assertRaisesRegex(RuntimeError, "placements 必须是数组"):
            prompt_engine.parse_analysis_response(json.dumps({
                "placements": None, "volume_relation_zh": relation,
            }, ensure_ascii=False))

    def test_vision_request_keeps_storyboard_then_three_detail_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            captured = []

            def storyboard(_source, path):
                path.write_bytes(b"storyboard")

            def details(_source, directory):
                paths = [directory / f"detail_{index}.jpg" for index in range(1, 4)]
                for index, path in enumerate(paths, start=1):
                    path.write_bytes(f"detail-{index}".encode())
                return paths

            def urlopen(request, timeout):
                captured.append(json.loads(request.data.decode("utf-8")))
                return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({
                    "placements": ["in_hand"], "other_location_zh": "",
                    "volume_relation_zh": "参考图产品比原视频产品矮",
                }, ensure_ascii=False)}}]}).encode())

            with patch.object(prompt_engine, "_make_storyboard", side_effect=storyboard), \
                 patch.object(prompt_engine, "_make_detail_frames", side_effect=details), \
                 patch.object(prompt_engine.urllib.request, "urlopen", side_effect=urlopen):
                result = prompt_engine.analyze_video_and_relation(source, "高度较矮", "vision-model", 20,
                                                                  "test-key", "https://api.deepseek.com")
            self.assertEqual(result["placements"], ["in_hand"])
            self.assertEqual(len(captured), 1)
            content = captured[0]["messages"][0]["content"]
            images = [part["image_url"]["url"].split(",", 1)[1] for part in content if part["type"] == "image_url"]
            self.assertEqual([base64.b64decode(image) for image in images],
                             [b"storyboard", b"detail-1", b"detail-2", b"detail-3"])
            self.assertIn("被手指夹捏", content[0]["text"])

    def test_detail_frames_cover_early_middle_and_late_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.write_bytes(b"video")
            calls = []

            def run(command, _description):
                calls.append(command)
                Path(command[-1]).write_bytes(b"jpg")

            with patch.object(prompt_engine.shutil, "which", return_value="ffmpeg"), \
                 patch.object(prompt_engine, "_video_duration", return_value=15), \
                 patch.object(prompt_engine, "_run_checked", side_effect=run):
                paths = prompt_engine._make_detail_frames(source, root)
            self.assertEqual(len(paths), 3)
            self.assertEqual([command[command.index("-ss") + 1] for command in calls],
                             ["2.500000", "7.500000", "12.500000"])
            self.assertTrue(all("-vf" not in command for command in calls))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 FFmpeg/FFprobe")
class LocalMediaIntegrationTests(unittest.TestCase):
    def test_real_clips_drive_cli_specs_and_over_limit_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "product.png"
            from PIL import Image
            Image.new("RGB", (32, 32), "white").save(image)
            for size, seconds, expected_ratio, expected_seconds in (
                ("160x90", "8.43", "16:9", 9),
                ("100x70", "3", "9:16", 3),
                ("90x160", "15", "9:16", 15),
                ("160x90", "15.1", None, None),
            ):
                with self.subTest(size=size, seconds=seconds):
                    source = root / f"{size}-{seconds}.mp4"
                    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi",
                                    "-i", f"color=c=black:s={size}:r=100", "-t", seconds,
                                    "-c:v", "libx264", "-preset", "ultrafast", str(source)],
                                   check=True, capture_output=True)
                    command = [sys.executable, str(SKILL_ROOT / "scripts" / "generate_video.py"),
                               "--mode", "product-swap", "--video", str(source),
                               "--reference-image", str(image), "--volume-relation", "大小一致",
                               "--output", str(root / "result.mp4"), "--dry-run", "--credential-source", "host-app"]
                    run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
                    result = json.loads(run.stdout)
                    if expected_ratio is None:
                        self.assertNotEqual(run.returncode, 0)
                        self.assertIn("超过 15 秒", result["error"])
                    else:
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                        self.assertEqual(result["output_parameters"]["aspect_ratio"], expected_ratio)
                        self.assertEqual(result["output_parameters"]["duration"], expected_seconds)
                        self.assertEqual(result["output_parameters"]["resolution"], "480p")
                        self.assertFalse(result["analysis_performed"])

    def test_display_rotation_and_anamorphic_pixels(self):
        for stream, ratio in (
            ({"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]}, "9:16"),
            ({"width": 720, "height": 576, "sample_aspect_ratio": "64:45"}, "16:9"),
        ):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "source.mp4"
                source.write_bytes(b"video")
                payload = {"streams": [stream], "format": {"duration": "8"}}
                result = subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b"")
                with patch("media_inspection._run", return_value=result):
                    self.assertEqual(probe_video(source).aspect_ratio, ratio)


if __name__ == "__main__":
    unittest.main()
