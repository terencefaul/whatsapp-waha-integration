# whatsapp_waha — a Home Assistant integration for a self-hosted WAHA gateway

**Status:** planned, not built. Written 2026-09-28 against `main` @ `59fa610`.
**Scaffold reviewed:** `waha-ha-integration-package/whatsapp_waha/custom_components/whatsapp_waha/` (untracked, 606 lines, v0.1.0).
**Sources:** three read-only audits (repo conventions; HA core 2026.9.x APIs; WAHA 2026.9.1 source and docs) and one adversarial design pass. `file:line` refs into the scaffold are written `cc/<file>:<line>`.

---

## Context

The scaffold is a Home Assistant **custom integration** (config flow, services, webhook, binary sensor) that talks to a WAHA (WhatsApp HTTP API) server. It was written from `waha-ha-integration-package/waha-proxmox-brief.md` §4. The request called it an "addon"; it is not one, and the repo this lives in is an add-on repository (`AGENTS.md:8-10`: "Each add-on is a **top-level folder containing a `config.yaml`**").

**Premise corrected.** "Addon" is loose wording. The deliverable is an *integration*. WAHA itself runs on the user's own Debian LXC on Proxmox (already built, running, **not yet linked** to a WhatsApp number). No Supervisor add-on is being built. The brief's decision stands: *"Hosting: dedicated LXC on Proxmox (NOT inside HA) — HA reboots never touch the WhatsApp session"* (brief §1).

**Stale claim in the brief, corrected.** Brief §1 says *"no existing HACS integration talks to WAHA directly"*. `sebastian-greco/home-assistant-whatsapp` (HACS, Apache-2.0, created 2026-07-19) now does. See decision D1.

**Verdict on the scaffold: it cannot load on any current HA**, and several of its assumptions about WAHA are wrong. The review findings are in [Scaffold review](#scaffold-review) and each one maps to a work item below.

**Deliberately out of scope**
- A Supervisor add-on that runs WAHA (would reverse the brief's LXC decision; user chose integration only).
- HACS publication (own repo, `hacs/action`, GitHub release). Layout is kept convertible; publishing is later. User: *"put it alongside and worry about hacs later"*.
- Message-history catch-up after an HA outage (WAHA has no replay; see accepted risks).
- Passkey pairing (`PASSKEY_*` statuses need a browser on `web.whatsapp.com`; we fall back to the WAHA dashboard).
- Changes to gate-pin. The notify service is designed so gate-pin's existing `notify_service` option works unchanged.

### Decisions locked during planning

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Build vs adopt? | **Build our own, borrow ideas** from `sebastian-greco/home-assistant-whatsapp`. | Ours targets NOWEB, keeps free-form `chat_id`/`group_name` services, adds the send queue, QR entity and gate-pin notify. Theirs targets GOWS and is notify-entity-only. It is 2 months old with 0 stars, so its code is reference, not a dependency. Rejected: adopt as-is (unverified on NOWEB), fork (inherits GOWS assumptions). |
| D2 | Deliverable | **Custom integration only.** | User: *"we just want the custom addon, integration/app. I have setup the waha lxc manually"*. |
| D3 | Appetite | **Everything on the README TODO list**, HACS packaging last. | User choice. QR linking, extra send types, `get_groups`, migration + reauth, auto-restart toggle, diagnostics, packaging. |
| D4 | WAHA server state | Running, **not linked**. | Plan must split "verifiable against an unpaired WAHA" from "needs a linked secondary number" (see Verification). |
| D5 | Where the code lives | **This repo, top-level folder beside `gate-pin/`.** HACS later. | User: *"This repo already has a gate-pin, lets put it along side"*. |
| D6 | Webhook wiring | **HA registers the webhook in WAHA itself, HMAC-signed**, and verifies every POST. | Removes the "paste URL, restart WAHA" step (`README.md:36-38`) and makes the endpoint authenticated. Rejected: manual `WHATSAPP_HOOK_URL` (LAN devices can forge messages); opt-in checkbox (two code paths). **Amended after pressure test:** never PUT onto a `WORKING` session without consent (see The part that quietly breaks). |
| D7 | Anti-ban | **Per-session send queue** with seen → typing → randomised delay → stop-typing → send, and per-chat hourly caps. **Amended after pressure test into two lanes**: a `normal` lane with the full human pacing, and an `alert` lane with short delays, no per-chat cap, TTL and coalescing. | User chose the queue for every send. The design pass showed a 30-60 s gap per message would time out gate-pin's REST `notify` call (`gate-pin/rootfs/app/gate_pin/ha.py:103-116`) and deliver lockout alerts minutes late. The alert lane keeps the queue and typing simulation and lowers the ban shield only marginally at single-digit alerts/hour. **This modifies the user's answer; flagged in the summary for confirmation.** |
| D8 | Notify | **Yes.** `notify.whatsapp_waha` (legacy service, title + message) to a default chat/group chosen in setup; also a `NotifyEntity`. | gate-pin calls `POST services/notify/<name>` (`ha.py:103-116`); entity-only notify would not satisfy it. |
| A1 | HA target | **HA ≥ 2026.9** | Assumed. `hass.components` gone since 2025.5.0; `probatio` alias for `voluptuous` in 2026.9. |
| A2 | `integration_type` / `iot_class` | **`service` / `local_push`** | Assumed. One service, no devices (`twilio`, `telegram_bot`); push is the headline (`tedee` is `local_push`). Scaffold has `hub`/`local_polling` (`cc/manifest.json:7-8`). |
| A3 | Sessions per entry | **One session per config entry; multiple entries allowed.** | Assumed. WAHA dropped the Core one-session limit in 2026.6.1, so `README.md:138` ("matches WAHA Core") is stale, but one-per-entry remains simplest. |
| A4 | Session creation | **Config flow creates the session if missing** (`POST /api/sessions`, `start:true`, webhook included). | Assumed. WAHA never auto-creates `default`; the scaffold's `GET /api/sessions/default` 404s on a fresh server (`cc/config_flow.py:50`). |
| A5 | Config-entry `unique_id` | **Random uuid**, duplicates checked on `(normalised base URL, session)`. | Assumed. The scaffold's `host:port:session` (`cc/config_flow.py:73-75`) breaks when the LXC's DHCP address changes. Reconfigure flow edits host/port/key. |
| A6 | `local_only` on the webhook | **`False`**; HMAC is the authentication. `allowed_methods=["POST"]`. | Assumed. Core returns **200** on a `local_only` failure (`homeassistant/components/webhook/__init__.py:140-216`), so WAHA believes delivery succeeded and the failure is invisible; behind a reverse proxy it would fail constantly. |
| A7 | Events | Bus events, **no `event` entity**. | Assumed. Follows `ifttt`; an entity would put message bodies in the recorder. |
| A8 | Service targeting | Optional `config_entry_id`, defaulting to the sole entry. | Assumed. Follows `telegram_bot`. |

---

## The one architectural problem to solve first

**Everything inbound and every session-state change depends on WAHA reaching HA, and WAHA's webhook config can only be changed by a call that restarts the session.**

Reasons, in descending strength:
1. `PUT /api/sessions/{name}` is a **full replace** of `config` and stops/restarts a running session (`SessionService.updateSession` → `upsert` → `saveConfig`; WAHA `src/core/services/SessionService.ts:76-103`, `manager.core.ts:312-317`).
2. `config` also holds `noweb.store`, `proxy` and `metadata`. WAHA's docs warn: *"Do not change the values after you scanned QR, it can lead to the loss of the chat history"* (`engines/noweb/index.md:66`).
3. Whether GET returns secrets (`proxy` credentials, `hmac.key`) unredacted is **unverified**. If GET masks them, a merge-PUT would overwrite real secrets with masks.
4. WAHA retries any non-2xx up to 15 times with no idempotency key (`WebhookPlugin.sender.ts:133`), and has no replay: events during an HA outage are lost.

So step 2 of the build sequence is a **spike against an unpaired throwaway session** on the LXC, *before* any other feature is built on registration. If GET redacts secrets, the design collapses to "create with the webhook; never PUT".

```
 WAHA (LXC 192.168.0.60)                       Home Assistant
 ┌──────────────────────┐   signed POST        ┌─────────────────────────────────┐
 │ session "default"    │ ───────────────────► │ /api/webhook/whatsapp_waha_<id> │
 │ config.webhooks[ours]│  X-Webhook-Hmac      │  verify raw-body sha512 HMAC    │
 └──────────▲───────────┘  sha512, retries     │  dedupe (envelope id, 30 min)   │
            │                                   │  → bus events / status push     │
            │ REST (X-Api-Key)                  └───────────────┬─────────────────┘
            │                                                   │
            │   sendSeen/typing/sendText…   ┌───────────────────▼─────────────────┐
            └───────────────────────────────│ SendQueue (per entry)               │
                                            │  alert lane │ normal lane           │
                                            └───────────────▲─────────────────────┘
                                   services / notify.whatsapp_waha / gate-pin REST
```

---

## Scaffold review

Every row is a defect in the scaffold as it stands. Sources are HA core `dev` at 2026.9.x, and WAHA source at 2026.9.1.

| # | Defect | Evidence | Fix in |
|---|---|---|---|
| S1 | **Cannot load.** Uses `hass.components.webhook`, removed in HA **2025.5.0**. | `cc/__init__.py:100`, `:203` | WI-3 |
| S2 | Manifest lacks `"dependencies": ["webhook"]`; the `/api/webhook/{id}` route exists only if `webhook` is set up. | `cc/manifest.json:1-12` | WI-3 |
| S3 | **Wrong auth status.** WAHA returns **401** for a missing/wrong key; 403 only for scoped-key denial. A wrong key shows `cannot_connect`, and reauth can never trigger. | `cc/api.py:55-56`, `cc/config_flow.py:49`; WAHA `api-key-auth.middleware.ts:19-29` | WI-1 |
| S4 | Config flow assumes session `default` exists; WAHA never auto-creates it, so `GET /api/sessions/default` 404s. | `cc/config_flow.py:50`; WAHA `SessionService.ts:36-42` | WI-3 |
| S5 | **`GET /api/{session}/groups` is a dict on NOWEB**, so `isinstance(result, list)` returns `[]` every time; the field is `subject`, not `name`. Group-name resolution can never succeed and fails silently. | `cc/api.py:104`, `cc/__init__.py:131`; WAHA `session.noweb.core.ts:2155-2159`, `groups.dto.ts:244-260` | WI-6 |
| S6 | **`/api/screenshot` is WEBJS/WPP-only** and returns JPEG. NOWEB QR is `GET /api/{session}/auth/qr`. | `cc/api.py:91-97`; WAHA `screenshot.controller.ts:34-37`, `features.md:15` | WI-7 |
| S7 | No HMAC verification; any POST is accepted. Webhook id is guessable (`f"{DOMAIN}_{entry_id}"`). | `cc/__init__.py:82-96`, `:98` | WI-2, WI-4 |
| S8 | Returns 400 on bad JSON, which triggers up to 15 WAHA retries. | `cc/__init__.py:87` | WI-4 |
| S9 | Webhook URL built from `external_url`, which can be empty. | `cc/__init__.py:106`, `README.md:38-39` | WI-2 |
| S10 | Services registered per entry and removed globally on any unload: breaks a second entry. `refresh_groups` handler is a bare `lambda` (runs in the executor, calls `hass.async_create_task` off-loop, returns a Task as a "response"). | `cc/__init__.py:160-205`, `:190` | WI-3 |
| S11 | Blocking `Path.read_bytes()` on the event loop; **no `allowlist_external_dirs` check**, so a service caller can send `/config/secrets.yaml` to WhatsApp. | `cc/api.py:135`, `cc/__init__.py:154` | WI-6 |
| S12 | `mimetype` hardcoded `image/jpeg`; 15 s timeout too short for base64 uploads. | `cc/api.py:124`, `:141`, `:53` | WI-6 |
| S13 | Uses `hass.data`, an un-subclassed coordinator without `config_entry=`, wrong exception types (a down WAHA logs a traceback every 30 s), no reauth, no reconfigure, no options flow, no migration. Webhook registered *after* the first refresh, so a `WahaAuthError` leaves it registered and the next setup raises `ValueError("Handler is already defined!")`. | `cc/__init__.py:65-79`, `:116-119` | WI-3 |
| S14 | Custom integrations read only `translations/en.json`; `strings.json` is ignored. No `services`, `exceptions`, `entity` blocks; `services.yaml` has no names. | `cc/strings.json`, `cc/translations/en.json` | WI-3 |
| S15 | Anti-ban guide entirely unimplemented. Its URL in the README/brief is a **404**; the live page is `/docs/overview/how-to-avoid-blocking/`. | `README.md:14-15` | WI-5 |
| S16 | Reads only `message`; drops `session`, `me`, `id`; no `@lid` handling; `reply_to` accepts any string though WAHA needs the full message id (`false_<chat>_<HEX>`). | `cc/__init__.py:94`, `:169` | WI-4, WI-5 |
| S17 | Unused code: `CannotConnect`/`InvalidAuth` classes; local `CONF_*` duplicates of `homeassistant.const`; no `ATTR_FILE_PATH`. | `cc/config_flow.py:84-89`, `cc/const.py:5-10` | WI-3 |
| S18 | `README.md:138` says one session "matches WAHA Core". Since WAHA 2026.6.1 there is no Core/Plus split and no session limit. | WAHA `waha-plus/index.md:15-25` | WI-9 |

---

## The design

### Repo layout (D5)

Precedent: `gate-pin/` (README, DOCS, CHANGELOG, `tests/` inside the folder; `AGENTS.md:31-33` "Per-add-on tooling stays inside the add-on").

```
whatsapp-waha-integration/            <- renamed from waha-ha-integration-package/whatsapp_waha
├── custom_components/whatsapp_waha/  (integration; nothing references the parent repo)
├── tests/
├── hacs.json                          (last; content minimal)
├── pyproject.toml, requirements_test.txt
└── README.md, CHANGELOG.md
docs/plans/whatsapp-waha-integration.md   <- this file
waha-proxmox-brief.md                  -> moved to whatsapp-waha-integration/docs/ (not kept at repo root)
```

- **No `config.yaml`** anywhere under the folder. `ci.yml:16` (`find . -maxdepth 2 -name config.yaml`) and `scripts/release.sh:18` therefore skip it, so the add-on jobs are untouched.
- A **separate workflow** `.github/workflows/integration.yml`, path-filtered: Python **3.14** (HA needs ≥ 3.14.2; `ci.yml:51` pins 3.12, which cannot install current HA), `pytest-homeassistant-custom-component`, `hassfest`. `hacs/action` joins only when HACS packaging is done.
- **`scripts/release-integration.sh`**: bumps `manifest.json` `version`, prepends `CHANGELOG.md`, tags `whatsapp_waha-vX.Y.Z`. Do not overload `release.sh` (requires `config.yaml`, seds its `version:`, and pushes straight to `main`, `release.sh:47-75`). No tags exist in the repo today, so this establishes the first tag convention.
- **Convertible to a HACS repo** with `git subtree split -P whatsapp-waha-integration`.
- `AGENTS.md` gets a short paragraph: a folder without `config.yaml` is an integration, not an add-on, and has its own CI/release path.

### Component precedents

| Piece | Precedent to follow | Why |
|---|---|---|
| Webhook + coordinator + runtime data | core `tedee` (`__init__.py:13-17,53-94,106-130`, `coordinator.py:34-62`) | typed `ConfigEntry[Coordinator]`, `webhook_generate_id`, `async_generate_url(..., allow_external=False, allow_ip=True)`, background-task registration; quality `platinum`, `local_push` |
| Bus-event handler | core `ifttt/__init__.py:103-142` | `isinstance(data, dict)` guard; bus event pattern |
| Services in `async_setup` with `config_entry_id` | core `telegram_bot` (`__init__.py:70`, `services.yaml:64-67`) | `action-setup` quality rule |
| QR image entity | core `fritz/image.py:72-131` | bumps `image_last_updated` only when bytes change, outside `async_image` |
| WAHA client, HMAC, session-PUT merge | `sebastian-greco/home-assistant-whatsapp` `api.py:356-411`, `__init__.py:852-870` | closest existing WAHA client (reference only; it targets GOWS) |
| Tests | `gate-pin/tests/test_bot.py:1-38` (`FakeTelegram` records calls) and `test_portability.py` (regex architecture guard) | repo's own harness idiom |

### WI-1 — API client (`api.py`)

- Auth: **401 and 403 handled separately**: 401 → `WahaAuthError`; 403 → `WahaPermissionError` (not a reauth case).
- Injectable transport seam so tests use a `FakeWaha`.
- `GET /api/sessions?all=true` (running-only otherwise); `GET/POST/PUT /api/sessions/{name}`; `/start`; `/auth/qr` (`Accept: image/png`), `/auth/request-code`; `GET /api/{session}/groups` normalised (dict *or* list; `subject`/`name`/`groupMetadata.subject`; `id` string or `{_serialized}`); `POST /groups/refresh`; `sendSeen`, `startTyping`, `stopTyping`, `sendText`, `sendImage`/`sendFile`/`sendVoice`/`sendVideo`/`sendSticker`/`sendPoll`/`sendLocation`, `PUT /api/reaction`; `GET /api/sessions/{s}/timelock` and `/capping`.
- Per-call timeouts (uploads longer than 15 s). Every network error → `WahaConnectionError`.
- `mimetype` derived from extension with an override. `linkPreview` defaults to `false`.
- `reply_to` must begin `true_` or `false_`; otherwise a clear `ServiceValidationError` (group ids have four parts).

### WI-2 — Webhook registration (`webhook_registration.py`)

Storage in `entry.data`: `webhook_id = "whatsapp_waha_" + token_hex(16)`, `hmac_key = token_urlsafe(48)`, generated once in the flow.

Callback URL: `webhook.async_generate_url(hass, id, allow_external=False, allow_ip=True)` with an override option `callback_base_url`. On `NoURLAvailableError` the flow shows a required field, and displays the URL it will register (`hass.config.api.local_ip` can be container-internal). Recomputed each check, never cached.

Desired hook (all three events subscribed permanently; filtering happens in HA, because changing the event set needs a PUT):

```json
{"url": "<callback>", "events": ["message","message.any","session.status"],
 "hmac": {"key": "<hmac_key>"},
 "retries": {"policy": "constant", "delaySeconds": 2, "attempts": 8}}
```
(Retry policy enum names `linear|exponential|constant` per WAHA `webhooks.config.dto.ts`; final values confirmed in the spike.)

`ensure_webhook(entry)`, run only after the first successful refresh:
1. `GET /api/sessions/{name}`. On 404: `POST /api/sessions {name, config:{webhooks:[desired]}, start:true}`. Done, no PUT, no restart.
2. Find "ours": any hook whose URL path starts `/api/webhook/whatsapp_waha_` (also adopts a stale hook from a deleted entry).
3. Compare only fields we own (url, hmac.key, sorted events, retries). Equal → do nothing.
4. Drift: deep-copy the exact `config` from the GET, replace **only** our hook, keep every other hook in order, synthesise nothing (`noweb.store`, `proxy`, `metadata` untouched).
5. PUT, re-GET, re-compare. Still different → stop, raise repair `webhook_drift`, never loop.
6. At most one PUT per setup, and none within 10 minutes of the last (`last_put_at` persisted).

By session state: `STOPPED/FAILED/SCAN_QR_CODE/STARTING` → PUT automatically (nothing to lose). **`WORKING` → never automatically**: raise repair `webhook_drift` with a `RepairsFlow` ("this restarts the session for a few seconds"), plus a `register_webhook` service. Unload/remove leave the hook (removal is itself a PUT; a dead hook gets a harmless 200 from core for an unknown id). Manual `unregister_webhook` service.

The user's existing global `WHATSAPP_HOOK_URL` (brief §2.3, `/api/webhook/waha_incoming`) is **harmless**: core returns 200 for an unknown webhook id (`webhook/__init__.py:140-216`), so nothing double-delivers or retries. Tell the user to delete it eventually; our id differs.

### WI-3 — Integration skeleton (rewrite of `__init__.py`, `config_flow.py`, `const.py`, manifest)

- Manifest: `integration_type: service`, `iot_class: local_push`, `"dependencies": ["webhook"]`, `"after_dependencies": ["notify"]`, `documentation` and `issue_tracker` pointing at this repo.
- `entry.runtime_data` (typed `ConfigEntry[WahaRuntime]`); subclassed `DataUpdateCoordinator` with `config_entry=`; `UpdateFailed`/`ConfigEntryAuthFailed`/`ConfigEntryNotReady`; `entry.async_on_unload` for everything (webhook unregister, timers, queue).
- Services registered in **`async_setup`**, never removed per entry; `async` handlers; `HomeAssistantError` for network faults, `ServiceValidationError` for bad input, both with `translation_key`.
- Config flow: `user` (host, port, SSL, key, session) validated by `GET /api/sessions?all=true` (401 → `invalid_auth`, 403 → `insufficient_permissions`, network → `cannot_connect`); create-session-if-missing step; `reauth`, `reconfigure`, options flow; `VERSION = 1` with an `async_migrate_entry` that handles the scaffold's keys (`use_ssl`, old `webhook_id`), tested by a stub.
- Both `voluptuous` and the 2026.9 `probatio` alias work; use `probatio`-compatible schemas only via `homeassistant.helpers.config_validation`.
- Delete `strings.json`; maintain `translations/en.json` (config, options, `services`, `exceptions`, `entity`, `issues`). Do not define centrally-translated abort keys locally (2026.10 change, `developers.home-assistant.io/blog/2026/09/28/central-config-flow-abort-reasons`).
- `DeviceInfo(entry_type=SERVICE)` so entities group; `_attr_has_entity_name = True` with `translation_key`s.

### WI-4 — Inbound handler (`webhook_handler.py`)

```python
raw = await request.read()
sig = request.headers.get("X-Webhook-Hmac", "")
ok  = hmac.compare_digest(hmac.new(key, raw, hashlib.sha512).hexdigest(), sig)
```
Raw bytes, never re-serialised JSON (WAHA signs the exact string sent: `WebhookPlugin.sender.ts:65-70,170-179`).

| Case | Response | Why |
|---|---|---|
| Missing/bad signature | **401** | unauthenticated; retries are bounded to 8. WARNING rate-limited to 1/10 min; ≥ 20 failures → repair `hmac_mismatch` (stale key) |
| Valid signature, bad JSON / unknown event / wrong `session` / duplicate | **200** | retry cannot help |
| Internal error | 200 + log | core swallows handler exceptions as 200 anyway |

The handler does no network I/O: verify, dedupe, parse, `async_fire`, `coordinator.async_set_updated_data`. Slow handlers cause WAHA timeouts, which cause duplicate deliveries.

- **Dedupe:** in-memory `OrderedDict` of envelope `id`, TTL 30 min, 4096 max. Not persisted (WAHA's retries end within minutes).
- **Events:** `message` → `whatsapp_waha_message_received` (also dropped if `payload.fromMe`); `message.any` with `fromMe` → `whatsapp_waha_message_sent` (opt-in, default off); `session.status` → coordinator. **Nothing fires `received` from `message.any`**, so an automation cannot answer itself.
- **Bus payload:** `entry_id`, `session`, `message_id` (the FULL id, usable as `reply_to`), `chat_id` (`from`), `is_group`, `sender_id` (`participant` or `from`), `sender_phone` (digits if `@c.us`, else null), `sender_lid` (if `@lid`, else null), `sender_name`, `body`, `timestamp`, `has_media`, `media{url,mimetype,filename}`, `reply_to_id`. The `media.url` host is rewritten to the entry's base URL (WAHA builds it from `WAHA_BASE_URL`, often wrong); fetching needs `X-Api-Key`, and the handler never fetches.
- **Status:** the coordinator polls `GET /api/sessions/{name}` every 60 s as a *reconciler*; `session.status` pushes update immediately. `binary_sensor.connected` goes off only after status has been non-`WORKING` for `disconnect_grace` (default 60 s) so a PUT-induced or WAHA-restart blip cannot fire a "WhatsApp is down" automation that alerts *through WhatsApp*. A raw `status` sensor is not debounced. Persistent failures raise repairs and `persistent_notification`, which do not depend on WhatsApp.
- **End-to-end proof:** a "last webhook received" timestamp sensor. Session `WORKING`, our own send echoed via `message.any`, and no delivery arrived → repair `webhook_unreachable`. This is the only proof the WAHA → HA path works (registration alone can look green).

### WI-5 — Send queue and anti-ban (`send_queue.py`) — see next section

### WI-6 — Services, notify, groups

- `send_message`, `send_image`, plus `send_file`, `send_voice`, `send_video`, `send_sticker`, `send_poll`, `send_location`, `send_reaction`, `get_groups` (returns `{id, subject}`), `refresh_groups` (rate-limited 1 per 5 min; `groups/refresh` can hit `rate-overlimit`), `register_webhook`, `unregister_webhook`, `request_pairing_code`.
- Common fields: `config_entry_id?`, `chat_id` xor `group_name`, `priority: normal|alert`, `wait: bool`.
- **Service semantics:** default is non-blocking; returns `{queued, item_id, lane, position}` (`SupportsResponse.OPTIONAL`). `wait: true` blocks (default cap 120 s) and returns `{message_id}`.
- **Notify:** `notify.whatsapp_waha` registered in `async_setup`, plus a `NotifyEntity`. Fire-and-forget, lane from an option (default `alert`), `data.priority` overrides. Target is the group **id**, picked from the group list in the options flow.
- **Groups:** normalise NOWEB dict/WEBJS list into `Group(id, subject)`; cache 10 min; fetch only when `WORKING`. Name matching: exact, then case-insensitive exact, then **error listing candidate ids**. No substring matching: a wrong-group send is worse than a failure.
- **`file_path`:** `hass.config.is_allowed_path` in the executor (`core_config.py:656`, blocking) then read+base64 in the executor. **`url`:** passed through to WAHA (`file.url`) and **never fetched by HA**, or HA becomes an SSRF pivot into the LAN. Documented that WAHA fetches it from its own network.

### WI-7 — Session lifecycle

- **Linking** is decoupled from entry creation (entry exists once the session is created and started). Three surfaces: a QR `ImageEntity` (fritz precedent; a 5 s timer runs only while `SCAN_QR_CODE`; fetches `/auth/qr` `Accept: image/png`; bumps `image_last_updated` only when the sha256 changes; unavailable otherwise), a `request_pairing_code` service, and a repair `needs_link` whose `RepairsFlow` takes a phone number and shows the pairing code as text (the most reliable UX; images cannot be embedded in a flow reliably).
- `PASSKEY_REQUIRED`/`PASSKEY_CONFIRMATION_REQUIRED` → repair with the dashboard URL; no auto-restart.
- **Auto-restart** (option, default **off**): from `FAILED`; from `STOPPED` only if we did not command it (persist `intended_state=stopped` when our own stop/logout service runs). Rate-limited to 3 per 24 h with 5 m / 30 m / 2 h backoff. Never while a 463/475 cooldown is active, never from `SCAN_QR_CODE`/`PASSKEY_*`, never re-pairs. A dashboard stop cannot be told from a fault; hence default off.
- **Reauth:** 401 in the coordinator → `ConfigEntryAuthFailed`; 401 in a send path → `HomeAssistantError` + `entry.async_start_reauth`. 403 never reauths.
- **Boot:** WAHA unreachable → `ConfigEntryNotReady`; `ensure_webhook` runs only after the first successful refresh. Webhook state (`registered`/`drift`/`unknown`) is a separate diagnostic, so the connection sensor cannot be green while registration is broken.

### WI-8 — Diagnostics

`diagnostics.py` (redacts key, HMAC key, webhook id, chat ids/phone numbers); webhook state; queue depth per lane; counters; `me.reachoutTimelock`/`me.messageCapping`; last webhook received.

### WI-9 — Docs and packaging (last)

README rewrite (correct the stale claims S15, S18 and the wrong `binary_sensor` entity id claim, which the review could not verify); a manual-setup section; `CHANGELOG.md`; `hacs.json` + `brand/` (HA ≥ 2026.3 ships local brand images; `icon.png`, `dark_icon.png`, `logo.png`) — only when HACS is picked up.

---

## The part that quietly breaks

Two mechanisms fail *misleadingly*. They are the reason the build sequence has a spike first.

### 1. Webhook self-registration (WI-2)

**The trap.** A PUT that omits a field it did not read silently wipes it; a PUT to a `WORKING` session restarts it. **What the wrong version looks like when it fails:** the integration reports "registered", the binary sensor is green, and messages simply stop arriving. Or `noweb.store` is gone and chat history is lost after pairing. Or a reload loop PUTs repeatedly and the session flaps `STARTING`/`WORKING`, triggering a "WhatsApp is down" automation that tries to notify *via WhatsApp*.

**The fix.** GET → deep-copy → replace only our hook → PUT → re-GET-and-compare; create-with-webhook on first setup so no PUT is needed; one PUT per setup and 10-minute lockout; **never PUT onto a `WORKING` session without consent** (repair + `RepairsFlow`); "last webhook received" plus echo test; debounced connectivity sensor.

**Alternatives rejected**
- *Manual `WHATSAPP_HOOK_URL`*: unauthenticated, requires WAHA restart per change.
- *Global env hook signed with `WHATSAPP_HOOK_HMAC_KEY`*: still an LXC edit and a container restart; cannot be verified from HA; not created per entry.
- *Always PUT*: restarts a `WORKING` session; effect on in-flight messages / QR / WhatsApp flags **unverified**.
- *Remove our hook on unload*: another PUT/restart, and a dead hook is harmless.

**Constraints the fix creates.** All three events are subscribed permanently (changing the set = PUT). The merge must be tested against a real GET body, not a hand-written fixture. Comment `ensure_webhook` with the "never synthesise fields" rule.

### 2. The send queue (WI-5)

**The trap.** The anti-ban guide (`how-to-avoid-blocking`) wants a human-paced pipeline, but this same integration carries security alerts. Pacing every message equally makes lockout alerts arrive minutes late or time out gate-pin's REST call; skipping pacing entirely removes the protection for the secondary number. Separately, `startTyping` is *"infinitive"* (`chatting.controller.ts:262`): a crash between `startTyping` and `stopTyping` leaves the chat "typing" forever. And a retried `sendText` after an ambiguous timeout **double-sends**.

**What the wrong version looks like when it fails:** a stale motion alert delivered three hours late; five alerts arriving as five messages 60 s apart; a chat stuck on "typing…"; the same message twice; or, after a 463/475 error, an "auto-restart" that re-pairs the number, which the guide explicitly says not to do.

**The fix (design).** One `SendQueue` per entry: one worker task, two lanes (`alert`, `normal`), items `{id, chat, kind, payload, lane, enqueued, expires, future|None}`.

- **Pipeline** (skip `sendSeen` unless replying): `sendSeen` → `startTyping` → sleep `clamp(base + len(text)*per_char, lo, hi) * uniform(0.8, 1.3)` → `stopTyping` → send → inter-message gap. Normal lane ≈ 1 s + 0.06 s/char capped at 10 s, gap random 30–60 s. Alert lane capped at 2 s, gap 2–5 s. Presets `strict` (the guide's numbers) / `balanced` / `off`, plus raw numbers in the options flow.
- **Latency:** an arriving alert wakes the worker out of the normal gap sleep (`asyncio.wait` on an `Event`); worst-case latency is one typing delay plus the send. ≥ 3 pending alerts for one chat are **coalesced** into one message.
- **Caps:** normal lane per-chat hourly cap (default 30; 0 = off) plus a per-session cap; an over-cap item waits, bounded by its `expires` (default 30 min), then fails. Alert lane exempt from the per-chat cap but has a ceiling (default 20/chat/hour) beyond which items coalesce instead of drop. **Nothing is dropped silently:** every drop/expiry/failure fires `whatsapp_waha_send_failed {reason, chat_id, item_id, ambiguous}` and bumps a counter sensor; alert-lane failures also create a `persistent_notification`.
- **Depth and staleness:** normal 50 (reject-new with `ServiceValidationError`); alert 20 (drop-oldest); alert TTL 15 min so nothing arrives hours late.
- **Gating:** dequeue only while cached status is `WORKING`; never `startTyping` otherwise. Items burn TTL while waiting.
- **Retries:** never auto-retry a `sendText` that may have reached WAHA (timeout/5xx = ambiguous → fail with `ambiguous: true`). Retry only provable non-delivery (connection refused / DNS), max 3 with backoff, alert lane only.
- **463/475:** parse the WAHA body; fail the item, cool the chat down (default 6 h; 463 per chat, 475 session-wide), fire `send_failed`, raise a repair. **No restart, no re-pair.** Expose `me.reachoutTimelock` / `me.messageCapping` in diagnostics.
- **Shutdown/cancel:** only the send POST is `asyncio.shield`ed (unload waits ≤ 10 s for an in-flight send); `startTyping` sits in try/finally with a shielded `stopTyping` (5 s timeout, best-effort). A `Store` marker `{chat}` is written before `startTyping` and cleared after `stopTyping`; on next setup, if present and `WORKING`, send `stopTyping`. The queue itself is **not persisted** (replay risks duplicates and stale alerts); unload emits `send_failed(reason=shutdown)` for what was pending.

**Alternatives evaluated and rejected**
- *One lane for everything* (the user's original answer): late alerts / timing out gate-pin's REST call.
- *Bypass the queue for alerts entirely*: removes the shield on the shared number.
- *Persist the queue across restarts*: duplicates and stale alerts.
- *Block the service call until sent*: gate-pin's REST call has no long timeout and swallows errors (`ha.py:103-116`).
- *Typing simulation only, no queue* (offered in the interview): two automations can burst.

**Constraints it creates.** Service calls are asynchronous by default; automations that need the message id must pass `wait: true`. Alerts and family-group traffic share one WhatsApp number's budget (diagnostics show one per-session counter). gate-pin must keep a **non-WhatsApp** path for a "WhatsApp is down" alert (its `notify_service` "falls back to a persistent notification", `gate-pin/DOCS.md:118`).

---

## Risks, and what mitigates them

1. **Restarting a `WORKING` session via PUT is unverified** (dropped messages, re-QR, WhatsApp flags). *Accepted; mitigated* by never doing it automatically, and by the spike.
2. **GET may redact secrets**, corrupting `proxy`/`hmac.key` on a merge-PUT. *Mitigation:* spike step 2 before any PUT code; fallback is "create-with-webhook only".
3. **Silent webhook loss.** Events during an HA outage longer than the retry window are gone; no replay. *Accepted.*
4. **Wrong callback URL** (container-internal IP, proxy). *Mitigation:* URL shown in the flow, override option, "last webhook received" + echo test, repair `webhook_unreachable`.
5. **Ban risk** from the alert lane on a shared secondary number; WhatsApp's thresholds are not knowable. *Accepted; mitigated* by coalescing, ceilings, cooldowns; number is a secondary (brief §1).
6. **Typing left on after a hard crash.** *Mitigated* by the `Store` marker; unresolved if HA never restarts.
7. **Ambiguous send failures** → possible duplicate or lost message by design. *Accepted*; reported via `send_failed(ambiguous)`.
8. **Alerting through the down channel.** *Mitigated* by `persistent_notification`; gate-pin's own fallback is outside this repo folder.
9. **`@lid` senders** break automations that match `from` on a phone number. *Mitigation:* separate `sender_phone`/`sender_lid`; lazy LID→phone resolution deferred (unverified endpoint).
10. **Dashboard-edit race** during a PUT (milliseconds wide, no ETag). *Accepted.*
11. **Auto-restart cannot distinguish a dashboard stop from a fault.** *Mitigated* by default-off and rate limit.
12. **`:latest` image drift.** WAHA changes protocol/engines through the image. *Mitigation:* README recommends a pinned tag; `GET /api/server/version` in diagnostics.
13. **`sebastian-greco` integration coexisting** on the same HA would share nothing but could fight over a session's webhooks. *Mitigation:* our "ours" test is by URL prefix, so a foreign hook is preserved.

---

## Build sequence

Steps 1–4 need only the running-but-unlinked WAHA. **Step 2 must be validated before anything is built on registration.**

1. **API client + `FakeWaha`** (WI-1). Verify against the real LXC: 401 vs 403, `?all=true`, the real NOWEB groups shape, `auth/qr` on a `SCAN_QR_CODE` session.
2. **SPIKE: webhook round trip on a throwaway, unpaired session** (WI-2 prerequisites). Answer four questions: (a) does GET return `proxy`/`hmac.key` unredacted; (b) does PUT of the GET body round-trip; (c) does create-with-webhook work; (d) do signed deliveries verify over raw bytes with `X-Webhook-Hmac` (use a `session.status` event). Record the answers in this plan.

   **Status as of milestone 1 (2026-09-29): not yet run.** The server became reachable mid-build (moved from `192.168.0.60` to `.40`), but the user chose to skip live verification for this milestone rather than share the API key. `webhook_registration.SUPPORTS_SAFE_MERGE_PUT` is left at its assumed default (`True`) and `ensure_webhook`/`remove_webhook` are fully unit-tested against `FakeWaha` for both the `True` and `False` cases, but **the real answers are unknown** — do not point this integration at an already-paired, `WORKING` session until this spike has actually run. See `whatsapp-waha-integration/_build_plan/milestones/1-connect-link-receive/milestone-log.md` for the full writeup; update this note with the real answers once run.
3. **Skeleton rewrite** (WI-3): manifest, `runtime_data`, coordinator, `async_setup` services, config flow, reauth, migration, translations.
4. **Handler, dedupe, bus events, status push and debounce** (WI-4).
5. **Queue** (WI-5) against `FakeWaha` recording call order, with a fake clock. Ship it *disabled* (`off` preset) until step 8.
6. **Services, notify, groups** (WI-6).
7. **QR entity, pairing code, repairs, auto-restart** (WI-7).
8. **Link the secondary number**, then validate steps 5–6 with a handful of real sends before enabling the queue defaults.

   **Status as of milestone 2 (2026-09-29): not yet run.** Same constraint as step 2's spike — no WAHA API key was available this session. Steps 1–7 (the send queue, services, groups, notify, auto-restart, PASSKEY_* repairs, the webhook_unreachable echo-test upgrade) are built and fully unit-tested against `FakeWaha`; the queue ships with `DEFAULT_SEND_QUEUE_PRESET = "off"` per the design above, and pacing has **not** been turned on. See `whatsapp-waha-integration/_build_plan/milestones/2-send-safely-notify/milestone-log.md` for the full writeup, including which numeric assumptions (pacing preset figures, 463/475 durations, `send_text`'s response shape) remain unverified.
9. **Diagnostics, per-folder CI, `release-integration.sh`, README, packaging** (WI-8, WI-9).

---

## Verification

Automated (all runnable without a linked number):
1. `pytest` in `whatsapp-waha-integration/tests`, Python 3.14, `pytest-homeassistant-custom-component`; `hassfest` clean.
2. Config flow: good key; **401 → `invalid_auth`**; 403 → `insufficient_permissions`; unreachable → `cannot_connect`; session missing → create path; duplicate `(base URL, session)` aborts; reauth and reconfigure complete; old-shape entry migrates.
3. Webhook: correct HMAC → event fired; **bad/missing signature → 401 and no event**; bad JSON with a valid signature → 200; duplicate envelope id → one event; `fromMe` message → no `received`; handler makes no network calls (spy on the client).
4. Registration: 404 → create-with-webhook, no PUT; matching hook → no PUT; drift on a stopped session → one PUT preserving `noweb.store`/`proxy`/`metadata`/foreign hooks; drift on `WORKING` → no PUT and a repair issue; second PUT within 10 min refused; post-PUT mismatch → repair, no loop.
5. Queue: call order `seen → typing → stop → send`; `stopTyping` sent on cancellation; alert overtakes a normal-lane gap; ≥ 3 alerts coalesce; hourly cap; TTL expiry fires `send_failed`; 463 → cooldown and **no restart call**; ambiguous timeout → **no second `sendText`**; queue empty and `send_failed(shutdown)` on unload; not `WORKING` → nothing typed.
6. Groups: NOWEB dict and WEBJS list both resolve; ambiguous name lists candidates and sends nothing; name not found; before `WORKING`.
7. Media: `file_path` outside `allowlist_external_dirs` rejected; `url` is passed to WAHA and never fetched by HA (assert no HA-side HTTP call).
8. Architecture guard (regex, like `gate-pin/tests/test_portability.py`): no `hass.components`, no blocking `open`/`read_bytes` in `async` code, no services registered in `async_setup_entry`, no `strings.json`.
9. `FakeWaha` fidelity tests: `401` vs `403` mode; groups as dict vs list.

Manual, against the LXC:
10. Steps 1–2 of the build sequence (above), on an unpaired throwaway session.
11. Scan the QR from the entity with the secondary number; status → `WORKING`; `message` and `message.any` deliveries verify; **only then** verify: a real send to a test group, `stopTyping` after a forced HA restart mid-delay, the `WORKING` restart behaviour of a PUT on a spare session, and 463/475 body shapes if reproducible.
12. `notify.whatsapp_waha` from gate-pin's `notify_service` (`notify_service` value `notify.whatsapp_waha`) returns within seconds, delivers, and the WAHA-down path produces a `persistent_notification`.

Not verifiable until a number is linked: anything that actually sends, the effect of restarting a `WORKING` NOWEB session, typing/stop behaviour, 463/475 bodies, real `@lid` payloads, `me.*` timelock fields.

---

## Manual setup outside the codebase

1. **WAHA on the LXC** (already built): confirm the image tag (`GET /api/server/version`) and pin it in `docker-compose.yml` rather than `:latest`; set `WAHA_BASE_URL` to the LAN address HA can reach (media URLs); keep `WAHA_API_KEY` set (an unset key is regenerated on every restart).
2. **Delete the global `WHATSAPP_HOOK_URL` / `WHATSAPP_HOOK_EVENTS`** from the LXC compose, once the integration self-registers (harmless in the meantime, see WI-2).
3. **HA URLs:** set the *internal URL* under Settings → System → Network so `async_generate_url` has something; if HA is behind a reverse proxy, set `http.use_x_forwarded_for` / `trusted_proxies`.
4. **`allowlist_external_dirs`** in HA `configuration.yaml` for any directory `file_path` will read.
5. **Secondary phone** with WhatsApp; scan the QR (or use the pairing code) from *Linked Devices*. Keep volume low and warm up gradually (WAHA `how-to-avoid-blocking`).
6. **Install** by copying `whatsapp-waha-integration/custom_components/whatsapp_waha` into HA `config/custom_components/` and restarting HA (until HACS packaging exists; no script covers this today).
7. **gate-pin:** set `notify_service: notify.whatsapp_waha` only after the integration is linked and tested; keep a non-WhatsApp alert path.
8. Ordering: 1 → 3 → install → set up the integration → link the number → 2 → 7.

---

## Documentation to produce when this is built

- `whatsapp-waha-integration/README.md` (rewrite; fix the stale claims in S15, S18) and `CHANGELOG.md`.
- `AGENTS.md`: a paragraph for the integration folder (no `config.yaml`, own CI workflow and release script).
- Root `README.md`: list the integration beside the add-ons.
- This plan is copied to `docs/plans/whatsapp-waha-integration.md` (durable per `AGENTS.md:76`), *not* `_build_plan/` (temporary, `AGENTS.md:61-68`).
- `docs/plans/gate-pin-addon.md` needs no change; add a one-line pointer to this plan where it discusses WhatsApp link previews (`docs/plans/gate-pin-addon.md:470`): GET must stay inert.

## Absences found while exploring (design properties to preserve)

- No `custom_components/`, `hacs.json`, `translations/`, `tests/`, `conftest.py`, `pyproject.toml`, `quality_scale.yaml`, `diagnostics.py`, `icons.json`, `brand/`, `LICENSE` in the scaffold or the repo.
- No CI for integrations (no `hassfest`, no HACS action, no `pytest-homeassistant-custom-component`); `ci.yml:51` uses Python 3.12.
- No git tags; `release.sh` requires `config.yaml` and pushes to `main`. No release path for a folder without one.
- No lint/format config, no test-deps manifest (`ci.yml:53` installs `pytest` ad hoc).
- No WhatsApp/WAHA prior art anywhere else in the repo; the only mentions are incidental (`docs/plans/gate-pin-addon.md:258,470`, `gate-pin/rootfs/app/addon/routes_guest.py:8`).
- No shared messaging abstraction in gate-pin (`bot.py:93-95` calls Telegram directly); the only bridge is `notify_service` (`config.yaml:37`).
- **Enforced by absence:** HA never fetches user-supplied `url`s; no code path ever auto-restarts or re-pairs a session on a 463/475; the queue is never persisted; nothing fires `message_received` from own-message events.
