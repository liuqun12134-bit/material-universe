import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))
from redesign_acceptance import run

if __name__ == "__main__":
    destination = ROOT / "发行版/维护资料/验收记录/1.9.0-创作改版"
    report = run(destination)
    (destination / "离线验收.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
