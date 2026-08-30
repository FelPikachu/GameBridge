import json
from datetime import UTC, datetime, timedelta

import pytest

from gamebridge.process import ProcessResult
from gamebridge.providers.epic import EpicProvider


class FakeRunner:
    def __init__(self):
        self.library_output_limit = None

    async def run(self, executable, *arguments, **kwargs):
        if arguments == ("--version",):
            return ProcessResult((str(executable), *arguments), 0, "legendary 0.20.99\n", "")
        if arguments[0] == "status":
            payload = {"logged_in": True, "account": {"displayName": "Deck User", "id": "1"}}
            return ProcessResult((str(executable), *arguments), 0, json.dumps(payload), "")
        self.library_output_limit = kwargs.get("output_limit")
        payload = [{"app_name": "SampleApp", "app_title": "Sample Game"}]
        return ProcessResult((str(executable), *arguments), 0, json.dumps(payload), "")


class InstallInfoRunner:
    async def run(self, executable, *arguments, **kwargs):
        payload = {
            "manifest": {
                "disk_size": 999,
                "download_size": 888,
                "tag_disk_size": [{"tag": "", "size": 700}, {"tag": "fr", "size": 100}],
                "tag_download_size": [{"tag": "", "size": 600}],
            }
        }
        return ProcessResult((str(executable), *arguments), 0, json.dumps(payload), "")


class RecordingRunner:
    def __init__(self):
        self.arguments = None
        self.kwargs = None

    async def run(self, executable, *arguments, **kwargs):
        self.arguments = arguments
        self.kwargs = kwargs
        return ProcessResult((str(executable), *arguments), 0, "", "")


class UpdateRunner:
    async def run(self, executable, *arguments, **kwargs):
        output = (
            "App name,App title,Installed version,Available version,Update available,"
            "Install size,Install path,Platform\n"
            "SampleApp,Sample Game,1.0,1.1,True,100,/games/sample,Windows\n"
            "CurrentApp,Current Game,2.0,2.0,False,100,/games/current,Windows\n"
        )
        return ProcessResult((str(executable), *arguments), 0, output, "")


class OfflineRunner:
    async def run(self, executable, *arguments, **kwargs):
        raise TimeoutError("network unavailable")


class UnexpectedRunner:
    async def run(self, executable, *arguments, **kwargs):
        raise AssertionError("Legendary must not run for an expired cached login")


async def connected_status():
    return {"state": "connected"}


@pytest.mark.asyncio
async def test_epic_status_and_library(tmp_path, monkeypatch):
    executable = tmp_path / "legendary"
    executable.touch(mode=0o755)
    runner = FakeRunner()
    provider = EpicProvider(tmp_path / "data", runner=runner)
    monkeypatch.setattr(provider, "executable", lambda: executable)
    status = await provider.connection_status()
    games = await provider.library()
    assert status["state"] == "connected"
    assert status["account"] == "Deck User"
    assert games[0].external_game_id == "SampleApp"
    assert runner.library_output_limit == 64 * 1024 * 1024


@pytest.mark.asyncio
async def test_epic_without_cli_is_actionable(tmp_path, monkeypatch):
    provider = EpicProvider(tmp_path)
    monkeypatch.setattr(provider, "executable", lambda: None)
    status = await provider.connection_status()
    assert status == {
        "state": "unavailable",
        "message": "epic.tool_missing",
        "action": "install_cli",
    }


@pytest.mark.asyncio
async def test_epic_install_sizes_use_base_language_tag(tmp_path, monkeypatch):
    executable = tmp_path / "legendary"
    executable.touch(mode=0o755)
    provider = EpicProvider(tmp_path / "data", runner=InstallInfoRunner())
    monkeypatch.setattr(provider, "executable", lambda: executable)
    sizes = await provider.install_sizes("SampleApp")
    assert sizes == {"required_bytes": 700, "download_bytes": 600}


def test_legendary_021_status_shape_is_supported(tmp_path):
    provider = EpicProvider(tmp_path)
    assert provider._is_signed_in({"account": "Deck User"})
    assert not provider._is_signed_in({"account": "<not logged in>"})
    assert provider._account_name({"account": "Deck User"}) == "Deck User"


def test_authorization_code_accepts_raw_code_or_json():
    assert EpicProvider._normalize_authorization_code("abc123") == "abc123"
    assert EpicProvider._normalize_authorization_code(
        '{"authorizationCode":"code-from-json"}'
    ) == "code-from-json"


def test_authorization_code_rejects_invalid_input():
    with pytest.raises(ValueError, match="error.invalid_auth_format"):
        EpicProvider._normalize_authorization_code("contains spaces")


def test_cached_status_does_not_start_legendary(tmp_path, monkeypatch):
    executable = tmp_path / "tools" / "legendary"
    executable.parent.mkdir()
    executable.touch(mode=0o755)
    provider = EpicProvider(tmp_path)
    monkeypatch.setattr(provider, "executable", lambda: executable)
    assert provider.cached_connection_status()["state"] == "disconnected"
    user_file = tmp_path / "config" / "legendary" / "user.json"
    user_file.parent.mkdir(parents=True)
    user_file.write_text('{"displayName":"Fast User"}', encoding="utf-8")
    status = provider.cached_connection_status()
    assert status["state"] == "connected"
    assert status["account"] == "Fast User"


@pytest.mark.asyncio
async def test_expired_epic_login_is_reported_without_starting_legendary(
    tmp_path, monkeypatch
):
    executable = tmp_path / "tools" / "legendary"
    executable.parent.mkdir()
    executable.touch(mode=0o755)
    provider = EpicProvider(tmp_path, runner=UnexpectedRunner())
    monkeypatch.setattr(provider, "executable", lambda: executable)
    user_file = tmp_path / "config" / "legendary" / "user.json"
    user_file.parent.mkdir(parents=True)
    user_file.write_text(
        json.dumps(
            {
                "displayName": "Expired User",
                "refresh_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    assert await provider.connection_status() == {
        "state": "disconnected",
        "message": "epic.login_expired",
    }


@pytest.mark.asyncio
async def test_offline_epic_status_falls_back_to_unexpired_cached_login(
    tmp_path, monkeypatch
):
    executable = tmp_path / "tools" / "legendary"
    executable.parent.mkdir()
    executable.touch(mode=0o755)
    provider = EpicProvider(tmp_path, runner=OfflineRunner())
    monkeypatch.setattr(provider, "executable", lambda: executable)
    user_file = tmp_path / "config" / "legendary" / "user.json"
    user_file.parent.mkdir(parents=True)
    user_file.write_text(
        json.dumps(
            {
                "displayName": "Offline User",
                "refresh_expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    status = await provider.connection_status()

    assert status["state"] == "connected"
    assert status["account"] == "Offline User"
    assert status["offline"] is True


@pytest.mark.asyncio
async def test_offline_epic_library_uses_structured_sync_error(tmp_path, monkeypatch):
    executable = tmp_path / "tools" / "legendary"
    executable.parent.mkdir()
    executable.touch(mode=0o755)
    provider = EpicProvider(tmp_path, runner=OfflineRunner())
    monkeypatch.setattr(provider, "executable", lambda: executable)

    with pytest.raises(RuntimeError, match="epic.sync_failed"):
        await provider.library()


def test_epic_installed_state_comes_from_legendary_record(tmp_path):
    provider = EpicProvider(tmp_path)
    assert not provider.is_installed("SampleApp")
    install_path = tmp_path / "games" / "sample"
    install_path.mkdir(parents=True)
    (install_path / "game.exe").touch()
    installed_file = tmp_path / "config" / "legendary" / "installed.json"
    installed_file.parent.mkdir(parents=True)
    installed_file.write_text(
        '{"SampleApp":{"install_path":"'
        + str(install_path)
        + '","executable":"game.exe"}}',
        encoding="utf-8",
    )
    assert provider.is_installed("SampleApp")
    assert not provider.is_installed("OtherApp")


@pytest.mark.asyncio
async def test_epic_uninstall_uses_legendary(tmp_path, monkeypatch):
    executable = tmp_path / "legendary"
    executable.touch(mode=0o755)
    runner = RecordingRunner()
    provider = EpicProvider(tmp_path / "data", runner=runner)
    monkeypatch.setattr(provider, "executable", lambda: executable)
    await provider.uninstall("SampleApp")
    assert runner.arguments == ("-y", "uninstall", "SampleApp", "--skip-uninstaller")


@pytest.mark.asyncio
async def test_epic_auth_marks_authorization_code_as_sensitive(tmp_path, monkeypatch):
    executable = tmp_path / "legendary"
    executable.touch(mode=0o755)
    runner = RecordingRunner()
    provider = EpicProvider(tmp_path / "data", runner=runner)
    monkeypatch.setattr(provider, "executable", lambda: executable)
    monkeypatch.setattr(provider, "connection_status", connected_status)
    await provider.authenticate("secret-code")
    assert runner.arguments == ("auth", "--code", "secret-code")
    assert runner.kwargs["sensitive_arguments"] == frozenset({2})


@pytest.mark.asyncio
async def test_epic_update_check_is_cached(tmp_path, monkeypatch):
    executable = tmp_path / "legendary"
    executable.touch(mode=0o755)
    provider = EpicProvider(tmp_path / "data", runner=UpdateRunner())
    monkeypatch.setattr(provider, "executable", lambda: executable)

    updates = await provider.check_updates()

    assert updates["SampleApp"]["update_available"] is True
    assert updates["SampleApp"]["latest_version"] == "1.1"
    assert provider.cached_update("CurrentApp")["update_available"] is False


def test_partial_uninstall_only_removes_owned_game_files(tmp_path):
    provider = EpicProvider(tmp_path / "provider")
    metadata = provider.data_directory / "config" / "legendary" / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "SampleApp.json").write_text(
        '{"metadata":{"customAttributes":{"FolderName":{"value":"SampleGame"}}}}',
        encoding="utf-8",
    )
    temporary = provider.data_directory / "config" / "legendary" / "tmp"
    temporary.mkdir(parents=True)
    (temporary / "SampleApp.resume").write_text("resume", encoding="utf-8")
    install_root = tmp_path / "games"
    game_folder = install_root / "SampleGame"
    game_folder.mkdir(parents=True)
    (game_folder / "game.exe").write_text("partial", encoding="utf-8")
    other_folder = install_root / "KeepMe"
    other_folder.mkdir()

    provider.remove_partial_install("SampleApp", install_root)

    assert not game_folder.exists()
    assert not (temporary / "SampleApp.resume").exists()
    assert other_folder.is_dir()
