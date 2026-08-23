from __future__ import annotations

import os
import re
import shlex

_ENVIRONMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_MANAGED_FLAGS = {"--provider", "--game-id", "--language", "--game-wrapper"}


def _is_framegen_wrapper(value: str) -> bool:
    expanded = os.path.expanduser(value)
    return expanded.endswith(("/fgmod/fgmod", "/fgmod/fgmod-uninstaller.sh"))


def _tokens(raw: str) -> list[str]:
    # Some Decky launch-option writers append their snippet without a leading
    # space (for example ``--game-id abc~/lsfg %command%``). Restore only the
    # known wrapper boundaries before shell parsing so the game id is not
    # corrupted.
    markers = (
        r"~/fgmod/fgmod-uninstaller\.sh",
        r"~/fgmod/fgmod(?!-uninstaller\.sh)",
        re.escape("~/lsfg"),
        re.escape("%command%"),
    )
    for marker in markers:
        raw = re.sub(rf"(?<!\s)({marker})", r" \1", raw)
        raw = re.sub(rf"({marker})(?!\s|$)", r"\1 ", raw)
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ValueError("launch_options.invalid") from exc


def managed_launch_tokens(provider: str, game_id: str) -> list[str]:
    return ["gamebridge/launcher.py", "--provider", provider, "--game-id", game_id]


def preset_launch_options(preset: str, provider: str, game_id: str) -> str:
    """Build one of the user-facing, device-tested launch presets."""
    sources = {
        "default": "",
        "lsfg": "~/lsfg %command%",
        "framegen": "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%",
        "combined": "~/fgmod/fgmod ~/lsfg %command%",
    }
    try:
        source = sources[preset]
    except KeyError as exc:
        raise ValueError("launch_options.invalid_preset") from exc
    return repair_launch_options(source, provider, game_id)


def _steam_join(items: list[str]) -> str:
    """Quote tokens without disabling SteamOS home-directory expansion."""
    rendered: list[str] = []
    for item in items:
        if re.fullmatch(r"~/[A-Za-z0-9_./-]+", item):
            rendered.append(item)
        else:
            rendered.append(shlex.quote(item))
    return " ".join(rendered)


def repair_launch_options(raw: str, provider: str, game_id: str) -> str:
    """Repair GameBridge's command while retaining user-owned launch modifiers.

    Steam modifiers such as environment assignments and wrapper commands live
    around ``%command%``.  GameBridge owns only its launcher script and flags;
    everything else is retained in its original token order.
    """
    source = _tokens(raw) if raw.strip() else []
    had_command = "%command%" in source
    custom: list[str] = []
    index = 0
    while index < len(source):
        token = source[index]
        if token.endswith("gamebridge/launcher.py"):
            index += 1
            while index < len(source):
                if source[index] in _MANAGED_FLAGS and index + 1 < len(source):
                    if source[index] == "--game-wrapper":
                        custom.append(source[index + 1])
                    index += 2
                    continue
                break
            continue
        custom.append(token)
        index += 1

    # Steam replaces ``%command%`` with the shortcut executable (python3).
    # Therefore wrappers must precede the placeholder and GameBridge's script
    # arguments must follow it: ``~/lsfg %command% gamebridge/launcher.py ...``.
    custom = [item for item in custom if item != "--"]
    game_wrappers: list[str] = []
    environments: list[str] = []
    lsfg_wrappers: list[str] = []
    steam_modifiers: list[str] = []
    for item in custom:
        expanded = os.path.expanduser(item)
        if _is_framegen_wrapper(item):
            if item not in game_wrappers:
                game_wrappers.append(item)
        elif _ENVIRONMENT.fullmatch(item):
            if item not in environments:
                environments.append(item)
        elif expanded.endswith("/lsfg"):
            if item not in lsfg_wrappers:
                lsfg_wrappers.append(item)
        elif item != "%command%":
            steam_modifiers.append(item)
    managed = managed_launch_tokens(provider, game_id)
    for wrapper in game_wrappers:
        managed.extend(["--game-wrapper", wrapper])
    prefix = [*environments, *lsfg_wrappers]
    if not prefix and not steam_modifiers and not had_command:
        return _steam_join(managed)
    return _steam_join([*prefix, "%command%", *managed, *steam_modifiers])


def launch_environment_tokens(raw: str) -> dict[str, str]:
    """Return explicit environment assignments for diagnostics and tests."""
    result: dict[str, str] = {}
    for token in _tokens(raw):
        if _ENVIRONMENT.fullmatch(token):
            key, value = token.split("=", 1)
            result[key] = value
    return result
