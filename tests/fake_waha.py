"""FakeWaha: a WahaTransport test double that behaves like the real WAHA server.

Implements the WahaTransport protocol directly (api.WahaTransport) -- no
aiohttp test server is involved. It is deliberately faithful to WAHA's real
full-replace-on-PUT semantics (S1/WI-2) so that a bug in ensure_webhook's
deep-copy-and-replace-only-ours logic is actually visible in assertions: a
naive implementation will show noweb.store/proxy/metadata/foreign hooks
*missing* from FakeWaha.sessions[name] after a PUT, not just "the test
fixture happened not to include them".
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json as json_module
from collections import deque
from typing import Any

from custom_components.whatsapp_waha.api import WahaConnectionError, WahaResponse, WahaUnreachableError

_SEND_PATHS = (
    "/api/sendText",
    "/api/sendImage",
    "/api/sendFile",
    "/api/sendVoice",
    "/api/sendVideo",
    "/api/sendSticker",
    "/api/sendPoll",
    "/api/sendLocation",
)

_QR_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-qr-bytes"


def _default_session_body(name: str, status: str) -> dict[str, Any]:
    """A realistic full GET /api/sessions/{name} body, not a minimal fixture."""
    return {
        "name": name,
        "status": status,
        "config": {
            "webhooks": [],
            "noweb": {"store": {"enabled": True, "fullSync": False}},
            "proxy": {"server": "socks5://10.0.0.1:1080", "username": "proxyuser"},
            "metadata": {"owner": "test-owner", "created": "2026-01-01"},
        },
        "me": {"id": "27821234567@c.us", "pushName": "Test User"},
        "engine": {"engine": "NOWEB"},
    }


class FakeWaha:
    """A configurable, call-recording WahaTransport double."""

    def __init__(
        self,
        *,
        auth_mode: str = "ok",  # "ok" | "unauthorized" | "forbidden"
        groups_shape: str = "dict",  # "dict" (NOWEB) | "list" (WEBJS)
        supports_safe_merge_put: bool = True,
        corrupt_put_hmac: bool = False,
    ) -> None:
        self.auth_mode = auth_mode
        self.groups_shape = groups_shape
        self.supports_safe_merge_put = supports_safe_merge_put
        # Simulates an unexpected server-side quirk that silently mangles a
        # PUT so the post-PUT re-GET-and-compare in webhook_registration.py
        # catches it (Verification item 4's "post-PUT mismatch -> repair, no
        # loop"), independent of the supports_safe_merge_put knob.
        self.corrupt_put_hmac = corrupt_put_hmac
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.put_count = 0
        self.qr_bytes = _QR_PNG_BYTES
        self.pairing_code = "ABCD-1234"
        self._send_outcomes: deque[str] = deque()
        self._send_message_counter = 0
        self.timelock: dict[str, Any] = {}
        self.capping: dict[str, Any] = {}

    # --- test setup helpers ---

    def add_session(self, name: str, status: str = "WORKING") -> dict[str, Any]:
        """Seed a session with a realistic full body; returns it for mutation."""
        body = _default_session_body(name, status)
        self.sessions[name] = body
        return body

    def set_groups(self, name: str, groups: list[dict[str, Any]]) -> None:
        """Seed the groups response for a session, in the shape groups_shape expects."""
        self._groups_by_session = getattr(self, "_groups_by_session", {})
        if self.groups_shape == "dict":
            self._groups_by_session[name] = {
                g["id"]: {"subject": g["subject"]} for g in groups
            }
        else:
            self._groups_by_session[name] = [
                {"id": {"_serialized": g["id"]}, "subject": g["subject"]} for g in groups
            ]

    def queue_send_outcomes(self, outcomes: list[str]) -> None:
        """Script the outcome of the next N send/typing/reaction calls, in order.
        One of "ok" (default once the queue is empty) | "timeout" | "5xx" |
        "connect_refused" | "463" | "475". Consumed one per matching call."""
        self._send_outcomes.extend(outcomes)

    def simulate_webhook(
        self, event: str, payload: dict[str, Any], session: str, hmac_key: str
    ) -> tuple[bytes, str]:
        """Build a signed raw body + X-Webhook-Hmac header value for a webhook POST."""
        body = {"event": event, "session": session, "payload": payload, "id": payload.get("id", "env-1")}
        raw = json_module.dumps(body).encode()
        sig = hmac.new(hmac_key.encode(), raw, hashlib.sha512).hexdigest()
        return raw, sig

    # --- WahaTransport protocol ---

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
        self.calls.append((method, path, json))

        if self.auth_mode == "unauthorized":
            return WahaResponse(status=401, json_body={"message": "Unauthorized"})
        if self.auth_mode == "forbidden":
            return WahaResponse(status=403, json_body={"message": "Forbidden"})

        if path == "/api/sessions" and method == "GET":
            all_ = bool(params and params.get("all") == "true")
            sessions = list(self.sessions.values())
            if not all_:
                sessions = [s for s in sessions if s["status"] == "WORKING"]
            return WahaResponse(status=200, json_body=sessions)

        if path == "/api/sessions" and method == "POST":
            name = json["name"]
            if name in self.sessions:
                return WahaResponse(status=422, json_body={"message": "already exists"})
            status = "SCAN_QR_CODE" if json.get("start", True) else "STOPPED"
            body = _default_session_body(name, status)
            if "config" in json:
                body["config"].update(json["config"])
            self.sessions[name] = body
            return WahaResponse(status=201, json_body=body)

        if path.startswith("/api/sessions/") and method == "GET":
            name = path.removeprefix("/api/sessions/")
            session = self.sessions.get(name)
            if session is None:
                return WahaResponse(status=404, json_body={"message": "not found"})
            return WahaResponse(status=200, json_body=copy.deepcopy(session))

        if path.startswith("/api/sessions/") and method == "PUT":
            name = path.removeprefix("/api/sessions/")
            session = self.sessions.get(name)
            if session is None:
                return WahaResponse(status=404, json_body={"message": "not found"})
            if not self.supports_safe_merge_put:
                # Simulate a server that redacts secrets on GET: a PUT of a
                # GET-derived body wipes proxy/hmac fields the caller never
                # actually saw.
                new_config = copy.deepcopy(json["config"])
                new_config.setdefault("proxy", None)
            else:
                # Real WAHA semantics: PUT fully replaces `config`. Whatever
                # the caller sends is exactly what ends up stored -- if the
                # caller dropped a field, it's gone.
                new_config = copy.deepcopy(json["config"])
            was_working = session["status"] == "WORKING"
            session["config"] = new_config
            if was_working:
                session["status"] = "STARTING"  # PUT restarts a WORKING session
            self.put_count += 1
            if self.corrupt_put_hmac:
                for hook in session["config"].get("webhooks") or []:
                    if "hmac" in hook:
                        hook["hmac"] = {"key": hook["hmac"]["key"] + "-corrupted"}
            return WahaResponse(status=200, json_body=copy.deepcopy(session))

        if path.endswith("/start") and method == "POST":
            name = path.removeprefix("/api/sessions/").removesuffix("/start")
            session = self.sessions.get(name)
            if session is None:
                return WahaResponse(status=404, json_body={"message": "not found"})
            session["status"] = "STARTING"
            return WahaResponse(status=200, json_body=copy.deepcopy(session))

        if path.endswith("/auth/qr") and method == "GET":
            return WahaResponse(status=200, raw_body=self.qr_bytes, headers={"Content-Type": "image/png"})

        if path.endswith("/auth/request-code") and method == "POST":
            return WahaResponse(status=200, json_body={"code": self.pairing_code})

        if path.endswith("/groups") and method == "GET":
            name = path.removeprefix("/api/").removesuffix("/groups")
            groups = getattr(self, "_groups_by_session", {}).get(
                name, {} if self.groups_shape == "dict" else []
            )
            return WahaResponse(status=200, json_body=groups)

        if path == "/api/sendSeen" and method == "POST":
            return WahaResponse(status=200, json_body={})

        if path == "/api/startTyping" and method == "POST":
            return WahaResponse(status=200, json_body={})

        if path == "/api/stopTyping" and method == "POST":
            return WahaResponse(status=200, json_body={})

        if (path in _SEND_PATHS or path == "/api/reaction") and method in ("POST", "PUT"):
            return self._handle_send_call(method, path)

        if path.endswith("/timelock") and method == "GET":
            return WahaResponse(status=200, json_body=self.timelock)

        if path.endswith("/capping") and method == "GET":
            return WahaResponse(status=200, json_body=self.capping)

        return WahaResponse(status=404, json_body={"message": f"unhandled path {path}"})

    def _handle_send_call(self, method: str, path: str) -> WahaResponse:
        """Apply the next scripted outcome (if any) to a send/reaction call."""
        outcome = self._send_outcomes.popleft() if self._send_outcomes else "ok"
        if outcome == "timeout":
            raise WahaConnectionError(f"Timed out calling {method} {path}")
        if outcome == "connect_refused":
            raise WahaUnreachableError(f"Could not reach WAHA for {method} {path}")
        if outcome == "5xx":
            return WahaResponse(status=500, json_body={"message": "Internal Server Error"})
        if outcome == "463":
            return WahaResponse(status=463, json_body={"message": "rate limit (chat)"})
        if outcome == "475":
            return WahaResponse(status=475, json_body={"message": "rate limit (session)"})
        self._send_message_counter += 1
        return WahaResponse(status=200, json_body={"id": f"fake-msg-{self._send_message_counter}"})
