"""Documented maintenance entry point for desktop agents and source users."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app_updates import check_update, download_update, backup_for_update, start_installer
from app_version import VERSION
from desktop_tasks import DesktopStore, TaskManager
from library_transfer import LibraryTransfer, PreparedBundle
from personal_library import atomic_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="素材万象库迁移与在线更新；不提交任何生成任务。")
    parser.add_argument("--report", type=Path, required=True, help="将结果写入此 JSON 文件（安装版无控制台）")
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "MaterialUniverse/任务与素材")
    parser.add_argument("--library-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-update")
    commands.add_parser("download-update")
    commands.add_parser("install-update")
    export = commands.add_parser("export-libraries")
    export.add_argument("destination", type=Path)
    export.add_argument("--scope", choices=["all", "assets", "skills"], default="all")
    for command in ("preview-import", "import-libraries"):
        sub = commands.add_parser(command); sub.add_argument("archive", type=Path)
    sub = commands.add_parser("import-skill-directory")
    sub.add_argument("directory", type=Path)
    sub.add_argument("--include-learning", action="store_true")
    commands.add_parser("migrate-legacy")
    args = parser.parse_args(argv)
    store = manager = None
    try:
        if args.command in {"check-update", "download-update"}:
            result = check_update()
            if args.command == "download-update" and result["available"]:
                result["downloaded"] = str(download_update(result, args.data_root / "updates"))
        else:
            store = DesktopStore(args.data_root)
            # Fail when the GUI owns this database. Do not mutate it from a second agent.
            manager = TaskManager(store, recover_interrupted=False)
            transfer = LibraryTransfer(store, args.library_root)
            transfer.recover()
            if args.command == "export-libraries":
                result = transfer.export(args.destination, assets=args.scope != "skills", skills=args.scope != "assets")
            elif args.command in {"preview-import", "import-libraries"}:
                prepared = PreparedBundle(args.archive)
                try:
                    result = transfer.preview(prepared) if args.command == "preview-import" else transfer.import_bundle(prepared)
                finally:
                    prepared.close()
            elif args.command == "import-skill-directory":
                prepared = transfer.import_skill_directory(args.directory, args.include_learning)
                try:
                    result = transfer.import_bundle(prepared)
                finally:
                    prepared.close()
            elif args.command == "migrate-legacy":
                from model_runner.runner import SKILL_ROOT
                transfer.migrate_legacy([SKILL_ROOT / "director-skill"])
                result = {"personal_library": str(transfer.root)}
            else:
                # Re-read the trusted release before installation; do not trust an old local ticket.
                update = check_update()
                if not update["available"]:
                    result = update
                else:
                    installer = download_update(update, store.root / "updates")
                    result = backup_for_update(transfer, update["version"])
                    start_installer(installer, update["manifest"], store.root)
                    result["installer_started"] = True
        atomic_json(args.report, {"success": True, "app_version": VERSION, "result": result})
        return 0
    except Exception as exc:
        atomic_json(args.report, {"success": False, "app_version": VERSION, "error": str(exc)})
        return 1
    finally:
        if manager:
            manager.stop()
        if store:
            store.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
