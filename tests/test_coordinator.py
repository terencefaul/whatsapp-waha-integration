"""Tests for coordinator.py: 401 -> ConfigEntryAuthFailed, network error -> ConfigEntryNotReady."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import DOMAIN
from custom_components.whatsapp_waha.coordinator import WahaCoordinator
from tests.fake_waha import FakeWaha


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="WAHA")
    entry.add_to_hass(hass)
    return entry


async def test_update_success_returns_session_status(hass: HomeAssistant) -> None:
    """A normal refresh returns the session's status."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = _entry(hass)
    coordinator = WahaCoordinator(hass, entry, WahaClient(fake, "default"))

    data = await coordinator._async_update_data()

    assert data.status == "WORKING"


async def test_401_raises_config_entry_auth_failed(hass: HomeAssistant) -> None:
    """A 401 from the API surfaces as ConfigEntryAuthFailed, not a generic failure."""
    fake = FakeWaha(auth_mode="unauthorized")
    entry = _entry(hass)
    coordinator = WahaCoordinator(hass, entry, WahaClient(fake, "default"))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_missing_session_raises_update_failed(hass: HomeAssistant) -> None:
    """A session that no longer exists is a fetch failure, not a crash."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    fake = FakeWaha()
    entry = _entry(hass)
    coordinator = WahaCoordinator(hass, entry, WahaClient(fake, "default"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_first_refresh_with_401_raises_config_entry_auth_failed(hass: HomeAssistant) -> None:
    """On first setup, a 401 propagates as ConfigEntryAuthFailed (not wrapped in ConfigEntryNotReady)."""
    fake = FakeWaha(auth_mode="unauthorized")
    entry = _entry(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    coordinator = WahaCoordinator(hass, entry, WahaClient(fake, "default"))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_config_entry_first_refresh()


async def test_push_status_update_sets_data_immediately(hass: HomeAssistant) -> None:
    """The webhook handler's session.status push updates coordinator.data without waiting for a poll."""
    fake = FakeWaha()
    fake.add_session("default", status="STARTING")
    entry = _entry(hass)
    coordinator = WahaCoordinator(hass, entry, WahaClient(fake, "default"))
    await coordinator.async_refresh()
    assert coordinator.data.status == "STARTING"

    coordinator.push_status_update("WORKING")

    assert coordinator.data.status == "WORKING"
