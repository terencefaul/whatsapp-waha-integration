"""Tests for the register_webhook/unregister_webhook/request_pairing_code services
(registered once in async_setup, S10) and _resolve_entry's config_entry_id handling."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_PHONE_NUMBER,
    CONF_HMAC_KEY,
    CONF_SESSION,
    DOMAIN,
    SERVICE_REGISTER_WEBHOOK,
    SERVICE_REQUEST_PAIRING_CODE,
    SERVICE_UNREGISTER_WEBHOOK,
)
from tests.fake_waha import FakeWaha

BASE_DATA = {
    "host": "192.168.0.40",
    "port": 3000,
    "ssl": False,
    "api_key": "key",
    CONF_SESSION: "default",
    CONF_HMAC_KEY: "hmac-key",
    "webhook_id": "whatsapp_waha_test",
}


async def _setup_loaded_entry(hass: HomeAssistant, fake: FakeWaha) -> MockConfigEntry:
    """Set up a real, fully-loaded config entry backed by FakeWaha.

    version=2 (WahaConfigFlow.VERSION): a version-1 entry would otherwise go
    through async_migrate_entry on setup, which always regenerates
    CONF_HMAC_KEY/CONF_WEBHOOK_ID fresh (S7) -- fine in general, but it would
    silently discard the exact values these tests seed and assert against.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.whatsapp_waha.AiohttpWahaTransport", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_register_webhook_service_forces_put_on_working_session(hass: HomeAssistant) -> None:
    """The service call is explicit consent -- it force-PUTs even onto a WORKING session."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")  # drift: no hook yet
    entry = await _setup_loaded_entry(hass, fake)
    puts_before = fake.put_count

    with patch("custom_components.whatsapp_waha.webhook_callback_url", return_value="http://ha:8123/api/webhook/whatsapp_waha_test"):
        response = await hass.services.async_call(
            DOMAIN, SERVICE_REGISTER_WEBHOOK, {}, blocking=True, return_response=True
        )

    assert response["outcome"] == "put_applied"
    assert fake.put_count == puts_before + 1


async def test_unregister_webhook_service_removes_hook(hass: HomeAssistant) -> None:
    """The unregister service removes this integration's hook, leaving foreign ones."""
    from custom_components.whatsapp_waha.webhook_registration import build_desired_hook

    callback_url = "http://ha:8123/api/webhook/whatsapp_waha_test"
    fake = FakeWaha()
    session = fake.add_session("default", status="STOPPED")
    # Seed a hook that exactly matches what ensure_webhook would build, so
    # setup's own reconcile pass sees no drift and doesn't consume a PUT.
    session["config"]["webhooks"] = [build_desired_hook(callback_url, "hmac-key")]

    with patch("custom_components.whatsapp_waha.webhook_callback_url", return_value=callback_url):
        entry = await _setup_loaded_entry(hass, fake)
        # Setup's own reconcile pass sees the hook already matches (same URL,
        # same key) -- so it doesn't PUT, and the unregister call right after
        # isn't caught by the 10-minute PUT lockout.
        assert fake.put_count == 0

        response = await hass.services.async_call(
            DOMAIN, SERVICE_UNREGISTER_WEBHOOK, {}, blocking=True, return_response=True
        )

    assert response["outcome"] == "put_applied"
    assert fake.sessions["default"]["config"]["webhooks"] == []


async def test_request_pairing_code_service_returns_code(hass: HomeAssistant) -> None:
    """The request_pairing_code service returns the code from the API."""
    fake = FakeWaha()
    fake.add_session("default", status="SCAN_QR_CODE")
    entry = await _setup_loaded_entry(hass, fake)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_REQUEST_PAIRING_CODE,
        {ATTR_PHONE_NUMBER: "+27821234567"},
        blocking=True,
        return_response=True,
    )

    assert response["code"] == fake.pairing_code


async def test_service_call_with_no_loaded_entry_raises_validation_error(hass: HomeAssistant) -> None:
    """Calling a service with zero loaded entries fails clearly, not with an AttributeError."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REQUEST_PAIRING_CODE,
            {ATTR_PHONE_NUMBER: "+27821234567"},
            blocking=True,
        )


async def test_service_call_with_multiple_entries_requires_config_entry_id(hass: HomeAssistant) -> None:
    """With more than one loaded entry, config_entry_id is required (A8)."""
    fake1, fake2 = FakeWaha(), FakeWaha()
    fake1.add_session("default", status="SCAN_QR_CODE")
    fake2.add_session("second", status="SCAN_QR_CODE")
    entry1 = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, version=2)
    entry2 = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_DATA, CONF_SESSION: "second", "webhook_id": "whatsapp_waha_second"},
        version=2,
    )
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    # Setting up the first entry also bootstraps the whatsapp_waha component
    # itself, at which point HA auto-sets-up any other already-registered,
    # not-yet-loaded entries for the same domain (entry2) -- so entry2 must
    # not be set up a second time explicitly, or it raises OperationNotAllowed.
    with patch("custom_components.whatsapp_waha.AiohttpWahaTransport", side_effect=[fake1, fake2]):
        assert await hass.config_entries.async_setup(entry1.entry_id)
        await hass.async_block_till_done()

    from homeassistant.config_entries import ConfigEntryState

    assert entry2.state is ConfigEntryState.LOADED

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REQUEST_PAIRING_CODE,
            {ATTR_PHONE_NUMBER: "+27821234567"},
            blocking=True,
        )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_REQUEST_PAIRING_CODE,
        {ATTR_PHONE_NUMBER: "+27821234567", ATTR_CONFIG_ENTRY_ID: entry2.entry_id},
        blocking=True,
        return_response=True,
    )
    assert response["code"] == fake2.pairing_code
