"""Config flow for whatsapp_waha (WI-3).

user -> session (create-if-missing) -> callback_url (only on NoURLAvailableError)
-> create entry. Also reauth (API key only) and reconfigure (full form, session
name locked -- push to a new entry instead of changing it mid-flow, since a
session-name change has webhook-ownership implications this design doesn't
cover) and a small options flow for the callback URL override and the
message_sent opt-in.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.webhook import async_generate_url as webhook_generate_url
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_SSL, CONF_WEBHOOK_ID
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.schema_config_entry_flow import SchemaFlowFormStep, SchemaOptionsFlowHandler
from homeassistant.helpers.selector import BooleanSelector, SelectSelector, SelectSelectorConfig

from .api import AiohttpWahaTransport, WahaAuthError, WahaClient, WahaConnectionError, WahaPermissionError
from .const import (
    CONF_AUTO_RESTART_ENABLED,
    CONF_CALLBACK_BASE_URL,
    CONF_FIRE_MESSAGE_SENT_EVENTS,
    CONF_HMAC_KEY,
    CONF_NORMAL_HOURLY_CAP,
    CONF_NOTIFY_LANE,
    CONF_NOTIFY_TARGET,
    CONF_SEND_QUEUE_PRESET,
    CONF_SESSION,
    DEFAULT_AUTO_RESTART_ENABLED,
    DEFAULT_FIRE_MESSAGE_SENT_EVENTS,
    DEFAULT_NOTIFY_LANE,
    DEFAULT_PORT,
    DEFAULT_SEND_QUEUE_PRESET,
    DEFAULT_SESSION,
    DOMAIN,
    LANE_ALERT,
    LANE_NORMAL,
    NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT,
    PRESET_BALANCED,
    PRESET_OFF,
    PRESET_STRICT,
    WEBHOOK_ID_PREFIX,
)
from .util import normalize_host_port

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=False): bool,
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_SESSION, default=DEFAULT_SESSION): str,
    }
)

OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(
        schema=vol.Schema(
            {
                vol.Optional(CONF_CALLBACK_BASE_URL): str,
                vol.Optional(
                    CONF_FIRE_MESSAGE_SENT_EVENTS, default=DEFAULT_FIRE_MESSAGE_SENT_EVENTS
                ): BooleanSelector(),
            }
        ),
        next_step="send_queue",
    ),
    # Milestone 2: notify target/lane, pacing preset, cap overrides, auto-restart.
    # A separate step rather than folding these into "init" -- six more fields
    # in one form would be hard to scan.
    "send_queue": SchemaFlowFormStep(
        schema=vol.Schema(
            {
                vol.Optional(CONF_NOTIFY_TARGET): str,
                vol.Optional(CONF_NOTIFY_LANE, default=DEFAULT_NOTIFY_LANE): SelectSelector(
                    SelectSelectorConfig(options=[LANE_NORMAL, LANE_ALERT])
                ),
                vol.Optional(CONF_SEND_QUEUE_PRESET, default=DEFAULT_SEND_QUEUE_PRESET): SelectSelector(
                    SelectSelectorConfig(options=[PRESET_OFF, PRESET_BALANCED, PRESET_STRICT])
                ),
                vol.Optional(
                    CONF_NORMAL_HOURLY_CAP, default=NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT
                ): int,
                vol.Optional(
                    CONF_AUTO_RESTART_ENABLED, default=DEFAULT_AUTO_RESTART_ENABLED
                ): BooleanSelector(),
            }
        ),
    ),
}


async def _build_client(hass: Any, data: Mapping[str, Any]) -> WahaClient:
    """Build a throwaway WahaClient for validation from raw flow input."""
    scheme = "https" if data.get(CONF_SSL) else "http"
    base_url = f"{scheme}://{data[CONF_HOST]}:{data[CONF_PORT]}"
    transport = AiohttpWahaTransport(async_get_clientsession(hass), base_url, data[CONF_API_KEY])
    return WahaClient(transport, data[CONF_SESSION])


class WahaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for whatsapp_waha.

    VERSION = 2: the broken scaffold's entries (never explicitly versioned,
    so HA defaults them to 1) used different key names (`use_ssl` instead of
    `ssl`, no `hmac_key`/`webhook_id`). Bumping to 2 lets HA's normal
    version-triggered migration call async_migrate_entry in __init__.py for
    any such entry, rather than relying on data shape alone to detect it.
    """

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow's working state."""
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First step: host/port/ssl/api_key/session, validated live."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = await _build_client(self.hass, user_input)
            try:
                await client.get_sessions(all_=True)
            except WahaAuthError:
                errors["base"] = "invalid_auth"
            except WahaPermissionError:
                errors["base"] = "insufficient_permissions"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating WAHA connection")
                errors["base"] = "unknown"
            else:
                normalized = normalize_host_port(user_input)
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if (
                        normalize_host_port(entry.data) == normalized
                        and entry.data.get(CONF_SESSION) == user_input[CONF_SESSION]
                    ):
                        return self.async_abort(reason="already_configured")

                self._data = dict(user_input)
                self._data[CONF_HMAC_KEY] = secrets.token_urlsafe(48)
                self._data[CONF_WEBHOOK_ID] = WEBHOOK_ID_PREFIX + secrets.token_hex(16)
                return await self.async_step_session()

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_session(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the session if it doesn't exist yet (plain -- no webhook config; ensure_webhook
        attaches it right after via its normal safe-PUT path, since the session isn't WORKING yet)."""
        client = await _build_client(self.hass, self._data)
        session = await client.get_session(self._data[CONF_SESSION])

        if session is None:
            if user_input is None:
                return self.async_show_form(
                    step_id="session",
                    data_schema=vol.Schema({}),
                    description_placeholders={"session": self._data[CONF_SESSION]},
                )
            await client.create_session(self._data[CONF_SESSION], start=True)

        return await self.async_step_callback_url()

    async def async_step_callback_url(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Only shown if HA has no resolvable URL for async_generate_url (NoURLAvailableError)."""
        try:
            webhook_generate_url(
                self.hass, self._data[CONF_WEBHOOK_ID], allow_external=False, allow_ip=True
            )
            self._data.setdefault(CONF_CALLBACK_BASE_URL, None)
        except NoURLAvailableError:
            if user_input is None:
                return self.async_show_form(
                    step_id="callback_url",
                    data_schema=vol.Schema({vol.Required(CONF_CALLBACK_BASE_URL): str}),
                )
            self._data[CONF_CALLBACK_BASE_URL] = user_input[CONF_CALLBACK_BASE_URL]

        return await self._finish()

    async def _finish(self) -> ConfigFlowResult:
        """Create or update the entry, depending on the flow's source.

        unique_id is a random UUID (A5) with no real-world identity to
        compare -- deduplication is done explicitly in async_step_user via
        the (host:port, session) scan, so there's no unique_id-mismatch
        check to perform here (unlike a device with a real serial number).
        """
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self._data
            )
        await self.async_set_unique_id(secrets.token_hex(16))
        return self.async_create_entry(
            title=f"WAHA ({self._data[CONF_SESSION]})", data=self._data
        )

    # --- reauth ---

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error (401)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask only for a new API key; re-validate against the existing host/port/session."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            candidate = {**reauth_entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            client = await _build_client(self.hass, candidate)
            try:
                await client.get_sessions(all_=True)
            except WahaAuthError:
                errors["base"] = "invalid_auth"
            except WahaPermissionError:
                errors["base"] = "insufficient_permissions"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    # --- reconfigure ---

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Full form, pre-filled. Session name is fixed -- create a new entry to change it."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            candidate = {**user_input, CONF_SESSION: reconfigure_entry.data[CONF_SESSION]}
            client = await _build_client(self.hass, candidate)
            try:
                await client.get_sessions(all_=True)
            except WahaAuthError:
                errors["base"] = "invalid_auth"
            except WahaPermissionError:
                errors["base"] = "insufficient_permissions"
            except WahaConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self._data = {**reconfigure_entry.data, **candidate}
                return await self._finish()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=reconfigure_entry.data[CONF_HOST]): str,
                    vol.Required(CONF_PORT, default=reconfigure_entry.data[CONF_PORT]): int,
                    vol.Required(CONF_SSL, default=reconfigure_entry.data.get(CONF_SSL, False)): bool,
                    vol.Required(CONF_API_KEY, default=reconfigure_entry.data[CONF_API_KEY]): str,
                }
            ),
            description_placeholders={"session": reconfigure_entry.data[CONF_SESSION]},
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> SchemaOptionsFlowHandler:
        """Return the options flow: callback URL override + message_sent opt-in."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)
