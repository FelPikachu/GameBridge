import base64
import struct
from pathlib import Path

import gamebridge.application as application_module
from gamebridge.application import GameBridgeApplication
from gamebridge.providers.hoyoplay import HoYoPlayProvider
import pytest


@pytest.mark.asyncio
async def test_official_installer_maps_external_storage_before_launch(
    tmp_path, monkeypatch
):
    application = GameBridgeApplication(tmp_path)
    application.start()
    provider = application.providers.get("mihoyo_cn")
    assert isinstance(provider, HoYoPlayProvider)
    provider.managed_installer.parent.mkdir(parents=True)
    provider.managed_installer.write_bytes(b"MZ")
    runtime = tmp_path / "GE-Proton"
    application.compatibility.umu_executable.parent.mkdir(parents=True, exist_ok=True)
    application.compatibility.umu_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(application.compatibility, "status", lambda: {"ready": True})
    monkeypatch.setattr(
        application.compatibility,
        "selected_proton",
        lambda provider_id, game_id: ("GE-Proton", runtime),
    )
    mapped: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        application_module,
        "ensure_wine_storage_drive",
        lambda prefix, executable: mapped.append((prefix, executable)),
    )

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_subprocess(*args, **kwargs):
        return Process()

    monkeypatch.setattr(application_module.asyncio, "create_subprocess_exec", fake_subprocess)

    result = await application.run_provider_installer("mihoyo_cn")

    assert result["state"] == "installer_started"
    assert mapped == [(provider.prefix_directory, provider.managed_installer)]


def test_library_search_and_pagination(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()
    with application.database.connect() as db:
        for index, title in enumerate(("Alpha Game", "Beta Game", "Alpha Two")):
            game_id = f"epic:game-{index}"
            db.execute(
                "INSERT INTO catalog_games(id,title,normalized_title) VALUES(?,?,?)",
                (game_id, title, title.casefold()),
            )
            db.execute(
                "INSERT INTO game_releases"
                "(canonical_game_id,provider_id,external_game_id,region,release_channel) "
                "VALUES(?,?,?,?,?)",
                (game_id, "epic", f"game-{index}", "global", "stable"),
            )
    first = application.list_games("alpha", 0, 1)
    second = application.list_games("alpha", 1, 1)
    assert first["total"] == 2
    assert len(first["items"]) == 1
    assert first["items"][0]["title"] == "Alpha Game"
    assert second["items"][0]["title"] == "Alpha Two"


@pytest.mark.asyncio
async def test_epic_logout_clears_provider_and_browser_sessions(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    provider = application.providers.get("epic")
    calls: list[str] = []

    async def fake_logout():
        calls.append("provider")
        return {"state": "disconnected", "message": "epic.login_required"}

    class FakeBrowser:
        async def clear_epic_session(self):
            calls.append("browser")
            return 2

    monkeypatch.setattr(provider, "logout", fake_logout)
    monkeypatch.setattr(application_module, "SteamBrowserAuthorization", FakeBrowser)

    result = await application.logout_provider("epic")

    assert calls == ["provider", "browser"]
    assert result["state"] == "disconnected"
    assert result["browserSessionCleared"] is True


@pytest.mark.asyncio
async def test_logout_rejects_provider_without_logout_contract(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()

    with pytest.raises(ValueError, match="provider.logout_unsupported"):
        await application.logout_provider("mihoyo_cn")


def test_library_search_escapes_wildcards(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()
    assert application.list_games("%", 0, 8)["total"] == 0


def test_game_details_returns_safe_provider_data(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()
    with application.database.connect() as db:
        db.execute(
            "INSERT INTO catalog_games(id,title,normalized_title) VALUES(?,?,?)",
            ("epic:sample", "Sample", "sample"),
        )
        db.execute(
            "INSERT INTO game_releases"
            "(canonical_game_id,provider_id,external_game_id,region,release_channel) "
            "VALUES(?,?,?,?,?)",
            ("epic:sample", "epic", "sample", "global", "stable"),
        )
    details = application.game_details("epic:sample")
    assert details["title"] == "Sample"
    assert details["provider_name"] == "Epic Games"
    assert details["installed"] is False


def test_genshin_shortcut_uses_stable_unified_region_router(tmp_path):
    application = GameBridgeApplication(tmp_path)
    executable = tmp_path / "Genshin Impact Game" / "YuanShen.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")
    profile = application._hoyoplay_steam_shortcut(
        {
            "provider_id": "mihoyo_cn",
            "external_game_id": "hk4e_cn",
            "installed": True,
            "executable": str(executable),
            "storage_state": "writable",
        }
    )

    assert profile == {
        "mode": "gamebridge_router",
        "executable": "/usr/bin/python3",
        "start_directory": "/home/deck/homebrew/plugins/GameBridge",
        "launch_options": '"gamebridge/launcher.py" --provider mihoyo --game-id "genshin"',
        "compatibility_tool": "",
    }


@pytest.mark.parametrize(
    ("external_game_id", "route_game_id"),
    (
        ("nap_cn", "zzz"),
        ("hkrpg_cn", "starrail"),
        ("bh3_cn", "honkai3"),
    ),
)
def test_other_shared_games_use_stable_unified_region_router(
    tmp_path, external_game_id, route_game_id
):
    application = GameBridgeApplication(tmp_path)
    profile = application._hoyoplay_steam_shortcut(
        {"provider_id": "mihoyo_cn", "external_game_id": external_game_id}
    )
    assert profile == {
        "mode": "gamebridge_router",
        "executable": "/usr/bin/python3",
        "start_directory": "/home/deck/homebrew/plugins/GameBridge",
        "launch_options": (
            '"gamebridge/launcher.py" --provider mihoyo '
            f'--game-id "{route_game_id}"'
        ),
        "compatibility_tool": "",
    }


def test_application_persists_global_hoyoplay_route(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()

    assert application.switch_hoyoplay_channel_selection("global") == {"current": "global"}
    assert application.hoyoplay_channel_selection() == {"current": "global"}
    assert (tmp_path / "mihoyo-selection").read_text(encoding="utf-8") == "global"

    assert application.switch_hoyoplay_channel_selection("bilibili") == {
        "current": "bilibili"
    }
    assert application.hoyoplay_channel_selection() == {"current": "bilibili"}

    restarted = GameBridgeApplication(tmp_path)
    restarted.start()
    assert restarted.hoyoplay_channel_selection() == {"current": "bilibili"}


def test_unknown_hoyoplay_game_does_not_replace_placeholder(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    executable = tmp_path / "YuanShen.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setattr(application.compatibility, "proton_layers", lambda: [])

    base = {
        "provider_id": "mihoyo_cn",
        "external_game_id": "unknown_cn",
        "installed": True,
        "executable": str(executable),
        "storage_state": "writable",
    }
    assert application._hoyoplay_steam_shortcut(base) is None
    assert application._hoyoplay_steam_shortcut({**base, "storage_state": "offline"}) is None


@pytest.mark.asyncio
async def test_dashboard_seeds_public_catalog_without_claiming_ownership(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()

    dashboard = await application.dashboard()
    page = application.list_games("", 0, 20)

    assert dashboard["gameCount"] == 8
    assert page["total"] == 8
    genshin = next(item for item in page["items"] if item["id"] == "mihoyo_cn:hk4e_cn")
    assert genshin["title"] == "原神"
    assert genshin["compatibility_status"] == "experimental"
    details = application.game_details("mihoyo_cn:hk4e_cn")
    assert details["installed"] is False
    assert details["launchable"] is False
    assert details["official_client_installed"] is False
    assert application.steam_library_games() == []


@pytest.mark.asyncio
async def test_dashboard_does_not_wait_for_community_artwork(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    monkeypatch.setattr(application.steamgriddb, "configured", lambda: True)
    monkeypatch.setattr(
        application.steamgriddb,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail("dashboard started artwork network I/O"),
    )

    dashboard = await application.dashboard()

    assert dashboard["status"] == "ready"
    assert dashboard["gameCount"] == 8


@pytest.mark.asyncio
async def test_hoyoplay_details_report_detected_game_without_enabling_launch(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()
    provider = application.providers.get("mihoyo_cn")
    game_drive = tmp_path / "game-drive"
    game_directory = game_drive / "Genshin Impact Game"
    game_directory.mkdir(parents=True)
    (game_directory / "YuanShen.exe").write_bytes(b"MZ")
    dosdevices = provider.prefix_directory / "dosdevices"
    dosdevices.mkdir(parents=True)
    (dosdevices / "g:").symlink_to(game_drive, target_is_directory=True)
    (provider.prefix_directory / "user.reg").write_text(
        '[Software\\\\miHoYo\\\\HYP\\\\1_1\\\\hk4e_cn]\n'
        '"GameInstallPath"="G:\\\\Genshin Impact Game"\n',
        encoding="utf-8",
    )

    await application.dashboard()
    details = application.game_details("mihoyo_cn:hk4e_cn")

    assert details["installed"] is True
    assert details["launchable"] is False
    assert details["install_path"] == str(game_directory)


@pytest.mark.asyncio
async def test_steam_library_exposes_public_cards_only_after_launcher_install(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"MZ")
    monkeypatch.setattr(HoYoPlayProvider, "launcher_executable", lambda _self: launcher)

    await application.dashboard()
    games = application.steam_library_games()

    mihoyo = [game for game in games if game["provider_id"] == "mihoyo_cn"]
    global_games = [game for game in games if game["provider_id"] == "hoyoplay_global"]
    assert {game["external_game_id"] for game in mihoyo} == {
        "hk4e_cn", "nap_cn", "hkrpg_cn", "bh3_cn"
    }
    assert global_games == []
    assert all(game["installed"] is False for game in mihoyo + global_games)
    assert next(game for game in mihoyo if game["external_game_id"] == "nap_cn")[
        "native_steam_app_id"
    ] == 4162040


@pytest.mark.asyncio
async def test_steam_library_uses_cached_official_mihoyo_artwork(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"MZ")
    monkeypatch.setattr(HoYoPlayProvider, "launcher_executable", lambda _self: launcher)
    official = "https://launcher-webstatic.mihoyo.com/launcher-public/genshin.webp"
    application.official_artwork._write_cache(
        {
            "mihoyo_cn:hk4e_cn": {
                "capsule": official,
                "hero": official,
                "header": official,
            }
        }
    )

    await application.dashboard()
    games = application.steam_library_games()
    genshin = next(game for game in games if game["id"] == "mihoyo_cn:hk4e_cn")

    assert genshin["artwork_url"] == official
    assert genshin["artwork_source"] == "official"


@pytest.mark.asyncio
async def test_community_artwork_overrides_official_artwork(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"MZ")
    monkeypatch.setattr(HoYoPlayProvider, "launcher_executable", lambda _self: launcher)
    application.steamgriddb._write_cache(
        {
            "mihoyo_cn:hk4e_cn": {
                    "schema": "full-v2-language",
                    "language": "en",
                "capsule": "https://cdn2.steamgriddb.com/community.png",
                "hero": "https://cdn2.steamgriddb.com/hero.png",
                "header": "https://cdn2.steamgriddb.com/header.png",
                "logo": "https://cdn2.steamgriddb.com/logo.png",
                "icon": "https://cdn2.steamgriddb.com/icon.png",
            }
        }
    )

    await application.dashboard()
    games = application.steam_library_games()
    genshin = next(game for game in games if game["id"] == "mihoyo_cn:hk4e_cn")

    assert genshin["artwork_url"].endswith("community.png")
    assert genshin["artwork_source"] == "steamgriddb"
    assert application.game_details("mihoyo_cn:hk4e_cn")["artwork_source"] == "steamgriddb"


@pytest.mark.asyncio
async def test_steam_library_warms_official_artwork_after_launcher_install(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"MZ")
    monkeypatch.setattr(HoYoPlayProvider, "launcher_executable", lambda _self: launcher)
    calls = []
    monkeypatch.setattr(
        application.official_artwork,
        "resolve",
        lambda provider_id, game_id: calls.append((provider_id, game_id)),
    )

    await application.refresh_official_artwork_catalog()

    assert calls == [
        ("mihoyo_cn", "hk4e_cn"),
        ("mihoyo_cn", "nap_cn"),
        ("mihoyo_cn", "hkrpg_cn"),
        ("mihoyo_cn", "bh3_cn"),
    ]


def test_epic_artwork_rejects_non_epic_hosts():
    assert GameBridgeApplication._select_epic_artwork(
        [{"type": "DieselGameBoxTall", "url": "https://example.com/tracker.png"}]
    ) is None


def test_unregister_shortcut_only_removes_matching_mapping(tmp_path):
    application = GameBridgeApplication(tmp_path)
    application.start()
    application.register_steam_shortcut("mihoyo_cn", "hk4e_cn", 1234)
    application.register_steam_shortcut("mihoyo_cn", "bh3_cn", 5678)

    application.unregister_steam_shortcut("mihoyo_cn", "hk4e_cn", 9999)
    with application.database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM steam_shortcuts WHERE steam_app_id=1234"
        ).fetchone() is not None

    application.unregister_steam_shortcut("mihoyo_cn", "hk4e_cn", 1234)
    with application.database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM steam_shortcuts WHERE steam_app_id=1234"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM steam_shortcuts WHERE steam_app_id=5678"
        ).fetchone() is not None


@pytest.mark.asyncio
async def test_install_shortcut_artwork_writes_steam_native_filenames(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path / "data")
    application.start()
    application.register_steam_shortcut("mihoyo_cn", "hk4e_cn", 1234)
    application.steamgriddb._write_cache(
        {
            "mihoyo_cn:hk4e_cn": {
                    "schema": "full-v2-language",
                    "language": "en",
                "capsule": "https://cdn2.steamgriddb.com/capsule.png",
                "hero": "https://cdn2.steamgriddb.com/hero.png",
                "header": "https://cdn2.steamgriddb.com/header.png",
                "logo": "https://cdn2.steamgriddb.com/logo.png",
                "icon": "https://cdn2.steamgriddb.com/icon.png",
            }
        }
    )
    userdata = tmp_path / "Steam" / "userdata"
    config = userdata / "123" / "config"
    config.mkdir(parents=True)
    (config / "shortcuts.vdf").write_bytes(b"shortcut")
    monkeypatch.setattr(
        application.steamgriddb,
        "download_image",
        lambda url: {
            "base64": "aW1hZ2U=",
            "mimeType": "image/x-icon" if url.endswith("icon.png") else "image/png",
        },
    )

    result = await application.install_steam_shortcut_artwork(
        "mihoyo_cn", "hk4e_cn", 1234, userdata
    )

    assert result == {
        "written": 5,
        "iconPath": str(config / "grid" / "1234_icon.ico"),
    }
    assert {path.name for path in (config / "grid").iterdir()} == {
        "1234p.png", "1234_hero.png", "1234.png", "1234_logo.png",
        "1234_icon.ico",
    }
    assert await application.ensure_all_steam_shortcut_artwork(userdata) == {
        "ready": 1, "synced": 0, "failed": 0
    }
    monkeypatch.setattr(
        application.steamgriddb,
        "download_image",
        lambda _url: (_ for _ in ()).throw(AssertionError("should reuse existing artwork")),
    )
    assert await application.install_steam_shortcut_artwork(
        "mihoyo_cn", "hk4e_cn", 1234, userdata
    ) == {
        "written": 5,
        "iconPath": str(config / "grid" / "1234_icon.ico"),
    }
    assert await application.download_steamgriddb_artwork(
        "https://cdn2.steamgriddb.com/icon.png", userdata
    ) == {
        "base64": "aW1hZ2U=",
        "mimeType": "image/x-icon",
    }


@pytest.mark.asyncio
async def test_artwork_backfill_resolves_and_installs_each_game_before_next(
    tmp_path, monkeypatch
):
    application = GameBridgeApplication(tmp_path / "data")
    application.start()
    await application.dashboard()
    games = [
        game for game in application.list_games("", 0, 50)["items"]
        if game["provider_id"] == "mihoyo_cn"
    ]
    for index, game in enumerate(games, start=1):
        application.register_steam_shortcut(
            "mihoyo_cn", str(game["external_game_id"]), 1000 + index
        )
    events = []
    monkeypatch.setattr(application.steamgriddb, "configured", lambda: True)
    monkeypatch.setattr(application.steamgriddb, "needs_refresh", lambda *_args: True)
    monkeypatch.setattr(
        application.steamgriddb,
        "resolve",
        lambda provider, external, _title: events.append(("resolve", external))
        or {"capsule": "https://cdn2.steamgriddb.com/capsule.png"},
    )
    monkeypatch.setattr(
        application.steamgriddb,
        "cached",
        lambda _provider, _external: {
            "capsule": "https://cdn2.steamgriddb.com/capsule.png"
        },
    )

    async def install(provider, external, _app_id, _userdata):
        events.append(("install", external))
        return {"written": 1, "iconPath": ""}

    monkeypatch.setattr(application, "install_steam_shortcut_artwork", install)

    result = await application.backfill_community_artwork(
        tmp_path / "userdata", "mihoyo_cn"
    )

    assert result == {"processed": 4, "matched": 4, "installed": 4, "failed": 0}
    assert events == [
        event
        for game in games
        for event in (
            ("resolve", str(game["external_game_id"])),
            ("install", str(game["external_game_id"])),
        )
    ]


@pytest.mark.asyncio
async def test_install_shortcut_artwork_replaces_square_file_in_header_slot(
    tmp_path, monkeypatch
):
    application = GameBridgeApplication(tmp_path / "data")
    application.start()
    application.register_steam_shortcut("mihoyo_cn", "hk4e_cn", 1234)
    application.steamgriddb._write_cache(
        {
            "mihoyo_cn:hk4e_cn": {
                    "schema": "full-v2-language",
                    "language": "en",
                "capsule": "https://cdn2.steamgriddb.com/capsule.png",
                "header": "https://cdn2.steamgriddb.com/header.png",
            }
        }
    )
    grid = tmp_path / "Steam" / "userdata" / "123" / "config" / "grid"
    grid.mkdir(parents=True)
    (grid.parent / "shortcuts.vdf").write_bytes(b"shortcut")
    square_png_header = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 256, 256)
    (grid / "1234p.png").write_bytes(b"existing capsule")
    (grid / "1234.png").write_bytes(square_png_header)
    requested = []
    monkeypatch.setattr(
        application.steamgriddb,
        "download_image",
        lambda url: requested.append(url) or {
            "base64": base64.b64encode(b"replacement").decode("ascii"),
            "mimeType": "image/png",
        },
    )

    await application.install_steam_shortcut_artwork(
        "mihoyo_cn", "hk4e_cn", 1234, tmp_path / "Steam" / "userdata"
    )

    assert requested == ["https://cdn2.steamgriddb.com/header.png"]
    assert (grid / "1234.png").read_bytes() == b"replacement"


@pytest.mark.asyncio
async def test_steam_game_details_recovers_unique_title_for_changed_shortcut_id(tmp_path):
    application = GameBridgeApplication(tmp_path / "data")
    application.start()
    await application.dashboard()

    recovered = application.steam_game_details(4_000_000_001, "原神")

    assert recovered is not None
    assert recovered["id"] == "mihoyo_cn:hk4e_cn"
    assert application.steam_game_details(4_000_000_001, "Missing Game") is None


def test_epic_artwork_selects_logo_independently():
    artwork = GameBridgeApplication._select_epic_artwork_set(
        [
            {
                "type": "DieselGameBoxTall",
                "url": "https://cdn1.epicgames.com/capsule.jpg",
            },
            {
                "type": "ProductLogo",
                "url": "https://cdn1.epicgames.com/logo.png",
            },
        ]
    )
    assert artwork["capsule"].endswith("capsule.jpg")
    assert artwork["logo"].endswith("logo.png")
    assert artwork["hero"] is None
