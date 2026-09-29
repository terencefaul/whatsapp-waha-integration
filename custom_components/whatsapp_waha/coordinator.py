"""WI-3/WI-4: WahaCoordinator -- polls session status, and accepts pushed updates
from the webhook handler's session.status events.

A 401 from the API raises ConfigEntryAuthFailed directly, which
DataUpdateCoordinator itself turns into a call to
config_entry.async_start_reauth_if_available() -- no extra wiring needed
beyond raising the right exception type here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WahaAuthError, WahaClient, WahaConnectionError
from .const import COORDINATOR_UPDATE_INTERVAL_SECONDS, DOMAIN

if TYPE_CHECKING:
    from .groups import GroupCache
    from .send_queue import SendQueue

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WahaSessionData:
    """The coordinator's data: the session's current status and raw body."""

    status: str | None
    raw: dict[str, Any]


type WahaConfigEntry = ConfigEntry["WahaRuntimeData"]


@dataclass(slots=True)
class WahaRuntimeData:
    """entry.runtime_data -- everything async_setup_entry needs to hand to platforms."""

    client: WahaClient
    coordinator: "WahaCoordinator"
    send_queue: "SendQueue | None" = None
    group_cache: "GroupCache | None" = None
    # (kind, timestamp) of the last ensure_webhook/register/unregister outcome --
    # a live diagnostic fact separate from whether the connected sensor is green.
    last_webhook_registration: tuple[str, float] | None = None


class WahaCoordinator(DataUpdateCoordinator[WahaSessionData]):
    """Polls WAHA's session status every 60s as a reconciler; also accepts
    immediate pushes from session.status webhook deliveries."""

    config_entry: WahaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: WahaConfigEntry, client: WahaClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_UPDATE_INTERVAL_SECONDS),
        )
        self._client = client

    async def _async_update_data(self) -> WahaSessionData:
        """Fetch the current session status. 401 -> reauth; network error -> UpdateFailed."""
        try:
            session = await self._client.get_session()
        except WahaAuthError as err:
            raise ConfigEntryAuthFailed("WAHA rejected the API key (401)") from err
        except WahaConnectionError as err:
            raise UpdateFailed(f"Could not reach WAHA: {err}") from err

        if session is None:
            raise UpdateFailed("WAHA session no longer exists")

        return WahaSessionData(status=session.get("status"), raw=session)

    def push_status_update(self, status: str) -> None:
        """Called by the webhook handler on a session.status event -- pushes
        an immediate update rather than waiting for the next 60s poll."""
        raw = dict(self.data.raw) if self.data else {}
        raw["status"] = status
        self.async_set_updated_data(WahaSessionData(status=status, raw=raw))
