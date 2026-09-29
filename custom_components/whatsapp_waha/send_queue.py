"""WI-5: the two-lane, human-paced send queue.

"The part that quietly breaks" #2 in the technical plan. One worker per
config entry, two lanes (normal: human-paced; alert: fast, for urgent
notices), draining only while the session is WORKING. Every drop/expiry/
failure calls back through `on_send_failed` rather than disappearing
silently.

Ships with DEFAULT_SEND_QUEUE_PRESET = "off": at "off" the typing pipeline
(sendSeen/startTyping/stopTyping) still runs -- only the delay and the
inter-message gap collapse to 0 -- so the crash-recovery marker logic gets
real production exercise from day one instead of staying dead code until
pacing is turned on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .api import WahaClient, WahaConnectionError, WahaError, WahaRateLimitedError, WahaUnreachableError
from .const import (
    ALERT_COALESCE_THRESHOLD,
    ALERT_HOURLY_CEILING_DEFAULT,
    ALERT_LANE_MAX_DEPTH,
    ALERT_TTL_SECONDS,
    LANE_ALERT,
    LANE_NORMAL,
    NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT,
    NORMAL_HOURLY_CAP_PER_SESSION_DEFAULT,
    NORMAL_LANE_MAX_DEPTH,
    NORMAL_LANE_MAX_WAIT_SECONDS,
    PRESET_BALANCED,
    PRESET_OFF,
    PRESET_STRICT,
    SEND_RATE_LIMIT_COOLDOWN_SECONDS,
    STATE_WORKING,
    UNREACHABLE_RETRY_BACKOFF_SECONDS,
    UNREACHABLE_RETRY_MAX_ATTEMPTS,
)

_LOGGER = logging.getLogger(__name__)


class MarkerStore(Protocol):
    """Duck-typed to match homeassistant.helpers.storage.Store exactly."""

    async def async_load(self) -> Any: ...
    async def async_save(self, data: Any) -> None: ...


@dataclass(slots=True)
class SendItem:
    """One queued send. `futures` may hold more than one entry once coalesced."""

    id: str
    chat_id: str
    kind: str  # "text"|"image"|"file"|"voice"|"video"|"sticker"|"poll"|"location"|"reaction"
    payload: dict[str, Any]
    lane: str
    enqueued_at: float
    expires_at: float | None = None
    futures: list[asyncio.Future] = field(default_factory=list)
    attempt: int = 0
    coalesced_count: int = 1


@dataclass(frozen=True, slots=True)
class PacingConfig:
    """Typing-delay and inter-message-gap parameters for one preset."""

    normal_base: float
    normal_per_char: float
    normal_lo: float
    normal_hi: float
    normal_gap_lo: float
    normal_gap_hi: float
    alert_delay_cap: float
    alert_gap_lo: float
    alert_gap_hi: float


PRESETS: dict[str, PacingConfig] = {
    PRESET_OFF: PacingConfig(0, 0, 0, 0, 0, 0, 0, 0, 0),
    # Not specified in the design doc -- a provisional midpoint, inert while
    # DEFAULT_SEND_QUEUE_PRESET stays "off"; needs real-world tuning.
    PRESET_BALANCED: PacingConfig(0.5, 0.03, 0.5, 5.0, 10.0, 20.0, 1.0, 1.0, 3.0),
    # The anti-ban guide's own numbers, per the design doc.
    PRESET_STRICT: PacingConfig(1.0, 0.06, 1.0, 10.0, 30.0, 60.0, 2.0, 2.0, 5.0),
}


def _clamp(value: float, lo: float, hi: float) -> float:
    if hi <= 0:
        return 0.0
    return max(lo, min(hi, value))


class SendQueue:
    """One per config entry. Injectable now_fn/sleep_fn/random_fn match api.py's
    WahaTransport seam so tests run against a fake clock, no monkeypatching."""

    def __init__(
        self,
        client: WahaClient,
        session_name: str,
        *,
        status_fn: Callable[[], str | None],
        preset: str = PRESET_OFF,
        pacing_override: PacingConfig | None = None,
        normal_hourly_cap_per_chat: int = NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT,
        normal_hourly_cap_per_session: int = NORMAL_HOURLY_CAP_PER_SESSION_DEFAULT,
        alert_hourly_ceiling: int = ALERT_HOURLY_CEILING_DEFAULT,
        marker_store: MarkerStore | None = None,
        on_send_failed: Callable[[SendItem, str, bool], None] | None = None,
        on_cooldown: Callable[[str, int], None] | None = None,
        now_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        random_fn: Callable[[float, float], float] | None = None,
        idle_poll_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.session_name = session_name
        self.status_fn = status_fn
        self._pacing = pacing_override if pacing_override is not None else PRESETS[preset]
        self._normal_hourly_cap_per_chat = normal_hourly_cap_per_chat
        self._normal_hourly_cap_per_session = normal_hourly_cap_per_session
        self._alert_hourly_ceiling = alert_hourly_ceiling
        self._marker_store = marker_store
        self.on_send_failed = on_send_failed
        self.on_cooldown = on_cooldown
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.random_fn = random_fn or random.uniform
        self._idle_poll_seconds = idle_poll_seconds

        self._normal: deque[SendItem] = deque()
        self._alert: deque[SendItem] = deque()
        self._wake_event = asyncio.Event()
        self._chat_cooldowns: dict[str, float] = {}
        self._session_cooldown_until: float | None = None
        self._normal_chat_sends: dict[str, deque[float]] = {}
        self._normal_session_sends: deque[float] = deque()
        self._alert_chat_sends: dict[str, deque[float]] = {}
        self._failure_counts: dict[str, int] = {}
        self._inflight_send: asyncio.Task | None = None

    # --- public introspection (diagnostics, services) ---

    def normal_depth(self) -> int:
        return len(self._normal)

    def alert_depth(self) -> int:
        return len(self._alert)

    def chat_cooldowns(self) -> dict[str, float]:
        now = self.now_fn()
        return {chat: until for chat, until in self._chat_cooldowns.items() if until > now}

    @property
    def session_cooldown_until(self) -> float | None:
        if self._session_cooldown_until is not None and self._session_cooldown_until <= self.now_fn():
            return None
        return self._session_cooldown_until

    def failure_counts(self) -> dict[str, int]:
        return dict(self._failure_counts)

    def is_session_cooling_down(self) -> bool:
        return self.session_cooldown_until is not None

    def position_of(self, item_id: str) -> int | None:
        for lane in (self._alert, self._normal):
            for index, item in enumerate(lane):
                if item.id == item_id:
                    return index
        return None

    # --- enqueue ---

    def enqueue(
        self, kind: str, chat_id: str, payload: dict[str, Any], *, lane: str = LANE_NORMAL, wait: bool = False
    ) -> dict[str, Any] | asyncio.Future:
        """Enqueue one send. Returns {"queued", "item_id", "lane", "position"} unless
        wait=True, in which case it returns a Future the caller awaits for {"message_id"}."""
        if lane == LANE_NORMAL and len(self._normal) >= NORMAL_LANE_MAX_DEPTH:
            raise ServiceValidationError("The normal-lane send queue is full; try again shortly")

        future: asyncio.Future | None = None
        if wait:
            future = asyncio.get_running_loop().create_future()

        item = SendItem(
            id=uuid.uuid4().hex,
            chat_id=chat_id,
            kind=kind,
            payload=payload,
            lane=lane,
            enqueued_at=self.now_fn(),
            expires_at=(self.now_fn() + ALERT_TTL_SECONDS) if lane == LANE_ALERT else None,
            futures=[future] if future is not None else [],
        )

        if lane == LANE_ALERT:
            merged = self._maybe_coalesce(item)
            if merged is None:
                if len(self._alert) >= ALERT_LANE_MAX_DEPTH:
                    dropped = self._alert.popleft()
                    self._fail(dropped, reason="queue_full")
                self._alert.append(item)
        else:
            self._normal.append(item)

        self._wake_event.set()

        if wait:
            return future
        return {"queued": True, "item_id": item.id, "lane": lane, "position": self.position_of(item.id)}

    def _maybe_coalesce(self, item: SendItem) -> SendItem | None:
        """If either coalescing trigger (design doc) is met, merge every
        currently-pending alert for this chat -- plus the new one -- into a
        single item. Returns the merged item, or None if `item` should be
        enqueued as-is."""
        if item.kind != "text":
            return None
        pending = [i for i in self._alert if i.chat_id == item.chat_id and i.kind == "text"]
        sent_recently = self._trailing_hour_count(self._alert_chat_sends.get(item.chat_id))
        should_coalesce = (len(pending) + 1 >= ALERT_COALESCE_THRESHOLD) or (
            sent_recently >= self._alert_hourly_ceiling
        )
        if not should_coalesce or not pending:
            return None
        target = pending[0]
        for other in pending[1:]:
            target.payload["text"] = f"{target.payload['text']}\n{other.payload['text']}"
            target.coalesced_count += other.coalesced_count
            target.futures.extend(other.futures)
            self._alert.remove(other)
        target.payload["text"] = f"{target.payload['text']}\n{item.payload['text']}"
        target.coalesced_count += 1
        target.futures.extend(item.futures)
        return target

    # --- caps / cooldowns ---

    def _trailing_hour_count(self, dq: deque[float] | None) -> int:
        if not dq:
            return 0
        cutoff = self.now_fn() - 3600
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)

    def _would_exceed_chat_cap(self, chat_id: str) -> bool:
        if self._normal_hourly_cap_per_chat <= 0:
            return False
        dq = self._normal_chat_sends.setdefault(chat_id, deque())
        return self._trailing_hour_count(dq) >= self._normal_hourly_cap_per_chat

    def _would_exceed_session_cap(self) -> bool:
        if self._normal_hourly_cap_per_session <= 0:
            return False
        return self._trailing_hour_count(self._normal_session_sends) >= self._normal_hourly_cap_per_session

    def _is_chat_cooling_down(self, chat_id: str) -> bool:
        until = self._chat_cooldowns.get(chat_id)
        return until is not None and self.now_fn() < until

    def _apply_cooldown(self, chat_id: str, status: int) -> None:
        until = self.now_fn() + SEND_RATE_LIMIT_COOLDOWN_SECONDS
        if status == 475:
            self._session_cooldown_until = until
        else:
            self._chat_cooldowns[chat_id] = until
        if self.on_cooldown is not None:
            self.on_cooldown(chat_id, status)

    def _record_send(self, item: SendItem) -> None:
        if item.lane == LANE_NORMAL:
            self._normal_chat_sends.setdefault(item.chat_id, deque()).append(self.now_fn())
            self._normal_session_sends.append(self.now_fn())
        else:
            self._alert_chat_sends.setdefault(item.chat_id, deque()).append(self.now_fn())

    # --- dequeue / processing ---

    def _peek_next_eligible(self) -> SendItem | None:
        for item in list(self._alert):
            if self._is_chat_cooling_down(item.chat_id) or self.is_session_cooling_down():
                continue
            return item
        if self._normal:
            head = self._normal[0]
            if self._is_chat_cooling_down(head.chat_id) or self.is_session_cooling_down():
                return None
            if self._would_exceed_chat_cap(head.chat_id) or self._would_exceed_session_cap():
                if self.now_fn() - head.enqueued_at > NORMAL_LANE_MAX_WAIT_SECONDS:
                    return head  # over its max wait -- process_one will fail it as cap_exceeded
                return None
            return head
        return None

    def _remove(self, item: SendItem) -> None:
        lane = self._alert if item.lane == LANE_ALERT else self._normal
        with contextlib.suppress(ValueError):
            lane.remove(item)

    async def process_one(self) -> bool:
        """Process at most one item. Returns True if it did anything (sent,
        failed, or expired something), False if nothing was eligible."""
        item = self._peek_next_eligible()
        if item is None:
            return False

        if item.lane == LANE_ALERT and item.expires_at is not None and self.now_fn() > item.expires_at:
            self._remove(item)
            self._fail(item, reason="ttl_expired")
            return True

        if item.lane == LANE_NORMAL and (
            self._would_exceed_chat_cap(item.chat_id) or self._would_exceed_session_cap()
        ):
            self._remove(item)
            self._fail(item, reason="cap_exceeded")
            return True

        if self.status_fn() != STATE_WORKING:
            return False

        self._remove(item)
        await self._run_pipeline(item)
        return True

    async def _run_pipeline(self, item: SendItem) -> None:
        if item.payload.get("reply_to"):
            with contextlib.suppress(WahaError):
                await self.client.send_seen(item.chat_id, session=self.session_name)

        await self._save_typing_marker(item.chat_id)
        with contextlib.suppress(WahaError):
            await self.client.start_typing(item.chat_id, session=self.session_name)
        try:
            await self.sleep_fn(self._compute_delay(item))
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.client.stop_typing(item.chat_id, session=self.session_name), timeout=5
                )
            await self._clear_typing_marker()

        try:
            result = await self._send(item)
        except WahaRateLimitedError as err:
            self._apply_cooldown(item.chat_id, err.status)
            self._fail(item, reason="rate_limited")
            return
        except WahaUnreachableError as err:
            await self._maybe_retry(item, err)
            return
        except WahaConnectionError as err:
            self._fail(item, reason="send_failed", ambiguous=err.ambiguous)
            return

        self._succeed(item, result)
        await self._interruptible_wait(self._compute_gap(item))

    async def _send(self, item: SendItem) -> dict[str, Any]:
        coro = self._dispatch_send(item)
        task = asyncio.ensure_future(coro)
        self._inflight_send = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight_send = None

    async def _dispatch_send(self, item: SendItem) -> dict[str, Any]:
        payload = item.payload
        if item.kind == "text":
            return await self.client.send_text(
                item.chat_id,
                payload["text"],
                reply_to=payload.get("reply_to"),
                link_preview=payload.get("link_preview", False),
                session=self.session_name,
            )
        if item.kind in ("image", "file", "voice", "video", "sticker"):
            method = getattr(self.client, f"send_{item.kind}")
            return await method(item.chat_id, session=self.session_name, **payload)
        if item.kind == "poll":
            return await self.client.send_poll(item.chat_id, session=self.session_name, **payload)
        if item.kind == "location":
            return await self.client.send_location(item.chat_id, session=self.session_name, **payload)
        if item.kind == "reaction":
            return await self.client.send_reaction(session=self.session_name, **payload)
        raise ValueError(f"Unknown send kind {item.kind!r}")

    async def _maybe_retry(self, item: SendItem, err: WahaUnreachableError) -> None:
        if item.lane != LANE_ALERT or item.attempt >= UNREACHABLE_RETRY_MAX_ATTEMPTS:
            self._fail(item, reason="unreachable", ambiguous=False)
            return
        backoff = UNREACHABLE_RETRY_BACKOFF_SECONDS[min(item.attempt, len(UNREACHABLE_RETRY_BACKOFF_SECONDS) - 1)]
        item.attempt += 1
        await self.sleep_fn(backoff)
        if item.expires_at is not None and self.now_fn() > item.expires_at:
            self._fail(item, reason="ttl_expired")
            return
        self._alert.appendleft(item)
        self._wake_event.set()

    def _compute_delay(self, item: SendItem) -> float:
        cfg = self._pacing
        if item.lane == LANE_ALERT:
            base = cfg.alert_delay_cap
        else:
            text_len = len(item.payload.get("text", "")) if item.kind == "text" else 0
            base = _clamp(cfg.normal_base + text_len * cfg.normal_per_char, cfg.normal_lo, cfg.normal_hi)
        if base <= 0:
            return 0.0
        return base * self.random_fn(0.8, 1.3)

    def _compute_gap(self, item: SendItem) -> float:
        cfg = self._pacing
        lo, hi = (cfg.alert_gap_lo, cfg.alert_gap_hi) if item.lane == LANE_ALERT else (cfg.normal_gap_lo, cfg.normal_gap_hi)
        if hi <= 0:
            return 0.0
        return self.random_fn(lo, hi)

    async def _interruptible_wait(self, gap: float) -> None:
        if gap <= 0:
            return
        sleep_task = asyncio.ensure_future(self.sleep_fn(gap))
        wake_task = asyncio.ensure_future(self._wake_event.wait())
        try:
            done, _pending = await asyncio.wait({sleep_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel and fully reap both tasks even if this wait itself was
            # cancelled (e.g. the worker task is being shut down) -- an
            # unreaped task shows up as "pending" in asyncio.all_tasks() and
            # the test harness's lingering-task check treats that as a failure.
            for task in (sleep_task, wake_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, wake_task, return_exceptions=True)
        if wake_task in done:
            self._wake_event.clear()

    # --- outcome bookkeeping ---

    def _succeed(self, item: SendItem, result: dict[str, Any]) -> None:
        self._record_send(item)
        message_id = result.get("id") or (result.get("_data") or {}).get("id")
        if isinstance(message_id, dict):
            message_id = message_id.get("_serialized")
        for future in item.futures:
            if not future.done():
                future.set_result({"message_id": message_id})

    def _fail(self, item: SendItem, *, reason: str, ambiguous: bool = False) -> None:
        self._failure_counts[reason] = self._failure_counts.get(reason, 0) + 1
        for future in item.futures:
            if not future.done():
                future.set_exception(HomeAssistantError(f"WhatsApp send failed: {reason}"))
        if self.on_send_failed is not None:
            self.on_send_failed(item, reason, ambiguous)

    # --- crash recovery ---

    async def _save_typing_marker(self, chat_id: str) -> None:
        if self._marker_store is not None:
            await self._marker_store.async_save({"chat_id": chat_id, "session": self.session_name})

    async def _clear_typing_marker(self) -> None:
        if self._marker_store is not None:
            await self._marker_store.async_save({})

    async def recover_from_crash(self) -> None:
        """Call once after the first successful coordinator refresh. If a typing
        marker survived a hard crash and the session is WORKING, send one
        best-effort stopTyping and clear it regardless of the outcome."""
        if self._marker_store is None:
            return
        marker = await self._marker_store.async_load()
        if not marker:
            return
        if self.status_fn() == STATE_WORKING:
            with contextlib.suppress(Exception):
                await self.client.stop_typing(marker["chat_id"], session=marker.get("session", self.session_name))
        await self._marker_store.async_save({})

    # --- worker lifecycle ---

    async def run(self) -> None:
        """The persistent worker loop. Run via entry.async_create_background_task,
        which cancels it automatically on unload -- see _shutdown_cleanup."""
        try:
            while True:
                processed = await self.process_one()
                if not processed:
                    await self._interruptible_wait(self._idle_poll_seconds)
        finally:
            await self._shutdown_cleanup()

    async def _shutdown_cleanup(self) -> None:
        if self._inflight_send is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._inflight_send), timeout=10)
        for lane in (self._alert, self._normal):
            while lane:
                item = lane.popleft()
                self._fail(item, reason="shutdown")
