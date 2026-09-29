"""WI-6: group caching and name resolution.

`api.normalize_groups` (milestone 1) stays the shape normalizer; this module
owns cache/TTL/WORKING-gating policy and name matching -- services-layer
concerns, not transport-layer ones.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from homeassistant.exceptions import ServiceValidationError

from .api import WahaClient
from .const import GROUP_CACHE_TTL_SECONDS, GROUP_REFRESH_MIN_INTERVAL_SECONDS, STATE_WORKING


@dataclass(frozen=True, slots=True)
class Group:
    id: str
    subject: str


class GroupCache:
    """Per-entry cache of WAHA groups. Fetches only while the session is WORKING."""

    def __init__(
        self,
        client: WahaClient,
        *,
        status_fn: Callable[[], str | None],
        ttl_seconds: float = GROUP_CACHE_TTL_SECONDS,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._status_fn = status_fn
        self._ttl = ttl_seconds
        self.now_fn = now_fn
        self._groups: list[Group] = []
        self._fetched_at: float | None = None
        self._last_refresh_at: float | None = None

    async def get_groups(self, *, force: bool = False) -> list[Group]:
        """Return the cached groups, refreshing if stale and the session is WORKING.
        Returns whatever's cached (possibly empty) if not WORKING -- never fetches then."""
        if self._status_fn() != STATE_WORKING:
            return list(self._groups)
        stale = self._fetched_at is None or (self.now_fn() - self._fetched_at) > self._ttl
        if force or stale:
            await self._fetch()
        return list(self._groups)

    async def refresh(self) -> list[Group]:
        """Explicit refresh (the refresh_groups service), rate-limited to avoid
        hitting WAHA's own groups/refresh rate-overlimit."""
        if self._last_refresh_at is not None:
            elapsed = self.now_fn() - self._last_refresh_at
            if elapsed < GROUP_REFRESH_MIN_INTERVAL_SECONDS:
                wait = GROUP_REFRESH_MIN_INTERVAL_SECONDS - elapsed
                raise ServiceValidationError(
                    f"Groups were refreshed too recently; try again in {int(wait)}s"
                )
        await self._fetch()
        self._last_refresh_at = self.now_fn()
        return list(self._groups)

    async def _fetch(self) -> None:
        raw = await self._client.get_groups()
        self._groups = [Group(id=g["id"], subject=g["subject"]) for g in raw]
        self._fetched_at = self.now_fn()

    @staticmethod
    def resolve(groups: list[Group], name: str) -> str:
        """Exact match -> case-insensitive exact match -> error listing every
        candidate. No substring matching, ever -- a wrong-group send is worse
        than a failure."""
        for group in groups:
            if group.subject == name:
                return group.id
        lowered = name.lower()
        matches = [g for g in groups if g.subject.lower() == lowered]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            candidates = ", ".join(f"{g.subject!r} ({g.id})" for g in matches)
            raise ServiceValidationError(f"Multiple groups match {name!r}: {candidates}")
        if groups:
            candidates = ", ".join(f"{g.subject!r}" for g in groups)
            raise ServiceValidationError(f"No group named {name!r} found. Known groups: {candidates}")
        raise ServiceValidationError(f"No group named {name!r} found (no groups cached yet)")
