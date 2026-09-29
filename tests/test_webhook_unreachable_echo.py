"""Tests for the webhook_unreachable echo-test upgrade (WI-7 remainder):
after 10 minutes of silence while WORKING, send one direct self-probe and
correlate it against the message.any echo, bypassing the send queue entirely.

The production code's time thresholds use time.time() (real wall time,
consistent with the rest of this codebase -- webhook_registration.py's
last_put_at, etc.), while async_track_time_interval's *scheduling* is driven
by HA's own simulated clock (advanced via async_fire_time_changed). So each
test advances both in lockstep: patch time.time() to the target offset, then
fire the matching HA time-changed event to actually invoke the interval
callback.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.whatsapp_waha.const import CONF_SESSION, DOMAIN
from tests.fake_waha import FakeWaha


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


BASE_DATA = {
    "host": "192.168.0.40",
    "port": 3000,
    "ssl": False,
    "api_key": "key",
    CONF_SESSION: "default",
    "hmac_key": "hmac-key",
    "webhook_id": "whatsapp_waha_test",
}


async def _setup_loaded_entry(hass: HomeAssistant, fake: FakeWaha, clock: FakeClock) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, version=2)
    entry.add_to_hass(hass)
    with (
        patch("custom_components.whatsapp_waha.AiohttpWahaTransport", return_value=fake),
        patch("custom_components.whatsapp_waha.time.time", clock.time),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _issue(hass: HomeAssistant, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"webhook_unreachable_{entry.entry_id}")


async def _advance(hass: HomeAssistant, clock: FakeClock, seconds: float) -> None:
    """Advance both the production code's real-time clock and HA's own
    scheduling clock together, then let the interval callback run."""
    clock.now += seconds
    with patch("custom_components.whatsapp_waha.time.time", clock.time):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
        await hass.async_block_till_done()


async def test_probe_sent_after_ten_minutes_of_silence(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    clock = FakeClock(1_000_000.0)
    await _setup_loaded_entry(hass, fake, clock)
    fake.calls.clear()

    await _advance(hass, clock, 11 * 60)

    assert any(c[1] == "/api/sendText" for c in fake.calls)


async def test_matching_echo_clears_and_never_raises_the_repair(hass: HomeAssistant) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.whatsapp_waha.webhook_handler import signal_message_any_received

    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    clock = FakeClock(1_000_000.0)
    entry = await _setup_loaded_entry(hass, fake, clock)
    fake.calls.clear()

    await _advance(hass, clock, 11 * 60)
    send_calls = [c for c in fake.calls if c[1] == "/api/sendText"]
    assert send_calls
    probe_message_id = "fake-msg-1"  # FakeWaha's first send in this test gets this id

    async_dispatcher_send(
        hass, signal_message_any_received(entry.entry_id), {"id": probe_message_id, "fromMe": True}
    )
    await hass.async_block_till_done()

    await _advance(hass, clock, 3 * 60)  # past the echo timeout, if it were going to fire

    assert _issue(hass, entry) is None


async def test_no_echo_within_timeout_raises_the_repair(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    clock = FakeClock(1_000_000.0)
    entry = await _setup_loaded_entry(hass, fake, clock)
    fake.calls.clear()

    await _advance(hass, clock, 11 * 60)  # sends the probe
    await _advance(hass, clock, 3 * 60)  # past the 2-minute echo timeout, no echo arrives

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable is False


async def test_any_webhook_delivery_before_ten_minutes_prevents_a_probe(hass: HomeAssistant) -> None:
    """If ANY webhook arrived recently, we don't need to actively probe."""
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from custom_components.whatsapp_waha.webhook_handler import signal_last_webhook_received

    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    clock = FakeClock(1_000_000.0)
    entry = await _setup_loaded_entry(hass, fake, clock)
    fake.calls.clear()

    async_dispatcher_send(hass, signal_last_webhook_received(entry.entry_id), clock.now)
    await hass.async_block_till_done()

    await _advance(hass, clock, 11 * 60)

    assert not any(c[1] == "/api/sendText" for c in fake.calls)
