"""Tests for send_queue.py (WI-5, Verification item 5).

A fake clock (now_fn/sleep_fn/random_fn all injected) makes every test
deterministic and instant -- no real waiting. sleep_fn just advances the
fake clock and returns; random_fn is pinned to a fixed multiplier so pacing
math is exact.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import (
    LANE_ALERT,
    LANE_NORMAL,
    PRESET_STRICT,
    STATE_SCAN_QR_CODE,
    STATE_WORKING,
)
from custom_components.whatsapp_waha.send_queue import PacingConfig, SendQueue
from tests.fake_waha import FakeWaha


class FakeClock:
    """A controllable clock: now_fn reads it, sleep_fn advances it instantly."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def now_fn(self) -> float:
        return self.now

    async def sleep_fn(self, seconds: float) -> None:
        self.now += seconds


def _queue(fake: FakeWaha, clock: FakeClock, *, status: str = STATE_WORKING, **kwargs) -> SendQueue:
    client = WahaClient(fake, "default")
    kwargs.setdefault("now_fn", clock.now_fn)
    kwargs.setdefault("sleep_fn", clock.sleep_fn)
    kwargs.setdefault("random_fn", lambda lo, hi: (lo + hi) / 2)  # deterministic midpoint
    return SendQueue(client, "default", status_fn=lambda: status, **kwargs)


async def _drain(queue: SendQueue, max_iterations: int = 50) -> int:
    """Call process_one() until it returns False or the cap is hit. Returns the count processed."""
    count = 0
    for _ in range(max_iterations):
        if not await queue.process_one():
            break
        count += 1
    return count


async def test_normal_send_call_order_is_seen_typing_stop_send(hass) -> None:
    """seen -> typing -> stop -> send, in that exact order (Verification item 5)."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock, preset=PRESET_STRICT)
    queue.enqueue("text", "27821234567@c.us", {"text": "hi", "reply_to": "true_x_1"})

    await _drain(queue)

    paths = [call[1] for call in fake.calls]
    assert paths == ["/api/sendSeen", "/api/startTyping", "/api/stopTyping", "/api/sendText"]


async def test_no_seen_call_when_not_replying(hass) -> None:
    """sendSeen is only called when replying (reply_to set)."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock)
    queue.enqueue("text", "27821234567@c.us", {"text": "hi"})

    await _drain(queue)

    paths = [call[1] for call in fake.calls]
    assert "/api/sendSeen" not in paths


async def test_stop_typing_called_even_if_send_fails(hass) -> None:
    """stopTyping runs in a finally -- it fires even when the send itself fails."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["5xx"])
    clock = FakeClock()
    queue = _queue(fake, clock)
    queue.enqueue("text", "27821234567@c.us", {"text": "hi"})

    await _drain(queue)

    paths = [call[1] for call in fake.calls]
    assert "/api/stopTyping" in paths


async def test_not_working_status_types_nothing(hass) -> None:
    """Gate on WORKING -- not-WORKING means zero startTyping calls, nothing dequeued."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock, status=STATE_SCAN_QR_CODE)
    queue.enqueue("text", "27821234567@c.us", {"text": "hi"})

    processed = await _drain(queue)

    assert processed == 0
    assert fake.calls == []
    assert queue.normal_depth() == 1


async def test_off_preset_has_zero_delay_and_gap(hass) -> None:
    """off preset: the pipeline still runs (seen/typing/stop/send), but pacing is inert."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock)  # default preset is "off"
    queue.enqueue("text", "1@c.us", {"text": "a"})
    queue.enqueue("text", "1@c.us", {"text": "b"})

    start = clock.now
    await _drain(queue)

    assert clock.now == start  # no delay, no gap -- fake clock never advanced
    paths = [call[1] for call in fake.calls]
    assert paths.count("/api/startTyping") == 2  # typing pipeline still exercised at "off"


async def test_wait_true_resolves_with_message_id(hass) -> None:
    """wait=True returns a future that resolves to {"message_id": ...} once sent."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock)
    future = queue.enqueue("text", "1@c.us", {"text": "hi"}, wait=True)

    await _drain(queue)

    result = await asyncio.wait_for(future, timeout=1)
    assert result == {"message_id": "fake-msg-1"}


async def test_normal_lane_depth_limit_rejects_new(hass) -> None:
    """Normal lane at max depth (50) rejects a new enqueue synchronously."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock, status="SCAN_QR_CODE")  # nothing drains
    for i in range(50):
        queue.enqueue("text", "1@c.us", {"text": str(i)})

    with pytest.raises(ServiceValidationError):
        queue.enqueue("text", "1@c.us", {"text": "one too many"})


async def test_alert_lane_drop_oldest_at_max_depth(hass) -> None:
    """Alert lane at max depth (20) drops the oldest and fires send_failed(queue_full)."""
    fake = FakeWaha()
    clock = FakeClock()
    failed: list[tuple[str, str]] = []
    queue = _queue(fake, clock, status="SCAN_QR_CODE", on_send_failed=lambda item, reason, amb: failed.append((item.chat_id, reason)))
    for i in range(20):
        queue.enqueue("text", f"chat-{i}@c.us", {"text": "x"}, lane=LANE_ALERT)

    queue.enqueue("text", "chat-20@c.us", {"text": "overflow"}, lane=LANE_ALERT)

    assert queue.alert_depth() == 20
    assert ("chat-0@c.us", "queue_full") in failed


async def test_alert_ttl_expiry_fires_send_failed_no_api_call(hass) -> None:
    """An alert item past its TTL is dropped without ever calling the API."""
    fake = FakeWaha()
    clock = FakeClock()
    failed = []
    queue = _queue(fake, clock, on_send_failed=lambda item, reason, amb: failed.append(reason))
    queue.enqueue("text", "1@c.us", {"text": "stale"}, lane=LANE_ALERT)
    clock.now += 901  # past the 15-minute TTL

    processed = await _drain(queue)

    assert processed == 1
    assert failed == ["ttl_expired"]
    assert fake.calls == []


async def test_three_pending_alerts_for_one_chat_coalesce(hass) -> None:
    """>=3 pending alerts for one chat coalesce into a single send."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock, status="SCAN_QR_CODE")  # keep them all pending
    queue.enqueue("text", "1@c.us", {"text": "one"}, lane=LANE_ALERT)
    queue.enqueue("text", "1@c.us", {"text": "two"}, lane=LANE_ALERT)
    queue.enqueue("text", "1@c.us", {"text": "three"}, lane=LANE_ALERT)

    assert queue.alert_depth() == 1
    assert queue._alert[0].coalesced_count == 3
    assert queue._alert[0].payload["text"] == "one\ntwo\nthree"


async def test_normal_lane_hourly_per_chat_cap(hass) -> None:
    """A normal-lane per-chat cap blocks further sends within the trailing hour."""
    fake = FakeWaha()
    clock = FakeClock()
    queue = _queue(fake, clock, normal_hourly_cap_per_chat=1)
    queue.enqueue("text", "1@c.us", {"text": "first"})
    await _drain(queue)
    assert len(fake.calls) > 0

    queue.enqueue("text", "1@c.us", {"text": "second"})
    processed = await _drain(queue)

    assert processed == 0  # capped -- waits, doesn't fail yet (under the 30 min wait bound)
    assert queue.normal_depth() == 1


async def test_normal_lane_cap_exceeded_after_max_wait(hass) -> None:
    """A capped item that's waited past its max-wait bound fails as cap_exceeded."""
    fake = FakeWaha()
    clock = FakeClock()
    failed = []
    queue = _queue(
        fake, clock, normal_hourly_cap_per_chat=1, on_send_failed=lambda item, reason, amb: failed.append(reason)
    )
    queue.enqueue("text", "1@c.us", {"text": "first"})
    await _drain(queue)

    queue.enqueue("text", "1@c.us", {"text": "second"})
    clock.now += 1801  # past the 30-minute max wait

    processed = await _drain(queue)

    assert processed == 1
    assert failed == ["cap_exceeded"]


async def test_463_cools_down_chat_only(hass) -> None:
    """A 463 response cools the chat down, not the whole session, and raises no restart call."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["463"])
    clock = FakeClock()
    cooldowns = []
    queue = _queue(fake, clock, on_cooldown=lambda chat_id, status: cooldowns.append((chat_id, status)))
    queue.enqueue("text", "1@c.us", {"text": "hi"})

    await _drain(queue)

    assert cooldowns == [("1@c.us", 463)]
    assert queue.is_session_cooling_down() is False
    assert "/api/sessions/default/start" not in [c[1] for c in fake.calls]


async def test_475_cools_down_whole_session(hass) -> None:
    """A 475 response cools the whole session down."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["475"])
    clock = FakeClock()
    queue = _queue(fake, clock)
    queue.enqueue("text", "1@c.us", {"text": "hi"})

    await _drain(queue)

    assert queue.is_session_cooling_down() is True


async def test_cooling_down_chat_is_skipped_not_processed(hass) -> None:
    """A chat under cooldown is skipped at dequeue time -- nothing sent to it."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["463"])
    clock = FakeClock()
    queue = _queue(fake, clock)
    queue.enqueue("text", "1@c.us", {"text": "first"})
    await _drain(queue)
    assert queue.is_session_cooling_down() is False

    queue.enqueue("text", "1@c.us", {"text": "second, should wait"})
    processed = await _drain(queue)

    assert processed == 0
    assert queue.normal_depth() == 1


async def test_ambiguous_timeout_never_retried(hass) -> None:
    """A timeout (ambiguous -- may have reached WAHA) is never retried, on either lane."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["timeout"])
    clock = FakeClock()
    failed = []
    queue = _queue(fake, clock, on_send_failed=lambda item, reason, amb: failed.append((reason, amb)))
    queue.enqueue("text", "1@c.us", {"text": "hi"}, lane=LANE_ALERT)

    await _drain(queue)

    send_calls = [c for c in fake.calls if c[1] == "/api/sendText"]
    assert len(send_calls) == 1  # no second sendText call
    assert failed == [("send_failed", True)]


async def test_connect_refused_retries_alert_lane_then_succeeds(hass) -> None:
    """A provable non-delivery (connect_refused) is retried on the alert lane, and succeeds."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["connect_refused", "ok"])
    clock = FakeClock()
    queue = _queue(fake, clock)
    queue.enqueue("text", "1@c.us", {"text": "hi"}, lane=LANE_ALERT)

    await _drain(queue)

    send_calls = [c for c in fake.calls if c[1] == "/api/sendText"]
    assert len(send_calls) == 2  # one failed attempt, one retry that succeeded


async def test_connect_refused_never_retried_on_normal_lane(hass) -> None:
    """Retries are alert-lane only -- normal lane just fails."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["connect_refused"])
    clock = FakeClock()
    failed = []
    queue = _queue(fake, clock, on_send_failed=lambda item, reason, amb: failed.append(reason))
    queue.enqueue("text", "1@c.us", {"text": "hi"}, lane=LANE_NORMAL)

    await _drain(queue)

    assert failed == ["unreachable"]


async def test_shutdown_fails_everything_still_queued(hass) -> None:
    """Cancelling the worker task fires send_failed(shutdown) for everything still queued."""
    fake = FakeWaha()
    clock = FakeClock()
    failed = []
    queue = _queue(fake, clock, status="SCAN_QR_CODE", on_send_failed=lambda item, reason, amb: failed.append(reason))
    queue.enqueue("text", "1@c.us", {"text": "a"})
    queue.enqueue("text", "2@c.us", {"text": "b"}, lane=LANE_ALERT)

    task = asyncio.ensure_future(queue.run())
    await asyncio.sleep(0)  # let it start and block on the idle wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(failed) == ["shutdown", "shutdown"]
    assert queue.normal_depth() == 0
    assert queue.alert_depth() == 0


async def test_alert_wakes_worker_out_of_normal_lane_gap(hass) -> None:
    """An alert enqueued while the worker is in a normal-lane gap sleep is picked up promptly,
    without waiting for the full gap to elapse. Real (but short) timings, real asyncio.sleep,
    to actually exercise the asyncio.wait/Event interruption mechanism -- this is the one test
    in this file that isn't run against the fake clock, on purpose."""
    fake = FakeWaha()
    # Zero typing delay (so the send happens immediately), a 5s normal-lane gap --
    # long enough that "the alert arrived well before it" is a meaningful assertion,
    # short enough the test doesn't take forever if the interruption fails to fire.
    pacing = PacingConfig(
        normal_base=0, normal_per_char=0, normal_lo=0, normal_hi=0,
        normal_gap_lo=5.0, normal_gap_hi=5.0,
        alert_delay_cap=0, alert_gap_lo=0, alert_gap_hi=0,
    )
    queue = _queue(fake, FakeClock(), pacing_override=pacing, sleep_fn=asyncio.sleep, random_fn=lambda lo, hi: lo)
    queue.enqueue("text", "1@c.us", {"text": "normal"})

    loop = asyncio.get_running_loop()
    start = loop.time()
    task = asyncio.ensure_future(queue.run())
    await asyncio.sleep(0.05)  # let the normal send complete and enter its 5s gap
    queue.enqueue("text", "2@c.us", {"text": "urgent"}, lane=LANE_ALERT)

    await asyncio.wait_for(_wait_for_alert_sent(fake), timeout=2.0)
    elapsed = loop.time() - start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert elapsed < 4.0  # well under the full 5s gap -- the alert interrupted it


async def _wait_for_alert_sent(fake: FakeWaha) -> None:
    while len([c for c in fake.calls if c[1] == "/api/sendText"]) < 2:
        await asyncio.sleep(0.01)
