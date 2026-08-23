import json
import urllib.error
from email.message import Message

import pytest

from gamebridge.application import GameBridgeApplication
from gamebridge.steamgriddb import SteamGridDbResolver


def test_request_retries_transient_http_failure(monkeypatch):
    attempts = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size=-1):
            return b'{"success": true, "data": []}'

    def urlopen(_request, timeout, context):
        nonlocal attempts
        attempts += 1
        assert timeout == 8
        assert context is not None
        if attempts == 1:
            raise urllib.error.HTTPError("https://example.invalid", 429, "", {}, None)
        return Response()

    monkeypatch.setattr("gamebridge.steamgriddb.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("gamebridge.steamgriddb.time.sleep", lambda _seconds: None)

    assert SteamGridDbResolver._request("/test", "valid_api_key_123456") == {
        "success": True,
        "data": [],
    }
    assert attempts == 2


def test_key_is_stored_outside_database_with_private_permissions(tmp_path):
    secret = tmp_path / "secrets" / "steamgriddb.key"
    resolver = SteamGridDbResolver(secret, tmp_path / "cache.json")

    resolver.save_key("valid_api_key_123456")

    assert resolver.configured() is True
    assert secret.read_text(encoding="utf-8") == "valid_api_key_123456"
    assert secret.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["", "short", "contains a space", "bad/token/value"])
def test_invalid_key_is_rejected(tmp_path, value):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")

    with pytest.raises(ValueError, match="steamgriddb.invalid_key"):
        resolver.save_key(value)


def test_exact_alias_match_fetches_best_portrait_and_caches(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")
    calls = []

    def request(path, _key):
        calls.append(path)
        if path.startswith("/search/"):
            return {"success": True, "data": [{"id": 10, "name": "Genshin Impact"}]}
        if "dimensions=600x900" in path:
            return {
                "success": True,
                "data": [
                    {"id": 1, "score": 99, "width": 460, "height": 215, "url": "https://cdn2.steamgriddb.com/wrong.png"},
                    {"id": 2, "score": 8, "width": 600, "height": 900, "url": "https://cdn2.steamgriddb.com/portrait-a.png"},
                    {"id": 3, "score": 12, "width": 600, "height": 900, "url": "https://cdn2.steamgriddb.com/portrait-b.png"},
                ],
            }
        if "dimensions=460x215" in path:
            return {"success": True, "data": [{"id": 6, "score": 4, "width": 460, "height": 215, "url": "https://cdn2.steamgriddb.com/header.png"}]}
        if path.startswith("/heroes/"):
            return {"success": True, "data": [{"id": 4, "score": 2, "url": "https://cdn2.steamgriddb.com/hero.png"}]}
        if path.startswith("/logos/"):
            return {"success": True, "data": [{"id": 5, "score": 1, "url": "https://cdn2.steamgriddb.com/logo.png"}]}
        return {"success": True, "data": [{"id": 7, "score": 3, "url": "https://cdn2.steamgriddb.com/icon.png"}]}

    monkeypatch.setattr(resolver, "_request", request)
    result = resolver.resolve("mihoyo_cn", "hk4e_cn", "原神")

    assert result == {
        "capsule": "https://cdn2.steamgriddb.com/portrait-b.png",
        "hero": "https://cdn2.steamgriddb.com/hero.png",
        "header": "https://cdn2.steamgriddb.com/header.png",
        "logo": "https://cdn2.steamgriddb.com/logo.png",
        "icon": "https://cdn2.steamgriddb.com/icon.png",
    }
    assert calls[0].endswith("Genshin%20Impact")
    calls.clear()
    assert resolver.resolve("mihoyo_cn", "hk4e_cn", "原神") == result
    assert calls == []
    assert "valid_api_key_123456" not in json.dumps(
        json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    )


def test_best_asset_prefers_local_language_then_english_and_community_votes():
    payload = {
        "success": True,
        "data": [
            {"id": 1, "language": "ja", "score": 100, "upvotes": 500, "downvotes": 0,
             "url": "https://cdn2.steamgriddb.com/ja.png"},
            {"id": 2, "language": "en", "score": 20, "upvotes": 50, "downvotes": 2,
             "url": "https://cdn2.steamgriddb.com/en.png"},
            {"id": 3, "language": "zh", "score": 5, "upvotes": 8, "downvotes": 1,
             "url": "https://cdn2.steamgriddb.com/zh-low.png"},
            {"id": 4, "language": "zh", "score": 4, "upvotes": 20, "downvotes": 1,
             "url": "https://cdn2.steamgriddb.com/zh-best.png"},
        ],
    }

    assert SteamGridDbResolver._best_asset(payload, language="zh-CN") == (
        "https://cdn2.steamgriddb.com/zh-best.png"
    )
    english_fallback = {**payload, "data": payload["data"][:2]}
    assert SteamGridDbResolver._best_asset(english_fallback, language="zh-CN") == (
        "https://cdn2.steamgriddb.com/en.png"
    )


def test_language_change_reuses_cached_selection_without_network(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")
    calls = []

    def request(path, _key):
        calls.append(path)
        if path.startswith("/search/"):
            return {"success": True, "data": [{"id": 10, "name": "Sample"}]}
        if "dimensions=600x900" in path:
            return {"success": True, "data": [{
                "id": 1, "language": "en", "width": 600, "height": 900,
                "url": "https://cdn2.steamgriddb.com/portrait.png",
            }]}
        if "dimensions=460x215" in path:
            return {"success": True, "data": [{
                "id": 2, "width": 460, "height": 215,
                "url": "https://cdn2.steamgriddb.com/header.png",
            }]}
        asset = path.split("/", 2)[1]
        return {"success": True, "data": [{
            "id": 3, "url": f"https://cdn2.steamgriddb.com/{asset}.png",
        }]}

    monkeypatch.setattr(resolver, "_request", request)
    resolver.resolve("epic", "sample", "Sample", language="zh-CN")
    first_count = len(calls)
    resolver.resolve("epic", "sample", "Sample", language="zh_CN")
    assert len(calls) == first_count
    resolver.resolve("epic", "sample", "Sample", language="en")
    assert len(calls) == first_count
    assert resolver.cached_language("epic", "sample") == "zh-cn"


def test_legacy_partial_artwork_is_visible_and_scheduled_for_completion(tmp_path):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver._write_cache({
        "mihoyo_cn:hk4e_cn": {
            "schema": "full-v1",
            "capsule": "https://cdn2.steamgriddb.com/legacy.png",
            "hero": "https://cdn2.steamgriddb.com/legacy-hero.png",
        }
    })

    assert resolver.cached("mihoyo_cn", "hk4e_cn", "zh-CN") == {
        "capsule": "https://cdn2.steamgriddb.com/legacy.png",
        "hero": "https://cdn2.steamgriddb.com/legacy-hero.png",
    }
    assert resolver.cached_language("mihoyo_cn", "hk4e_cn") == "en"
    assert resolver.needs_refresh("mihoyo_cn", "hk4e_cn") is True


def test_force_refresh_replaces_legacy_capsule_instead_of_merging_over_it(
    tmp_path, monkeypatch
):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")
    resolver._write_cache({
        "mihoyo_cn:hk4e_cn": {
            "schema": "full-v1",
            "capsule": "https://cdn2.steamgriddb.com/legacy.png",
        }
    })

    def request(path, _key):
        if path.startswith("/search/"):
            return {"success": True, "data": [{"id": 10, "name": "Genshin Impact"}]}
        if "dimensions=600x900" in path:
            return {"success": True, "data": [{
                "id": 1, "language": "zh", "width": 600, "height": 900,
                "url": "https://cdn2.steamgriddb.com/zh-new.png",
            }]}
        return {"success": True, "data": []}

    monkeypatch.setattr(resolver, "_request", request)
    refreshed = resolver.resolve(
        "mihoyo_cn", "hk4e_cn", "原神", force=True, language="zh-CN"
    )

    assert refreshed["capsule"] == "https://cdn2.steamgriddb.com/zh-new.png"
    assert resolver.cached_language("mihoyo_cn", "hk4e_cn") == "zh-cn"


def test_ambiguous_or_untrusted_results_are_not_cached(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")
    monkeypatch.setattr(
        resolver,
        "_request",
        lambda path, _key: (
            {"success": True, "data": [{"id": 1, "name": "Sample"}, {"id": 2, "name": "Sample"}]}
            if path.startswith("/search/")
            else {"success": True, "data": [{"id": 3, "width": 600, "height": 900, "url": "https://example.com/tracker.png"}]}
        ),
    )

    assert resolver.resolve("epic", "sample", "Sample") is None
    assert not (tmp_path / "cache.json").exists()


def test_missing_optional_asset_does_not_discard_portrait(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")

    def request(path, _key):
        if path.startswith("/search/"):
            return {"success": True, "data": [{"id": 10, "name": "Sample"}]}
        if "dimensions=600x900" in path:
            return {"success": True, "data": [{"id": 1, "score": 1, "width": 600, "height": 900, "url": "https://cdn2.steamgriddb.com/portrait.png"}]}
        return {"success": True, "data": []}

    monkeypatch.setattr(resolver, "_request", request)

    assert resolver.resolve("epic", "sample", "Sample") == {
        "capsule": "https://cdn2.steamgriddb.com/portrait.png"
    }
    assert resolver.cached("epic", "sample") == {
        "capsule": "https://cdn2.steamgriddb.com/portrait.png"
    }


def test_partial_asset_success_is_kept_and_missing_assets_are_retried(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")
    hero_attempts = 0

    def request(path, _key):
        nonlocal hero_attempts
        if path.startswith("/search/"):
            return {"success": True, "data": [{"id": 10, "name": "Sample"}]}
        if "dimensions=600x900" in path:
            return {"success": True, "data": [{
                "id": 1, "width": 600, "height": 900,
                "url": "https://cdn2.steamgriddb.com/portrait.png",
            }]}
        if path.startswith("/heroes/"):
            hero_attempts += 1
            if hero_attempts == 1:
                return None
            return {"success": True, "data": [{
                "id": 2, "url": "https://cdn2.steamgriddb.com/hero.png",
            }]}
        return {"success": True, "data": []}

    monkeypatch.setattr(resolver, "_request", request)

    assert resolver.resolve("epic", "sample", "Sample") == {
        "capsule": "https://cdn2.steamgriddb.com/portrait.png"
    }
    assert resolver.needs_refresh("epic", "sample") is True
    assert resolver.resolve("epic", "sample", "Sample") == {
        "capsule": "https://cdn2.steamgriddb.com/portrait.png",
        "hero": "https://cdn2.steamgriddb.com/hero.png",
    }
    assert resolver.needs_refresh("epic", "sample") is True
    assert hero_attempts == 2


def test_capsule_is_persisted_before_later_asset_request_is_interrupted(
    tmp_path, monkeypatch
):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")

    def request(path, _key):
        if path.startswith("/search/"):
            return {"success": True, "data": [{"id": 10, "name": "Sample"}]}
        if "dimensions=600x900" in path:
            return {"success": True, "data": [{
                "id": 1, "width": 600, "height": 900,
                "url": "https://cdn2.steamgriddb.com/portrait.png",
            }]}
        raise RuntimeError("plugin unloaded")

    monkeypatch.setattr(resolver, "_request", request)

    with pytest.raises(RuntimeError, match="plugin unloaded"):
        resolver.resolve("epic", "sample", "Sample")
    assert resolver.cached("epic", "sample") == {
        "capsule": "https://cdn2.steamgriddb.com/portrait.png"
    }
    assert resolver.needs_refresh("epic", "sample") is True


def test_connection_failure_does_not_expose_key(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    resolver.save_key("valid_api_key_123456")
    monkeypatch.setattr(resolver, "_request", lambda _path, _key: None)

    assert resolver.test_connection() is False


def test_download_image_accepts_only_trusted_cdn_and_bounded_image(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")

    class Response:
        headers = Message()

        def __init__(self):
            self.headers["Content-Type"] = "image/png"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b"png-data"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    assert resolver.download_image("https://cdn2.steamgriddb.com/image.png") == {
        "base64": "cG5nLWRhdGE=",
        "mimeType": "image/png",
    }
    with pytest.raises(ValueError, match="steamgriddb.invalid_image_url"):
        resolver.download_image("https://example.com/image.png")


def test_download_image_retries_transient_network_failure(tmp_path, monkeypatch):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")
    attempts = 0

    class Response:
        headers = Message()

        def __init__(self):
            self.headers["Content-Type"] = "image/png"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b"png-data"

    def urlopen(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary connection reset")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = resolver.download_image("https://cdn2.steamgriddb.com/image.png")
    assert result["mimeType"] == "image/png"
    assert attempts == 2


@pytest.mark.parametrize("content_type", ["image/x-icon", "image/vnd.microsoft.icon"])
def test_download_image_accepts_steamgriddb_icon_formats(
    tmp_path, monkeypatch, content_type
):
    resolver = SteamGridDbResolver(tmp_path / "key", tmp_path / "cache.json")

    class Response:
        headers = Message()

        def __init__(self):
            self.headers["Content-Type"] = content_type

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b"ico-data"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    assert resolver.download_image("https://cdn2.steamgriddb.com/icon.ico") == {
        "base64": "aWNvLWRhdGE=",
        "mimeType": content_type,
    }


@pytest.mark.asyncio
async def test_application_save_and_test_returns_one_success_result(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    monkeypatch.setattr(application.steamgriddb, "test_connection", lambda: True)

    result = await application.save_steamgriddb_key("valid_api_key_123456")

    assert result == {
        "steamGridDbConfigured": True,
        "steamGridDbLastValidationSucceeded": True,
        "connected": True,
    }


@pytest.mark.asyncio
async def test_application_save_and_test_reports_authentication_failure(tmp_path, monkeypatch):
    application = GameBridgeApplication(tmp_path)
    application.start()
    monkeypatch.setattr(application.steamgriddb, "test_connection", lambda: False)

    with pytest.raises(ValueError, match="steamgriddb.connection_failed"):
        await application.save_steamgriddb_key("valid_api_key_123456")
    assert application.artwork_settings()["steamGridDbLastValidationSucceeded"] is False
