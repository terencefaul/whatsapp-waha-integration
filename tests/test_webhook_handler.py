"""Tests for webhook_handler.py (WI-4, Verification item 3)."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import DOMAIN, EVENT_MESSAGE_RECEIVED, EVENT_MESSAGE_SENT
from custom_components.whatsapp_waha.coordinator import WahaCoordinator
from custom_components.whatsapp_waha.webhook_handler import (
    EnvelopeDedupe,
    HmacFailureTracker,
    create_webhook_handler,
)
from tests.fake_waha import FakeWaha

HMAC_KEY = "test-hmac-key"
ENTRY_ID = "test-entry-id"
BASE_URL = "http://homeassistant.local:8123"


class _FakeRequest:
    """A minimal aiohttp.web.Request stand-in -- handler only calls .read() and .headers."""

    def __init__(self, raw: bytes, headers: dict[str, str]) -> None:
        self._raw = raw
        self.headers = headers

    async def read(self) -> bytes:
        return self._raw


def _build_handler(hass: HomeAssistant, *, fire_sent: bool = False):
    fake = FakeWaha()
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="WAHA")
    entry.add_to_hass(hass)
    coordinator = WahaCoordinator(hass, entry, WahaClient(fake, "default"))
    handler = create_webhook_handler(
        hass,
        ENTRY_ID,
        HMAC_KEY,
        EnvelopeDedupe(),
        HmacFailureTracker(),
        coordinator,
        BASE_URL,
        fire_message_sent_events=fire_sent,
    )
    return handler, fake, coordinator


async def test_correct_hmac_fires_event(hass: HomeAssistant) -> None:
    """A correctly signed `message` delivery fires whatsapp_waha_message_received."""
    handler, fake, _ = _build_handler(hass)
    events = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, lambda e: events.append(e))
    raw, sig = fake.simulate_webhook(
        "message", {"id": "msg-1", "from": "27821234567@c.us", "body": "hi"}, "default", HMAC_KEY
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    response = await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert response.status == 200
    assert len(events) == 1
    assert events[0].data["chat_id"] == "27821234567@c.us"
    assert events[0].data["body"] == "hi"


async def test_missing_signature_returns_401_and_no_event(hass: HomeAssistant) -> None:
    """A missing X-Webhook-Hmac header -> 401, no event fired."""
    handler, fake, _ = _build_handler(hass)
    events = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, lambda e: events.append(e))
    raw, _sig = fake.simulate_webhook(
        "message", {"id": "msg-1", "from": "27821234567@c.us", "body": "hi"}, "default", HMAC_KEY
    )
    request = _FakeRequest(raw, {})

    response = await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert response.status == 401
    assert events == []


async def test_bad_signature_returns_401_and_no_event(hass: HomeAssistant) -> None:
    """A wrong X-Webhook-Hmac value -> 401, no event fired."""
    handler, fake, _ = _build_handler(hass)
    events = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, lambda e: events.append(e))
    raw, _sig = fake.simulate_webhook(
        "message", {"id": "msg-1", "from": "27821234567@c.us", "body": "hi"}, "default", HMAC_KEY
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": "0" * 128})

    response = await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert response.status == 401
    assert events == []


async def test_bad_json_with_valid_signature_returns_200(hass: HomeAssistant) -> None:
    """A correctly signed but unparsable body -> 200 (retry cannot help)."""
    import hashlib
    import hmac as hmac_module

    handler, _fake, _ = _build_handler(hass)
    raw = b"not-json{{{"
    sig = hmac_module.new(HMAC_KEY.encode(), raw, hashlib.sha512).hexdigest()
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    response = await handler(hass, "whatsapp_waha_test", request)

    assert response.status == 200


async def test_duplicate_envelope_id_fires_only_once(hass: HomeAssistant) -> None:
    """The same envelope id delivered twice (a WAHA retry) fires the event only once."""
    handler, fake, _ = _build_handler(hass)
    events = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, lambda e: events.append(e))
    raw, sig = fake.simulate_webhook(
        "message", {"id": "msg-dup", "from": "27821234567@c.us", "body": "hi"}, "default", HMAC_KEY
    )
    request1 = _FakeRequest(raw, {"X-Webhook-Hmac": sig})
    request2 = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    await handler(hass, "whatsapp_waha_test", request1)
    await handler(hass, "whatsapp_waha_test", request2)
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_from_me_message_does_not_fire_received(hass: HomeAssistant) -> None:
    """A `message` event with fromMe=True never fires received (an automation can't answer itself)."""
    handler, fake, _ = _build_handler(hass)
    events = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, lambda e: events.append(e))
    raw, sig = fake.simulate_webhook(
        "message",
        {"id": "msg-1", "from": "27821234567@c.us", "body": "hi", "fromMe": True},
        "default",
        HMAC_KEY,
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert events == []


async def test_message_any_from_me_fires_sent_when_opted_in(hass: HomeAssistant) -> None:
    """message.any + fromMe fires message_sent only when the opt-in option is enabled."""
    handler, fake, _ = _build_handler(hass, fire_sent=True)
    sent_events = []
    hass.bus.async_listen(EVENT_MESSAGE_SENT, lambda e: sent_events.append(e))
    raw, sig = fake.simulate_webhook(
        "message.any",
        {"id": "msg-1", "from": "27821234567@c.us", "body": "hi", "fromMe": True},
        "default",
        HMAC_KEY,
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert len(sent_events) == 1


async def test_message_any_from_me_default_off_fires_nothing(hass: HomeAssistant) -> None:
    """By default (opt-in off), message.any + fromMe fires nothing."""
    handler, fake, _ = _build_handler(hass, fire_sent=False)
    sent_events = []
    hass.bus.async_listen(EVENT_MESSAGE_SENT, lambda e: sent_events.append(e))
    raw, sig = fake.simulate_webhook(
        "message.any",
        {"id": "msg-1", "from": "27821234567@c.us", "body": "hi", "fromMe": True},
        "default",
        HMAC_KEY,
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert sent_events == []


async def test_session_status_event_pushes_coordinator_no_bus_event(hass: HomeAssistant) -> None:
    """session.status updates the coordinator directly and fires no bus event."""
    handler, fake, coordinator = _build_handler(hass)
    fake.add_session("default", status="STARTING")
    await coordinator.async_refresh()
    all_events = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, lambda e: all_events.append(e))
    raw, sig = fake.simulate_webhook(
        "session.status", {"id": "evt-1", "status": "WORKING"}, "default", HMAC_KEY
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    await handler(hass, "whatsapp_waha_test", request)
    await hass.async_block_till_done()

    assert coordinator.data.status == "WORKING"
    assert all_events == []


async def test_handler_makes_no_network_calls(hass: HomeAssistant) -> None:
    """The handler itself never calls the WAHA API -- verify, dedupe, parse, fire, return."""
    handler, fake, _ = _build_handler(hass)
    calls_before = list(fake.calls)
    raw, sig = fake.simulate_webhook(
        "message", {"id": "msg-1", "from": "27821234567@c.us", "body": "hi"}, "default", HMAC_KEY
    )
    request = _FakeRequest(raw, {"X-Webhook-Hmac": sig})

    await handler(hass, "whatsapp_waha_test", request)

    assert fake.calls == calls_before
