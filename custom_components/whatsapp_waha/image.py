"""The QR-code ImageEntity (WI-7's linking pieces).

Fritz precedent: only fetches while the session is SCAN_QR_CODE, and only
bumps image_last_updated (making the frontend re-fetch) when the bytes
actually changed -- WAHA rotates the QR periodically while waiting.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .api import WahaConnectionError
from .const import QR_REFRESH_INTERVAL_SECONDS, STATE_SCAN_QR_CODE
from .coordinator import WahaConfigEntry
from .entity import WahaEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WahaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the QR image entity."""
    runtime = entry.runtime_data
    async_add_entities([WahaQrImage(hass, runtime.coordinator, entry, runtime.client)])


class WahaQrImage(WahaEntity, ImageEntity):
    """Shows the current pairing QR code while the session is SCAN_QR_CODE."""

    _attr_translation_key = "qr_code"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, coordinator, entry, client) -> None:
        """Initialize the QR image entity."""
        WahaEntity.__init__(self, coordinator, entry)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{entry.entry_id}_qr_code"
        self._client = client
        self._image_bytes: bytes | None = None
        self._image_hash: str | None = None
        self._unsub_timer = None

    @property
    def available(self) -> bool:
        """Only available once we have a QR image while scanning is in progress."""
        status = self.coordinator.data.status if self.coordinator.data else None
        return status == STATE_SCAN_QR_CODE and self._image_bytes is not None

    async def async_image(self) -> bytes | None:
        """Return the cached QR bytes -- fetching happens on the 5s timer, not here."""
        return self._image_bytes

    @callback
    def _handle_coordinator_update(self) -> None:
        status = self.coordinator.data.status if self.coordinator.data else None
        if status == STATE_SCAN_QR_CODE:
            self._ensure_timer_running()
        else:
            self._stop_timer()
            self._image_bytes = None
        self.async_write_ha_state()

    @callback
    def _ensure_timer_running(self) -> None:
        if self._unsub_timer is None:
            self._unsub_timer = async_track_time_interval(
                self.hass, self._async_refresh_qr, timedelta(seconds=QR_REFRESH_INTERVAL_SECONDS)
            )
            self.hass.async_create_task(self._async_refresh_qr(None))

    @callback
    def _stop_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    async def _async_refresh_qr(self, _now) -> None:
        try:
            raw = await self._client.get_qr()
        except WahaConnectionError:
            _LOGGER.debug("Could not fetch QR code for %s", self.entity_id)
            return

        new_hash = hashlib.sha256(raw).hexdigest()
        if new_hash == self._image_hash:
            return
        self._image_hash = new_hash
        self._image_bytes = raw
        self._attr_image_last_updated = datetime.now(tz=timezone.utc)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the refresh timer on removal."""
        self._stop_timer()
        await super().async_will_remove_from_hass()
