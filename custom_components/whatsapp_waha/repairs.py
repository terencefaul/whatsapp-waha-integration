"""Repairs for whatsapp_waha.

- needs_link (fixable): shown while the session is SCAN_QR_CODE; the flow
  offers the pairing-code fallback (phone number -> code shown as text --
  images can't be embedded in a repair flow reliably).
- webhook_drift (fixable): raised when ensure_webhook found drift it either
  needs consent for (a WORKING session) or could not resolve. The confirm
  step re-runs ensure_webhook with force_working_put=True.
- hmac_mismatch (fixable): >=20 signature failures -- regenerates the HMAC
  key and force-PUTs the new one.
- webhook_unreachable (not fixable): WORKING for a while with zero webhook
  deliveries -- informational only in milestone 1 (see the reduced-heuristic
  note in the milestone log); points at reconfigure to check the callback URL.
"""

from __future__ import annotations

import logging
import secrets
import time

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import webhook_registration as wr
from .const import (
    ATTR_PHONE_NUMBER,
    CONF_HMAC_KEY,
    CONF_LAST_PUT_AT,
    CONF_SESSION,
    DOMAIN,
    ISSUE_HMAC_MISMATCH,
    ISSUE_NEEDS_LINK,
    ISSUE_PASSKEY_REQUIRED,
    ISSUE_SEND_RATE_LIMITED,
    ISSUE_WEBHOOK_DRIFT,
    ISSUE_WEBHOOK_UNREACHABLE,
)
from .util import webhook_callback_url

_LOGGER = logging.getLogger(__name__)


# --- create/clear helpers, called from __init__.py / coordinator listeners ---


def async_create_needs_link_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_NEEDS_LINK}_{entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="needs_link",
        data={"entry_id": entry_id},
    )


def async_clear_needs_link_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_NEEDS_LINK}_{entry_id}")


def async_create_webhook_drift_issue(hass: HomeAssistant, entry_id: str, *, fixable: bool) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_WEBHOOK_DRIFT}_{entry_id}",
        is_fixable=fixable,
        severity=ir.IssueSeverity.WARNING,
        translation_key="webhook_drift" if fixable else "webhook_drift_unfixable",
        data={"entry_id": entry_id},
    )


def async_clear_webhook_drift_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_WEBHOOK_DRIFT}_{entry_id}")


def async_create_hmac_mismatch_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_HMAC_MISMATCH}_{entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="hmac_mismatch",
        data={"entry_id": entry_id},
    )


def async_clear_hmac_mismatch_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_HMAC_MISMATCH}_{entry_id}")


def async_create_webhook_unreachable_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_WEBHOOK_UNREACHABLE}_{entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="webhook_unreachable",
        data={"entry_id": entry_id},
    )


def async_clear_webhook_unreachable_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_WEBHOOK_UNREACHABLE}_{entry_id}")


def async_create_passkey_required_issue(hass: HomeAssistant, entry_id: str, dashboard_url: str) -> None:
    """WAHA is in PASSKEY_REQUIRED/PASSKEY_CONFIRMATION_REQUIRED -- not fixable here
    (out of scope, see the PRD), points at the WAHA dashboard instead."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_PASSKEY_REQUIRED}_{entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="passkey_required",
        translation_placeholders={"dashboard_url": dashboard_url},
        data={"entry_id": entry_id},
    )


def async_clear_passkey_required_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PASSKEY_REQUIRED}_{entry_id}")


def async_create_send_rate_limited_issue(hass: HomeAssistant, entry_id: str) -> None:
    """A 463/475 cooldown is active -- informational; no restart, no re-pair."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_SEND_RATE_LIMITED}_{entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="send_rate_limited",
        data={"entry_id": entry_id},
    )


def async_clear_send_rate_limited_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_SEND_RATE_LIMITED}_{entry_id}")


# --- fix flows ---


class NeedsLinkRepairFlow(RepairsFlow):
    """Pairing-code fallback for linking, from the repairs UI."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> RepairsFlowResult:
        return await self.async_step_phone_number()

    async def async_step_phone_number(self, user_input: dict | None = None) -> RepairsFlowResult:
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            client = entry.runtime_data.client
            code = await client.request_pairing_code(user_input[ATTR_PHONE_NUMBER])
            return await self.async_step_show_code(code=code)

        return self.async_show_form(
            step_id="phone_number",
            data_schema=vol.Schema({vol.Required(ATTR_PHONE_NUMBER): str}),
        )

    async def async_step_show_code(
        self, user_input: dict | None = None, code: str | None = None
    ) -> RepairsFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="show_code",
            data_schema=vol.Schema({}),
            description_placeholders={"code": code or ""},
        )


class WebhookDriftRepairFlow(RepairsFlow):
    """Confirm + force a webhook PUT onto a WORKING session (or acknowledge an unfixable one)."""

    def __init__(self, entry_id: str, *, fixable: bool) -> None:
        self.entry_id = entry_id
        self.fixable = fixable

    async def async_step_init(self, user_input: dict | None = None) -> RepairsFlowResult:
        if not self.fixable:
            return self.async_show_form(step_id="unfixable_info", data_schema=vol.Schema({}))
        return await self.async_step_confirm()

    async def async_step_unfixable_info(self, user_input: dict | None = None) -> RepairsFlowResult:
        return self.async_abort(reason="webhook_drift_unfixable")

    async def async_step_confirm(self, user_input: dict | None = None) -> RepairsFlowResult:
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            runtime = entry.runtime_data
            result = await wr.ensure_webhook(
                runtime.client,
                entry.data[CONF_SESSION],
                webhook_callback_url(self.hass, entry),
                entry.data[CONF_HMAC_KEY],
                last_put_at=entry.data.get(CONF_LAST_PUT_AT),
                force_working_put=True,
            )
            if result.outcome == wr.RegistrationOutcome.PUT_APPLIED:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_LAST_PUT_AT: time.time()}
                )
                async_clear_webhook_drift_issue(self.hass, self.entry_id)
                return self.async_create_entry(data={})
            return self.async_abort(reason="webhook_drift_retry_failed")

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
        )


class HmacMismatchRepairFlow(RepairsFlow):
    """Regenerate the HMAC key and force-PUT it."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> RepairsFlowResult:
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            runtime = entry.runtime_data
            new_key = secrets.token_urlsafe(48)
            result = await wr.ensure_webhook(
                runtime.client,
                entry.data[CONF_SESSION],
                webhook_callback_url(self.hass, entry),
                new_key,
                last_put_at=entry.data.get(CONF_LAST_PUT_AT),
                force_working_put=True,
            )
            if result.outcome == wr.RegistrationOutcome.PUT_APPLIED:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_HMAC_KEY: new_key, CONF_LAST_PUT_AT: time.time()},
                )
                async_clear_hmac_mismatch_issue(self.hass, self.entry_id)
                return self.async_create_entry(data={})
            return self.async_abort(reason="hmac_rotation_failed")

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Route an issue_id to its RepairsFlow. issue_id is f'{kind}_{entry_id}'."""
    assert data is not None
    entry_id = data["entry_id"]
    if issue_id.startswith(ISSUE_NEEDS_LINK):
        return NeedsLinkRepairFlow(entry_id)
    if issue_id.startswith(ISSUE_HMAC_MISMATCH):
        return HmacMismatchRepairFlow(entry_id)
    if issue_id.startswith(ISSUE_WEBHOOK_DRIFT):
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        return WebhookDriftRepairFlow(entry_id, fixable=bool(issue and issue.is_fixable))
    raise ValueError(f"Unknown issue_id {issue_id}")
