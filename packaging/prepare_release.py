"""Emit the stable update contract from a finished installer, never from source files."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))
from app_version import VERSION, REPOSITORY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path, default=ROOT / ".build/release-upload")
    args = parser.parse_args()
    if args.tag and args.tag != "v" + VERSION:
        raise SystemExit("Release tag does not match app_version.py")
    source = ROOT / "发行版/素材万象安装程序.exe"
    args.output.mkdir(parents=True, exist_ok=True)
    installer = args.output / f"material-universe-setup-{VERSION}.exe"
    shutil.copy2(source, installer)
    with installer.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"format_version": 1, "repository": REPOSITORY, "version": VERSION, "data_format": 1,
                "installer": {"name": installer.name, "size": installer.stat().st_size, "sha256": checksum}}
    (args.output / "update.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (args.output / "SHA256SUMS.txt").write_text(f"{checksum}  {installer.name}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
