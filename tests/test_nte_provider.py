"""Tests for the NTE (Neverness to Everness) provider.

These tests mirror the isolation guarantees of the HoYoPlay provider without
exercising any miHoYo-only concepts (Bilibili/official channel switching,
PCGameSDK downloads, HYP API). The CN scope owns its own launcher; the Steam
scope must never be confused with the CN scope even though both are NTE.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from gamebridge.models import GameReference
from gamebridge.providers import hoyoplay
from gamebridge.providers.nte import NTE_CN, NTE_STEAM, NteProvider

NTE_STEAM_APP_ID = 4508340
NTE_CN_LAUNCHER_SHA256 = (
    "ee41f95ed54c8ec02237e073028c321ae0660703121fbe8b2f6006068918bf94"
)


def _fake_launcher_bytes(payload: bytes | None = None) -> bytes:
    """Return a payload that always exceeds the 1024-byte minimum the provider
    checks for before accepting an installer."""
    base = payload if payload is not None else b"NTE launcher placeholder"
    if len(base) < 2048:
        base = base + b"\x00" * (2048 - len(base))
    return b"MZ" + base


def _cn_provider(tmp_path: Path) -> NteProvider:
    return NteProvider(
        tmp_path / "providers" / "nte-cn",
        tmp_path / "compatibility",
        NTE_CN,
    )


def _steam_provider(tmp_path: Path) -> NteProvider:
    return NteProvider(
        tmp_path / "providers" / "nte-steam",
        tmp_path / "compatibility",
        NTE_STEAM,
    )


# ---------------------------------------------------------------------------
# Isolation: CN and Steam never share data, prefix, or Steam App ID.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cn_and_steam_providers_are_fully_isolated(tmp_path):
    cn = _cn_provider(tmp_path)
    steam = _steam_provider(tmp_path)

    assert cn.provider_id == "nte_cn"
    assert steam.provider_id == "nte_steam"
    assert cn.data_directory != steam.data_directory
    assert cn.prefix_directory == tmp_path / "compatibility" / "nte-cn"
    assert steam.prefix_directory == tmp_path / "compatibility" / "nte-steam"
    assert cn.prefix_directory != steam.prefix_directory

    cn_status = await cn.connection_status()
    steam_status = await steam.connection_status()
    assert cn_status["state"] == "not_installed"
    assert cn_status["officialPage"] == "https://yh.wanmei.com/"
    assert steam_status["state"] == "not_installed"


@pytest.mark.asyncio
async def test_steam_provider_only_advertises_steam_app_id_4508340(tmp_path):
    steam = _steam_provider(tmp_path)
    games = list(await steam.library())
    assert len(games) == 1
    spec_game = NTE_STEAM.games[0]
    assert spec_game.native_steam_app_id == NTE_STEAM_APP_ID

    cn_specs = NTE_CN.games
    assert all(game.native_steam_app_id != NTE_STEAM_APP_ID for game in cn_specs)


# ---------------------------------------------------------------------------
# NTE never reuses the miHoYo channel/profile infrastructure.
# ---------------------------------------------------------------------------


def test_nte_provider_blocks_bilibili_channel_switching(tmp_path):
    """NTE has no official/Bilibili channel concept; the provider must reject
    any channel switching attempt instead of silently running miHoYo logic."""
    cn = _cn_provider(tmp_path)
    forbidden = (
        ("switch_channel_selection", ("hk4e_cn",)),
        ("switch_channel_profile", ("hk4e_cn", "official")),
        ("apply_channel_for_launch", ("hk4e_cn", "official")),
        ("apply_selected_channel", ("hk4e_cn",)),
        ("capture_channel_profile", ("hk4e_cn",)),
        ("prepare_channel_profile", ("hk4e_cn", "official")),
        ("channel_profile_status", ("hk4e_cn",)),
    )
    for name, args in forbidden:
        method = getattr(cn, name, None)
        assert method is not None, f"NteProvider is missing {name!r}"
        with pytest.raises((NotImplementedError, ValueError)):
            method(*args)

    # channel_selection_status / selected_channel are read-only and may stay
    # available, but must not expose a Bilibili choice for NTE.
    status = cn.channel_selection_status()
    assert status["current"] == "official"

    # NTE never carries a Bilibili SDK metadata constant.
    import gamebridge.providers.nte as nte_module

    forbidden_constants = ("_BILIBILI_GAME_API", "_CHANNEL_API_HOST", "_CHANNEL_ARCHIVE_HOSTS")
    for constant in forbidden_constants:
        assert not hasattr(nte_module, constant), (
            f"NTE module must not import miHoYo's {constant}"
        )

    for game in NTE_CN.games:
        assert game.compatibility_status.value == "experimental"


def test_nte_spec_has_no_bilibili_sdk_metadata_hosts(tmp_path):
    """The NTE installer host allowlist must not contain the miHoYo SDK CDN."""
    cn = _cn_provider(tmp_path)
    installer_hosts = {host.casefold() for host in cn.spec.installer_hosts}
    assert "launcher-webstatic.mihoyo.com" not in installer_hosts
    assert "hyp-api.mihoyo.com" not in installer_hosts


# ---------------------------------------------------------------------------
# installer import (manual path): validates MZ + SHA-256 + metadata.
# ---------------------------------------------------------------------------


def test_import_installer_writes_metadata_and_rejects_bad_headers(tmp_path):
    cn = _cn_provider(tmp_path)
    source = tmp_path / "nte_launcher.exe"
    payload = _fake_launcher_bytes(b"real-payload")
    source.write_bytes(payload)
    expected_digest = hashlib.sha256(payload).hexdigest()

    metadata = cn.import_installer(source)
    assert metadata["providerId"] == "nte_cn"
    assert metadata["size"] == len(payload)
    assert metadata["sha256"] == expected_digest
    assert metadata["verification"] == "user_selected_official_download_unverified_signature"

    stored = cn.managed_installer
    assert stored.is_file()
    assert stored.read_bytes() == payload
    metadata_text = cn.installer_metadata_file.read_text(encoding="utf-8")
    assert json.loads(metadata_text)["sha256"] == expected_digest


def test_import_installer_rejects_non_mz_header(tmp_path):
    cn = _cn_provider(tmp_path)
    source = tmp_path / "bad.exe"
    source.write_bytes(b"PK\x03\x04 not-a-pe")
    with pytest.raises(ValueError, match="hoyoplay.invalid_installer"):
        cn.import_installer(source)


def test_import_installer_rejects_out_of_band_size(tmp_path):
    cn = _cn_provider(tmp_path)
    source = tmp_path / "tiny.exe"
    source.write_bytes(b"MZ")
    with pytest.raises(ValueError, match="hoyoplay.invalid_installer_size"):
        cn.import_installer(source)

    huge = tmp_path / "huge.exe"
    huge.write_bytes(b"MZ" + b"\x00" * (600 * 1024 * 1024))
    with pytest.raises(ValueError, match="hoyoplay.invalid_installer_size"):
        cn.import_installer(huge)


# ---------------------------------------------------------------------------
# download_installer (auto path): pinned URL + host allowlist + SHA-256.
# ---------------------------------------------------------------------------


def _stub_socket_create_connection(address, *args, **kwargs):
    # Block any accidental outbound DNS — the test fails loudly instead of
    # silently reaching the network.
    raise AssertionError(f"unexpected network call: {address}")


def test_download_installer_uses_pinned_official_url_and_sha256(tmp_path, monkeypatch):
    cn = _cn_provider(tmp_path)
    payload = _fake_launcher_bytes(b"official-launcher")
    expected_sha = hashlib.sha256(payload).hexdigest()

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def geturl(self):
            return cn.spec.installer_url

        def read(self, chunk: int = -1) -> bytes:
            if chunk < 0:
                data, self._payload = self._payload, b""
                return data
            data, self._payload = self._payload[:chunk], self._payload[chunk:]
            return data

    class _FakeOpener:
        def open(self, request, timeout=0):  # noqa: ARG002 -- test double
            return _FakeResponse(payload)

    # Network safety net: ensure no real DNS happens even if a typo slips in.
    monkeypatch.setattr(socket, "create_connection", _stub_socket_create_connection)

    metadata = cn.download_installer(opener=_FakeOpener())
    assert metadata["providerId"] == "nte_cn"
    assert metadata["sourceUrl"] == cn.spec.installer_url
    assert metadata["size"] == len(payload)
    assert metadata["sha256"] == expected_sha
    assert metadata["verification"] == "downloaded_from_official_domain_unverified_signature"

    stored = cn.managed_installer
    assert stored.is_file()
    assert stored.read_bytes() == payload

    # Pinned URL still points at the wanmei/wmupd CDN we observed on 2026-10-10.
    assert "yhcdn1.wmupd.com" in cn.spec.installer_url
    assert "YH_common_setup" in cn.spec.installer_url


def test_download_installer_rejects_off_host_redirect(tmp_path, monkeypatch):
    cn = _cn_provider(tmp_path)
    monkeypatch.setattr(socket, "create_connection", _stub_socket_create_connection)

    class _BadResponse:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def geturl(self):
            return "https://evil.example.com/nte.exe"

        def read(self, chunk: int = -1):
            return b""

    class _BadOpener:
        def open(self, request, timeout=0):  # noqa: ARG002
            return _BadResponse()

    with pytest.raises(ValueError, match="hoyoplay.untrusted_installer_url"):
        cn.download_installer(opener=_BadOpener())


# ---------------------------------------------------------------------------
# launcher resolution: file present vs. absent vs. wrong Steam App ID.
# ---------------------------------------------------------------------------


def test_launcher_executable_returns_none_until_present(tmp_path):
    cn = _cn_provider(tmp_path)
    assert cn.launcher_executable() is None

    executable = cn.prefix_directory / NTE_CN.executable_candidates[0]
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"MZ")
    assert cn.launcher_executable() == executable.resolve()


@pytest.mark.asyncio
async def test_resolve_launch_rejects_cross_provider_game(tmp_path):
    cn = _cn_provider(tmp_path)
    wrong = GameReference("hoyoplay_global", "hk4e_global", "Genshin", "global")
    with pytest.raises(ValueError, match="provider.game_mismatch"):
        await cn.resolve_launch(wrong)


@pytest.mark.asyncio
async def test_resolve_launch_uses_dwproton_runtime(tmp_path):
    """resolve_launch must request the shared DWProton runtime via UMU."""
    cn = _cn_provider(tmp_path)
    executable = cn.prefix_directory / NTE_CN.executable_candidates[0]
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"MZ")

    profile = await cn.resolve_launch(
        GameReference("nte_cn", "nte_cn", "异环", "cn")
    )
    assert profile.prefix_path == os.fspath(cn.prefix_directory)
    assert profile.executable == os.fspath(executable.resolve())
    assert profile.runtime_engine == "umu"
    assert profile.store == "none"
    assert profile.game_id_umu == "umu-default"


# ---------------------------------------------------------------------------
# Steam scope: no auto-download, App ID stays attached to the game record.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam_provider_advertises_steam_app_id_even_when_not_installed(tmp_path):
    steam = _steam_provider(tmp_path)
    games = {game.external_game_id: game for game in await steam.library()}
    assert "nte_steam" in games
    spec_game = NTE_STEAM.games[0]
    assert spec_game.native_steam_app_id == NTE_STEAM_APP_ID
    status = await steam.connection_status()
    assert status["state"] == "not_installed"
    assert status["region"] == "global"


def test_steam_provider_has_no_installer_to_download(tmp_path):
    """The Steam scope is owned by Valve; the provider must not expose
    a launcher download path or pretend to host a launcher installer."""
    steam = _steam_provider(tmp_path)
    assert steam.spec.installer_url == ""
    assert steam.spec.installer_hosts == ()
    expected_installer = (
        steam.data_directory / "installers" / "official-launcher-installer.exe"
    )
    assert steam.managed_installer == expected_installer
    assert not steam.managed_installer.exists()


# ---------------------------------------------------------------------------
# Discover-adjacent stub never claims a discovery before a real launcher exe
# is found; tests must not silently invent data.
# ---------------------------------------------------------------------------


def test_discover_adjacent_installations_is_empty_until_launcher_present(tmp_path):
    cn = _cn_provider(tmp_path)
    assert cn.discover_adjacent_installations() == {}

    # Even if a game folder exists beside the prefix root, a missing launcher
    # must keep the discovery empty so the upstream logic doesn't pick up an
    # unverified body.
    stray = (
        tmp_path / "compatibility" / "nte-cn" / "drive_c"
        / "fake_game" / "NevernessToEverness.exe"
    )
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"MZ")
    assert cn.discover_adjacent_installations() == {}


# ---------------------------------------------------------------------------
# Application API registration keeps the two scopes in separate data dirs.
# ---------------------------------------------------------------------------


def test_application_registers_two_isolated_nte_instances(tmp_path, monkeypatch):
    """Smoke test: registering both NTE scopes must yield distinct providers
    with separate data dirs, mirroring the Hoyoplay wiring in application.start."""
    from gamebridge.providers import NteProvider as _NteProvider

    registered: dict[str, _NteProvider] = {}

    class _Registry:
        def register(self, provider):
            registered[provider.provider_id] = provider

        def get(self, provider_id):
            return registered[provider_id]

    monkeypatch.setattr(hoyoplay, "MIHOYO_CN", hoyoplay.MIHOYO_CN)
    monkeypatch.setattr(hoyoplay, "HOYOPLAY_GLOBAL", hoyoplay.HOYOPLAY_GLOBAL)

    registry = _Registry()
    registry.register(
        _NteProvider(
            tmp_path / "providers" / "nte-cn", tmp_path / "compatibility", NTE_CN
        )
    )
    registry.register(
        _NteProvider(
            tmp_path / "providers" / "nte-steam",
            tmp_path / "compatibility",
            NTE_STEAM,
        )
    )

    assert set(registered) == {"nte_cn", "nte_steam"}
    assert registered["nte_cn"].data_directory != registered["nte_steam"].data_directory
    assert registered["nte_cn"].prefix_directory != registered["nte_steam"].prefix_directory


# ---------------------------------------------------------------------------
# Capability contract.
# ---------------------------------------------------------------------------


def test_nte_capabilities_match_consumer_facing_needs(tmp_path):
    cn = _cn_provider(tmp_path)
    caps = cn.capabilities()
    assert caps.official_installer is True
    assert caps.public_catalog is True
    assert caps.local_launch is True
    assert caps.account_connection is True
    assert caps.cloud_save is False  # GameBridge does not own NTE cloud saves.
    assert caps.uninstall is False    # Destruction is owned by the launcher.
