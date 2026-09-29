"""Two sensors: the raw (non-debounced) session status, and a 'last webhook
received' timestamp -- the only falsifiable proof the WAHA -> HA webhook
path actually works (registration alone can look green)."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WahaConfigEntry
from .entity import WahaEntity
from .webhook_handler import signal_last_webhook_received


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WahaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the status and last-webhook-received sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            WahaStatusSensor(coordinator, entry),
            WahaLastWebhookReceivedSensor(coordinator, entry),
        ]
    )


class WahaStatusSensor(WahaEntity, SensorEntity):
    """The raw session status string -- not debounced (unlike the connected binary sensor)."""

    _attr_translation_key = "status"

    def __init__(self, coordinator, entry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        """Return the raw session status."""
        return self.coordinator.data.status if self.coordinator.data else None


class WahaLastWebhookReceivedSensor(WahaEntity, SensorEntity):
    """Timestamp of the last webhook delivery received, of any kind.

    Independent of the 60s coordinator poll -- fed directly by the webhook
    handler via a dispatcher signal the instant a delivery verifies.
    """

    _attr_translation_key = "last_webhook_received"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_webhook_received"
        self._entry_id = entry.entry_id
        self._value: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        """Return the last-received timestamp."""
        return self._value

    async def async_added_to_hass(self) -> None:
        """Subscribe to the webhook handler's dispatcher signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_last_webhook_received(self._entry_id), self._handle_received
            )
        )

    @callback
    def _handle_received(self, ts: float) -> None:
        self._value = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.async_write_ha_state()
