import json
import os
import tarfile

import pytest

from gamebridge.compatibility import CompatibilityManager
from gamebridge.tooling import DWPROTON_RELEASE, GE_PROTON_RELEASE, ToolInstaller, ToolRelease


def test_compatibility_uses_shared_runtime_and_isolated_prefixes(tmp_path, monkeypatch):
    manager = CompatibilityManager(tmp_path)
    ge = tmp_path / "GE-Proton10-34"
    experimental = tmp_path / "Proton Experimental"
    ge.mkdir()
    experimental.mkdir()
    monkeypatch.setattr(
        manager,
        "proton_layers",
        lambda: [("GE-Proton10-34", ge), ("Proton Experimental", experimental)],
    )

    assert manager.selected_proton("epic", "one") == ("GE-Proton10-34", ge)
    assert manager.prefix("epic", "one") != manager.prefix("epic", "two")

    manager.set_selected_proton("epic", "one", "Proton Experimental")
    assert manager.selected_proton("epic", "one") == (
        "Proton Experimental",
        experimental,
    )


def test_compatibility_resolves_umu_id_then_falls_back(tmp_path):
    manager = CompatibilityManager(tmp_path)
    manager.cache_file.parent.mkdir(parents=True)
    manager.cache_file.write_text(
        json.dumps(
            [
                {"title": "Example Game", "codename": "ExampleCode", "umu_id": "umu-123"}
            ]
        ),
        encoding="utf-8",
    )

    assert manager.umu_id("ExampleCode") == "umu-123"
    assert manager.umu_id("unknown", "Example Game") == "umu-123"
    assert manager.umu_id("unknown", "Missing") == "umu-default"


def test_compatibility_finds_proton_in_external_steam_library(tmp_path, monkeypatch):
    home = tmp_path / "home"
    library = tmp_path / "external" / "SteamLibrary"
    proton = library / "steamapps/common/Proton Hotfix/proton"
    proton.parent.mkdir(parents=True)
    proton.write_text("#!/bin/sh\n", encoding="utf-8")
    proton.chmod(0o755)
    monkeypatch.setattr("gamebridge.compatibility.Path.home", lambda: home)
    monkeypatch.setattr(
        "gamebridge.compatibility.steam_library_paths",
        lambda current_home: [library] if current_home == home else [],
    )

    layers = CompatibilityManager(tmp_path / "data").proton_layers()

    assert layers == [("Proton Hotfix", proton.parent.resolve())]


def test_prepare_installs_umu_and_verified_default_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    manager = CompatibilityManager(tmp_path / "data")
    installed: list[str] = []
    monkeypatch.setattr("gamebridge.compatibility.Path.home", lambda: home)

    def install_umu(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        installed.append("umu")

    def install_runtime(release, root):
        proton = root / release.version / "proton"
        proton.parent.mkdir(parents=True, exist_ok=True)
        proton.touch()
        proton.chmod(0o755)
        installed.append(release.version)
        return {"version": release.version, "path": os.fspath(proton.parent)}

    monkeypatch.setattr(manager.installer, "install_umu", install_umu)
    monkeypatch.setattr(manager.installer, "install_compatibility_tool", install_runtime)
    monkeypatch.setattr(manager, "refresh_database", lambda: [])

    status = manager.prepare()

    assert installed == ["umu", GE_PROTON_RELEASE.version]
    assert status["ready"] is True


def test_on_demand_base_prepare_does_not_download_a_large_proton_runtime(
    tmp_path, monkeypatch
):
    manager = CompatibilityManager(tmp_path / "data")
    installed: list[str] = []

    def install_umu(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        installed.append("umu")

    monkeypatch.setattr(manager.installer, "install_umu", install_umu)
    monkeypatch.setattr(
        manager.installer,
        "install_compatibility_tool",
        lambda *_args, **_kwargs: installed.append("proton"),
    )

    manager.prepare_base()

    assert installed == ["umu"]


def test_status_does_not_treat_an_arbitrary_proton_as_prepared(tmp_path, monkeypatch):
    manager = CompatibilityManager(tmp_path / "data")
    monkeypatch.setattr("gamebridge.compatibility.Path.home", lambda: tmp_path / "home")
    manager.umu_executable.parent.mkdir(parents=True)
    manager.umu_executable.touch()
    hotfix = tmp_path / "Proton Hotfix"
    hotfix.mkdir()
    monkeypatch.setattr(manager, "proton_layers", lambda: [("Proton Hotfix", hotfix)])

    assert manager.status()["ready"] is False


def test_verified_default_runtime_is_ranked_before_other_ge_builds(tmp_path, monkeypatch):
    home = tmp_path / "home"
    default = home / ".local/share/Steam/compatibilitytools.d" / GE_PROTON_RELEASE.version
    older = home / ".local/share/Steam/compatibilitytools.d/GE-Proton10-1"
    for runtime in (default, older):
        runtime.mkdir(parents=True)
        proton = runtime / "proton"
        proton.touch()
        proton.chmod(0o755)
    monkeypatch.setattr("gamebridge.compatibility.Path.home", lambda: home)
    monkeypatch.setattr("gamebridge.compatibility.steam_library_paths", lambda _home: [])

    layers = CompatibilityManager(tmp_path / "data").proton_layers()

    assert layers[0] == (GE_PROTON_RELEASE.version, default.resolve())


def test_steam_per_game_compatibility_override_has_highest_priority(tmp_path, monkeypatch):
    manager = CompatibilityManager(tmp_path)
    ge = tmp_path / "GE-Proton10-34"
    experimental = tmp_path / "Proton Experimental"
    ge.mkdir()
    experimental.mkdir()
    monkeypatch.setattr(
        manager,
        "proton_layers",
        lambda: [("GE-Proton10-34", ge), ("Proton Experimental", experimental)],
    )
    monkeypatch.setattr(
        manager, "steam_compatibility_override", lambda app_id: "proton_experimental"
    )
    manager.set_selected_proton("epic", "one", "GE-Proton10-34")

    assert manager.selected_proton("epic", "one", 4_003_555_155) == (
        "Proton Experimental",
        experimental,
    )


def test_vdf_block_reads_nested_compatibility_mapping():
    text = '''"CompatToolMapping"
    {
        "4003555155"
        {
            "name" "proton_experimental"
            "config" ""
        }
    }'''
    mapping = CompatibilityManager._vdf_block(text, "CompatToolMapping")
    assert mapping is not None
    game = CompatibilityManager._vdf_block(mapping, "4003555155")
    assert game is not None
    assert '"name" "proton_experimental"' in game


def test_hoyoplay_runtime_mapping_uses_dw_except_for_bh3(tmp_path, monkeypatch):
    manager = CompatibilityManager(tmp_path)
    installed = []
    monkeypatch.setattr(
        manager.installer,
        "install_compatibility_tool",
        lambda release, root: installed.append((release, root)) or {"version": release.version},
    )
    monkeypatch.setattr("gamebridge.compatibility.Path.home", lambda: tmp_path)

    assert manager.ensure_hoyoplay_runtime("hk4e_cn")["version"] == DWPROTON_RELEASE.version
    assert manager.ensure_hoyoplay_runtime("nap_global")["version"] == DWPROTON_RELEASE.version
    assert manager.ensure_hoyoplay_runtime("bh3_cn")["version"] == GE_PROTON_RELEASE.version
    assert manager.ensure_hoyoplay_runtime("bh3_global")["version"] == GE_PROTON_RELEASE.version
    assert all(root == tmp_path / ".local/share/Steam/compatibilitytools.d" for _, root in installed)


def test_compatibility_tool_install_is_verified_and_atomic(tmp_path, monkeypatch):
    source = tmp_path / "source.tar.gz"
    payload = tmp_path / "Example-Proton"
    payload.mkdir()
    proton = payload / "proton"
    proton.write_text("#!/bin/sh\n", encoding="utf-8")
    with tarfile.open(source, "w:gz") as bundle:
        bundle.add(payload, arcname=payload.name)
    import hashlib

    digest = hashlib.sha512(source.read_bytes()).hexdigest()
    release = ToolRelease("Example-Proton", "https://github.com/example/tool.tar.gz", digest)
    installer = ToolInstaller()
    monkeypatch.setattr(installer, "_download", lambda *args, **kwargs: source)

    result = installer.install_compatibility_tool(release, tmp_path / "tools")

    installed = tmp_path / "tools/Example-Proton/proton"
    assert result["path"] == os.fspath(installed.parent)
    assert installed.is_file()
    assert os.access(installed, os.X_OK)


def test_compatibility_tool_rejects_unsafe_archive_member():
    member = tarfile.TarInfo("../escape")
    try:
        ToolInstaller._validate_archive_members([member])
    except RuntimeError as error:
        assert "unsafe path" in str(error)
    else:
        raise AssertionError("unsafe member was accepted")


def test_compatibility_tool_allows_link_that_stays_inside_release_root():
    member = tarfile.TarInfo("Example-Proton/files/bin/tool")
    member.type = tarfile.SYMTYPE
    member.linkname = "../lib/tool"
    ToolInstaller._validate_archive_members([member])


def test_compatibility_tool_rejects_link_that_escapes_release_root():
    member = tarfile.TarInfo("Example-Proton/files/tool")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../escape"
    try:
        ToolInstaller._validate_archive_members([member])
    except RuntimeError as error:
        assert "unsafe link" in str(error)
    else:
        raise AssertionError("unsafe link was accepted")


@pytest.mark.asyncio
async def test_application_prepares_runtime_before_hoyoplay_game_action(
    tmp_path, monkeypatch
):
    from gamebridge.application import GameBridgeApplication

    application = GameBridgeApplication(tmp_path)
    monkeypatch.setattr(application, "prepare_compatibility", lambda: _ready())
    prepared = []
    monkeypatch.setattr(
        application.compatibility,
        "ensure_hoyoplay_runtime",
        lambda game_id: prepared.append(game_id) or {"version": "runtime", "path": "/tmp/runtime"},
    )

    result = await application.prepare_hoyoplay_game_runtime("nap_global")

    assert result["version"] == "runtime"
    assert prepared == ["nap_global"]


async def _ready():
    return {"ready": True}


@pytest.mark.asyncio
async def test_application_rejects_runtime_for_unknown_hoyoplay_game(tmp_path):
    from gamebridge.application import GameBridgeApplication

    application = GameBridgeApplication(tmp_path)
    with pytest.raises(ValueError, match="unsupported_hoyoplay_game"):
        await application.prepare_hoyoplay_game_runtime("unknown")


def test_application_only_claims_fresh_matching_steam_install_request(
    tmp_path, monkeypatch
):
    from gamebridge.application import GameBridgeApplication

    application = GameBridgeApplication(tmp_path)
    request = tmp_path / "compatibility/steam-install-request.json"
    request.parent.mkdir(parents=True)
    request.write_text(
        json.dumps({"appId": 2180100, "requestedAt": 1000}),
        encoding="utf-8",
    )
    monkeypatch.setattr("gamebridge.application.time.time", lambda: 1050)

    assert application.claim_steam_install_request(858280) == {"claimed": False}
    assert request.exists()
    assert application.claim_steam_install_request(2180100) == {"claimed": True}
    assert not request.exists()

    request.write_text("[]", encoding="utf-8")
    assert application.claim_steam_install_request(2180100) == {"claimed": False}
