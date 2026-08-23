from __future__ import annotations

import csv
import io
import json
import os
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..compatibility import CompatibilityManager
from ..models import GameReference, JsonObject, ProviderCapabilities, RuntimeProfile
from ..process import ProcessError, SafeProcessRunner
from ..provider import GameProvider


class EpicProvider(GameProvider):
    provider_id = "epic"
    display_name = "Epic Games"

    def __init__(self, data_directory: str | Path, runner: SafeProcessRunner | None = None) -> None:
        self.data_directory = Path(data_directory)
        self.runner = runner or SafeProcessRunner()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            account_connection=True,
            owned_library=True,
            direct_download=True,
            update=True,
            repair=True,
            uninstall=True,
            local_launch=True,
            cloud_save=True,
            dlc=True,
        )

    def executable(self) -> Path | None:
        candidates = [
            self.data_directory / "tools" / "legendary",
            shutil.which("legendary"),
            os.path.expanduser("~/.local/bin/legendary"),
            "/usr/local/bin/legendary",
            "/usr/bin/legendary",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
        return None

    def environment(self) -> dict[str, str]:
        config = self.data_directory / "config"
        cache = self.data_directory / "cache"
        config.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        return {
            "HOME": os.fspath(self.data_directory),
            "XDG_CONFIG_HOME": os.fspath(config),
            "XDG_CACHE_HOME": os.fspath(cache),
            "LEGENDARY_CONFIG_PATH": os.fspath(config / "legendary"),
        }

    def is_installed(self, external_game_id: str) -> bool:
        installed_file = self.data_directory / "config" / "legendary" / "installed.json"
        if not installed_file.is_file() or installed_file.stat().st_size > 5_000_000:
            return False
        try:
            payload = json.loads(installed_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        installation = payload.get(external_game_id)
        if not isinstance(installation, dict):
            return False
        install_path = installation.get("install_path")
        executable = installation.get("executable")
        return (
            isinstance(install_path, str)
            and isinstance(executable, str)
            and (Path(install_path) / executable).is_file()
        )

    def retained_installations(self) -> JsonObject:
        installed_file = self.data_directory / "config" / "legendary" / "installed.json"
        try:
            payload = json.loads(installed_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        retained: JsonObject = {}
        for external_id, installation in payload.items():
            if not isinstance(external_id, str) or not isinstance(installation, dict):
                continue
            raw_path = installation.get("install_path")
            executable = installation.get("executable")
            if not isinstance(raw_path, str) or not isinstance(executable, str):
                continue
            try:
                install_path = Path(raw_path).expanduser().resolve()
                candidate = (install_path / executable).resolve()
            except OSError:
                continue
            if install_path not in candidate.parents or not candidate.is_file():
                continue
            retained[external_id] = dict(installation)
        return retained

    def restore_retained_installations(self, payload: JsonObject) -> None:
        if not payload:
            return
        installed_file = self.data_directory / "config" / "legendary" / "installed.json"
        installed_file.parent.mkdir(parents=True, exist_ok=True)
        staged = installed_file.with_suffix(".json.new")
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staged, installed_file)

    async def connection_status(self) -> JsonObject:
        executable = self.executable()
        if executable is None:
            return {
                "state": "unavailable",
                "message": "epic.tool_missing",
                "action": "install_cli",
            }
        cached_status = self.cached_connection_status()
        if cached_status.get("message") == "epic.login_expired":
            return cached_status
        try:
            version_result = await self.runner.run(executable, "--version", check=False)
            result = await self.runner.run(
                executable,
                "status",
                "--offline",
                "--json",
                environment=self.environment(),
                check=False,
            )
            payload = self._parse_json(result.stdout)
            signed_in = result.returncode == 0 and self._is_signed_in(payload)
            return {
                "state": "connected" if signed_in else "disconnected",
                "version": (version_result.stdout or version_result.stderr).strip(),
                "account": self._account_name(payload),
                "message": "epic.connected" if signed_in else "epic.login_required",
            }
        except (OSError, RuntimeError, ValueError) as exc:
            if cached_status.get("state") in {"connected", "disconnected"}:
                return {**cached_status, "offline": True}
            return {"state": "error", "message": str(exc)[:300]}

    def cached_connection_status(self) -> JsonObject:
        executable = self.executable()
        if executable is None:
            return {
                "state": "unavailable",
                "message": "epic.tool_missing",
                "action": "install_cli",
            }
        user_file = self.data_directory / "config" / "legendary" / "user.json"
        if not user_file.is_file():
            return {"state": "disconnected", "message": "epic.login_required"}
        try:
            payload = json.loads(user_file.read_text(encoding="utf-8"))
            display_name = payload.get("displayName")
            refresh_expiry = payload.get("refresh_expires_at")
            if isinstance(refresh_expiry, str):
                expiry = datetime.fromisoformat(refresh_expiry.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=UTC)
                if expiry <= datetime.now(UTC):
                    return {"state": "disconnected", "message": "epic.login_expired"}
            return {
                "state": "connected",
                "account": display_name if isinstance(display_name, str) else None,
                "message": "epic.connected",
                "cached": True,
            }
        except (OSError, ValueError, TypeError):
            return {"state": "error", "message": "epic.login_corrupt"}

    async def library(self) -> Sequence[GameReference]:
        executable = self.executable()
        if executable is None:
            raise RuntimeError("legendary.not_installed")
        try:
            result = await self.runner.run(
                executable, "list", "--json", environment=self.environment()
            )
        except (ProcessError, OSError, RuntimeError) as exc:
            raise RuntimeError("epic.sync_failed") from exc
        payload = self._parse_json(result.stdout)
        if not isinstance(payload, list):
            raise ValueError("epic.library_invalid")
        games: list[GameReference] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            app_name = item.get("app_name") or item.get("appName")
            title = item.get("app_title") or item.get("title")
            if isinstance(app_name, str) and isinstance(title, str):
                games.append(GameReference(self.provider_id, app_name, title))
        return games

    async def authenticate(self, authorization_code: str) -> JsonObject:
        executable = self.executable()
        if executable is None:
            raise RuntimeError("legendary.not_installed")
        code = self._normalize_authorization_code(authorization_code)
        try:
            await self.runner.run(
                executable,
                "auth",
                "--code",
                code,
                environment=self.environment(),
                sensitive_arguments=frozenset({2}),
            )
        except ProcessError as exc:
            message = exc.result.stderr.lower()
            if "authorization_code_not_found" in message or "invalid" in message:
                raise ValueError("error.invalid_auth_code") from None
            raise RuntimeError("error.epic_login_failed") from None
        return await self.connection_status()

    async def logout(self) -> JsonObject:
        executable = self.executable()
        if executable is None:
            raise RuntimeError("legendary.not_installed")
        await self.runner.run(
            executable, "auth", "--delete", environment=self.environment()
        )
        return await self.connection_status()

    async def uninstall(self, external_game_id: str) -> None:
        executable = self.executable()
        if executable is None:
            raise RuntimeError("legendary.not_installed")
        try:
            await self.runner.run(
                executable,
                "-y",
                "uninstall",
                external_game_id,
                "--skip-uninstaller",
                environment=self.environment(),
            )
        except ProcessError as exc:
            raise RuntimeError("error.epic_uninstall_failed") from exc

    async def check_updates(self) -> dict[str, JsonObject]:
        """Refresh Legendary's assets and return installed titles with newer builds."""
        executable = self.executable()
        if executable is None:
            raise RuntimeError("legendary.not_installed")
        try:
            result = await self.runner.run(
                executable,
                "list-installed",
                "--check-updates",
                "--csv",
                environment=self.environment(),
            )
        except ProcessError as exc:
            raise RuntimeError("epic.update_check_failed") from exc
        updates: dict[str, JsonObject] = {}
        for row in csv.DictReader(io.StringIO(result.stdout)):
            app_name = row.get("App name")
            installed = row.get("Installed version")
            available = row.get("Available version")
            raw_update = (row.get("Update available") or "").strip().casefold()
            if not app_name or not installed or not available:
                continue
            update_available = raw_update in {"true", "1", "yes"} or installed != available
            updates[app_name] = {
                "installed_version": installed,
                "latest_version": available,
                "update_available": update_available,
            }
        cache = self.data_directory / "cache" / "updates.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(updates, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache)
        return updates

    def cached_update(self, external_game_id: str) -> JsonObject:
        cache = self.data_directory / "cache" / "updates.json"
        if not cache.is_file() or cache.stat().st_size > 5_000_000:
            return {"update_available": False, "latest_version": None}
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            update = payload.get(external_game_id)
        except (OSError, ValueError, AttributeError):
            update = None
        return update if isinstance(update, dict) else {
            "update_available": False,
            "latest_version": None,
        }

    def mark_updated(self, external_game_id: str) -> None:
        current = self.cached_update(external_game_id)
        latest = current.get("latest_version")
        payload: dict[str, object] = {}
        cache = self.data_directory / "cache" / "updates.json"
        try:
            decoded = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
        except (OSError, ValueError):
            pass
        payload[external_game_id] = {
            "installed_version": latest,
            "latest_version": latest,
            "update_available": False,
        }
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache)

    def remove_partial_install(self, external_game_id: str, install_root: str | Path) -> None:
        metadata_file = (
            self.data_directory / "config" / "legendary" / "metadata"
            / f"{external_game_id}.json"
        )
        folder_name: str | None = None
        try:
            payload = json.loads(metadata_file.read_text(encoding="utf-8"))
            attributes = payload.get("metadata", {}).get("customAttributes", {})
            folder = attributes.get("FolderName", {})
            if isinstance(folder, dict) and isinstance(folder.get("value"), str):
                folder_name = folder["value"]
        except (OSError, ValueError, AttributeError):
            pass
        root = Path(install_root).expanduser().resolve()
        if folder_name:
            candidate = (root / folder_name).resolve()
            if candidate.parent == root and candidate.is_dir():
                shutil.rmtree(candidate)
        temporary = self.data_directory / "config" / "legendary" / "tmp"
        for suffix in (".resume", ".json"):
            candidate = temporary / f"{external_game_id}{suffix}"
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    async def install_sizes(self, external_game_id: str) -> dict[str, int]:
        executable = self.executable()
        if executable is None:
            raise RuntimeError("legendary.not_installed")
        result = await self.runner.run(
            executable, "info", external_game_id, "--json", environment=self.environment()
        )
        payload = self._parse_json(result.stdout)
        manifest = payload.get("manifest") if isinstance(payload, dict) else None
        if not isinstance(manifest, dict):
            raise RuntimeError("error.epic_manifest_missing")

        def base_tag_size(key: str, fallback: str) -> int:
            tagged = manifest.get(key)
            if isinstance(tagged, list):
                for item in tagged:
                    if isinstance(item, dict) and item.get("tag") == "":
                        value = item.get("size")
                        if isinstance(value, int):
                            return value
            value = manifest.get(fallback)
            return value if isinstance(value, int) else 0

        return {
            "required_bytes": base_tag_size("tag_disk_size", "disk_size"),
            "download_bytes": base_tag_size("tag_download_size", "download_size"),
        }

    async def resolve_launch(self, game: GameReference) -> RuntimeProfile:
        installed_file = self.data_directory / "config" / "legendary" / "installed.json"
        try:
            installations = json.loads(installed_file.read_text(encoding="utf-8"))
            installation = installations[game.external_game_id]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("epic.game_not_installed") from exc
        install_path = Path(  # noqa: ASYNC240 -- small local metadata lookup
            str(installation.get("install_path", ""))
        ).expanduser().resolve()
        executable = (install_path / str(installation.get("executable", ""))).resolve()
        if install_path not in executable.parents or not executable.is_file():
            raise RuntimeError("epic.executable_missing")
        compatibility = CompatibilityManager(self.data_directory.parents[1] / "compatibility")
        runtime_name, runtime_path = compatibility.selected_proton(
            self.provider_id, game.external_game_id
        )
        return RuntimeProfile(
            game_id=f"epic:{game.external_game_id}",
            prefix_path=os.fspath(compatibility.prefix(self.provider_id, game.external_game_id)),
            executable=os.fspath(executable),
            runtime_version=os.fspath(runtime_path),
            game_id_umu=compatibility.umu_id(game.external_game_id, game.title),
            store="egs",
            environment={"UMU_LOG": "1"},
        )

    @staticmethod
    def _parse_json(text: str) -> Any:
        text = text.strip()
        if not text:
            return {}
        return json.loads(text)

    @staticmethod
    def _is_signed_in(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        account = payload.get("account")
        return bool(
            payload.get("logged_in")
            or payload.get("loggedIn")
            or (isinstance(account, dict) and account.get("id"))
            or (isinstance(account, str) and account != "<not logged in>")
        )

    @staticmethod
    def _account_name(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        account = payload.get("account")
        if isinstance(account, dict):
            value = account.get("displayName") or account.get("display_name")
            return value if isinstance(value, str) else None
        if isinstance(account, str) and account != "<not logged in>":
            return account
        return None

    @staticmethod
    def _normalize_authorization_code(value: str) -> str:
        value = value.strip()
        if value.startswith("{"):
            try:
                payload = json.loads(value)
                value = str(payload.get("authorizationCode", "")).strip()
            except json.JSONDecodeError as exc:
                raise ValueError("error.invalid_auth_json") from exc
        if not value or len(value) > 4096 or any(character.isspace() for character in value):
            raise ValueError("error.invalid_auth_format")
        return value
