"""Tests for groups.py (WI-6, Verification item 6)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.whatsapp_waha.api import WahaClient
from custom_components.whatsapp_waha.const import STATE_SCAN_QR_CODE, STATE_WORKING
from custom_components.whatsapp_waha.groups import Group, GroupCache
from tests.fake_waha import FakeWaha


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def now_fn(self) -> float:
        return self.now


def _cache(fake: FakeWaha, clock: FakeClock, *, status: str = STATE_WORKING) -> GroupCache:
    client = WahaClient(fake, "default")
    return GroupCache(client, status_fn=lambda: status, now_fn=clock.now_fn)


async def test_noweb_dict_shape_resolves() -> None:
    fake = FakeWaha(groups_shape="dict")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "Family"}])
    cache = _cache(fake, FakeClock())

    groups = await cache.get_groups()

    assert groups == [Group(id="1@g.us", subject="Family")]


async def test_webjs_list_shape_resolves() -> None:
    fake = FakeWaha(groups_shape="list")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "Family"}])
    cache = _cache(fake, FakeClock())

    groups = await cache.get_groups()

    assert groups == [Group(id="1@g.us", subject="Family")]


async def test_not_fetched_before_working() -> None:
    """Never fetches while the session isn't WORKING -- returns whatever's cached (empty)."""
    fake = FakeWaha(groups_shape="dict")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "Family"}])
    cache = _cache(fake, FakeClock(), status=STATE_SCAN_QR_CODE)

    groups = await cache.get_groups()

    assert groups == []
    assert fake.calls == []


async def test_cache_is_reused_within_ttl() -> None:
    fake = FakeWaha(groups_shape="dict")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "Family"}])
    clock = FakeClock()
    cache = _cache(fake, clock)

    await cache.get_groups()
    await cache.get_groups()

    groups_calls = [c for c in fake.calls if c[1].endswith("/groups")]
    assert len(groups_calls) == 1


async def test_cache_refetches_after_ttl() -> None:
    fake = FakeWaha(groups_shape="dict")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "Family"}])
    clock = FakeClock()
    cache = _cache(fake, clock)
    await cache.get_groups()

    clock.now += 601  # past the 10-minute TTL
    await cache.get_groups()

    groups_calls = [c for c in fake.calls if c[1].endswith("/groups")]
    assert len(groups_calls) == 2


async def test_refresh_service_is_rate_limited() -> None:
    fake = FakeWaha(groups_shape="dict")
    clock = FakeClock()
    cache = _cache(fake, clock)
    await cache.refresh()

    with pytest.raises(ServiceValidationError):
        await cache.refresh()


async def test_refresh_allowed_after_min_interval() -> None:
    fake = FakeWaha(groups_shape="dict")
    clock = FakeClock()
    cache = _cache(fake, clock)
    await cache.refresh()

    clock.now += 301  # past the 5-minute minimum interval
    await cache.refresh()  # should not raise

    groups_calls = [c for c in fake.calls if c[1].endswith("/groups")]
    assert len(groups_calls) == 2


def test_resolve_exact_match() -> None:
    groups = [Group(id="1@g.us", subject="Family"), Group(id="2@g.us", subject="Work")]
    assert GroupCache.resolve(groups, "Family") == "1@g.us"


def test_resolve_case_insensitive_exact_match() -> None:
    groups = [Group(id="1@g.us", subject="Family")]
    assert GroupCache.resolve(groups, "family") == "1@g.us"


def test_resolve_no_match_raises_with_candidates() -> None:
    groups = [Group(id="1@g.us", subject="Family"), Group(id="2@g.us", subject="Work")]
    with pytest.raises(ServiceValidationError) as exc_info:
        GroupCache.resolve(groups, "Fam")  # a substring -- must NOT match
    assert "Family" in str(exc_info.value)
    assert "Work" in str(exc_info.value)


def test_resolve_ambiguous_case_insensitive_match_raises() -> None:
    groups = [Group(id="1@g.us", subject="family"), Group(id="2@g.us", subject="Family")]
    with pytest.raises(ServiceValidationError):
        GroupCache.resolve(groups, "FAMILY")


def test_resolve_no_groups_cached_yet() -> None:
    with pytest.raises(ServiceValidationError):
        GroupCache.resolve([], "Family")
