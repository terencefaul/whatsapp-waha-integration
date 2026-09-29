"""Tests for async_migrate_entry -- a version-1 (scaffold-shape) entry
migrates to version 2 with renamed keys and fresh HMAC/webhook_id."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha import async_migrate_entry
from custom_components.whatsapp_waha.const import CONF_HMAC_KEY, CONF_SESSION, DOMAIN


async def test_migrates_use_ssl_key_to_ssl(hass: HomeAssistant) -> None:
    """The scaffold's `use_ssl` key becomes CONF_SSL."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_HOST: "192.168.0.40",
            CONF_PORT: 3000,
            "use_ssl": True,
            CONF_API_KEY: "old-key",
            CONF_SESSION: "default",
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 2
    assert entry.data[CONF_SSL] is True
    assert "use_ssl" not in entry.data


async def test_migration_generates_fresh_hmac_and_webhook_id(hass: HomeAssistant) -> None:
    """The scaffold had no HMAC verification and a guessable webhook id (S7) --
    migration never trusts anything the old shape might have had for these."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_HOST: "192.168.0.40",
            CONF_PORT: 3000,
            CONF_API_KEY: "old-key",
            CONF_SESSION: "default",
            "webhook_id": "whatsapp_waha_test-entry-id",  # old guessable scheme
        },
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    assert CONF_HMAC_KEY in entry.data
    assert len(entry.data[CONF_HMAC_KEY]) > 20
    assert entry.data["webhook_id"] != "whatsapp_waha_test-entry-id"
    assert entry.data["webhook_id"].startswith("whatsapp_waha_")


async def test_migration_defaults_missing_port_and_session(hass: HomeAssistant) -> None:
    """Missing port/session default sensibly rather than crashing the migration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_HOST: "192.168.0.40", CONF_API_KEY: "old-key"},
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    assert entry.data[CONF_PORT] == 3000
    assert entry.data[CONF_SESSION] == "default"


async def test_already_current_version_is_a_no_op(hass: HomeAssistant) -> None:
    """An entry already at version 2 is left alone."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_HOST: "192.168.0.40",
            CONF_PORT: 3000,
            CONF_SSL: False,
            CONF_API_KEY: "key",
            CONF_SESSION: "default",
            CONF_HMAC_KEY: "existing-key",
            "webhook_id": "whatsapp_waha_existing",
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.data[CONF_HMAC_KEY] == "existing-key"
    assert entry.data["webhook_id"] == "whatsapp_waha_existing"
