from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from gui_support import (
    build_generate_command,
    build_prompt_command,
    normalize_reference,
    references_for_swap,
)


class GuiSupportTests(unittest.TestCase):
    def test_build_command_preserves_prompt_and_repeated_references(self) -> None:
        command = build_generate_command(
            "python.exe",
            Path("generate_video.py"),
            prompt="保持 原样 -- 不改写",
            model="wan3.0-video",
            references=["image=D:\\ref.png", "video=https://example.com/a.mp4"],
            output="D:\\out.mp4",
            duration=8,
            aspect_ratio="9:16",
            resolution="1080p",
            dry_run=True,
        )
        self.assertEqual(command[command.index("--prompt") + 1], "保持 原样 -- 不改写")
        self.assertEqual(command.count("--reference"), 2)
        self.assertIn("--aspect-ratio", command)
        self.assertIn("--resolution", command)
        self.assertEqual(command[-1], "--dry-run")

    def test_normalize_reference_rejects_unknown_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支持的参考类型"):
            normalize_reference("document", "D:\\a.pdf")

    def test_prompt_command_requests_json_and_preserves_relation(self) -> None:
        command = build_prompt_command(
            "python.exe",
            Path("generate_swap_prompt.py"),
            video="D:\\source.mp4",
            reference_image="D:\\ref.png",
            volume_relation="高度约为三分之二，宽度一致",
            model="deepseek-v4-flash-vision-exp",
        )
        self.assertEqual(command[command.index("--volume-relation") + 1], "高度约为三分之二，宽度一致")
        self.assertEqual(command[command.index("--credential-source") + 1], "host-app")
        self.assertIn("--json", command)

    def test_omniflash_requires_public_image_but_keeps_local_video(self) -> None:
        references = references_for_swap(
            "omniflash",
            source_video="D:\\source.mp4",
            local_reference_image="D:\\ref.png",
            public_reference_url="https://example.com/ref.png",
        )
        self.assertEqual(references[0], "video=D:\\source.mp4")
        self.assertEqual(references[1], "image=https://example.com/ref.png")

    def test_omniflash_never_auto_uses_local_image(self) -> None:
        with self.assertRaisesRegex(ValueError, "不会擅自上传"):
            references_for_swap(
                "omniflash",
                source_video="D:\\source.mp4",
                local_reference_image="D:\\ref.png",
            )


if __name__ == "__main__":
    unittest.main()
