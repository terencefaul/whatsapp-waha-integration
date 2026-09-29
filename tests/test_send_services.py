"""End-to-end service tests for the WI-6 send/group services (Verification items 5-7),
via hass.services.async_call against a fully-loaded entry backed by FakeWaha."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.whatsapp_waha.const import (
    ATTR_CHAT_ID,
    ATTR_FILE_PATH,
    ATTR_GROUP_NAME,
    ATTR_MESSAGE,
    ATTR_URL,
    ATTR_WAIT,
    CONF_SESSION,
    DOMAIN,
    SERVICE_GET_GROUPS,
    SERVICE_SEND_IMAGE,
    SERVICE_SEND_MESSAGE,
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


async def _setup_loaded_entry(hass: HomeAssistant, fake: FakeWaha) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_DATA, version=2)
    entry.add_to_hass(hass)
    with patch("custom_components.whatsapp_waha.AiohttpWahaTransport", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_send_message_by_chat_id_returns_queued(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    await _setup_loaded_entry(hass, fake)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {ATTR_CHAT_ID: "1@c.us", ATTR_MESSAGE: "hi"},
        blocking=True,
        return_response=True,
    )

    assert response["queued"] is True
    assert response["lane"] == "normal"


async def test_send_message_wait_true_returns_message_id(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    await _setup_loaded_entry(hass, fake)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {ATTR_CHAT_ID: "1@c.us", ATTR_MESSAGE: "hi", ATTR_WAIT: True},
        blocking=True,
        return_response=True,
    )

    assert "message_id" in response


async def test_send_message_by_group_name_resolves_to_chat_id(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    fake.set_groups("default", [{"id": "120@g.us", "subject": "Family"}])
    await _setup_loaded_entry(hass, fake)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {ATTR_GROUP_NAME: "Family", ATTR_MESSAGE: "hi", ATTR_WAIT: True},
        blocking=True,
        return_response=True,
    )

    send_calls = [c for c in fake.calls if c[1] == "/api/sendText"]
    assert send_calls[0][2]["chatId"] == "120@g.us"


async def test_send_message_unknown_group_name_raises_and_sends_nothing(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    fake.set_groups("default", [{"id": "120@g.us", "subject": "Family"}])
    await _setup_loaded_entry(hass, fake)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {ATTR_GROUP_NAME: "Nonexistent", ATTR_MESSAGE: "hi"},
            blocking=True,
        )

    assert not any(c[1] == "/api/sendText" for c in fake.calls)


async def test_get_groups_service_returns_normalized_list(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    fake.set_groups("default", [{"id": "1@g.us", "subject": "Family"}])
    await _setup_loaded_entry(hass, fake)

    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_GROUPS, {}, blocking=True, return_response=True
    )

    assert response["groups"] == [{"id": "1@g.us", "subject": "Family"}]


async def test_send_image_with_url_never_fetched_by_ha(hass: HomeAssistant) -> None:
    """url is passed straight through to WAHA -- exactly one FakeWaha call, no HA-side fetch."""
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    await _setup_loaded_entry(hass, fake)
    calls_before = len(fake.calls)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_IMAGE,
        {ATTR_CHAT_ID: "1@c.us", ATTR_URL: "http://example.com/pic.jpg", ATTR_WAIT: True},
        blocking=True,
        return_response=True,
    )

    new_calls = fake.calls[calls_before:]
    # Exactly one send-related call chain: startTyping, stopTyping, sendImage (typing pipeline,
    # preset "off" so no delay) -- no separate fetch of the url anywhere.
    assert all(c[1] != "http://example.com/pic.jpg" for c in new_calls)
    send_calls = [c for c in new_calls if c[1] == "/api/sendImage"]
    assert len(send_calls) == 1
    assert send_calls[0][2]["file"]["url"] == "http://example.com/pic.jpg"


async def test_send_image_file_path_outside_allowlist_rejected(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    await _setup_loaded_entry(hass, fake)

    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        tmp.write(b"fake image bytes")
        tmp.flush()
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SEND_IMAGE,
                {ATTR_CHAT_ID: "1@c.us", ATTR_FILE_PATH: tmp.name},
                blocking=True,
            )

    assert not any(c[1] == "/api/sendImage" for c in fake.calls)


async def test_send_image_file_path_inside_allowlist_succeeds(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    await _setup_loaded_entry(hass, fake)

    with tempfile.TemporaryDirectory() as tmpdir:
        # macOS's tempdir is itself a symlink (/var -> /private/var); is_allowed_path
        # resolves the real path, so the allowlist entry must match that, not the raw path.
        hass.config.allowlist_external_dirs.add(os.path.realpath(tmpdir))
        file_path = Path(tmpdir) / "pic.jpg"
        file_path.write_bytes(b"fake image bytes")

        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_IMAGE,
            {ATTR_CHAT_ID: "1@c.us", ATTR_FILE_PATH: str(file_path), ATTR_WAIT: True},
            blocking=True,
            return_response=True,
        )

    assert "message_id" in response
    send_calls = [c for c in fake.calls if c[1] == "/api/sendImage"]
    assert "data" in send_calls[0][2]["file"]


async def test_neither_chat_id_nor_group_name_raises(hass: HomeAssistant) -> None:
    fake = FakeWaha()
    fake.add_session("default", status="WORKING")
    await _setup_loaded_entry(hass, fake)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_MESSAGE: "hi"}, blocking=True
        )
