# Milestone 2 — Send safely & notify — log

## What's new in the app

- You can now send a WhatsApp text message, image, file, voice note, video, sticker, poll, location, or reaction from any Home Assistant automation or from Developer Tools — to a phone number or to a WhatsApp group by name.
- Every send shows a human-like typing pause before it arrives, and messages are spaced out rather than arriving in a burst — protecting your secondary number from being flagged. This pacing currently ships **turned off** (see below); the typing pause itself still runs even at "off".
- Urgent sends can use a faster "alert" lane that can interrupt the normal pacing so they still arrive promptly, and several urgent alerts to the same chat in quick succession get combined into one message instead of arriving as a burst.
- A standard `notify.whatsapp_waha` target now exists — point gate-pin's (or any automation's) notify setting at it and it will deliver a WhatsApp message with a title and body.
- A new options screen lets you pick which chat/group your notify messages go to, choose the pacing preset, set a per-chat hourly send cap, and turn on automatic session restart if you want it (off by default).
- If WhatsApp starts rate-limiting your sends, the integration backs off automatically (per-chat or for the whole session) rather than retrying blindly or trying to restart/re-link — and tells you about it via a repair.
- A new diagnostics download is available for this integration, showing connection state, queue depth, and recent failures (with all secrets and chat identifiers redacted) — useful for a bug report.
- If WAHA asks for its newer "passkey" linking method, Home Assistant now tells you clearly and points you at the WAHA dashboard to finish it there, instead of just looking stuck.

## What was built

New files in `custom_components/whatsapp_waha/`:

- `send_queue.py` — `SendQueue`: two lanes (`normal`/`alert`), the full pipeline (`sendSeen` → `startTyping` → delay → `stopTyping` → send → gap), coalescing, per-chat/session hourly caps (sliding windows), depth limits, 463/475 cooldowns, retries (alert lane only, provable non-delivery only), a crash-recovery typing marker (`homeassistant.helpers.storage.Store`), and shutdown handling (shielded in-flight send, `send_failed(shutdown)` for everything left queued). Injectable `now_fn`/`sleep_fn`/`random_fn`, matching `api.py`'s existing transport seam, so the whole thing is unit-tested against a fake clock with no real waiting.
- `groups.py` — `GroupCache` (10-minute TTL, fetches only while `WORKING`) wrapping milestone 1's `api.normalize_groups`; `resolve()` does exact → case-insensitive-exact → error-listing-every-candidate, never substring matching.
- `services.py` — all 14 services now live here (the original 3 moved from `__init__.py`, plus 11 new ones for sending and groups). `_resolve_entry` moved here too.
- `notify.py` — both halves of the notify duality: a `NotifyEntity` (`WahaNotifyEntity`, callable only via `notify.send_message` targeted by entity_id) and the legacy platform (`async_get_service` → `WahaNotificationService`, callable as `notify.whatsapp_waha`) that gate-pin's `POST services/notify/<name>` actually needs. Confirmed against HA 2026.9.4 core source (`homeassistant/components/nfandroidtv`) that a bare `NotifyEntity` is *never* exposed as its own named service — only the legacy path produces that.
- `diagnostics.py` — `async_get_config_entry_diagnostics`, redacting `api_key`/`hmac_key`/`webhook_id`/`me.id`.

Extended:
- `api.py` — `send_seen`/`start_typing`/`stop_typing`/`send_text`/`send_image`/`send_file`/`send_voice`/`send_video`/`send_sticker`/`send_poll`/`send_location`/`send_reaction`/`get_timelock`/`get_capping`; `WahaConnectionError` gained `ambiguous: bool`; new `WahaUnreachableError` (provable non-delivery, `ambiguous=False`) and `WahaRateLimitedError` (463/475, carries `status`); the transport now distinguishes `aiohttp.ClientConnectorError` (→ `WahaUnreachableError`) from other client errors.
- `const.py` — lanes, presets, caps/TTLs/cooldowns, new service/attribute/issue names.
- `coordinator.py` — `WahaRuntimeData` gained `send_queue`, `group_cache`, `last_webhook_registration` (all optional, defaulting to `None`, so milestone 1's existing tests that construct it without them still work unchanged).
- `config_flow.py` — a second options-flow step (`send_queue`): notify target/lane, pacing preset, per-chat cap override, auto-restart toggle.
- `__init__.py` — `PLATFORMS` gained `Platform.NOTIFY`; `hass.data[DATA_HASS_CONFIG]` stashed for the legacy notify platform's discovery bootstrap; the send queue's worker runs via `entry.async_create_background_task` (HA cancels it automatically on unload); auto-restart limiter and listener; `PASSKEY_*` repair wiring; the `webhook_unreachable` echo-test upgrade; service registration now delegates to `services.async_setup_services(hass)`.
- `webhook_handler.py` — one new dispatcher signal, `signal_message_any_received`, fired for every verified `message.any` + `fromMe` delivery (independent of the `fire_message_sent_events` option) — this is what the echo-test correlates its probe against.
- `repairs.py` — `passkey_required` and `send_rate_limited` (both non-fixable, informational).
- `services.yaml`, `icons.json`, `translations/en.json` — all 14 services, the new options step, and the two new repairs.

`tests/` gained `test_send_queue.py` (20 tests), `test_groups.py` (12), `test_send_services.py` (9), `test_notify.py` (7), `test_diagnostics.py` (4), `test_auto_restart.py` (9), `test_webhook_unreachable_echo.py` (4), plus extensions to `test_api_client.py`, `test_repairs.py`, `test_portability.py`, and `fake_waha.py` (send/typing/timelock/capping endpoints, a `queue_send_outcomes` scripting helper for timeout/5xx/connect-refused/463/475). **160 tests pass**, up from milestone 1's 83.

## Two real bugs found and fixed while building this milestone

- **Dispatcher/coordinator callbacks missing `@callback`.** `_on_webhook_received`, `_on_message_any_received`, `_on_coordinator_update`, and `_maybe_auto_restart` in `__init__.py` were plain nested functions with no `@callback` decorator. Home Assistant's dispatcher/coordinator machinery inspects the callable and, seeing no `@callback` marker, treats it as a possibly-blocking sync function and runs it via an executor thread rather than directly on the event loop. This was harmless right up until one of them called `issue_registry.async_delete` (a loop-thread-only API), which then raised `RuntimeError: ... calls issue_registry.async_delete from a thread other than the event loop`. Caught by `test_webhook_unreachable_echo.py`'s echo-clears-the-repair test; fixed by adding `@callback` to all four. Milestone 1's own entity callbacks (`binary_sensor.py`, `sensor.py`, `image.py`) were already correctly decorated — this was specifically a gap in the new `__init__.py` wiring.
- **`webhook_registration.py`'s hook-replace-in-place used identity comparison across independent `deepcopy` calls** (`h is ours`), so it silently never matched and would have appended a duplicate hook instead of replacing the stale one. This was actually a milestone 1 bug, caught and fixed by a milestone 1 regression test (`test_drift_replaces_only_our_stale_hook_keeps_foreign_ones`) — noted here because it's the kind of finding that stayed relevant once the send queue's own object-identity-adjacent coalescing logic was being written, and was double-checked not to repeat the same mistake (coalescing matches on `chat_id`/`kind`, not object identity, deliberately).

## Decisions made during implementation that weren't pre-specified

- **Notify naming for multiple entries**: the entry whose session is literally named `"default"` claims the bare `notify.whatsapp_waha`; any other entry gets `notify.whatsapp_waha_<session_slug>`. Deterministic, no "first loaded wins" fragility.
- **`off` preset still runs the typing pipeline** (`sendSeen`/`startTyping`/`stopTyping`) — only the delay and inter-message gap collapse to zero. This means the crash-recovery typing-marker logic gets real production exercise immediately, rather than staying dead code until someone turns pacing on.
- **`CONF_INTENDED_STATE` is defined and checked now**, even though no stop/logout service exists yet to ever set it to `"stopped"` — so in practice this milestone, auto-restart treats *any* `STOPPED` the same as one it didn't cause (the design doc's Risk #11, mitigated only by the option defaulting off). The flag is there so a future stop/logout service doesn't require touching this code again.
- **`reply_to` prefix validation (`true_`/`false_`) lives in `services.py`**, not `api.py` — keeps `api.py` a pure transport client that only ever raises `WahaError` subtypes, never `ServiceValidationError`.
- **Coalescing merges *all* currently-pending alerts for a chat into one**, not just the newest into the last — the first implementation only merged pairwise (newest into the previous tail), which left multiple partially-merged items in the lane instead of the one message the design calls for. Caught by `test_three_pending_alerts_for_one_chat_coalesce` before it shipped.
- **`hassfest`/Python-3.14 note carried over from milestone 1** applies unchanged; no new CI was added this milestone either (not in scope).
- Numeric defaults not specified in the design doc: per-session hourly cap defaults to 200 (arbitrary, generous, inert while pacing is off), and the `balanced` preset's numbers are a provisional midpoint between the doc's `strict` numbers and "no pacing" — both are provisional and clearly commented as such in `send_queue.py`.

## What milestone 3 (or whoever picks this up) will need to know

- **Build-sequence step 5 did not run.** No WAHA API key was available this session (same as milestone 1's skipped spike). Everything through step 4 is built and fully unit-tested against `FakeWaha`; step 5 (link a number, verify real sends including a visible typing pause and message arrival, verify `stopTyping` survives a forced restart mid-delay, verify gate-pin's `notify.whatsapp_waha` round-trip for real, and *only then* confirm with the user before flipping `DEFAULT_SEND_QUEUE_PRESET` off `"off"`) is still pending.
- **463/475 status-code handling is unverified against a real server.** The queue keys its cooldown behavior only off the HTTP status code (463 = chat, 475 = session), never the response body (whose shape is unknown) — this should hold regardless of what real bodies turn out to contain, but the actual *durations* WhatsApp expects, and whether 463/475 are really the correct codes, remain unconfirmed.
- **The pacing preset numbers (`balanced`, and `strict`'s exact figures from the design doc) are unverified.** Don't trust them as tuned; they need real-world observation once pacing is turned on.
- **The `/auth/qr`, timelock, and capping paths flagged as unverified in milestone 1 remain unverified** — `get_timelock`/`get_capping` in `api.py` are best-effort guesses at the real endpoint shapes.
- **`WahaClient.send_text`'s real response body shape (and therefore the exact `message_id` extraction) is unverified.** `send_queue._succeed` extracts `result.get("id")` with a fallback to `result.get("_data", {}).get("id")`, guessing at the two most likely shapes based on the design doc's mention of WAHA's message-id conventions (`true_<chat>_<HEX>` style ids) — confirm against a real send and correct if wrong.
- **The connectivity echo-test's self-probe text and target** (`client.send_text(me_id, "whatsapp_waha connectivity check")`) hasn't been tried against a real session — confirm `me.id` is actually a valid self-chat target for a `sendText` call on NOWEB.

## Deviations from the PRD or the technical plan, and why

- **Live verification (build-sequence step 5) skipped for this milestone too** — a scope/verification deviation, not a design one, made because no API key was available, exactly mirroring milestone 1's situation.
- **Pacing was *not* turned on.** Per the milestone's own explicit instruction, flipping the default away from `"off"` requires the user's confirmation *after* real sends have been verified — neither happened this session.
- No deviations from the PRD's milestone 2 scope: HACS publishing and message editing/un-sending were correctly left out. WI-7's auto-restart and `PASSKEY_*` handling, previously deferred by milestone 1, are now built as this milestone's own prompt specified.
