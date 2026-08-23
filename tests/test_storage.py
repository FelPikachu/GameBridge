from pathlib import Path

from gamebridge import storage
from gamebridge.storage import StorageRoot


def test_mountinfo_decodes_spaces(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "42 31 8:1 / /run/media/alice/My\\040Games rw,nosuid - ext4 /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    assert storage.mounted_filesystems(mountinfo) == [
        (Path("/run/media/alice/My Games"), "/dev/sdb1")
    ]


def test_storage_roots_are_dynamic_and_not_tied_to_deck_username(tmp_path, monkeypatch):
    home = tmp_path / "home" / "alice"
    external = tmp_path / "run" / "media" / "alice" / "SD Card"
    home.mkdir(parents=True)
    external.mkdir(parents=True)
    monkeypatch.setattr(
        storage,
        "mounted_filesystems",
        lambda: [(tmp_path, "/dev/internal"), (external, "/dev/mmcblk0p1")],
    )
    monkeypatch.setattr(storage, "_steam_library_paths", lambda _home: [external / "SteamLibrary"])

    roots = storage.storage_roots(home)

    assert roots[0].path == home.resolve()
    assert roots[0].internal is True
    assert any(root.path == external.resolve() for root in roots)
    assert all("/run/media/deck" not in str(root.path) for root in roots)


def test_storage_health_reports_readonly_mount_without_writing(tmp_path):
    mountpoint = tmp_path / "Game Drive"
    game = mountpoint / "Games" / "Example"
    game.mkdir(parents=True)
    mountinfo = tmp_path / "mountinfo"
    escaped = str(mountpoint).replace(" ", "\\040")
    mountinfo.write_text(
        f"42 31 8:1 / {escaped} ro,nosuid - ntfs3 /dev/sdb1 ro\n",
        encoding="utf-8",
    )

    health = storage.storage_health(game, mountinfo)

    assert health.state == "readonly"
    assert health.mountpoint == mountpoint
    assert health.filesystem == "ntfs3"


def test_wine_storage_drive_maps_largest_external_root_before_prefix_exists(
    tmp_path, monkeypatch
):
    small = tmp_path / "small"
    game = tmp_path / "Game"
    small.mkdir()
    game.mkdir()
    sizes = {small.resolve(): 10, game.resolve(): 100}

    class StatVfs:
        f_frsize = 1

        def __init__(self, available):
            self.f_bavail = available

    monkeypatch.setattr(
        storage.os,
        "statvfs",
        lambda path: StatVfs(sizes[Path(path).resolve()]),
    )

    selected = storage.ensure_wine_storage_drive(
        tmp_path / "prefix",
        tmp_path / "installer.exe",
        [StorageRoot(small, "small"), StorageRoot(game, "game")],
    )

    assert selected == game.resolve()
    assert (tmp_path / "prefix/dosdevices/g:").resolve() == game.resolve()


def test_storage_health_reports_missing_path_as_unavailable(tmp_path):
    health = storage.storage_health(tmp_path / "missing", tmp_path / "missing-mountinfo")
    assert health.state == "unavailable"
