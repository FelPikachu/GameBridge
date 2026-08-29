from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import ssl
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from ..models import (
    CompatibilityStatus,
    GameReference,
    JsonObject,
    ProviderCapabilities,
    RuntimeProfile,
)
from ..provider import GameProvider
from ..storage import storage_health, storage_roots

SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)

_REGISTRY_SECTION = re.compile(
    r"(?ms)^\[(?P<header>[^\n]+)\]\s*(?P<body>.*?)(?=^\[|\Z)"
)
_REGISTRY_VALUE = re.compile(r'^"(?P<name>[^"]+)"="(?P<value>(?:\\\\.|[^"])*)"$', re.MULTILINE)
_WINE_DRIVE_PATH = re.compile(r"^(?P<drive>[A-Za-z]):\\(?P<path>.*)$")
_CHANNEL_VALUE = re.compile(r"(?mi)^channel[ \t]*=[ \t]*([^\r\n]+)\r?$")
_CHANNEL_COMPONENTS = (
    "PCGameSDK.dll",
    "sdk_pkg_version",
    "license.txt",
    "lfailedlog.db",
    "deletefiles.txt",
)
_CHANNEL_NAMES = {"1": "official", "14": "bilibili"}
_CHANNEL_SETTINGS = {
    "official": {"channel": "1", "sub_channel": "1", "cps": "hyp_mihoyo"},
    "bilibili": {"channel": "14", "sub_channel": "0", "cps": "hyp_mihoyo"},
}
_CHANNEL_API_HOST = "hyp-api.mihoyo.com"
_CHANNEL_ARCHIVE_HOSTS = {"launcher-webstatic.mihoyo.com"}
_CHANNEL_SDK_MAX_SIZE = 512 * 1024 * 1024
_BILIBILI_GAME_API = {
    "hk4e_cn": ("T2S0Gz4Dr2", "umfgRO5gh5"),
    "hkrpg_cn": ("EdtUqXfCHh", "6P5gHMNyK3"),
    "nap_cn": ("HXAFlmYa17", "xV0f4r1GT0"),
}
_OFFICIAL_GAME_API = {
    "hk4e_cn": "1Z8W5NHUQb",
    "hkrpg_cn": "64kMb5iAWu",
    "nap_cn": "x6znKlJ0xK",
}


@dataclass(frozen=True, slots=True)
class LauncherSpec:
    provider_id: str
    display_name: str
    region: str
    official_page: str
    installer_url: str
    installer_hosts: tuple[str, ...]
    prefix_name: str
    executable_candidates: tuple[str, ...]
    games: tuple["LauncherGameSpec", ...]


@dataclass(frozen=True, slots=True)
class LauncherGameSpec:
    external_game_id: str
    title: str
    executable_names: tuple[str, ...]
    compatibility_status: CompatibilityStatus
    native_steam_app_id: int | None = None


MIHOYO_CN_GAMES = (
    LauncherGameSpec(
        "hk4e_cn", "原神", ("YuanShen.exe",), CompatibilityStatus.EXPERIMENTAL
    ),
    LauncherGameSpec(
        "nap_cn",
        "绝区零",
        ("ZenlessZoneZero.exe",),
        CompatibilityStatus.EXPERIMENTAL,
        4162040,
    ),
    LauncherGameSpec(
        "hkrpg_cn",
        "崩坏：星穹铁道",
        ("StarRail.exe",),
        CompatibilityStatus.EXPERIMENTAL,
    ),
    LauncherGameSpec(
        "bh3_cn", "崩坏3", ("BH3.exe",), CompatibilityStatus.EXPERIMENTAL
    ),
)

HOYOPLAY_GLOBAL_GAMES = (
    LauncherGameSpec(
        "hk4e_global",
        "Genshin Impact",
        ("GenshinImpact.exe",),
        CompatibilityStatus.EXPERIMENTAL,
    ),
    LauncherGameSpec(
        "nap_global",
        "Zenless Zone Zero",
        ("ZenlessZoneZero.exe",),
        CompatibilityStatus.EXPERIMENTAL,
        4162040,
    ),
    LauncherGameSpec(
        "hkrpg_global",
        "Honkai: Star Rail",
        ("StarRail.exe",),
        CompatibilityStatus.EXPERIMENTAL,
    ),
    LauncherGameSpec(
        "bh3_global",
        "Honkai Impact 3rd",
        ("BH3.exe",),
        CompatibilityStatus.EXPERIMENTAL,
    ),
)


MIHOYO_CN = LauncherSpec(
    provider_id="mihoyo_cn",
    display_name="provider.mihoyo_cn",
    region="cn",
    # The unified launcher landing page does not render reliably in Steam's
    # embedded browser. This official PC game page exposes the same miHoYo
    # launcher download through a Steam-compatible page.
    official_page="https://sr.mihoyo.com/ad",
    installer_url="https://hyp-webstatic.mihoyo.com/hyp-client/hyp_cn_setup_1.1.4.exe",
    installer_hosts=("hyp-webstatic.mihoyo.com",),
    prefix_name="mihoyo-cn",
    executable_candidates=(
        "drive_c/Program Files/miHoYo Launcher/launcher.exe",
        "drive_c/Program Files/miHoYo Launcher/launcher_main.exe",
        "drive_c/Program Files (x86)/miHoYo Launcher/launcher.exe",
    ),
    games=MIHOYO_CN_GAMES,
)

HOYOPLAY_GLOBAL = LauncherSpec(
    provider_id="hoyoplay_global",
    display_name="provider.hoyoplay_global",
    region="global",
    official_page="https://genshin.hoyoverse.com/en/",
    installer_url="https://hyp-webstatic.hoyoverse.com/hyp-client/hyp_global_setup_1.0.5.exe",
    installer_hosts=("hyp-webstatic.hoyoverse.com",),
    prefix_name="hoyoplay-global",
    executable_candidates=(
        "drive_c/Program Files/HoYoPlay/launcher.exe",
        "drive_c/Program Files/HoYoPlay/launcher_main.exe",
        "drive_c/Program Files (x86)/HoYoPlay/launcher.exe",
    ),
    games=HOYOPLAY_GLOBAL_GAMES,
)


class HoYoPlayProvider(GameProvider):
    """Official-launcher host shared by the isolated CN and global providers."""

    def __init__(
        self,
        data_directory: str | Path,
        compatibility_directory: str | Path,
        spec: LauncherSpec,
    ) -> None:
        self.data_directory = Path(data_directory)
        self.compatibility_directory = Path(compatibility_directory)
        self.spec = spec
        self.provider_id = spec.provider_id
        self.display_name = spec.display_name

    @property
    def prefix_directory(self) -> Path:
        return self.compatibility_directory / self.spec.prefix_name

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            account_connection=True,
            public_catalog=True,
            official_installer=True,
            update=True,
            local_launch=True,
        )

    def launcher_executable(self) -> Path | None:
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
        """Remember unregistered games found beside the verified official launcher."""
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

    def prepare_launcher_game_association(self) -> JsonObject:
        """Make restored CN games acceptable to a freshly installed official launcher."""
        discovered = self.discover_adjacent_installations()
        repaired: list[str] = []
        if self.provider_id == "mihoyo_cn":
            for external_game_id in _BILIBILI_GAME_API:
                try:
                    directory = self._installed_game_directory(external_game_id)
                    config, current = self._read_channel_config(directory)
                    if current == "bilibili":
                        # Keep a complete Bilibili profile before presenting the
                        # same game body to the official launcher for association.
                        self.switch_channel_profile(external_game_id, "official")
                        repaired.append(external_game_id)
                    elif self._has_bilibili_residue(directory, config):
                        # Some older builds restored channel=1 but left the Bilibili
                        # SDK behind. Reconstruct its Bilibili profile first, so the
                        # cleanup below is reversible and later switching still works.
                        backup = (
                            self.data_directory
                            / "association-recovery"
                            / external_game_id
                            / "config.ini.before-recovery"
                        )
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if not backup.exists():
                            backup.write_bytes(config)
                        staged = directory / ".config.ini.gamebridge-association"
                        staged.write_bytes(
                            self._normalize_channel_config(config, "bilibili")
                        )
                        os.replace(staged, directory / "config.ini")
                        try:
                            self.switch_channel_profile(external_game_id, "official")
                        except (OSError, ValueError, RuntimeError):
                            restore = directory / ".config.ini.gamebridge-restore"
                            restore.write_bytes(config)
                            os.replace(restore, directory / "config.ini")
                            raise
                        finally:
                            staged.unlink(missing_ok=True)
                        repaired.append(external_game_id)
                except (OSError, ValueError):
                    continue
        return {"discovered": discovered, "repaired": repaired}

    def _has_bilibili_residue(self, directory: Path, config: bytes) -> bool:
        try:
            text = config.decode("utf-8", errors="strict")
        except UnicodeError:
            return False
        if re.search(r"(?mi)^sdk_version[ \t]*=", text):
            return True
        return any(
            "blplatform64" in {part.casefold() for part in source.relative_to(directory).parts}
            for source in self._channel_component_files(directory)
        )

    @property
    def retained_launcher_path(self) -> Path:
        return self.data_directory / "retained-launcher-path"

    def _retained_launcher_executable(self) -> Path | None:
        try:
            raw = self.retained_launcher_path.read_text(encoding="utf-8").strip()
            executable = Path(raw).expanduser().resolve()
        except OSError:
            return None
        return executable if executable.is_file() else None

    def _remember_launcher_executable(self, executable: Path) -> None:
        resolved = executable.expanduser().resolve()
        try:
            current = self.retained_launcher_path.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current == os.fspath(resolved):
            return
        staged = self.retained_launcher_path.with_suffix(".new")
        try:
            self.retained_launcher_path.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text(os.fspath(resolved), encoding="utf-8")
            os.replace(staged, self.retained_launcher_path)
        except OSError:
            return
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass

    def _registered_launcher_executable(self, *, allow_missing: bool = False) -> Path | None:
        """Resolve the official install record through this prefix's drive map."""
        for registry_name in ("system.reg", "user.reg"):
            registry = self.prefix_directory / registry_name
            try:
                contents = registry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for _header, values in self._registry_sections(contents):
                if not values.get("GameBiz", "").casefold().startswith("hyp_"):
                    continue
                if values.get("ExeName", "").casefold() != "launcher.exe":
                    continue
                install_path = values.get("InstallPath")
                if install_path is None:
                    continue
                executable = self._resolve_wine_path(
                    install_path.rstrip("\\") + "\\launcher.exe",
                    allow_missing=allow_missing,
                )
                if executable is not None:
                    return executable
        return None

    def _registered_launcher_storage_unavailable(self) -> bool:
        for registry_name in ("system.reg", "user.reg"):
            registry = self.prefix_directory / registry_name
            try:
                contents = registry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for _header, values in self._registry_sections(contents):
                if not values.get("GameBiz", "").casefold().startswith("hyp_"):
                    continue
                if values.get("ExeName", "").casefold() != "launcher.exe":
                    continue
                install_path = values.get("InstallPath")
                if install_path is not None:
                    return self._wine_storage_unavailable(install_path)
        return False

    @staticmethod
    def _registry_sections(contents: str) -> Sequence[tuple[str, dict[str, str]]]:
        return tuple(
            (
                match.group("header"),
                {
                    item.group("name"): item.group("value").replace("\\\\", "\\")
                    for item in _REGISTRY_VALUE.finditer(match.group("body"))
                },
            )
            for match in _REGISTRY_SECTION.finditer(contents)
        )

    def _wine_path_parts(self, value: str) -> tuple[str, tuple[str, ...]] | None:
        match = _WINE_DRIVE_PATH.fullmatch(value)
        if match is None:
            return None
        relative_parts = tuple(part for part in match.group("path").split("\\") if part)
        if not relative_parts or any(part in {".", ".."} for part in relative_parts):
            return None
        return match.group("drive").lower(), relative_parts

    @staticmethod
    def _candidate_rank(candidate: Path, required_names: tuple[str, ...]) -> int:
        if candidate.is_file():
            return 3
        if not candidate.is_dir():
            return 0
        if required_names and any((candidate / name).is_file() for name in required_names):
            return 3
        if any(
            (candidate / marker).exists()
            for marker in ("chunk", "staging", "pkg_version", "config.ini")
        ):
            return 2
        return 1

    def _resolve_wine_path(
        self,
        value: str,
        *,
        required_names: tuple[str, ...] = (),
        allow_missing: bool = False,
    ) -> Path | None:
        parsed = self._wine_path_parts(value)
        if parsed is None:
            return None
        drive_name, relative_parts = parsed
        drive = self.prefix_directory / "dosdevices" / f"{drive_name}:"
        missing_candidate: Path | None = None
        try:
            drive_root = drive.resolve(strict=False)
            candidate = drive_root.joinpath(*relative_parts)
            missing_candidate = candidate
            resolved = candidate.resolve(strict=True)
        except OSError:
            resolved = None
        if resolved is not None and resolved != drive_root and resolved.is_relative_to(drive_root):
            if self._candidate_rank(resolved, required_names):
                return resolved

        # Removable-drive letters are not stable across a reinstall or another
        # machine. Reapply the registry's relative path to every mounted root.
        if drive_name not in {"c", "z"}:
            ranked: list[tuple[int, Path]] = []
            for root in storage_roots():
                if root.internal:
                    continue
                try:
                    root_path = root.path.resolve(strict=True)
                    external = root_path.joinpath(*relative_parts).resolve(strict=True)
                except OSError:
                    continue
                if external == root_path or not external.is_relative_to(root_path):
                    continue
                rank = self._candidate_rank(external, required_names)
                if rank:
                    ranked.append((rank, external))
            if ranked:
                best = max(rank for rank, _candidate in ranked)
                matches = sorted(
                    {candidate for rank, candidate in ranked if rank == best},
                    key=os.fspath,
                )
                if len(matches) == 1:
                    return matches[0]
        return missing_candidate if allow_missing else None

    def _wine_storage_unavailable(self, value: str) -> bool:
        parsed = self._wine_path_parts(value)
        if parsed is None:
            return False
        drive_name, _relative_parts = parsed
        drive = self.prefix_directory / "dosdevices" / f"{drive_name}:"
        if not drive.is_symlink():
            return False
        try:
            return not drive.resolve(strict=False).is_dir()
        except OSError:
            return True

    @property
    def managed_installer(self) -> Path:
        return self.data_directory / "installers" / "official-launcher-installer.exe"

    @property
    def installer_metadata_file(self) -> Path:
        return self.data_directory / "installers" / "metadata.json"

    def import_installer(self, source: str | Path) -> JsonObject:
        source_path = Path(source).expanduser().resolve()
        if source_path.suffix.casefold() != ".exe" or not source_path.is_file():
            raise ValueError("hoyoplay.invalid_installer")
        size = source_path.stat().st_size
        if size < 1024 or size > 500 * 1024 * 1024:
            raise ValueError("hoyoplay.invalid_installer_size")
        with source_path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("hoyoplay.invalid_installer")
            digest = hashlib.sha256()
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)

        target = self.managed_installer
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_suffix(".new")
        try:
            with source_path.open("rb") as source_stream, staged.open("wb") as output:
                shutil.copyfileobj(source_stream, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)

        metadata: JsonObject = {
            "providerId": self.provider_id,
            "sourcePage": self.spec.official_page,
            "sourceFilename": source_path.name,
            "importedAt": datetime.now(UTC).isoformat(),
            "size": size,
            "sha256": digest.hexdigest(),
            "verification": "user_selected_official_download_unverified_signature",
        }
        temporary_metadata = self.installer_metadata_file.with_suffix(".tmp")
        temporary_metadata.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary_metadata, self.installer_metadata_file)
        return {**metadata, "path": os.fspath(target)}

    def _validate_installer_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.spec.installer_hosts:
            raise ValueError("hoyoplay.untrusted_installer_url")

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        for candidate in SYSTEM_CA_FILES:
            if Path(candidate).is_file():
                return ssl.create_default_context(cafile=candidate)
        raise RuntimeError("hoyoplay.system_ca_missing")

    def download_installer(self, opener: object | None = None) -> JsonObject:
        """Download an installer only from the provider's pinned official host."""
        provider = self

        class OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                provider._validate_installer_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        self._validate_installer_url(self.spec.installer_url)
        client = opener or urllib.request.build_opener(
            OfficialRedirectHandler(),
            urllib.request.HTTPSHandler(context=self._ssl_context()),
        )
        request = urllib.request.Request(
            self.spec.installer_url,
            headers={"User-Agent": "GameBridge/0.18"},
        )
        target = self.managed_installer
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_suffix(".download")
        digest = hashlib.sha256()
        size = 0
        final_url = self.spec.installer_url
        try:
            with client.open(request, timeout=60) as response, staged.open("wb") as output:
                final_url = response.geturl()
                self._validate_installer_url(final_url)
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > 500 * 1024 * 1024:
                    raise ValueError("hoyoplay.invalid_installer_size")
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 500 * 1024 * 1024:
                        raise ValueError("hoyoplay.invalid_installer_size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size < 1024:
                raise ValueError("hoyoplay.invalid_installer_size")
            with staged.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise ValueError("hoyoplay.invalid_installer")
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)

        metadata: JsonObject = {
            "providerId": self.provider_id,
            "sourcePage": self.spec.official_page,
            "sourceUrl": self.spec.installer_url,
            "finalUrl": final_url,
            "downloadedAt": datetime.now(UTC).isoformat(),
            "size": size,
            "sha256": digest.hexdigest(),
            "verification": "downloaded_from_official_domain_unverified_signature",
        }
        temporary_metadata = self.installer_metadata_file.with_suffix(".tmp")
        temporary_metadata.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary_metadata, self.installer_metadata_file)
        return {**metadata, "path": os.fspath(target)}

    async def connection_status(self) -> JsonObject:
        executable = self.launcher_executable()
        if executable is None:
            unavailable = self._registered_launcher_executable(allow_missing=True)
            if unavailable is not None and self._registered_launcher_storage_unavailable():
                return {
                    "state": "storage_unavailable",
                    "message": "hoyoplay.launcher_storage_unavailable",
                    "action": "wait_for_storage",
                    "officialPage": self.spec.official_page,
                    "region": self.spec.region,
                    "prefixPath": os.fspath(self.prefix_directory),
                    "executable": os.fspath(unavailable),
                    "storageState": "unavailable",
                }
            status: JsonObject = {
                "state": "not_installed",
                "message": "hoyoplay.not_installed",
                "action": (
                    "run_installer"
                    if self.managed_installer.is_file()
                    else "download_installer"
                ),
                "officialPage": self.spec.official_page,
                "region": self.spec.region,
                "prefixPath": os.fspath(self.prefix_directory),
            }
            if self.installer_metadata_file.is_file():
                try:
                    metadata = json.loads(
                        self.installer_metadata_file.read_text(encoding="utf-8")
                    )
                    if isinstance(metadata, dict):
                        status["installer"] = metadata
                except (OSError, ValueError):
                    pass
            return status
        status: JsonObject = {
            "state": "installed",
            "message": "hoyoplay.installed_login_in_client",
            "action": "launch_client",
            "officialPage": self.spec.official_page,
            "region": self.spec.region,
            "prefixPath": os.fspath(self.prefix_directory),
            "executable": os.fspath(executable),
        }
        unavailable_games = [
            game.title
            for game in self.spec.games
            if self.game_installation(
                game.external_game_id, include_channel_profile=False
            ).get("storage_state") == "unavailable"
        ]
        if unavailable_games:
            status.update(
                {
                    "message": "hoyoplay.game_storage_unavailable",
                    "storageState": "unavailable",
                    "unavailableGames": unavailable_games,
                }
            )
        return status

    async def library(self) -> Sequence[GameReference]:
        # This is a public product catalog, not an ownership or account claim.
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
        self, external_game_id: str, *, include_channel_profile: bool = True
    ) -> JsonObject:
        game = next(
            (item for item in self.spec.games if item.external_game_id == external_game_id),
            None,
        )
        if game is None:
            raise KeyError(f"unknown {self.provider_id} game: {external_game_id}")
        install_path: Path | None = None
        storage_unavailable = False
        raw_path = self.game_registry_install_path(external_game_id)
        if raw_path:
            install_path = self._resolve_wine_path(
                raw_path, required_names=game.executable_names
            )
        retained = self._retained_install_path(external_game_id)
        if install_path is None and retained is not None and retained.exists():
            install_path = retained
        if install_path is None and raw_path and self._wine_storage_unavailable(raw_path):
            install_path = self._resolve_wine_path(raw_path, allow_missing=True)
            storage_unavailable = install_path is not None
        if install_path is None and retained is not None:
            install_path = retained
            storage_unavailable = self._external_storage_unavailable(retained)

        executable = None
        partial = False
        if install_path is not None and install_path.is_dir():
            for name in game.executable_names:
                candidate = install_path / name
                if candidate.is_file():
                    executable = candidate
                    break
            partial = executable is None and any(
                (install_path / marker).exists()
                for marker in ("chunk", "staging", "pkg_version", "config.ini")
            )
        installed = executable is not None
        if installed and install_path is not None:
            self._remember_install_path(external_game_id, install_path)
        official_client_installed = self.launcher_executable() is not None
        health = storage_health(install_path) if install_path is not None else None
        result: JsonObject = {
            "installed": installed,
            "partial": partial,
            "install_state": (
                "installed"
                if installed
                else "partial"
                if partial
                else "storage_unavailable"
                if storage_unavailable
                else "not_installed"
            ),
            "launchable": bool(
                installed
                and official_client_installed
                and game.compatibility_status == CompatibilityStatus.EXPERIMENTAL
            ),
            "install_path": (
                os.fspath(install_path)
                if (installed or partial or storage_unavailable) and install_path
                else None
            ),
            "executable": os.fspath(executable) if executable else None,
            "installed_version": self._installed_game_version(install_path) if installed else None,
            "official_client_installed": official_client_installed,
            "native_steam_app_id": game.native_steam_app_id,
            "storage_state": (
                "unavailable"
                if storage_unavailable
                else health.state
                if health is not None
                else "unknown"
            ),
            "storage_filesystem": health.filesystem if health is not None else None,
        }
        if (
            include_channel_profile
            and self.provider_id == "mihoyo_cn"
        ):
            result["channel_profile"] = self.channel_profile_status(
                external_game_id, install_path if installed else None
            )
        return result

    @property
    def retained_installations_path(self) -> Path:
        return self.data_directory / "retained-installations.json"

    def _retained_install_path(self, external_game_id: str) -> Path | None:
        try:
            payload = json.loads(self.retained_installations_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        raw_path = payload.get(external_game_id) if isinstance(payload, dict) else None
        if not isinstance(raw_path, str):
            return None
        try:
            path = Path(raw_path).expanduser().resolve(strict=False)
        except OSError:
            return None
        return path

    def _remember_install_path(self, external_game_id: str, install_path: Path) -> None:
        try:
            payload = json.loads(self.retained_installations_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        resolved = os.fspath(install_path.expanduser().resolve())
        if payload.get(external_game_id) == resolved:
            return
        payload[external_game_id] = resolved
        try:
            self.restore_retained_installations(payload)
        except OSError:
            return

    @staticmethod
    def _external_storage_unavailable(path: Path) -> bool:
        parts = path.parts
        mount: Path | None = None
        if len(parts) >= 5 and parts[:3] == ("/", "run", "media"):
            mount = Path(*parts[:5])
        elif len(parts) >= 4 and parts[:2] == ("/", "media"):
            mount = Path(*parts[:4])
        elif len(parts) >= 3 and parts[:2] == ("/", "mnt"):
            mount = Path(*parts[:3])
        return mount is not None and not mount.is_dir()

    def retained_installations(self) -> JsonObject:
        retained: JsonObject = {}
        for game in self.spec.games:
            installation = self.game_installation(
                game.external_game_id, include_channel_profile=False
            )
            raw_path = installation.get("install_path")
            if installation.get("installed") and isinstance(raw_path, str):
                retained[game.external_game_id] = raw_path
        return retained

    def restore_retained_installations(self, payload: JsonObject) -> None:
        if not payload:
            return
        self.retained_installations_path.parent.mkdir(parents=True, exist_ok=True)
        staged = self.retained_installations_path.with_suffix(".json.new")
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staged, self.retained_installations_path)

    def game_registry_install_path(
        self, external_game_id: str, prefix_directory: Path | None = None
    ) -> str | None:
        """Read one non-secret HYP game path from a specific Wine prefix."""
        registry = (prefix_directory or self.prefix_directory) / "user.reg"
        try:
            contents = registry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        section_suffix = f"\\\\{external_game_id}".casefold()
        for header, values in self._registry_sections(contents):
            section_name = header.split("]", 1)[0].casefold()
            if (
                values.get("GameBiz", "").casefold() != external_game_id.casefold()
                and not section_name.endswith(section_suffix)
            ):
                continue
            return values.get("GameInstallPath")
        return None

    @property
    def channel_profiles_directory(self) -> Path:
        return self.data_directory / "channel-profiles"

    @property
    def channel_selection_path(self) -> Path:
        return self.channel_profiles_directory / "selected"

    def selected_channel(self) -> str:
        """Return the single official/Bilibili choice shared by the CN catalog."""
        try:
            selected = self.channel_selection_path.read_text(encoding="utf-8").strip()
        except OSError:
            legacy = {
                self._selected_channel(game_id)
                for game_id in _BILIBILI_GAME_API
                if (self.channel_profiles_directory / game_id / "selected").is_file()
            }
            selected = legacy.pop() if len(legacy) == 1 else "official"
        return selected if selected in _CHANNEL_SETTINGS else "official"

    def _write_channel_selection(self, channel: str) -> None:
        if channel not in _CHANNEL_SETTINGS:
            raise ValueError("hoyoplay.unsupported_channel")
        self.channel_selection_path.parent.mkdir(parents=True, exist_ok=True)
        staged = self.channel_selection_path.with_suffix(".new")
        staged.write_text(channel, encoding="utf-8")
        os.replace(staged, self.channel_selection_path)

    def channel_selection_status(self) -> JsonObject:
        return {"current": self.selected_channel()}

    def switch_channel_selection(self, channel: str) -> JsonObject:
        if self.provider_id != "mihoyo_cn" or channel not in _CHANNEL_SETTINGS:
            raise ValueError("hoyoplay.unsupported_channel")
        self._write_channel_selection(channel)
        return self.channel_selection_status()

    def apply_channel_for_launch(
        self, external_game_id: str, channel: str
    ) -> JsonObject:
        """Prepare one selected CN game and persist the choice only after success."""
        if external_game_id not in _BILIBILI_GAME_API or channel not in _CHANNEL_SETTINGS:
            raise ValueError("hoyoplay.unsupported_channel")
        self.switch_channel_profile(external_game_id, channel)
        self._write_channel_selection(channel)
        return self.channel_profile_status(external_game_id)

    def _installed_game_directory(self, external_game_id: str) -> Path:
        installation = self.game_installation(
            external_game_id, include_channel_profile=False
        )
        raw_path = installation.get("install_path")
        if not installation.get("installed") or not isinstance(raw_path, str):
            raise ValueError("hoyoplay.game_not_installed")
        directory = Path(raw_path)
        if not directory.is_dir():
            raise ValueError("hoyoplay.game_not_installed")
        return directory

    @staticmethod
    def _read_channel_config(directory: Path) -> tuple[bytes, str]:
        config = directory / "config.ini"
        try:
            if config.is_symlink() or not config.is_file() or config.stat().st_size > 64 * 1024:
                raise ValueError("hoyoplay.invalid_channel_config")
            contents = config.read_bytes()
            text = contents.decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as error:
            raise ValueError("hoyoplay.invalid_channel_config") from error
        match = _CHANNEL_VALUE.search(text)
        channel = _CHANNEL_NAMES.get(match.group(1).strip() if match else "")
        if channel is None:
            raise ValueError("hoyoplay.unsupported_channel")
        return contents, channel

    def _profile_directory(self, external_game_id: str, channel: str) -> Path:
        if channel not in _CHANNEL_NAMES.values():
            raise ValueError("hoyoplay.unsupported_channel")
        if external_game_id not in {game.external_game_id for game in self.spec.games}:
            raise KeyError(f"unknown {self.provider_id} game: {external_game_id}")
        return self.channel_profiles_directory / external_game_id / channel

    def _selected_channel(self, external_game_id: str) -> str:
        preference = self.channel_profiles_directory / external_game_id / "selected"
        try:
            selected = preference.read_text(encoding="utf-8").strip()
        except OSError:
            try:
                directory = self._installed_game_directory(external_game_id)
                _contents, selected = self._read_channel_config(directory)
            except (OSError, ValueError):
                selected = "official"
        return selected if selected in _CHANNEL_SETTINGS else "official"

    def _write_selected_channel(self, external_game_id: str, channel: str) -> None:
        if channel not in _CHANNEL_SETTINGS:
            raise ValueError("hoyoplay.unsupported_channel")
        preference = self.channel_profiles_directory / external_game_id / "selected"
        preference.parent.mkdir(parents=True, exist_ok=True)
        staged = preference.with_suffix(".new")
        staged.write_text(channel, encoding="utf-8")
        os.replace(staged, preference)

    def channel_profile_status(
        self, external_game_id: str, directory: Path | None = None
    ) -> JsonObject:
        selected = (
            self.selected_channel()
            if external_game_id in _BILIBILI_GAME_API
            else self._selected_channel(external_game_id)
        )
        preference_exists = (
            self.channel_profiles_directory / external_game_id / "selected"
        ).is_file()
        if directory is None:
            try:
                directory = self._installed_game_directory(external_game_id)
            except ValueError:
                directory = None
        actual = "unknown"
        if directory is not None and external_game_id != "bh3_cn":
            try:
                _contents, actual = self._read_channel_config(directory)
            except ValueError:
                pass
        current = selected if external_game_id in _BILIBILI_GAME_API else (
            selected if preference_exists or actual == "unknown" else actual
        )
        return {
            "current": current,
            "official_ready": external_game_id == "bh3_cn" or (
                self._profile_directory(external_game_id, "official") / "config.ini"
            ).is_file(),
            "bilibili_ready": external_game_id == "bh3_cn" or (
                self._profile_directory(external_game_id, "bilibili") / "config.ini"
            ).is_file(),
            "mode": "qr" if external_game_id == "bh3_cn" else "sdk",
        }

    @staticmethod
    def _normalize_channel_config(contents: bytes, channel: str) -> bytes:
        try:
            text = contents.decode("utf-8")
        except UnicodeError as error:
            raise ValueError("hoyoplay.invalid_channel_config") from error
        for key, value in _CHANNEL_SETTINGS[channel].items():
            pattern = re.compile(rf"(?mi)^{re.escape(key)}[ \t]*=[^\r\n]*")
            if pattern.search(text):
                text = pattern.sub(f"{key}={value}", text)
            else:
                if text and not text.endswith(("\n", "\r")):
                    text += "\n"
                text += f"{key}={value}\n"
        return text.encode("utf-8")

    @classmethod
    def _rewrite_channel_config(
        cls, contents: bytes, channel: str, sdk_version: str
    ) -> bytes:
        text = cls._normalize_channel_config(contents, channel).decode("utf-8")
        pattern = re.compile(r"(?mi)^sdk_version[ \t]*=[^\r\n]*")
        if pattern.search(text):
            text = pattern.sub(f"sdk_version={sdk_version}", text)
        else:
            if text and not text.endswith(("\n", "\r")):
                text += "\n"
            text += f"sdk_version={sdk_version}\n"
        return text.encode("utf-8")

    @staticmethod
    def _channel_sdk_metadata(external_game_id: str, channel: str) -> dict[str, object]:
        if channel == "bilibili":
            game_id, launcher_id = _BILIBILI_GAME_API[external_game_id]
        else:
            game_id = _OFFICIAL_GAME_API[external_game_id]
            launcher_id = "jGHBHlcOq1"
        query = urllib.parse.urlencode(
            {
                "launcher_id": launcher_id,
                "language": "zh-cn",
                "game_ids[]": game_id,
                "channel": _CHANNEL_SETTINGS[channel]["channel"],
                "sub_channel": _CHANNEL_SETTINGS[channel]["sub_channel"],
            }
        )
        url = f"https://{_CHANNEL_API_HOST}/hyp/hyp-connect/api/getGameChannelSDKs?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "GameBridge/HoYoPlay"})
        with urllib.request.urlopen(request, timeout=30, context=HoYoPlayProvider._ssl_context()) as response:
            payload = json.load(response)
        if payload.get("retcode") != 0:
            raise RuntimeError("hoyoplay.channel_sdk_api_failed")
        entries = payload.get("data", {}).get("game_channel_sdks", [])
        if len(entries) != 1 or entries[0].get("game", {}).get("biz") != external_game_id:
            raise RuntimeError("hoyoplay.channel_sdk_api_invalid")
        return entries[0]

    def prepare_channel_profile(self, external_game_id: str, channel: str) -> JsonObject:
        if self.provider_id != "mihoyo_cn":
            raise ValueError("hoyoplay.channel_switch_cn_only")
        if external_game_id == "bh3_cn":
            if channel not in _CHANNEL_SETTINGS:
                raise ValueError("hoyoplay.unsupported_channel")
            return self.channel_profile_status(external_game_id)
        if external_game_id not in _BILIBILI_GAME_API or channel not in _CHANNEL_SETTINGS:
            raise ValueError("hoyoplay.unsupported_channel")
        directory = self._installed_game_directory(external_game_id)
        current_config, _current = self._read_channel_config(directory)
        if channel == "official":
            self._prepare_default_official_profile(
                external_game_id, directory, current_config
            )
            return self.channel_profile_status(external_game_id)
        metadata = self._channel_sdk_metadata(external_game_id, channel)
        package = metadata.get("channel_sdk_pkg", {})
        url = str(package.get("url", ""))
        parsed = urlparse(url)
        size = int(package.get("size", 0))
        md5 = str(package.get("md5", "")).casefold()
        version = str(metadata.get("version", ""))
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _CHANNEL_ARCHIVE_HOSTS
            or not re.fullmatch(r"[0-9a-f]{32}", md5)
            or not version
            or size < 1
            or size > _CHANNEL_SDK_MAX_SIZE
        ):
            raise ValueError("hoyoplay.invalid_channel_sdk")
        profile = self._profile_directory(external_game_id, channel)
        profile.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=profile.parent) as temporary:
            archive_path = Path(temporary) / "channel-sdk.zip"
            request = urllib.request.Request(url, headers={"User-Agent": "GameBridge/HoYoPlay"})
            digest = hashlib.md5(usedforsecurity=False)
            received = 0
            with urllib.request.urlopen(request, timeout=90, context=self._ssl_context()) as response, archive_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > size or received > _CHANNEL_SDK_MAX_SIZE:
                        raise ValueError("hoyoplay.invalid_channel_sdk_size")
                    digest.update(chunk)
                    output.write(chunk)
            if received != size or digest.hexdigest().casefold() != md5:
                raise ValueError("hoyoplay.invalid_channel_sdk_digest")
            extracted = Path(temporary) / "components"
            extracted.mkdir()
            names: list[str] = []
            with zipfile.ZipFile(archive_path) as archive:
                total = 0
                for member in archive.infolist():
                    path = Path(member.filename.replace("\\", "/"))
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("hoyoplay.invalid_channel_sdk_archive")
                    total += member.file_size
                    if total > _CHANNEL_SDK_MAX_SIZE:
                        raise ValueError("hoyoplay.invalid_channel_sdk_size")
                    if not member.is_dir():
                        names.append(path.as_posix())
                archive.extractall(extracted)
            staged = profile.with_name(profile.name + ".new")
            if staged.exists():
                shutil.rmtree(staged)
            staged.mkdir()
            shutil.move(os.fspath(extracted), os.fspath(staged / "components"))
            (staged / "config.ini").write_bytes(
                self._rewrite_channel_config(current_config, channel, version)
            )
            (staged / "manifest.json").write_text(
                json.dumps({"channel": channel, "components": names, "version": version}, ensure_ascii=False),
                encoding="utf-8",
            )
            if profile.exists():
                shutil.rmtree(profile)
            os.replace(staged, profile)
        return self.channel_profile_status(external_game_id)

    @staticmethod
    def _channel_component_files(directory: Path) -> list[Path]:
        """Find channel-specific files, including Bilibili's nested platform bundle."""
        found: dict[str, Path] = {}
        for name in _CHANNEL_COMPONENTS:
            candidates = [directory / name]
            candidates.extend(directory.glob(f"*_Data/Plugins/**/{name}"))
            for source in candidates:
                if source.is_file() and not source.is_symlink():
                    found[source.relative_to(directory).as_posix()] = source
        for platform in directory.glob("*_Data/Plugins/**/BLPlatform64"):
            if not platform.is_dir() or platform.is_symlink():
                continue
            for source in platform.rglob("*"):
                if source.is_file() and not source.is_symlink():
                    found[source.relative_to(directory).as_posix()] = source
        return [found[name] for name in sorted(found)]

    def _prepare_default_official_profile(
        self, external_game_id: str, directory: Path, current_config: bytes
    ) -> None:
        """Build the default CN profile locally; it has no downloadable channel SDK."""
        text = self._normalize_channel_config(current_config, "official").decode("utf-8")
        text = re.sub(
            r"(?mi)^sdk_version[ \t]*=[^\r\n]*(?:\r?\n|$)", "", text
        )
        profile = self._profile_directory(external_game_id, "official")
        staged = profile.with_name(profile.name + ".new")
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        (staged / "config.ini").write_bytes(text.encode("utf-8"))
        components = staged / "components"
        components.mkdir()
        retained: list[str] = []
        # The official Genshin snapshot includes a channel-neutral licence file.
        # Preserve it when it exists; Bilibili SDK binaries are deliberately omitted.
        for source in self._channel_component_files(directory):
            if source.name.casefold() != "license.txt":
                continue
            relative = source.relative_to(directory)
            destination = components / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            retained.append(relative.as_posix())
        (staged / "manifest.json").write_text(
            json.dumps(
                {"channel": "official", "components": retained},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if profile.exists():
            shutil.rmtree(profile)
        os.replace(staged, profile)

    def capture_channel_profile(self, external_game_id: str) -> JsonObject:
        if self.provider_id != "mihoyo_cn":
            raise ValueError("hoyoplay.channel_switch_cn_only")
        directory = self._installed_game_directory(external_game_id)
        config, channel = self._read_channel_config(directory)
        self._write_selected_channel(external_game_id, channel)
        profile = self._profile_directory(external_game_id, channel)
        profile.parent.mkdir(parents=True, exist_ok=True)
        staged_profile = profile.with_name(profile.name + ".new")
        if staged_profile.exists():
            shutil.rmtree(staged_profile)
        staged_profile.mkdir()
        (staged_profile / "config.ini").write_bytes(config)
        captured: list[str] = []
        components = staged_profile / "components"
        components.mkdir()
        for source in self._channel_component_files(directory):
            relative = source.relative_to(directory)
            destination = components / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            captured.append(relative.as_posix())
        (staged_profile / "manifest.json").write_text(
            json.dumps({"channel": channel, "components": captured}, ensure_ascii=False),
            encoding="utf-8",
        )
        previous_profile = profile.with_name(profile.name + ".old")
        if previous_profile.exists():
            shutil.rmtree(previous_profile)
        if profile.exists():
            os.replace(profile, previous_profile)
        os.replace(staged_profile, profile)
        if previous_profile.exists():
            shutil.rmtree(previous_profile)
        return self.channel_profile_status(external_game_id)

    def switch_channel_profile(self, external_game_id: str, channel: str) -> JsonObject:
        if self.provider_id != "mihoyo_cn":
            raise ValueError("hoyoplay.channel_switch_cn_only")
        if external_game_id == "bh3_cn":
            self._write_selected_channel(external_game_id, channel)
            return self.channel_profile_status(external_game_id)
        if external_game_id not in _BILIBILI_GAME_API or channel not in _CHANNEL_SETTINGS:
            raise ValueError("hoyoplay.unsupported_channel")
        try:
            directory = self._installed_game_directory(external_game_id)
        except ValueError:
            self._write_selected_channel(external_game_id, channel)
            return self.channel_profile_status(external_game_id)
        game = next(item for item in self.spec.games if item.external_game_id == external_game_id)
        executable_names = {name.casefold() for name in game.executable_names}
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if (process / "exe").resolve(strict=True).name.casefold() in executable_names:
                    raise ValueError("hoyoplay.game_running")
            except (FileNotFoundError, PermissionError, OSError):
                continue
        _current_config, current = self._read_channel_config(directory)
        if current == channel:
            normalized = self._normalize_channel_config(_current_config, channel)
            if normalized != _current_config:
                staged = directory / ".config.ini.gamebridge-new"
                staged.write_bytes(normalized)
                os.replace(staged, directory / "config.ini")
                profile_config = self._profile_directory(
                    external_game_id, channel
                ) / "config.ini"
                if profile_config.is_file() and not profile_config.is_symlink():
                    staged_profile = profile_config.with_suffix(".ini.new")
                    staged_profile.write_bytes(normalized)
                    os.replace(staged_profile, profile_config)
            self._write_selected_channel(external_game_id, channel)
            return self.channel_profile_status(external_game_id)
        current_profile = self._profile_directory(external_game_id, current)
        if not (current_profile / "config.ini").is_file() or not (
            current_profile / "manifest.json"
        ).is_file():
            # The official API does not consistently publish an official SDK
            # package. Preserve the installed, known-working channel before the
            # first transition so returning never depends on that API.
            self.capture_channel_profile(external_game_id)
        target = self._profile_directory(external_game_id, channel)
        target_config = target / "config.ini"
        if not target_config.is_file() or target_config.is_symlink():
            self.prepare_channel_profile(external_game_id, channel)
        target_contents = target_config.read_bytes()
        normalized_target = self._normalize_channel_config(target_contents, channel)
        if normalized_target != target_contents:
            staged_target = target_config.with_suffix(".ini.new")
            staged_target.write_bytes(normalized_target)
            os.replace(staged_target, target_config)
        current_manifest_path = self._profile_directory(external_game_id, current) / "manifest.json"
        try:
            current_manifest = json.loads(current_manifest_path.read_text(encoding="utf-8"))
            current_components = current_manifest.get("components", [])
        except (OSError, ValueError, TypeError):
            current_components = []
        manifest_path = target / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            component_names = manifest.get("components", [])
        except (OSError, ValueError, TypeError):
            component_names = []
        if not isinstance(component_names, list) or any(not isinstance(name, str) for name in component_names):
            raise ValueError("hoyoplay.invalid_channel_profile")
        staged = directory / ".config.ini.gamebridge-new"
        shutil.copyfile(target_config, staged)
        os.replace(staged, directory / "config.ini")
        all_names = set(component_names)
        if isinstance(current_components, list):
            all_names.update(name for name in current_components if isinstance(name, str))
        for name in sorted(all_names):
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("hoyoplay.invalid_channel_profile")
            source = target / "components" / name
            destination = directory / name
            if source.is_file() and not source.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged_component = destination.with_name(f".{destination.name}.gamebridge-new")
                shutil.copyfile(source, staged_component)
                os.replace(staged_component, destination)
            elif (
                name in current_components
                and destination.is_file()
                and not destination.is_symlink()
            ):
                destination.unlink()
        self._write_selected_channel(external_game_id, channel)
        return self.channel_profile_status(external_game_id)

    def apply_selected_channel(self, external_game_id: str) -> JsonObject:
        """Apply the persisted user choice immediately before a managed launch."""
        if external_game_id not in _BILIBILI_GAME_API:
            return self.channel_profile_status(external_game_id)
        return self.apply_channel_for_launch(external_game_id, self.selected_channel())

    @property
    def chunk_config_database(self) -> Path:
        return (
            self.prefix_directory
            / "drive_c/users/steamuser/AppData/Roaming/miHoYo/HYP/1_1"
            / "modules/sophon/sophon_db/chunk_config.db"
        )

    def repair_completed_install_metadata(self, external_game_id: str) -> bool:
        """Fill an empty game version only when Sophon proves the build is complete."""
        game = next(
            (item for item in self.spec.games if item.external_game_id == external_game_id),
            None,
        )
        if game is None:
            raise KeyError(f"unknown {self.provider_id} game: {external_game_id}")
        registry = self.prefix_directory / "user.reg"
        try:
            contents = registry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        raw_path = None
        section_suffix = f"\\{external_game_id}".casefold()
        for header, values in self._registry_sections(contents):
            if (
                values.get("GameBiz", "").casefold() == external_game_id.casefold()
                or header.split("]", 1)[0].casefold().endswith(section_suffix)
            ):
                raw_path = values.get("GameInstallPath")
                break
        if not raw_path:
            return False
        install_path = self._resolve_wine_path(raw_path)
        if install_path is None or not any(
            (install_path / name).is_file() for name in game.executable_names
        ):
            return False
        config = install_path / "config.ini"
        try:
            if config.stat().st_size > 64 * 1024:
                return False
            config_text = config.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return False
        match = re.search(r"(?mi)^game_version[ \t]*=[ \t]*([^\r\n]*)$", config_text)
        if match is None or match.group(1).strip():
            return False
        try:
            database_uri = f"file:{self.chunk_config_database}?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as connection:
                rows = connection.execute(
                    "SELECT local_version, server_version, local_build_id, server_build_id "
                    "FROM config WHERE lower(install_dir)=lower(?) AND matching_field='game'",
                    (raw_path,),
                ).fetchall()
        except sqlite3.Error:
            return False
        versions = {
            str(local_version)
            for local_version, server_version, local_build, server_build in rows
            if local_version
            and local_version == server_version
            and local_build
            and local_build == server_build
        }
        if len(versions) != 1:
            return False
        version = versions.pop()
        repaired = config_text[: match.start(1)] + version + config_text[match.end(1) :]
        backup = self.data_directory / "repairs" / f"{external_game_id}-config.ini.backup"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copyfile(config, backup)
        staged = config.with_suffix(".ini.gamebridge-new")
        try:
            staged.write_text(repaired, encoding="utf-8")
            os.replace(staged, config)
        finally:
            staged.unlink(missing_ok=True)
        return True

    def partial_installations(self) -> tuple[JsonObject, ...]:
        return tuple(
            installation
            for game in self.spec.games
            if (installation := self.game_installation(game.external_game_id)).get("partial")
        )

    def storage_blocker(self) -> JsonObject | None:
        launcher = self.launcher_executable()
        if launcher is not None:
            health = storage_health(launcher)
            if health.state != "writable":
                return {
                    "state": health.state,
                    "path": os.fspath(launcher),
                    "filesystem": health.filesystem,
                }
        for installation in self.partial_installations():
            if installation.get("storage_state") != "writable":
                return installation
        return None

    @staticmethod
    def _installed_game_version(install_path: Path | None) -> str | None:
        if install_path is None:
            return None
        config = install_path / "config.ini"
        try:
            if config.stat().st_size > 64 * 1024:
                return None
            contents = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        match = re.search(r"(?mi)^game_version[ \t]*=[ \t]*([^\r\n]+)$", contents)
        return match.group(1).strip() if match else None

    async def resolve_launch(self, game: GameReference) -> RuntimeProfile:
        if game.provider_id != self.provider_id:
            raise ValueError("provider.game_mismatch")
        executable = self.launcher_executable()
        profile_game_id = "launcher"
        if game.external_game_id != "launcher":
            installation = self.game_installation(game.external_game_id)
            game_executable = installation.get("executable")
            if installation.get("launchable") and isinstance(game_executable, str):
                candidate = Path(game_executable)
                if candidate.is_file():
                    executable = candidate
                    profile_game_id = game.external_game_id
        if executable is None:
            raise FileNotFoundError("hoyoplay.launcher_missing")
        return RuntimeProfile(
            game_id=f"{self.provider_id}:{profile_game_id}",
            prefix_path=os.fspath(self.prefix_directory),
            executable=os.fspath(executable),
            game_id_umu="umu-default",
            store="none",
            environment={"UMU_LOG": "1"},
        )
