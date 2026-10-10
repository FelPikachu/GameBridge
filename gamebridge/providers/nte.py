"""NTE (Neverness to Everness) provider.

NTE is a single-title provider. The CN scope owns the wanmei-hosted PC
launcher and downloads it from the same wmupd CDN that backs
``yh.wanmei.com``. The Steam scope owns App ID ``4508340`` and never
downloads or hosts a launcher; it lets Steam drive installation and updates.

NTE does not share miHoYo's official/Bilibili channel switching, SDK
downloads, or HYP API surface. Every inherited channel entry point must
reject NTE explicitly so a misconfigured UI cannot silently run miHoYo
logic on NTE.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..models import (
    CompatibilityStatus,
    GameReference,
    JsonObject,
    ProviderCapabilities,
    RuntimeProfile,
)
from ..storage import storage_health
from .hoyoplay import HoYoPlayProvider, LauncherGameSpec, LauncherSpec

# wanmei/wmupd hosts that publish the NTE PC launcher. Captured 2026-10-10 from
# https://yh.wanmei.com/ via yh-gameDownload.js (desktop field). The CN scope
# must never reach for the miHoYo CDN; the allowlist encodes that boundary.
NTE_INSTALLER_HOSTS: tuple[str, ...] = ("yhcdn1.wmupd.com", "yhapk.wmupd.com")

# Pinned installer URL observed on 2026-10-10. The CDN reuses the same path
# across small version bumps; update both ``installer_url`` and the host
# allowlist together when the official side rolls a new build.
NTE_CN_INSTALLER_URL = (
    "https://yhcdn1.wmupd.com/clientRes/OBInstaller/"
    "YH_common_setup_1.0.4.0415_20260416.exe"
)

# NTE is a single-title provider. The CN scope owns the wanmei-hosted PC
# launcher and downloads it from the same wmupd CDN that backs
# ``yh.wanmei.com``. The international release is distributed by Steam
# under App ID 4508340, so GameBridge does not register a parallel
# ``nte_steam`` scope: Steam's own library card and download flow already
# cover that surface.
@dataclass(frozen=True, slots=True)
class _NteGameSpec(LauncherGameSpec):
    """NTE-only game spec; currently a single CN title."""


NTE_CN_GAMES = (
    _NteGameSpec(
        external_game_id="nte_cn",
        title="异环",
        executable_names=("NevernessToEverness.exe", "NTE.exe", "NTEClient.exe"),
        compatibility_status=CompatibilityStatus.EXPERIMENTAL,
    ),
)


NTE_CN = LauncherSpec(
    provider_id="nte_cn",
    display_name="provider.nte_cn",
    region="cn",
    official_page="https://yh.wanmei.com/",
    installer_url=NTE_CN_INSTALLER_URL,
    installer_hosts=NTE_INSTALLER_HOSTS,
    prefix_name="nte-cn",
    executable_candidates=(
        # Real-device evidence required to lock these paths. The launcher exe
        # name depends on what the NSIS installer drops into ``drive_c``;
        # until a Steam Deck install pins the real path, candidates are
        # placeholders that always miss and surface the
        # ``hoyoplay.launcher_missing`` error to the UI.
        "drive_c/Program Files/WanmeiGames/NTE/NTELauncher.exe",
        "drive_c/Program Files (x86)/NTE/NTELauncher.exe",
        "drive_c/Program Files/NTE/NTELauncher.exe",
        "drive_c/Program Files/WanmeiGames/NTE/launcher.exe",
        "drive_c/Program Files (x86)/NTE/launcher.exe",
    ),
    games=NTE_CN_GAMES,
)


class NteProvider(HoYoPlayProvider):
    """Single-title provider for NTE (Neverness to Everness).

    Inherits installer import/download metadata handling from
    ``HoYoPlayProvider`` so the same SHA-256 + MZ validation applies to
    the wanmei-hosted NTE launcher. Channel/profile entry points
    inherited from HoYoPlay are overridden to reject NTE explicitly;
    NTE never participates in the official/Bilibili workflow. Steam
    covers the international release under App ID 4508340 on its own,
    so GameBridge does not register a parallel nte_steam scope.
    """

    def __init__(
        self,
        data_directory: str | Path,
        compatibility_directory: str | Path,
        spec: LauncherSpec,
    ) -> None:
        super().__init__(data_directory, compatibility_directory, spec)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            account_connection=True,
            public_catalog=True,
            official_installer=bool(self.spec.installer_url),
            update=True,
            local_launch=True,
        )

    # ------------------------------------------------------------------
    # NTE has no Steam scope; Steam covers the international release
    # directly. ``launcher_executable`` and ``game_installation`` must
    # return None when the wanmei launcher has not yet been imported so
    # the application layer does not invent a launch path.

    def launcher_executable(self) -> Path | None:
        if not self.spec.executable_candidates:
            return None
        for relative in self.spec.executable_candidates:
            candidate = self.prefix_directory / relative
            if candidate.is_file():
                return candidate
        retained = self._retained_launcher_executable()
        if retained is not None:
            return retained
        registered = self._registered_launcher_executable()
        if registered is not None and registered.is_file():
            self._remember_launcher_executable(registered)
            return registered
        return None

    def discover_adjacent_installations(self) -> JsonObject:
        """Scan for already-installed game bodies beside a known launcher.

        NTE does not yet have real-device evidence for which directory the
        wanmei launcher drops its game body into. Until a Steam Deck install
        pins the layout, this returns an empty dict instead of guessing —
        the upstream layer must require an explicit launcher executable
        before trusting any "discovered" path.
        """
        launcher = self.launcher_executable()
        if launcher is None:
            return {}
        games_root = launcher.parent / "games"
        if games_root.is_symlink() or not games_root.is_dir():
            return {}
        try:
            directories = tuple(
                path
                for path in games_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
        except OSError:
            return {}
        discovered: JsonObject = {}
        for game in self.spec.games:
            matches = [
                directory
                for directory in directories
                if any(
                    (directory / executable).is_file()
                    and not (directory / executable).is_symlink()
                    for executable in game.executable_names
                )
            ]
            if len(matches) != 1:
                continue
            resolved = matches[0].resolve()
            self._remember_install_path(game.external_game_id, resolved)
            discovered[game.external_game_id] = os.fspath(resolved)
        return discovered

    async def connection_status(self) -> JsonObject:
        executable = self.launcher_executable()
        if executable is None:
            status: JsonObject = {
                "state": "not_installed",
                "message": "hoyoplay.not_installed",
                "action": (
                    "run_installer"
                    if self.spec.installer_url and self.managed_installer.is_file()
                    else "download_installer"
                    if self.spec.installer_url
                    else "open_steam"
                ),
                "officialPage": self.spec.official_page,
                "region": self.spec.region,
                "prefixPath": os.fspath(self.prefix_directory),
            }
            if self.spec.installer_url and self.installer_metadata_file.is_file():
                try:
                    metadata = json.loads(
                        self.installer_metadata_file.read_text(encoding="utf-8")
                    )
                    if isinstance(metadata, dict):
                        status["installer"] = metadata
                except (OSError, ValueError):
                    pass
            return status
        return {
            "state": "installed",
            "message": "hoyoplay.installed_login_in_client",
            "action": "launch_client",
            "officialPage": self.spec.official_page,
            "region": self.spec.region,
            "prefixPath": os.fspath(self.prefix_directory),
            "executable": os.fspath(executable),
        }

    async def library(self) -> Sequence[GameReference]:
        return tuple(
            GameReference(
                self.provider_id,
                game.external_game_id,
                game.title,
                self.spec.region,
                compatibility_status=game.compatibility_status,
            )
            for game in self.spec.games
        )

    def game_installation(
        self, external_game_id: str, *, include_channel_profile: bool = False
    ) -> JsonObject:
        game = next(
            (item for item in self.spec.games if item.external_game_id == external_game_id),
            None,
        )
        if game is None:
            raise KeyError(f"unknown {self.provider_id} game: {external_game_id}")

        launcher_installed = self.launcher_executable() is not None
        # NTE has no ``user.reg`` GameInstallPath contract. Until real-device
        # evidence proves otherwise, the provider reports launcher presence
        # only; the application layer must drive the Steam shortcut for the
        # Steam scope and the launcher itself for the CN scope.
        executable = None
        if launcher_installed:
            discovered = self.discover_adjacent_installations()
            install_path = discovered.get(external_game_id)
            if install_path is not None:
                candidate = Path(install_path)
                for name in game.executable_names:
                    body = candidate / name
                    if body.is_file():
                        executable = body
                        break
                health = storage_health(candidate)
                return {
                    "installed": executable is not None,
                    "partial": False,
                    "install_state": "installed" if executable else "partial",
                    "launchable": executable is not None,
                    "install_path": os.fspath(candidate),
                    "executable": os.fspath(executable) if executable else None,
                    "installed_version": None,
                    "official_client_installed": launcher_installed,
                    "native_steam_app_id": game.native_steam_app_id,
                    "storage_state": health.state,
                    "storage_filesystem": health.filesystem,
                }

        return {
            "installed": False,
            "partial": False,
            "install_state": "not_installed",
            "launchable": False,
            "install_path": None,
            "executable": None,
            "installed_version": None,
            "official_client_installed": launcher_installed,
            "native_steam_app_id": game.native_steam_app_id,
            "storage_state": "unknown",
            "storage_filesystem": None,
        }

    async def resolve_launch(self, game: GameReference) -> RuntimeProfile:
        if game.provider_id != self.provider_id:
            raise ValueError("provider.game_mismatch")
        executable = self.launcher_executable()
        if executable is None:
            raise FileNotFoundError("hoyoplay.launcher_missing")
        return RuntimeProfile(
            game_id=f"{self.provider_id}:{game.external_game_id}",
            prefix_path=os.fspath(self.prefix_directory),
            executable=os.fspath(executable),
            game_id_umu="umu-default",
            store="none",
            environment={"UMU_LOG": "1"},
        )

    # ------------------------------------------------------------------
    # miHoYo channel surface: explicitly rejected for NTE. NTE has no
    # official/Bilibili channel split; inherited methods must fail closed
    # so a misconfigured UI never runs miHoYo logic on NTE data.
    # ------------------------------------------------------------------

    @staticmethod
    def _reject_nte_channel() -> None:
        raise NotImplementedError(
            "nte.channel_switch_unsupported: NTE has no official/Bilibili split"
        )

    def switch_channel_selection(self, channel: str) -> JsonObject:  # noqa: ARG002
        self._reject_nte_channel()

    def apply_channel_for_launch(  # noqa: ARG002
        self, external_game_id: str, channel: str
    ) -> JsonObject:
        self._reject_nte_channel()

    def switch_channel_profile(  # noqa: ARG002
        self, external_game_id: str, channel: str
    ) -> JsonObject:
        self._reject_nte_channel()

    def apply_selected_channel(self, external_game_id: str) -> JsonObject:  # noqa: ARG002
        self._reject_nte_channel()

    def capture_channel_profile(self, external_game_id: str) -> JsonObject:  # noqa: ARG002
        self._reject_nte_channel()

    def prepare_channel_profile(  # noqa: ARG002
        self, external_game_id: str, channel: str
    ) -> JsonObject:
        self._reject_nte_channel()

    def channel_profile_status(  # noqa: ARG002
        self, external_game_id: str, directory=None  # noqa: ANN001
    ) -> JsonObject:
        self._reject_nte_channel()

    def channel_selection_status(self) -> JsonObject:
        return {"current": "official"}


__all__ = [
    "NTE_CN",
    "NteProvider",
    "NTE_CN_INSTALLER_URL",
    "NTE_INSTALLER_HOSTS",
]
