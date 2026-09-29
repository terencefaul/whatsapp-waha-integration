"""WI-6/D8: the notify duality.

A bare NotifyEntity is only ever callable via the generic notify.send_message
entity service, targeted by entity_id -- it is never exposed as its own
named service. gate-pin needs a real `notify.whatsapp_waha` service
(POST services/notify/whatsapp_waha with {"title","message"}), which only
the legacy platform path produces. Both live here, exactly like core's
homeassistant/components/nfandroidtv/notify.py + __init__.py.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.notify import ATTR_TITLE, BaseNotificationService, NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_NOTIFY_LANE, CONF_NOTIFY_TARGET, DEFAULT_NOTIFY_LANE, LANE_ALERT, LANE_NORMAL
from .coordinator import WahaConfigEntry, WahaRuntimeData
from .entity import WahaEntity

_LOGGER = logging.getLogger(__name__)


def _resolve_notify_target(entry: WahaConfigEntry) -> tuple[str | None, str]:
    """The chat/group id and lane configured in the options flow. Deliberately
    an id, not a free-form name -- no ambiguous matching at send time."""
    target = entry.options.get(CONF_NOTIFY_TARGET)
    lane = entry.options.get(CONF_NOTIFY_LANE, DEFAULT_NOTIFY_LANE)
    return target, lane


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WahaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the notify entity platform."""
    async_add_entities([WahaNotifyEntity(entry.runtime_data.coordinator, entry)])


class WahaNotifyEntity(WahaEntity, NotifyEntity):
    """Callable only via notify.send_message, targeted by entity_id."""

    _attr_translation_key = "notify"
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator, entry: WahaConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_notify"
        self._entry = entry

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Enqueue through the send queue -- fire-and-forget, no wait."""
        runtime: WahaRuntimeData = self._entry.runtime_data
        target, lane = _resolve_notify_target(self._entry)
        if not target:
            _LOGGER.warning("No notify target configured for %s; message dropped", self._entry.title)
            return
        text = f"*{title}*\n{message}" if title else message
        runtime.send_queue.enqueue("text", target, {"text": text}, lane=lane)


async def async_get_service(
    hass: HomeAssistant,
    config: ConfigType,
    discovery_info: DiscoveryInfoType | None = None,
) -> WahaNotificationService | None:
    """The legacy platform entry point -- discovery-only (see __init__.py's
    discovery.async_load_platform call), never from YAML."""
    if discovery_info is None:
        return None
    entry = hass.config_entries.async_get_entry(discovery_info["entry_id"])
    if entry is None:
        return None
    return WahaNotificationService(entry)


class WahaNotificationService(BaseNotificationService):
    """Produces the real notify.whatsapp_waha (or notify.whatsapp_waha_<session>)
    service gate-pin's POST services/notify/<name> calls."""

    def __init__(self, entry: WahaConfigEntry) -> None:
        self._entry = entry

    async def async_send_message(self, message: str, **kwargs: Any) -> None:
        runtime: WahaRuntimeData = self._entry.runtime_data
        target, default_lane = _resolve_notify_target(self._entry)
        if not target:
            _LOGGER.warning("No notify target configured for %s; message dropped", self._entry.title)
            return
        title = kwargs.get(ATTR_TITLE)
        text = f"*{title}*\n{message}" if title else message
        data = kwargs.get("data") or {}
        lane = data.get("priority", default_lane)
        if lane not in (LANE_NORMAL, LANE_ALERT):
            lane = default_lane
        runtime.send_queue.enqueue("text", target, {"text": text}, lane=lane)
