"""Tests for repairs.py: needs_link, webhook_drift, and hmac_mismatch fix flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import CONF_HMAC_KEY, CONF_SESSION, DOMAIN
from custom_components.whatsapp_waha.coordinator import WahaCoordinator, WahaRuntimeData
from custom_components.whatsapp_waha.repairs import (
    async_clear_passkey_required_issue,
    async_clear_send_rate_limited_issue,
    async_create_fix_flow,
    async_create_needs_link_issue,
    async_create_passkey_required_issue,
    async_create_send_rate_limited_issue,
    async_create_webhook_drift_issue,
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


def _entry_with_runtime(hass: HomeAssistant, fake: FakeWaha) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA)
    entry.add_to_hass(hass)
    client = WahaClient(fake, "default")
    coordinator = WahaCoordinator(hass, entry, client)
    entry.runtime_data = WahaRuntimeData(client=client, coordinator=coordinator)
    return entry


async def test_needs_link_flow_requests_pairing_code(hass: HomeAssistant) -> None:
    """The needs_link repair flow asks for a phone number and shows the pairing code."""
    fake = FakeWaha()
    fake.add_session("default", status="SCAN_QR_CODE")
    entry = _entry_with_runtime(hass, fake)
    async_create_needs_link_issue(hass, entry.entry_id)

    flow = await async_create_fix_flow(hass, f"needs_link_{entry.entry_id}", {"entry_id": entry.entry_id})
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["step_id"] == "phone_number"

    result = await flow.async_step_phone_number({"phone_number": "+27821234567"})
    assert result["step_id"] == "show_code"
    assert result["description_placeholders"]["code"] == fake.pairing_code


async def test_webhook_drift_confirm_forces_put(hass: HomeAssistant) -> None:
    """Confirming the webhook_drift repair force-PUTs the webhook onto a WORKING session."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")  # drift: no hook yet
    entry = _entry_with_runtime(hass, fake)
    async_create_webhook_drift_issue(hass, entry.entry_id, fixable=True)

    with patch(
        "custom_components.whatsapp_waha.repairs.webhook_callback_url",
        return_value="http://ha.local:8123/api/webhook/whatsapp_waha_test",
    ):
        flow = await async_create_fix_flow(
            hass, f"webhook_drift_{entry.entry_id}", {"entry_id": entry.entry_id}
        )
        flow.hass = hass
        result = await flow.async_step_init()
        assert result["step_id"] == "confirm"

        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.put_count == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"webhook_drift_{entry.entry_id}") is None


async def test_webhook_drift_unfixable_shows_info_and_aborts(hass: HomeAssistant) -> None:
    """An unfixable webhook_drift issue shows an info step and aborts, never PUTs."""
    fake = FakeWaha()
    entry = _entry_with_runtime(hass, fake)
    async_create_webhook_drift_issue(hass, entry.entry_id, fixable=False)

    flow = await async_create_fix_flow(
        hass, f"webhook_drift_{entry.entry_id}", {"entry_id": entry.entry_id}
    )
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["step_id"] == "unfixable_info"

    result = await flow.async_step_unfixable_info({})
    assert result["type"] is FlowResultType.ABORT
    assert fake.put_count == 0


# --- milestone 2: passkey_required + send_rate_limited (never fixable, no fix flow) ---


async def test_passkey_required_issue_create_and_clear(hass: HomeAssistant) -> None:
    async_create_passkey_required_issue(hass, "entry-1", "http://192.168.0.40:3000/dashboard")

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "passkey_required_entry-1")
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {"dashboard_url": "http://192.168.0.40:3000/dashboard"}

    async_clear_passkey_required_issue(hass, "entry-1")
    assert ir.async_get(hass).async_get_issue(DOMAIN, "passkey_required_entry-1") is None


async def test_send_rate_limited_issue_create_and_clear(hass: HomeAssistant) -> None:
    async_create_send_rate_limited_issue(hass, "entry-1")

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "send_rate_limited_entry-1")
    assert issue is not None
    assert issue.is_fixable is False

    async_clear_send_rate_limited_issue(hass, "entry-1")
    assert ir.async_get(hass).async_get_issue(DOMAIN, "send_rate_limited_entry-1") is None
