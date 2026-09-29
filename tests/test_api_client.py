"""Tests for api.py (WI-1) and FakeWaha fidelity (Verification item 9)."""

from __future__ import annotations

import pytest

from custom_components.whatsapp_waha.api import (
    WahaAuthError,
    WahaClient,
    WahaConnectionError,
    WahaPermissionError,
    WahaRateLimitedError,
    WahaUnreachableError,
    normalize_groups,
)
from tests.fake_waha import FakeWaha


async def test_get_sessions_running_only_by_default() -> None:
    """GET /api/sessions with no ?all=true only returns running sessions."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    fake.add_session("stopped-one", status="STOPPED")
    client = WahaClient(fake, "default")

    sessions = await client.get_sessions()

    assert [s["name"] for s in sessions] == ["default"]
    assert fake.calls[-1] == ("GET", "/api/sessions", None)


async def test_get_sessions_all_true_returns_everything() -> None:
    """GET /api/sessions?all=true returns stopped sessions too."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    fake.add_session("stopped-one", status="STOPPED")
    client = WahaClient(fake, "default")

    sessions = await client.get_sessions(all_=True)

    assert {s["name"] for s in sessions} == {"default", "stopped-one"}


async def test_get_session_missing_returns_none() -> None:
    """GET /api/sessions/{name} on a missing session returns None, not a raised 404."""
    fake = FakeWaha()
    client = WahaClient(fake, "default")

    result = await client.get_session()

    assert result is None


async def test_401_raises_waha_auth_error() -> None:
    """A missing/wrong API key (401) raises WahaAuthError, never conflated with 403."""
    fake = FakeWaha(auth_mode="unauthorized")
    client = WahaClient(fake, "default")

    with pytest.raises(WahaAuthError):
        await client.get_sessions()


async def test_403_raises_waha_permission_error() -> None:
    """A scoped-key denial (403) raises WahaPermissionError -- not a reauth case."""
    fake = FakeWaha(auth_mode="forbidden")
    client = WahaClient(fake, "default")

    with pytest.raises(WahaPermissionError):
        await client.get_sessions()


async def test_create_session_default_starts_it() -> None:
    """POST /api/sessions with start=true creates a session that leaves STOPPED."""
    fake = FakeWaha()
    client = WahaClient(fake, "default")

    body = await client.create_session("default")

    assert fake.sessions["default"]["status"] != "STOPPED"
    assert body["name"] == "default"


async def test_get_qr_returns_raw_png_bytes() -> None:
    """GET /auth/qr returns the raw PNG bytes, not JSON (NOWEB, not /api/screenshot)."""
    fake = FakeWaha()
    fake.add_session("default", status="SCAN_QR_CODE")
    client = WahaClient(fake, "default")

    qr = await client.get_qr()

    assert qr == fake.qr_bytes
    method, path, _ = fake.calls[-1]
    assert (method, path) == ("GET", "/api/default/auth/qr")


async def test_request_pairing_code_returns_code() -> None:
    """POST /auth/request-code returns the pairing code string."""
    fake = FakeWaha()
    fake.add_session("default", status="SCAN_QR_CODE")
    client = WahaClient(fake, "default")

    code = await client.request_pairing_code("+27821234567")

    assert code == fake.pairing_code


# --- groups shape normalization (NOWEB dict vs WEBJS list; S5) ---


def test_normalize_groups_noweb_dict_shape() -> None:
    """NOWEB returns a dict keyed by group id; the old scaffold's isinstance(list) check
    silently returned [] here forever (S5). subject is the field name, not name."""
    raw = {
        "120363012345678901@g.us": {"subject": "Family Group"},
        "120363098765432109@g.us": {"subject": "Work Group"},
    }

    groups = normalize_groups(raw)

    assert {"id": "120363012345678901@g.us", "subject": "Family Group"} in groups
    assert {"id": "120363098765432109@g.us", "subject": "Work Group"} in groups


def test_normalize_groups_webjs_list_shape() -> None:
    """WEBJS returns a list with a nested {_serialized} id."""
    raw = [
        {"id": {"_serialized": "120363011111111111@g.us"}, "subject": "Family Group"},
    ]

    groups = normalize_groups(raw)

    assert groups == [{"id": "120363011111111111@g.us", "subject": "Family Group"}]


def test_normalize_groups_unknown_shape_returns_empty() -> None:
    """Anything unrecognised degrades to an empty list rather than raising."""
    assert normalize_groups(None) == []
    assert normalize_groups("not a shape") == []


async def test_client_get_groups_dict_shape() -> None:
    """FakeWaha in dict (NOWEB) mode round-trips through the client's normalizer."""
    fake = FakeWaha(groups_shape="dict")
    fake.add_session("default")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "One"}])
    client = WahaClient(fake, "default")

    groups = await client.get_groups()

    assert groups == [{"id": "1@g.us", "subject": "One"}]


async def test_client_get_groups_list_shape() -> None:
    """FakeWaha in list (WEBJS) mode round-trips through the client's normalizer."""
    fake = FakeWaha(groups_shape="list")
    fake.add_session("default")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "One"}])
    client = WahaClient(fake, "default")

    groups = await client.get_groups()

    assert groups == [{"id": "1@g.us", "subject": "One"}]


# --- WI-5/WI-6 send/typing/timelock/capping endpoints and error taxonomy ---


async def test_send_text_returns_message_id() -> None:
    fake = FakeWaha()
    client = WahaClient(fake, "default")

    result = await client.send_text("1@c.us", "hello")

    assert result == {"id": "fake-msg-1"}
    assert fake.calls[-1][:2] == ("POST", "/api/sendText")


async def test_typing_endpoints_call_correct_paths() -> None:
    fake = FakeWaha()
    client = WahaClient(fake, "default")

    await client.send_seen("1@c.us")
    await client.start_typing("1@c.us")
    await client.stop_typing("1@c.us")

    paths = [c[1] for c in fake.calls]
    assert paths == ["/api/sendSeen", "/api/startTyping", "/api/stopTyping"]


async def test_send_image_with_url_passes_it_through() -> None:
    fake = FakeWaha()
    client = WahaClient(fake, "default")

    await client.send_image("1@c.us", url="http://example.com/pic.jpg")

    _method, _path, body = fake.calls[-1]
    assert body["file"]["url"] == "http://example.com/pic.jpg"
    assert "data" not in body["file"]


async def test_send_reaction_uses_put() -> None:
    fake = FakeWaha()
    client = WahaClient(fake, "default")

    await client.send_reaction("true_1_ABC", "👍")

    method, path, body = fake.calls[-1]
    assert (method, path) == ("PUT", "/api/reaction")
    assert body["reaction"] == "👍"


async def test_get_timelock_and_capping() -> None:
    fake = FakeWaha()
    fake.timelock = {"reachoutTimelock": 0}
    fake.capping = {"messageCapping": "low"}
    client = WahaClient(fake, "default")

    assert await client.get_timelock() == {"reachoutTimelock": 0}
    assert await client.get_capping() == {"messageCapping": "low"}


async def test_timeout_raises_ambiguous_connection_error() -> None:
    """A timeout is ambiguous -- it may have reached WAHA -- never safely retryable."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["timeout"])
    client = WahaClient(fake, "default")

    with pytest.raises(WahaConnectionError) as exc_info:
        await client.send_text("1@c.us", "hi")
    assert exc_info.value.ambiguous is True


async def test_connect_refused_raises_unreachable_not_ambiguous() -> None:
    """A provable non-delivery raises WahaUnreachableError, ambiguous=False."""
    fake = FakeWaha()
    fake.queue_send_outcomes(["connect_refused"])
    client = WahaClient(fake, "default")

    with pytest.raises(WahaUnreachableError) as exc_info:
        await client.send_text("1@c.us", "hi")
    assert exc_info.value.ambiguous is False
    assert isinstance(exc_info.value, WahaConnectionError)  # still a WahaConnectionError subtype


async def test_463_raises_rate_limited_with_status() -> None:
    fake = FakeWaha()
    fake.queue_send_outcomes(["463"])
    client = WahaClient(fake, "default")

    with pytest.raises(WahaRateLimitedError) as exc_info:
        await client.send_text("1@c.us", "hi")
    assert exc_info.value.status == 463


async def test_475_raises_rate_limited_with_status() -> None:
    fake = FakeWaha()
    fake.queue_send_outcomes(["475"])
    client = WahaClient(fake, "default")

    with pytest.raises(WahaRateLimitedError) as exc_info:
        await client.send_text("1@c.us", "hi")
    assert exc_info.value.status == 475


async def test_5xx_raises_plain_connection_error() -> None:
    fake = FakeWaha()
    fake.queue_send_outcomes(["5xx"])
    client = WahaClient(fake, "default")

    with pytest.raises(WahaConnectionError) as exc_info:
        await client.send_text("1@c.us", "hi")
    assert not isinstance(exc_info.value, WahaRateLimitedError)
    assert not isinstance(exc_info.value, WahaUnreachableError)
