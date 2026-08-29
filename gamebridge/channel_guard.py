#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if os.fspath(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PLUGIN_ROOT))

from gamebridge.providers.hoyoplay import MIHOYO_CN, HoYoPlayProvider  # noqa: E402


def _global_genshin_router(root_data: Path, game_id: str) -> list[str] | None:
    """Keep Global Genshin on its isolated GameBridge route."""
    if game_id != "hk4e_cn":
        return None
    try:
        selection = (root_data / "mihoyo-selection").read_text(encoding="utf-8").strip()
    except OSError:
        selection = "official"
    if selection != "global":
        return None
    return [
        sys.executable,
        os.fspath(PLUGIN_ROOT / "gamebridge/launcher.py"),
        "--provider",
        "mihoyo",
        "--game-id",
        "genshin",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-id", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    if not command:
        return 2
    root_data = Path.home() / "homebrew" / "data" / "GameBridge" / "data"
    genshin_router = _global_genshin_router(root_data, arguments.game_id)
    if genshin_router is not None:
        # Global Genshin always needs the unified router and isolated prefix.
        os.execvp(genshin_router[0], genshin_router)
        return 7
    provider = HoYoPlayProvider(
        root_data / "providers" / "mihoyo-cn",
        root_data / "compatibility",
        MIHOYO_CN,
    )
    try:
        provider.apply_selected_channel(arguments.game_id)
    except (OSError, ValueError, RuntimeError):
        # Fail closed: launching the wrong account channel is worse than asking
        # the user to retry after GameBridge repairs the selected profile.
        return 6
    os.execvp(command[0], command)
    return 7


if __name__ == "__main__":
    raise SystemExit(main())
