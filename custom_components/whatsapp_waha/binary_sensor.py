"""The debounced 'connected' binary sensor -- the PRD's 'sensor showing
whether the WhatsApp session is connected'.

Goes on immediately when status becomes WORKING, but only goes off after
DISCONNECT_GRACE_SECONDS of continuous non-WORKING status, so a PUT-induced
restart blip (or a brief WAHA hiccup) can't false-trigger a "WhatsApp is
down" automation.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DISCONNECT_GRACE_SECONDS, STATE_WORKING
from .coordinator import WahaConfigEntry
from .entity import WahaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WahaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connected binary sensor."""
    async_add_entities([WahaConnectedBinarySensor(entry.runtime_data.coordinator, entry)])


class WahaConnectedBinarySensor(WahaEntity, BinarySensorEntity):
    """Debounced connectivity sensor."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connected"

    def __init__(self, coordinator, entry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_is_on = False
        self._pending_offline_unsub = None

    @callback
    def _handle_coordinator_update(self) -> None:
        status = self.coordinator.data.status if self.coordinator.data else None
        if status == STATE_WORKING:
            self._cancel_pending_offline()
            self._attr_is_on = True
            self.async_write_ha_state()
            return

        if self._attr_is_on and self._pending_offline_unsub is None:
            # Don't flip off immediately -- wait out the grace period first.
            self._pending_offline_unsub = async_call_later(
                self.hass, DISCONNECT_GRACE_SECONDS, self._go_offline
            )
        elif not self._attr_is_on:
            self.async_write_ha_state()

    @callback
    def _go_offline(self, _now) -> None:
        self._pending_offline_unsub = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _cancel_pending_offline(self) -> None:
        if self._pending_offline_unsub is not None:
            self._pending_offline_unsub()
            self._pending_offline_unsub = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending debounce timer on removal."""
        self._cancel_pending_offline()
        await super().async_will_remove_from_hass()
