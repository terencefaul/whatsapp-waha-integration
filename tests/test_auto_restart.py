"""Tests for the WI-7 remainder: auto-restart (default off, rate-limited,
cooldown/PASSKEY-excluded) and PASSKEY_* repairs."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha import _AutoRestartLimiter
from custom_components.whatsapp_waha.const import (
    CONF_AUTO_RESTART_ENABLED,
    CONF_SESSION,
    DOMAIN,
)
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


async def _setup_loaded_entry(hass: HomeAssistant, fake: FakeWaha, *, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, options=options or {}, version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.whatsapp_waha.AiohttpWahaTransport", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def now_fn(self) -> float:
        return self.now


def test_limiter_allows_up_to_three_per_day() -> None:
    clock = FakeClock()
    limiter = _AutoRestartLimiter(now_fn=clock.now_fn)

    for _ in range(3):
        assert limiter.allow()
        limiter.record()
        clock.now += 7300  # past each backoff tier

    assert limiter.allow() is False


def test_limiter_enforces_backoff_between_attempts() -> None:
    clock = FakeClock()
    limiter = _AutoRestartLimiter(now_fn=clock.now_fn)
    limiter.allow()
    limiter.record()

    clock.now += 100  # well under the 5-minute first backoff
    assert limiter.allow() is False

    clock.now += 300  # now past it
    assert limiter.allow() is True


def test_limiter_resets_after_24_hours() -> None:
    clock = FakeClock()
    limiter = _AutoRestartLimiter(now_fn=clock.now_fn)
    for _ in range(3):
        limiter.allow()
        limiter.record()
        clock.now += 7300

    clock.now += 86400
    assert limiter.allow() is True


async def test_auto_restart_disabled_by_default_never_starts_session(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="FAILED")
    await _setup_loaded_entry(hass, fake)  # no CONF_AUTO_RESTART_ENABLED option

    assert not any(c[1].endswith("/start") for c in fake.calls)


async def test_auto_restart_enabled_starts_from_failed(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="FAILED")
    await _setup_loaded_entry(hass, fake, options={CONF_AUTO_RESTART_ENABLED: True})

    assert any(c[1].endswith("/start") for c in fake.calls)


async def test_auto_restart_never_fires_from_scan_qr_code(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="SCAN_QR_CODE")
    await _setup_loaded_entry(hass, fake, options={CONF_AUTO_RESTART_ENABLED: True})

    assert not any(c[1].endswith("/start") for c in fake.calls)


async def test_passkey_required_raises_non_fixable_repair(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="PASSKEY_REQUIRED")
    entry = await _setup_loaded_entry(hass, fake)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"passkey_required_{entry.entry_id}")
    assert issue is not None
    assert issue.is_fixable is False


async def test_passkey_required_never_triggers_auto_restart(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="PASSKEY_REQUIRED")
    await _setup_loaded_entry(hass, fake, options={CONF_AUTO_RESTART_ENABLED: True})

    assert not any(c[1].endswith("/start") for c in fake.calls)


async def test_leaving_passkey_state_clears_the_repair(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="PASSKEY_REQUIRED")
    entry = await _setup_loaded_entry(hass, fake)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"passkey_required_{entry.entry_id}") is not None

    fake.sessions["default"]["status"] = "WORKING"
    entry.runtime_data.coordinator.push_status_update("WORKING")

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"passkey_required_{entry.entry_id}") is None
