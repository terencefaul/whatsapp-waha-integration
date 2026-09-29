"""Shared pytest fixtures for the whatsapp_waha test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integration loading for every test (pytest-homeassistant-custom-component)."""
    return enable_custom_integrations
