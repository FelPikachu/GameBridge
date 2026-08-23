from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CompatibilityStatus(StrEnum):
    VERIFIED = "verified"
    PLAYABLE = "playable"
    EXPERIMENTAL = "experimental"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    DEPRECATED = "deprecated"


class JobState(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    WAITING_FOR_SPACE = "waiting_for_space"
    DOWNLOADING_INSTALLER = "downloading_installer"
    VERIFYING_INSTALLER = "verifying_installer"
    PREPARING_PREFIX = "preparing_prefix"
    INSTALLING_LAUNCHER = "installing_launcher"
    WAITING_FOR_LOGIN = "waiting_for_login"
    DOWNLOADING_GAME = "downloading_game"
    VERIFYING_GAME = "verifying_game"
    CONFIGURING_RUNTIME = "configuring_runtime"
    CREATING_SHORTCUT = "creating_shortcut"
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"
    BLOCKED_BY_COMPATIBILITY = "blocked_by_compatibility"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    account_connection: bool = False
    owned_library: bool = False
    public_catalog: bool = False
    official_installer: bool = False
    direct_download: bool = False
    update: bool = False
    repair: bool = False
    uninstall: bool = False
    local_launch: bool = False
    cloud_launch: bool = False
    cloud_save: bool = False
    achievements: bool = False
    dlc: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GameReference:
    provider_id: str
    external_game_id: str
    title: str
    region: str = "global"
    release_channel: str = "stable"
    compatibility_status: CompatibilityStatus = CompatibilityStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    game_id: str
    prefix_path: str
    executable: str
    runtime_engine: str = "umu"
    runtime_version: str = "UMU-Proton"
    game_id_umu: str = "umu-default"
    store: str = "none"
    launch_arguments: tuple[str, ...] = field(default_factory=tuple)
    environment: dict[str, str] = field(default_factory=dict)


JsonObject = dict[str, Any]
