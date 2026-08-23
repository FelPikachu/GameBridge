from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_MANIFEST = ".gamebridge-cloud-manifest.json"
_MAX_METADATA_SIZE = 20 * 1024 * 1024
_MAX_SAVE_FILES = 20_000
_MAX_BACKUPS = 5


@dataclass(frozen=True, slots=True)
class CloudSyncResult:
    supported: bool
    state: str
    direction: Literal["download", "upload", "status"]
    local_path: str | None = None
    local_files: int = 0
    local_bytes: int = 0
    local_timestamp: int = 0
    cloud_timestamp: int = 0
    backup_path: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "state": self.state,
            "direction": self.direction,
            # The resolved path may contain Epic's account id. It is useful to
            # the local adapter but must not cross the RPC boundary.
            "localPath": None,
            "localFiles": self.local_files,
            "localBytes": self.local_bytes,
            "localTimestamp": self.local_timestamp,
            "cloudTimestamp": self.cloud_timestamp,
            "backupPath": self.backup_path,
            "message": self.message,
        }


class EpicCloudSaveManager:
    """Safe Legendary cloud-save adapter shared by RPC and the launcher.

    Authentication remains entirely in Legendary's provider directory. This
    adapter reads only the account id needed to resolve Epic's ``{EpicID}``
    path token and never returns or logs it.
    """

    def __init__(self, root_data: str | Path) -> None:
        self.root_data = Path(root_data)
        self.provider_data = self.root_data / "providers" / "epic"
        self.legendary = self.provider_data / "tools" / "legendary"
        self.legendary_config = self.provider_data / "config" / "legendary"
        self.prefix_root = self.root_data / "compatibility" / "prefixes" / "epic"
        self.state_root = self.root_data / "cloud-saves" / "epic"

    def settings(self) -> dict[str, bool]:
        path = self.root_data / "cloud-saves" / "settings.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        return {
            "enabled": payload.get("enabled", True) is not False,
            "autoDownload": payload.get("autoDownload", True) is not False,
            "autoUpload": payload.get("autoUpload", True) is not False,
        }

    def set_enabled(self, enabled: bool) -> dict[str, bool]:
        settings = self.settings()
        settings["enabled"] = bool(enabled)
        self._atomic_json(self.root_data / "cloud-saves" / "settings.json", settings)
        return settings

    def status(self, game_id: str) -> CloudSyncResult:
        local_path = self.resolve_save_path(game_id)
        cloud_timestamp = self._cloud_timestamp(game_id) if local_path else 0
        local = self._snapshot(local_path) if local_path else self._empty_snapshot()
        persisted = self._read_state(game_id) if local_path else {}
        return CloudSyncResult(
            supported=local_path is not None,
            state=str(persisted.get("state") or "ready") if local_path else "unsupported",
            direction="status",
            local_path=os.fspath(local_path) if local_path else None,
            local_files=local["file_count"],
            local_bytes=local["total_bytes"],
            local_timestamp=local["timestamp"],
            cloud_timestamp=cloud_timestamp,
            message=(
                str(persisted["message"])
                if isinstance(persisted.get("message"), str)
                else None
            ),
        )

    def sync_before_launch(self, game_id: str) -> CloudSyncResult:
        settings = self.settings()
        if not settings["enabled"] or not settings["autoDownload"]:
            return CloudSyncResult(False, "disabled", "download")
        return self.sync(game_id, "download")

    def sync_after_exit(self, game_id: str) -> CloudSyncResult:
        settings = self.settings()
        if not settings["enabled"] or not settings["autoUpload"]:
            return CloudSyncResult(False, "disabled", "upload")
        return self.sync(game_id, "upload")

    def sync(
        self, game_id: str, direction: Literal["download", "upload"]
    ) -> CloudSyncResult:
        if not _SAFE_ID.fullmatch(game_id):
            raise ValueError("cloud_save.invalid_game_id")
        local_path = self.resolve_save_path(game_id)
        if local_path is None:
            return CloudSyncResult(False, "unsupported", direction)
        lock_path = self.state_root / game_id / "sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return CloudSyncResult(
                    True, "busy", direction, local_path=os.fspath(local_path)
                )
            try:
                if direction == "download":
                    return self._download(game_id, local_path)
                return self._upload(game_id, local_path)
            except (OSError, RuntimeError) as exc:
                message = str(exc).split(":", 1)[0]
                self._write_state(
                    game_id,
                    self._snapshot(local_path),
                    "failed",
                    self._cloud_timestamp(game_id),
                    message=message,
                )
                raise

    def resolve_save_path(self, game_id: str) -> Path | None:
        if not _SAFE_ID.fullmatch(game_id):
            return None
        metadata = self._metadata(game_id)
        template = self._cloud_template(metadata)
        installation = self._installation(game_id)
        install_path_value = installation.get("install_path")
        if not template or not isinstance(install_path_value, str):
            return None
        try:
            install_path = Path(install_path_value).expanduser().resolve()
        except OSError:
            return None
        prefix = (self.prefix_root / game_id).resolve()
        user = prefix / "drive_c" / "users" / "steamuser"
        account_id = self._account_id()
        replacements = {
            "{AppData}": user / "AppData" / "Local",
            "{LocalAppData}": user / "AppData" / "Local",
            "{RoamingAppData}": user / "AppData" / "Roaming",
            "{UserDir}": user,
            "{UserProfile}": user,
            "{Documents}": user / "Documents",
            "{UserDocuments}": user / "Documents",
            "{SavedGames}": user / "Saved Games",
            "{InstallDir}": install_path,
        }
        value = template.replace("\\", "/")
        for token, replacement in replacements.items():
            value = value.replace(token, os.fspath(replacement))
        if "{EpicID}" in value:
            if not account_id:
                return None
            value = value.replace("{EpicID}", account_id)
        if "{" in value or "}" in value or "\x00" in value:
            return None
        try:
            candidate = Path(value).expanduser().resolve()
        except OSError:
            return None
        if not self._contained(candidate, prefix) and not self._contained(
            candidate, install_path
        ):
            return None
        return candidate

    def _download(self, game_id: str, local_path: Path) -> CloudSyncResult:
        backup = self._backup(game_id, local_path)
        local_path.mkdir(parents=True, exist_ok=True)
        result = self._legendary_sync(game_id, local_path, "download")
        snapshot = self._snapshot(local_path)
        cloud_timestamp = self._cloud_timestamp(game_id)
        state = "downloaded" if "Downloading remote savegame" in result else "unchanged"
        self._write_state(game_id, snapshot, state, cloud_timestamp)
        return CloudSyncResult(
            True,
            state,
            "download",
            os.fspath(local_path),
            snapshot["file_count"],
            snapshot["total_bytes"],
            snapshot["timestamp"],
            cloud_timestamp,
            os.fspath(backup) if backup else None,
        )

    def _upload(self, game_id: str, local_path: Path) -> CloudSyncResult:
        snapshot = self._snapshot(local_path)
        if snapshot["file_count"] == 0:
            result = CloudSyncResult(
                True,
                "blocked_empty",
                "upload",
                os.fspath(local_path),
                message="cloud_save.empty_upload_blocked",
            )
            self._write_state(
                game_id,
                snapshot,
                result.state,
                self._cloud_timestamp(game_id),
                message=result.message,
            )
            return result
        previous = self._read_state(game_id).get("files")
        if isinstance(previous, dict) and set(previous) - set(snapshot["files"]):
            backup = self._backup(game_id, local_path)
            result = CloudSyncResult(
                True,
                "conflict_missing_files",
                "upload",
                os.fspath(local_path),
                snapshot["file_count"],
                snapshot["total_bytes"],
                snapshot["timestamp"],
                self._cloud_timestamp(game_id),
                os.fspath(backup) if backup else None,
                "cloud_save.local_files_missing",
            )
            self._write_state(
                game_id,
                snapshot,
                result.state,
                result.cloud_timestamp,
                message=result.message,
            )
            return result
        backup = self._backup(game_id, local_path)
        result = self._legendary_sync(game_id, local_path, "upload")
        cloud_timestamp = self._cloud_timestamp(game_id)
        if "Uploading local savegame" in result:
            state = "uploaded"
        elif "Cloud save" in result and "newer" in result:
            state = "conflict_cloud_newer"
        else:
            state = "unchanged"
        self._write_state(game_id, snapshot, state, cloud_timestamp)
        return CloudSyncResult(
            True,
            state,
            "upload",
            os.fspath(local_path),
            snapshot["file_count"],
            snapshot["total_bytes"],
            snapshot["timestamp"],
            cloud_timestamp,
            os.fspath(backup) if backup else None,
        )

    def _legendary_sync(
        self, game_id: str, local_path: Path, direction: Literal["download", "upload"]
    ) -> str:
        if not self.legendary.is_file():
            raise RuntimeError("legendary.not_installed")
        command = [
            os.fspath(self.legendary),
            "-y",
            "sync-saves",
            game_id,
            "--save-path",
            os.fspath(local_path),
            "--disable-filters",
            "--skip-upload" if direction == "download" else "--skip-download",
        ]
        try:
            result = subprocess.run(
                command,
                env=self._environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("cloud_save.timeout") from exc
        if result.returncode:
            raise RuntimeError(f"cloud_save.sync_failed:{result.returncode}")
        # Legendary writes stable state messages to stderr. Return them only to
        # the local parser; callers and logs receive structured state instead.
        return f"{result.stdout}\n{result.stderr}"

    def _cloud_timestamp(self, game_id: str) -> int:
        if not self.legendary.is_file():
            return 0
        try:
            result = subprocess.run(
                [os.fspath(self.legendary), "list-saves", game_id],
                env=self._environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0
        stamps: list[int] = []
        for match in re.finditer(
            r"(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})\.manifest",
            result.stdout,
        ):
            try:
                values = [int(value) for value in match.groups()]
                stamps.append(int(datetime(*values, tzinfo=UTC).timestamp()))
            except ValueError:
                continue
        return max(stamps, default=0)

    def _metadata(self, game_id: str) -> dict[str, Any]:
        path = self.legendary_config / "metadata" / f"{game_id}.json"
        try:
            if path.stat().st_size > _MAX_METADATA_SIZE:
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _cloud_template(cls, payload: object) -> str | None:
        if not isinstance(payload, dict):
            return None
        candidates = [payload]
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata)
            attributes = metadata.get("customAttributes")
            if isinstance(attributes, dict):
                candidates.append(attributes)
        for candidate in candidates:
            for key in ("cloud_save_folder", "CloudSaveFolder"):
                value = candidate.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, dict) and isinstance(value.get("value"), str):
                    return value["value"]
        return None

    def _installation(self, game_id: str) -> dict[str, Any]:
        path = self.legendary_config / "installed.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get(game_id)
        except (OSError, ValueError, AttributeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _account_id(self) -> str | None:
        try:
            payload = json.loads(
                (self.legendary_config / "user.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        value = payload.get("account_id")
        return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None

    def _environment(self) -> dict[str, str]:
        return {
            "HOME": os.fspath(self.provider_data),
            "XDG_CONFIG_HOME": os.fspath(self.provider_data / "config"),
            "XDG_CACHE_HOME": os.fspath(self.provider_data / "cache"),
            "LEGENDARY_CONFIG_PATH": os.fspath(self.legendary_config),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            # Legendary compares naive cloud datetimes with local file mtimes.
            # UTC prevents freshly downloaded saves looking older under DST.
            "TZ": "UTC",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }

    def _snapshot(self, directory: Path) -> dict[str, Any]:
        files: dict[str, dict[str, int]] = {}
        total = 0
        newest = 0
        if directory.is_dir():
            for entry in directory.rglob("*"):
                if not entry.is_file() or entry.name == _MANIFEST:
                    continue
                if len(files) >= _MAX_SAVE_FILES:
                    raise RuntimeError("cloud_save.too_many_files")
                try:
                    stat = entry.stat()
                    relative = os.fspath(entry.relative_to(directory))
                except (OSError, ValueError):
                    continue
                files[relative] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                total += stat.st_size
                newest = max(newest, int(stat.st_mtime))
        return {
            "files": files,
            "file_count": len(files),
            "total_bytes": total,
            "timestamp": newest,
        }

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        return {"files": {}, "file_count": 0, "total_bytes": 0, "timestamp": 0}

    def _backup(self, game_id: str, source: Path) -> Path | None:
        snapshot = self._snapshot(source)
        if not snapshot["files"]:
            return None
        root = self.state_root / game_id / "backups"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = root / stamp
        try:
            destination.mkdir(parents=True, exist_ok=False)
            for relative in snapshot["files"]:
                source_file = source / relative
                destination_file = destination / relative
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination_file)
            backups = sorted(path for path in root.iterdir() if path.is_dir())
            for old in backups[:-_MAX_BACKUPS]:
                shutil.rmtree(old)
        except OSError as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError("cloud_save.backup_failed") from exc
        return destination

    def _write_state(
        self,
        game_id: str,
        snapshot: dict[str, Any],
        state: str,
        cloud_timestamp: int,
        *,
        message: str | None = None,
    ) -> None:
        self._atomic_json(
            self.state_root / game_id / "state.json",
            {
                **snapshot,
                "state": state,
                "cloud_timestamp": cloud_timestamp,
                "updated_at": int(time.time()),
                "message": message,
            },
        )

    def _read_state(self, game_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(
                (self.state_root / game_id / "state.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _contained(candidate: Path, root: Path) -> bool:
        return candidate == root or root in candidate.parents
