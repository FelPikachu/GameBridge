#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Steam launches this file directly, so Python initially adds gamebridge/ rather
# than the plugin root to sys.path. Add the parent before importing the package.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if os.fspath(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PLUGIN_ROOT))

from gamebridge.compatibility import CompatibilityManager  # noqa: E402, I001
from gamebridge.cloud_saves import EpicCloudSaveManager  # noqa: E402, I001
from gamebridge.play_history import merge_history_store  # noqa: E402, I001
from gamebridge.providers.hoyoplay import (  # noqa: E402, I001
    HOYOPLAY_GLOBAL,
    MIHOYO_CN,
    HoYoPlayProvider,
    LauncherSpec,
)
from gamebridge.storage import (  # noqa: E402, I001
    StorageRoot,
    ensure_wine_storage_drive,
)


LANGUAGE_ENVIRONMENTS = {
    "zh-CN": {"LANG": "zh_CN.UTF-8", "LANGUAGE": "zh_CN:zh", "LC_ALL": "zh_CN.UTF-8"},
    "zh-TW": {"LANG": "zh_TW.UTF-8", "LANGUAGE": "zh_TW:zh", "LC_ALL": "zh_TW.UTF-8"},
}


def _split_glued_modifiers(tokens: list[str]) -> list[str]:
    """Split known Decky wrappers accidentally glued to a managed argument."""
    result: list[str] = []
    for token in tokens:
        parts = [token]
        for marker in ("~/fgmod/fgmod", "~/lsfg"):
            separated: list[str] = []
            for part in parts:
                boundary = part.find(marker)
                if boundary > 0:
                    separated.extend([part[:boundary], part[boundary:]])
                else:
                    separated.append(part)
            parts = separated
        result.extend(part for part in parts if part)
    return result


def _launch_modifiers(tokens: list[str]) -> tuple[dict[str, str], str | None]:
    """Translate Decky-style options into an env overlay and Legendary wrapper."""
    environment: dict[str, str] = {}
    wrappers: list[str] = []
    before_command = True
    for item in tokens:
        if item == "%command%":
            before_command = False
            continue
        if before_command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item):
            key, value = item.split("=", 1)
            environment[key] = value
            continue
        if before_command:
            wrappers.append(os.path.expanduser(item))
    return environment, shlex.join(wrappers) if wrappers else None


def _compose_game_wrapper(
    configured: list[str], legacy: str | None, user_home: Path
) -> str | None:
    wrappers = [os.path.expanduser(value) for value in configured]
    if legacy:
        wrappers.extend(shlex.split(legacy))
    if not wrappers:
        return None
    # Decky Framegen's uninstaller removes itself after a successful one-shot
    # run. Ignore that stale managed wrapper on later launches.
    wrappers = [
        value
        for value in wrappers
        if not value.endswith("/fgmod/fgmod-uninstaller.sh") or Path(value).is_file()
    ]
    if not wrappers:
        return None
    if any(
        value.endswith(("/fgmod/fgmod", "/fgmod/fgmod-uninstaller.sh"))
        for value in wrappers
    ):
        wrappers = [
            "/usr/bin/env",
            f"HOME={user_home}",
            f"XDG_CONFIG_HOME={user_home / '.config'}",
            f"XDG_CACHE_HOME={user_home / '.cache'}",
            *wrappers,
        ]
    return shlex.join(wrappers)


def _apply_lsfg_paths(environment: dict[str, str], user_home: Path) -> None:
    if environment.get("LSFG_PROCESS"):
        environment["XDG_CONFIG_HOME"] = os.fspath(user_home / ".config")
        # The LSFG Vulkan implicit-layer manifest is installed below the real
        # user's data directory. GameBridge keeps HOME isolated for Legendary,
        # so explicitly expose that XDG location to the Vulkan loader.
        environment["XDG_DATA_HOME"] = os.fspath(user_home / ".local" / "share")


def _recover_embedded_lsfg(
    environment: dict[str, str],
    tokens: list[str],
    user_home: Path,
    game_wrappers: list[str] | None = None,
) -> bool:
    """Recover misplaced LSFG and Framegen modifiers from Steam's argument tail."""
    requested = any(os.path.expanduser(item).endswith("/lsfg") for item in tokens)
    if not requested:
        return False
    for item in tokens:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item):
            key, value = item.split("=", 1)
            environment[key] = value
        elif game_wrappers is not None and os.path.expanduser(item).endswith(
            ("/fgmod/fgmod", "/fgmod/fgmod-uninstaller.sh")
        ):
            expanded = os.path.expanduser(item)
            if expanded not in game_wrappers:
                game_wrappers.append(expanded)
    script = user_home / "lsfg"
    try:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, value = line[len("export "):].split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            environment[key] = value.strip().strip("'\"")
    return True


def _write_log(directory: Path, message: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "launch.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now(UTC).isoformat()} {message}\n")


def _record_last_played(root_data: Path, provider_id: str, game_id: str) -> None:
    try:
        merge_history_store(
            root_data / "play-history.json",
            [{
                "providerId": provider_id,
                "externalGameId": game_id,
                "playtimeMinutes": 0,
                "lastPlayed": int(datetime.now(UTC).timestamp()),
            }],
        )
    except OSError:
        # A read-only data mount must not turn a successful game session into a
        # launcher failure. Steam playtime remains authoritative.
        pass


def _legendary_launch_spec(payload: object) -> tuple[list[str], Path, dict[str, str], list[str] | None, bool]:
    """Convert Legendary's JSON launch plan into validated process arguments."""
    if not isinstance(payload, dict):
        raise ValueError("Legendary returned an invalid launch plan")

    def string_list(name: str) -> list[str]:
        value = payload.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Legendary launch plan has an invalid {name}")
        return value

    launch_command = string_list("launch_command")
    game_parameters = string_list("game_parameters")
    user_parameters = string_list("user_parameters")
    egl_parameters = string_list("egl_parameters")
    game_directory = payload.get("game_directory")
    game_executable = payload.get("game_executable")
    working_directory = payload.get("working_directory")
    environment = payload.get("environment", {})
    if (
        not launch_command
        or not isinstance(game_directory, str)
        or not isinstance(game_executable, str)
        or not isinstance(working_directory, str)
        or not isinstance(environment, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())
    ):
        raise ValueError("Legendary returned an incomplete launch plan")
    executable = os.path.join(game_directory, game_executable)
    command = [
        *launch_command,
        executable,
        *game_parameters,
        *user_parameters,
        *egl_parameters,
    ]
    pre_launch = payload.get("pre_launch_command")
    if pre_launch is not None and not isinstance(pre_launch, str):
        raise ValueError("Legendary launch plan has an invalid pre-launch command")
    return (
        command,
        Path(working_directory),
        dict(environment),
        shlex.split(pre_launch) if pre_launch else None,
        payload.get("pre_launch_wait") is True,
    )


def _steam_app_id(root_data: Path, provider: str, game_id: str) -> int | None:
    try:
        with sqlite3.connect(root_data / "gamebridge.db") as connection:
            row = connection.execute(
                "SELECT steam_app_id FROM steam_shortcuts "
                "WHERE provider_id=? AND external_game_id=?",
                (provider, game_id),
            ).fetchone()
        return int(row[0]) if row else None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _runtime_language(root_data: Path, provider: str, game_id: str, fallback: str) -> str:
    try:
        with sqlite3.connect(root_data / "gamebridge.db") as connection:
            row = connection.execute(
                "SELECT profile_json FROM runtime_profiles WHERE game_id=?",
                (f"{provider}:{game_id}",),
            ).fetchone()
        if row:
            language = json.loads(row[0]).get("language")
            if isinstance(language, str) and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", language):
                return language
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        pass
    return fallback


def _ensure_hoyoplay_game_drive(
    prefix: Path,
    executable: Path,
    roots: list[StorageRoot] | None = None,
) -> Path | None:
    return ensure_wine_storage_drive(prefix, executable, roots)


def _is_hoyoplay_target(spec: LauncherSpec, game_id: str) -> bool:
    return game_id in {"installer", "launcher", *(game.external_game_id for game in spec.games)}


def _unified_hoyoplay_selection(root_data: Path) -> str:
    try:
        selection = (root_data / "mihoyo-selection").read_text(encoding="utf-8").strip()
    except OSError:
        try:
            selection = (
                root_data / "providers/mihoyo-cn/channel-profiles/selected"
            ).read_text(encoding="utf-8").strip()
        except OSError:
            return "official"
    if selection not in {"official", "bilibili", "global"}:
        raise ValueError("invalid unified HoYoPlay selection")
    return selection


def _unified_hoyoplay_route(
    root_data: Path, game_id: str, selection: str | None = None
) -> tuple[str, str]:
    routes = {
        "genshin": ("hk4e_cn", "hk4e_global"),
        "zzz": ("nap_cn", "nap_global"),
        "starrail": ("hkrpg_cn", "hkrpg_global"),
        "honkai3": ("bh3_cn", "bh3_global"),
    }
    if game_id not in routes:
        raise ValueError("unknown unified HoYoPlay game")
    selection = selection or _unified_hoyoplay_selection(root_data)
    if selection not in {"official", "bilibili", "global"}:
        raise ValueError("invalid unified HoYoPlay selection")
    cn_game, global_game = routes[game_id]
    return ("hoyoplay_global", global_game) if selection == "global" else ("mihoyo_cn", cn_game)


def _newest_dwproton(compatibility: CompatibilityManager) -> tuple[str, Path] | None:
    candidates = [
        (name, path)
        for name, path in compatibility.proton_layers()
        if name.casefold().startswith("dwproton-")
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: tuple(int(value) for value in re.findall(r"\d+", item[0])),
    )


def _bh3_runtime(
    compatibility: CompatibilityManager,
) -> tuple[str, Path] | None:
    for name, path in compatibility.proton_layers():
        if re.fullmatch(r"GE-Proton11-5(?:-x86_64)?", name, flags=re.IGNORECASE):
            return name, path
    return None


def _hoyoplay_launch_prefix(
    provider_prefix: Path,
    provider_id: str,
    steam_app_id: int | None,
    *,
    uses_game_runtime: bool = False,
) -> Path:
    if provider_id == "mihoyo_cn" and uses_game_runtime and steam_app_id is not None:
        return (
            Path.home()
            / ".local/share/Steam/steamapps/compatdata"
            / str(steam_app_id)
            / "pfx"
        )
    return provider_prefix


def _hoyoplay_umu_id(provider_id: str, game_id: str) -> str:
    if provider_id != "hoyoplay_global":
        return "umu-default"
    # These standalone IDs are published by ProtonFixes. Using umu-default
    # identifies the executables as unknown games and skips their maintained
    # compatibility environment.
    return {
        "hk4e_global": "umu-genshin",
        "nap_global": "umu-zenlesszonezero",
    }.get(game_id, "umu-default")


def _hoyoplay_launch_executable(
    provider: HoYoPlayProvider, game_id: str
) -> Path | None:
    if game_id == "installer":
        return provider.managed_installer
    if game_id == "launcher":
        return provider.launcher_executable()
    installation = provider.game_installation(game_id)
    executable = installation.get("executable")
    if installation.get("launchable") and isinstance(executable, str):
        candidate = Path(executable)
        if candidate.is_file():
            return candidate
    launcher = provider.launcher_executable()
    if launcher is not None:
        return launcher
    return provider.managed_installer if provider.managed_installer.is_file() else None


def _hoyoplay_uses_game_runtime(
    provider: HoYoPlayProvider, game_id: str, executable: Path
) -> bool:
    if game_id in {"installer", "launcher"}:
        return False
    launcher = provider.launcher_executable()
    return launcher is None or executable != launcher


def _hoyoplay_uses_nested_steam_proton(provider_id: str, game_id: str) -> bool:
    """Use Steam's proven direct-Proton path only for standalone game EXEs."""
    return (
        provider_id == "mihoyo_cn"
        and game_id in {"hk4e_cn", "nap_cn", "hkrpg_cn", "bh3_cn"}
    ) or (provider_id == "hoyoplay_global" and game_id == "bh3_global")


def _hoyoplay_uses_shared_dwproton(provider_id: str) -> bool:
    """Keep every process using the shared CN prefix on one Proton family."""
    return provider_id == "mihoyo_cn"


def _official_client_command(
    umu_executable: Path,
    executable: Path,
    *,
    inhibit_sleep: bool,
    arguments: list[str] | None = None,
) -> list[str]:
    command = [os.fspath(umu_executable), os.fspath(executable), *(arguments or [])]
    inhibitor = shutil.which("systemd-inhibit")
    if not inhibit_sleep or inhibitor is None:
        return command
    return [
        inhibitor,
        "--what=sleep:shutdown",
        "--who=GameBridge",
        "--why=Official game download is active",
        "--mode=block",
        *command,
    ]


def _hoyoplay_working_directory(executable: Path) -> Path:
    """Keep channel SDK relative lookups rooted beside the launched client."""
    return executable.parent


def _uses_unified_cn_channel_switch(
    shortcut_provider: str,
    shortcut_game_id: str,
    resolved_provider: str,
) -> bool:
    return (
        shortcut_provider == "mihoyo"
        and shortcut_game_id in {"genshin", "zzz", "starrail"}
        and resolved_provider == "mihoyo_cn"
    )


def _steam_proton_command(
    runtime_path: Path,
    executable: Path,
    arguments: list[str] | None = None,
) -> list[str]:
    entry = (
        Path.home()
        / ".local/share/Steam/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point"
    )
    proton = runtime_path / "proton"
    if not entry.is_file() or not proton.is_file():
        raise FileNotFoundError("Steam Proton runtime is incomplete")
    return [
        os.fspath(entry),
        "--verb=waitforexitandrun",
        "--",
        os.fspath(proton),
        "waitforexitandrun",
        os.fspath(executable),
        *(arguments or []),
    ]


def _steam_compat_data_path(launch_prefix: Path) -> Path:
    return launch_prefix.parent if launch_prefix.name == "pfx" else launch_prefix


def _hoyoplay_wine_prefix(
    launch_prefix: Path, provider_id: str, game_id: str
) -> Path:
    if provider_id == "hoyoplay_global" and game_id == "bh3_global":
        return launch_prefix / "pfx"
    return launch_prefix


def main() -> int:
    user_home = Path.home()
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--game-id", required=True)
    # Kept for shortcuts made by older releases. New shortcuts store language
    # in runtime_profiles so repairing their command never changes user options.
    parser.add_argument("--language", default="en")
    parser.add_argument("--game-wrapper", action="append", default=[])
    parser.add_argument("launch_modifiers", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(_split_glued_modifiers(sys.argv[1:]))
    if arguments.provider in {"mihoyo", "mihoyo_cn", "hoyoplay_global"}:
        root_data = Path.home() / "homebrew" / "data" / "GameBridge" / "data"
        compatibility = CompatibilityManager(root_data / "compatibility")
        shortcut_provider = arguments.provider
        shortcut_game_id = arguments.game_id
        unified_selection = None
        if arguments.provider == "mihoyo":
            try:
                unified_selection = _unified_hoyoplay_selection(root_data)
                arguments.provider, arguments.game_id = _unified_hoyoplay_route(
                    root_data, arguments.game_id, unified_selection
                )
            except ValueError:
                return 2
        spec = MIHOYO_CN if arguments.provider == "mihoyo_cn" else HOYOPLAY_GLOBAL
        if not _is_hoyoplay_target(spec, arguments.game_id):
            return 2
        data_name = "mihoyo-cn" if arguments.provider == "mihoyo_cn" else "hoyoplay-global"
        provider = HoYoPlayProvider(
            root_data / "providers" / data_name,
            root_data / "compatibility",
            spec,
        )
        if arguments.game_id not in {"installer", "launcher"}:
            provider.repair_completed_install_metadata(arguments.game_id)
            if arguments.provider == "mihoyo_cn":
                try:
                    if _uses_unified_cn_channel_switch(
                        shortcut_provider, shortcut_game_id, arguments.provider
                    ) and unified_selection in {"official", "bilibili"}:
                        provider.apply_channel_for_launch(
                            arguments.game_id, unified_selection
                        )
                    else:
                        provider.apply_selected_channel(arguments.game_id)
                except (OSError, ValueError, RuntimeError):
                    return 6
        else:
            for game in spec.games:
                provider.repair_completed_install_metadata(game.external_game_id)
        executable = _hoyoplay_launch_executable(provider, arguments.game_id)
        if executable is None or not executable.is_file():
            return 3
        blocker = provider.storage_blocker()
        if blocker is not None:
            _write_log(
                root_data / "compatibility" / "logs",
                f"blocked {arguments.provider}:{arguments.game_id} storage={blocker.get('state')}",
            )
            return 5
        partial_download = bool(provider.partial_installations())
        unified_cn_games = {
            "genshin": "hk4e_cn",
            "zzz": "nap_cn",
            "starrail": "hkrpg_cn",
            "honkai3": "bh3_cn",
        }
        steam_app_id = (
            _steam_app_id(root_data, "mihoyo_cn", unified_cn_games[shortcut_game_id])
            if shortcut_provider == "mihoyo" and shortcut_game_id in unified_cn_games
            else None
        )
        uses_game_runtime = _hoyoplay_uses_game_runtime(
            provider, arguments.game_id, executable
        )
        launch_prefix = _hoyoplay_launch_prefix(
            provider.prefix_directory,
            arguments.provider,
            steam_app_id,
            uses_game_runtime=uses_game_runtime,
        )
        _ensure_hoyoplay_game_drive(launch_prefix, executable)
        try:
            if not compatibility.umu_executable.is_file():
                compatibility.prepare()
            if arguments.game_id not in {"installer", "launcher"}:
                compatibility.ensure_hoyoplay_runtime(arguments.game_id)
            selected_game = (
                arguments.game_id if uses_game_runtime else "launcher"
            )
            runtime = None
            if uses_game_runtime and arguments.game_id in {"bh3_cn", "bh3_global"}:
                runtime = _bh3_runtime(compatibility)
            elif _hoyoplay_uses_shared_dwproton(arguments.provider):
                runtime = _newest_dwproton(compatibility)
            elif selected_game != "launcher":
                runtime = (
                    _bh3_runtime(compatibility)
                    if arguments.provider == "hoyoplay_global"
                    and arguments.game_id == "bh3_global"
                    else _newest_dwproton(compatibility)
                )
            runtime_name, runtime_path = runtime or compatibility.selected_proton(
                arguments.provider, selected_game,
                steam_app_id,
            )
        except (OSError, ValueError, RuntimeError):
            return 4
        is_bh3_global = (
            arguments.provider == "hoyoplay_global"
            and arguments.game_id == "bh3_global"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "WINEPREFIX": os.fspath(
                    _hoyoplay_wine_prefix(
                        launch_prefix, arguments.provider, arguments.game_id
                    )
                ),
                "GAMEID": _hoyoplay_umu_id(arguments.provider, arguments.game_id),
                "STORE": "none",
                "PROTONPATH": os.fspath(runtime_path),
                "UMU_LOG": "1",
            }
        )
        if shortcut_provider == "mihoyo" and shortcut_game_id in unified_cn_games:
            if not is_bh3_global:
                environment["WINE_ENABLE_TIMEOUT_FIX"] = "1"
            environment["STEAM_COMPAT_INSTALL_PATH"] = os.fspath(executable.parent)
            environment["PROTON_VERB"] = "waitforexitandrun"
            environment["STEAM_COMPAT_DATA_PATH"] = os.fspath(
                _steam_compat_data_path(launch_prefix)
            )
            environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.fspath(
                Path.home() / ".local/share/Steam"
            )
            if steam_app_id is not None:
                environment["SteamAppId"] = str(steam_app_id % (2**32))
                environment["SteamGameId"] = str(steam_app_id % (2**32))
        log_directory = root_data / "compatibility" / "logs"
        _write_log(
            log_directory,
            f"launch {arguments.provider}:{arguments.game_id} with {runtime_name}",
        )
        with (log_directory / f"{arguments.provider}-{arguments.game_id}.log").open(
            "ab"
        ) as log_stream:
            launcher_arguments = (
                [f"--game={arguments.game_id}"]
                if arguments.game_id not in {"installer", "launcher"}
                and not _hoyoplay_uses_game_runtime(
                    provider, arguments.game_id, executable
                )
                else []
            )
            if (
                shortcut_provider == "mihoyo"
                and shortcut_game_id in unified_cn_games
                and _hoyoplay_uses_nested_steam_proton(
                    arguments.provider, arguments.game_id
                )
                and isinstance(runtime_path, Path)
            ):
                command = _steam_proton_command(
                    runtime_path, executable, arguments=launcher_arguments
                )
            else:
                command = _official_client_command(
                    compatibility.umu_executable,
                    executable,
                    inhibit_sleep=partial_download,
                    arguments=launcher_arguments,
                )
            result = subprocess.run(
                command,
                cwd=_hoyoplay_working_directory(executable),
                env=environment,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        _write_log(log_directory, f"exit {arguments.provider}:{arguments.game_id} {result.returncode}")
        return result.returncode
    if arguments.provider != "epic":
        return 2
    if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", arguments.language):
        return 2

    root_data = Path.home() / "homebrew" / "data" / "GameBridge" / "data"
    arguments.language = _runtime_language(
        root_data, arguments.provider, arguments.game_id, arguments.language
    )
    data_directory = root_data / "providers" / "epic"
    legendary = data_directory / "tools" / "legendary"
    if not legendary.is_file():
        return 3
    compatibility = CompatibilityManager(root_data / "compatibility")
    try:
        if not compatibility.umu_executable.is_file():
            compatibility.prepare()
        installed_file = data_directory / "config" / "legendary" / "installed.json"
        installations = json.loads(installed_file.read_text(encoding="utf-8"))
        installation = installations[arguments.game_id]
        title = str(installation.get("title") or arguments.game_id)
        install_path = Path(str(installation["install_path"])).expanduser().resolve()
        steam_app_id = _steam_app_id(root_data, "epic", arguments.game_id)
        runtime_name, runtime_path = compatibility.selected_proton(
            "epic", arguments.game_id, steam_app_id
        )
        prefix = compatibility.prefix("epic", arguments.game_id)
        prefix.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        _write_log(root_data / "compatibility" / "logs", f"prepare failed: {exc}")
        return 4
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": os.fspath(data_directory),
            "XDG_CONFIG_HOME": os.fspath(data_directory / "config"),
            "XDG_CACHE_HOME": os.fspath(data_directory / "cache"),
            "LEGENDARY_CONFIG_PATH": os.fspath(data_directory / "config" / "legendary"),
            "WINEPREFIX": os.fspath(prefix),
            "GAMEID": compatibility.umu_id(arguments.game_id, title),
            "STORE": "egs",
            "PROTONPATH": os.fspath(runtime_path),
            "STEAM_COMPAT_INSTALL_PATH": os.fspath(install_path),
            "STEAM_COMPAT_DATA_PATH": os.fspath(prefix),
            "PROTON_VERB": "waitforexitandrun",
        }
    )
    environment.update(LANGUAGE_ENVIRONMENTS.get(arguments.language, {}))
    embedded_lsfg = _recover_embedded_lsfg(
        environment,
        arguments.launch_modifiers,
        user_home,
        arguments.game_wrapper,
    )
    if embedded_lsfg:
        # Steam already expanded %command% into its launcher machinery. None of
        # those trailing tokens are game arguments; LSFG is represented by the
        # recovered environment above.
        arguments.launch_modifiers = []
    _apply_lsfg_paths(environment, user_home)
    modifier_environment, wrapper = _launch_modifiers(arguments.launch_modifiers)
    environment.update(modifier_environment)
    wrapper = _compose_game_wrapper(arguments.game_wrapper, wrapper, user_home)
    cloud_saves = EpicCloudSaveManager(root_data)
    try:
        cloud_before = cloud_saves.sync_before_launch(arguments.game_id)
        _write_log(
            root_data / "compatibility" / "logs",
            f"cloud download {arguments.game_id} {cloud_before.state}",
        )
    except Exception as exc:
        # Cloud availability must never prevent an owned local game from
        # launching. Persist only the normalized exception class, not CLI
        # output, paths, account ids, or authentication material.
        _write_log(
            root_data / "compatibility" / "logs",
            f"cloud download {arguments.game_id} failed {type(exc).__name__}",
        )
    _write_log(
        root_data / "compatibility" / "logs",
        f"launch {arguments.game_id} appid={steam_app_id} with {runtime_name} "
        f"({environment['GAMEID']})",
    )
    command = [
            os.fspath(legendary),
            "launch",
            arguments.game_id,
            "--language",
            arguments.language,
            "--wine",
            os.fspath(compatibility.umu_executable),
            "--wine-prefix",
            os.fspath(prefix),
        ]
    if wrapper:
        command.extend(["--wrapper", wrapper])
        _write_log(root_data / "compatibility" / "logs", f"wrapper {wrapper}")
    # Legendary intentionally detaches the launched game. Ask it for the
    # structured launch plan instead, then keep this Steam-owned process alive
    # until UMU/the game exits so Steam can account for the complete session.
    plan_result = subprocess.run(  # noqa: S603 -- fixed executable and argument array
        [*command, "--json"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if plan_result.returncode:
        _write_log(root_data / "compatibility" / "logs", f"launch plan failed {plan_result.returncode}")
        return plan_result.returncode
    try:
        launch_command, working_directory, launch_environment, pre_launch, pre_launch_wait = (
            _legendary_launch_spec(json.loads(plan_result.stdout))
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _write_log(root_data / "compatibility" / "logs", f"launch plan invalid: {exc}")
        return 5
    environment.update(launch_environment)
    if pre_launch:
        pre_process = subprocess.Popen(pre_launch, env=environment)  # noqa: S603 -- parsed trusted plan
        if pre_launch_wait:
            pre_process.wait()
    result = subprocess.run(  # noqa: S603 -- validated Legendary argument array
        launch_command,
        cwd=working_directory,
        env=environment,
        check=False,
    )
    try:
        cloud_after = cloud_saves.sync_after_exit(arguments.game_id)
        _write_log(
            root_data / "compatibility" / "logs",
            f"cloud upload {arguments.game_id} {cloud_after.state}",
        )
    except Exception as exc:
        _write_log(
            root_data / "compatibility" / "logs",
            f"cloud upload {arguments.game_id} failed {type(exc).__name__}",
        )
    _record_last_played(root_data, "epic", arguments.game_id)
    _write_log(root_data / "compatibility" / "logs", f"exit {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
