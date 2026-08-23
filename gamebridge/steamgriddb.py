from __future__ import annotations

import json
import logging
import os
import re
import base64
import ssl
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

_API_ROOT = "https://www.steamgriddb.com/api/v2"
_CACHE_SCHEMA = "full-v2-language"
_LEGACY_CACHE_SCHEMA = "full-v1"
_IMAGE_HOSTS = {"cdn2.steamgriddb.com", "cdn.steamgriddb.com", "s3.amazonaws.com"}
_SYSTEM_CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
)
logger = logging.getLogger(__name__)


def _system_tls_context() -> ssl.SSLContext:
    for candidate in _SYSTEM_CA_FILES:
        if Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


_TLS_CONTEXT = _system_tls_context()
_ALIASES = {
    "原神": "Genshin Impact",
    "崩坏3": "Honkai Impact 3rd",
    "崩坏：星穹铁道": "Honkai: Star Rail",
    "崩坏: 星穹铁道": "Honkai: Star Rail",
    "绝区零": "Zenless Zone Zero",
}


class SteamGridDbResolver:
    def __init__(self, secret_file: str | Path, cache_file: str | Path) -> None:
        self.secret_file = Path(secret_file)
        self.cache_file = Path(cache_file)
        self._lock = Lock()
        self._download_lock = Lock()

    def configured(self) -> bool:
        return self._read_key() is not None

    def save_key(self, value: str) -> None:
        key = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", key):
            raise ValueError("steamgriddb.invalid_key")
        self.secret_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.secret_file.with_suffix(".new")
        temporary.write_text(key, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.secret_file)
        self.secret_file.chmod(0o600)

    def test_connection(self) -> bool:
        key = self._read_key()
        if key is None:
            return False
        response = self._request("/search/autocomplete/Genshin%20Impact", key)
        return isinstance(response, dict) and response.get("success") is True

    def download_image(self, url: str) -> dict[str, str]:
        if not self._valid_image(url):
            raise ValueError("steamgriddb.invalid_image_url")
        with self._download_lock:
            return self._download_image(url)

    @staticmethod
    def _download_image(url: str) -> dict[str, str]:
        request = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": "GameBridge/0.18"}
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=15, context=_TLS_CONTEXT
                ) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in {
                        "image/png",
                        "image/jpeg",
                        "image/webp",
                        "image/x-icon",
                        "image/vnd.microsoft.icon",
                    }:
                        raise ValueError("steamgriddb.invalid_image")
                    data = response.read(10 * 1024 * 1024 + 1)
                break
            except OSError as exc:
                if attempt == 2:
                    raise ValueError("steamgriddb.image_download_failed") from exc
                time.sleep(0.25 * (attempt + 1))
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError("steamgriddb.invalid_image")
        return {
            "base64": base64.b64encode(data).decode("ascii"),
            "mimeType": content_type,
        }

    def cached(
        self, provider_id: str, external_game_id: str, language: str | None = None
    ) -> dict[str, str] | None:
        value = self._read_cache().get(f"{provider_id}:{external_game_id}")
        schema = value.get("schema") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or schema not in {_CACHE_SCHEMA, _LEGACY_CACHE_SCHEMA}
            or (
                language is not None
                and schema == _CACHE_SCHEMA
                and value.get("language") != self._normalize_language(language)
            )
            or not self._valid_image(value.get("capsule"))
            or any(
                asset in value and not self._valid_image(value.get(asset))
                for asset in ("hero", "header", "logo", "icon")
            )
        ):
            return None
        return {
            key: item
            for key, item in value.items()
            if key not in {"schema", "language"} and isinstance(item, str)
        }

    def needs_refresh(self, provider_id: str, external_game_id: str) -> bool:
        """Retry incomplete sets while keeping already written assets visible."""
        return (
            self.cached(provider_id, external_game_id) is None
            or not self._cache_entry_complete(provider_id, external_game_id)
        )

    def cached_language(self, provider_id: str, external_game_id: str) -> str | None:
        value = self._read_cache().get(f"{provider_id}:{external_game_id}")
        if not isinstance(value, dict):
            return None
        if value.get("schema") == _LEGACY_CACHE_SCHEMA:
            return "en"
        if value.get("schema") != _CACHE_SCHEMA:
            return None
        language = value.get("language")
        return language if isinstance(language, str) else None

    def cached_asset_references(self, url: str) -> list[tuple[str, str, str]]:
        """Map a trusted cached URL back to every matching provider game asset."""
        if not self._valid_image(url):
            return []
        matches = []
        for cache_key, value in self._read_cache().items():
            if not isinstance(value, dict) or ":" not in cache_key:
                continue
            for asset in ("capsule", "hero", "header", "logo", "icon"):
                if value.get(asset) == url:
                    provider_id, external_game_id = cache_key.split(":", 1)
                    matches.append((provider_id, external_game_id, asset))
        return matches

    def resolve(
        self,
        provider_id: str,
        external_game_id: str,
        title: str,
        *,
        force: bool = False,
        language: str | None = None,
    ) -> dict[str, str] | None:
        preferred_language = self._normalize_language(language) if language else "any"
        # A successfully cached artwork set is stable. Language changes must not
        # turn ordinary library navigation into another SteamGridDB request.
        cached = None if force else self.cached(provider_id, external_game_id)
        if cached is not None and self._cache_entry_complete(
            provider_id, external_game_id
        ):
            return cached
        key = self._read_key()
        if key is None:
            return None
        search_title = _ALIASES.get(title, title)
        payload = self._request(
            "/search/autocomplete/" + urllib.parse.quote(search_title, safe=""), key
        )
        results = payload.get("data") if isinstance(payload, dict) and payload.get("success") else None
        if not isinstance(results, list):
            return None
        normalized = self._normalize(search_title)
        exact = [
            item for item in results
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and self._normalize(str(item.get("name", ""))) == normalized
        ]
        if len(exact) != 1:
            return None
        game_id = exact[0]["id"]
        capsule_payload = self._request(f"/grids/game/{game_id}?dimensions=600x900", key)
        capsule = self._best_asset(
            capsule_payload,
            dimensions={(600, 900)},
            language=language,
        )
        if capsule is None:
            return cached

        # Persist every successful asset immediately. Decky may unload a plugin
        # while the remaining network requests are still running; delaying the
        # write until all five requests finish used to discard earlier successes.
        value: dict[str, object] = {
            "schema": _CACHE_SCHEMA,
            "language": preferred_language,
            "complete": False,
            "capsule": capsule,
        }
        self._merge_cache_entry(provider_id, external_game_id, value, replace=force)

        header_payload = self._request(f"/grids/game/{game_id}?dimensions=460x215", key)
        header = self._best_asset(
            header_payload,
            dimensions={(460, 215)},
            language=language,
        )
        header_fallback_payload = None
        if header is None:
            header_fallback_payload = self._request(
                f"/grids/game/{game_id}?dimensions=920x430", key
            )
            header = self._best_asset(
                header_fallback_payload,
                dimensions={(920, 430)},
                language=language,
            )
        if header:
            value["header"] = header
            self._merge_cache_entry(provider_id, external_game_id, {"header": header})

        hero_payload = self._request(f"/heroes/game/{game_id}", key)
        hero = self._best_asset(hero_payload, language=language)
        if hero:
            value["hero"] = hero
            self._merge_cache_entry(provider_id, external_game_id, {"hero": hero})

        logo_payload = self._request(f"/logos/game/{game_id}", key)
        logo = self._best_asset(logo_payload, language=language)
        if logo:
            value["logo"] = logo
            self._merge_cache_entry(provider_id, external_game_id, {"logo": logo})

        icon_payload = self._request(f"/icons/game/{game_id}", key)
        icon = self._best_asset(icon_payload, language=language)
        if icon:
            value["icon"] = icon
            self._merge_cache_entry(provider_id, external_game_id, {"icon": icon})

        responses = [capsule_payload, header_payload, hero_payload, logo_payload, icon_payload]
        if header_fallback_payload is not None:
            responses.append(header_fallback_payload)
        complete = all(self._successful_asset_response(payload) for payload in responses) and all(
            isinstance(value.get(asset), str)
            for asset in ("capsule", "hero", "header", "logo", "icon")
        )
        value["complete"] = complete
        self._merge_cache_entry(
            provider_id, external_game_id, {"complete": complete}
        )
        return {
            key: item
            for key, item in value.items()
            if key not in {"schema", "language"} and isinstance(item, str)
        }

    def _cache_entry_complete(self, provider_id: str, external_game_id: str) -> bool:
        value = self._read_cache().get(f"{provider_id}:{external_game_id}")
        return bool(isinstance(value, dict) and value.get("complete") is True)

    def _merge_cache_entry(
        self,
        provider_id: str,
        external_game_id: str,
        update: dict[str, object],
        *,
        replace: bool = False,
    ) -> None:
        cache_key = f"{provider_id}:{external_game_id}"
        with self._lock:
            cache = self._read_cache()
            current = {} if replace else cache.get(cache_key, {})
            value = dict(current) if isinstance(current, dict) else {}
            value.update(update)
            cache[cache_key] = value
            self._write_cache(cache)

    @staticmethod
    def _successful_asset_response(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and payload.get("success") is True
            and isinstance(payload.get("data"), list)
        )

    def _read_key(self) -> str | None:
        try:
            value = self.secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if re.fullmatch(r"[A-Za-z0-9_-]{16,256}", value) else None

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    @staticmethod
    def _normalize_language(value: str) -> str:
        normalized = value.strip().replace("_", "-").casefold()
        return normalized if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", normalized) else "en"

    @classmethod
    def _best_asset(
        cls,
        payload: Any,
        dimensions: set[tuple[int, int]] | None = None,
        language: str | None = None,
    ) -> str | None:
        items = payload.get("data") if isinstance(payload, dict) and payload.get("success") else None
        if not isinstance(items, list):
            return None
        preferred = cls._normalize_language(language) if language else None
        base = preferred.split("-", 1)[0] if preferred else None
        candidates = []
        for item in items:
            if not isinstance(item, dict) or not cls._valid_image(item.get("url")):
                continue
            if dimensions and (item.get("width"), item.get("height")) not in dimensions:
                continue
            score = item.get("score")
            item_language = str(item.get("language") or "").replace("_", "-").casefold()
            if preferred is None:
                language_rank = 0
            elif item_language == preferred:
                language_rank = 4
            elif item_language == base:
                language_rank = 3
            elif item_language == "en":
                language_rank = 2
            elif not item_language:
                language_rank = 1
            else:
                language_rank = 0
            upvotes = item.get("upvotes")
            downvotes = item.get("downvotes")
            net_votes = (
                upvotes - downvotes
                if isinstance(upvotes, int) and isinstance(downvotes, int)
                else 0
            )
            candidates.append(
                (
                    language_rank,
                    net_votes,
                    score if isinstance(score, int) else 0,
                    int(item.get("id", 0)),
                    item["url"],
                )
            )
        return max(candidates, default=(0, 0, 0, 0, None))[-1]

    @staticmethod
    def _valid_image(value: object) -> bool:
        if not isinstance(value, str) or len(value) > 2048:
            return False
        parsed = urllib.parse.urlparse(value)
        return parsed.scheme == "https" and parsed.hostname in _IMAGE_HOSTS and not parsed.username

    @staticmethod
    def _request(path: str, key: str) -> Any:
        request = urllib.request.Request(  # noqa: S310
            _API_ROOT + path,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "GameBridge/0.18"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=8, context=_TLS_CONTEXT
                ) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                # Authentication and malformed requests cannot recover by retrying.
                if exc.code not in {429, 500, 502, 503, 504}:
                    logger.warning(
                        "SteamGridDB request rejected status=%d", exc.code
                    )
                    return None
            except (OSError, ValueError) as exc:
                if attempt == 2:
                    logger.warning(
                        "SteamGridDB request failed error=%s", type(exc).__name__
                    )
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
        logger.warning("SteamGridDB request exhausted transient retries")
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
