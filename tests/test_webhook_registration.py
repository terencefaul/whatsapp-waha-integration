"""Tests for webhook_registration.py (WI-2, Verification item 4).

Every test asserts on fake.put_count / fake.calls, not just the returned
outcome -- the property under test is "our code never sends a PUT it
shouldn't", proven against a realistic full session body (noweb.store,
proxy, metadata, a foreign hook) so a naive implementation's data loss is
actually visible.
"""

from __future__ import annotations

import copy

from custom_components.whatsapp_waha import webhook_registration as wr
from custom_components.whatsapp_waha.api import WahaClient
from tests.fake_waha import FakeWaha

CALLBACK_URL = "http://homeassistant.local:8123/api/webhook/whatsapp_waha_abc123"
HMAC_KEY = "test-hmac-key"


def _client(fake: FakeWaha, name: str = "default") -> WahaClient:
    return WahaClient(fake, name)


async def test_missing_session_creates_with_webhook_no_put() -> None:
    """404 -> create-with-webhook, no PUT."""
    fake = FakeWaha()
    client = _client(fake)

    result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.CREATED
    assert fake.put_count == 0
    assert fake.sessions["default"]["config"]["webhooks"][0]["url"] == CALLBACK_URL


async def test_matching_hook_is_a_no_op() -> None:
    """An already-correct hook -> UNCHANGED, no PUT."""
    fake = FakeWaha()
    session = fake.add_session("default", status="WORKING")
    session["config"]["webhooks"] = [wr.build_desired_hook(CALLBACK_URL, HMAC_KEY)]
    client = _client(fake)

    result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.UNCHANGED
    assert fake.put_count == 0


async def test_drift_on_stopped_session_puts_once_preserving_everything_else() -> None:
    """Drift on a STOPPED session -> one PUT preserving noweb.store/proxy/metadata/foreign hooks."""
    fake = FakeWaha()
    session = fake.add_session("default", status="STOPPED")
    foreign_hook = {"url": "http://example.com/some-other-hook", "events": ["message"]}
    session["config"]["webhooks"] = [foreign_hook]
    original_noweb = copy.deepcopy(session["config"]["noweb"])
    original_proxy = copy.deepcopy(session["config"]["proxy"])
    original_metadata = copy.deepcopy(session["config"]["metadata"])
    client = _client(fake)

    result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.PUT_APPLIED
    assert fake.put_count == 1
    stored = fake.sessions["default"]["config"]
    assert stored["noweb"] == original_noweb
    assert stored["proxy"] == original_proxy
    assert stored["metadata"] == original_metadata
    assert foreign_hook in stored["webhooks"]
    urls = [h["url"] for h in stored["webhooks"]]
    assert CALLBACK_URL in urls
    assert len(stored["webhooks"]) == 2  # foreign hook kept, ours added


async def test_drift_replaces_only_our_stale_hook_keeps_foreign_ones() -> None:
    """An existing-but-stale hook of ours is replaced in place; a foreign hook is untouched."""
    fake = FakeWaha()
    session = fake.add_session("default", status="STOPPED")
    stale_ours = {
        "url": "http://old-ha-address:8123/api/webhook/whatsapp_waha_oldid",
        "events": ["message"],
        "hmac": {"key": "stale-key"},
        "retries": {"policy": "constant", "delaySeconds": 2, "attempts": 8},
    }
    foreign_hook = {"url": "http://example.com/some-other-hook", "events": ["message"]}
    session["config"]["webhooks"] = [foreign_hook, stale_ours]
    client = _client(fake)

    result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.PUT_APPLIED
    stored = fake.sessions["default"]["config"]["webhooks"]
    assert len(stored) == 2  # replaced in place, not appended as a third
    assert foreign_hook in stored
    assert {"url": CALLBACK_URL, "events": ["message", "message.any", "session.status"],
            "hmac": {"key": HMAC_KEY},
            "retries": {"policy": "constant", "delaySeconds": 2, "attempts": 8}} in stored
    assert stale_ours not in stored


async def test_drift_on_working_session_needs_consent_no_put() -> None:
    """Drift on a WORKING session -> no PUT, needs explicit consent (drives the webhook_drift repair)."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")  # no webhooks yet -> drift
    client = _client(fake)

    result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.NEEDS_CONSENT
    assert fake.put_count == 0


async def test_forced_put_on_working_session_is_allowed() -> None:
    """force_working_put=True (register_webhook service / repair confirm) allows the PUT."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    client = _client(fake)

    result = await wr.ensure_webhook(
        client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None, force_working_put=True
    )

    assert result.outcome == wr.RegistrationOutcome.PUT_APPLIED
    assert fake.put_count == 1


async def test_second_put_within_ten_minutes_is_refused() -> None:
    """A second PUT within the 10-minute lockout is refused, not silently retried."""
    fake = FakeWaha()
    fake.add_session("default", status="STOPPED")
    client = _client(fake)
    now = 1_000_000.0

    result = await wr.ensure_webhook(
        client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=now - 60, now=now
    )

    assert result.outcome == wr.RegistrationOutcome.RATE_LIMITED
    assert fake.put_count == 0


async def test_put_lockout_expires_after_ten_minutes() -> None:
    """After the 10-minute lockout window, a PUT is allowed again."""
    fake = FakeWaha()
    fake.add_session("default", status="STOPPED")
    client = _client(fake)
    now = 1_000_000.0

    result = await wr.ensure_webhook(
        client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=now - 601, now=now
    )

    assert result.outcome == wr.RegistrationOutcome.PUT_APPLIED
    assert fake.put_count == 1


async def test_post_put_mismatch_is_unfixable_and_does_not_loop() -> None:
    """If a PUT doesn't actually take (re-GET disagrees), stop -- raise unfixable, never loop."""
    fake = FakeWaha(corrupt_put_hmac=True)
    fake.add_session("default", status="STOPPED")
    client = _client(fake)

    result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.UNFIXABLE
    assert fake.put_count == 1  # exactly one PUT was attempted, then it stopped


async def test_unsupported_merge_put_never_puts_onto_existing_session() -> None:
    """Spike found GET redacts secrets / PUT doesn't round-trip -> never PUT an existing session."""
    wr.SUPPORTS_SAFE_MERGE_PUT = False
    try:
        fake = FakeWaha(supports_safe_merge_put=False)
        fake.add_session("default", status="STOPPED")  # drift: no hook yet
        client = _client(fake)

        result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

        assert result.outcome == wr.RegistrationOutcome.UNFIXABLE
        assert fake.put_count == 0
    finally:
        wr.SUPPORTS_SAFE_MERGE_PUT = True  # restore for other tests


async def test_unsupported_merge_put_create_branch_unaffected() -> None:
    """The 404/create branch is unaffected by the merge-PUT support flag -- nothing to redact."""
    wr.SUPPORTS_SAFE_MERGE_PUT = False
    try:
        fake = FakeWaha()
        client = _client(fake)

        result = await wr.ensure_webhook(client, "default", CALLBACK_URL, HMAC_KEY, last_put_at=None)

        assert result.outcome == wr.RegistrationOutcome.CREATED
        assert fake.put_count == 0
    finally:
        wr.SUPPORTS_SAFE_MERGE_PUT = True


async def test_remove_webhook_on_stopped_session_puts_once() -> None:
    """unregister_webhook's underlying remove_webhook drops only our hook."""
    fake = FakeWaha()
    session = fake.add_session("default", status="STOPPED")
    foreign_hook = {"url": "http://example.com/some-other-hook", "events": ["message"]}
    session["config"]["webhooks"] = [foreign_hook, wr.build_desired_hook(CALLBACK_URL, HMAC_KEY)]
    client = _client(fake)

    result = await wr.remove_webhook(client, "default", last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.PUT_APPLIED
    assert fake.sessions["default"]["config"]["webhooks"] == [foreign_hook]


async def test_remove_webhook_when_none_present_is_a_no_op() -> None:
    """Removing a hook that isn't there is a no-op, not an error (best-effort removal)."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    client = _client(fake)

    result = await wr.remove_webhook(client, "default", last_put_at=None)

    assert result.outcome == wr.RegistrationOutcome.UNCHANGED
    assert fake.put_count == 0
