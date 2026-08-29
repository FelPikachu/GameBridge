#!/usr/bin/env python3
"""Run GameBridge's code-level regression gate.

This deliberately does not claim real-device acceptance. Real-device evidence is
maintained separately in docs/verified-baseline.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if shutil.which("node", path=environment.get("PATH")):
        return environment
    bundled_node = (
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
    )
    if bundled_node.is_file():
        current_path = environment.get("PATH", "")
        environment["PATH"] = f"{bundled_node.parent}{os.pathsep}{current_path}"
    return environment


def run(label: str, command: list[str]) -> bool:
    print(f"\n[GameBridge 保护锁] {label}")
    result = subprocess.run(  # noqa: S603 - commands come from the fixed gate list
        command,
        cwd=ROOT,
        check=False,
        env=command_environment(),
    )
    if result.returncode:
        print(f"[GameBridge 保护锁] 失败：{label}")
        return False
    print(f"[GameBridge 保护锁] 通过：{label}")
    return True


def package_runner() -> str | None:
    for name in ("pnpm", "npm"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None


def python_runner() -> str | None:
    candidates = [
        ROOT / ".venv/bin/python",
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3",
        Path(sys.executable),
    ]
    checked: set[Path] = set()
    for candidate in candidates:
        # Keep a virtual environment's launcher path intact. Resolving its
        # symlink would invoke the base interpreter without the venv packages.
        candidate = candidate.absolute()
        if candidate in checked or not candidate.is_file():
            continue
        checked.add(candidate)
        result = subprocess.run(  # noqa: S603 - candidate paths are locally derived
            [str(candidate), "-c", "import pytest"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return str(candidate)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GameBridge regression guard")
    parser.add_argument(
        "--python-only",
        action="store_true",
        help="Run only Python tests for a quick diagnostic; not a release gate.",
    )
    arguments = parser.parse_args()

    python = python_runner()
    if python is None:
        print("[GameBridge 保护锁] 失败：找不到已安装 pytest 的 Python 环境。")
        print("[GameBridge 保护锁] 可先创建 .venv，再安装 requirements-dev.txt。")
        return 2
    checks = [("Python 回归测试", [python, "-m", "pytest", "-q"])]
    if not arguments.python_only:
        runner = package_runner()
        if runner is None:
            print("[GameBridge 保护锁] 失败：找不到 pnpm 或 npm，无法检查前端。")
            return 2
        checks.extend(
            [
                ("TypeScript 类型检查", [runner, "run", "typecheck"]),
                ("前端构建", [runner, "run", "build"]),
            ]
        )

    passed = True
    for label, command in checks:
        passed = run(label, command) and passed
    if not passed:
        print("\n[GameBridge 保护锁] 未通过，不应标记完成或发布。")
        return 1
    print("\n[GameBridge 保护锁] 代码回归检查全部通过。")
    print("[GameBridge 保护锁] 注意：这不等于真机验收，请继续查看 docs/verified-baseline.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
