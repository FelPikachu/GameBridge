#!/usr/bin/env python3
"""Build the Decky plugin ZIP with stable, checked Unix permissions."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = ("plugin.json", "package.json", "main.py", "LICENSE", "dist/index.js")
EXECUTABLE_FILES = {Path("gamebridge/channel_guard.py")}


def package_files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    files.extend(sorted((ROOT / "gamebridge").rglob("*.py")))
    missing = [path for path in files if not path.is_file() or path.is_symlink()]
    if missing:
        raise FileNotFoundError(f"missing or unsafe package file: {missing[0]}")
    guard = ROOT / "gamebridge/channel_guard.py"
    if not os.access(guard, os.X_OK):
        raise PermissionError("gamebridge/channel_guard.py must remain executable")
    return files


def add_file(archive: zipfile.ZipFile, source: Path) -> None:
    relative = source.relative_to(ROOT)
    mode = 0o755 if relative in EXECUTABLE_FILES else 0o644
    modified = time.localtime(source.stat().st_mtime)[:6]
    info = zipfile.ZipInfo(f"GameBridge/{relative.as_posix()}", modified)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, source.read_bytes())


def build(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        for source in package_files():
            add_file(archive, source)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the GameBridge Decky ZIP")
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if os.environ.get("GAMEBRIDGE_REGRESSION_GUARD_ACTIVE") != "1":
        guard = subprocess.run(
            [sys.executable, str(ROOT / "scripts/regression_guard.py"), "--release"],
            cwd=ROOT,
            check=False,
        )
        if guard.returncode:
            print("保护锁未通过，已禁止生成 GameBridge 发布包。")
            return guard.returncode
    build(arguments.output.resolve())
    print(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
