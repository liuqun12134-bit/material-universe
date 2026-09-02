from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_swap_prompt.py"
SPEC = importlib.util.spec_from_file_location("generate_swap_prompt", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class CredentialIsolationTests(unittest.TestCase):
    def test_direct_skill_ignores_system_key_when_own_env_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "system-key"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "不会使用 Windows"):
                    MODULE._credential_config("skill-env", env_path)

    def test_direct_skill_uses_only_its_own_env_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "DEEPSEEK_API_KEY=skill-key\n"
                "DEEPSEEK_API_BASE=https://skill.example.com\n"
                "DEEPSEEK_PROMPT_MODEL=skill-model\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "system-key"}, clear=False):
                self.assertEqual(
                    MODULE._credential_config("skill-env", env_path),
                    ("skill-key", "https://skill.example.com", "skill-model"),
                )

    def test_host_app_requires_its_explicit_child_environment_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "",
                "DEEPSEEK_API_BASE": "https://api.deepseek.com",
                "DEEPSEEK_PROMPT_MODEL": "deepseek-v4-flash-vision-exp",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "宿主应用没有提供"):
                MODULE._credential_config("host-app")


if __name__ == "__main__":
    unittest.main()
