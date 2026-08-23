import json
import os
from pathlib import Path

from gamebridge.cloud_saves import EpicCloudSaveManager


GAME_ID = "hogwarts-id"
ACCOUNT_ID = "account-id"


def manager(tmp_path: Path, template: str | None = None) -> EpicCloudSaveManager:
    root = tmp_path / "data"
    provider = root / "providers/epic"
    config = provider / "config/legendary"
    metadata = config / "metadata"
    metadata.mkdir(parents=True)
    attributes = {}
    if template is not None:
        attributes["CloudSaveFolder"] = {"value": template}
    (metadata / f"{GAME_ID}.json").write_text(
        json.dumps({"metadata": {"customAttributes": attributes}}), encoding="utf-8"
    )
    install = tmp_path / "games/Hogwarts"
    install.mkdir(parents=True)
    (config / "installed.json").write_text(
        json.dumps({GAME_ID: {"install_path": str(install)}}), encoding="utf-8"
    )
    (config / "user.json").write_text(
        json.dumps({"account_id": ACCOUNT_ID}), encoding="utf-8"
    )
    legendary = provider / "tools/legendary"
    legendary.parent.mkdir(parents=True)
    legendary.write_text(
        """#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if args and args[0] == 'list-saves':
    print(' + 2026.01.12-12.25.40.manifest')
    raise SystemExit(0)
direction = 'download' if '--skip-upload' in args else 'upload'
path = pathlib.Path(args[args.index('--save-path') + 1])
log = pathlib.Path(os.environ['LEGENDARY_CONFIG_PATH']).parent / 'commands.log'
with log.open('a') as stream:
    stream.write(direction + '\\n')
if direction == 'download':
    path.mkdir(parents=True, exist_ok=True)
    (path / 'HL-00-00.sav').write_bytes(b'cloud-save')
    print('Downloading remote savegame...', file=sys.stderr)
else:
    print('Uploading local savegame...', file=sys.stderr)
""",
        encoding="utf-8",
    )
    legendary.chmod(0o755)
    return EpicCloudSaveManager(root)


def test_epic_template_resolves_only_inside_the_game_prefix(tmp_path):
    cloud = manager(
        tmp_path, "{AppData}/HogwartsLegacy/Saved/SaveGames/{EpicID}/"
    )
    assert cloud.resolve_save_path(GAME_ID) == (
        tmp_path
        / "data/compatibility/prefixes/epic"
        / GAME_ID
        / "drive_c/users/steamuser/AppData/Local/HogwartsLegacy/Saved/SaveGames"
        / ACCOUNT_ID
    )


def test_unknown_or_escaping_template_is_not_supported(tmp_path):
    cloud = manager(tmp_path, "{Unknown}/../../outside")
    assert cloud.resolve_save_path(GAME_ID) is None
    assert cloud.status(GAME_ID).state == "unsupported"


def test_cloud_download_restores_an_empty_local_save_directory(tmp_path):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")
    result = cloud.sync_before_launch(GAME_ID)
    assert result.state == "downloaded"
    assert result.local_files == 1
    assert Path(result.local_path or "") .joinpath("HL-00-00.sav").read_bytes() == b"cloud-save"


def test_empty_local_save_is_never_uploaded(tmp_path):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")
    result = cloud.sync_after_exit(GAME_ID)
    assert result.state == "blocked_empty"
    log = cloud.provider_data / "config/commands.log"
    assert not log.exists()


def test_missing_previously_synced_file_blocks_upload(tmp_path):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")
    downloaded = cloud.sync_before_launch(GAME_ID)
    save = Path(downloaded.local_path or "") / "HL-00-00.sav"
    save.unlink()
    (save.parent / "new.sav").write_bytes(b"replacement")
    result = cloud.sync_after_exit(GAME_ID)
    assert result.state == "conflict_missing_files"
    commands = (cloud.provider_data / "config/commands.log").read_text().splitlines()
    assert commands == ["download"]


def test_nonempty_local_save_uploads_and_creates_backup(tmp_path):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")
    save_dir = cloud.resolve_save_path(GAME_ID)
    assert save_dir is not None
    save_dir.mkdir(parents=True)
    (save_dir / "manual.sav").write_bytes(b"local")
    result = cloud.sync_after_exit(GAME_ID)
    assert result.state == "uploaded"
    assert result.backup_path is not None
    assert (Path(result.backup_path) / "manual.sav").read_bytes() == b"local"


def test_successful_upload_wins_over_legendary_newer_wording(tmp_path, monkeypatch):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")
    save_dir = cloud.resolve_save_path(GAME_ID)
    assert save_dir is not None
    save_dir.mkdir(parents=True)
    (save_dir / "manual.sav").write_bytes(b"local")
    monkeypatch.setattr(
        cloud,
        "_legendary_sync",
        lambda *_args: "Local save is newer than Cloud save\nUploading local savegame",
    )

    assert cloud.sync_after_exit(GAME_ID).state == "uploaded"


def test_cloud_sync_defaults_to_automatic_both_directions(tmp_path):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")
    assert cloud.settings() == {
        "enabled": True,
        "autoDownload": True,
        "autoUpload": True,
    }
    assert cloud.set_enabled(False)["enabled"] is False
    assert cloud.sync_before_launch(GAME_ID).state == "disabled"


def test_legendary_process_uses_utc_to_avoid_dst_conflicts(tmp_path):
    cloud = manager(tmp_path, "{AppData}/Hogwarts/Saves/{EpicID}")

    assert cloud._environment()["TZ"] == "UTC"
