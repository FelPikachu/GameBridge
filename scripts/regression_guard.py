#!/usr/bin/env python3
"""Run GameBridge's code-level regression gate.

This deliberately does not claim real-device acceptance. Real-device evidence is
maintained separately in docs/verified-baseline.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / "docs/protected-baseline.json"


def fail(message: str) -> bool:
    print(f"[GameBridge 保护锁] 失败：{message}")
    return False


def git_output(*arguments: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def verify_protected_baseline(*, require_archive: bool) -> bool:
    print("\n[GameBridge 保护锁] 核对 beta4 出生证明与已锁测试")
    try:
        baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return fail(f"无法读取 {BASELINE_FILE.relative_to(ROOT)}：{error}")

    repository_root = git_output("rev-parse", "--show-toplevel")
    if repository_root is None or Path(repository_root).resolve() != ROOT.resolve():
        return fail("当前目录不是 GameBridge 正式 Git 项目根目录")

    commit = str(baseline.get("gitCommit", ""))
    if not commit or git_output("cat-file", "-t", commit) != "commit":
        return fail("找不到已锁定的 beta4 基线提交")
    ancestor = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", commit, "HEAD"],
        check=False,
    )
    if ancestor.returncode != 0:
        return fail("当前代码不是从已发布 beta4 继承而来，禁止继续打包")

    accepted_commits = baseline.get("acceptedCommits", [])
    if not isinstance(accepted_commits, list):
        return fail("已验收功能提交清单格式错误")
    for milestone in accepted_commits:
        if not isinstance(milestone, dict):
            return fail("已验收功能提交清单格式错误")
        milestone_name = str(milestone.get("name", "未命名功能"))
        milestone_commit = str(milestone.get("gitCommit", ""))
        if not milestone_commit or git_output("cat-file", "-t", milestone_commit) != "commit":
            return fail(f"找不到已验收功能提交：{milestone_name}")
        milestone_ancestor = subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", milestone_commit, "HEAD"],
            check=False,
        )
        if milestone_ancestor.returncode != 0:
            return fail(f"当前代码未继承已验收功能：{milestone_name}")

    required_tests = baseline.get("requiredTests")
    if not isinstance(required_tests, dict) or not required_tests:
        return fail("已锁测试清单为空")
    for relative, names in required_tests.items():
        path = ROOT / relative
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            return fail(f"已锁测试文件缺失：{relative}")
        if not isinstance(names, list):
            return fail(f"已锁测试清单格式错误：{relative}")
        for name in names:
            if f"def {name}(" not in source:
                return fail(f"已锁能力测试消失：{relative}::{name}")

    if require_archive:
        archive = ROOT / str(baseline.get("releaseArchive", ""))
        if not archive.is_file():
            return fail(f"发布基线包缺失：{archive}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != baseline.get("releaseSha256"):
            return fail("发布基线包 SHA-256 不匹配")

    print("[GameBridge 保护锁] 通过：项目继承自正式 beta4 与全部已验收功能，已锁测试齐全")
    return True


def command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Packaging is one of the regression tests.  Mark child commands so a
    # package build exercised by pytest does not start a second full guard and
    # recurse forever.  A normal, standalone package build still runs the gate.
    environment["GAMEBRIDGE_REGRESSION_GUARD_ACTIVE"] = "1"
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
    parser.add_argument(
        "--release",
        action="store_true",
        help="Also require the pinned beta4 release archive; use before packaging.",
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Only verify repository ancestry and locked-test presence before editing.",
    )
    arguments = parser.parse_args()

    if not verify_protected_baseline(require_archive=arguments.release):
        return 2
    if arguments.baseline_only:
        return 0

    python = python_runner()
    if python is None:
        print("[GameBridge 保护锁] 失败：找不到已安装 pytest 的 Python 环境。")
        print("[GameBridge 保护锁] 可先创建 .venv，再安装 requirements-dev.txt。")
        return 2
    checks = [("Python 回归测试", [python, "-m", "pytest", "-q", "-x"])]
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
