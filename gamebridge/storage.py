from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_MEDIA_DRIVE_LETTERS = ("v", "w", "y")


@dataclass(frozen=True)
class StorageRoot:
    path: Path
    source: str
    internal: bool = False


@dataclass(frozen=True)
class StorageHealth:
    path: Path
    mountpoint: Path | None
    source: str
    filesystem: str
    mounted: bool
    writable: bool
    readonly: bool

    @property
    def state(self) -> str:
        if not self.mounted:
            return "unavailable"
        if self.readonly or not self.writable:
            return "readonly"
        return "writable"


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mounted_filesystems(mountinfo: Path = Path("/proc/self/mountinfo")) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = []
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            target = Path(_unescape_mount_field(fields[4])).resolve()
            source = _unescape_mount_field(fields[separator + 2])
        except (ValueError, IndexError, OSError):
            continue
        entries.append((target, source))
    return entries


def storage_health(
    path: str | Path,
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> StorageHealth:
    """Describe the backing mount without writing into a user-owned game directory."""
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return StorageHealth(candidate, None, "", "", False, False, False)
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    matches: list[tuple[Path, str, str, set[str]]] = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mountpoint = Path(_unescape_mount_field(fields[4])).resolve()
            source = _unescape_mount_field(fields[separator + 2])
            filesystem = fields[separator + 1]
            options = set(fields[5].split(",")) | set(fields[separator + 3].split(","))
        except (ValueError, IndexError, OSError):
            continue
        if resolved == mountpoint or resolved.is_relative_to(mountpoint):
            matches.append((mountpoint, source, filesystem, options))
    if not matches:
        return StorageHealth(resolved, None, "", "", False, False, False)
    mountpoint, source, filesystem, options = max(
        matches, key=lambda item: len(item[0].parts)
    )
    readonly = "ro" in options
    writable = not readonly and os.access(resolved, os.W_OK)
    return StorageHealth(
        resolved, mountpoint, source, filesystem, True, writable, readonly
    )


def steam_library_paths(home: Path) -> list[Path]:
    candidates = (
        home / ".local/share/Steam/steamapps/libraryfolders.vdf",
        home / ".steam/root/steamapps/libraryfolders.vdf",
    )
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        paths = []
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            try:
                paths.append(Path(raw.replace("\\\\", "\\")).expanduser().resolve())
            except OSError:
                continue
        return paths
    return []


def storage_roots(home: Path | None = None) -> list[StorageRoot]:
    """Return writable user-facing storage roots without assuming a username or label."""
    resolved_home = (home or Path.home()).expanduser().resolve()
    mounts = mounted_filesystems()
    steam_libraries = steam_library_paths(resolved_home)
    roots = [StorageRoot(resolved_home, _source_for_path(resolved_home, mounts), True)]
    seen_devices: set[tuple[int, Path]] = set()
    try:
        seen_devices.add((resolved_home.stat().st_dev, resolved_home))
    except OSError:
        pass

    for mount, source in mounts:
        user_media = any(
            mount == prefix or mount.is_relative_to(prefix)
            for prefix in (Path("/run/media"), Path("/media"), Path("/mnt"))
        )
        steam_storage = any(
            library == mount or library.is_relative_to(mount) for library in steam_libraries
        )
        if not (user_media or steam_storage):
            continue
        try:
            if mount == Path("/") or not mount.is_dir() or not os.access(mount, os.W_OK):
                continue
            device = mount.stat().st_dev
        except OSError:
            continue
        identity = (device, mount)
        if identity in seen_devices:
            continue
        seen_devices.add(identity)
        roots.append(StorageRoot(mount, source))
    return roots


def ensure_wine_media_drive(
    prefix: Path,
    media_root: Path = Path("/run/media"),
) -> str | None:
    """Expose every standard SteamOS user and mounted disk through one drive."""
    try:
        resolved_media = media_root.resolve(strict=True)
    except OSError:
        return None
    if not resolved_media.is_dir():
        return None

    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    for letter in _MEDIA_DRIVE_LETTERS:
        drive = dosdevices / f"{letter}:"
        if not drive.is_symlink():
            continue
        try:
            if drive.resolve(strict=False) == resolved_media:
                return f"{letter.upper()}:"
        except OSError:
            continue

    for letter in _MEDIA_DRIVE_LETTERS:
        drive = dosdevices / f"{letter}:"
        if os.path.lexists(drive):
            continue
        try:
            drive.symlink_to(resolved_media, target_is_directory=True)
        except OSError:
            continue
        return f"{letter.upper()}:"
    return None


def ensure_wine_storage_drive(
    prefix: Path,
    executable: Path,
    roots: list[StorageRoot] | None = None,
    media_root: Path = Path("/run/media"),
) -> Path | None:
    """Expose an external storage root as G: so Wine reports its real capacity."""
    ensure_wine_media_drive(prefix, media_root)
    external_roots = [
        root.path.resolve()
        for root in (roots if roots is not None else storage_roots())
        if not root.internal
    ]
    if not external_roots:
        return None
    try:
        resolved_executable = executable.resolve()
    except OSError:
        resolved_executable = executable
    matching = [
        root
        for root in external_roots
        if resolved_executable == root or resolved_executable.is_relative_to(root)
    ]
    if matching:
        selected = max(matching, key=lambda path: len(path.parts))
    else:
        try:
            selected = max(
                external_roots,
                key=lambda path: os.statvfs(path).f_bavail * os.statvfs(path).f_frsize,
            )
        except OSError:
            return None

    dosdevices = prefix / "dosdevices"
    game_drive = dosdevices / "g:"
    dosdevices.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(game_drive):
        if not game_drive.is_symlink():
            return None
        try:
            current = game_drive.resolve(strict=False)
        except OSError:
            current = None
        if current == selected and game_drive.is_dir():
            return selected
        # A working G: can intentionally point to the game disk while the
        # launcher lives elsewhere. Only repair a link whose disk is gone.
        if game_drive.is_dir():
            return None
        try:
            game_drive.unlink()
        except OSError:
            return None
    try:
        game_drive.symlink_to(selected, target_is_directory=True)
    except OSError:
        return None
    return selected


def _source_for_path(path: Path, mounts: list[tuple[Path, str]]) -> str:
    matches = [
        (mount, source) for mount, source in mounts if path == mount or path.is_relative_to(mount)
    ]
    return max(matches, key=lambda item: len(item[0].parts))[1] if matches else ""


def approved_install_path(path: Path, home: Path | None = None) -> bool:
    candidate = path.expanduser().resolve()
    if candidate == Path("/"):
        return False
    return any(
        candidate == root.path or candidate.is_relative_to(root.path)
        for root in storage_roots(home)
    )
