"""Tests for image.py's QR entity: timer only while SCAN_QR_CODE, sha256 dedupe."""

from __future__ import annotations

from contextlib import asynccontextmanager

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockEntityPlatform

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import CONF_SESSION, DOMAIN
from custom_components.whatsapp_waha.coordinator import WahaCoordinator, WahaSessionData
from custom_components.whatsapp_waha.image import WahaQrImage
from tests.fake_waha import FakeWaha

BASE_DATA = {
    "host": "192.168.0.40",
    "port": 3000,
    "ssl": False,
    "api_key": "key",
    CONF_SESSION: "default",
    "hmac_key": "hmac-key",
    "webhook_id": "whatsapp_waha_test",
}


@asynccontextmanager
async def _entity(hass: HomeAssistant, fake: FakeWaha):
    """Build a real, platform-attached QR entity, and shut its coordinator down
    on exit -- adding a CoordinatorEntity as a listener starts the
    coordinator's periodic-refresh timer, which must be cancelled or the
    harness's lingering-timer check fails the test."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA)
    entry.add_to_hass(hass)
    client = WahaClient(fake, "default")
    coordinator = WahaCoordinator(hass, entry, client)
    entity = WahaQrImage(hass, coordinator, entry, client)
    platform = MockEntityPlatform(hass, domain="image", platform_name=DOMAIN)
    entity.add_to_platform_start(hass, platform, None)
    entity.entity_id = "image.test_qr"
    await entity.add_to_platform_finish()
    try:
        yield entity, coordinator
    finally:
        await entity.async_remove(force_remove=True)
        await coordinator.async_shutdown()


async def test_unavailable_before_any_qr_fetched(hass: HomeAssistant) -> None:
    """No QR bytes yet -> unavailable, even while SCAN_QR_CODE."""
    fake = FakeWaha()
    async with _entity(hass, fake) as (entity, coordinator):
        coordinator.data = WahaSessionData(status="SCAN_QR_CODE", raw={})

        assert entity.available is False


async def test_refresh_fetches_and_caches_qr_bytes(hass: HomeAssistant) -> None:
    """A refresh fetches the QR and becomes available."""
    fake = FakeWaha()
    async with _entity(hass, fake) as (entity, coordinator):
        coordinator.data = WahaSessionData(status="SCAN_QR_CODE", raw={})

        await entity._async_refresh_qr(None)

        assert entity.available is True
        assert await entity.async_image() == fake.qr_bytes


async def test_refresh_skips_update_when_bytes_unchanged(hass: HomeAssistant) -> None:
    """Fetching the same bytes twice doesn't bump image_last_updated again."""
    fake = FakeWaha()
    async with _entity(hass, fake) as (entity, coordinator):
        coordinator.data = WahaSessionData(status="SCAN_QR_CODE", raw={})

        await entity._async_refresh_qr(None)
        first_updated = entity._attr_image_last_updated

        await entity._async_refresh_qr(None)

        assert entity._attr_image_last_updated == first_updated


async def test_refresh_bumps_update_when_bytes_change(hass: HomeAssistant) -> None:
    """A rotated QR code (different bytes) bumps image_last_updated."""
    fake = FakeWaha()
    async with _entity(hass, fake) as (entity, coordinator):
        coordinator.data = WahaSessionData(status="SCAN_QR_CODE", raw={})

        await entity._async_refresh_qr(None)
        first_updated = entity._attr_image_last_updated

        fake.qr_bytes = b"different-qr-bytes"
        await entity._async_refresh_qr(None)

        assert entity._attr_image_last_updated != first_updated
        assert await entity.async_image() == b"different-qr-bytes"


async def test_leaving_scan_qr_code_clears_cached_image(hass: HomeAssistant) -> None:
    """Once linked (status leaves SCAN_QR_CODE), the cached QR is cleared and it's unavailable."""
    fake = FakeWaha()
    async with _entity(hass, fake) as (entity, coordinator):
        coordinator.data = WahaSessionData(status="SCAN_QR_CODE", raw={})
        await entity._async_refresh_qr(None)
        assert entity.available is True

        coordinator.data = WahaSessionData(status="WORKING", raw={})
        entity._handle_coordinator_update()

        assert entity.available is False
