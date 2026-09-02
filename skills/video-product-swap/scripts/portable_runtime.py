from __future__ import annotations

import io
import json
import os
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable


_RUNTIME_LOCK = threading.RLock()


def _entrypoint(script_name: str) -> Callable[[], int]:
    if script_name == "generate_video.py":
        from generate_video import main

        return main
    if script_name == "generate_swap_prompt.py":
        from generate_swap_prompt import main

        return main
    raise RuntimeError(f"打包版不支持运行内部脚本：{script_name}")


def _redact(message: str, env: dict[str, str]) -> str:
    result = message
    for name, value in env.items():
        upper_name = name.upper()
        if value and any(token in upper_name for token in ("KEY", "TOKEN", "SECRET")):
            result = result.replace(value, "***")
    return result


def run_internal_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
) -> tuple[int, str, str]:
    """Run a bundled CLI entrypoint without requiring a second Python installation."""
    if len(command) < 2:
        return 2, json.dumps({"success": False, "error": "内部命令不完整。"}, ensure_ascii=False), ""

    script_name = Path(command[1]).name
    args = command[2:]
    stdout = io.StringIO()
    stderr = io.StringIO()

    with _RUNTIME_LOCK:
        previous_argv = sys.argv[:]
        previous_cwd = Path.cwd()
        previous_env = dict(os.environ)
        try:
            sys.argv = [script_name, *args]
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(env)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    return_code = int(_entrypoint(script_name)() or 0)
                except SystemExit as exc:
                    return_code = int(exc.code) if isinstance(exc.code, int) else 1
                except Exception as exc:
                    return_code = 1
                    print(
                        json.dumps(
                            {"success": False, "error": _redact(str(exc), env)},
                            ensure_ascii=False,
                        )
                    )
        finally:
            sys.argv = previous_argv
            os.chdir(previous_cwd)
            os.environ.clear()
            os.environ.update(previous_env)

    return return_code, stdout.getvalue().strip(), stderr.getvalue().strip()
