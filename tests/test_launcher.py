import os
from pathlib import Path

from gamebridge.launcher import (
    _apply_lsfg_paths,
    _bh3_runtime,
    _compose_game_wrapper,
    _ensure_hoyoplay_game_drive,
    _hoyoplay_launch_executable,
    _hoyoplay_launch_prefix,
    _hoyoplay_umu_id,
    _hoyoplay_uses_game_runtime,
    _hoyoplay_uses_nested_steam_proton,
    _hoyoplay_uses_shared_dwproton,
    _hoyoplay_wine_prefix,
    _hoyoplay_working_directory,
    _is_hoyoplay_target,
    _launch_modifiers,
    _legendary_launch_spec,
    _official_client_command,
    _recover_embedded_lsfg,
    _record_last_played,
    _split_glued_modifiers,
    _steam_compat_data_path,
    _steam_proton_command,
    _unified_hoyoplay_route,
    _uses_unified_cn_channel_switch,
)
from gamebridge.providers.hoyoplay import MIHOYO_CN, HoYoPlayProvider
from gamebridge.storage import StorageRoot
from gamebridge.play_history import read_history_store


def test_legendary_json_plan_becomes_a_structured_waited_command(tmp_path):
    command, working_directory, environment, pre_launch, pre_launch_wait = (
        _legendary_launch_spec({
            "launch_command": ["/opt/umu/umu-run"],
            "game_directory": str(tmp_path / "game"),
            "game_executable": "Game.exe",
            "game_parameters": ["-AUTH_PASSWORD=secret"],
            "user_parameters": ["-dx12"],
            "egl_parameters": ["-epicapp=test"],
            "working_directory": str(tmp_path / "game"),
            "environment": {"WINEPREFIX": str(tmp_path / "prefix")},
            "pre_launch_command": "/usr/bin/true --check",
            "pre_launch_wait": True,
        })
    )

    assert command == [
        "/opt/umu/umu-run",
        str(tmp_path / "game/Game.exe"),
        "-AUTH_PASSWORD=secret",
        "-dx12",
        "-epicapp=test",
    ]
    assert working_directory == tmp_path / "game"
    assert environment == {"WINEPREFIX": str(tmp_path / "prefix")}
    assert pre_launch == ["/usr/bin/true", "--check"]
    assert pre_launch_wait is True


def test_legendary_json_plan_rejects_string_commands(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="launch_command"):
        _legendary_launch_spec({
            "launch_command": "/opt/umu/umu-run Game.exe",
            "game_directory": str(tmp_path),
            "game_executable": "Game.exe",
            "working_directory": str(tmp_path),
            "environment": {},
        })


def test_epic_launcher_records_last_played_without_adding_minutes(tmp_path):
    before = int(__import__("time").time())
    _record_last_played(tmp_path, "epic", "lego")
    record = read_history_store(tmp_path / "play-history.json")[("epic", "lego")]
    assert record["playtimeMinutes"] == 0
    assert record["lastPlayed"] >= before


def test_unified_genshin_route_uses_cn_by_default(tmp_path):
    assert _unified_hoyoplay_route(tmp_path, "genshin") == ("mihoyo_cn", "hk4e_cn")


def test_unified_genshin_route_migrates_existing_bilibili_selection(tmp_path):
    legacy = tmp_path / "providers/mihoyo-cn/channel-profiles/selected"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("bilibili", encoding="utf-8")
    assert _unified_hoyoplay_route(tmp_path, "genshin") == ("mihoyo_cn", "hk4e_cn")


def test_unified_genshin_route_uses_global_selection(tmp_path):
    (tmp_path / "mihoyo-selection").write_text("global", encoding="utf-8")
    assert _unified_hoyoplay_route(tmp_path, "genshin") == (
        "hoyoplay_global", "hk4e_global"
    )


def test_other_unified_games_follow_global_selection(tmp_path):
    (tmp_path / "mihoyo-selection").write_text("global", encoding="utf-8")
    assert _unified_hoyoplay_route(tmp_path, "zzz") == (
        "hoyoplay_global", "nap_global"
    )
    assert _unified_hoyoplay_route(tmp_path, "starrail") == (
        "hoyoplay_global", "hkrpg_global"
    )
    assert _unified_hoyoplay_route(tmp_path, "honkai3") == (
        "hoyoplay_global", "bh3_global"
    )


def test_only_the_three_shared_cn_games_use_sdk_channel_switching():
    for game_id in ("genshin", "zzz", "starrail"):
        assert _uses_unified_cn_channel_switch("mihoyo", game_id, "mihoyo_cn")
        assert not _uses_unified_cn_channel_switch(
            "mihoyo", game_id, "hoyoplay_global"
        )
    assert not _uses_unified_cn_channel_switch("mihoyo", "honkai3", "mihoyo_cn")


def test_unified_hoyoplay_route_rejects_unknown_game(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="unknown unified"):
        _unified_hoyoplay_route(tmp_path, "unknown")


def test_unified_hoyoplay_route_rejects_corrupt_persisted_selection(tmp_path):
    import pytest

    (tmp_path / "mihoyo-selection").write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid unified HoYoPlay selection"):
        _unified_hoyoplay_route(tmp_path, "genshin")


def test_global_hoyoplay_never_reuses_cn_steam_prefix(tmp_path):
    provider_prefix = tmp_path / "global-prefix"
    assert _hoyoplay_launch_prefix(provider_prefix, "hoyoplay_global", 1234) == provider_prefix


def test_unified_cn_launcher_keeps_shared_provider_prefix(tmp_path):
    provider_prefix = tmp_path / "provider-prefix"
    assert _hoyoplay_launch_prefix(provider_prefix, "mihoyo_cn", 1234) == provider_prefix


def test_unified_cn_game_uses_its_preserved_steam_card_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert _hoyoplay_launch_prefix(
        tmp_path / "provider-prefix",
        "mihoyo_cn",
        1234,
        uses_game_runtime=True,
    ) == tmp_path / ".local/share/Steam/steamapps/compatdata/1234/pfx"


def test_global_hoyoplay_uses_published_standalone_umu_ids():
    assert _hoyoplay_umu_id("hoyoplay_global", "hk4e_global") == "umu-genshin"
    assert _hoyoplay_umu_id("hoyoplay_global", "nap_global") == "umu-zenlesszonezero"
    assert _hoyoplay_umu_id("hoyoplay_global", "hkrpg_global") == "umu-default"
    assert _hoyoplay_umu_id("mihoyo_cn", "hk4e_cn") == "umu-default"


def test_missing_hoyoplay_game_fallback_uses_launcher_runtime(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "provider", tmp_path / "compatibility", MIHOYO_CN
    )
    launcher = tmp_path / "launcher.exe"
    launcher.touch()
    provider.launcher_executable = lambda: launcher
    assert not _hoyoplay_uses_game_runtime(provider, "nap_cn", launcher)
    assert _hoyoplay_uses_game_runtime(provider, "nap_cn", tmp_path / "game.exe")


def test_only_proven_standalone_games_use_nested_steam_proton():
    assert _hoyoplay_uses_nested_steam_proton("hoyoplay_global", "bh3_global")
    assert _hoyoplay_uses_nested_steam_proton("mihoyo_cn", "hk4e_cn")
    assert _hoyoplay_uses_nested_steam_proton("mihoyo_cn", "nap_cn")
    assert _hoyoplay_uses_nested_steam_proton("mihoyo_cn", "hkrpg_cn")
    assert _hoyoplay_uses_nested_steam_proton("mihoyo_cn", "bh3_cn")
    assert not _hoyoplay_uses_nested_steam_proton("hoyoplay_global", "hkrpg_global")


def test_all_cn_hoyoplay_routes_share_dwproton_family():
    assert _hoyoplay_uses_shared_dwproton("mihoyo_cn")
    assert not _hoyoplay_uses_shared_dwproton("hoyoplay_global")


def test_official_client_command_forwards_structured_launcher_arguments(tmp_path):
    assert _official_client_command(
        tmp_path / "umu-run",
        tmp_path / "launcher.exe",
        inhibit_sleep=False,
        arguments=["--game=hkrpg_global"],
    ) == [
        str(tmp_path / "umu-run"),
        str(tmp_path / "launcher.exe"),
        "--game=hkrpg_global",
    ]


def test_hoyoplay_launches_from_the_executable_directory(tmp_path):
    executable = tmp_path / "games/Genshin Impact Game/YuanShen.exe"
    assert _hoyoplay_working_directory(executable) == executable.parent


def test_steam_proton_command_preserves_verified_runtime_order(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    entry = tmp_path / ".local/share/Steam/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point"
    proton = tmp_path / "dwproton/proton"
    executable = tmp_path / "game/Game.exe"
    for path in (entry, proton, executable):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert _steam_proton_command(proton.parent, executable) == [
        str(entry), "--verb=waitforexitandrun", "--",
        str(proton), "waitforexitandrun", str(executable),
    ]


def test_steam_proton_command_forwards_structured_launcher_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    entry = tmp_path / ".local/share/Steam/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point"
    proton = tmp_path / "dwproton/proton"
    launcher = tmp_path / "launcher/launcher.exe"
    for path in (entry, proton, launcher):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    assert _steam_proton_command(
        proton.parent, launcher, arguments=["--game=bh3_cn"]
    )[-2:] == [str(launcher), "--game=bh3_cn"]


def test_steam_compat_data_path_accepts_prefix_root_and_pfx_directory(tmp_path):
    root_prefix = tmp_path / "hoyoplay-global"
    steam_pfx = tmp_path / "compatdata/1234/pfx"

    assert _steam_compat_data_path(root_prefix) == root_prefix
    assert _steam_compat_data_path(steam_pfx) == steam_pfx.parent


def test_bh3_global_uses_proton_pfx_inside_its_isolated_compat_data(tmp_path):
    global_prefix = tmp_path / "hoyoplay-global"

    assert _hoyoplay_wine_prefix(
        global_prefix, "hoyoplay_global", "bh3_global"
    ) == global_prefix / "pfx"
    assert _hoyoplay_wine_prefix(
        global_prefix, "hoyoplay_global", "hkrpg_global"
    ) == global_prefix


def test_bh3_prefers_device_validated_ge_proton_11_5(tmp_path, monkeypatch):
    runtime = tmp_path / "GE-Proton11-5-x86_64"
    fallback = tmp_path / "dwproton-11.0-11-x86_64"
    monkeypatch.setattr(
        "gamebridge.compatibility.CompatibilityManager.proton_layers",
        lambda _self: [
            ("dwproton-11.0-11-x86_64", fallback),
            ("GE-Proton11-5-x86_64", runtime),
        ],
    )

    from gamebridge.compatibility import CompatibilityManager

    assert _bh3_runtime(CompatibilityManager(tmp_path / "compatibility")) == (
        "GE-Proton11-5-x86_64",
        runtime,
    )


def test_decky_wrappers_are_forwarded_to_the_real_game_command():
    environment, wrapper = _launch_modifiers(
        ["Dx12Upscaler=fsr31", "~/fgmod/fgmod", "~/lsfg", "%command%"]
    )
    assert environment == {"Dx12Upscaler": "fsr31"}
    assert wrapper is not None
    assert "/fgmod/fgmod" in wrapper
    assert "/lsfg" in wrapper
    assert "%command%" not in wrapper


def test_game_arguments_are_not_mistaken_for_wrappers():
    environment, wrapper = _launch_modifiers(["~/lsfg", "%command%", "-dx12"])
    assert environment == {}
    assert wrapper is not None and wrapper.endswith("/lsfg")


def test_fgmod_wrapper_receives_the_real_user_home():
    wrapper = _compose_game_wrapper(
        ["/home/deck/fgmod/fgmod"], None, Path("/home/deck")
    )
    assert wrapper == (
        "/usr/bin/env HOME=/home/deck XDG_CONFIG_HOME=/home/deck/.config "
        "XDG_CACHE_HOME=/home/deck/.cache /home/deck/fgmod/fgmod"
    )


def test_framegen_uninstaller_is_deferred_when_present(tmp_path):
    uninstaller = tmp_path / "fgmod" / "fgmod-uninstaller.sh"
    uninstaller.parent.mkdir()
    uninstaller.touch()
    wrapper = _compose_game_wrapper([str(uninstaller)], None, tmp_path)
    assert wrapper is not None
    assert f"HOME={tmp_path}" in wrapper
    assert wrapper.endswith(str(uninstaller))


def test_removed_one_shot_framegen_uninstaller_is_ignored(tmp_path):
    missing = tmp_path / "fgmod" / "fgmod-uninstaller.sh"
    assert _compose_game_wrapper([str(missing)], None, tmp_path) is None


def test_lsfg_uses_the_plugin_managed_user_configuration():
    environment = {
        "LSFG_PROCESS": "decky-lsfg-vk",
        "XDG_CONFIG_HOME": "/isolated/provider/config",
    }
    _apply_lsfg_paths(environment, Path("/home/deck"))
    assert environment["XDG_CONFIG_HOME"] == "/home/deck/.config"
    assert environment["XDG_DATA_HOME"] == "/home/deck/.local/share"


def test_games_without_lsfg_keep_the_provider_configuration_isolated():
    environment = {"XDG_CONFIG_HOME": "/isolated/provider/config"}
    _apply_lsfg_paths(environment, Path("/home/deck"))
    assert environment["XDG_CONFIG_HOME"] == "/isolated/provider/config"
    assert "XDG_DATA_HOME" not in environment


def test_wrong_order_lsfg_is_recovered_without_running_steam_command_twice(tmp_path):
    script = tmp_path / "lsfg"
    script.write_text(
        "#!/bin/bash\nexport ENABLE_GAMESCOPE_WSI=0\n"
        "export LSFG_PROCESS=decky-lsfg-vk\nexec \"$@\"\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}
    recovered = _recover_embedded_lsfg(
        environment,
        [str(script), "/usr/bin/python3", "gamebridge/launcher.py"],
        tmp_path,
    )
    assert recovered is True
    assert environment["LSFG_PROCESS"] == "decky-lsfg-vk"
    assert environment["ENABLE_GAMESCOPE_WSI"] == "0"


def test_wrong_order_combination_recovers_framegen_and_environment(tmp_path):
    script = tmp_path / "lsfg"
    script.write_text(
        "#!/bin/bash\nexport LSFG_PROCESS=decky-lsfg-vk\nexec \"$@\"\n",
        encoding="utf-8",
    )
    fgmod = tmp_path / "fgmod" / "fgmod"
    environment: dict[str, str] = {}
    wrappers: list[str] = []
    recovered = _recover_embedded_lsfg(
        environment,
        [str(fgmod), "%command%", "SteamDeck=0", str(script)],
        tmp_path,
        wrappers,
    )
    assert recovered is True
    assert environment["SteamDeck"] == "0"
    assert environment["LSFG_PROCESS"] == "decky-lsfg-vk"
    assert wrappers == [str(fgmod)]


def test_runtime_splits_lsfg_glued_to_game_id():
    assert _split_glued_modifiers(
        ["--game-id", "lego-id~/lsfg", "%command%"]
    ) == ["--game-id", "lego-id", "~/lsfg", "%command%"]


def test_hoyoplay_game_drive_maps_the_external_root_containing_launcher(tmp_path):
    prefix = tmp_path / "prefix"
    first = tmp_path / "first"
    game = tmp_path / "Game"
    executable = game / "miHoYo Launcher" / "launcher.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    first.mkdir()

    selected = _ensure_hoyoplay_game_drive(
        prefix,
        executable,
        [StorageRoot(first, "first"), StorageRoot(game, "game")],
    )

    assert selected == game
    assert (prefix / "dosdevices/g:").resolve() == game


def test_hoyoplay_game_drive_does_not_replace_an_existing_mapping(tmp_path):
    prefix = tmp_path / "prefix"
    existing = tmp_path / "existing"
    requested = tmp_path / "requested"
    existing.mkdir()
    requested.mkdir()
    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(existing, target_is_directory=True)

    selected = _ensure_hoyoplay_game_drive(
        prefix,
        requested / "launcher.exe",
        [StorageRoot(requested, "requested")],
    )

    assert selected is None
    assert (dosdevices / "g:").resolve() == existing


def test_hoyoplay_shortcuts_accept_public_games_but_reject_unknown_targets():
    assert _is_hoyoplay_target(MIHOYO_CN, "launcher") is True
    assert _is_hoyoplay_target(MIHOYO_CN, "hk4e_cn") is True
    assert _is_hoyoplay_target(MIHOYO_CN, "unknown_game") is False


def test_hoyoplay_launch_target_uses_game_exe_only_when_provider_marks_it_launchable(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    launcher = provider.prefix_directory / MIHOYO_CN.executable_candidates[0]
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"MZ")
    game_drive = tmp_path / "game-drive"
    genshin = game_drive / "Genshin"
    star_rail = game_drive / "StarRail"
    genshin.mkdir(parents=True)
    star_rail.mkdir(parents=True)
    genshin_exe = genshin / "YuanShen.exe"
    genshin_exe.write_bytes(b"MZ")
    (star_rail / "StarRail.exe").write_bytes(b"MZ")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin"\n\n'
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hkrpg_cn]\n'
        '"GameInstallPath"="G:\\\\StarRail"\n',
        encoding="utf-8",
    )

    assert _hoyoplay_launch_executable(provider, "hk4e_cn") == genshin_exe
    assert _hoyoplay_launch_executable(provider, "hkrpg_cn") == star_rail / "StarRail.exe"


def test_hoyoplay_game_falls_back_to_downloaded_installer(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    provider.managed_installer.parent.mkdir(parents=True)
    provider.managed_installer.write_bytes(b"MZ")

    assert _hoyoplay_launch_executable(provider, "hk4e_cn") == provider.managed_installer


def test_official_client_download_uses_argument_array_sleep_inhibitor(tmp_path, monkeypatch):
    inhibitor = tmp_path / "systemd-inhibit"
    inhibitor.touch()
    monkeypatch.setattr("shutil.which", lambda _name: os.fspath(inhibitor))

    command = _official_client_command(
        Path("/opt/umu/umu-run"), Path("/games/launcher.exe"), inhibit_sleep=True
    )

    assert command == [
        os.fspath(inhibitor),
        "--what=sleep:shutdown",
        "--who=GameBridge",
        "--why=Official game download is active",
        "--mode=block",
        "/opt/umu/umu-run",
        "/games/launcher.exe",
    ]
