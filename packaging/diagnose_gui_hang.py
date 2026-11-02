"""Opt-in, local stack capture for the existing release; never reads frame locals."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "发行版" / "素材万象.exe"
SPY = ROOT / ".build" / "venv" / "Scripts" / "py-spy.exe"
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def capture(pid: int, native: bool = False) -> str:
    command = [str(SPY), "dump", "--pid", str(pid)]
    if native:
        command.append("--native")
    try:
        result = subprocess.run(command, capture_output=True, timeout=8,
                                creationflags=HIDDEN)
        return (result.stdout + result.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "Stack capture exceeded 8 seconds."


def release_processes():
    found = []
    for proc in psutil.process_iter(["pid", "exe", "ppid"]):
        try:
            if proc.info["exe"] and os.path.normcase(proc.info["exe"]) == os.path.normcase(str(RELEASE)):
                found.append(proc)
        except (psutil.Error, OSError):
            pass
    return found


def monitor(proc, output: Path, seconds: float):
    recent = deque(maxlen=90)
    deadline = time.monotonic() + seconds
    number = 0
    while time.monotonic() < deadline and proc.is_running():
        stamp = datetime.now().isoformat(timespec="seconds")
        stack = capture(proc.pid, native=number % 5 == 0)
        recent.append(f"\n=== {stamp} | PID {proc.pid} ===\n{stack}")
        temporary = output / "recent-stacks.tmp"
        temporary.write_text("".join(recent), encoding="utf-8")
        temporary.replace(output / "最近线程现场.txt")
        number += 1
        time.sleep(2)
    (output / "采样结束.txt").write_text(datetime.now().isoformat(), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, help="Capture a specific local diagnostic process")
    parser.add_argument("--seconds", type=float, default=3600)
    args = parser.parse_args()
    output = ROOT / ".build" / "卡顿诊断" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True)
    try:
        if not SPY.is_file():
            raise RuntimeError("缺少诊断工具 py-spy。")
        if args.pid:
            proc = psutil.Process(args.pid)
        else:
            processes = release_processes()
            if not processes:
                subprocess.Popen([str(RELEASE)], cwd=ROOT)
            # The single-file bootloader is a parent process; capture its Python child.
            limit = time.monotonic() + 45
            proc = None
            while time.monotonic() < limit:
                processes = release_processes()
                pids = {p.pid for p in processes}
                children = [p for p in processes if p.ppid() in pids]
                if children:
                    proc = children[-1]
                    if "Python v" in capture(proc.pid):
                        break
                time.sleep(.5)
            else:
                raise RuntimeError("45 秒内未找到 EXE 的 Python 进程。")
        (output / "说明.json").write_text(json.dumps({
            "pid": proc.pid, "executable": proc.exe(),
            "max_seconds": args.seconds, "frame_locals_captured": False,
            "note": "本地循环记录最近 90 次线程调用栈；关闭软件即停止。诊断采样可能轻微影响性能。",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        monitor(proc, output, args.seconds)
    except Exception as exc:
        (output / "诊断错误.txt").write_text(str(exc), encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
