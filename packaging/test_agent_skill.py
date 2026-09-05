"""Offline installation checks for the actual distributable, outside the source tree."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from build_agent_skill import PROJECT, build


class AgentSkillPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agent-skill-install-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.result = build(PROJECT, cls.root / "release", "1.2.0", "liuqun12134-bit/material-universe")
        cls.archive = Path(cls.result["archive"])
        with ZipFile(cls.archive) as bundle:
            bundle.extractall(cls.root / "isolated")
        cls.skill = cls.root / "isolated/video-product-swap"
        cls.env = {
            key: value for key, value in os.environ.items()
            if not key.upper().endswith(("_KEY", "_TOKEN"))
            and not key.startswith(("MATERIAL_UNIVERSE_", "PYTHONPATH", "VIDEO_SWAP_", "KAIYUNCODE_", "DEEPSEEK_", "DASHSCOPE_"))
        }
        cls.env["PYTHONIOENCODING"] = "utf-8"
        cls.source = cls.root / "source.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=160x90:r=10", "-t", "2.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.source),
        ], check=True, capture_output=True, env=cls.env)
        from PIL import Image
        cls.product = cls.root / "product.png"
        Image.new("RGB", (64, 64), "white").save(cls.product)

    def run_python(self, code):
        # Fail immediately if any installation/preflight check attempts network IO.
        guard = "import socket\ndef denied(*a, **k): raise AssertionError('Network forbidden during install test')\nsocket.socket.connect=denied\nsocket.create_connection=denied\n"
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", guard + code], cwd=self.skill,
            env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def run_cli(self, arguments):
        code = (
            "import runpy,sys\nsys.path.insert(0,'scripts')\n"
            f"sys.argv=['scripts/generate_video.py']+{arguments!r}\n"
            "runpy.run_path('scripts/generate_video.py',run_name='__main__')\n"
        )
        return json.loads(self.run_python(code))

    def test_archive_manifest_and_no_private_or_desktop_files(self):
        manifest = json.loads((self.skill / "package-manifest.json").read_text(encoding="utf-8"))
        for relative, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.skill / relative).read_bytes()).hexdigest(), digest)
        with ZipFile(self.archive) as bundle:
            for name in bundle.namelist():
                self.assertTrue(name.startswith("video-product-swap/"))
                self.assertNotIn("..", Path(name).parts)
                self.assertNotIn(Path(name).name, (".env", "credentials.dat", "video_gui.py", "launch_gui.pyw"))
                self.assertNotIn(Path(name).suffix, (".exe", ".mp4", ".pyc"))
        self.assertFalse((self.skill.parent / "ai-video-swap-prompt-generator").exists())

    def test_repeated_build_has_identical_bytes(self):
        second = build(PROJECT, self.root / "second", "1.2.0", "liuqun12134-bit/material-universe")
        self.assertEqual(self.result["sha256"], second["sha256"])

    def test_list_models_without_keys_or_network(self):
        result = self.run_cli(["--list-models"])
        self.assertTrue(result["success"])
        self.assertGreaterEqual(len(result["models"]), 3)

    def test_general_preflight(self):
        result = self.run_cli([
            "--dry-run", "--model", "wan3", "--prompt", "用户原始提示词",
            "--reference", str(self.product), "--duration", "5", "--resolution", "480p",
            "--output", str(self.root / "general.mp4"),
        ])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["request_payload"]["prompt"], "用户原始提示词")

    def test_product_swap_preflight_without_sibling_skill(self):
        result = self.run_cli([
            "--mode", "product-swap", "--dry-run", "--video", str(self.source),
            "--reference-image", str(self.product), "--volume-relation", "参考图产品和原视频产品一样大",
            "--output", str(self.root / "swap.mp4"),
        ])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["analysis_performed"])
        self.assertTrue(result["prompt_pending"])
        self.assertEqual(result["output_parameters"]["duration"], 3)
        self.assertEqual(result["output_parameters"]["aspect_ratio"], "16:9")
        self.assertFalse((self.root / "swap.mp4").exists())

    def test_bundled_engine_and_configuration_are_local(self):
        self.run_python(
            "import sys,os\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
            "sys.path.insert(0,'scripts')\nimport prompt_engine\nfrom model_runner.runner import ModelRunner\n"
            "assert Path(prompt_engine.__file__).resolve().parent == Path.cwd().resolve()/'scripts'\n"
            "with patch('model_runner.credentials.load_env',return_value={'DEEPSEEK_API_KEY':'test-local-value'}) as read:\n"
            "    assert ModelRunner().credentials.vision()[0]=='test-local-value'\n"
            "    read.assert_called_once_with(Path.cwd().resolve()/'.env')\n"
            "with patch('model_runner.credentials.load_env') as read:\n"
            "    assert ModelRunner(values={'DEEPSEEK_API_KEY':'test-host-value'}).credentials.vision()[0]=='test-host-value'\n"
            "    read.assert_not_called()\n"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
