"""The whatsapp_waha integration.

WI-3 skeleton: entry.runtime_data, a subclassed coordinator, services
registered once in async_setup (never removed per-entry -- fixes scaffold
bug S10), and ensure_webhook run once after the first successful refresh
(WI-2), gated by the session's status and never PUTting onto a WORKING
session without consent.

Milestone 2 adds: the send queue worker (WI-5), the notify duality (WI-6/D8,
via discovery.async_load_platform bootstrapping notify.py's legacy service),
auto-restart and PASSKEY_* repairs (WI-7 remainder), and the webhook_unreachable
echo-test upgrade.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta
from typing import Any

from homeassistant.components.persistent_notification import async_create as async_create_notification
from homeassistant.components.webhook import async_register as webhook_register
from homeassistant.components.webhook import async_unregister as webhook_unregister
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SSL,
    CONF_WEBHOOK_ID,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from . import repairs
from . import services as services_module
from . import webhook_registration as wr
from .api import AiohttpWahaTransport, WahaClient, WahaError
from .const import (
    AUTO_RESTART_BACKOFF_SECONDS,
    AUTO_RESTART_MAX_ATTEMPTS_PER_DAY,
    CONF_AUTO_RESTART_ENABLED,
    CONF_CALLBACK_BASE_URL,
    CONF_FIRE_MESSAGE_SENT_EVENTS,
    CONF_HMAC_KEY,
    CONF_INTENDED_STATE,
    CONF_LAST_PUT_AT,
    CONF_NORMAL_HOURLY_CAP,
    CONF_SEND_QUEUE_PRESET,
    CONF_SESSION,
    DATA_HASS_CONFIG,
    DEFAULT_FIRE_MESSAGE_SENT_EVENTS,
    DEFAULT_PORT,
    DEFAULT_SEND_QUEUE_PRESET,
    DEFAULT_SESSION,
    DOMAIN,
    EVENT_SEND_FAILED,
    INTENDED_STATE_STOPPED,
    LANE_ALERT,
    NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT,
    NOTIFY_SERVICE_BASE_NAME,
    STATE_FAILED,
    STATE_PASSKEY_CONFIRMATION_REQUIRED,
    STATE_PASSKEY_REQUIRED,
    STATE_SCAN_QR_CODE,
    STATE_STOPPED,
    STATE_WORKING,
    TYPING_MARKER_STORE_VERSION,
    WEBHOOK_ECHO_PROBE_TIMEOUT_SECONDS,
    WEBHOOK_ID_PREFIX,
    WEBHOOK_UNREACHABLE_GRACE_MINUTES,
)
from .coordinator import WahaCoordinator, WahaConfigEntry, WahaRuntimeData
from .groups import GroupCache
from .send_queue import SendQueue
from .util import base_url_for, webhook_callback_url
from .webhook_handler import (
    EnvelopeDedupe,
    HmacFailureTracker,
    create_webhook_handler,
    signal_last_webhook_received,
    signal_message_any_received,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.IMAGE, Platform.NOTIFY]

_AUTO_RESTART_ATTEMPT_STATES = (STATE_FAILED, STATE_STOPPED)


class _AutoRestartLimiter:
    """Sliding 24h window, max 3 attempts, 5m/30m/2h backoff between attempts."""

    def __init__(self, *, now_fn=time.time) -> None:
        self._now_fn = now_fn
        self._max_attempts = AUTO_RESTART_MAX_ATTEMPTS_PER_DAY
        self._backoff = AUTO_RESTART_BACKOFF_SECONDS
        self._attempts: list[float] = []

    def allow(self) -> bool:
        now = self._now_fn()
        self._attempts = [t for t in self._attempts if now - t < 86400]
        if len(self._attempts) >= self._max_attempts:
            return False
        if self._attempts:
            backoff = self._backoff[min(len(self._attempts) - 1, len(self._backoff) - 1)]
            if now - self._attempts[-1] < backoff:
                return False
        return True

    def record(self) -> None:
        self._attempts.append(self._now_fn())


def _notify_service_name(entry: WahaConfigEntry) -> str:
    """The bare name for the entry whose session is 'default'; a session-suffixed
    name for every other entry -- deterministic, no "first loaded wins" fragility."""
    session = entry.data.get(CONF_SESSION, DEFAULT_SESSION)
    if session == DEFAULT_SESSION:
        return NOTIFY_SERVICE_BASE_NAME
    return f"{NOTIFY_SERVICE_BASE_NAME}_{slugify(session)}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once, globally, and stash the YAML config for the
    legacy notify platform's discovery bootstrap."""
    hass.data[DATA_HASS_CONFIG] = config
    await services_module.async_setup_services(hass)
    return True


async def _async_reconcile_webhook(hass: HomeAssistant, entry: WahaConfigEntry, client: WahaClient) -> None:
    """Run ensure_webhook once after the first successful refresh, and raise/clear repairs."""
    result = await wr.ensure_webhook(
        client,
        entry.data[CONF_SESSION],
        webhook_callback_url(hass, entry),
        entry.data[CONF_HMAC_KEY],
        last_put_at=entry.data.get(CONF_LAST_PUT_AT),
    )
    runtime: WahaRuntimeData = entry.runtime_data
    runtime.last_webhook_registration = (result.outcome.value, time.time())
    if result.outcome == wr.RegistrationOutcome.PUT_APPLIED:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LAST_PUT_AT: time.time()}
        )
        repairs.async_clear_webhook_drift_issue(hass, entry.entry_id)
    elif result.outcome in (wr.RegistrationOutcome.NEEDS_CONSENT, wr.RegistrationOutcome.UNFIXABLE):
        repairs.async_create_webhook_drift_issue(
            hass, entry.entry_id, fixable=result.outcome == wr.RegistrationOutcome.NEEDS_CONSENT
        )
    else:
        repairs.async_clear_webhook_drift_issue(hass, entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> bool:
    """Set up whatsapp_waha from a config entry."""
    transport = AiohttpWahaTransport(
        async_get_clientsession(hass), base_url_for(entry.data), entry.data[CONF_API_KEY]
    )
    client = WahaClient(transport, entry.data[CONF_SESSION])
    coordinator = WahaCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    marker_store = Store(
        hass, TYPING_MARKER_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_typing_marker"
    )
    send_queue = SendQueue(
        client,
        entry.data[CONF_SESSION],
        status_fn=lambda: coordinator.data.status if coordinator.data else None,
        preset=entry.options.get(CONF_SEND_QUEUE_PRESET, DEFAULT_SEND_QUEUE_PRESET),
        normal_hourly_cap_per_chat=entry.options.get(
            CONF_NORMAL_HOURLY_CAP, NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT
        ),
        marker_store=marker_store,
        on_send_failed=lambda item, reason, ambiguous: _on_send_failed(hass, entry, item, reason, ambiguous),
        on_cooldown=lambda chat_id, status: repairs.async_create_send_rate_limited_issue(hass, entry.entry_id),
    )
    group_cache = GroupCache(client, status_fn=lambda: coordinator.data.status if coordinator.data else None)

    runtime = WahaRuntimeData(
        client=client, coordinator=coordinator, send_queue=send_queue, group_cache=group_cache
    )
    entry.runtime_data = runtime

    await send_queue.recover_from_crash()
    entry.async_create_background_task(hass, send_queue.run(), name=f"whatsapp_waha_send_queue_{entry.entry_id}")

    # -- notify duality (WI-6/D8): the entity platform is forwarded normally
    # below; the legacy notify.<name> service needs discovery bootstrapping. --
    hass.async_create_task(
        discovery.async_load_platform(
            hass,
            Platform.NOTIFY,
            DOMAIN,
            {CONF_NAME: _notify_service_name(entry), "entry_id": entry.entry_id},
            hass.data[DATA_HASS_CONFIG],
        )
    )

    hmac_tracker = HmacFailureTracker()
    dedupe = EnvelopeDedupe()

    def _on_hmac_repair_threshold() -> None:
        repairs.async_create_hmac_mismatch_issue(hass, entry.entry_id)

    handler = create_webhook_handler(
        hass,
        entry.entry_id,
        entry.data[CONF_HMAC_KEY],
        dedupe,
        hmac_tracker,
        coordinator,
        base_url_for(entry.data),
        fire_message_sent_events=entry.options.get(
            CONF_FIRE_MESSAGE_SENT_EVENTS, DEFAULT_FIRE_MESSAGE_SENT_EVENTS
        ),
        on_hmac_repair_threshold=_on_hmac_repair_threshold,
    )
    webhook_register(
        hass, DOMAIN, "WAHA events", entry.data[CONF_WEBHOOK_ID], handler, allowed_methods=["POST"]
    )
    entry.async_on_unload(lambda: webhook_unregister(hass, entry.data[CONF_WEBHOOK_ID]))

    # -- "last webhook received" + the webhook_unreachable echo-test --
    last_received: dict[str, float | None] = {"at": None}
    working_since: dict[str, float | None] = {"at": None}
    pending_probe: dict[str, Any] = {"message_id": None, "sent_at": None}
    restart_limiter = _AutoRestartLimiter()

    @callback
    def _on_webhook_received(ts: float) -> None:
        last_received["at"] = ts

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, signal_last_webhook_received(entry.entry_id), _on_webhook_received
        )
    )

    @callback
    def _on_message_any_received(payload: dict[str, Any]) -> None:
        if pending_probe["message_id"] and payload.get("id") == pending_probe["message_id"]:
            pending_probe["message_id"] = None
            repairs.async_clear_webhook_unreachable_issue(hass, entry.entry_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, signal_message_any_received(entry.entry_id), _on_message_any_received
        )
    )

    @callback
    def _maybe_auto_restart(status: str | None) -> None:
        if not entry.options.get(CONF_AUTO_RESTART_ENABLED, False):
            return
        if status not in _AUTO_RESTART_ATTEMPT_STATES:
            return
        if status == STATE_STOPPED and entry.data.get(CONF_INTENDED_STATE) == INTENDED_STATE_STOPPED:
            return
        if send_queue.is_session_cooling_down():
            return
        if not restart_limiter.allow():
            return
        restart_limiter.record()
        hass.async_create_task(client.start_session())

    @callback
    def _on_coordinator_update() -> None:
        status = coordinator.data.status if coordinator.data else None
        if status == STATE_WORKING:
            if working_since["at"] is None:
                working_since["at"] = time.time()
        else:
            working_since["at"] = None

        if status == STATE_SCAN_QR_CODE:
            repairs.async_create_needs_link_issue(hass, entry.entry_id)
        else:
            repairs.async_clear_needs_link_issue(hass, entry.entry_id)

        if status in (STATE_PASSKEY_REQUIRED, STATE_PASSKEY_CONFIRMATION_REQUIRED):
            repairs.async_create_passkey_required_issue(
                hass, entry.entry_id, f"{base_url_for(entry.data)}/dashboard"
            )
        else:
            repairs.async_clear_passkey_required_issue(hass, entry.entry_id)

        _maybe_auto_restart(status)

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))
    _on_coordinator_update()

    async def _check_webhook_unreachable(_now: Any) -> None:
        if pending_probe["message_id"] is not None:
            if time.time() - pending_probe["sent_at"] > WEBHOOK_ECHO_PROBE_TIMEOUT_SECONDS:
                repairs.async_create_webhook_unreachable_issue(hass, entry.entry_id)
                pending_probe["message_id"] = None
            return

        since = working_since["at"]
        if since is None:
            return
        if (time.time() - since) <= WEBHOOK_UNREACHABLE_GRACE_MINUTES * 60 or last_received["at"] is not None:
            return

        me_id = (coordinator.data.raw.get("me") or {}).get("id") if coordinator.data else None
        if not me_id:
            return
        try:
            # Deliberately bypasses the send queue -- internal connectivity
            # plumbing, not user traffic, needs a synchronous message id to
            # correlate against the message.any echo.
            result = await client.send_text(me_id, "whatsapp_waha connectivity check")
        except WahaError:
            return
        pending_probe["message_id"] = result.get("id")
        pending_probe["sent_at"] = time.time()

    entry.async_on_unload(
        async_track_time_interval(hass, _check_webhook_unreachable, timedelta(minutes=2))
    )

    await _async_reconcile_webhook(hass, entry, client)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _on_send_failed(hass: HomeAssistant, entry: WahaConfigEntry, item, reason: str, ambiguous: bool) -> None:
    """Fire whatsapp_waha_send_failed for every drop/expiry/failure -- nothing
    disappears silently. Alert-lane failures also raise a persistent notification,
    via a direct import (the old hass dot components proxy was removed in HA
    2025.5.0, see S1)."""
    hass.bus.async_fire(
        EVENT_SEND_FAILED,
        {
            "entry_id": entry.entry_id,
            "chat_id": item.chat_id,
            "item_id": item.id,
            "reason": reason,
            "ambiguous": ambiguous,
        },
    )
    if item.lane == LANE_ALERT:
        async_create_notification(
            hass,
            f"A WhatsApp alert to {item.chat_id} failed to send ({reason}).",
            title="WhatsApp send failed",
            notification_id=f"whatsapp_waha_send_failed_{entry.entry_id}_{item.id}",
        )


async def async_unload_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> bool:
    """Unload a config entry, clearing any repair issues raised for it."""
    for issue in (
        "needs_link",
        "webhook_drift",
        "hmac_mismatch",
        "webhook_unreachable",
        "passkey_required",
        "send_rate_limited",
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: WahaConfigEntry) -> bool:
    """Migrate a version-1 (scaffold-shape) entry to version 2.

    Defensive: nobody has a *working* scaffold install (it can't load), but
    an entry could still exist in storage from an attempt. Renames
    `use_ssl` -> CONF_SSL, defaults a missing CONF_PORT/CONF_SESSION, and --
    since the scaffold had no HMAC verification at all and a guessable
    webhook id (S7) -- always generates a fresh CONF_HMAC_KEY/CONF_WEBHOOK_ID
    rather than trusting anything the old shape might have had.
    """
    if entry.version >= 2:
        return True

    data = dict(entry.data)
    if "use_ssl" in data:
        data[CONF_SSL] = data.pop("use_ssl")
    data.setdefault(CONF_PORT, DEFAULT_PORT)
    data.setdefault(CONF_SESSION, DEFAULT_SESSION)
    data[CONF_HMAC_KEY] = secrets.token_urlsafe(48)
    data[CONF_WEBHOOK_ID] = WEBHOOK_ID_PREFIX + secrets.token_hex(16)

    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True
