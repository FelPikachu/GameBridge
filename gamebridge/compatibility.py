from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from .storage import steam_library_paths
from .tooling import DWPROTON_RELEASE, GE_PROTON_RELEASE, ToolInstaller

UMU_DATABASE_URL = "https://umu.openwinecomponents.org/umu_api.php?store=egs"
SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class CompatibilityManager:
    def __init__(
        self,
        data_directory: str | Path,
        installer: ToolInstaller | None = None,
    ) -> None:
        self.data_directory = Path(data_directory)
        self.tools_directory = self.data_directory / "tools"
        self.cache_file = self.data_directory / "cache" / "umu-egs.json"
        self.preferences_file = self.data_directory / "runtime-preferences.json"
        self.installer = installer or ToolInstaller()

    @property
    def umu_executable(self) -> Path:
        return self.tools_directory / "umu-run"

    @property
    def default_runtime(self) -> Path:
        return (
            Path.home()
            / ".local/share/Steam/compatibilitytools.d"
            / GE_PROTON_RELEASE.version
        )

    def default_runtime_ready(self) -> bool:
        proton = self.default_runtime / "proton"
        return proton.is_file() and os.access(proton, os.X_OK)

    def status(self) -> dict[str, Any]:
        layers = self.proton_layers()
        return {
            "ready": self.umu_executable.is_file() and self.default_runtime_ready(),
            "umuInstalled": self.umu_executable.is_file(),
            "umuPath": os.fspath(self.umu_executable) if self.umu_executable.is_file() else None,
            "protonLayers": [
                {"name": name, "path": os.fspath(path), "recommended": index == 0}
                for index, (name, path) in enumerate(layers)
            ],
            "preparation": self.installer.progress_status(),
        }

    def prepare_base(self) -> dict[str, Any]:
        """Install only the small shared runner; game runtimes stay on-demand."""
        self.installer.set_progress(
            active=True,
            component="umu",
            phase="starting",
            progress=0.01,
            source=None,
            downloadedBytes=0,
            totalBytes=None,
        )
        try:
            if not self.umu_executable.is_file():
                self.installer.install_umu(self.umu_executable)
            else:
                self.installer.set_progress(
                    active=False, component="umu", phase="complete", progress=1.0
                )
            return self.status()
        except BaseException:
            self.installer.set_progress(active=False, phase="failed")
            raise

    def prepare(self) -> dict[str, Any]:
        self.prepare_base()
        self.installer.set_progress(
            active=True,
            component=GE_PROTON_RELEASE.component,
            phase="checking_runtime",
            progress=0.14,
            source=None,
        )
        if not self.default_runtime_ready():
            self.installer.install_compatibility_tool(
                GE_PROTON_RELEASE, self.default_runtime.parent
            )
        else:
            self.installer.set_progress(
                active=False,
                component=GE_PROTON_RELEASE.component,
                phase="complete",
                progress=1.0,
            )
        try:
            self.refresh_database()
        except (OSError, ValueError, RuntimeError):
            # The database only improves per-game identity. UMU's documented
            # umu-default fallback remains launchable while offline.
            pass
        return self.status()

    def ensure_hoyoplay_runtime(self, game_id: str) -> dict[str, str]:
        release = (
            GE_PROTON_RELEASE
            if game_id in {"bh3_cn", "bh3_global"}
            else DWPROTON_RELEASE
        )
        root = Path.home() / ".local/share/Steam/compatibilitytools.d"
        target = root / release.version
        self.installer.set_progress(
            active=True,
            component=release.component,
            phase="checking_runtime",
            progress=0.14,
            source=None,
            downloadedBytes=0,
            totalBytes=None,
        )
        if (target / "proton").is_file():
            self.installer.set_progress(
                active=False,
                component=release.component,
                phase="complete",
                progress=1.0,
            )
            return {"version": release.version, "path": os.fspath(target)}
        return self.installer.install_compatibility_tool(release, root)

    def refresh_database(self) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            UMU_DATABASE_URL, headers={"User-Agent": "GameBridge/0.18"}
        )
        with urllib.request.urlopen(  # noqa: S310 -- fixed HTTPS endpoint
            request, timeout=20, context=self.installer._ssl_context()
        ) as response:
            payload = json.load(response)
        if not isinstance(payload, list):
            raise RuntimeError("UMU database returned an unexpected response")
        clean = [entry for entry in payload if isinstance(entry, dict)]
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.cache_file)
        return clean

    def umu_id(self, epic_codename: str, title: str = "") -> str:
        entries = self._database()
        codename = epic_codename.casefold()
        normalized_title = " ".join(title.casefold().split())
        for entry in entries:
            candidate = entry.get("codename")
            if isinstance(candidate, str) and candidate.casefold() == codename:
                value = entry.get("umu_id")
                if isinstance(value, str) and value.startswith("umu-"):
                    return value
        if normalized_title:
            for entry in entries:
                candidate = entry.get("title")
                if (
                    isinstance(candidate, str)
                    and " ".join(candidate.casefold().split()) == normalized_title
                ):
                    value = entry.get("umu_id")
                    if isinstance(value, str) and value.startswith("umu-"):
                        return value
        return "umu-default"

    def prefix(self, provider: str, game_id: str) -> Path:
        safe = SAFE_ID.sub("_", game_id).strip("._") or "game"
        return self.data_directory / "prefixes" / provider / safe

    def selected_proton(
        self, provider: str, game_id: str, steam_app_id: int | None = None
    ) -> tuple[str, Path | str]:
        steam_override = self.steam_compatibility_override(steam_app_id)
        if steam_override:
            resolved = self._resolve_proton_tool(steam_override)
            if resolved is not None:
                return resolved
        preference = self._preferences().get(f"{provider}:{game_id}")
        layers = self.proton_layers()
        if isinstance(preference, str):
            for name, path in layers:
                if preference in {name, os.fspath(path)}:
                    return name, path
        if layers:
            return layers[0]
        return "UMU-Proton", "UMU-Proton"

    def steam_compatibility_override(self, steam_app_id: int | None) -> str | None:
        """Return Steam's per-shortcut Force Compatibility tool, if configured."""
        if not steam_app_id:
            return None
        # Shortcut ids are occasionally exposed as signed 32-bit values by a
        # Steam API while config.vdf stores the unsigned representation.
        app_ids = {str(steam_app_id), str(steam_app_id % (2**32))}
        config = Path.home() / ".local/share/Steam/config/config.vdf"
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        mapping = self._vdf_block(text, "CompatToolMapping")
        if mapping is None:
            return None
        for app_id in app_ids:
            block = self._vdf_block(mapping, app_id)
            if block is None:
                continue
            match = re.search(r'"name"\s+"([^"]+)"', block, flags=re.IGNORECASE)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return None

    def _resolve_proton_tool(self, tool_name: str) -> tuple[str, Path] | None:
        def canonical(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", value.casefold())

        wanted = canonical(tool_name)
        for name, path in self.proton_layers():
            candidates = {
                canonical(name),
                canonical(path.name),
            }
            if wanted in candidates:
                return name, path
        return None

    @staticmethod
    def _vdf_block(text: str, key: str) -> str | None:
        match = re.search(rf'"{re.escape(key)}"\s*\{{', text, flags=re.IGNORECASE)
        if not match:
            return None
        start = match.end()
        depth = 1
        quoted = False
        escaped = False
        for index in range(start, len(text)):
            character = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == '"':
                quoted = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index]
        return None

    def set_selected_proton(self, provider: str, game_id: str, value: str | None) -> None:
        preferences = self._preferences()
        key = f"{provider}:{game_id}"
        if value:
            preferences[key] = value
        else:
            preferences.pop(key, None)
        self.preferences_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.preferences_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(preferences, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.preferences_file)

    def proton_layers(self) -> list[tuple[str, Path]]:
        home = Path.home()
        roots = {
            home / ".local/share/Steam/compatibilitytools.d",
            home / ".steam/root/compatibilitytools.d",
            home / ".local/share/Steam/steamapps/common",
            *(library / "steamapps/common" for library in steam_library_paths(home)),
        }
        found: dict[str, Path] = {}
        for root in roots:
            if not root.is_dir():
                continue
            for proton in root.glob("*/proton"):
                if proton.is_file() and os.access(proton, os.X_OK):
                    found.setdefault(proton.parent.name, proton.parent.resolve())

        def rank(item: tuple[str, Path]) -> tuple[int, str]:
            name = item[0].casefold()
            if name == GE_PROTON_RELEASE.version.casefold():
                return (0, name)
            if "ge-proton" in name:
                return (1, name)
            if "experimental" in name:
                return (2, name)
            return (3, name)

        return sorted(found.items(), key=rank)

    def _database(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, ValueError):
            return []

    def _preferences(self) -> dict[str, str]:
        try:
            payload = json.loads(self.preferences_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}
