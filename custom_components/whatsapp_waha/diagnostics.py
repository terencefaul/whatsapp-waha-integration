"""WI-8: diagnostics.

Auto-discovered by filename -- no manifest entry needed. Redacts secrets and
chat/phone identifiers; the rest is a live snapshot useful for a bug report.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant

from .api import WahaError
from .const import CONF_HMAC_KEY
from .coordinator import WahaConfigEntry, WahaRuntimeData

TO_REDACT = {CONF_API_KEY, CONF_HMAC_KEY, CONF_WEBHOOK_ID}
TO_REDACT_ME = {"id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: WahaConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime: WahaRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    session_raw = coordinator.data.raw if coordinator.data else {}

    timelock: dict[str, Any] = {}
    capping: dict[str, Any] = {}
    if runtime.client is not None:
        try:
            timelock = await runtime.client.get_timelock()
        except WahaError as err:
            timelock = {"error": str(err)}
        try:
            capping = await runtime.client.get_capping()
        except WahaError as err:
            capping = {"error": str(err)}

    queue_info: dict[str, Any] = {}
    if runtime.send_queue is not None:
        queue_info = {
            "normal_lane_depth": runtime.send_queue.normal_depth(),
            "alert_lane_depth": runtime.send_queue.alert_depth(),
            "session_cooldown_until": runtime.send_queue.session_cooldown_until,
            "chat_cooldowns_count": len(runtime.send_queue.chat_cooldowns()),
            "send_failed_counts": runtime.send_queue.failure_counts(),
        }

    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "config_entry_options": dict(entry.options),
        "session_status": coordinator.data.status if coordinator.data else None,
        "webhook_registration": runtime.last_webhook_registration,
        "queue": queue_info,
        "me": async_redact_data(session_raw.get("me") or {}, TO_REDACT_ME),
        "reachout_timelock": timelock,
        "message_capping": capping,
    }
