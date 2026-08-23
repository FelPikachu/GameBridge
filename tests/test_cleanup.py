import json
from types import SimpleNamespace

import pytest

import gamebridge.application as application_module
from gamebridge.application import GameBridgeApplication
from gamebridge.providers.epic import EpicProvider
from gamebridge.providers.hoyoplay import HoYoPlayProvider
from gamebridge.steam_browser import SteamBrowserAuthorization


def _registered_epic_game(application, storage_root):
    game_path = storage_root / "Games" / "GameBridge" / "Epic" / "Sample Game"
    game_path.mkdir(parents=True)
    (game_path / "sample.exe").write_bytes(b"game")
    provider = application.providers.get("epic")
    assert isinstance(provider, EpicProvider)
    manifest = provider.data_directory / "config" / "legendary" / "installed.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"SampleApp": {
            "install_path": str(game_path), "executable": "sample.exe"
        }}),
        encoding="utf-8",
    )
    return provider, game_path


async def _prepare_cleanup(application, provider, monkeypatch, uninstall_calls):
    async def fake_logout():
        return {"state": "disconnected"}

    async def fake_uninstall(external_id):
        uninstall_calls.append(external_id)

    async def fake_clear_session(_self):
        return None

    monkeypatch.setattr(provider, "logout", fake_logout)
    monkeypatch.setattr(provider, "uninstall", fake_uninstall)
    monkeypatch.setattr(SteamBrowserAuthorization, "clear_epic_session", fake_clear_session)
    monkeypatch.setattr(SteamBrowserAuthorization, "clear_steamgriddb_session", fake_clear_session)


@pytest.mark.asyncio
async def test_cleanup_keeps_installed_games_by_default(tmp_path, monkeypatch):
    data_directory = tmp_path / "data"
    storage_root = tmp_path / "storage"
    application = GameBridgeApplication(data_directory)
    application.start()
    provider, game_path = _registered_epic_game(application, storage_root)
    monkeypatch.setattr(
        application_module, "storage_roots", lambda: [SimpleNamespace(path=storage_root)]
    )
    uninstall_calls = []
    await _prepare_cleanup(application, provider, monkeypatch, uninstall_calls)

    result = await application.cleanup_before_uninstall()

    assert game_path.is_dir()
    assert (game_path / "sample.exe").is_file()
    assert uninstall_calls == []
    assert result["removedGames"] == 0
    restored = provider.data_directory / "config" / "legendary" / "installed.json"
    assert json.loads(restored.read_text(encoding="utf-8"))["SampleApp"]["install_path"] == str(game_path)


@pytest.mark.asyncio
async def test_destructive_cleanup_removes_only_managed_epic_games(tmp_path, monkeypatch):
    data_directory = tmp_path / "data"
    storage_root = tmp_path / "storage"
    application = GameBridgeApplication(data_directory)
    application.start()
    provider, game_path = _registered_epic_game(application, storage_root)
    external_path = tmp_path / "external" / "Keep Me"
    external_path.mkdir(parents=True)
    (external_path / "keep.exe").write_bytes(b"game")
    manifest = provider.data_directory / "config" / "legendary" / "installed.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["ExternalApp"] = {
        "install_path": str(external_path), "executable": "keep.exe"
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        application_module, "storage_roots", lambda: [SimpleNamespace(path=storage_root)]
    )
    uninstall_calls = []
    await _prepare_cleanup(application, provider, monkeypatch, uninstall_calls)

    result = await application.cleanup_before_uninstall(delete_games=True)

    assert not game_path.exists()
    assert external_path.is_dir()
    assert (external_path / "keep.exe").is_file()
    assert uninstall_calls == ["SampleApp"]
    assert result["removedGames"] == 1
    restored = json.loads(manifest.read_text(encoding="utf-8"))
    assert set(restored) == {"ExternalApp"}


@pytest.mark.asyncio
async def test_cleanup_retains_all_verified_provider_installations(tmp_path, monkeypatch):
    data_directory = tmp_path / "data"
    storage_root = tmp_path / "storage"
    application = GameBridgeApplication(data_directory)
    application.start()
    epic, first_epic = _registered_epic_game(application, storage_root)
    second_epic = storage_root / "Games" / "GameBridge" / "Epic" / "Second Game"
    second_epic.mkdir(parents=True)
    (second_epic / "second.exe").write_bytes(b"game")
    epic_manifest = epic.data_directory / "config" / "legendary" / "installed.json"
    epic_payload = json.loads(epic_manifest.read_text(encoding="utf-8"))
    epic_payload["SecondApp"] = {
        "install_path": str(second_epic), "executable": "second.exe"
    }
    epic_payload["SampleApp"]["executable"] = "sample.exe"
    epic_manifest.write_text(json.dumps(epic_payload), encoding="utf-8")

    hoyo_paths = {}
    hoyo_prefix_markers = {}
    for provider_id, game_id in (("mihoyo_cn", "hk4e_cn"), ("hoyoplay_global", "nap_global")):
        provider = application.providers.get(provider_id)
        assert isinstance(provider, HoYoPlayProvider)
        game = next(item for item in provider.spec.games if item.external_game_id == game_id)
        game_path = tmp_path / provider_id / game_id
        game_path.mkdir(parents=True)
        (game_path / game.executable_names[0]).write_bytes(b"game")
        provider.retained_installations_path.parent.mkdir(parents=True, exist_ok=True)
        provider.retained_installations_path.write_text(
            json.dumps({game_id: str(game_path)}), encoding="utf-8"
        )
        marker = provider.prefix_directory / "drive_c" / "launcher-login.dat"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("preserve", encoding="utf-8")
        hoyo_paths[provider_id] = (provider, game_id, game_path)
        hoyo_prefix_markers[provider_id] = marker

    monkeypatch.setattr(
        application_module, "storage_roots", lambda: [SimpleNamespace(path=storage_root)]
    )
    await _prepare_cleanup(application, epic, monkeypatch, [])
    await application.cleanup_before_uninstall()

    restored_epic = json.loads(epic_manifest.read_text(encoding="utf-8"))
    assert set(restored_epic) == {"SampleApp", "SecondApp"}
    assert first_epic.is_dir() and second_epic.is_dir()
    for provider, game_id, game_path in hoyo_paths.values():
        assert provider.game_installation(game_id)["install_path"] == str(game_path)
        assert provider.game_installation(game_id)["installed"] is True
    assert all(marker.read_text(encoding="utf-8") == "preserve" for marker in hoyo_prefix_markers.values())


@pytest.mark.asyncio
async def test_destructive_cleanup_removes_hoyoplay_prefixes_but_keeps_external_games(
    tmp_path, monkeypatch
):
    application = GameBridgeApplication(tmp_path / "data")
    application.start()
    epic, _game_path = _registered_epic_game(application, tmp_path / "storage")
    external_game = tmp_path / "external-hoyoplay-game"
    external_game.mkdir()
    (external_game / "GenshinImpact.exe").write_bytes(b"game")
    markers = []
    verified_roots = []
    for provider_id in ("mihoyo_cn", "hoyoplay_global"):
        provider = application.providers.get(provider_id)
        assert isinstance(provider, HoYoPlayProvider)
        marker = provider.prefix_directory / "drive_c" / "launcher-login.dat"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("remove", encoding="utf-8")
        markers.append(marker)
        launcher_root = tmp_path / (
            "miHoYo Launcher" if provider_id == "mihoyo_cn" else "HoYoPlay"
        )
        launcher_root.mkdir()
        launcher = launcher_root / "launcher.exe"
        launcher.write_bytes(b"MZ")
        (launcher_root / "config.ini").write_text("[hyp]", encoding="utf-8")
        game = provider.spec.games[0]
        game_path = launcher_root / "games" / game.external_game_id
        game_path.mkdir(parents=True)
        (game_path / game.executable_names[0]).write_bytes(b"game")
        provider.retained_installations_path.parent.mkdir(parents=True, exist_ok=True)
        provider.retained_installations_path.write_text(
            json.dumps({game.external_game_id: str(game_path)}), encoding="utf-8"
        )
        monkeypatch.setattr(provider, "launcher_executable", lambda launcher=launcher: launcher)
        verified_roots.append(launcher_root)
    monkeypatch.setattr(
        application_module,
        "storage_roots",
        lambda: [SimpleNamespace(path=tmp_path / "storage")],
    )
    await _prepare_cleanup(application, epic, monkeypatch, [])

    await application.cleanup_before_uninstall(delete_games=True)

    assert all(not marker.exists() for marker in markers)
    assert all(not root.exists() for root in verified_roots)
    assert (external_game / "GenshinImpact.exe").is_file()
