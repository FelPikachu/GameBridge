from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import RuntimeProfile

ALLOWED_ENVIRONMENT = {
    "DXVK_ASYNC", "DXVK_CONFIG_FILE", "MANGOHUD", "PROTON_ENABLE_NVAPI",
    "PROTON_HIDE_NVIDIA_GPU", "PROTON_LOG", "PROTON_NO_ESYNC", "PROTON_NO_FSYNC",
    "PROTON_USE_WINED3D", "UMU_LOG", "WINEDLLOVERRIDES",
}


@dataclass(frozen=True, slots=True)
class LaunchCommand:
    argv: tuple[str, ...]
    environment: dict[str, str]


class UmuRuntime:
    def __init__(self, executable: str = "umu-run") -> None:
        self.executable = executable

    def build(self, profile: RuntimeProfile) -> LaunchCommand:
        prefix = Path(profile.prefix_path).expanduser().resolve()
        executable = Path(profile.executable).expanduser().resolve()
        if not executable.is_file():
            raise FileNotFoundError(executable)
        if prefix == Path("/"):
            raise ValueError("root cannot be used as a Wine prefix")
        rejected = set(profile.environment) - ALLOWED_ENVIRONMENT
        if rejected:
            raise ValueError(f"unsupported environment keys: {sorted(rejected)}")
        environment = {
            "WINEPREFIX": os.fspath(prefix),
            "GAMEID": profile.game_id_umu,
            "STORE": profile.store,
            "PROTONPATH": profile.runtime_version,
            **profile.environment,
        }
        return LaunchCommand(
            (self.executable, os.fspath(executable), *profile.launch_arguments), environment
        )

