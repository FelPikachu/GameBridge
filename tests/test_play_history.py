from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gamebridge.play_history import (
    apply_staged_history,
    export_history,
    import_history,
    merge_history_store,
    read_app_history,
    read_history_store,
    stage_history_import,
)
from gamebridge.application import GameBridgeApplication


def test_application_uses_system_python_for_detached_history_worker() -> None:
    source = Path("gamebridge/application.py").read_text(encoding="utf-8")
    assert '"/usr/bin/python3"' in source
    assert "sys.executable" not in source


def test_managed_history_includes_both_mihoyo_regions() -> None:
    source = Path("gamebridge/application.py").read_text(encoding="utf-8")
    assert "('epic', 'mihoyo_cn', 'hoyoplay_global')" in source


def test_latest_play_history_export_skips_newer_invalid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    valid = desktop / "GameBridge-play-history-20260823-120000.json"
    invalid = desktop / "GameBridge-play-history-20260823-130000.json"
    valid.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1, "games": [],
    }), encoding="utf-8")
    invalid.write_text("not json", encoding="utf-8")
    invalid.touch()
    monkeypatch.setenv("HOME", str(tmp_path))

    app = object.__new__(GameBridgeApplication)
    assert app.latest_play_history_export() == str(valid)
    assert [item["path"] for item in app.play_history_exports()] == [str(valid)]


def test_detached_system_python_worker_applies_staged_history(tmp_path: Path) -> None:
    config = _localconfig(tmp_path)
    sleeper = subprocess.Popen(["/usr/bin/sleep", "0.1"])
    fields = Path(f"/proc/{sleeper.pid}/stat").read_text(encoding="utf-8").split()
    pending = tmp_path / "pending.json"
    result = tmp_path / "result.json"
    pending.write_text(json.dumps({
        "format": "gamebridge.pending-play-history",
        "version": 1,
        "userHome": str(tmp_path),
        "steamPid": sleeper.pid,
        "steamStartTime": fields[21],
        "updates": {"111": {"playtimeMinutes": 34, "lastPlayed": 200}},
    }), encoding="utf-8")

    worker = subprocess.Popen(
        ["/usr/bin/python3", "gamebridge/play_history_worker.py", str(pending), str(result)],
    )
    sleeper.wait(timeout=5)
    returncode = worker.wait(timeout=5)

    assert returncode == 0
    assert json.loads(result.read_text(encoding="utf-8"))["ok"] is True
    assert read_app_history(config.read_text(encoding="utf-8"))[111] == {
        "playtimeMinutes": 34,
        "lastPlayed": 200,
    }
    assert not pending.exists()


def _localconfig(home: Path) -> Path:
    path = home / ".local/share/Steam/userdata/123/config/localconfig.vdf"
    path.parent.mkdir(parents=True)
    path.write_text(
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
        '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n'
        '\t\t\t\t\t"111"\n\t\t\t\t\t{\n\t\t\t\t\t\t"Playtime"\t\t"12"\n'
        '\t\t\t\t\t\t"LastPlayed"\t\t"100"\n\t\t\t\t\t\t"Cloud"\n'
        '\t\t\t\t\t\t{\n\t\t\t\t\t\t\t"Keep"\t\t"yes"\n\t\t\t\t\t\t}\n'
        '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )
    return path


def _games() -> list[dict[str, object]]:
    return [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 111, "title": "LEGO"}]


def test_export_and_import_merge_without_overwriting_newer_values(tmp_path: Path) -> None:
    config = _localconfig(tmp_path)
    exported = export_history(tmp_path, _games())
    payload = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))
    assert payload["format"] == "gamebridge.play-history"
    assert payload["games"][0]["playtimeMinutes"] == 12

    payload["games"][0]["playtimeMinutes"] = 50
    payload["games"][0]["lastPlayed"] = 90
    source = tmp_path / "import.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = import_history(tmp_path, source, _games())

    assert result["matched"] == 1
    assert result["updated"] == 1
    assert read_app_history(config.read_text(encoding="utf-8"))[111] == {
        "playtimeMinutes": 50,
        "lastPlayed": 100,
    }
    assert '"Keep"\t\t"yes"' in config.read_text(encoding="utf-8")
    assert Path(str(result["backupPath"])).is_file()


def test_export_prefers_live_steam_values(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    exported = export_history(
        tmp_path, _games(),
        [{"steamAppId": 111, "playtimeMinutes": 77, "lastPlayed": 300}],
    )
    record = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))["games"][0]
    assert record["playtimeMinutes"] == 77
    assert record["lastPlayed"] == 300


def test_import_matches_stable_identity_after_app_id_change(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    payload = {
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 999,
                   "playtimeMinutes": 20, "lastPlayed": 200}],
    }
    source = tmp_path / "import.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = import_history(tmp_path, source, _games())
    assert result["matched"] == 1


def test_import_creates_history_for_a_new_shortcut_app_id(tmp_path: Path) -> None:
    config = _localconfig(tmp_path)
    games = [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 0xF1234567, "title": "LEGO"}]
    source = tmp_path / "import.json"
    source.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 111,
                   "playtimeMinutes": 55, "lastPlayed": 300}],
    }), encoding="utf-8")
    result = import_history(tmp_path, source, games)
    restored = read_app_history(config.read_text(encoding="utf-8"))[0xF1234567]
    assert result["updated"] == 1
    assert restored == {"playtimeMinutes": 55, "lastPlayed": 300}


def test_import_rejects_invalid_format_without_backup(tmp_path: Path) -> None:
    config = _localconfig(tmp_path)
    source = tmp_path / "import.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="play_history.invalid_format"):
        import_history(tmp_path, source, _games())
    assert config.read_text(encoding="utf-8")
    assert not list(config.parent.glob("*.gamebridge-backup-*"))


def test_staged_import_does_not_modify_config_before_steam_exits(tmp_path: Path) -> None:
    config = _localconfig(tmp_path)
    source = tmp_path / "import.json"
    source.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 999,
                   "playtimeMinutes": 55, "lastPlayed": 300}],
    }), encoding="utf-8")
    pending = tmp_path / "pending.json"

    result = stage_history_import(tmp_path, source, _games(), pending, 123, "456")

    assert read_app_history(config.read_text(encoding="utf-8"))[111]["playtimeMinutes"] == 12
    assert result["nonEmpty"] == 1
    assert pending.is_file()

    applied = apply_staged_history(pending)
    assert applied["updated"] == 1
    assert read_app_history(config.read_text(encoding="utf-8"))[111] == {
        "playtimeMinutes": 55,
        "lastPlayed": 300,
    }
    assert not pending.exists()


def test_staged_import_reports_an_empty_backup(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    source = tmp_path / "import.json"
    source.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 999,
                   "playtimeMinutes": 0, "lastPlayed": 0}],
    }), encoding="utf-8")
    result = stage_history_import(tmp_path, source, _games(), tmp_path / "pending.json", 123, "456")
    assert result["nonEmpty"] == 0


def test_staged_import_does_not_keep_a_second_playtime_counter(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    source = tmp_path / "import.json"
    source.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 999,
                   "playtimeMinutes": 55, "lastPlayed": 300}],
    }), encoding="utf-8")
    store = tmp_path / "data/play-history.json"

    stage_history_import(tmp_path, source, _games(), tmp_path / "pending.json", 123, "456", store)

    assert read_history_store(store) == {}


def test_history_store_only_merges_newer_values(tmp_path: Path) -> None:
    store = tmp_path / "play-history.json"
    merge_history_store(store, [{"providerId": "epic", "externalGameId": "lego",
                                 "playtimeMinutes": 55, "lastPlayed": 300}])
    merge_history_store(store, [{"providerId": "epic", "externalGameId": "lego",
                                 "playtimeMinutes": 20, "lastPlayed": 200}])
    assert read_history_store(store)[("epic", "lego")] == {
        "playtimeMinutes": 55,
        "lastPlayed": 300,
    }


def test_staged_import_uses_steam_as_the_only_counter(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    source = tmp_path / "import.json"
    source.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 111,
                   "playtimeMinutes": 3, "lastPlayed": 500}],
    }), encoding="utf-8")
    store = tmp_path / "play-history.json"
    stage_history_import(
        tmp_path, source, _games(), tmp_path / "pending.json", 123, "456", store,
        [{"steamAppId": 111, "playtimeMinutes": 1, "lastPlayed": 500}],
    )
    assert read_history_store(store) == {}
    pending = json.loads((tmp_path / "pending.json").read_text(encoding="utf-8"))
    assert pending["updates"]["111"] == {
        "playtimeMinutes": 3,
        "lastPlayed": 500,
    }


def test_staged_import_never_replaces_newer_live_steam_values(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    source = tmp_path / "import.json"
    source.write_text(json.dumps({
        "format": "gamebridge.play-history", "version": 1,
        "games": [{"providerId": "epic", "externalGameId": "lego", "steamAppId": 111,
                   "playtimeMinutes": 3, "lastPlayed": 200}],
    }), encoding="utf-8")
    pending = tmp_path / "pending.json"
    stage_history_import(
        tmp_path, source, _games(), pending, 123, "456", runtime=[{
            "steamAppId": 111, "playtimeMinutes": 77, "lastPlayed": 500,
        }],
    )
    assert json.loads(pending.read_text(encoding="utf-8"))["updates"]["111"] == {
        "playtimeMinutes": 77,
        "lastPlayed": 500,
    }


def test_export_ignores_legacy_gamebridge_history_store(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    exported = export_history(
        tmp_path,
        _games(),
        stored={("epic", "lego"): {"playtimeMinutes": 88, "lastPlayed": 400}},
    )
    record = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))["games"][0]
    assert record["playtimeMinutes"] == 12
    assert record["lastPlayed"] == 400


def test_export_does_not_add_legacy_history_to_native_playtime(tmp_path: Path) -> None:
    _localconfig(tmp_path)
    exported = export_history(
        tmp_path,
        _games(),
        runtime=[{"steamAppId": 111, "playtimeMinutes": 1, "lastPlayed": 500}],
        stored={("epic", "lego"): {"playtimeMinutes": 2, "lastPlayed": 400}},
    )
    record = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))["games"][0]
    assert record["playtimeMinutes"] == 12
    assert record["lastPlayed"] == 500
