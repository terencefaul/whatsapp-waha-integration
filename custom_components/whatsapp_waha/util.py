"""Small shared helpers used by config_flow, __init__, repairs, and entity.py."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_WEBHOOK_ID

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def base_url_for(data: Mapping[str, Any]) -> str:
    """The WAHA server's base URL, built from a config entry's data."""
    scheme = "https" if data.get(CONF_SSL) else "http"
    host = data[CONF_HOST]
    port = data.get(CONF_PORT)
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def normalize_host_port(data: Mapping[str, Any]) -> tuple[str, int | None]:
    """A normalized (host, port) tuple used for the config flow's duplicate check."""
    return (str(data[CONF_HOST]).strip().lower(), data.get(CONF_PORT))


def webhook_callback_url(hass: "HomeAssistant", entry: "ConfigEntry") -> str:
    """The URL this integration wants registered as its webhook.

    Recomputed fresh every time it's needed (WI-2: never cached), using the
    entry's callback_base_url override if HA has no resolvable URL of its
    own (see config_flow's NoURLAvailableError handling). Raises
    homeassistant.helpers.network.NoURLAvailableError if neither works.
    """
    from .const import CONF_CALLBACK_BASE_URL

    override = entry.data.get(CONF_CALLBACK_BASE_URL) or entry.options.get(CONF_CALLBACK_BASE_URL)
    if override:
        from homeassistant.components.webhook import async_generate_path

        return override.rstrip("/") + async_generate_path(entry.data[CONF_WEBHOOK_ID])

    from homeassistant.components.webhook import async_generate_url

    return async_generate_url(
        hass, entry.data[CONF_WEBHOOK_ID], allow_external=False, allow_ip=True
    )
