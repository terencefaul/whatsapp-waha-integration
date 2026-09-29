"""Tests for notify.py's dual pattern (WI-6/D8, Verification item 12).

The end-to-end proof (hass.services.async_call("notify", "whatsapp_waha", ...)
through to a real FakeWaha call, both the legacy service and the entity's
send_message) is also incidentally exercised by test_send_services.py's full
entry setup, which already showed notify.waha_default (entity) and
notify.whatsapp_waha (legacy service) both getting registered. These tests
target the module's pieces directly and the full round-trip explicitly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import (
    CONF_NOTIFY_LANE,
    CONF_NOTIFY_TARGET,
    CONF_SESSION,
    DOMAIN,
    LANE_ALERT,
    LANE_NORMAL,
)
from custom_components.whatsapp_waha import _notify_service_name
from custom_components.whatsapp_waha.coordinator import WahaCoordinator, WahaRuntimeData
from custom_components.whatsapp_waha.notify import WahaNotificationService
from custom_components.whatsapp_waha.send_queue import SendQueue
from tests.fake_waha import FakeWaha

BASE_DATA = {
    "host": "192.168.0.40",
    "port": 3000,
    "ssl": False,
    "api_key": "key",
    CONF_SESSION: "default",
    "hmac_key": "hmac-key",
    "webhook_id": "whatsapp_waha_test",
}


def _entry_with_runtime(hass: HomeAssistant, fake: FakeWaha, *, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, options=options or {}, version=2)
    entry.add_to_hass(hass)
    client = WahaClient(fake, "default")
    coordinator = WahaCoordinator(hass, entry, client)
    send_queue = SendQueue(client, "default", status_fn=lambda: "WORKING")
    entry.runtime_data = WahaRuntimeData(client=client, coordinator=coordinator, send_queue=send_queue)
    return entry


async def test_legacy_service_enqueues_with_title_and_message(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = _entry_with_runtime(hass, fake, options={CONF_NOTIFY_TARGET: "1@c.us"})
    service = WahaNotificationService(entry)

    await service.async_send_message("lockout detected", title="Gate Alert")

    assert entry.runtime_data.send_queue.alert_depth() + entry.runtime_data.send_queue.normal_depth() == 1


async def test_legacy_service_with_no_target_drops_silently_but_does_not_raise(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    entry = _entry_with_runtime(hass, fake)  # no CONF_NOTIFY_TARGET set
    service = WahaNotificationService(entry)

    await service.async_send_message("hello")  # must not raise

    assert entry.runtime_data.send_queue.normal_depth() == 0
    assert entry.runtime_data.send_queue.alert_depth() == 0


async def test_legacy_service_default_lane_is_alert(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    entry = _entry_with_runtime(hass, fake, options={CONF_NOTIFY_TARGET: "1@c.us"})
    service = WahaNotificationService(entry)

    await service.async_send_message("hello")

    assert entry.runtime_data.send_queue.alert_depth() == 1
    assert entry.runtime_data.send_queue.normal_depth() == 0


async def test_legacy_service_data_priority_overrides_lane(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    entry = _entry_with_runtime(
        hass, fake, options={CONF_NOTIFY_TARGET: "1@c.us", CONF_NOTIFY_LANE: LANE_ALERT}
    )
    service = WahaNotificationService(entry)

    await service.async_send_message("hello", data={"priority": LANE_NORMAL})

    assert entry.runtime_data.send_queue.normal_depth() == 1
    assert entry.runtime_data.send_queue.alert_depth() == 0


def test_notify_service_name_default_session_is_bare() -> None:
    entry = type("E", (), {"data": {CONF_SESSION: "default"}})()
    assert _notify_service_name(entry) == "whatsapp_waha"


def test_notify_service_name_other_session_is_suffixed() -> None:
    entry = type("E", (), {"data": {CONF_SESSION: "second phone"}})()
    assert _notify_service_name(entry) == "whatsapp_waha_second_phone"


async def test_full_round_trip_via_hass_services_async_call(hass: HomeAssistant) -> None:
    """gate-pin's exact call shape: hass.services.async_call("notify", "whatsapp_waha",
    {"title": ..., "message": ...}) reaches FakeWaha."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_DATA, options={CONF_NOTIFY_TARGET: "1@c.us"}, version=2
    )
    entry.add_to_hass(hass)

    with patch("custom_components.whatsapp_waha.AiohttpWahaTransport", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            "notify",
            "whatsapp_waha",
            {"title": "Gate Alert", "message": "Lockout detected"},
            blocking=True,
        )
        # Enqueueing just wakes the background worker task -- actually sending
        # happens asynchronously in its own run() loop, so poll briefly rather
        # than assuming async_block_till_done() drains a sleeping background task.
        await _wait_for_send_text(fake)

    send_calls = [c for c in fake.calls if c[1] == "/api/sendText"]
    assert len(send_calls) == 1
    assert "Lockout detected" in send_calls[0][2]["text"]
    assert "Gate Alert" in send_calls[0][2]["text"]


async def _wait_for_send_text(fake: FakeWaha, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not any(c[1] == "/api/sendText" for c in fake.calls):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)
