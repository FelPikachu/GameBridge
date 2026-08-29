import pytest

from gamebridge.steam_browser import (
    EPIC_STORE_URL,
    EpicCorrectiveActionRequired,
    SteamBrowserAuthorization,
)


def test_extracts_epic_code_directly_from_browser_page(monkeypatch) -> None:
    calls: list[str] = []

    def command(cls, websocket_url, method, params=None):
        calls.append(method)
        if method == "Runtime.evaluate":
            return {
                "result": {
                    "value": '{"authorizationCode":"browser-page-code"}'
                }
            }
        raise AssertionError(f"unexpected fallback call: {method}")

    monkeypatch.setattr(SteamBrowserAuthorization, "_cdp_command", classmethod(command))

    code = SteamBrowserAuthorization._code_from_session(
        "ws://127.0.0.1:8080/devtools/page/epic"
    )

    assert code == "browser-page-code"
    assert calls == ["Runtime.evaluate"]


def test_detects_epic_privacy_policy_without_exposing_continuation(monkeypatch) -> None:
    def command(cls, websocket_url, method, params=None):
        if method == "Runtime.evaluate":
            return {"result": {"value": (
                '{"errorCode":"errors.com.epicgames.oauth.corrective_action_required",'
                '"metadata":{"correctiveAction":"PRIVACY_POLICY_ACCEPTANCE",'
                '"continuation":"private-one-time-value"}}'
            )}}
        raise AssertionError(f"unexpected fallback call: {method}")

    monkeypatch.setattr(SteamBrowserAuthorization, "_cdp_command", classmethod(command))

    with pytest.raises(EpicCorrectiveActionRequired) as raised:
        SteamBrowserAuthorization._code_from_session(
            "ws://127.0.0.1:8080/devtools/page/epic"
        )

    assert raised.value.action == "PRIVACY_POLICY_ACCEPTANCE"
    assert "private-one-time-value" not in str(raised.value)


@pytest.mark.asyncio
async def test_privacy_policy_flow_opens_account_page_once_then_returns_code(monkeypatch) -> None:
    browser = SteamBrowserAuthorization()
    results: list[object] = [
        EpicCorrectiveActionRequired("PRIVACY_POLICY_ACCEPTANCE"),
        None,
        "accepted-code",
    ]
    opened: list[tuple[str, str]] = []

    monkeypatch.setattr(browser, "_json_pages", lambda: [
        {"webSocketDebuggerUrl": "ws://127.0.0.1:8080/devtools/page/epic"}
    ])

    def code_from_session(websocket_url: str):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(browser, "_code_from_session", code_from_session)
    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr("gamebridge.steam_browser.asyncio.sleep", no_wait)
    monkeypatch.setattr(
        browser,
        "_open_epic_privacy_page",
        lambda websocket_url: opened.append((websocket_url, EPIC_STORE_URL)),
    )

    assert await browser._poll() == "accepted-code"
    assert opened == [
        ("ws://127.0.0.1:8080/devtools/page/epic", EPIC_STORE_URL)
    ]


def test_corrective_action_navigation_uses_official_epic_store(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    def command(cls, websocket_url, method, params=None):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(SteamBrowserAuthorization, "_cdp_command", classmethod(command))

    SteamBrowserAuthorization._open_epic_privacy_page(
        "ws://127.0.0.1:8080/devtools/page/epic"
    )

    assert calls == [("Page.navigate", {"url": EPIC_STORE_URL})]


def test_delete_epic_cookies_only(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    def command(
        cls, websocket_url: str, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        calls.append((method, params))
        if method == "Network.getAllCookies":
            return {
                "cookies": [
                    {"name": "EPIC_SESSION_AP", "value": "secret", "domain": ".epicgames.com", "path": "/"},
                    {"name": "child", "value": "secret", "domain": "store.epicgames.com", "path": "/account"},
                    {"name": "steamLoginSecure", "value": "keep", "domain": ".steampowered.com", "path": "/"},
                    {"name": "lookalike", "value": "keep", "domain": "notepicgames.com", "path": "/"},
                ]
            }
        return {}

    monkeypatch.setattr(SteamBrowserAuthorization, "_cdp_command", classmethod(command))

    removed = SteamBrowserAuthorization._delete_epic_cookies(
        "ws://127.0.0.1:8080/devtools/page/test"
    )

    assert removed == 2
    deletions = [params for method, params in calls if method == "Network.deleteCookies"]
    assert deletions == [
        {"name": "EPIC_SESSION_AP", "domain": ".epicgames.com", "path": "/"},
        {"name": "child", "domain": "store.epicgames.com", "path": "/account"},
    ]


def test_epic_cookie_domain_does_not_match_lookalikes() -> None:
    matches = SteamBrowserAuthorization._is_epic_cookie_domain
    assert matches(".epicgames.com")
    assert matches("www.epicgames.com")
    assert not matches("notepicgames.com")
    assert not matches("epicgames.com.example.org")


def test_delete_steamgriddb_cookies_and_site_storage_only(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    def command(
        cls, websocket_url: str, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        calls.append((method, params))
        if method == "Network.getAllCookies":
            return {"cookies": [
                {"name": "session", "domain": ".steamgriddb.com", "path": "/"},
                {"name": "child", "domain": "www.steamgriddb.com", "path": "/profile"},
                {"name": "keep", "domain": ".steampowered.com", "path": "/"},
                {"name": "lookalike", "domain": "notsteamgriddb.com", "path": "/"},
            ]}
        return {}

    monkeypatch.setattr(SteamBrowserAuthorization, "_cdp_command", classmethod(command))

    removed = SteamBrowserAuthorization._delete_steamgriddb_data(
        "ws://127.0.0.1:8080/devtools/page/test"
    )

    assert removed == 2
    deletions = [params for method, params in calls if method == "Network.deleteCookies"]
    assert deletions == [
        {"name": "session", "domain": ".steamgriddb.com", "path": "/"},
        {"name": "child", "domain": "www.steamgriddb.com", "path": "/profile"},
    ]
    origins = [params["origin"] for method, params in calls if method == "Storage.clearDataForOrigin"]
    assert origins == ["https://steamgriddb.com", "https://www.steamgriddb.com"]


def test_steamgriddb_cookie_domain_does_not_match_lookalikes() -> None:
    matches = SteamBrowserAuthorization._is_steamgriddb_cookie_domain
    assert matches(".steamgriddb.com")
    assert matches("www.steamgriddb.com")
    assert not matches("notsteamgriddb.com")
    assert not matches("steamgriddb.com.example.org")


def test_navigation_uses_steam_route_history() -> None:
    expression = SteamBrowserAuthorization._navigate_back_expression()

    assert "tempNavStore" in expression
    assert "m_history" in expression
    assert "goBack" in expression


def test_external_route_detection_requires_shared_context() -> None:
    detect = SteamBrowserAuthorization._is_external_route

    assert detect({"title": "SharedJSContext", "url": "https://steamloopback.host/routes/externalweb"})
    assert not detect({"title": "Epic", "url": "https://steamloopback.host/routes/externalweb"})
    assert not detect({"title": "SharedJSContext", "url": "https://steamloopback.host/routes/library"})
