from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
_DETAIL_URL = "https://store.steampowered.com/api/appdetails"
_CDN_ROOT = "https://cdn.cloudflare.steamstatic.com/steam/apps"
_PCGW_API = "https://www.pcgamingwiki.com/w/api.php"


class SteamArtworkResolver:
    """Best-effort, conservative Epic-title to Steam-AppID resolver.

    This is an optional artwork enhancement. Successful results are cached, so
    opening the library never performs network access.
    """

    def __init__(self, cache_file: str | Path) -> None:
        self.cache_file = Path(cache_file)
        self._cache_lock = Lock()

    def cached(self, provider_id: str, external_game_id: str) -> dict[str, str | int] | None:
        value = self._read_cache().get(f"{provider_id}:{external_game_id}")
        if not isinstance(value, dict) or not isinstance(value.get("app_id"), int):
            return None
        return value

    def resolve(
        self, provider_id: str, external_game_id: str, title: str, developer: str | None
    ) -> dict[str, str | int] | None:
        key = f"{provider_id}:{external_game_id}"
        cache = self._read_cache()
        if key in cache:
            value = cache[key]
            return value if isinstance(value, dict) and value.get("app_id") else None

        match = self._find_match(title, developer)
        # Other games may resolve concurrently. Merge with the newest on-disk
        # cache so one completed request cannot discard another result.
        if match:
            with self._cache_lock:
                cache = self._read_cache()
                cache[key] = match
                self._write_cache(cache)
        return match

    def _find_match(self, title: str, developer: str | None) -> dict[str, str | int] | None:
        search_title = self._search_title(title)
        search = self._json(
            _SEARCH_URL
            + "?"
            + urllib.parse.urlencode({"term": search_title, "l": "english", "cc": "US"})
        )
        items = search.get("items") if isinstance(search, dict) else None
        if not isinstance(items, list):
            items = []
        exact = [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and self._normalize(str(item.get("name", ""))) == self._normalize(title)
        ]
        if len(exact) == 1:
            app_id = int(exact[0]["id"])
        elif not exact:
            app_id = self._pcgamingwiki_app_id(search_title)
            if app_id is None:
                return None
        else:
            return None
        if developer and not self._developer_matches(app_id, developer):
            return None
        candidates = {
            "capsule": f"{_CDN_ROOT}/{app_id}/library_600x900_2x.jpg",
            "hero": f"{_CDN_ROOT}/{app_id}/library_hero.jpg",
            "header": f"{_CDN_ROOT}/{app_id}/header.jpg",
            "logo": f"{_CDN_ROOT}/{app_id}/logo.png",
        }
        available = {name: url for name, url in candidates.items() if self._exists(url)}
        return {"app_id": app_id, **available}

    def _pcgamingwiki_app_id(self, title: str) -> int | None:
        search = self._json(
            _PCGW_API
            + "?"
            + urllib.parse.urlencode(
                {"action": "query", "list": "search", "srsearch": title, "format": "json"}
            )
        )
        query = search.get("query") if isinstance(search, dict) else None
        results = query.get("search") if isinstance(query, dict) else None
        if not isinstance(results, list):
            return None
        exact = [
            item
            for item in results
            if isinstance(item, dict)
            and isinstance(item.get("pageid"), int)
            and self._normalize(str(item.get("title", ""))) == self._normalize(title)
        ]
        if len(exact) != 1:
            return None
        cargo = self._json(
            _PCGW_API
            + "?"
            + urllib.parse.urlencode(
                {
                    "action": "cargoquery",
                    "format": "json",
                    "tables": "Infobox_game",
                    "fields": "Steam_AppID",
                    "where": f"Infobox_game._pageID={exact[0]['pageid']}",
                }
            )
        )
        rows = cargo.get("cargoquery") if isinstance(cargo, dict) else None
        if not isinstance(rows, list) or len(rows) != 1:
            return None
        row_title = rows[0].get("title") if isinstance(rows[0], dict) else None
        raw_app_id = row_title.get("Steam AppID") if isinstance(row_title, dict) else None
        try:
            app_id = int(raw_app_id)
        except (TypeError, ValueError):
            return None
        return app_id if app_id > 0 else None

    def _developer_matches(self, app_id: int, expected: str) -> bool:
        payload = self._json(
            _DETAIL_URL + "?" + urllib.parse.urlencode({"appids": app_id, "l": "english"})
        )
        entry = payload.get(str(app_id)) if isinstance(payload, dict) else None
        data = entry.get("data") if isinstance(entry, dict) and entry.get("success") else None
        companies: list[object] = []
        if isinstance(data, dict):
            for field in ("developers", "publishers"):
                values = data.get(field)
                if isinstance(values, list):
                    companies.extend(values)
        if not companies:
            return False
        wanted = self._normalize_company(expected)
        return any(
            wanted == self._normalize_company(str(candidate))
            or wanted in self._normalize_company(str(candidate))
            or self._normalize_company(str(candidate)) in wanted
            for candidate in companies
            if candidate
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", SteamArtworkResolver._search_title(value).casefold())

    @staticmethod
    def _search_title(value: str) -> str:
        return " ".join(value.translate(str.maketrans("", "", "®™©℠")).split())

    @staticmethod
    def _normalize_company(value: str) -> str:
        value = re.sub(
            r"\b(inc|llc|ltd|limited|corp|corporation|games|game|studios?)\b",
            "",
            value.casefold(),
        )
        return re.sub(r"[^a-z0-9]+", "", value)

    @staticmethod
    def _json(url: str) -> Any:
        request = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": "GameBridge/0.16"}
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:  # noqa: S310
                return json.load(response)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _exists(url: str) -> bool:
        request = urllib.request.Request(  # noqa: S310
            url, method="HEAD", headers={"User-Agent": "GameBridge/0.16"}
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
                return response.status == 200 and int(response.headers.get("Content-Length", 1)) > 0
        except (OSError, ValueError):
            return False

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
