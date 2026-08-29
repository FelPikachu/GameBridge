from __future__ import annotations

import asyncio
import base64
import json
import os
import pwd
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .cloud_saves import EpicCloudSaveManager
from .compatibility import CompatibilityManager
from .database import Database
from .install import EpicInstallManager
from .jobs import InstallJobStore
from .launch_options import (
    preset_launch_options,
    repair_launch_options,
    shortcut_profile_launch_options,
)
from .models import GameReference, JobState, RuntimeProfile
from .official_artwork import OfficialLauncherArtworkResolver
from .play_history import (
    export_history,
    locate_localconfig,
    read_app_history,
    read_history_store,
    stage_history_import,
)
from .provider import ProviderRegistry
from .providers import HOYOPLAY_GLOBAL, MIHOYO_CN, EpicProvider, HoYoPlayProvider
from .runtime import UmuRuntime
from .steam_artwork import SteamArtworkResolver
from .steam_browser import SteamBrowserAuthorization
from .steamgriddb import SteamGridDbResolver
from .storage import approved_install_path, ensure_wine_storage_drive, storage_roots
from .tooling import ToolInstaller


class GameBridgeApplication:
    def __init__(self, data_directory: str | Path) -> None:
        self.data_directory = Path(data_directory)
        self.database = Database(self.data_directory / "gamebridge.db")
        self.jobs = InstallJobStore(self.database)
        self.providers = ProviderRegistry()
        self.tool_installer = ToolInstaller()
        self.compatibility = CompatibilityManager(
            self.data_directory / "compatibility", self.tool_installer
        )
        self.cloud_saves = EpicCloudSaveManager(self.data_directory)
        self.epic_installs: EpicInstallManager | None = None
        self.simulated_updates: dict[str, asyncio.Task[None]] = {}
        self.provider_installer_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cleanup_lock = asyncio.Lock()
        self.steam_artwork = SteamArtworkResolver(
            self.data_directory / "cache" / "steam-artwork.json"
        )
        self.official_artwork = OfficialLauncherArtworkResolver(
            self.data_directory / "cache" / "official-artwork.json"
        )
        self.steamgriddb = SteamGridDbResolver(
            self.data_directory / "secrets" / "steamgriddb.key",
            self.data_directory / "cache" / "steamgriddb-artwork.json",
        )
        self._steamgriddb_last_validation = False

    def start(self) -> None:
        self.database.initialize()
        epic = EpicProvider(self.data_directory / "providers" / "epic")
        self.providers.register(epic)
        compatibility_root = self.data_directory / "compatibility"
        self.providers.register(
            HoYoPlayProvider(
                self.data_directory / "providers" / "mihoyo-cn",
                compatibility_root,
                MIHOYO_CN,
            )
        )
        self.providers.register(
            HoYoPlayProvider(
                self.data_directory / "providers" / "hoyoplay-global",
                compatibility_root,
                HOYOPLAY_GLOBAL,
            )
        )
        self.epic_installs = EpicInstallManager(epic, self.jobs)
        # Never let a UI-only development simulation become a real update after
        # Decky reloads.
        for job in self.jobs.active():
            if job.payload.get("simulated"):
                try:
                    self.jobs.transition(job.id, JobState.CANCELLED)
                except ValueError:
                    pass
        self.epic_installs.recover_interrupted_jobs()
        with self.database.connect() as db:
            for provider in self.providers.summaries():
                db.execute(
                    "INSERT INTO providers(id, display_name) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name",
                    (provider["id"], provider["name"]),
                )

    @staticmethod
    def _systemd_user_environment() -> dict[str, str]:
        """Read the current graphical session values from the user manager.

        Decky can survive a Steam or desktop-session restart, so its plugin
        children may retain an obsolete or incomplete environment.  The user
        manager is the stable same-user source for the newly generated Xauthority
        path.  Only the small graphical-session allowlist is accepted.
        """
        allowed = {
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_DATA_DIRS",
            "GAMESCOPE_WAYLAND_DISPLAY",
        }
        runtime_directory = Path(f"/run/user/{os.getuid()}")
        manager_environment = {
            "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": os.fspath(runtime_directory),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_directory / 'bus'}",
        }
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                check=True,
                capture_output=True,
                env=manager_environment,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        values: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            key, separator, value = raw_line.partition("=")
            if separator and key in allowed and value and "\x00" not in value:
                values[key] = value
        return values

    @staticmethod
    def _umu_session_environment(extra: dict[str, str]) -> dict[str, str]:
        user_id = os.getuid()
        runtime_directory = Path(f"/run/user/{user_id}")
        gamescope_values: dict[str, str] = {}
        gamescope_file = runtime_directory / "gamescope-environment"
        allowed_gamescope_keys = {
            "DISPLAY",
            "GAMESCOPE_WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_DATA_DIRS",
        }
        try:
            for raw_line in gamescope_file.read_text(encoding="utf-8").splitlines():
                key, separator, value = raw_line.partition("=")
                if separator and key in allowed_gamescope_keys and value:
                    gamescope_values[key] = value
        except OSError:
            pass
        environment = {
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", os.fspath(Path.home())),
            "USER": os.environ.get("USER", "deck"),
            "LOGNAME": os.environ.get("USER", "deck"),
        }
        for key in (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_DATA_DIRS",
        ):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        for key, value in GameBridgeApplication._systemd_user_environment().items():
            environment.setdefault(key, value)
        for key, value in gamescope_values.items():
            environment.setdefault(key, value)
        environment.setdefault("XDG_RUNTIME_DIR", os.fspath(runtime_directory))
        environment.setdefault(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path={runtime_directory / 'bus'}",
        )
        environment.setdefault("DISPLAY", ":0")
        if "WAYLAND_DISPLAY" not in environment and gamescope_values.get(
            "GAMESCOPE_WAYLAND_DISPLAY"
        ):
            environment["WAYLAND_DISPLAY"] = gamescope_values[
                "GAMESCOPE_WAYLAND_DISPLAY"
            ]
        environment.update(extra)
        return environment

    @staticmethod
    def _authorize_x11_local_user(environment: dict[str, str]) -> bool:
        """Allow this same Unix user to reach Xwayland from the UMU container.

        Xauthority files under ``/run/user`` are not always visible inside the
        pressure-vessel namespace.  A narrowly scoped X11 local-user ACL avoids
        copying or persisting the session cookie and survives Wine child-process
        startup.  Failure stays non-fatal so native Wayland sessions continue to
        work normally.
        """
        display = environment.get("DISPLAY")
        if not display:
            return False
        xhost = shutil.which("xhost", path="/usr/bin:/bin")
        if xhost is None:
            return False
        user = pwd.getpwuid(os.getuid()).pw_name
        command_environment = {
            "DISPLAY": display,
            "PATH": "/usr/bin:/bin",
            "HOME": environment.get("HOME", os.fspath(Path.home())),
        }
        if authority := environment.get("XAUTHORITY"):
            command_environment["XAUTHORITY"] = authority
        try:
            subprocess.run(
                [xhost, f"+SI:localuser:{user}"],
                check=True,
                env=command_environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def set_runtime_language(
        self, provider_id: str, game_id: str, language: str
    ) -> None:
        valid_language = re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", language)
        if provider_id != "epic" or not valid_language:
            raise ValueError("runtime.invalid_language")
        profile_key = f"{provider_id}:{game_id}"
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT profile_json FROM runtime_profiles WHERE game_id=?", (profile_key,)
            ).fetchone()
            profile = self.database.decode(row[0]) if row else {}
            profile["language"] = language
            connection.execute(
                "INSERT INTO runtime_profiles(game_id, profile_json) VALUES (?, ?) "
                "ON CONFLICT(game_id) DO UPDATE SET profile_json=excluded.profile_json, "
                "revision=runtime_profiles.revision+1, updated_at=CURRENT_TIMESTAMP",
                (profile_key, self.database.encode(profile)),
            )

    def cloud_save_settings(self) -> dict[str, bool]:
        return self.cloud_saves.settings()

    def set_cloud_save_enabled(self, enabled: bool) -> dict[str, bool]:
        return self.cloud_saves.set_enabled(enabled)

    async def cloud_save_status(self, provider_id: str, game_id: str) -> dict[str, object]:
        if provider_id != "epic":
            return {"supported": False, "state": "unsupported", "direction": "status"}
        return (await asyncio.to_thread(self.cloud_saves.status, game_id)).to_dict()

    async def sync_cloud_save(
        self, provider_id: str, game_id: str, direction: str
    ) -> dict[str, object]:
        if provider_id != "epic" or direction not in {"download", "upload"}:
            raise ValueError("cloud_save.invalid_request")
        return (
            await asyncio.to_thread(self.cloud_saves.sync, game_id, direction)
        ).to_dict()

    @staticmethod
    def repair_shortcut_launch_options(raw: str, provider_id: str, game_id: str) -> str:
        return repair_launch_options(raw, provider_id, game_id)

    @staticmethod
    def shortcut_launch_preset(preset: str, provider_id: str, game_id: str) -> str:
        return preset_launch_options(preset, provider_id, game_id)

    @staticmethod
    def shortcut_profile_launch_preset(preset: str, base: str, mode: str) -> str:
        return shortcut_profile_launch_options(preset, base, mode)

    @staticmethod
    def launch_modifier_availability(plugin_directory: str | Path) -> dict[str, bool]:
        root = Path(plugin_directory)
        try:
            names = {item.name.casefold() for item in root.iterdir() if item.is_dir()}
        except OSError:
            names = set()
        return {
            "lsfg": "decky-lsfg-vk" in names,
            "framegen": "decky-framegen" in names,
        }

    async def dashboard(self) -> dict[str, object]:
        for summary in self.providers.summaries():
            capabilities = summary.get("capabilities")
            if isinstance(capabilities, dict) and capabilities.get("public_catalog"):
                # Dashboard refresh must remain a local/fast operation. Artwork
                # downloads belong to explicit library sync and must never hold
                # the status RPC open until Decky times it out.
                await self.sync_provider_library(
                    str(summary["id"]), resolve_artwork=False
                )
        providers = self.providers.summaries()
        with self.database.connect() as db:
            provider_game_counts = {
                str(row["provider_id"]): int(row["game_count"])
                for row in db.execute(
                    "SELECT provider_id, COUNT(*) AS game_count "
                    "FROM game_releases GROUP BY provider_id"
                ).fetchall()
            }
        provider_statuses = []
        for provider in providers:
            instance = self.providers.get(str(provider["id"]))
            status = (
                instance.cached_connection_status()
                if isinstance(instance, EpicProvider)
                else await instance.connection_status()
            )
            provider_statuses.append(
                {
                    **provider,
                    "gameCount": provider_game_counts.get(str(provider["id"]), 0),
                    "status": status,
                }
            )
        with self.database.connect() as db:
            game_count = db.execute("SELECT COUNT(*) FROM catalog_games").fetchone()[0]
        return {
            "version": __version__,
            "providerCount": len(providers),
            "gameCount": game_count,
            "activeJobCount": len(self.jobs.active()),
            "providers": provider_statuses,
            "runtime": self.compatibility.status(),
            "status": "ready",
        }

    async def prepare_compatibility(self) -> dict[str, object]:
        return await asyncio.to_thread(self.compatibility.prepare_base)

    async def prepare_default_compatibility(self) -> dict[str, object]:
        return await asyncio.to_thread(self.compatibility.prepare)

    def tool_download_progress(self) -> dict[str, object]:
        return self.tool_installer.progress_status()

    async def prepare_hoyoplay_game_runtime(self, game_id: str) -> dict[str, str]:
        supported = {
            "hk4e_cn",
            "nap_cn",
            "hkrpg_cn",
            "bh3_cn",
            "hk4e_global",
            "nap_global",
            "hkrpg_global",
            "bh3_global",
        }
        if game_id not in supported:
            raise ValueError("compatibility.unsupported_hoyoplay_game")
        await self.prepare_compatibility()
        return await asyncio.to_thread(
            self.compatibility.ensure_hoyoplay_runtime, game_id
        )

    def claim_steam_install_request(self, app_id: int) -> dict[str, bool]:
        """Claim a fresh install request created by the standalone launcher."""
        request_file = self.data_directory / "compatibility" / "steam-install-request.json"
        try:
            payload = json.loads(request_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {"claimed": False}
            requested_at = float(payload.get("requestedAt", 0))
            matches = int(payload.get("appId", 0)) == int(app_id)
        except (OSError, TypeError, ValueError):
            return {"claimed": False}
        fresh = 0 <= time.time() - requested_at <= 120
        if not matches or not fresh:
            if not fresh:
                request_file.unlink(missing_ok=True)
            return {"claimed": False}
        request_file.unlink(missing_ok=True)
        return {"claimed": True}

    async def install_provider_tool(self, provider_id: str) -> dict[str, str]:
        if provider_id != "epic":
            raise ValueError("provider.no_managed_tool")
        provider = self.providers.get(provider_id)
        if not isinstance(provider, EpicProvider):
            raise RuntimeError("epic.unavailable")
        target = provider.data_directory / "tools" / "legendary"
        return await asyncio.to_thread(self.tool_installer.install_legendary, target)

    async def authenticate_epic(self, authorization_code: str) -> dict[str, object]:
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            raise RuntimeError("epic.unavailable")
        return await provider.authenticate(authorization_code)

    async def automatic_epic_login(self) -> dict[str, object]:
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            raise RuntimeError("epic.unavailable")
        browser = SteamBrowserAuthorization()
        # The RPC starts just before the frontend opens Steam's browser. Waiting
        # for that route prevents a cached Epic session from completing the RPC
        # before the browser has even appeared, which would leave a black page.
        await browser.wait_for_external_route()
        code = await browser.wait_for_epic_code()
        try:
            await provider.authenticate(code)
        finally:
            # Always leave the external route after Epic produced a code. This
            # also prevents authentication errors from trapping the user there.
            await browser.navigate_back()
        sync_result = await self.sync_provider_library("epic")
        status = await provider.connection_status()
        return {**status, "libraryCount": sync_result["count"]}

    async def refresh_provider_status(self, provider_id: str) -> dict[str, object]:
        return await self.providers.get(provider_id).connection_status()

    async def logout_provider(self, provider_id: str) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, EpicProvider):
            raise ValueError("provider.logout_unsupported")
        status = await provider.logout()
        browser_session_cleared = True
        try:
            await SteamBrowserAuthorization().clear_epic_session()
        except RuntimeError:
            # Legendary is already logged out. Report the best-effort browser
            # cleanup result without restoring or exposing authentication data.
            browser_session_cleared = False
        return {**status, "browserSessionCleared": browser_session_cleared}

    async def launch_provider_client(self, provider_id: str) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("provider.client_launch_unsupported")
        provider.prepare_launcher_game_association()
        if not self.compatibility.status()["ready"]:
            await self.prepare_compatibility()
        profile = await provider.resolve_launch(
            GameReference(provider_id, "launcher", provider.display_name, provider.spec.region)
        )
        command = UmuRuntime(os.fspath(self.compatibility.umu_executable)).build(profile)
        environment = self._umu_session_environment(command.environment)
        await asyncio.to_thread(self._authorize_x11_local_user, environment)
        await asyncio.create_subprocess_exec(
            *command.argv,
            env=environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return await provider.connection_status()

    def import_provider_installer(
        self, provider_id: str, source_path: str
    ) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("provider.installer_import_unsupported")
        return provider.import_installer(source_path)

    async def download_and_run_provider_installer(
        self, provider_id: str
    ) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("provider.installer_download_unsupported")
        await asyncio.to_thread(provider.download_installer)
        return await self.run_provider_installer(provider_id)

    async def download_provider_installer(self, provider_id: str) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("provider.installer_download_unsupported")
        return await asyncio.to_thread(provider.download_installer)

    async def run_provider_installer(self, provider_id: str) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("provider.installer_launch_unsupported")
        if not provider.managed_installer.is_file():
            raise FileNotFoundError("hoyoplay.installer_missing")
        existing = self.provider_installer_processes.get(provider_id)
        if existing is not None and existing.returncode is None:
            return {
                "state": "installer_already_running",
                "prefixPath": os.fspath(provider.prefix_directory),
            }
        if not self.compatibility.status()["ready"]:
            await self.prepare_compatibility()
        runtime_name, runtime_path = self.compatibility.selected_proton(
            provider_id, "launcher"
        )
        ensure_wine_storage_drive(provider.prefix_directory, provider.managed_installer)
        profile = RuntimeProfile(
            game_id=f"{provider_id}:installer",
            prefix_path=os.fspath(provider.prefix_directory),
            executable=os.fspath(provider.managed_installer),
            runtime_version=os.fspath(runtime_path),
            game_id_umu="umu-default",
            store="none",
            environment={"UMU_LOG": "1"},
        )
        command = UmuRuntime(os.fspath(self.compatibility.umu_executable)).build(profile)
        environment = self._umu_session_environment(command.environment)
        await asyncio.to_thread(self._authorize_x11_local_user, environment)
        log_directory = self.data_directory / "compatibility" / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"{provider_id}-installer.log"
        with log_path.open("ab") as log_stream:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                env=environment,
                stdout=log_stream,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self.provider_installer_processes[provider_id] = process
        try:
            return_code = await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            return_code = None
        if return_code not in (None, 0):
            raise RuntimeError(f"hoyoplay.installer_exited:{return_code}")
        return {
            "state": "installer_started",
            "runtime": runtime_name,
            "prefixPath": os.fspath(provider.prefix_directory),
            "logPath": os.fspath(log_path),
        }

    async def sync_provider_library(
        self, provider_id: str, *, resolve_artwork: bool = False
    ) -> dict[str, int]:
        provider = self.providers.get(provider_id)
        games = await provider.library()
        with self.database.connect() as db:
            if provider_id == "epic":
                current_releases = {
                    (game.external_game_id, game.region, game.release_channel)
                    for game in games
                }
                existing_releases = db.execute(
                    "SELECT id, canonical_game_id, external_game_id, region, release_channel "
                    "FROM game_releases WHERE provider_id=?",
                    (provider_id,),
                ).fetchall()
                stale_releases = [
                    row
                    for row in existing_releases
                    if (
                        str(row["external_game_id"]),
                        str(row["region"]),
                        str(row["release_channel"]),
                    )
                    not in current_releases
                ]
                db.executemany(
                    "DELETE FROM game_releases WHERE id=?",
                    ((int(row["id"]),) for row in stale_releases),
                )
                db.executemany(
                    "DELETE FROM catalog_games WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM game_releases WHERE canonical_game_id=?)",
                    (
                        (str(row["canonical_game_id"]), str(row["canonical_game_id"]))
                        for row in stale_releases
                    ),
                )
            for game in games:
                canonical_id = f"{game.provider_id}:{game.external_game_id}"
                normalized_title = " ".join(game.title.casefold().split())
                db.execute(
                    "INSERT INTO catalog_games"
                    "(id, title, normalized_title, compatibility_status) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
                    "normalized_title=excluded.normalized_title, "
                    "compatibility_status=excluded.compatibility_status",
                    (
                        canonical_id,
                        game.title,
                        normalized_title,
                        game.compatibility_status.value,
                    ),
                )
                db.execute(
                    "INSERT INTO game_releases"
                    "(canonical_game_id, provider_id, external_game_id, region, release_channel) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(provider_id, external_game_id, region, "
                    "release_channel) DO UPDATE SET canonical_game_id=excluded.canonical_game_id",
                    (
                        canonical_id,
                        game.provider_id,
                        game.external_game_id,
                        game.region,
                        game.release_channel,
                    ),
                )
        if provider_id == "epic":
            await self._resolve_steam_artwork(games)
            try:
                await provider.check_updates()
            except RuntimeError:
                # Library synchronization remains useful when Epic's update endpoint
                # is temporarily unavailable; retain the last successful update cache.
                pass
        artwork_count = 0
        if resolve_artwork and self.steamgriddb.configured():
            artwork_count = await self._resolve_community_artwork(games)
        return {"count": len(games), "artworkCount": artwork_count}

    async def _resolve_community_artwork(self, games: object) -> int:
        if not isinstance(games, (list, tuple)):
            return 0
        # SteamGridDB resolution makes several requests per game. Keep games
        # strictly serial so rate limiting or a transient network stall cannot
        # invalidate an entire provider batch before any result is persisted.
        semaphore = asyncio.Semaphore(1)

        async def resolve(game: object) -> bool:
            provider_id = getattr(game, "provider_id", None)
            external_id = getattr(game, "external_game_id", None)
            title = getattr(game, "title", None)
            if not all(isinstance(value, str) for value in (provider_id, external_id, title)):
                return False
            async with semaphore:
                result = await asyncio.to_thread(
                    self.steamgriddb.resolve, provider_id, external_id, title
                )
                return result is not None

        matches = await asyncio.gather(*(resolve(game) for game in games))
        return sum(matches)

    async def _resolve_steam_artwork(self, games: object) -> None:
        if not isinstance(games, (list, tuple)):
            return
        semaphore = asyncio.Semaphore(4)

        async def resolve(game: object) -> None:
            external_id = getattr(game, "external_game_id", None)
            title = getattr(game, "title", None)
            if not isinstance(external_id, str) or not isinstance(title, str):
                return
            metadata = self._epic_raw_metadata(external_id)
            developer = metadata.get("developer")
            async with semaphore:
                await asyncio.to_thread(
                    self.steam_artwork.resolve,
                    "epic",
                    external_id,
                    title,
                    developer if isinstance(developer, str) else None,
                )

        await asyncio.gather(*(resolve(game) for game in games))

    def list_games(self, query: str = "", offset: int = 0, limit: int = 8) -> dict[str, object]:
        offset = max(0, int(offset))
        limit = max(1, min(50, int(limit)))
        normalized_query = " ".join(query.casefold().split())
        escaped_query = (
            normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        with self.database.connect() as db:
            base_select = (
                "SELECT g.id, g.title, g.compatibility_status, r.provider_id, "
                "r.external_game_id, r.region, p.display_name AS provider_name "
                "FROM catalog_games g JOIN game_releases r ON r.canonical_game_id=g.id "
                "JOIN providers p ON p.id=r.provider_id "
            )
            ordering = "ORDER BY g.normalized_title, r.provider_id LIMIT ? OFFSET ?"
            if escaped_query:
                pattern = f"%{escaped_query}%"
                total = db.execute(
                    "SELECT COUNT(*) FROM catalog_games "
                    "WHERE normalized_title LIKE ? ESCAPE '\\'",
                    (pattern,),
                ).fetchone()[0]
                rows = db.execute(
                    base_select
                    + "WHERE g.normalized_title LIKE ? ESCAPE '\\' "
                    + ordering,
                    (pattern, limit, offset),
                ).fetchall()
            else:
                total = db.execute("SELECT COUNT(*) FROM catalog_games").fetchone()[0]
                rows = db.execute(base_select + ordering, (limit, offset)).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def game_details(self, game_id: str) -> dict[str, object]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT g.id, g.title, g.compatibility_status, r.provider_id, "
                "r.external_game_id, r.region, r.release_channel, "
                "p.display_name AS provider_name "
                "FROM catalog_games g JOIN game_releases r ON r.canonical_game_id=g.id "
                "JOIN providers p ON p.id=r.provider_id WHERE g.id=? "
                "ORDER BY r.provider_id LIMIT 1",
                (game_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown game: {game_id}")
        details: dict[str, object] = {
            **dict(row),
            "description": None,
            "developer": None,
            "artwork_url": None,
            "installed": False,
            "install_path": None,
        }
        if row["provider_id"] == "epic":
            details.update(self._epic_game_metadata(row["external_game_id"]))
            details.update(self._epic_installation(row["external_game_id"]))
            provider = self.providers.get("epic")
            if isinstance(provider, EpicProvider):
                details.update(provider.cached_update(row["external_game_id"]))
        elif row["provider_id"] in {"mihoyo_cn", "hoyoplay_global"}:
            provider = self.providers.get(row["provider_id"])
            if isinstance(provider, HoYoPlayProvider):
                details.update(provider.game_installation(row["external_game_id"]))
                details["steam_shortcut"] = self._hoyoplay_steam_shortcut(details)
        community_artwork = self.steamgriddb.cached(
            str(row["provider_id"]), str(row["external_game_id"])
        )
        if community_artwork:
            details.update(
                {
                    "artwork_url": community_artwork.get("capsule"),
                    "hero_url": community_artwork.get("hero"),
                    "header_url": community_artwork.get("header"),
                    "logo_url": community_artwork.get("logo"),
                    "icon_url": community_artwork.get("icon"),
                    "artwork_source": "steamgriddb",
                    "artwork_language": self.steamgriddb.cached_language(
                        str(row["provider_id"]), str(row["external_game_id"])
                    ),
                }
            )
        latest_job = self.jobs.latest_for_game(row["provider_id"], row["external_game_id"])
        if latest_job is not None:
            details["install_job"] = self.install_job(latest_job.id)
        else:
            details["install_job"] = None
        return details

    def capture_hoyoplay_channel_profile(self, game_id: str) -> dict[str, object]:
        details = self.game_details(game_id)
        if details.get("provider_id") != "mihoyo_cn":
            raise ValueError("hoyoplay.channel_switch_cn_only")
        provider = self.providers.get("mihoyo_cn")
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("hoyoplay.provider_unavailable")
        return dict(provider.capture_channel_profile(str(details["external_game_id"])))

    def switch_hoyoplay_channel_profile(
        self, game_id: str, channel: str
    ) -> dict[str, object]:
        details = self.game_details(game_id)
        if details.get("provider_id") != "mihoyo_cn":
            raise ValueError("hoyoplay.channel_switch_cn_only")
        provider = self.providers.get("mihoyo_cn")
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("hoyoplay.provider_unavailable")
        external_game_id = str(details["external_game_id"])
        if external_game_id in {"hk4e_cn", "nap_cn", "hkrpg_cn"}:
            provider.switch_channel_selection(channel)
        else:
            provider.switch_channel_profile(external_game_id, channel)
        return dict(provider.channel_profile_status(external_game_id))

    def hoyoplay_channel_selection(self) -> dict[str, object]:
        provider = self.providers.get("mihoyo_cn")
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("hoyoplay.provider_unavailable")
        try:
            selected = (self.data_directory / "mihoyo-selection").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            selected = str(provider.channel_selection_status()["current"])
        if selected not in {"official", "bilibili", "global"}:
            selected = str(provider.channel_selection_status()["current"])
        return {"current": selected}

    def switch_hoyoplay_channel_selection(self, channel: str) -> dict[str, object]:
        if channel not in {"official", "bilibili", "global"}:
            raise ValueError("hoyoplay.unsupported_channel")
        provider = self.providers.get("mihoyo_cn")
        if not isinstance(provider, HoYoPlayProvider):
            raise ValueError("hoyoplay.provider_unavailable")
        if channel != "global":
            provider.switch_channel_selection(channel)
        selection = self.data_directory / "mihoyo-selection"
        staged = selection.with_suffix(".new")
        staged.write_text(channel, encoding="utf-8")
        os.replace(staged, selection)
        return {"current": channel}

    def steam_library_games(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        offset = 0
        while True:
            page = self.list_games("", offset, 50)
            page_items = page["items"]
            if not isinstance(page_items, list):
                break
            items.extend(page_items)
            offset += len(page_items)
            if not page_items or offset >= int(page["total"]):
                break
        games: list[dict[str, object]] = []
        for item in items:
            game = dict(item)
            if game["provider_id"] == "hoyoplay_global" and game["external_game_id"] in {
                "hk4e_global", "nap_global", "hkrpg_global", "bh3_global"
            }:
                # These global releases are routed through the existing CN cards
                # so Steam keeps one App ID, artwork set and controller layout.
                continue
            if game["provider_id"] in {"mihoyo_cn", "hoyoplay_global"}:
                provider = self.providers.get(str(game["provider_id"]))
                if not isinstance(provider, HoYoPlayProvider) or provider.launcher_executable() is None:
                    continue
                game.update(provider.game_installation(str(game["external_game_id"])))
                game["steam_shortcut"] = self._hoyoplay_steam_shortcut(game)
                cached_artwork = self.official_artwork.cached(
                    str(game["provider_id"]), str(game["external_game_id"])
                )
                artwork_source = "official"
                if not cached_artwork:
                    cached_artwork = self.steam_artwork.cached(
                        str(game["provider_id"]), str(game["external_game_id"])
                    )
                    artwork_source = "steam"
                if cached_artwork:
                    game.update(
                        {
                            "artwork_url": cached_artwork.get("capsule"),
                            "hero_url": cached_artwork.get("hero"),
                            "header_url": cached_artwork.get("header"),
                            "logo_url": cached_artwork.get("logo"),
                            "icon_url": cached_artwork.get("icon"),
                            "artwork_source": artwork_source,
                        }
                    )
            elif game["provider_id"] == "epic":
                game.update(self._epic_game_metadata(str(game["external_game_id"])))
                game.update(self._epic_installation(str(game["external_game_id"])))
                provider = self.providers.get("epic")
                if isinstance(provider, EpicProvider):
                    game.update(provider.cached_update(str(game["external_game_id"])))
            else:
                continue
            community_artwork = self.steamgriddb.cached(
                str(game["provider_id"]), str(game["external_game_id"])
            )
            if community_artwork:
                game.update(
                    {
                        "artwork_url": community_artwork.get("capsule"),
                        "hero_url": community_artwork.get("hero"),
                        "header_url": community_artwork.get("header"),
                        "logo_url": community_artwork.get("logo"),
                        "icon_url": community_artwork.get("icon"),
                        "artwork_source": "steamgriddb",
                        "artwork_language": self.steamgriddb.cached_language(
                            str(game["provider_id"]), str(game["external_game_id"])
                        ),
                    }
                )
            with self.database.connect() as connection:
                shortcut = connection.execute(
                    "SELECT steam_app_id FROM steam_shortcuts "
                    "WHERE provider_id=? AND external_game_id=?",
                    (game["provider_id"], game["external_game_id"]),
                ).fetchone()
            game["steam_app_id"] = shortcut["steam_app_id"] if shortcut else None
            games.append(game)
        return games

    def _managed_play_history_games(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.provider_id, s.external_game_id, s.steam_app_id, "
                "COALESCE(c.title, s.external_game_id) AS title "
                "FROM steam_shortcuts s "
                "LEFT JOIN game_releases r ON r.provider_id=s.provider_id "
                "AND r.external_game_id=s.external_game_id "
                "LEFT JOIN catalog_games c ON c.id=r.canonical_game_id "
                "WHERE s.provider_id IN ('epic', 'mihoyo_cn', 'hoyoplay_global') "
                "ORDER BY s.provider_id, s.external_game_id"
            ).fetchall()
        return [
            {
                "providerId": str(row["provider_id"]),
                "externalGameId": str(row["external_game_id"]),
                "steamAppId": int(row["steam_app_id"]),
                "title": str(row["title"]),
            }
            for row in rows
        ]

    def play_history_default_directory(self) -> str:
        return os.fspath(Path.home() / "Desktop")

    def latest_play_history_export(self) -> str:
        exports = self.play_history_exports()
        if exports:
            return str(exports[0]["path"])
        raise ValueError("play_history.no_export_on_desktop")

    def play_history_exports(self) -> list[dict[str, object]]:
        exports: list[dict[str, object]] = []
        for candidate in (Path.home() / "Desktop").glob("GameBridge-play-history-*.json"):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                payload.get("format") == "gamebridge.play-history"
                and payload.get("version") == 1
                and isinstance(payload.get("games"), list)
            ):
                exports.append({
                    "path": os.fspath(candidate),
                    "name": candidate.name,
                    "exportedAt": str(payload.get("exportedAt", "")),
                    "gameCount": len(payload["games"]),
                    "modifiedNs": candidate.stat().st_mtime_ns,
                })
        exports.sort(key=lambda item: int(item["modifiedNs"]), reverse=True)
        return exports

    def export_play_history(self, runtime: list[dict[str, int]] | None = None) -> dict[str, object]:
        stored = read_history_store(self.data_directory / "play-history.json")
        return export_history(Path.home(), self._managed_play_history_games(), runtime, stored)

    def import_play_history(
        self, source_path: str, runtime: list[dict[str, int]] | None = None
    ) -> dict[str, object]:
        steam_process = self._steam_process_identity()
        if steam_process is None:
            raise ValueError("play_history.steam_not_running")
        steam_pid, steam_start_time = steam_process
        pending = self.data_directory / "pending-play-history.json"
        result = stage_history_import(
            Path.home(),
            Path(source_path).expanduser().resolve(),
            self._managed_play_history_games(),
            pending,
            steam_pid,
            steam_start_time,
            None,
            runtime,
        )
        worker = Path(__file__).with_name("play_history_worker.py")
        log = self.data_directory / "play-history-worker.log"
        with log.open("ab") as output:
            subprocess.Popen(
                [
                    "/usr/bin/python3",
                    os.fspath(worker),
                    os.fspath(pending),
                    os.fspath(self.data_directory / "play-history-import-result.json"),
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        return result

    @staticmethod
    def _steam_process_identity() -> tuple[int, str] | None:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "comm").read_text(encoding="utf-8").strip()
                fields = (entry / "stat").read_text(encoding="utf-8").split()
            except OSError:
                continue
            if command == "steam" and len(fields) >= 22:
                return int(entry.name), fields[21]
        return None

    def _hoyoplay_steam_shortcut(
        self, game: dict[str, object]
    ) -> dict[str, str] | None:
        """Return only real-device-verified direct Steam shortcut profiles."""
        if game.get("provider_id") != "mihoyo_cn":
            return None
        unified_games = {
            "nap_cn": "zzz",
            "hkrpg_cn": "starrail",
            "bh3_cn": "honkai3",
        }
        try:
            selected_region = (self.data_directory / "mihoyo-selection").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            selected_region = "official"
        if (
            game.get("external_game_id") == "hk4e_cn"
            and selected_region == "global"
        ):
            unified_games["hk4e_cn"] = "genshin"
        unified_game = unified_games.get(str(game.get("external_game_id")))
        if unified_game is not None:
            return {
                "mode": "gamebridge_router",
                "executable": "/usr/bin/python3",
                "start_directory": "/home/deck/homebrew/plugins/GameBridge",
                "launch_options": (
                    '"gamebridge/launcher.py" --provider mihoyo '
                    f'--game-id "{unified_game}"'
                ),
                "compatibility_tool": "",
            }
        if game.get("external_game_id") not in {"hk4e_cn", "nap_cn", "hkrpg_cn", "bh3_cn"}:
            return None
        if game.get("launchable") is False:
            return None
        executable = game.get("executable")
        if not game.get("installed") or not isinstance(executable, str):
            return None
        candidate = Path(executable)
        if not candidate.is_file() or game.get("storage_state") != "writable":
            return None
        dwproton = [
            (name, path)
            for name, path in self.compatibility.proton_layers()
            if name.casefold().startswith("dwproton-")
        ]
        if not dwproton:
            return None

        def version_key(item: tuple[str, Path]) -> tuple[int, ...]:
            return tuple(int(value) for value in re.findall(r"\d+", item[0]))

        runtime_name, _runtime_path = max(dwproton, key=version_key)
        guard = Path.home() / "homebrew" / "plugins" / "GameBridge" / "gamebridge" / "channel_guard.py"
        return {
            "mode": "direct_executable",
            "executable": os.fspath(candidate),
            "start_directory": os.fspath(candidate.parent),
            "launch_options": (
                f'WINE_ENABLE_TIMEOUT_FIX=1 "{guard}" '
                f'--game-id "{game["external_game_id"]}" -- %command%'
            ),
            "compatibility_tool": runtime_name,
        }

    async def refresh_official_artwork_catalog(self) -> None:
        """Warm official public artwork before returning native library cards."""
        provider = self.providers.get("mihoyo_cn")
        if not isinstance(provider, HoYoPlayProvider) or provider.launcher_executable() is None:
            return
        for external_game_id in (game.external_game_id for game in provider.spec.games):
            await asyncio.to_thread(
                self.official_artwork.resolve,
                "mihoyo_cn",
                external_game_id,
            )

    async def refresh_steam_artwork(
        self, game_id: str, language: str | None = None
    ) -> dict[str, object]:
        details = self.game_details(game_id)
        community = await asyncio.to_thread(
            self.steamgriddb.resolve,
            str(details["provider_id"]),
            str(details["external_game_id"]),
            str(details["title"]),
            language=language,
        )
        if community:
            details.update(
                {
                    "artwork_url": community.get("capsule"),
                    "hero_url": community.get("hero"),
                    "header_url": community.get("header"),
                    "logo_url": community.get("logo"),
                    "icon_url": community.get("icon"),
                    "artwork_source": "steamgriddb",
                    "artwork_language": self.steamgriddb.cached_language(
                        str(details["provider_id"]), str(details["external_game_id"])
                    ),
                }
            )
            return details
        if details["provider_id"] in {"mihoyo_cn", "hoyoplay_global"}:
            official = await asyncio.to_thread(
                self.official_artwork.resolve,
                str(details["provider_id"]),
                str(details["external_game_id"]),
            )
            if official:
                details.update(
                    {
                        "artwork_url": official.get("capsule"),
                        "hero_url": official.get("hero"),
                        "header_url": official.get("header"),
                        "logo_url": official.get("logo"),
                        "artwork_source": "official",
                    }
                )
                return details
            await asyncio.to_thread(
                self.steam_artwork.resolve,
                str(details["provider_id"]),
                str(details["external_game_id"]),
                str(details["title"]),
                None,
            )
            cached = self.steam_artwork.cached(
                str(details["provider_id"]), str(details["external_game_id"])
            )
            if cached:
                details.update(
                    {
                        "artwork_url": cached.get("capsule"),
                        "hero_url": cached.get("hero"),
                        "header_url": cached.get("header"),
                        "logo_url": cached.get("logo"),
                        "artwork_source": "steam",
                    }
                )
            return details
        if details["provider_id"] != "epic":
            return details
        external_id = str(details["external_game_id"])
        metadata = self._epic_raw_metadata(external_id)
        developer = metadata.get("developer")
        await asyncio.to_thread(
            self.steam_artwork.resolve,
            "epic",
            external_id,
            str(details["title"]),
            developer if isinstance(developer, str) else None,
        )
        return self.game_details(game_id)

    def artwork_settings(self) -> dict[str, object]:
        return {
            "steamGridDbConfigured": self.steamgriddb.configured(),
            "steamGridDbLastValidationSucceeded": self._steamgriddb_last_validation,
        }

    async def save_steamgriddb_key(self, key: str) -> dict[str, object]:
        self._steamgriddb_last_validation = False
        self.steamgriddb.save_key(key)
        connected = await asyncio.to_thread(self.steamgriddb.test_connection)
        if not connected:
            raise ValueError("steamgriddb.connection_failed")
        self._steamgriddb_last_validation = True
        return {**self.artwork_settings(), "connected": True}

    async def test_steamgriddb_connection(self) -> dict[str, bool]:
        connected = await asyncio.to_thread(self.steamgriddb.test_connection)
        if not connected:
            raise ValueError("steamgriddb.connection_failed")
        return {"connected": True}

    async def download_steamgriddb_artwork(
        self, url: str, steam_userdata: str | Path | None = None
    ) -> dict[str, str]:
        references = self.steamgriddb.cached_asset_references(url)
        if references and steam_userdata is not None:
            with self.database.connect() as connection:
                mapped = [
                    (reference, connection.execute(
                        "SELECT steam_app_id FROM steam_shortcuts WHERE provider_id=? "
                        "AND external_game_id=?",
                        (reference[0], reference[1]),
                    ).fetchone())
                    for reference in references
                ]
            userdata = Path(steam_userdata).resolve()
            if userdata.is_dir():
                for (provider_id, external_game_id, asset), shortcut in mapped:
                    if shortcut is None:
                        continue
                    suffix = {
                        "capsule": "p", "hero": "_hero", "header": "",
                        "logo": "_logo", "icon": "_icon",
                    }[asset]
                    app_id = int(shortcut["steam_app_id"])
                    local = self._read_local_steam_artwork(userdata, app_id, suffix)
                    if local is not None:
                        return local
        return await asyncio.to_thread(self.steamgriddb.download_image, url)

    @staticmethod
    def _read_local_steam_artwork(
        userdata: Path, app_id: int, suffix: str
    ) -> dict[str, str] | None:
        for account in userdata.iterdir():
            grid = account / "config" / "grid"
            if not account.name.isdigit():
                continue
            for extension, mime_type in (
                (".png", "image/png"), (".jpg", "image/jpeg"),
                (".jpeg", "image/jpeg"), (".ico", "image/x-icon"),
            ):
                candidate = grid / f"{app_id}{suffix}{extension}"
                try:
                    data = candidate.read_bytes()
                except OSError:
                    continue
                if data and len(data) <= 10 * 1024 * 1024:
                    return {
                        "base64": base64.b64encode(data).decode("ascii"),
                        "mimeType": mime_type,
                    }
        return None

    @staticmethod
    def _steam_artwork_dimensions(path: Path) -> tuple[int, int] | None:
        """Read common image dimensions without adding an image-library dependency."""
        try:
            with path.open("rb") as stream:
                header = stream.read(24)
                if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) == 24:
                    return struct.unpack(">II", header[16:24])
                if header[:2] != b"\xff\xd8":
                    return None
                stream.seek(2)
                while True:
                    marker = stream.read(2)
                    if len(marker) != 2 or marker[0] != 0xFF:
                        return None
                    while marker[1] == 0xFF:
                        marker = bytes((0xFF, stream.read(1)[0]))
                    if marker[1] in {0xD8, 0xD9}:
                        continue
                    length_data = stream.read(2)
                    if len(length_data) != 2:
                        return None
                    length = struct.unpack(">H", length_data)[0]
                    if marker[1] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                        size = stream.read(5)
                        return struct.unpack(">HH", size[1:5])[::-1] if len(size) == 5 else None
                    stream.seek(length - 2, os.SEEK_CUR)
        except (OSError, IndexError, struct.error):
            return None

    @classmethod
    def _steam_artwork_file_valid(cls, path: Path, asset: str) -> bool:
        dimensions = cls._steam_artwork_dimensions(path)
        if dimensions is None:
            return True
        width, height = dimensions
        if width <= 0 or height <= 0:
            return False
        ratio = width / height
        return {
            "capsule": ratio < 1,
            "hero": ratio >= 2,
            "header": 1.8 <= ratio <= 2.3,
            "logo": True,
            "icon": 0.75 <= ratio <= 1.33,
        }[asset]

    async def install_steam_shortcut_artwork(
        self,
        provider_id: str,
        external_game_id: str,
        steam_app_id: int,
        steam_userdata: str | Path,
    ) -> dict[str, int | str]:
        with self.database.connect() as connection:
            mapped = connection.execute(
                "SELECT 1 FROM steam_shortcuts WHERE provider_id=? "
                "AND external_game_id=? AND steam_app_id=?",
                (provider_id, external_game_id, steam_app_id),
            ).fetchone()
        if mapped is None:
            raise ValueError("steam.shortcut_mapping_missing")
        artwork = self.steamgriddb.cached(provider_id, external_game_id)
        if artwork is None:
            raise ValueError("steamgriddb.artwork_missing")
        userdata = Path(steam_userdata).resolve()
        if not userdata.is_dir():
            raise ValueError("steam.userdata_missing")
        targets = {
            "capsule": "p",
            "hero": "_hero",
            "header": "",
            "logo": "_logo",
            "icon": "_icon",
        }
        accounts = [
            account for account in userdata.iterdir()
            if account.name.isdigit() and (account / "config" / "shortcuts.vdf").is_file()
        ]
        extensions = (".png", ".jpg", ".jpeg", ".ico")
        written = 0
        icon_path = ""
        for asset, suffix in targets.items():
            pending: list[tuple[Path, Path | None]] = []
            for account in accounts:
                grid = account / "config" / "grid"
                grid.mkdir(parents=True, exist_ok=True)
                existing = next(
                    (grid / f"{steam_app_id}{suffix}{extension}"
                     for extension in extensions
                    if (grid / f"{steam_app_id}{suffix}{extension}").is_file()),
                    None,
                )
                if existing is not None and self._steam_artwork_file_valid(existing, asset):
                    if suffix == "_icon" and not icon_path:
                        icon_path = str(existing)
                    written += 1
                    continue
                pending.append((grid, existing))
            if not pending:
                continue
            url = artwork.get(asset)
            if not url:
                continue
            try:
                image = await asyncio.to_thread(self.steamgriddb.download_image, url)
            except ValueError:
                # Preserve every asset already written. A later sync retries only
                # this missing item instead of restarting the whole five-file set.
                continue
            for grid, invalid_existing in pending:
                extension = {
                    "image/png": ".png",
                    "image/x-icon": ".ico",
                    "image/vnd.microsoft.icon": ".ico",
                }.get(image["mimeType"], ".jpg")
                destination = grid / f"{steam_app_id}{suffix}{extension}"
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                temporary.write_bytes(base64.b64decode(image["base64"], validate=True))
                os.replace(temporary, destination)
                if invalid_existing is not None and invalid_existing != destination:
                    invalid_existing.unlink(missing_ok=True)
                if suffix == "_icon" and not icon_path:
                    icon_path = str(destination)
                written += 1
        return {"written": written, "iconPath": icon_path}

    async def ensure_all_steam_shortcut_artwork(
        self, steam_userdata: str | Path
    ) -> dict[str, int]:
        userdata = Path(steam_userdata).resolve()
        if not userdata.is_dir():
            raise ValueError("steam.userdata_missing")
        accounts = [
            account for account in userdata.iterdir()
            if account.name.isdigit() and (account / "config" / "shortcuts.vdf").is_file()
        ]
        with self.database.connect() as connection:
            shortcuts = connection.execute(
                "SELECT provider_id, external_game_id, steam_app_id FROM steam_shortcuts"
            ).fetchall()
        ready = synced = failed = 0
        for shortcut in shortcuts:
            provider_id = str(shortcut["provider_id"])
            external_game_id = str(shortcut["external_game_id"])
            steam_app_id = int(shortcut["steam_app_id"])
            if self.steamgriddb.cached(provider_id, external_game_id) is None:
                continue
            assets = (("capsule", "p"), ("hero", "_hero"), ("header", ""),
                      ("logo", "_logo"), ("icon", "_icon"))
            complete = bool(accounts) and all(
                any((candidate := account / "config" / "grid" /
                     f"{steam_app_id}{suffix}{extension}").is_file()
                    and self._steam_artwork_file_valid(candidate, asset)
                    for extension in (".png", ".jpg", ".jpeg", ".ico"))
                for account in accounts
                for asset, suffix in assets
            )
            if complete:
                ready += 1
                continue
            try:
                result = await self.install_steam_shortcut_artwork(
                    provider_id, external_game_id, steam_app_id, userdata
                )
                if result["written"] >= 5:
                    synced += 1
                else:
                    failed += 1
            except (OSError, ValueError, KeyError):
                failed += 1
        return {"ready": ready, "synced": synced, "failed": failed}

    async def refresh_all_community_artwork(self, language: str = "en") -> dict[str, int]:
        if not self.steamgriddb.configured():
            raise ValueError("steamgriddb.not_configured")
        page = self.list_games("", 0, 50)
        semaphore = asyncio.Semaphore(1)

        async def refresh(game: dict[str, object]) -> bool:
            async with semaphore:
                match = await asyncio.to_thread(
                    self.steamgriddb.resolve,
                    str(game["provider_id"]),
                    str(game["external_game_id"]),
                    str(game["title"]),
                    force=True,
                    language=language,
                )
                return match is not None

        matches = await asyncio.gather(*(refresh(game) for game in page["items"]))
        return {"count": sum(matches)}

    async def ensure_community_artwork(self) -> None:
        """Fill missing community artwork before the native library is rendered."""
        if not self.steamgriddb.configured():
            return
        page = self.list_games("", 0, 50)
        missing = [
            game
            for game in page["items"]
            if self.steamgriddb.needs_refresh(
                str(game["provider_id"]), str(game["external_game_id"])
            )
        ]
        semaphore = asyncio.Semaphore(1)

        async def resolve(game: dict[str, object]) -> None:
            async with semaphore:
                await asyncio.to_thread(
                    self.steamgriddb.resolve,
                    str(game["provider_id"]),
                    str(game["external_game_id"]),
                    str(game["title"]),
                )

        await asyncio.gather(*(resolve(game) for game in missing))

    async def backfill_community_artwork(
        self,
        steam_userdata: str | Path,
        provider_id: str | None = None,
    ) -> dict[str, int]:
        """Resolve and install artwork one game at a time outside UI RPCs.

        Each game's URLs are persisted incrementally by ``SteamGridDbResolver``
        and immediately copied into every mapped Steam shortcut before moving
        to the next game. Existing native files remain the source of truth:
        ``install_steam_shortcut_artwork`` skips every valid kind already on
        disk and therefore naturally backfills only gaps.
        """
        if not self.steamgriddb.configured():
            return {"processed": 0, "matched": 0, "installed": 0, "failed": 0}
        page = self.list_games("", 0, 50)
        games = [
            game for game in page["items"]
            if provider_id is None or str(game["provider_id"]) == provider_id
        ]
        processed = matched = installed = failed = 0
        for game in games:
            current_provider = str(game["provider_id"])
            external_id = str(game["external_game_id"])
            processed += 1
            if self.steamgriddb.needs_refresh(current_provider, external_id):
                try:
                    await asyncio.to_thread(
                        self.steamgriddb.resolve,
                        current_provider,
                        external_id,
                        str(game["title"]),
                    )
                except (OSError, ValueError):
                    failed += 1
            if self.steamgriddb.cached(current_provider, external_id) is None:
                continue
            matched += 1
            with self.database.connect() as connection:
                shortcuts = connection.execute(
                    "SELECT steam_app_id FROM steam_shortcuts "
                    "WHERE provider_id=? AND external_game_id=?",
                    (current_provider, external_id),
                ).fetchall()
            for shortcut in shortcuts:
                try:
                    result = await self.install_steam_shortcut_artwork(
                        current_provider,
                        external_id,
                        int(shortcut["steam_app_id"]),
                        steam_userdata,
                    )
                    if result["written"]:
                        installed += 1
                except (OSError, ValueError, KeyError):
                    failed += 1
        return {
            "processed": processed,
            "matched": matched,
            "installed": installed,
            "failed": failed,
        }

    def register_steam_shortcut(
        self, provider_id: str, external_game_id: str, steam_app_id: int
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO steam_shortcuts(provider_id, external_game_id, steam_app_id) "
                "VALUES(?,?,?) "
                "ON CONFLICT(provider_id, external_game_id) DO UPDATE SET "
                "steam_app_id=excluded.steam_app_id, updated_at=CURRENT_TIMESTAMP",
                (provider_id, external_game_id, steam_app_id),
            )

    def unregister_steam_shortcut(
        self, provider_id: str, external_game_id: str, steam_app_id: int
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM steam_shortcuts WHERE provider_id=? "
                "AND external_game_id=? AND steam_app_id=?",
                (provider_id, external_game_id, steam_app_id),
            )

    def steam_game_details(
        self, steam_app_id: int, title: str | None = None
    ) -> dict[str, object] | None:
        with self.database.connect() as connection:
            shortcut = connection.execute(
                "SELECT provider_id, external_game_id FROM steam_shortcuts WHERE steam_app_id=?",
                (steam_app_id,),
            ).fetchone()
            if shortcut is None and isinstance(title, str) and title.strip():
                matches = connection.execute(
                    "SELECT r.provider_id, r.external_game_id FROM game_releases r "
                    "JOIN catalog_games g ON g.id=r.canonical_game_id WHERE g.title=?",
                    (title.strip(),),
                ).fetchall()
                if len(matches) == 1:
                    shortcut = matches[0]
        if shortcut is None:
            return None
        details = self.game_details(f"{shortcut['provider_id']}:{shortcut['external_game_id']}")
        app_id = int(steam_app_id)
        try:
            config = locate_localconfig(Path.home(), [app_id])
            steam_history = read_app_history(
                config.read_text(encoding="utf-8", errors="surrogateescape")
            ).get(app_id, {"playtimeMinutes": 0, "lastPlayed": 0})
        except (OSError, ValueError):
            steam_history = {"playtimeMinutes": 0, "lastPlayed": 0}
        local_history = read_history_store(self.data_directory / "play-history.json").get(
            (str(shortcut["provider_id"]), str(shortcut["external_game_id"])),
            {"playtimeMinutes": 0, "lastPlayed": 0},
        )
        details["play_history"] = {
            "playtimeMinutes": int(steam_history["playtimeMinutes"]),
            "lastPlayed": max(
                int(steam_history["lastPlayed"]), int(local_history["lastPlayed"])
            ),
        }
        return details

    def start_game_install(self, game_id: str, install_path: str | None = None) -> dict[str, str]:
        details = self.game_details(game_id)
        if details["provider_id"] != "epic" or self.epic_installs is None:
            raise ValueError("provider.install_unsupported")
        job_id = self.epic_installs.start(
            str(details["external_game_id"]),
            str(details["title"]),
            Path(install_path) if install_path else Path.home() / "Games" / "GameBridge" / "Epic",
        )
        return {"jobId": job_id}

    def start_game_update(self, game_id: str) -> dict[str, str]:
        details = self.game_details(game_id)
        if details["provider_id"] != "epic" or self.epic_installs is None:
            raise ValueError("provider.update_unsupported")
        if not details.get("installed"):
            raise ValueError("update.game_not_installed")
        install_path = details.get("install_path")
        if not isinstance(install_path, str) or not install_path:
            raise ValueError("update.install_path_missing")
        job_id = self.epic_installs.start(
            str(details["external_game_id"]),
            str(details["title"]),
            Path(install_path).resolve().parent,
            operation="update",
        )
        return {"jobId": job_id}

    def start_visible_update_simulation(self) -> dict[str, object]:
        """Create a UI-only update task for an installed Epic Steam shortcut."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT g.id, g.title, r.external_game_id, s.steam_app_id "
                "FROM catalog_games g "
                "JOIN game_releases r ON r.canonical_game_id=g.id "
                "JOIN steam_shortcuts s ON s.provider_id=r.provider_id "
                "AND s.external_game_id=r.external_game_id "
                "WHERE r.provider_id='epic' "
                "ORDER BY CASE WHEN g.normalized_title LIKE '%arknight%' THEN 0 ELSE 1 END, "
                "g.normalized_title"
            ).fetchall()
        selected = next((row for row in rows if self.game_details(row["id"])["installed"]), None)
        if selected is None:
            raise ValueError("simulation.installed_shortcut_required")
        external_id = str(selected["external_game_id"])
        existing = self.jobs.latest_for_game("epic", external_id)
        terminal = {
            JobState.COMPLETED,
            JobState.CANCELLED,
            JobState.FAILED_RETRYABLE,
            JobState.FAILED_PERMANENT,
        }
        if (
            existing is not None
            and existing.payload.get("simulated")
            and existing.state not in terminal
        ):
            return {
                "jobId": existing.id,
                "gameId": selected["id"],
                "steamAppId": selected["steam_app_id"],
                "title": selected["title"],
            }
        details = self.game_details(str(selected["id"]))
        job = self.jobs.create(
            "epic",
            external_id,
            {
                "title": selected["title"],
                "installRoot": details.get("install_path") or "",
                "phase": "install.downloading",
                "downloadedMiB": 0,
                "speedMiBs": 18.5,
                "eta": "00:01:00",
                "operation": "update",
                "simulated": True,
            },
        )
        for state in (
            JobState.VALIDATING,
            JobState.WAITING_FOR_SPACE,
            JobState.DOWNLOADING_INSTALLER,
            JobState.VERIFYING_INSTALLER,
            JobState.PREPARING_PREFIX,
            JobState.INSTALLING_LAUNCHER,
            JobState.WAITING_FOR_LOGIN,
            JobState.DOWNLOADING_GAME,
        ):
            self.jobs.transition(job.id, state)
        self.simulated_updates[job.id] = asyncio.create_task(
            self._run_visible_update_simulation(job.id)
        )
        return {
            "jobId": job.id,
            "gameId": selected["id"],
            "steamAppId": selected["steam_app_id"],
            "title": selected["title"],
        }

    async def _run_visible_update_simulation(self, job_id: str) -> None:
        try:
            # Keep this UI-only test visible long enough to inspect navigation,
            # pausing, resuming and progress rendering in Gaming Mode.
            step = max(1, round(self.jobs.get(job_id).progress * 120))
            while step <= 120:
                job = self.jobs.get(job_id)
                if job.state == JobState.PAUSED:
                    await asyncio.sleep(0.25)
                    continue
                if job.state != JobState.DOWNLOADING_GAME:
                    return
                progress = step / 120
                remaining = max(0, 120 - step)
                minutes, seconds = divmod(remaining, 60)
                self.jobs.update_progress(
                    job_id,
                    progress,
                    {
                        "phase": "install.downloading",
                        "downloadedMiB": round(progress * 2048, 1),
                        "speedMiBs": 18.5,
                        "eta": f"00:{minutes:02d}:{seconds:02d}",
                    },
                )
                step += 1
                await asyncio.sleep(1)
            self.jobs.transition(job_id, JobState.VERIFYING_GAME)
            self.jobs.transition(job_id, JobState.CONFIGURING_RUNTIME)
            self.jobs.transition(job_id, JobState.CREATING_SHORTCUT)
            self.jobs.transition(job_id, JobState.COMPLETED)
        except asyncio.CancelledError:
            try:
                self.jobs.transition(job_id, JobState.CANCELLED)
            except ValueError:
                pass
            raise
        finally:
            self.simulated_updates.pop(job_id, None)

    @staticmethod
    def storage_info(path: str) -> dict[str, object]:
        candidate = Path(path).expanduser().resolve()
        if not approved_install_path(candidate):
            raise ValueError("install.path_unapproved")
        candidate.mkdir(parents=True, exist_ok=True)
        filesystem = os.statvfs(candidate)
        return {
            "path": str(candidate),
            "device": candidate.stat().st_dev,
            "free_bytes": filesystem.f_bavail * filesystem.f_frsize,
        }

    @staticmethod
    def _storage_roots() -> list[Path]:
        return [root.path for root in storage_roots()]

    async def storage_locations(self, game_id: str) -> dict[str, object]:
        details = self.game_details(game_id)
        if details["provider_id"] != "epic":
            raise ValueError("provider.install_unsupported")
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            raise RuntimeError("epic.unavailable")
        sizes = await provider.install_sizes(str(details["external_game_id"]))
        required = sizes["required_bytes"]
        locations: list[dict[str, object]] = []
        for storage in storage_roots():
            root = storage.path
            install_path = root / "Games" / "GameBridge" / "Epic"
            try:
                filesystem = os.statvfs(root)
                free_bytes = filesystem.f_bavail * filesystem.f_frsize
            except OSError:
                continue
            is_internal = storage.internal
            is_sd = "mmcblk" in storage.source
            display_name = "storage.internal" if is_internal else str(root)
            locations.append({
                "id": "internal" if is_internal else str(root),
                "name": display_name,
                "path": str(install_path),
                "free_bytes": free_bytes,
                "enough_space": free_bytes >= required,
                "kind": "internal" if is_internal else ("sd" if is_sd else "drive"),
            })
        recommended = max(
            (item for item in locations if item["enough_space"]),
            key=lambda item: int(item["free_bytes"]),
            default=None,
        )
        return {
            **sizes,
            "locations": locations,
            "recommended_id": recommended["id"] if recommended else None,
        }

    @staticmethod
    def _mount_source(root: Path) -> str:
        try:
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                separator = fields.index("-")
                if len(fields) > separator + 2 and Path(fields[4]).resolve() == root:
                    return fields[separator + 2]
        except (OSError, ValueError):
            pass
        return ""

    async def install_requirements(self, game_id: str, path: str) -> dict[str, object]:
        details = self.game_details(game_id)
        if details["provider_id"] != "epic":
            raise ValueError("provider.install_unsupported")
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            raise RuntimeError("epic.unavailable")
        storage = self.storage_info(path)
        sizes = await provider.install_sizes(str(details["external_game_id"]))
        return {
            **storage,
            **sizes,
            "enough_space": storage["free_bytes"] >= sizes["required_bytes"],
        }

    def install_job(self, job_id: str) -> dict[str, object]:
        job = self.jobs.get(job_id)
        return {
            "id": job.id,
            "state": job.state,
            "progress": job.progress,
            "payload": job.payload,
        }

    async def cancel_install(self, job_id: str) -> None:
        if self.epic_installs is None:
            raise RuntimeError("install.manager_unavailable")
        await self.epic_installs.cancel(job_id)

    def pause_install(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job.payload.get("simulated"):
            if job.state != JobState.PAUSED:
                self.jobs.update_progress(
                    job_id, job.progress, {"resumeState": str(JobState.DOWNLOADING_GAME)}
                )
                self.jobs.transition(job_id, JobState.PAUSED)
            return
        if self.epic_installs is None:
            raise RuntimeError("install.manager_unavailable")
        self.epic_installs.pause(job_id)

    def resume_install(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job.payload.get("simulated"):
            if job.state == JobState.PAUSED and job_id in self.simulated_updates:
                self.jobs.transition(job_id, JobState.DOWNLOADING_GAME)
            return
        if self.epic_installs is None:
            raise RuntimeError("install.manager_unavailable")
        self.epic_installs.resume(job_id)

    async def uninstall_game(self, game_id: str) -> None:
        details = self.game_details(game_id)
        if details["provider_id"] != "epic":
            raise ValueError("provider.uninstall_unsupported")
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            raise RuntimeError("epic.unavailable")
        external_game_id = str(details["external_game_id"])
        latest_job = self.jobs.latest_for_game("epic", external_game_id)
        active_job = latest_job is not None and latest_job.state not in {
            JobState.COMPLETED,
            JobState.CANCELLED,
            JobState.FAILED_RETRYABLE,
            JobState.FAILED_PERMANENT,
            JobState.BLOCKED_BY_COMPATIBILITY,
        }
        if active_job and self.epic_installs is not None:
            await self.epic_installs.stop_game(external_game_id)
        if details["installed"]:
            await provider.uninstall(external_game_id)
        elif active_job and latest_job is not None:
            install_root = latest_job.payload.get("installRoot")
            if isinstance(install_root, str):
                await asyncio.to_thread(
                    provider.remove_partial_install, external_game_id, install_root
                )

    async def cleanup_before_uninstall(self, delete_games: bool = False) -> dict[str, object]:
        """Remove GameBridge state, optionally including safely managed Epic games."""
        async with self._cleanup_lock:
            with self.database.connect() as db:
                shortcut_ids = [
                    int(row[0])
                    for row in db.execute("SELECT steam_app_id FROM steam_shortcuts").fetchall()
                ]

            retained_installations = {
                summary["id"]: self.providers.get(str(summary["id"])).retained_installations()
                for summary in self.providers.summaries()
            }
            provider = self.providers.get("epic")
            removed_games = 0
            errors: list[str] = []
            destructive_hoyoplay_paths: set[Path] = set()
            destructive_hoyoplay_games: set[Path] = set()
            if delete_games:
                for provider_id in ("mihoyo_cn", "hoyoplay_global"):
                    hoyoplay = self.providers.get(provider_id)
                    payload = retained_installations.get(provider_id)
                    if not isinstance(hoyoplay, HoYoPlayProvider) or not isinstance(payload, dict):
                        continue
                    games = self._verified_hoyoplay_game_paths(hoyoplay, payload)
                    destructive_hoyoplay_games.update(games)
                    destructive_hoyoplay_paths.update(games)
                    launcher_root = self._verified_hoyoplay_launcher_root(hoyoplay)
                    if launcher_root is not None:
                        destructive_hoyoplay_paths.add(launcher_root)
            if isinstance(provider, EpicProvider) and delete_games:
                installations = self._epic_installed_records(provider)
                epic_retained = retained_installations.get("epic")
                for external_id, install_path in installations:
                    if install_path is None or not self._is_registered_game_path(install_path):
                        continue
                    if isinstance(epic_retained, dict):
                        epic_retained.pop(external_id, None)
                    try:
                        await provider.uninstall(external_id)
                    except (RuntimeError, OSError) as exc:
                        errors.append(str(exc))
                    try:
                        await asyncio.to_thread(shutil.rmtree, install_path, True)
                        removed_games += 1
                    except OSError as exc:
                        errors.append(str(exc))
            if isinstance(provider, EpicProvider):
                try:
                    await provider.logout()
                except (RuntimeError, OSError):
                    # Removing the provider directory below also removes all local tokens.
                    pass

            try:
                await SteamBrowserAuthorization().clear_epic_session()
            except (RuntimeError, OSError) as exc:
                errors.append(str(exc))
            try:
                await SteamBrowserAuthorization().clear_steamgriddb_session()
            except (RuntimeError, OSError) as exc:
                errors.append(str(exc))

            await self.shutdown()
            if delete_games:
                for root in storage_roots():
                    managed = root.path / "Games" / "GameBridge" / "Epic"
                    if managed.is_dir():
                        await asyncio.to_thread(shutil.rmtree, managed, True)
                top_level_hoyoplay_paths = {
                    path
                    for path in destructive_hoyoplay_paths
                    if not any(path != other and path.is_relative_to(other) for other in destructive_hoyoplay_paths)
                }
                for path in sorted(top_level_hoyoplay_paths, key=lambda item: len(item.parts), reverse=True):
                    try:
                        await asyncio.to_thread(shutil.rmtree, path)
                    except OSError as exc:
                        errors.append(str(exc))
                removed_games += sum(not path.exists() for path in destructive_hoyoplay_games)

            preserved_hoyoplay_paths: set[Path] = set()
            if not delete_games:
                for provider_id in ("mihoyo_cn", "hoyoplay_global"):
                    hoyoplay = self.providers.get(provider_id)
                    if isinstance(hoyoplay, HoYoPlayProvider):
                        preserved_hoyoplay_paths.update(
                            {
                                hoyoplay.data_directory.resolve(),
                                hoyoplay.prefix_directory.resolve(),
                            }
                        )

            async def remove_unpreserved(path: Path) -> None:
                resolved = path.resolve()
                if resolved in preserved_hoyoplay_paths:
                    return
                if path.is_dir() and not path.is_symlink():
                    if any(item.is_relative_to(resolved) for item in preserved_hoyoplay_paths):
                        for nested in tuple(path.iterdir()):
                            await remove_unpreserved(nested)
                        return
                    await asyncio.to_thread(shutil.rmtree, path)
                    return
                path.unlink(missing_ok=True)

            children = (
                tuple(self.data_directory.iterdir())
                if self.data_directory.exists()
                else ()
            )
            for child in children:
                try:
                    await remove_unpreserved(child)
                except OSError as exc:
                    errors.append(str(exc))

            self.database.initialize()
            with self.database.connect() as db:
                for summary in self.providers.summaries():
                    db.execute(
                        "INSERT INTO providers(id, display_name) VALUES (?, ?)",
                        (summary["id"], summary["name"]),
                    )
            epic = self.providers.get("epic")
            if isinstance(epic, EpicProvider):
                self.epic_installs = EpicInstallManager(epic, self.jobs)
            for provider_id, payload in retained_installations.items():
                if isinstance(payload, dict):
                    self.providers.get(provider_id).restore_retained_installations(payload)
            return {
                "steamAppIds": shortcut_ids,
                "removedGames": removed_games,
                "errors": errors,
            }

    @staticmethod
    def _epic_installed_records(provider: EpicProvider) -> list[tuple[str, Path | None]]:
        installed = provider.data_directory / "config" / "legendary" / "installed.json"
        try:
            payload = json.loads(installed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        records: list[tuple[str, Path | None]] = []
        for external_id, value in payload.items():
            if not isinstance(external_id, str) or not isinstance(value, dict):
                continue
            raw_path = value.get("install_path")
            try:
                path = Path(raw_path).expanduser().resolve() if isinstance(raw_path, str) else None
            except OSError:
                path = None
            records.append((external_id, path))
        return records

    @staticmethod
    def _verified_hoyoplay_game_paths(
        provider: HoYoPlayProvider, payload: dict[str, object]
    ) -> set[Path]:
        verified: set[Path] = set()
        games = {game.external_game_id: game for game in provider.spec.games}
        for external_id, raw_path in payload.items():
            game = games.get(external_id)
            if game is None or not isinstance(raw_path, str):
                continue
            try:
                path = Path(raw_path).expanduser().resolve()
            except OSError:
                continue
            if path in {Path.home().resolve(), Path("/")} or not path.is_dir():
                continue
            if any((path / name).is_file() for name in game.executable_names):
                verified.add(path)
        return verified

    @staticmethod
    def _verified_hoyoplay_launcher_root(provider: HoYoPlayProvider) -> Path | None:
        executable = provider.launcher_executable()
        if executable is None:
            return None
        try:
            executable = executable.resolve()
            root = executable.parent
            prefix = provider.prefix_directory.resolve()
        except OSError:
            return None
        if executable.name.casefold() not in {"launcher.exe", "launcher_main.exe"}:
            return None
        if executable.is_relative_to(prefix):
            return None
        expected_names = {
            "mihoyo launcher" if provider.provider_id == "mihoyo_cn" else "hoyoplay"
        }
        if (
            root.name.casefold() not in expected_names
            or not (root / "config.ini").is_file()
            or root in {Path.home().resolve(), Path("/")}
        ):
            return None
        return root

    @staticmethod
    def _is_registered_game_path(path: Path) -> bool:
        for root in storage_roots():
            managed = (root.path / "Games" / "GameBridge" / "Epic").resolve()
            if path != managed and path.is_relative_to(managed):
                return True
        return False

    def _epic_game_metadata(self, external_game_id: str) -> dict[str, object]:
        metadata = self._epic_raw_metadata(external_game_id)
        images = metadata.get("keyImages")
        epic = self._select_epic_artwork_set(images if isinstance(images, list) else [])
        steam = self.steam_artwork.cached("epic", external_game_id) or {}
        return {
            "description": metadata.get("description")
            if isinstance(metadata.get("description"), str)
            else None,
            "developer": metadata.get("developer")
            if isinstance(metadata.get("developer"), str)
            else None,
            "artwork_url": steam.get("capsule") or epic["capsule"],
            "hero_url": steam.get("hero") or epic["hero"],
            "header_url": steam.get("header") or epic["header"],
            "logo_url": steam.get("logo") or epic["logo"],
            "artwork_source": "steam" if steam else "epic",
        }

    def _epic_raw_metadata(self, external_game_id: str) -> dict[str, object]:
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            return {}
        metadata_root = (provider.data_directory / "config" / "legendary" / "metadata").resolve()
        candidate = (metadata_root / f"{external_game_id}.json").resolve()
        if (
            candidate.parent != metadata_root
            or not candidate.is_file()
            or candidate.stat().st_size > 5_000_000
        ):
            return {}
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return {}
        return metadata

    def _epic_installation(self, external_game_id: str) -> dict[str, object]:
        provider = self.providers.get("epic")
        if not isinstance(provider, EpicProvider):
            return {}
        installed_file = provider.data_directory / "config" / "legendary" / "installed.json"
        if not installed_file.is_file() or installed_file.stat().st_size > 5_000_000:
            return {}
        try:
            payload = json.loads(installed_file.read_text(encoding="utf-8"))
            installation = payload.get(external_game_id)
        except (OSError, ValueError):
            return {}
        if not isinstance(installation, dict):
            return {}
        install_path = installation.get("install_path")
        if not isinstance(install_path, str) or not Path(install_path).is_dir():
            return {}
        executable = installation.get("executable")
        if not isinstance(executable, str) or not (Path(install_path) / executable).is_file():
            return {}
        return {
            "installed": True,
            "install_path": install_path,
            "installed_version": installation.get("version"),
            "executable": executable,
        }

    async def shutdown(self) -> None:
        simulations = list(self.simulated_updates.values())
        for task in simulations:
            task.cancel()
        if simulations:
            await asyncio.gather(*simulations, return_exceptions=True)
        if self.epic_installs is not None:
            await self.epic_installs.shutdown()

    @staticmethod
    def _select_epic_artwork(images: list[object]) -> str | None:
        return GameBridgeApplication._select_epic_artwork_set(images)["capsule"]

    @staticmethod
    def _select_epic_artwork_set(images: list[object]) -> dict[str, str | None]:
        priorities = ("DieselGameBoxTall", "OfferImageTall", "DieselGameBox", "Thumbnail")
        valid: dict[str, str] = {}
        for image in images:
            if not isinstance(image, dict):
                continue
            url = image.get("url")
            image_type = image.get("type")
            if not isinstance(url, str) or not isinstance(image_type, str):
                continue
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname == "cdn1.epicgames.com":
                valid[image_type] = url
        capsule = next((valid[item] for item in priorities if item in valid), None)
        wide_priorities = ("DieselGameBox", "OfferImageWide", "Featured", "Thumbnail")
        wide = next((valid[item] for item in wide_priorities if item in valid), None)
        logo_priorities = ("DieselGameBoxLogo", "ProductLogo", "Logo")
        logo = next((valid[item] for item in logo_priorities if item in valid), None)
        return {"capsule": capsule, "hero": wide, "header": wide, "logo": logo}
