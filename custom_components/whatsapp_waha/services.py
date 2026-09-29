"""All service registration for whatsapp_waha, registered once in async_setup
(never removed per-entry -- fixes scaffold bug S10). Milestone 1's 3 services
(register_webhook, unregister_webhook, request_pairing_code) moved here
unchanged; milestone 2 adds the WI-6 send/group services.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import repairs
from . import webhook_registration as wr
from .api import mimetype_for
from .const import (
    ATTR_ADDRESS,
    ATTR_CAPTION,
    ATTR_CHAT_ID,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_EMOJI,
    ATTR_FILE_PATH,
    ATTR_FILENAME,
    ATTR_GROUP_NAME,
    ATTR_LATITUDE,
    ATTR_LINK_PREVIEW,
    ATTR_LONGITUDE,
    ATTR_MESSAGE,
    ATTR_MESSAGE_ID,
    ATTR_MIMETYPE,
    ATTR_MULTIPLE_ANSWERS,
    ATTR_NAME,
    ATTR_OPTIONS,
    ATTR_PHONE_NUMBER,
    ATTR_PRIORITY,
    ATTR_QUESTION,
    ATTR_REPLY_TO,
    ATTR_URL,
    ATTR_WAIT,
    CONF_HMAC_KEY,
    CONF_LAST_PUT_AT,
    CONF_SESSION,
    DOMAIN,
    LANE_ALERT,
    LANE_NORMAL,
    SEND_WAIT_TIMEOUT_SECONDS,
    SERVICE_GET_GROUPS,
    SERVICE_REFRESH_GROUPS,
    SERVICE_REGISTER_WEBHOOK,
    SERVICE_REQUEST_PAIRING_CODE,
    SERVICE_SEND_FILE,
    SERVICE_SEND_IMAGE,
    SERVICE_SEND_LOCATION,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_POLL,
    SERVICE_SEND_REACTION,
    SERVICE_SEND_STICKER,
    SERVICE_SEND_VIDEO,
    SERVICE_SEND_VOICE,
    SERVICE_UNREGISTER_WEBHOOK,
)
from .coordinator import WahaConfigEntry, WahaRuntimeData
from .groups import GroupCache
from .util import webhook_callback_url

_LOGGER = logging.getLogger(__name__)

_ENTRY_ID_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str})

_TARGET_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
        vol.Exclusive(ATTR_CHAT_ID, "target"): cv.string,
        vol.Exclusive(ATTR_GROUP_NAME, "target"): cv.string,
        vol.Optional(ATTR_PRIORITY, default=LANE_NORMAL): vol.In([LANE_NORMAL, LANE_ALERT]),
        vol.Optional(ATTR_WAIT, default=False): cv.boolean,
    }
)
_MEDIA_SCHEMA = _TARGET_SCHEMA.extend(
    {
        vol.Exclusive(ATTR_FILE_PATH, "media"): cv.string,
        vol.Exclusive(ATTR_URL, "media"): cv.string,
        vol.Optional(ATTR_CAPTION): cv.string,
        vol.Optional(ATTR_FILENAME): cv.string,
        vol.Optional(ATTR_MIMETYPE): cv.string,
    }
)


def _resolve_entry(hass: HomeAssistant, call: ServiceCall) -> WahaConfigEntry:
    """Resolve which config entry a service call targets.

    Explicit config_entry_id if given; otherwise the sole loaded entry (A8).
    """
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(f"No loaded whatsapp_waha config entry with id {entry_id}")
    if len(entries) == 1:
        return entries[0]
    if not entries:
        raise ServiceValidationError("No whatsapp_waha config entry is loaded")
    raise ServiceValidationError(
        "Multiple whatsapp_waha config entries exist; specify config_entry_id"
    )


async def _resolve_target(hass: HomeAssistant, entry: WahaConfigEntry, call: ServiceCall) -> str:
    """chat_id xor group_name (validated exclusive by the schema) -> a concrete chat id."""
    runtime: WahaRuntimeData = entry.runtime_data
    chat_id = call.data.get(ATTR_CHAT_ID)
    group_name = call.data.get(ATTR_GROUP_NAME)
    if not chat_id and not group_name:
        raise ServiceValidationError("Specify either chat_id or group_name")
    if chat_id:
        return chat_id
    groups = await runtime.group_cache.get_groups()
    return GroupCache.resolve(groups, group_name)


def _read_file_as_base64(file_path: str) -> str:
    """Blocking read+encode -- always called via hass.async_add_executor_job,
    never from an async function directly (the architecture guard checks this)."""
    with open(file_path, "rb") as handle:  # noqa: PTH123
        return base64.b64encode(handle.read()).decode("ascii")


async def _resolve_media_payload(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """file_path (allowlist-checked, read+base64'd in the executor) xor url (passed
    straight through to WAHA -- HA never fetches it itself)."""
    file_path = call.data.get(ATTR_FILE_PATH)
    url = call.data.get(ATTR_URL)
    if not file_path and not url:
        raise ServiceValidationError("Specify either file_path or url")

    payload: dict[str, Any] = {}
    if url:
        payload["url"] = url
    else:
        allowed = await hass.async_add_executor_job(hass.config.is_allowed_path, file_path)
        if not allowed:
            raise ServiceValidationError(
                f"{file_path} is not an allowed path -- add its directory to "
                "allowlist_external_dirs in configuration.yaml"
            )
        payload["file_base64"] = await hass.async_add_executor_job(_read_file_as_base64, file_path)
        payload["mimetype"] = mimetype_for(file_path, call.data.get(ATTR_MIMETYPE))

    if call.data.get(ATTR_CAPTION):
        payload["caption"] = call.data[ATTR_CAPTION]
    if call.data.get(ATTR_FILENAME):
        payload["filename"] = call.data[ATTR_FILENAME]
    return payload


async def _enqueue_and_respond(
    entry: WahaConfigEntry, kind: str, chat_id: str, payload: dict[str, Any], call: ServiceCall
) -> ServiceResponse:
    runtime: WahaRuntimeData = entry.runtime_data
    lane = call.data.get(ATTR_PRIORITY, LANE_NORMAL)
    wait = call.data.get(ATTR_WAIT, False)
    result = runtime.send_queue.enqueue(kind, chat_id, payload, lane=lane, wait=wait)
    if wait:
        try:
            return await asyncio.wait_for(result, timeout=SEND_WAIT_TIMEOUT_SECONDS)
        except TimeoutError as err:
            raise ServiceValidationError("Timed out waiting for the message to send") from err
    return result


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register every whatsapp_waha service. Called once from async_setup."""

    # --- milestone 1: webhook registration + pairing (unchanged behavior) ---

    async def _handle_register_webhook(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        runtime: WahaRuntimeData = entry.runtime_data
        result = await wr.ensure_webhook(
            runtime.client,
            entry.data[CONF_SESSION],
            webhook_callback_url(hass, entry),
            entry.data[CONF_HMAC_KEY],
            last_put_at=entry.data.get(CONF_LAST_PUT_AT),
            force_working_put=True,
        )
        if result.outcome == wr.RegistrationOutcome.PUT_APPLIED:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_LAST_PUT_AT: time.time()}
            )
            repairs.async_clear_webhook_drift_issue(hass, entry.entry_id)
        runtime.last_webhook_registration = (result.outcome.value, time.time())
        return {"outcome": result.outcome.value, "detail": result.detail}

    async def _handle_unregister_webhook(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        runtime: WahaRuntimeData = entry.runtime_data
        result = await wr.remove_webhook(
            runtime.client,
            entry.data[CONF_SESSION],
            last_put_at=entry.data.get(CONF_LAST_PUT_AT),
            force_working_put=True,
        )
        runtime.last_webhook_registration = (result.outcome.value, time.time())
        return {"outcome": result.outcome.value, "detail": result.detail}

    async def _handle_request_pairing_code(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        runtime: WahaRuntimeData = entry.runtime_data
        code = await runtime.client.request_pairing_code(call.data[ATTR_PHONE_NUMBER])
        return {"code": code}

    hass.services.async_register(
        DOMAIN, SERVICE_REGISTER_WEBHOOK, _handle_register_webhook,
        schema=_ENTRY_ID_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNREGISTER_WEBHOOK, _handle_unregister_webhook,
        schema=_ENTRY_ID_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REQUEST_PAIRING_CODE, _handle_request_pairing_code,
        schema=_ENTRY_ID_SCHEMA.extend({vol.Required(ATTR_PHONE_NUMBER): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    # --- milestone 2: sending (WI-5/WI-6) ---

    async def _handle_send_message(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        chat_id = await _resolve_target(hass, entry, call)
        payload: dict[str, Any] = {
            "text": call.data[ATTR_MESSAGE],
            "link_preview": call.data.get(ATTR_LINK_PREVIEW, False),
        }
        reply_to = call.data.get(ATTR_REPLY_TO)
        if reply_to:
            if not (reply_to.startswith("true_") or reply_to.startswith("false_")):
                raise ServiceValidationError("reply_to must start with 'true_' or 'false_'")
            payload["reply_to"] = reply_to
        return await _enqueue_and_respond(entry, "text", chat_id, payload, call)

    def _make_media_handler(kind: str):
        async def handler(call: ServiceCall) -> ServiceResponse:
            entry = _resolve_entry(hass, call)
            chat_id = await _resolve_target(hass, entry, call)
            payload = await _resolve_media_payload(hass, call)
            return await _enqueue_and_respond(entry, kind, chat_id, payload, call)

        return handler

    async def _handle_send_poll(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        chat_id = await _resolve_target(hass, entry, call)
        payload = {
            "question": call.data[ATTR_QUESTION],
            "options": call.data[ATTR_OPTIONS],
            "multiple_answers": call.data.get(ATTR_MULTIPLE_ANSWERS, False),
        }
        return await _enqueue_and_respond(entry, "poll", chat_id, payload, call)

    async def _handle_send_location(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        chat_id = await _resolve_target(hass, entry, call)
        payload = {
            "latitude": call.data[ATTR_LATITUDE],
            "longitude": call.data[ATTR_LONGITUDE],
            "name": call.data.get(ATTR_NAME),
            "address": call.data.get(ATTR_ADDRESS),
        }
        return await _enqueue_and_respond(entry, "location", chat_id, payload, call)

    async def _handle_send_reaction(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        # Reactions target a message, not a chat -- chat_id is only used for
        # queue routing/pacing purposes and defaults to the reaction itself
        # having no chat concept, so it always goes out immediately.
        runtime: WahaRuntimeData = entry.runtime_data
        payload = {"message_id": call.data[ATTR_MESSAGE_ID], "emoji": call.data[ATTR_EMOJI]}
        result = runtime.send_queue.enqueue("reaction", call.data[ATTR_MESSAGE_ID], payload, lane=LANE_ALERT)
        return result

    async def _handle_get_groups(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        runtime: WahaRuntimeData = entry.runtime_data
        groups = await runtime.group_cache.get_groups()
        return {"groups": [{"id": g.id, "subject": g.subject} for g in groups]}

    async def _handle_refresh_groups(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(hass, call)
        runtime: WahaRuntimeData = entry.runtime_data
        groups = await runtime.group_cache.refresh()
        return {"groups": [{"id": g.id, "subject": g.subject} for g in groups]}

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_MESSAGE, _handle_send_message,
        schema=_TARGET_SCHEMA.extend(
            {
                vol.Required(ATTR_MESSAGE): cv.string,
                vol.Optional(ATTR_REPLY_TO): cv.string,
                vol.Optional(ATTR_LINK_PREVIEW, default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    for service_name, kind in (
        (SERVICE_SEND_IMAGE, "image"),
        (SERVICE_SEND_FILE, "file"),
        (SERVICE_SEND_VOICE, "voice"),
        (SERVICE_SEND_VIDEO, "video"),
        (SERVICE_SEND_STICKER, "sticker"),
    ):
        hass.services.async_register(
            DOMAIN, service_name, _make_media_handler(kind),
            schema=_MEDIA_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_POLL, _handle_send_poll,
        schema=_TARGET_SCHEMA.extend(
            {
                vol.Required(ATTR_QUESTION): cv.string,
                vol.Required(ATTR_OPTIONS): vol.All(cv.ensure_list, [cv.string], vol.Length(min=2)),
                vol.Optional(ATTR_MULTIPLE_ANSWERS, default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_LOCATION, _handle_send_location,
        schema=_TARGET_SCHEMA.extend(
            {
                vol.Required(ATTR_LATITUDE): vol.Coerce(float),
                vol.Required(ATTR_LONGITUDE): vol.Coerce(float),
                vol.Optional(ATTR_NAME): cv.string,
                vol.Optional(ATTR_ADDRESS): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_REACTION, _handle_send_reaction,
        schema=_ENTRY_ID_SCHEMA.extend(
            {vol.Required(ATTR_MESSAGE_ID): cv.string, vol.Required(ATTR_EMOJI): cv.string}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_GROUPS, _handle_get_groups,
        schema=_ENTRY_ID_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_GROUPS, _handle_refresh_groups,
        schema=_ENTRY_ID_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
