from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

_MIHOYO_API = "https://hyp-api.mihoyo.com/hyp/hyp-connect/api/getAllGameBasicInfo"
_MIHOYO_LAUNCHER_ID = "jGHBHlcOq1"
_MIHOYO_IMAGE_HOST = "launcher-webstatic.mihoyo.com"
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

# TapTap-hosted app icon for NTE (Neverness to Everness), captured 2026-10-10
# from the public TapTap CDN. Used as a hardcoded artwork source because NTE
# does not publish launcher-advertised artwork via the miHoYo HYP API and
# SteamGridDB community artwork for the wanmei launcher is not stable.
_NTE_CN_TAPTAP_ICON = (
    "https://img-tc.tapimg.com/market/images/"
    "ec0bd50c4a0bafa60cde8d0f3775ee84.png/_tap_appicon.jpg"
)
_NTE_CN_ARTWORK: dict[str, str] = {
    "capsule": _NTE_CN_TAPTAP_ICON,
    "hero": _NTE_CN_TAPTAP_ICON,
    "header": _NTE_CN_TAPTAP_ICON,
    "logo": _NTE_CN_TAPTAP_ICON,
    "icon": _NTE_CN_TAPTAP_ICON,
}


class OfficialLauncherArtworkResolver:
    """Resolve public artwork advertised by an official launcher endpoint."""

    def __init__(self, cache_file: str | Path) -> None:
        self.cache_file = Path(cache_file)
        self._cache_lock = Lock()

    def cached(self, provider_id: str, external_game_id: str) -> dict[str, str] | None:
        # NTE has a stable TapTap-hosted icon. Return it directly so the
        # Steam library and detail views always have a fallback before they
        # touch the on-disk cache or the network. Only the single canonical
        # ``nte_cn`` title is hardcoded; any other external_game_id falls
        # through to the persistent cache to avoid silently reusing the icon.
        if provider_id == "nte_cn" and external_game_id == "nte_cn":
            return dict(_NTE_CN_ARTWORK)
        value = self._read_cache().get(f"{provider_id}:{external_game_id}")
        if not isinstance(value, dict) or not self._valid_image_url(value.get("capsule")):
            return None
        return {key: item for key, item in value.items() if isinstance(item, str)}

    def resolve(self, provider_id: str, external_game_id: str) -> dict[str, str] | None:
        cached = self.cached(provider_id, external_game_id)
        if cached is not None:
            return cached
        if provider_id != "mihoyo_cn":
            return None
        entries = self._mihoyo_catalog()
        if entries:
            with self._cache_lock:
                cache = self._read_cache()
                cache.update(entries)
                self._write_cache(cache)
        return self.cached(provider_id, external_game_id)

    def _mihoyo_catalog(self) -> dict[str, dict[str, str]]:
        url = _MIHOYO_API + "?" + urllib.parse.urlencode(
            {"launcher_id": _MIHOYO_LAUNCHER_ID, "language": "zh-cn"}
        )
        payload = self._json(url)
        data = payload.get("data") if isinstance(payload, dict) and payload.get("retcode") == 0 else None
        games = data.get("game_info_list") if isinstance(data, dict) else None
        if not isinstance(games, list):
            return {}
        result: dict[str, dict[str, str]] = {}
        for item in games:
            if not isinstance(item, dict):
                continue
            game = item.get("game")
            backgrounds = item.get("backgrounds")
            biz = game.get("biz") if isinstance(game, dict) else None
            if not isinstance(biz, str) or not biz.endswith("_cn") or not isinstance(backgrounds, list):
                continue
            artwork = next(
                (entry for entry in backgrounds if isinstance(entry, dict)), None
            )
            background = artwork.get("background") if isinstance(artwork, dict) else None
            icon = artwork.get("icon") if isinstance(artwork, dict) else None
            image_url = background.get("url") if isinstance(background, dict) else None
            logo_url = icon.get("url") if isinstance(icon, dict) else None
            if not self._valid_image_url(image_url):
                continue
            value = {"capsule": image_url, "hero": image_url, "header": image_url}
            if self._valid_image_url(logo_url):
                value["logo"] = logo_url
            result[f"mihoyo_cn:{biz}"] = value
        return result

    @staticmethod
    def _valid_image_url(value: object) -> bool:
        if not isinstance(value, str) or len(value) > 2048:
            return False
        parsed = urllib.parse.urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == _MIHOYO_IMAGE_HOST
            and parsed.path.casefold().endswith(_IMAGE_SUFFIXES)
            and not parsed.username
            and not parsed.password
        )

    @staticmethod
    def _json(url: str) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": "GameBridge/0.18"})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                return json.load(response)
        except (OSError, ValueError):
            return None

    def _read_cache(self) -> dict[str, object]:
        try:
            value = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self, value: dict[str, object]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.cache_file)
