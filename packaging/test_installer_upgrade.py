"""CI-only acceptance of the actual installer and frozen maintenance entry.

Installs into an isolated directory twice, without uninstalling between versions.
This writes current-user installer registration, so run only on a disposable CI VM.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/video-product-swap/scripts"))


def run():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This installer acceptance is restricted to disposable GitHub runners.")
    from PIL import Image
    from desktop_tasks import DesktopStore
    from library_transfer import PreparedBundle
    from personal_library import collect, digest

    work = ROOT / ".build/installer-acceptance"
    work.mkdir(parents=True, exist_ok=True)
    app_dir, data = work / "app", work / "data"
    installer = ROOT / "发行版/素材万象安装程序.exe"
    exe = app_dir / "素材万象.exe"
    report = {}

    def install(label):
        subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                        "/CLOSEAPPLICATIONS", "/DIR=" + str(app_dir), "/LOG=" + str(work / (label + ".log"))],
                       check=True, timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
        assert exe.is_file() and (app_dir / "unins000.exe").is_file()

    def maintenance(label, *args):
        path = work / (label + ".json")
        subprocess.run([str(exe), "--maintenance", "--report", str(path), "--data-root", str(data), *map(str, args)],
                       check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(path.read_text(encoding="utf-8"))
        assert result["success"], result
        return result

    def rows():
        with sqlite3.connect(data / "history.sqlite3") as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall()
                    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    install("first-install")
    report["first_install"] = True
    source = DesktopStore(data)
    group = source.create_group("保留分组")
    picture = work / "fixture.png"
    Image.new("RGB", (32, 32), "#927753").save(picture)
    asset = source.import_asset(picture, group["id"])
    source.db.close()
    # Simulate an older installed director bundle. Updating must leave it in
    # place until its records and attachments have been copied to personal data.
    bundle = app_dir / "_internal/skills/video-product-swap"
    shutil.copytree(bundle / "director-rules", bundle / "director-skill")
    maintenance("legacy-copy", "migrate-legacy")
    personal = data / "Skill附属库"
    assert sum(len(v) for v in collect(personal).values()) == 1
    before = rows()
    case_hashes = {str(p.relative_to(personal)): digest(p) for p in personal.rglob("*") if p.is_file()}
    first = work / "before.mu-library.zip"
    maintenance("first-export", "export-libraries", first)
    with_bundle = PreparedBundle(first, temporary_parent=work)
    try:
        assert len(with_bundle.manifest["assets"]) == 1
        assert len(with_bundle.manifest["groups"]) == 1
        assert with_bundle.manifest["skill_included"]
    finally:
        with_bundle.close()
    report["frozen_migration_and_export"] = True
    install("cover-install")
    assert rows() == before
    assert {str(p.relative_to(personal)): digest(p) for p in personal.rglob("*") if p.is_file()} == case_hashes
    assert digest(Path(asset["path"])) == digest(picture)
    assert (bundle / "director-skill").is_dir()
    maintenance("preview-after-upgrade", "preview-import", first)
    maintenance("export-after-upgrade", "export-libraries", work / "after.mu-library.zip")
    report.update(cover_install_without_uninstall=True, data_unchanged=True, success=True)
    (work / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    run()
