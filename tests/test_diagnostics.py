"""Tests for diagnostics.py (WI-8) -- calling the module function directly,
matching this repo's existing style rather than needing a diagnostics-platform
test fixture."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import CONF_SESSION, DOMAIN
from custom_components.whatsapp_waha.coordinator import WahaCoordinator, WahaRuntimeData
from custom_components.whatsapp_waha.diagnostics import async_get_config_entry_diagnostics
from custom_components.whatsapp_waha.send_queue import SendQueue
from tests.fake_waha import FakeWaha

BASE_DATA = {
    "host": "192.168.0.40",
    "port": 3000,
    "ssl": False,
    "api_key": "super-secret-key",
    CONF_SESSION: "default",
    "hmac_key": "super-secret-hmac",
    "webhook_id": "whatsapp_waha_abc123",
}


def _entry_with_runtime(hass: HomeAssistant, fake: FakeWaha) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, version=2)
    entry.add_to_hass(hass)
    client = WahaClient(fake, "default")
    coordinator = WahaCoordinator(hass, entry, client)
    send_queue = SendQueue(client, "default", status_fn=lambda: "WORKING")
    entry.runtime_data = WahaRuntimeData(
        client=client,
        coordinator=coordinator,
        send_queue=send_queue,
        last_webhook_registration=("unchanged", 1_000_000.0),
    )
    return entry


async def test_secrets_are_redacted(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = _entry_with_runtime(hass, fake)
    await entry.runtime_data.coordinator.async_refresh()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    dumped = str(diagnostics["config_entry_data"])
    assert "super-secret-key" not in dumped
    assert "super-secret-hmac" not in dumped
    assert "whatsapp_waha_abc123" not in dumped


async def test_me_id_is_redacted(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = _entry_with_runtime(hass, fake)
    await entry.runtime_data.coordinator.async_refresh()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["me"]["id"] == "**REDACTED**"


async def test_queue_and_webhook_state_present(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = _entry_with_runtime(hass, fake)
    await entry.runtime_data.coordinator.async_refresh()
    entry.runtime_data.send_queue.enqueue("text", "1@c.us", {"text": "queued but never processed"})

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["session_status"] == "WORKING"
    assert diagnostics["webhook_registration"] == ("unchanged", 1_000_000.0)
    assert diagnostics["queue"]["normal_lane_depth"] == 1


async def test_timelock_and_capping_included(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    fake.timelock = {"reachoutTimelock": 0}
    fake.capping = {"messageCapping": "low"}
    entry = _entry_with_runtime(hass, fake)
    await entry.runtime_data.coordinator.async_refresh()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["reachout_timelock"] == {"reachoutTimelock": 0}
    assert diagnostics["message_capping"] == {"messageCapping": "low"}
