"""Thin async client for the WAHA (WhatsApp HTTP API) REST API.

WI-1. All requests funnel through a single ``WahaTransport`` seam so tests
can swap in ``FakeWaha`` (see tests/fake_waha.py) without an aiohttp test
server. 401 and 403 are never conflated (scaffold bug S3): WAHA returns 401
for a missing/wrong API key and 403 only for a scoped-key permission denial,
and only the former is a reauth case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from homeassistant.exceptions import HomeAssistantError

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
UPLOAD_TIMEOUT_SECONDS: Final = 30.0


class WahaError(HomeAssistantError):
    """Base class for all WahaClient errors."""


class WahaAuthError(WahaError):
    """Raised on HTTP 401 -- missing or invalid API key."""


class WahaPermissionError(WahaError):
    """Raised on HTTP 403 -- a scoped API key denied this call. Not a reauth case."""


class WahaConnectionError(WahaError):
    """Raised for network failures, timeouts, and any other unexpected error.

    ``ambiguous`` (default True) is what the send queue's retry logic keys
    off: a timeout or 5xx might have reached WAHA and actually sent, so it is
    never safely retryable. Only WahaUnreachableError below is provably safe
    to retry.
    """

    def __init__(self, *args: Any, ambiguous: bool = True) -> None:
        super().__init__(*args)
        self.ambiguous = ambiguous


class WahaUnreachableError(WahaConnectionError):
    """Raised only for a provable non-delivery (DNS failure / connection refused --
    the request never left the socket layer). The only error the send queue retries."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args, ambiguous=False)


class WahaRateLimitedError(WahaError):
    """Raised on HTTP 463/475 -- WAHA's anti-ban rate limiting.

    ``status`` distinguishes 463 (chat-scoped cooldown) from 475
    (session-wide cooldown). ``body`` is the raw response body -- its shape
    is unverified against a real server, so callers must not depend on it;
    only ``status`` drives behavior.
    """

    def __init__(self, status: int, body: Any, *args: Any) -> None:
        super().__init__(*args)
        self.status = status
        self.body = body


class WahaNotFoundError(WahaError):
    """Raised on HTTP 404 where the caller needs to distinguish it from other errors."""


@dataclass(slots=True)
class WahaResponse:
    """A normalized response from the transport seam."""

    status: int
    json_body: Any = None
    raw_body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class WahaTransport(Protocol):
    """The transport seam WahaClient talks to.

    Production: AiohttpWahaTransport (wraps homeassistant's shared aiohttp
    session). Tests: FakeWaha, implementing this protocol directly with no
    aiohttp test server involved.
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> WahaResponse:
        """Perform a request and return a normalized response."""


class AiohttpWahaTransport:
    """Production WahaTransport backed by Home Assistant's shared aiohttp session."""

    def __init__(self, session: Any, base_url: str, api_key: str) -> None:
        """Initialize the transport.

        ``session`` is an aiohttp.ClientSession, normally obtained via
        homeassistant.helpers.aiohttp_client.async_get_clientsession(hass).
        """
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> WahaResponse:
        """Perform an HTTP request against the WAHA server."""
        import asyncio

        import aiohttp

        req_headers = {"X-Api-Key": self._api_key, **(headers or {})}
        url = f"{self._base_url}{path}"
        client_timeout = aiohttp.ClientTimeout(total=timeout or DEFAULT_TIMEOUT_SECONDS)
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers=req_headers,
                timeout=client_timeout,
            ) as resp:
                raw = await resp.read()
                content_type = resp.headers.get("Content-Type", "")
                body: Any = None
                if "application/json" in content_type and raw:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = None
                return WahaResponse(
                    status=resp.status,
                    json_body=body,
                    raw_body=raw,
                    headers=dict(resp.headers),
                )
        except TimeoutError as err:
            # Ambiguous: the request may have reached WAHA before the timeout.
            raise WahaConnectionError(f"Timed out calling {method} {path}") from err
        except aiohttp.ClientConnectorError as err:
            # Provably never left the socket layer (DNS failure / connection
            # refused) -- the only case safe to retry.
            raise WahaUnreachableError(f"Could not reach WAHA for {method} {path}: {err}") from err
        except aiohttp.ClientError as err:
            raise WahaConnectionError(f"Error calling {method} {path}: {err}") from err
        except asyncio.CancelledError:
            raise


def mimetype_for(file_path: str, override: str | None = None) -> str:
    """Best-effort mimetype from a file extension, or an explicit override."""
    if override:
        return override
    import mimetypes

    guessed, _ = mimetypes.guess_type(file_path)
    return guessed or "application/octet-stream"


def normalize_groups(raw: Any) -> list[dict[str, Any]]:
    """Normalize WAHA's groups response into a list of {id, subject} dicts.

    WAHA's NOWEB engine returns a *dict* keyed by group id (scaffold bug S5:
    the old code's isinstance(result, list) check always returned []); WEBJS
    returns a *list*. The subject field name also varies (subject / name /
    groupMetadata.subject), and id can be a bare string or {_serialized}.
    """
    items: list[Any]
    if isinstance(raw, dict):
        items = []
        for key, value in raw.items():
            entry = dict(value) if isinstance(value, dict) else {}
            entry.setdefault("id", key)
            items.append(entry)
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if isinstance(raw_id, dict):
            group_id = raw_id.get("_serialized") or raw_id.get("id")
        else:
            group_id = raw_id
        if not group_id:
            continue
        subject = (
            item.get("subject")
            or item.get("name")
            or (item.get("groupMetadata") or {}).get("subject")
            or group_id
        )
        normalized.append({"id": group_id, "subject": subject})
    return normalized


class WahaClient:
    """Async client for the subset of the WAHA REST API milestone 1 needs."""

    def __init__(self, transport: WahaTransport, session_name: str) -> None:
        """Initialize the client for a single WAHA session."""
        self._transport = transport
        self.session_name = session_name

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        not_found_ok: bool = False,
    ) -> WahaResponse:
        """Make a call and raise the appropriate WahaError on non-2xx."""
        resp = await self._transport.request(
            method, path, json=json, params=params, headers=headers, timeout=timeout
        )
        if resp.status == 401:
            raise WahaAuthError(f"{method} {path}: unauthorized (401)")
        if resp.status == 403:
            raise WahaPermissionError(f"{method} {path}: forbidden (403)")
        if resp.status == 404 and not_found_ok:
            return resp
        if resp.status == 404:
            raise WahaNotFoundError(f"{method} {path}: not found (404)")
        if resp.status in (463, 475):
            raise WahaRateLimitedError(resp.status, resp.json_body, f"{method} {path}: HTTP {resp.status}")
        if resp.status >= 400:
            raise WahaConnectionError(f"{method} {path}: HTTP {resp.status}")
        return resp

    # --- sessions ---

    async def get_sessions(self, *, all_: bool = False) -> list[dict[str, Any]]:
        """GET /api/sessions -- running-only unless all_=True."""
        params = {"all": "true"} if all_ else None
        resp = await self._call("GET", "/api/sessions", params=params)
        return resp.json_body or []

    async def get_session(self, name: str | None = None) -> dict[str, Any] | None:
        """GET /api/sessions/{name}. Returns None on 404 rather than raising."""
        resp = await self._call(
            "GET", f"/api/sessions/{name or self.session_name}", not_found_ok=True
        )
        if resp.status == 404:
            return None
        return resp.json_body or {}

    async def create_session(
        self,
        name: str | None = None,
        *,
        config: dict[str, Any] | None = None,
        start: bool = True,
    ) -> dict[str, Any]:
        """POST /api/sessions -- create a session, optionally with webhook config."""
        body: dict[str, Any] = {"name": name or self.session_name, "start": start}
        if config is not None:
            body["config"] = config
        resp = await self._call("POST", "/api/sessions", json=body)
        return resp.json_body or {}

    async def update_session(
        self, name: str | None, config: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT /api/sessions/{name} -- full replace of config. Restarts a WORKING session."""
        resp = await self._call(
            "PUT", f"/api/sessions/{name or self.session_name}", json={"config": config}
        )
        return resp.json_body or {}

    async def start_session(self, name: str | None = None) -> dict[str, Any]:
        """POST /api/sessions/{name}/start."""
        resp = await self._call("POST", f"/api/sessions/{name or self.session_name}/start")
        return resp.json_body or {}

    # --- linking ---

    async def get_qr(self, name: str | None = None) -> bytes:
        """GET /api/{session}/auth/qr, Accept: image/png. NOWEB-specific (not /api/screenshot)."""
        resp = await self._call(
            "GET",
            f"/api/{name or self.session_name}/auth/qr",
            headers={"Accept": "image/png"},
        )
        return resp.raw_body

    async def request_pairing_code(
        self, phone_number: str, name: str | None = None
    ) -> str:
        """POST /api/{session}/auth/request-code. Returns the pairing code."""
        resp = await self._call(
            "POST",
            f"/api/{name or self.session_name}/auth/request-code",
            json={"phoneNumber": phone_number},
        )
        body = resp.json_body or {}
        return body.get("code", "")

    # --- groups (client + normalization built now per build-sequence step 1;
    # the services that consume this, e.g. name resolution, are milestone 2) ---

    async def get_groups(self, name: str | None = None) -> list[dict[str, Any]]:
        """GET /api/{session}/groups, normalized to a list of {id, subject}."""
        resp = await self._call("GET", f"/api/{name or self.session_name}/groups")
        return normalize_groups(resp.json_body)

    # --- sending (WI-5/WI-6). Session is passed in the JSON body, matching
    # WAHA's send endpoints -- not in the URL path like the other endpoints
    # above. Paths/body shapes here are best-effort from the design doc and
    # WAHA's public docs; not yet verified against a live server (see the
    # milestone 2 log). ---

    async def send_seen(self, chat_id: str, *, session: str | None = None) -> None:
        """POST /api/sendSeen -- best-effort, only called when replying."""
        await self._call(
            "POST", "/api/sendSeen", json={"session": session or self.session_name, "chatId": chat_id}
        )

    async def start_typing(self, chat_id: str, *, session: str | None = None) -> None:
        """POST /api/startTyping."""
        await self._call(
            "POST", "/api/startTyping", json={"session": session or self.session_name, "chatId": chat_id}
        )

    async def stop_typing(self, chat_id: str, *, session: str | None = None) -> None:
        """POST /api/stopTyping."""
        await self._call(
            "POST", "/api/stopTyping", json={"session": session or self.session_name, "chatId": chat_id}
        )

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        link_preview: bool = False,
        session: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/sendText. Returns WAHA's response body (carries the real message id)."""
        body: dict[str, Any] = {
            "session": session or self.session_name,
            "chatId": chat_id,
            "text": text,
            "linkPreview": link_preview,
        }
        if reply_to:
            body["reply_to"] = reply_to
        resp = await self._call("POST", "/api/sendText", json=body)
        return resp.json_body or {}

    async def _send_media(
        self,
        endpoint: str,
        chat_id: str,
        *,
        file_path: str | None = None,
        file_base64: str | None = None,
        url: str | None = None,
        mimetype: str | None = None,
        filename: str | None = None,
        caption: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Shared body-building for POST /api/send{Image,File,Voice,Video,Sticker}.

        `url` is passed straight through in `file.url` for WAHA to fetch on
        its own network -- HA never issues that fetch itself (SSRF guard).
        """
        file_obj: dict[str, Any] = {}
        if url:
            file_obj["url"] = url
        if file_base64:
            file_obj["data"] = file_base64
            file_obj["mimetype"] = mimetype or "application/octet-stream"
        if filename:
            file_obj["filename"] = filename
        body: dict[str, Any] = {
            "session": session or self.session_name,
            "chatId": chat_id,
            "file": file_obj,
        }
        if caption:
            body["caption"] = caption
        resp = await self._call(
            "POST", endpoint, json=body, timeout=UPLOAD_TIMEOUT_SECONDS if file_base64 else None
        )
        return resp.json_body or {}

    async def send_image(self, chat_id: str, **kwargs: Any) -> dict[str, Any]:
        """POST /api/sendImage."""
        return await self._send_media("/api/sendImage", chat_id, **kwargs)

    async def send_file(self, chat_id: str, **kwargs: Any) -> dict[str, Any]:
        """POST /api/sendFile."""
        return await self._send_media("/api/sendFile", chat_id, **kwargs)

    async def send_voice(self, chat_id: str, **kwargs: Any) -> dict[str, Any]:
        """POST /api/sendVoice."""
        return await self._send_media("/api/sendVoice", chat_id, **kwargs)

    async def send_video(self, chat_id: str, **kwargs: Any) -> dict[str, Any]:
        """POST /api/sendVideo."""
        return await self._send_media("/api/sendVideo", chat_id, **kwargs)

    async def send_sticker(self, chat_id: str, **kwargs: Any) -> dict[str, Any]:
        """POST /api/sendSticker."""
        return await self._send_media("/api/sendSticker", chat_id, **kwargs)

    async def send_poll(
        self,
        chat_id: str,
        question: str,
        options: list[str],
        *,
        multiple_answers: bool = False,
        session: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/sendPoll."""
        resp = await self._call(
            "POST",
            "/api/sendPoll",
            json={
                "session": session or self.session_name,
                "chatId": chat_id,
                "poll": {"name": question, "options": options, "multipleAnswers": multiple_answers},
            },
        )
        return resp.json_body or {}

    async def send_location(
        self,
        chat_id: str,
        latitude: float,
        longitude: float,
        *,
        name: str | None = None,
        address: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/sendLocation."""
        body: dict[str, Any] = {
            "session": session or self.session_name,
            "chatId": chat_id,
            "latitude": latitude,
            "longitude": longitude,
        }
        if name:
            body["name"] = name
        if address:
            body["address"] = address
        resp = await self._call("POST", "/api/sendLocation", json=body)
        return resp.json_body or {}

    async def send_reaction(
        self, message_id: str, emoji: str, *, session: str | None = None
    ) -> dict[str, Any]:
        """PUT /api/reaction. An empty emoji string removes a reaction (WAHA convention)."""
        resp = await self._call(
            "PUT",
            "/api/reaction",
            json={"session": session or self.session_name, "messageId": message_id, "reaction": emoji},
        )
        return resp.json_body or {}

    async def get_timelock(self, name: str | None = None) -> dict[str, Any]:
        """GET /api/{session}/timelock -- me.reachoutTimelock, surfaced in diagnostics."""
        resp = await self._call("GET", f"/api/{name or self.session_name}/timelock")
        return resp.json_body or {}

    async def get_capping(self, name: str | None = None) -> dict[str, Any]:
        """GET /api/{session}/capping -- me.messageCapping, surfaced in diagnostics."""
        resp = await self._call("GET", f"/api/{name or self.session_name}/capping")
        return resp.json_body or {}
