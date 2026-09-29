"""Tests for config_flow.py (WI-3, Verification item 2)."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.const import CONF_SESSION, DOMAIN
from tests.fake_waha import FakeWaha

USER_INPUT = {
    CONF_HOST: "192.168.0.40",
    CONF_PORT: 3000,
    CONF_SSL: False,
    CONF_API_KEY: "test-key",
    CONF_SESSION: "default",
}


def _patch_transport(fake: FakeWaha):
    """Patch AiohttpWahaTransport everywhere it's imported (config_flow's throwaway
    client, and __init__.py's real client, since creating an entry auto-triggers
    async_setup_entry in these tests) so nothing does a real network call."""
    stack = ExitStack()
    stack.enter_context(
        patch("custom_components.whatsapp_waha.config_flow.AiohttpWahaTransport", return_value=fake)
    )
    stack.enter_context(
        patch("custom_components.whatsapp_waha.AiohttpWahaTransport", return_value=fake)
    )
    return stack


async def _run_flow_to_session_step(hass: HomeAssistant, fake: FakeWaha):
    with _patch_transport(fake):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    return result


async def test_good_key_and_existing_session_creates_entry(hass: HomeAssistant) -> None:
    """A good key + already-existing session sails through to entry creation."""
    fake = FakeWaha()
    fake.add_session("default", status="STOPPED")

    with _patch_transport(fake), patch(
        "custom_components.whatsapp_waha.config_flow.webhook_generate_url",
        return_value="http://ha.local:8123/api/webhook/whatsapp_waha_x",
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.0.40"
    assert "hmac_key" in result["data"]
    assert result["data"]["webhook_id"].startswith("whatsapp_waha_")


async def test_401_shows_invalid_auth(hass: HomeAssistant) -> None:
    """A 401 from WAHA shows invalid_auth, never cannot_connect (fixes scaffold bug S3)."""
    fake = FakeWaha(auth_mode="unauthorized")

    result = await _run_flow_to_session_step(hass, fake)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_403_shows_insufficient_permissions(hass: HomeAssistant) -> None:
    """A 403 from WAHA shows insufficient_permissions, not invalid_auth (not a reauth case)."""
    fake = FakeWaha(auth_mode="forbidden")

    result = await _run_flow_to_session_step(hass, fake)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "insufficient_permissions"


async def test_connection_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """A network failure shows cannot_connect."""

    class _BrokenTransport:
        async def request(self, *args, **kwargs):
            from custom_components.whatsapp_waha.api import WahaConnectionError

            raise WahaConnectionError("boom")

    with patch(
        "custom_components.whatsapp_waha.config_flow.AiohttpWahaTransport",
        return_value=_BrokenTransport(),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["errors"]["base"] == "cannot_connect"


async def test_missing_session_offers_to_create_it(hass: HomeAssistant) -> None:
    """A missing session shows the create-session confirmation step."""
    fake = FakeWaha()  # no sessions seeded

    result = await _run_flow_to_session_step(hass, fake)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "session"


async def test_confirming_session_creation_creates_it_then_proceeds(hass: HomeAssistant) -> None:
    """Confirming the session step actually creates the session (plain, no webhook config)."""
    fake = FakeWaha()

    with _patch_transport(fake), patch(
        "custom_components.whatsapp_waha.config_flow.webhook_generate_url",
        return_value="http://ha.local:8123/api/webhook/whatsapp_waha_x",
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        assert result["step_id"] == "session"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "default" in fake.sessions
    # The session is created plain (no webhook config) by this step -- but
    # entry setup runs right after and its own ensure_webhook call attaches
    # the hook via the ordinary safe-PUT path, since a freshly created
    # session isn't WORKING yet. See plan step 9 / util.webhook_callback_url.
    assert len(fake.sessions["default"]["config"]["webhooks"]) == 1
    webhook_id = result["data"]["webhook_id"]
    assert fake.sessions["default"]["config"]["webhooks"][0]["url"].endswith(
        f"/api/webhook/{webhook_id}"
    )


async def test_duplicate_host_port_session_aborts(hass: HomeAssistant) -> None:
    """A duplicate (host:port, session) combination aborts as already_configured."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, "hmac_key": "x", "webhook_id": "whatsapp_waha_existing"},
        version=2,
    )
    existing.add_to_hass(hass)

    result = await _run_flow_to_session_step(hass, fake)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_url_available_shows_callback_url_step(hass: HomeAssistant) -> None:
    """NoURLAvailableError shows the callback_url step, and the value is stored on the entry."""
    fake = FakeWaha()
    fake.add_session("default", status="STOPPED")

    with _patch_transport(fake), patch(
        "custom_components.whatsapp_waha.config_flow.webhook_generate_url",
        side_effect=NoURLAvailableError,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        assert result["step_id"] == "callback_url"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_base_url": "http://192.168.1.5:8123"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["callback_base_url"] == "http://192.168.1.5:8123"


async def test_reauth_success_updates_api_key(hass: HomeAssistant) -> None:
    """Reauth with a working new key updates the entry and reloads it."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_API_KEY: "old-key", "hmac_key": "x", "webhook_id": "whatsapp_waha_x"},
        version=2,
    )
    entry.add_to_hass(hass)

    with _patch_transport(fake):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )
        # async_update_reload_and_abort schedules the entry reload as a
        # background task -- wait for it (and everything it spawns, e.g.
        # the QR entity's own background fetch) before the test ends, or
        # the harness's lingering-task check fails this test.
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"


async def test_reauth_with_bad_key_shows_invalid_auth(hass: HomeAssistant) -> None:
    """Reauth with a still-wrong key shows invalid_auth and does not update the entry."""
    fake = FakeWaha(auth_mode="unauthorized")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_API_KEY: "old-key", "hmac_key": "x", "webhook_id": "whatsapp_waha_x"},
        version=2,
    )
    entry.add_to_hass(hass)

    with _patch_transport(fake):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "still-wrong"}
        )

    assert result["errors"]["base"] == "invalid_auth"
    assert entry.data[CONF_API_KEY] == "old-key"


async def test_reconfigure_success_updates_entry(hass: HomeAssistant) -> None:
    """Reconfigure with a good key updates host/port/ssl/api_key and reloads."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, "hmac_key": "x", "webhook_id": "whatsapp_waha_x"},
        version=2,
    )
    entry.add_to_hass(hass)

    with _patch_transport(fake):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.0.99",
                CONF_PORT: 3000,
                CONF_SSL: False,
                CONF_API_KEY: "test-key",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.168.0.99"
    assert entry.data[CONF_SESSION] == "default"  # session is locked, not changed
