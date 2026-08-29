from __future__ import annotations

import asyncio
import base64
import json
import secrets
import socket
import struct
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EPIC_REDIRECT_URL = (
    "https://www.epicgames.com/id/api/redirect?"
    "clientId=34a02cf8f4414e29b15921876da36f9a&responseType=code"
)
EPIC_STORE_URL = "https://store.epicgames.com/zh-CN/"


class EpicCorrectiveActionRequired(RuntimeError):
    """Epic requires an account-owner action before issuing an auth code."""

    def __init__(self, action: str) -> None:
        super().__init__("epic.corrective_action_required")
        self.action = action


class SteamBrowserAuthorization:
    """Read an Epic authorization result from Steam's local CEF debugger."""

    def __init__(self, port: int = 8080, timeout: float = 300) -> None:
        self.port = port
        self.timeout = timeout

    async def wait_for_epic_code(self) -> str:
        return await asyncio.wait_for(self._poll(), self.timeout)

    async def wait_for_external_route(self, timeout: float = 15) -> None:
        """Wait until Steam has actually mounted its external-web route."""
        async def poll() -> None:
            while True:
                pages = await asyncio.to_thread(self._json_pages)
                if any(self._is_external_route(page) for page in pages):
                    return
                await asyncio.sleep(0.1)

        await asyncio.wait_for(poll(), timeout)

    @staticmethod
    def _is_external_route(page: dict[str, object]) -> bool:
        return (
            page.get("title") == "SharedJSContext"
            and "/routes/externalweb" in str(page.get("url", ""))
        )

    async def navigate_back(self) -> None:
        """Close the Steam browser route after authentication completes."""
        pages = await asyncio.to_thread(self._json_pages)
        shared_contexts = [
            page for page in pages if page.get("title") == "SharedJSContext"
        ]
        for page in shared_contexts or pages:
            debugger = page.get("webSocketDebuggerUrl")
            if not isinstance(debugger, str):
                continue
            try:
                await asyncio.to_thread(
                    self._cdp_command,
                    debugger,
                    "Runtime.evaluate",
                    {
                        "expression": self._navigate_back_expression(),
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                return
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                continue

    @staticmethod
    def _navigate_back_expression() -> str:
        # The embedded Epic page owns window.history, but Steam's gamepad UI
        # route is stored separately in SharedJSContext.
        return """(() => {
            const routeHistory = globalThis.tempNavStore?.m_history;
            if (routeHistory && typeof routeHistory.goBack === "function") {
                routeHistory.goBack();
                return true;
            }
            window.history.back();
            return false;
        })()"""

    async def clear_epic_session(self) -> int:
        """Delete Epic cookies from Steam's browser without touching other sites."""
        pages = await asyncio.to_thread(self._json_pages)
        if not pages:
            raise RuntimeError("Steam browser debugger is unavailable")

        last_error: Exception | None = None
        for page in pages:
            debugger = page.get("webSocketDebuggerUrl")
            if not isinstance(debugger, str):
                continue
            try:
                return await asyncio.to_thread(self._delete_epic_cookies, debugger)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
        if last_error is not None:
            raise RuntimeError("Could not clear the Epic browser session") from last_error
        raise RuntimeError("Steam browser debugger has no usable page")

    async def clear_steamgriddb_session(self) -> int:
        """Delete SteamGridDB browser state without touching other sites."""
        pages = await asyncio.to_thread(self._json_pages)
        if not pages:
            raise RuntimeError("Steam browser debugger is unavailable")

        last_error: Exception | None = None
        for page in pages:
            debugger = page.get("webSocketDebuggerUrl")
            if not isinstance(debugger, str):
                continue
            try:
                return await asyncio.to_thread(self._delete_steamgriddb_data, debugger)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
        if last_error is not None:
            raise RuntimeError("Could not clear the SteamGridDB browser session") from last_error
        raise RuntimeError("Steam browser debugger has no usable page")

    async def _poll(self) -> str:
        corrective_page_opened = False
        while True:
            pages = await asyncio.to_thread(self._json_pages)
            for page in pages:
                debugger = page.get("webSocketDebuggerUrl")
                if not isinstance(debugger, str):
                    continue
                try:
                    code = await asyncio.to_thread(self._code_from_session, debugger)
                except EpicCorrectiveActionRequired as exc:
                    if exc.action == "PRIVACY_POLICY_ACCEPTANCE" and not corrective_page_opened:
                        await asyncio.to_thread(self._open_epic_privacy_page, debugger)
                        corrective_page_opened = True
                        continue
                    if exc.action == "PRIVACY_POLICY_ACCEPTANCE":
                        continue
                    raise
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError, HTTPError, URLError):
                    continue
                if code:
                    return code
            # Once the account page is open, a slower retry is enough to notice
            # that the user accepted Epic's policy without hammering its API.
            await asyncio.sleep(2 if corrective_page_opened else 0.3)

    def _json_pages(self) -> list[dict[str, object]]:
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1) as response:
                payload = json.loads(response.read(1_000_000))
        except (OSError, URLError, ValueError):
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    @classmethod
    def _cdp_command(
        cls, websocket_url: str, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        if not websocket_url.startswith("ws://127.0.0.1:"):
            raise ValueError("unexpected debugger address")
        authority, target = websocket_url[5:].split("/", 1)
        host, port_text = authority.rsplit(":", 1)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        with socket.create_connection((host, int(port_text)), timeout=2) as connection:
            connection.sendall(
                (
                    f"GET /{target} HTTP/1.1\r\nHost: {authority}\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\nOrigin: http://127.0.0.1\r\n\r\n"
                ).encode("ascii")
            )
            response = cls._read_until(connection, b"\r\n\r\n")
            if b" 101 " not in response.split(b"\r\n", 1)[0]:
                raise RuntimeError("Steam browser debugger rejected the connection")
            cls._send_text(
                connection,
                json.dumps({"id": 1, "method": method, "params": params or {}}),
            )
            for _ in range(20):
                message = json.loads(cls._receive_text(connection))
                if message.get("id") == 1:
                    result = message.get("result", {})
                    return result if isinstance(result, dict) else {}
        return {}

    @classmethod
    def _code_from_session(cls, websocket_url: str) -> str | None:
        # Epic renders the completed authorization response as a JSON document
        # in the browser page itself.  Reading that document through CDP is the
        # reliable path: Steam's embedded browser owns the authenticated
        # session, and replaying its cookies with urllib is not guaranteed to
        # reproduce the same request/session state.
        content_result = cls._cdp_command(
            websocket_url,
            "Runtime.evaluate",
            {
                "expression": (
                    "document.body?.innerText || "
                    "document.documentElement?.innerText || ''"
                ),
                "returnByValue": True,
            },
        )
        runtime_result = content_result.get("result", {})
        if isinstance(runtime_result, dict):
            page_text = runtime_result.get("value")
            if isinstance(page_text, str):
                code = cls._extract_code(page_text)
                if code:
                    return code
                corrective_action = cls._extract_corrective_action(page_text)
                if corrective_action:
                    raise EpicCorrectiveActionRequired(corrective_action)

        # Compatibility fallback for older CEF builds where Runtime.evaluate
        # cannot expose the external page's body.
        cookie_result = cls._cdp_command(websocket_url, "Network.getAllCookies")
        raw_cookies = cookie_result.get("cookies", [])
        if not isinstance(raw_cookies, list):
            return None
        epic_cookies = [
            cookie for cookie in raw_cookies
            if isinstance(cookie, dict)
            and "epicgames.com" in str(cookie.get("domain", ""))
            and cookie.get("name") and cookie.get("value")
        ]
        names = {str(cookie["name"]) for cookie in epic_cookies}
        if not names.intersection({"EPIC_SESSION_AP", "EPIC_SSO", "EPIC_BEARER_TOKEN"}):
            return None

        version = cls._cdp_command(websocket_url, "Browser.getVersion")
        user_agent = str(version.get("userAgent") or "Mozilla/5.0")
        cookie_header = "; ".join(
            f"{cookie['name']}={cookie['value']}" for cookie in epic_cookies
        )
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Cookie": cookie_header,
            "Referer": "https://www.epicgames.com/",
            "User-Agent": user_agent,
        }
        xsrf = next((str(c["value"]) for c in epic_cookies if c["name"] == "XSRF-TOKEN"), None)
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
        request = Request(EPIC_REDIRECT_URL, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                text = response.read(1_000_000).decode("utf-8", errors="replace")
        except HTTPError as exc:
            text = exc.read(1_000_000).decode("utf-8", errors="replace")
            corrective_action = cls._extract_corrective_action(text)
            if corrective_action:
                raise EpicCorrectiveActionRequired(corrective_action) from exc
            raise
        return cls._extract_code(text)

    @classmethod
    def _open_epic_privacy_page(cls, websocket_url: str) -> None:
        cls._cdp_command(
            websocket_url,
            "Page.navigate",
            {"url": EPIC_STORE_URL},
        )

    @classmethod
    def _delete_epic_cookies(cls, websocket_url: str) -> int:
        cookie_result = cls._cdp_command(websocket_url, "Network.getAllCookies")
        raw_cookies = cookie_result.get("cookies", [])
        if not isinstance(raw_cookies, list):
            raise RuntimeError("Steam browser returned an invalid cookie list")

        epic_cookies = [
            cookie
            for cookie in raw_cookies
            if isinstance(cookie, dict)
            and cls._is_epic_cookie_domain(str(cookie.get("domain", "")))
            and isinstance(cookie.get("name"), str)
        ]
        for cookie in epic_cookies:
            params: dict[str, object] = {
                "name": cookie["name"],
                "domain": cookie["domain"],
                "path": cookie.get("path") or "/",
            }
            cls._cdp_command(websocket_url, "Network.deleteCookies", params)
        return len(epic_cookies)

    @classmethod
    def _delete_steamgriddb_data(cls, websocket_url: str) -> int:
        cookie_result = cls._cdp_command(websocket_url, "Network.getAllCookies")
        raw_cookies = cookie_result.get("cookies", [])
        if not isinstance(raw_cookies, list):
            raise RuntimeError("Steam browser returned an invalid cookie list")
        cookies = [
            cookie for cookie in raw_cookies
            if isinstance(cookie, dict)
            and cls._is_steamgriddb_cookie_domain(str(cookie.get("domain", "")))
            and isinstance(cookie.get("name"), str)
        ]
        for cookie in cookies:
            cls._cdp_command(websocket_url, "Network.deleteCookies", {
                "name": cookie["name"],
                "domain": cookie["domain"],
                "path": cookie.get("path") or "/",
            })
        for origin in ("https://steamgriddb.com", "https://www.steamgriddb.com"):
            cls._cdp_command(websocket_url, "Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": "local_storage,session_storage,indexeddb,cache_storage,service_workers",
            })
        return len(cookies)

    @staticmethod
    def _is_epic_cookie_domain(domain: str) -> bool:
        normalized = domain.lstrip(".").casefold()
        return normalized == "epicgames.com" or normalized.endswith(".epicgames.com")

    @staticmethod
    def _is_steamgriddb_cookie_domain(domain: str) -> bool:
        normalized = domain.lstrip(".").casefold()
        return normalized == "steamgriddb.com" or normalized.endswith(".steamgriddb.com")

    @staticmethod
    def _extract_code(text: str) -> str | None:
        try:
            payload = json.loads(text.strip())
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("authorizationCode") or payload.get("code")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _extract_corrective_action(text: str) -> str | None:
        try:
            payload = json.loads(text.strip())
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("errorCode") != "errors.com.epicgames.oauth.corrective_action_required":
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        action = metadata.get("correctiveAction")
        return action.strip() if isinstance(action, str) and action.strip() else None

    @staticmethod
    def _read_until(connection: socket.socket, marker: bytes) -> bytes:
        data = b""
        while marker not in data and len(data) < 65536:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _send_text(connection: socket.socket, text: str) -> None:
        payload = text.encode("utf-8")
        mask = secrets.token_bytes(4)
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header.extend((0x80 | 126, *struct.pack("!H", len(payload))))
        else:
            header.extend((0x80 | 127, *struct.pack("!Q", len(payload))))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        connection.sendall(bytes(header) + mask + masked)

    @staticmethod
    def _receive_text(connection: socket.socket) -> str:
        first, second = SteamBrowserAuthorization._recv_exact(connection, 2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", SteamBrowserAuthorization._recv_exact(connection, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", SteamBrowserAuthorization._recv_exact(connection, 8))[0]
        mask = SteamBrowserAuthorization._recv_exact(connection, 4) if second & 0x80 else None
        payload = SteamBrowserAuthorization._recv_exact(connection, length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if first & 0x0F == 0x8:
            raise RuntimeError("Steam browser debugger closed")
        return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _recv_exact(connection: socket.socket, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise RuntimeError("unexpected websocket EOF")
            data += chunk
        return data
