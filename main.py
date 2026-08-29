from __future__ import annotations

import asyncio
import contextlib
import os
import sys

import decky

# Decky loads main.py through an import spec; the plugin root is not guaranteed
# to be present in sys.path on every loader release.
PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from gamebridge.application import GameBridgeApplication  # noqa: E402


class Plugin:
    app: GameBridgeApplication

    async def _main(self) -> None:
        runtime_dir = getattr(decky, "DECKY_RUNTIME_DIR", None)
        runtime_dir = runtime_dir or getattr(decky, "DECKY_PLUGIN_RUNTIME_DIR", None)
        runtime_dir = runtime_dir or os.environ.get("DECKY_PLUGIN_RUNTIME_DIR")
        if not runtime_dir:
            runtime_dir = os.path.join(decky.DECKY_USER_HOME, ".local", "share", "GameBridge")
        data_dir = os.path.join(runtime_dir, "data")
        self.app = GameBridgeApplication(data_dir)
        self._artwork_sync_task: asyncio.Task[dict[str, int]] | None = None
        self._artwork_catalog_task: asyncio.Task[None] | None = None
        self._artwork_requests: set[str | None] = set()
        self.app.start()
        decky.logger.info("GameBridge backend ready")

    async def get_dashboard(self) -> dict[str, object]:
        return await self.app.dashboard()

    async def prepare_compatibility(self) -> dict[str, object]:
        return await self.app.prepare_compatibility()

    async def prepare_default_compatibility(self) -> dict[str, object]:
        return await self.app.prepare_default_compatibility()

    async def tool_download_progress(self) -> dict[str, object]:
        return self.app.tool_download_progress()

    async def prepare_hoyoplay_game_runtime(self, game_id: str) -> dict[str, str]:
        return await self.app.prepare_hoyoplay_game_runtime(game_id)

    async def claim_steam_install_request(self, app_id: int) -> dict[str, bool]:
        return self.app.claim_steam_install_request(app_id)

    async def list_providers(self) -> list[dict[str, object]]:
        return self.app.providers.summaries()

    async def install_provider_tool(self, provider_id: str) -> dict[str, str]:
        return await self.app.install_provider_tool(provider_id)

    async def authenticate_epic(self, authorization_code: str) -> dict[str, object]:
        return await self.app.authenticate_epic(authorization_code)

    async def automatic_epic_login(self) -> dict[str, object]:
        result = await self.app.automatic_epic_login()
        self._queue_artwork_refresh("epic")
        return result

    async def refresh_provider_status(self, provider_id: str) -> dict[str, object]:
        return await self.app.refresh_provider_status(provider_id)

    async def logout_provider(self, provider_id: str) -> dict[str, object]:
        return await self.app.logout_provider(provider_id)

    async def launch_provider_client(self, provider_id: str) -> dict[str, object]:
        return await self.app.launch_provider_client(provider_id)

    async def import_provider_installer(
        self, provider_id: str, source_path: str
    ) -> dict[str, object]:
        return self.app.import_provider_installer(provider_id, source_path)

    async def run_provider_installer(self, provider_id: str) -> dict[str, object]:
        return await self.app.run_provider_installer(provider_id)

    async def download_and_run_provider_installer(
        self, provider_id: str
    ) -> dict[str, object]:
        try:
            return await self.app.download_and_run_provider_installer(provider_id)
        except Exception:
            decky.logger.exception(
                "Official launcher download/install failed for %s", provider_id
            )
            raise

    async def download_provider_installer(self, provider_id: str) -> dict[str, object]:
        try:
            return await self.app.download_provider_installer(provider_id)
        except Exception:
            decky.logger.exception("Official launcher download failed for %s", provider_id)
            raise

    async def sync_provider_library(self, provider_id: str) -> dict[str, int]:
        # Library/catalog persistence belongs to the RPC. Network artwork work
        # is queued separately so the Decky route can always return promptly.
        result = await self.app.sync_provider_library(
            provider_id, resolve_artwork=False
        )
        decky.logger.info(
            "Provider library sync provider=%s games=%d",
            provider_id,
            result["count"],
        )
        self._queue_artwork_refresh(provider_id)
        return result

    def _queue_artwork_refresh(self, provider_id: str | None = None) -> None:
        self._artwork_requests.add(provider_id)
        if self._artwork_catalog_task is None or self._artwork_catalog_task.done():
            self._artwork_catalog_task = asyncio.create_task(
                self._run_artwork_queue()
            )

    async def _run_artwork_queue(self) -> None:
        steam_userdata = os.path.join(
            decky.DECKY_USER_HOME, ".local", "share", "Steam", "userdata"
        )
        while self._artwork_requests:
            requested = self._artwork_requests.pop()
            # A whole-library request supersedes any provider requests that were
            # queued before the worker reached them.
            if requested is None:
                self._artwork_requests.clear()
            try:
                await self.app.refresh_official_artwork_catalog()
                result = await self.app.backfill_community_artwork(
                    steam_userdata, requested
                )
                decky.logger.info(
                    "Artwork backfill provider=%s processed=%d matched=%d "
                    "installed=%d failed=%d",
                    requested or "all",
                    result["processed"],
                    result["matched"],
                    result["installed"],
                    result["failed"],
                )
            except Exception:
                decky.logger.exception(
                    "Artwork backfill failed for %s", requested or "all"
                )

    async def list_games(
        self, query: str = "", offset: int = 0, limit: int = 8
    ) -> dict[str, object]:
        return self.app.list_games(query, offset, limit)

    async def game_details(self, game_id: str) -> dict[str, object]:
        return self.app.game_details(game_id)

    async def capture_hoyoplay_channel_profile(
        self, game_id: str
    ) -> dict[str, object]:
        return self.app.capture_hoyoplay_channel_profile(game_id)

    async def switch_hoyoplay_channel_profile(
        self, game_id: str, channel: str
    ) -> dict[str, object]:
        return self.app.switch_hoyoplay_channel_profile(game_id, channel)

    async def hoyoplay_channel_selection(self) -> dict[str, object]:
        return self.app.hoyoplay_channel_selection()

    async def switch_hoyoplay_channel_selection(
        self, channel: str
    ) -> dict[str, object]:
        return self.app.switch_hoyoplay_channel_selection(channel)

    async def steam_library_games(self) -> list[dict[str, object]]:
        # Library navigation must only read local state. Network artwork refreshes
        # run after the cards are returned so a slow CDN/API cannot hold the tab
        # or serialize a following game-details RPC behind it.
        games = self.app.steam_library_games()
        self._queue_artwork_refresh()
        return games

    async def play_history_default_directory(self) -> str:
        return self.app.play_history_default_directory()

    async def latest_play_history_export(self) -> str:
        return self.app.latest_play_history_export()

    async def play_history_exports(self) -> list[dict[str, object]]:
        return self.app.play_history_exports()

    async def export_play_history(self, runtime: list[dict[str, int]] | None = None) -> dict[str, object]:
        return self.app.export_play_history(runtime)

    async def import_play_history(
        self, source_path: str, runtime: list[dict[str, int]] | None = None
    ) -> dict[str, object]:
        return self.app.import_play_history(source_path, runtime)

    async def _refresh_artwork_catalog(self) -> None:
        await self.app.refresh_official_artwork_catalog()
        await self.app.ensure_community_artwork()

    async def _sync_steam_shortcut_artwork(self) -> dict[str, int]:
        steam_userdata = os.path.join(
            decky.DECKY_USER_HOME, ".local", "share", "Steam", "userdata"
        )
        result = await self.app.ensure_all_steam_shortcut_artwork(steam_userdata)
        decky.logger.info(
            "Steam shortcut artwork sync ready=%d synced=%d failed=%d",
            result["ready"], result["synced"], result["failed"],
        )
        return result

    async def refresh_steam_artwork(
        self, game_id: str, language: str | None = None
    ) -> dict[str, object]:
        return await self.app.refresh_steam_artwork(game_id, language)

    async def artwork_settings(self) -> dict[str, object]:
        return self.app.artwork_settings()

    async def save_steamgriddb_key(self, key: str) -> dict[str, object]:
        return await self.app.save_steamgriddb_key(key)

    async def test_steamgriddb_connection(self) -> dict[str, bool]:
        return await self.app.test_steamgriddb_connection()

    async def download_steamgriddb_artwork(self, url: str) -> dict[str, str]:
        steam_userdata = os.path.join(
            decky.DECKY_USER_HOME, ".local", "share", "Steam", "userdata"
        )
        return await self.app.download_steamgriddb_artwork(url, steam_userdata)

    async def install_steam_shortcut_artwork(
        self, provider_id: str, external_game_id: str, steam_app_id: int
    ) -> dict[str, int | str]:
        steam_userdata = os.path.join(
            decky.DECKY_USER_HOME, ".local", "share", "Steam", "userdata"
        )
        return await self.app.install_steam_shortcut_artwork(
            provider_id, external_game_id, steam_app_id, steam_userdata
        )

    async def refresh_all_community_artwork(self) -> dict[str, int]:
        return await self.app.refresh_all_community_artwork()

    async def register_steam_shortcut(
        self, provider_id: str, external_game_id: str, steam_app_id: int
    ) -> None:
        self.app.register_steam_shortcut(provider_id, external_game_id, steam_app_id)

    async def unregister_steam_shortcut(
        self, provider_id: str, external_game_id: str, steam_app_id: int
    ) -> None:
        self.app.unregister_steam_shortcut(provider_id, external_game_id, steam_app_id)

    async def set_runtime_language(
        self, provider_id: str, external_game_id: str, language: str
    ) -> None:
        self.app.set_runtime_language(provider_id, external_game_id, language)

    async def cloud_save_settings(self) -> dict[str, bool]:
        return self.app.cloud_save_settings()

    async def set_cloud_save_enabled(self, enabled: bool) -> dict[str, bool]:
        return self.app.set_cloud_save_enabled(enabled)

    async def cloud_save_status(
        self, provider_id: str, external_game_id: str
    ) -> dict[str, object]:
        return await self.app.cloud_save_status(provider_id, external_game_id)

    async def sync_cloud_save(
        self, provider_id: str, external_game_id: str, direction: str
    ) -> dict[str, object]:
        return await self.app.sync_cloud_save(
            provider_id, external_game_id, direction
        )

    async def repair_shortcut_launch_options(
        self, current: str, provider_id: str, external_game_id: str
    ) -> str:
        return self.app.repair_shortcut_launch_options(
            current, provider_id, external_game_id
        )

    async def shortcut_launch_preset(
        self, preset: str, provider_id: str, external_game_id: str
    ) -> str:
        return self.app.shortcut_launch_preset(preset, provider_id, external_game_id)

    async def shortcut_profile_launch_preset(
        self, preset: str, base: str, mode: str
    ) -> str:
        return self.app.shortcut_profile_launch_preset(preset, base, mode)

    async def launch_modifier_availability(self) -> dict[str, bool]:
        plugin_root = os.path.join(decky.DECKY_USER_HOME, "homebrew", "plugins")
        return self.app.launch_modifier_availability(plugin_root)

    async def steam_game_details(
        self, steam_app_id: int, title: str | None = None
    ) -> dict[str, object] | None:
        return self.app.steam_game_details(steam_app_id, title)

    async def start_game_install(
        self, game_id: str, install_path: str | None = None
    ) -> dict[str, str]:
        return self.app.start_game_install(game_id, install_path)

    async def start_game_update(self, game_id: str) -> dict[str, str]:
        return self.app.start_game_update(game_id)

    async def start_visible_update_simulation(self) -> dict[str, object]:
        return self.app.start_visible_update_simulation()

    async def storage_info(self, path: str) -> dict[str, object]:
        return self.app.storage_info(path)

    async def install_requirements(self, game_id: str, path: str) -> dict[str, object]:
        return await self.app.install_requirements(game_id, path)

    async def storage_locations(self, game_id: str) -> dict[str, object]:
        return await self.app.storage_locations(game_id)

    async def get_install_job(self, job_id: str) -> dict[str, object]:
        return self.app.install_job(job_id)

    async def cancel_install(self, job_id: str) -> None:
        await self.app.cancel_install(job_id)

    async def pause_install(self, job_id: str) -> None:
        self.app.pause_install(job_id)

    async def resume_install(self, job_id: str) -> None:
        self.app.resume_install(job_id)

    async def uninstall_game(self, game_id: str) -> None:
        await self.app.uninstall_game(game_id)

    async def cleanup_before_uninstall(self, delete_games: bool = False) -> dict[str, object]:
        return await self.app.cleanup_before_uninstall(delete_games)

    async def _unload(self) -> None:
        if self._artwork_catalog_task is not None:
            self._artwork_catalog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._artwork_catalog_task
        await self.app.shutdown()
        decky.logger.info("GameBridge backend stopped")

    async def _uninstall(self) -> None:
        # User game files, prefixes, and the database are deliberately retained.
        decky.logger.info("GameBridge uninstalled; user data retained")
