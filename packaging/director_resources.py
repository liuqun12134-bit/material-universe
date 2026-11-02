"""Build a public rules bundle without copying local personal examples or media."""
import importlib.util
import shutil
from pathlib import Path

PUBLIC_EXAMPLES = ("demo-method-sheet-mask-01",)


def stage_director(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("Use a fresh staging directory for director resources.")
    destination.mkdir(parents=True)
    for name in ("SKILL.md", "learning-config.json"):
        shutil.copy2(source / name, destination / name)
    for name in ("references", "scripts", "templates", "agents"):
        shutil.copytree(source / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for identity in PUBLIC_EXAMPLES:
        folder = source / "demo-library/entries" / identity
        shutil.copytree(folder, destination / "demo-library/entries" / identity)
    spec = importlib.util.spec_from_file_location("release_director_schema", source / "scripts/library.py")
    schema = importlib.util.module_from_spec(spec); spec.loader.exec_module(schema)
    records = schema.collect(destination)
    for kind, rows in records.items():
        schema.atomic_json(destination / f"{kind}-library/index.json", schema.index_data(destination, kind, rows))
    return destination
