# Changelog

## [0.2.0] - 2026-09-29
### Added
- `send_queue.py`: the two-lane (normal/alert) human-paced send queue --
  typing simulation, per-chat/session hourly caps, alert coalescing
  (≥3 pending or hourly ceiling), 463/475 rate-limit cooldowns (chat-scoped
  vs session-wide), retries only for provable non-delivery (alert lane,
  max 3), a crash-recovery marker for `stopTyping`, and clean shutdown
  handling. Ships with the `off` pacing preset by default -- see the
  milestone 2 log and README's "Sending safely" before changing it.
- `api.py`: send/typing/timelock/capping methods, `WahaUnreachableError`
  (provable non-delivery, retryable) and `WahaRateLimitedError` (463/475)
  as distinct, more specific `WahaConnectionError` subtypes
- `groups.py`: WORKING-gated, TTL-cached group list with exact ->
  case-insensitive-exact -> candidate-listing name resolution (no substring
  matching, ever)
- `services.py`: `send_message`, `send_image`, `send_file`, `send_voice`,
  `send_video`, `send_sticker`, `send_poll`, `send_location`,
  `send_reaction`, `get_groups`, `refresh_groups` -- moved the existing 3
  webhook/pairing services here too, out of `__init__.py`
- `notify.py`: both halves of the notify duality -- a `NotifyEntity` (only
  callable via `notify.send_message` by entity_id) and the legacy
  `notify.whatsapp_waha` service gate-pin's `POST services/notify/<name>`
  actually needs, bootstrapped via `discovery.async_load_platform`
- `diagnostics.py`: redacted config/options, webhook registration state,
  queue depth/cooldowns/failure counts, `me`, timelock, and capping
- WI-7 remainder: an auto-restart option (default off, rate-limited 3/24h
  with 5m/30m/2h backoff, never during a cooldown or from
  `SCAN_QR_CODE`/`PASSKEY_*`) and a `passkey_required` repair
- Upgraded milestone 1's reduced `webhook_unreachable` heuristic to a real
  echo-test: a direct (queue-bypassing) self-probe correlated against its
  `message.any` echo
- A second options-flow step for the notify target/lane, pacing preset,
  cap override, and auto-restart toggle
- `whatsapp_waha_send_failed` bus event, fired for every drop/expiry/failure

### Fixed
- Dispatcher- and coordinator-registered callbacks in `__init__.py`
  (`_on_webhook_received`, `_on_message_any_received`, `_on_coordinator_update`,
  `_maybe_auto_restart`) were missing `@callback`, so Home Assistant ran them
  via an executor thread instead of the event loop -- harmless until one of
  them touched a loop-thread-only API (`issue_registry.async_delete`), which
  then crashed. Caught by `test_webhook_unreachable_echo.py`.
- `webhook_registration.py`'s hook-replacement logic compared hook objects by
  identity (`is`) across two independent `deepcopy` calls, so the "replace
  only our stale hook" path never actually matched and silently appended a
  duplicate instead of replacing in place. Caught by a milestone 1 test;
  fixed to match by URL prefix instead.

### Known limitations (see the milestone 2 log)
- Build-sequence step 5 (link a real number, verify real sends, confirm
  before enabling pacing) has not run -- no WAHA API key was available this
  session either. The `off` preset, caps, TTLs, cooldowns, retries, and
  shutdown handling are all unit-tested against `FakeWaha`, but the pacing
  preset numbers and the 463/475 body/duration assumptions are unverified
  against a real server.

## [0.1.0] - 2026-09-29
### Added
- Config flow: connect to a WAHA server, create the session if missing,
  reauth and reconfigure flows
- Automatic, HMAC-signed webhook self-registration (`ensure_webhook`) that
  never PUTs onto a `WORKING` session without explicit consent, and never
  synthesises config fields it didn't read from WAHA
- Inbound webhook handler: raw-body HMAC verification, envelope dedupe,
  `whatsapp_waha_message_received` / `whatsapp_waha_message_sent` bus events
- `binary_sensor.connected` (debounced), `sensor.status`,
  `sensor.last_webhook_received`
- QR code image entity and a pairing-code fallback (`request_pairing_code`
  service and the `needs_link` repair)
- Repairs: `needs_link`, `webhook_drift`, `hmac_mismatch`, a reduced
  `webhook_unreachable` (activity-timeout heuristic; the full echo-test
  version needs milestone 2's send capability)
- `register_webhook` / `unregister_webhook` services
- Defensive `async_migrate_entry` from a hypothetical scaffold-shape (v1)
  entry to v2

### Known limitations (see the milestone 1 log)
- The webhook spike (does GET redact secrets; does a PUT round-trip; does
  create-with-webhook work; do signed deliveries verify) has not yet been
  run against the real WAHA server — `SUPPORTS_SAFE_MERGE_PUT` is left at
  its assumed default (`True`). Do not link a real, already-paired session
  to this integration until that spike has run.
- Not yet verified end-to-end against a linked WhatsApp number.
