"""Run the isolated desktop voice acceptance and save reviewable evidence."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))

from voice_acceptance import run


if __name__ == "__main__":
    destination = ROOT / "output/声音工作区验收"
    destination.mkdir(parents=True, exist_ok=True)
    report = run(destination)
    (destination / "离线验收.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
