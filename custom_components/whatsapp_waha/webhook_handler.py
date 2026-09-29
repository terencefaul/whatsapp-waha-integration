"""WI-4: the inbound webhook handler.

Verifies the HMAC over the *raw* request bytes (WAHA signs the exact string
it sent -- re-serializing the parsed JSON before verifying would break the
signature on any whitespace/key-order difference), dedupes by envelope id,
fires bus events, and feeds the coordinator/connectivity-debounce/last-seen
sensor. Does no network I/O itself -- verify, dedupe, parse, fire, return.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ENVELOPE_DEDUPE_MAX_SIZE,
    ENVELOPE_DEDUPE_TTL_SECONDS,
    EVENT_MESSAGE_RECEIVED,
    EVENT_MESSAGE_SENT,
    HMAC_FAILURE_REPAIR_THRESHOLD,
    HMAC_FAILURE_WARNING_INTERVAL_SECONDS,
    HMAC_HEADER,
    WEBHOOK_EVENT_MESSAGE,
    WEBHOOK_EVENT_MESSAGE_ANY,
    WEBHOOK_EVENT_SESSION_STATUS,
)

if TYPE_CHECKING:
    from .coordinator import WahaCoordinator

_LOGGER = logging.getLogger(__name__)


def signal_last_webhook_received(entry_id: str) -> str:
    """Dispatcher signal name for the 'last webhook received' timestamp sensor."""
    return f"whatsapp_waha_last_webhook_received_{entry_id}"


def signal_hmac_failure(entry_id: str) -> str:
    """Dispatcher signal name fired on every HMAC verification failure."""
    return f"whatsapp_waha_hmac_failure_{entry_id}"


def signal_message_any_received(entry_id: str) -> str:
    """Dispatcher signal fired for every verified message.any with fromMe=True,
    independent of the fire_message_sent_events option -- this is what the
    webhook_unreachable echo-test correlates its self-probe against."""
    return f"whatsapp_waha_message_any_received_{entry_id}"


class EnvelopeDedupe:
    """In-memory, TTL-bounded dedupe of webhook envelope ids. Not persisted --
    WAHA's own retries end within minutes, so nothing meaningful survives a
    restart anyway."""

    def __init__(
        self,
        *,
        ttl_seconds: float = ENVELOPE_DEDUPE_TTL_SECONDS,
        max_size: int = ENVELOPE_DEDUPE_MAX_SIZE,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._seen: OrderedDict[str, float] = OrderedDict()

    def seen_before(self, envelope_id: str, *, now: float | None = None) -> bool:
        """Record this id and return True if it was already seen (and not expired)."""
        now = time.time() if now is None else now
        self._evict_expired(now)
        if envelope_id in self._seen:
            return True
        self._seen[envelope_id] = now
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        while self._seen:
            oldest_id, oldest_at = next(iter(self._seen.items()))
            if now - oldest_at <= self._ttl:
                break
            self._seen.pop(oldest_id)


@dataclass(slots=True)
class HmacFailureTracker:
    """Rate-limits the "bad signature" warning and trips a repair after enough failures."""

    count: int = 0
    last_warning_at: float = 0.0

    def record_failure(self, *, now: float | None = None) -> bool:
        """Returns True if a repair issue should be (re)raised now."""
        now = time.time() if now is None else now
        self.count += 1
        if now - self.last_warning_at >= HMAC_FAILURE_WARNING_INTERVAL_SECONDS:
            _LOGGER.warning(
                "Rejected a webhook delivery with a missing/invalid %s signature (%d so far)",
                HMAC_HEADER,
                self.count,
            )
            self.last_warning_at = now
        return self.count >= HMAC_FAILURE_REPAIR_THRESHOLD

    def reset(self) -> None:
        """Reset after a key rotation / repair fix."""
        self.count = 0
        self.last_warning_at = 0.0


def _verify_signature(raw: bytes, signature: str, hmac_key: str) -> bool:
    """Constant-time HMAC-SHA512 verification over the exact raw bytes received."""
    if not signature:
        return False
    expected = hmac_module.new(hmac_key.encode(), raw, hashlib.sha512).hexdigest()
    return hmac_module.compare_digest(expected, signature)


def _extract_sender(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Returns (sender_id, sender_phone, sender_lid) from a message payload.

    sender_id is `participant` for a group message, else `from`. A `@lid`
    sender has no stable phone number; sender_phone is only populated for a
    `@c.us` id.
    """
    sender_id = payload.get("participant") or payload.get("from")
    if not sender_id:
        return None, None, None
    if sender_id.endswith("@c.us"):
        return sender_id, sender_id.split("@", 1)[0], None
    if sender_id.endswith("@lid"):
        return sender_id, None, sender_id.split("@", 1)[0]
    return sender_id, None, None


def _rewrite_media_host(media: dict[str, Any] | None, base_url: str) -> dict[str, Any] | None:
    """Rewrite a media URL's host to the entry's configured base_url.

    WAHA builds media URLs from its own WAHA_BASE_URL env var, which is
    often wrong/unreachable from HA's perspective. HA never fetches the
    media itself either way -- this only fixes the URL a user/automation
    would use to fetch it (with X-Api-Key) themselves.
    """
    if not media or not media.get("url"):
        return media
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(media["url"])
    base_parts = urlsplit(base_url)
    rewritten = urlunsplit(
        (base_parts.scheme, base_parts.netloc, parts.path, parts.query, parts.fragment)
    )
    return {**media, "url": rewritten}


def build_bus_payload(entry_id: str, session: str, payload: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Build the whatsapp_waha_message_received/sent bus event payload."""
    sender_id, sender_phone, sender_lid = _extract_sender(payload)
    chat_id = payload.get("from")
    media = payload.get("media")
    return {
        "entry_id": entry_id,
        "session": session,
        "message_id": payload.get("id"),
        "chat_id": chat_id,
        "is_group": bool(chat_id and chat_id.endswith("@g.us")),
        "sender_id": sender_id,
        "sender_phone": sender_phone,
        "sender_lid": sender_lid,
        "sender_name": payload.get("notifyName") or payload.get("pushName") or payload.get("_data", {}).get("notifyName"),
        "body": payload.get("body"),
        "timestamp": payload.get("timestamp"),
        "has_media": bool(media),
        "media": _rewrite_media_host(media, base_url) if media else None,
        "reply_to_id": (payload.get("_data", {}).get("quotedMsg") or {}).get("id")
        if isinstance(payload.get("_data"), dict)
        else None,
    }


def create_webhook_handler(
    hass: HomeAssistant,
    entry_id: str,
    hmac_key: str,
    dedupe: EnvelopeDedupe,
    hmac_tracker: HmacFailureTracker,
    coordinator: "WahaCoordinator",
    base_url: str,
    *,
    fire_message_sent_events: bool,
    on_hmac_repair_threshold: Any = None,
):
    """Build a per-entry webhook handler closure.

    Registered via homeassistant.components.webhook.async_register(hass,
    DOMAIN, name, webhook_id, handler, allowed_methods=["POST"]).
    """

    async def handler(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        raw = await request.read()
        signature = request.headers.get(HMAC_HEADER, "")

        if not _verify_signature(raw, signature, hmac_key):
            should_repair = hmac_tracker.record_failure()
            if should_repair and on_hmac_repair_threshold is not None:
                on_hmac_repair_threshold()
            return web.Response(status=401)

        try:
            body = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Webhook delivery had a valid signature but invalid JSON")
            return web.Response(status=200)

        if not isinstance(body, dict):
            return web.Response(status=200)

        session = body.get("session")
        event = body.get("event")
        envelope_id = body.get("id")
        payload = body.get("payload") or {}

        async_dispatcher_send(hass, signal_last_webhook_received(entry_id), time.time())

        if envelope_id and dedupe.seen_before(str(envelope_id)):
            return web.Response(status=200)

        if event == WEBHOOK_EVENT_SESSION_STATUS:
            new_status = payload.get("status")
            if new_status:
                coordinator.push_status_update(new_status)
            return web.Response(status=200)

        if event == WEBHOOK_EVENT_MESSAGE:
            if payload.get("fromMe"):
                # Nothing fires "received" from our own message -- an
                # automation must never be able to answer itself.
                return web.Response(status=200)
            hass.bus.async_fire(
                EVENT_MESSAGE_RECEIVED, build_bus_payload(entry_id, session, payload, base_url)
            )
            return web.Response(status=200)

        if event == WEBHOOK_EVENT_MESSAGE_ANY:
            if payload.get("fromMe"):
                async_dispatcher_send(hass, signal_message_any_received(entry_id), payload)
                if fire_message_sent_events:
                    hass.bus.async_fire(
                        EVENT_MESSAGE_SENT, build_bus_payload(entry_id, session, payload, base_url)
                    )
            return web.Response(status=200)

        # Unknown event: retry cannot help, so 200.
        return web.Response(status=200)

    return handler
