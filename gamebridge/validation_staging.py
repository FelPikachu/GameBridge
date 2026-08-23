from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


class StagingError(RuntimeError):
    """Raised when a validation directory cannot be hidden or restored safely."""


@dataclass(frozen=True)
class StagedGame:
    game_id: str
    original_path: str
    staged_path: str
    filesystem_device: int
    key_executable: str
    size_bytes: int
    stage: str
    staged_at: str


def _directory_size(path: Path) -> int:
    total = 0
    for root, _directories, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _write_manifest(path: Path, entry: StagedGame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(asdict(entry), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_manifest(path: Path) -> StagedGame:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return StagedGame(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise StagingError("validation_staging.manifest_invalid") from error


def stage_game_directory(
    *,
    game_id: str,
    game_path: Path,
    key_executable: Path,
    manifest_path: Path,
    stage: str,
    is_busy: Callable[[Path], bool],
) -> StagedGame:
    original = game_path.expanduser().resolve(strict=True)
    executable = key_executable.expanduser().resolve(strict=True)
    if executable.parent != original and not executable.is_relative_to(original):
        raise StagingError("validation_staging.executable_outside_game")
    if is_busy(original):
        raise StagingError("validation_staging.game_busy")

    staged = original.with_name(f"{original.name}.gamebridge-test-hidden")
    if staged.exists() or staged.is_symlink():
        raise StagingError("validation_staging.staged_path_exists")
    if manifest_path.exists():
        raise StagingError("validation_staging.manifest_exists")
    if original.parent.stat().st_dev != original.stat().st_dev:
        raise StagingError("validation_staging.cross_filesystem")

    entry = StagedGame(
        game_id=game_id,
        original_path=os.fspath(original),
        staged_path=os.fspath(staged),
        filesystem_device=original.stat().st_dev,
        key_executable=os.fspath(executable.relative_to(original)),
        size_bytes=_directory_size(original),
        stage=stage,
        staged_at=datetime.now(UTC).isoformat(),
    )
    _write_manifest(manifest_path, entry)
    try:
        original.rename(staged)
    except OSError:
        manifest_path.unlink(missing_ok=True)
        raise
    if original.exists() or not staged.is_dir():
        raise StagingError("validation_staging.stage_verification_failed")
    return entry


def restore_game_directory(
    manifest_path: Path,
    *,
    is_busy: Callable[[Path], bool],
) -> StagedGame:
    entry = _load_manifest(manifest_path)
    original = Path(entry.original_path)
    staged = Path(entry.staged_path)
    if original.exists() or original.is_symlink():
        raise StagingError("validation_staging.original_path_exists")
    if not staged.is_dir():
        raise StagingError("validation_staging.staged_path_missing")
    if staged.stat().st_dev != entry.filesystem_device:
        raise StagingError("validation_staging.filesystem_changed")
    executable = staged / entry.key_executable
    if not executable.is_file():
        raise StagingError("validation_staging.executable_missing")
    if is_busy(staged):
        raise StagingError("validation_staging.game_busy")

    staged.rename(original)
    restored_executable = original / entry.key_executable
    if staged.exists() or not restored_executable.is_file():
        raise StagingError("validation_staging.restore_verification_failed")
    if _directory_size(original) != entry.size_bytes:
        raise StagingError("validation_staging.size_changed")
    manifest_path.unlink()
    return entry
