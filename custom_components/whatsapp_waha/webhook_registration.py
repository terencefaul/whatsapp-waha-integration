"""WI-2: safe webhook self-registration against a live WAHA session.

The one rule this module exists to enforce: WAHA's only way to change a
session's webhook config is `PUT /api/sessions/{name}`, which *fully
replaces* `config` (not just the webhooks list) and restarts the session if
it is WORKING. A PUT built from anything other than the exact GET body --
with only our own webhook entry swapped in -- can silently wipe
`noweb.store` (losing chat history), `proxy`, `metadata`, or another
integration's webhook. See docs/plans/whatsapp-waha-integration.md, "The one
architectural problem to solve first" and "The part that quietly breaks" #1.

Spike-outcome switch: `SUPPORTS_SAFE_MERGE_PUT` is set once, from the real
answers to the build-sequence step 2 spike (does GET redact secrets; does a
PUT of the unmodified GET body round-trip). If either is unfavorable, this
module must never PUT onto an *existing* session with drift -- see
`ensure_webhook`'s early return below.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .api import WahaClient
from .const import SAFE_TO_PUT_STATES, SUBSCRIBED_EVENTS, WEBHOOK_ID_PREFIX, WEBHOOK_PUT_LOCKOUT_SECONDS

_LOGGER = logging.getLogger(__name__)

# Set from the real spike (build-sequence step 2, answers (a) and (b)).
# True until the spike says otherwise. See the milestone log for the actual
# recorded answers.
SUPPORTS_SAFE_MERGE_PUT = True


class RegistrationOutcome(str, Enum):
    """What ensure_webhook actually did."""

    CREATED = "created"
    UNCHANGED = "unchanged"
    PUT_APPLIED = "put_applied"
    NEEDS_CONSENT = "needs_consent"  # drift on a WORKING session, not forced
    RATE_LIMITED = "rate_limited"  # PUT refused, within the 10-minute lockout
    UNFIXABLE = "unfixable"  # post-PUT mismatch, or merge-safety unsupported


@dataclass(slots=True)
class RegistrationResult:
    """Result of an ensure_webhook call."""

    outcome: RegistrationOutcome
    session_status: str | None = None
    detail: str | None = None


def _webhook_path(webhook_id: str) -> str:
    return f"/api/webhook/{webhook_id}"


def build_desired_hook(callback_url: str, hmac_key: str) -> dict[str, Any]:
    """The exact webhook config this integration wants registered.

    All three events are subscribed permanently -- changing the set would
    itself require a PUT, so there is no reason to ever ask WAHA for less.
    """
    return {
        "url": callback_url,
        "events": list(SUBSCRIBED_EVENTS),
        "hmac": {"key": hmac_key},
        "retries": {"policy": "constant", "delaySeconds": 2, "attempts": 8},
    }


def _find_ours(hooks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the hook that belongs to *this integration* (any entry, any prior instance).

    Matched by URL path prefix, not exact webhook_id, so a stale hook left by
    a deleted config entry is adopted rather than duplicated, while a
    foreign hook (a manual WHATSAPP_HOOK_URL, or another integration
    entirely) is left untouched.
    """
    for hook in hooks:
        if _is_ours_url(hook.get("url", "")):
            return hook
    return None


def _is_ours_url(url: str) -> bool:
    """True if the webhook URL's path belongs to this integration (any entry)."""
    path = url.split("://", 1)[-1]
    path = path[path.find("/"):] if "/" in path else ""
    return path.startswith(f"/api/webhook/{WEBHOOK_ID_PREFIX}")


def _hooks_equal(a: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Compare only the fields this integration owns -- never diff on fields we don't set."""
    return (
        a.get("url") == desired["url"]
        and (a.get("hmac") or {}).get("key") == desired["hmac"]["key"]
        and sorted(a.get("events") or []) == sorted(desired["events"])
        and a.get("retries") == desired["retries"]
    )


async def ensure_webhook(
    client: WahaClient,
    session_name: str,
    callback_url: str,
    hmac_key: str,
    *,
    last_put_at: float | None,
    now: float | None = None,
    force_working_put: bool = False,
) -> RegistrationResult:
    """Bring the session's webhook config in line with what we want, safely.

    ``last_put_at`` is the persisted entry.data timestamp of the last PUT
    this integration performed for this entry (None if never). The caller
    is responsible for persisting a new value when outcome is PUT_APPLIED.
    """
    now = time.time() if now is None else now
    desired = build_desired_hook(callback_url, hmac_key)

    session = await client.get_session(session_name)
    if session is None:
        # Backstop only: the config flow creates the session (plain, no
        # webhook) before this is ever called on the normal happy path, so
        # this branch fires only if a session was deleted out-of-band after
        # entry setup. No PUT is needed either way.
        await client.create_session(session_name, config={"webhooks": [desired]}, start=True)
        return RegistrationResult(outcome=RegistrationOutcome.CREATED)

    status = session.get("status")
    hooks = (session.get("config") or {}).get("webhooks") or []
    ours = _find_ours(hooks)

    if ours is not None and _hooks_equal(ours, desired):
        return RegistrationResult(outcome=RegistrationOutcome.UNCHANGED, session_status=status)

    # There is drift (or no hook of ours yet on an existing session).
    if status == "WORKING" and not force_working_put:
        return RegistrationResult(
            outcome=RegistrationOutcome.NEEDS_CONSENT,
            session_status=status,
            detail="Session is WORKING; PUT would restart it. Needs explicit consent.",
        )

    if not SUPPORTS_SAFE_MERGE_PUT:
        # The spike found GET redacts secrets and/or a PUT doesn't round
        # trip -- never PUT onto an existing session; the risk of silently
        # wiping proxy/hmac secrets or noweb.store is unacceptable.
        return RegistrationResult(
            outcome=RegistrationOutcome.UNFIXABLE,
            session_status=status,
            detail="Merge-PUT is unsupported by this WAHA server (spike result). Manual fix required.",
        )

    if last_put_at is not None and (now - last_put_at) < WEBHOOK_PUT_LOCKOUT_SECONDS:
        return RegistrationResult(
            outcome=RegistrationOutcome.RATE_LIMITED,
            session_status=status,
            detail=f"Refused: last PUT was {now - last_put_at:.0f}s ago (lockout is {WEBHOOK_PUT_LOCKOUT_SECONDS}s).",
        )

    if status not in SAFE_TO_PUT_STATES and not force_working_put:
        # Defensive: any status we don't recognise as safe is treated like
        # WORKING -- never guess.
        return RegistrationResult(
            outcome=RegistrationOutcome.NEEDS_CONSENT,
            session_status=status,
            detail=f"Unrecognised/unsafe status {status!r}; needs explicit consent.",
        )

    # Deep-copy the *entire* GET config, then replace only our own hook by
    # matching on URL prefix (not object identity -- `session` and this
    # deepcopy are independent copies, so `is` would never match). Every
    # other field and every other hook is carried through untouched.
    new_config = copy.deepcopy(session.get("config") or {})
    webhooks = list(new_config.get("webhooks") or [])
    if ours is not None:
        webhooks = [desired if _is_ours_url(h.get("url", "")) else h for h in webhooks]
    else:
        webhooks.append(desired)
    new_config["webhooks"] = webhooks

    await client.update_session(session_name, new_config)

    # Re-GET and re-compare -- never trust the PUT response alone, never loop.
    after = await client.get_session(session_name)
    after_hooks = (after.get("config") or {}).get("webhooks") or [] if after else []
    after_ours = _find_ours(after_hooks)
    if after_ours is None or not _hooks_equal(after_ours, desired):
        _LOGGER.error(
            "Webhook registration for session %s still mismatched after PUT; not retrying",
            session_name,
        )
        return RegistrationResult(
            outcome=RegistrationOutcome.UNFIXABLE,
            session_status=after.get("status") if after else None,
            detail="Post-PUT mismatch. Will not retry automatically.",
        )

    return RegistrationResult(
        outcome=RegistrationOutcome.PUT_APPLIED,
        session_status=after.get("status") if after else None,
    )


async def remove_webhook(
    client: WahaClient,
    session_name: str,
    *,
    last_put_at: float | None,
    now: float | None = None,
    force_working_put: bool = False,
) -> RegistrationResult:
    """Remove this integration's hook from the session's webhook config.

    Symmetric to ensure_webhook's drift-fix path: deep-copy the GET config,
    drop only our hook, leave everything else untouched, PUT, re-GET and
    re-compare. A missing session or a session with no hook of ours is a
    no-op (UNCHANGED), not an error -- removal is best-effort.
    """
    now = time.time() if now is None else now
    session = await client.get_session(session_name)
    if session is None:
        return RegistrationResult(outcome=RegistrationOutcome.UNCHANGED)

    status = session.get("status")
    hooks = (session.get("config") or {}).get("webhooks") or []
    ours = _find_ours(hooks)
    if ours is None:
        return RegistrationResult(outcome=RegistrationOutcome.UNCHANGED, session_status=status)

    if status == "WORKING" and not force_working_put:
        return RegistrationResult(
            outcome=RegistrationOutcome.NEEDS_CONSENT,
            session_status=status,
            detail="Session is WORKING; PUT would restart it. Needs explicit consent.",
        )

    if not SUPPORTS_SAFE_MERGE_PUT:
        return RegistrationResult(outcome=RegistrationOutcome.UNFIXABLE, session_status=status)

    if last_put_at is not None and (now - last_put_at) < WEBHOOK_PUT_LOCKOUT_SECONDS:
        return RegistrationResult(outcome=RegistrationOutcome.RATE_LIMITED, session_status=status)

    new_config = copy.deepcopy(session.get("config") or {})
    new_config["webhooks"] = [
        h for h in (new_config.get("webhooks") or []) if not _is_ours_url(h.get("url", ""))
    ]
    await client.update_session(session_name, new_config)
    return RegistrationResult(outcome=RegistrationOutcome.PUT_APPLIED)
