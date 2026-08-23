import json
import os
import sqlite3
import hashlib
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest

from gamebridge.models import GameReference
from gamebridge.providers.hoyoplay import HOYOPLAY_GLOBAL, MIHOYO_CN, HoYoPlayProvider


@pytest.mark.asyncio
async def test_cn_and_global_providers_are_fully_isolated(tmp_path):
    compatibility = tmp_path / "compatibility"
    cn = HoYoPlayProvider(tmp_path / "providers/mihoyo-cn", compatibility, MIHOYO_CN)
    global_provider = HoYoPlayProvider(
        tmp_path / "providers/hoyoplay-global", compatibility, HOYOPLAY_GLOBAL
    )

    assert cn.provider_id == "mihoyo_cn"
    assert global_provider.provider_id == "hoyoplay_global"
    assert cn.data_directory != global_provider.data_directory
    assert cn.prefix_directory == compatibility / "mihoyo-cn"
    assert global_provider.prefix_directory == compatibility / "hoyoplay-global"
    assert cn.prefix_directory != global_provider.prefix_directory
    cn_status = await cn.connection_status()
    assert cn_status["state"] == "not_installed"
    assert cn_status["officialPage"] == "https://sr.mihoyo.com/ad"
    assert (await global_provider.connection_status())["state"] == "not_installed"


@pytest.mark.asyncio
async def test_global_star_rail_is_exposed_as_experimental_after_device_validation(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/hoyoplay-global",
        tmp_path / "compatibility",
        HOYOPLAY_GLOBAL,
    )

    games = {game.external_game_id: game for game in await provider.library()}
    assert games["hkrpg_global"].compatibility_status.value == "experimental"


@pytest.mark.asyncio
async def test_global_honkai_impact_is_experimental_after_ge_proton_validation(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/hoyoplay-global",
        tmp_path / "compatibility",
        HOYOPLAY_GLOBAL,
    )

    games = {game.external_game_id: game for game in await provider.library()}
    assert games["bh3_global"].compatibility_status.value == "experimental"


@pytest.mark.asyncio
async def test_provider_detects_and_resolves_only_its_official_launcher(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/hoyoplay-global",
        tmp_path / "compatibility",
        HOYOPLAY_GLOBAL,
    )
    executable = provider.prefix_directory / HOYOPLAY_GLOBAL.executable_candidates[0]
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")

    status = await provider.connection_status()
    assert status["state"] == "installed"
    assert status["action"] == "launch_client"
    assert status["executable"] == os.fspath(executable)

    profile = await provider.resolve_launch(
        GameReference("hoyoplay_global", "launcher", "HoYoPlay", "global")
    )
    assert profile.prefix_path == os.fspath(provider.prefix_directory)
    assert profile.executable == os.fspath(executable)
    assert profile.environment == {"UMU_LOG": "1"}


@pytest.mark.asyncio
async def test_provider_resolves_launcher_installed_on_mapped_external_drive(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    external_drive = tmp_path / "external"
    executable = external_drive / "miHoYo Launcher" / "launcher.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "v:").symlink_to(external_drive, target_is_directory=True)
    (provider.prefix_directory / "system.reg").write_text(
        '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\HYP_1_1_cn]\n'
        '"ExeName"="launcher.exe"\n'
        '"GameBiz"="hyp_cn"\n'
        '"InstallPath"="V:\\\\miHoYo Launcher"\n',
        encoding="utf-8",
    )

    status = await provider.connection_status()
    assert status["state"] == "installed"
    assert status["executable"] == os.fspath(executable)
    profile = await provider.resolve_launch(
        GameReference("mihoyo_cn", "launcher", "miHoYo Launcher", "cn")
    )
    assert profile.executable == os.fspath(executable)


def test_provider_ignores_unrelated_or_escaping_registry_install_paths(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    external_drive = tmp_path / "external"
    external_drive.mkdir()
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "v:").symlink_to(external_drive, target_is_directory=True)
    (provider.prefix_directory / "system.reg").write_text(
        '[Unrelated]\n'
        '"ExeName"="launcher.exe"\n'
        '"GameBiz"="other_store"\n'
        '"InstallPath"="V:\\\\other"\n\n'
        '[Escaping]\n'
        '"ExeName"="launcher.exe"\n'
        '"GameBiz"="hyp_cn"\n'
        '"InstallPath"="V:\\\\..\\\\outside"\n',
        encoding="utf-8",
    )

    assert provider.launcher_executable() is None


@pytest.mark.asyncio
async def test_public_catalog_does_not_claim_an_owned_library(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    capabilities = provider.capabilities()
    assert capabilities.official_installer
    assert capabilities.local_launch
    assert capabilities.public_catalog
    assert not capabilities.owned_library
    games = await provider.library()
    assert [game.external_game_id for game in games] == [
        "hk4e_cn",
        "nap_cn",
        "hkrpg_cn",
        "bh3_cn",
    ]
    assert games[0].title == "原神"
    assert games[0].compatibility_status.value == "experimental"
    assert games[2].compatibility_status.value == "experimental"


def test_provider_detects_only_complete_game_from_its_registered_drive(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Games" / "Genshin Impact Game"
    game_directory.mkdir(parents=True)
    executable = game_directory / "YuanShen.exe"
    executable.write_bytes(b"MZ")
    (game_directory / "config.ini").write_text("game_version=7.0.1\n", encoding="utf-8")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Games\\\\Genshin Impact Game"\n',
        encoding="utf-8",
    )

    installation = provider.game_installation("hk4e_cn")
    assert installation["installed"] is True
    assert installation["partial"] is False
    assert installation["install_state"] == "installed"
    assert installation["launchable"] is False
    assert installation["install_path"] == os.fspath(game_directory)
    assert installation["executable"] == os.fspath(executable)
    assert installation["installed_version"] == "7.0.1"
    assert installation["official_client_installed"] is False
    assert installation["native_steam_app_id"] is None
    assert installation["storage_state"] == "writable"


def test_provider_reads_game_registration_from_card_specific_prefix(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    card_prefix = tmp_path / "compatdata/1234/pfx"
    card_prefix.mkdir(parents=True)
    (card_prefix / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\bh3_cn]\n'
        '"GameBiz"="bh3_cn"\n'
        '"GameInstallPath"="G:\\\\miHoYo Launcher\\\\games\\\\Honkai Impact 3rd Game"\n',
        encoding="utf-8",
    )

    assert provider.game_registry_install_path("bh3_cn", card_prefix) == (
        r"G:\miHoYo Launcher\games\Honkai Impact 3rd Game"
    )


def test_provider_detects_global_game_when_registry_key_has_package_suffix(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/hoyoplay-global", tmp_path / "compatibility", HOYOPLAY_GLOBAL
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Honkai Impact 3rd game"
    game_directory.mkdir(parents=True)
    executable = game_directory / "BH3.exe"
    executable.write_bytes(b"MZ")
    (game_directory / "config.ini").write_text("game_version=9.0.0\n", encoding="utf-8")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\Cognosphere\\\\HYP\\\\1_0\\\\bh3_globalglb_official]\n'
        '"GameBiz"="bh3_global"\n'
        '"GameInstallPath"="G:\\\\Honkai Impact 3rd game"\n',
        encoding="utf-8",
    )

    installation = provider.game_installation("bh3_global")

    assert installation["installed"] is True
    assert installation["install_path"] == os.fspath(game_directory)
    assert installation["executable"] == os.fspath(executable)


def test_provider_exposes_official_steam_app_id_for_zenless_zone_zero(tmp_path):
    cn = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    global_provider = HoYoPlayProvider(
        tmp_path / "providers/hoyoplay-global", tmp_path / "compatibility", HOYOPLAY_GLOBAL
    )

    assert cn.game_installation("nap_cn")["native_steam_app_id"] == 4162040
    assert global_provider.game_installation("nap_global")["native_steam_app_id"] == 4162040
    assert cn.game_installation("hk4e_cn")["native_steam_app_id"] is None


def test_provider_does_not_treat_partial_download_as_installed(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    partial = game_drive / "Genshin Impact Game"
    partial.mkdir(parents=True)
    (partial / "chunk").mkdir()
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin Impact Game"\n',
        encoding="utf-8",
    )

    installation = provider.game_installation("hk4e_cn")
    assert installation["installed"] is False
    assert installation["launchable"] is False
    assert installation["partial"] is True
    assert installation["install_state"] == "partial"
    assert installation["install_path"] == os.fspath(partial)
    assert installation["executable"] is None


@pytest.mark.parametrize("game_id", ["hk4e_cn", "nap_cn", "hkrpg_cn"])
def test_provider_remembers_channel_before_game_is_installed(tmp_path, game_id):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )

    status = provider.switch_channel_profile(game_id, "bilibili")

    assert status["current"] == "bilibili"
    assert status["bilibili_ready"] is False
    assert (
        provider.channel_profiles_directory / game_id / "selected"
    ).read_text(encoding="utf-8") == "bilibili"


def test_provider_uses_one_channel_selection_for_three_cn_games(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )

    assert provider.switch_channel_selection("bilibili") == {"current": "bilibili"}
    assert provider.channel_selection_path.read_text(encoding="utf-8") == "bilibili"
    for game_id in ("hk4e_cn", "nap_cn", "hkrpg_cn"):
        assert provider.channel_profile_status(game_id)["current"] == "bilibili"


def test_selecting_channel_is_immediate_and_defers_game_file_preparation(
    tmp_path, monkeypatch
):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    prepared = []
    monkeypatch.setattr(
        provider,
        "switch_channel_profile",
        lambda game_id, channel: prepared.append((game_id, channel)),
    )

    assert provider.switch_channel_selection("bilibili") == {"current": "bilibili"}
    assert prepared == []


def test_launch_uses_explicit_ui_channel_instead_of_stale_provider_selection(
    tmp_path, monkeypatch
):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    provider._write_channel_selection("official")
    applied = []
    monkeypatch.setattr(
        provider,
        "switch_channel_profile",
        lambda game_id, channel: applied.append((game_id, channel)) or {"current": channel},
    )

    status = provider.apply_channel_for_launch("hk4e_cn", "bilibili")

    assert applied == [("hk4e_cn", "bilibili")]
    assert provider.selected_channel() == "bilibili"
    assert status["current"] == "bilibili"


def test_launch_channel_preparation_failure_does_not_fallback_or_change_selection(
    tmp_path, monkeypatch
):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    provider._write_channel_selection("official")

    def fail(_game_id, _channel):
        raise RuntimeError("sdk unavailable")

    monkeypatch.setattr(provider, "switch_channel_profile", fail)
    with pytest.raises(RuntimeError, match="sdk unavailable"):
        provider.apply_channel_for_launch("hk4e_cn", "bilibili")
    assert provider.selected_channel() == "official"


def test_provider_migrates_unanimous_legacy_channel_choices(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    for game_id in ("hk4e_cn", "nap_cn", "hkrpg_cn"):
        preference = provider.channel_profiles_directory / game_id / "selected"
        preference.parent.mkdir(parents=True, exist_ok=True)
        preference.write_text("bilibili", encoding="utf-8")

    assert provider.channel_selection_status() == {"current": "bilibili"}


def test_provider_captures_and_switches_exact_official_channel_profiles(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Genshin"
    game_directory.mkdir(parents=True)
    (game_directory / "YuanShen.exe").write_bytes(b"MZ")
    config = game_directory / "config.ini"
    official = b"[General]\r\ngame_version=7.0.0\r\nchannel=1\r\nsub_channel=1\r\ncps=hyp_mihoyo\r\n"
    bilibili = b"[General]\r\ngame_version=7.0.0\r\nchannel=14\r\nsub_channel=0\r\ncps=hyp_mihoyo\r\n"
    config.write_bytes(official)
    sdk = game_directory / "YuanShen_Data/Plugins/PCGameSDK.dll"
    sdk.parent.mkdir(parents=True)
    sdk.write_bytes(b"official-sdk")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin"\n',
        encoding="utf-8",
    )

    status = provider.capture_channel_profile("hk4e_cn")
    assert status == {
        "current": "official",
        "official_ready": True,
        "bilibili_ready": False,
        "mode": "sdk",
    }
    (provider.channel_profiles_directory / "hk4e_cn" / "selected").unlink()
    config.write_bytes(bilibili)
    sdk.write_bytes(b"bilibili-sdk")
    status = provider.capture_channel_profile("hk4e_cn")
    assert status["bilibili_ready"] is True

    provider.switch_channel_profile("hk4e_cn", "official")
    assert config.read_bytes() == official
    assert sdk.read_bytes() == b"official-sdk"
    provider.switch_channel_profile("hk4e_cn", "bilibili")
    assert config.read_bytes() == bilibili
    assert sdk.read_bytes() == b"bilibili-sdk"


def test_provider_normalizes_bilibili_config_when_channel_is_already_selected(
    tmp_path,
):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_directory = tmp_path / "game"
    game_directory.mkdir()
    config = game_directory / "config.ini"
    legacy = b"channel=14\nsub_channel=0\ncps=stale\nsdk_version=5.0.4\n"
    config.write_bytes(legacy)
    profile = provider._profile_directory("hk4e_cn", "bilibili")
    profile.mkdir(parents=True)
    (profile / "config.ini").write_bytes(legacy)
    (profile / "manifest.json").write_text(
        '{"channel":"bilibili","components":[]}', encoding="utf-8"
    )
    provider.game_installation = lambda _game_id, **_kwargs: {
        "installed": True,
        "install_path": os.fspath(game_directory),
    }

    provider.switch_channel_profile("hk4e_cn", "bilibili")

    assert "cps=hyp_mihoyo" in config.read_text(encoding="utf-8")
    assert "cps=hyp_mihoyo" in (profile / "config.ini").read_text(encoding="utf-8")


def test_provider_preserves_current_official_profile_before_first_bilibili_switch(
    tmp_path, monkeypatch
):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Genshin"
    sdk = game_directory / "YuanShen_Data/Plugins/PCGameSDK.dll"
    sdk.parent.mkdir(parents=True)
    (game_directory / "YuanShen.exe").write_bytes(b"MZ")
    (game_directory / "config.ini").write_text("channel=1\n", encoding="utf-8")
    sdk.write_bytes(b"official-sdk")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin"\n',
        encoding="utf-8",
    )

    def prepare(_game_id, channel):
        target = provider._profile_directory("hk4e_cn", channel)
        component = target / "components/YuanShen_Data/Plugins/PCGameSDK.dll"
        component.parent.mkdir(parents=True)
        component.write_bytes(b"bilibili-sdk")
        (target / "config.ini").write_text("channel=14\n", encoding="utf-8")
        (target / "manifest.json").write_text(
            '{"channel":"bilibili","components":["YuanShen_Data/Plugins/PCGameSDK.dll"]}',
            encoding="utf-8",
        )
        return provider.channel_profile_status("hk4e_cn")

    monkeypatch.setattr(provider, "prepare_channel_profile", prepare)

    provider.switch_channel_profile("hk4e_cn", "bilibili")

    official = provider._profile_directory("hk4e_cn", "official")
    assert (official / "components/YuanShen_Data/Plugins/PCGameSDK.dll").read_bytes() == b"official-sdk"
    assert "YuanShen_Data/Plugins/PCGameSDK.dll" in (
        official / "manifest.json"
    ).read_text(encoding="utf-8")


def test_capture_channel_profile_replaces_stale_component_snapshot(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_directory = tmp_path / "game"
    game_directory.mkdir()
    (game_directory / "config.ini").write_text("channel=1\n", encoding="utf-8")
    provider.game_installation = lambda _game_id, **_kwargs: {
        "installed": True,
        "install_path": os.fspath(game_directory),
    }
    profile = provider._profile_directory("hk4e_cn", "official")
    stale = profile / "components/YuanShen_Data/Plugins/PCGameSDK.dll"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    provider.capture_channel_profile("hk4e_cn")

    assert not stale.exists()
    manifest = json.loads((profile / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"] == []


def test_provider_prepares_missing_bilibili_profile_from_verified_official_sdk(tmp_path, monkeypatch):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Genshin"
    game_directory.mkdir(parents=True)
    (game_directory / "YuanShen.exe").write_bytes(b"MZ")
    config = game_directory / "config.ini"
    config.write_text("channel=1\n", encoding="utf-8")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin"\n',
        encoding="utf-8",
    )

    archive_stream = BytesIO()
    with zipfile.ZipFile(archive_stream, "w") as archive:
        archive.writestr("YuanShen_Data/Plugins/PCGameSDK.dll", b"official-bilibili-sdk")
        archive.writestr("sdk_pkg_version", b'{"remoteName":"sdk"}\n')
    archive_bytes = archive_stream.getvalue()
    monkeypatch.setattr(
        provider,
        "_channel_sdk_metadata",
        lambda *_: {
            "version": "5.0.4",
            "channel_sdk_pkg": {
                "url": "https://launcher-webstatic.mihoyo.com/sdk.zip",
                "md5": hashlib.md5(archive_bytes, usedforsecurity=False).hexdigest(),
                "size": len(archive_bytes),
            },
        },
    )
    monkeypatch.setattr(
        "gamebridge.providers.hoyoplay.urllib.request.urlopen",
        lambda *_args, **_kwargs: BytesIO(archive_bytes),
    )
    status = provider.switch_channel_profile("hk4e_cn", "bilibili")
    assert status["current"] == "bilibili"
    assert status["bilibili_ready"] is True
    assert "channel=14" in config.read_text(encoding="utf-8")
    assert "sub_channel=0" in config.read_text(encoding="utf-8")
    assert "sdk_version=5.0.4" in config.read_text(encoding="utf-8")
    assert (game_directory / "YuanShen_Data/Plugins/PCGameSDK.dll").read_bytes() == b"official-bilibili-sdk"
    config.write_text("channel=99\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported_channel"):
        provider.capture_channel_profile("hk4e_cn")


def test_provider_repairs_empty_version_only_from_matching_completed_sophon_build(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Genshin"
    game_directory.mkdir(parents=True)
    (game_directory / "YuanShen.exe").write_bytes(b"MZ")
    config = game_directory / "config.ini"
    config.write_text("[General]\ngame_version=\nchannel=1\n", encoding="utf-8")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin"\n',
        encoding="utf-8",
    )
    provider.chunk_config_database.parent.mkdir(parents=True)
    with sqlite3.connect(provider.chunk_config_database) as connection:
        connection.execute(
            "CREATE TABLE config (local_version TEXT, server_version TEXT, "
            "local_build_id TEXT, server_build_id TEXT, matching_field TEXT, install_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO config VALUES (?,?,?,?,?,?)",
            ("7.0.0", "7.0.0", "build", "build", "game", r"G:\Genshin"),
        )

    assert provider.repair_completed_install_metadata("hk4e_cn") is True
    assert "game_version=7.0.0" in config.read_text(encoding="utf-8")
    backup = provider.data_directory / "repairs/hk4e_cn-config.ini.backup"
    assert "game_version=\n" in backup.read_text(encoding="utf-8")
    assert provider._installed_game_version(game_directory) == "7.0.0"


def test_provider_does_not_repair_version_when_sophon_build_is_incomplete(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Genshin"
    game_directory.mkdir(parents=True)
    (game_directory / "YuanShen.exe").write_bytes(b"MZ")
    config = game_directory / "config.ini"
    config.write_text("[General]\ngame_version=\n", encoding="utf-8")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin"\n',
        encoding="utf-8",
    )
    provider.chunk_config_database.parent.mkdir(parents=True)
    with sqlite3.connect(provider.chunk_config_database) as connection:
        connection.execute(
            "CREATE TABLE config (local_version TEXT, server_version TEXT, "
            "local_build_id TEXT, server_build_id TEXT, matching_field TEXT, install_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO config VALUES (?,?,?,?,?,?)",
            ("6.9.0", "7.0.0", "old", "new", "game", r"G:\Genshin"),
        )

    assert provider.repair_completed_install_metadata("hk4e_cn") is False
    assert config.read_text(encoding="utf-8").endswith("game_version=\n")


def test_provider_blocks_official_client_when_its_storage_is_readonly(tmp_path, monkeypatch):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    launcher = provider.prefix_directory / MIHOYO_CN.executable_candidates[0]
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"MZ")
    monkeypatch.setattr(
        "gamebridge.providers.hoyoplay.storage_health",
        lambda _path: SimpleNamespace(state="readonly", filesystem="ntfs3"),
    )

    blocker = provider.storage_blocker()

    assert blocker == {
        "state": "readonly",
        "path": os.fspath(launcher),
        "filesystem": "ntfs3",
    }


def test_provider_enables_direct_launch_only_for_installed_experimental_game(tmp_path):
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
    (genshin / "YuanShen.exe").write_bytes(b"MZ")
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

    assert provider.game_installation("hk4e_cn")["launchable"] is True
    assert provider.game_installation("hkrpg_cn")["launchable"] is True


def test_provider_ignores_other_region_game_registration(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    game_drive = tmp_path / "game-drive"
    global_game = game_drive / "Genshin Impact Game"
    global_game.mkdir(parents=True)
    (global_game / "GenshinImpact.exe").touch()
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_global]\n'
        '"GameInstallPath"="G:\\\\Genshin Impact Game"\n',
        encoding="utf-8",
    )

    assert provider.game_installation("hk4e_cn")["installed"] is False


@pytest.mark.asyncio
async def test_provider_rejects_cross_region_game_reference(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    with pytest.raises(ValueError, match="game_mismatch"):
        await provider.resolve_launch(
            GameReference("hoyoplay_global", "launcher", "HoYoPlay", "global")
        )


def test_provider_imports_pe_installer_and_records_hash(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    source = tmp_path / "hyp_cn_setup.exe"
    source.write_bytes(b"MZ" + b"official-test-payload" * 100)
    result = provider.import_installer(source)

    assert provider.managed_installer.read_bytes() == source.read_bytes()
    assert result["sourceFilename"] == "hyp_cn_setup.exe"
    assert len(result["sha256"]) == 64
    metadata = json.loads(provider.installer_metadata_file.read_text(encoding="utf-8"))
    assert metadata["sha256"] == result["sha256"]
    assert metadata["verification"].endswith("unverified_signature")


def test_provider_rejects_non_pe_installer(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    source = tmp_path / "fake.exe"
    source.write_bytes(b"not a windows executable" * 100)
    with pytest.raises(ValueError, match="invalid_installer"):
        provider.import_installer(source)


class FakeDownload(BytesIO):
    def __init__(self, payload, url):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout):
        assert timeout == 60
        return self.response


def test_provider_downloads_only_official_pe_and_records_provenance(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    payload = b"MZ" + b"official-download" * 100
    result = provider.download_installer(
        FakeOpener(FakeDownload(payload, MIHOYO_CN.installer_url))
    )

    assert provider.managed_installer.read_bytes() == payload
    assert result["sourceUrl"] == MIHOYO_CN.installer_url
    assert result["finalUrl"] == MIHOYO_CN.installer_url
    assert result["verification"] == "downloaded_from_official_domain_unverified_signature"
    assert len(result["sha256"]) == 64


def test_provider_rejects_download_redirected_to_untrusted_host(tmp_path):
    provider = HoYoPlayProvider(
        tmp_path / "providers/mihoyo-cn", tmp_path / "compatibility", MIHOYO_CN
    )
    payload = b"MZ" + b"payload" * 200
    with pytest.raises(ValueError, match="untrusted_installer_url"):
        provider.download_installer(
            FakeOpener(FakeDownload(payload, "https://example.com/installer.exe"))
        )
    assert not provider.managed_installer.exists()
