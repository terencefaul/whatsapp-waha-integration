"""Shared entity base for whatsapp_waha -- one DeviceInfo per config entry so
every platform's entities group under a single device (fixes the scaffold's
missing device registry entry for a manifest that claims integration_type:
service)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SESSION, DOMAIN
from .coordinator import WahaCoordinator
from .util import base_url_for


class WahaEntity(CoordinatorEntity[WahaCoordinator]):
    """Base entity for all whatsapp_waha entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WahaCoordinator, entry: ConfigEntry) -> None:
        """Initialize the entity with the shared device info."""
        super().__init__(coordinator)
        self._entry = entry
        session_name = entry.data.get(CONF_SESSION, "default")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"WAHA ({session_name})",
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="WAHA",
            model="WAHA session",
            configuration_url=f"{base_url_for(entry.data)}/dashboard",
        )
