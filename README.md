# WhatsApp (WAHA)

> Installable via HACS as a custom repository:
> `terencefaul/whatsapp-waha-integration`. That repo is a generated mirror
> (via `git subtree split`) of this folder — please open issues/PRs against
> [`terencefaul/homeassistant-addons`](https://github.com/terencefaul/homeassistant-addons)
> instead.

A Home Assistant custom integration that connects to your self-hosted [WAHA](https://waha.devlike.pro/)
(WhatsApp HTTP API) server, so Home Assistant can send and receive WhatsApp
messages natively. WAHA itself runs on your own server (a Proxmox LXC, a
Docker host, wherever) — this integration never runs WAHA for you.

**Status:** milestones 1 and 2 are built (connect/link/receive, and
send/pace/notify). The human-like send-pacing default ships **off** — see
"Sending safely" below before turning it on. See
`docs/plans/whatsapp-waha-integration.md` (repo root) for the full technical
design, and `_build_plan/milestones/*/milestone-log.md` for exactly what
each milestone shipped and what's still pending live verification against a
real WAHA server.

## What it does today

- **Setup screen** connecting to your WAHA server (host, port, API key,
  session name), creating the session on WAHA if it doesn't exist yet
- **Automatic, signed webhook registration** — Home Assistant registers
  itself with WAHA and verifies every delivery with an HMAC signature; no
  manual URL pasting, no editing WAHA's own config
- **QR code entity** to link your WhatsApp number, plus a pairing-code
  fallback (via a repair or the `request_pairing_code` service) for when
  scanning isn't convenient
- **A sensor showing whether the session is connected**, debounced so a
  brief blip doesn't fire a false "WhatsApp is down" automation
- **Incoming messages trigger Home Assistant automations** via the
  `whatsapp_waha_message_received` bus event
- **Sending text, images, files, voice notes, video, stickers, polls,
  locations, and reactions**, to a phone number or a WhatsApp group by name
- **Human-like pacing** on normal sends (a visible typing pause, a gap
  between messages) and a faster lane for urgent alerts that can interrupt
  that pacing — ships disabled (`off` preset) by default; see below
- **A standard `notify.whatsapp_waha` target** any automation or add-on
  (including gate-pin) can send a title and message to, plus a `notify`
  entity for `notify.send_message`
- **Diagnostics**, an auto-restart option for a failed/stopped session
  (off by default), and repairs for a WAHA passkey prompt or a WhatsApp
  rate-limit cooldown

## Installing

Until this is packaged for HACS, install by hand:

1. Copy `custom_components/whatsapp_waha/` into your Home Assistant config's
   `custom_components/` folder.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → search "WhatsApp (WAHA)".
4. Enter your WAHA server's address, API key, and session name.
5. If Home Assistant can't work out its own address (uncommon on a plain LAN
   setup, more likely behind a reverse proxy), you'll be asked for a callback
   base URL — the address WAHA should use to reach Home Assistant.
6. Scan the QR code entity with the phone you're linking, or use the
   **needs_link** repair's pairing-code option instead.
7. Watch the connected sensor turn on, then send yourself a test WhatsApp
   message and confirm it can trigger an automation.
8. In the integration's options, set a notify target (a chat or group id)
   if you want `notify.whatsapp_waha` to work — e.g. for gate-pin's
   `notify_service` setting.

See the technical plan's "Manual setup outside the codebase" section for
what to do on the WAHA server side (pinning the image tag, setting
`WAHA_BASE_URL`, `allowlist_external_dirs` for file sends, etc).

## Sending safely

The send queue's human-like pacing (typing pause, gap between messages,
per-chat/session hourly caps, a faster "alert" lane for urgent notices) ships
with the **`off`** preset by default: the typing-simulation calls
(`sendSeen`/`startTyping`/`stopTyping`) still run, but the delay and gap are
zero. **Do not switch the pacing preset to `balanced` or `strict` in the
options flow until you've verified a handful of real sends against your own
WAHA server** — the preset's exact numbers, and WAHA's 463/475 rate-limit
behavior, are taken from the design doc and are not yet verified live (see
the milestone 2 log).

## Events

- `whatsapp_waha_message_received` — fired for an inbound message not sent
  by you. Payload: `entry_id`, `session`, `message_id`, `chat_id`, `is_group`,
  `sender_id`, `sender_phone`, `sender_lid`, `sender_name`, `body`,
  `timestamp`, `has_media`, `media`, `reply_to_id`.
- `whatsapp_waha_message_sent` — fired for your own outbound message, **only**
  if the `fire_message_sent_events` option is turned on (off by default, since
  most setups don't want to react to their own sends).
- `whatsapp_waha_send_failed` — fired for every dropped, expired, or failed
  send (queue full, TTL expired, rate-limited, ambiguous failure, or a
  shutdown mid-send). Alert-lane failures also raise a persistent
  notification, since those are the ones most likely to matter right away.

## Services

- `whatsapp_waha.register_webhook` / `whatsapp_waha.unregister_webhook` —
  manually force webhook (re-)registration, including onto a live
  (`WORKING`) session. Normally you never need these; they exist for the
  `webhook_drift` repair and for troubleshooting.
- `whatsapp_waha.request_pairing_code` — request a WhatsApp pairing code for
  a phone number, as an alternative to the QR code.
- `whatsapp_waha.send_message` / `send_image` / `send_file` / `send_voice` /
  `send_video` / `send_sticker` / `send_poll` / `send_location` /
  `send_reaction` — send to `chat_id` or `group_name` (exactly one), with an
  optional `priority: normal|alert` and `wait: true` to block for the real
  message id instead of getting back a queued/`item_id` response.
- `whatsapp_waha.get_groups` / `refresh_groups` — read or force-refresh the
  cached group list (`refresh_groups` is rate-limited to once per 5 minutes).

All services accept an optional `config_entry_id`, needed only if you have
more than one WAHA connection set up.

## Not in this milestone (milestone 2)

Publishing to HACS, and editing or un-sending a message after it goes out.
See `_build_plan/prd.html` for the full project scope.

## Development

```
cd whatsapp-waha-integration
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest tests/
```

`hassfest` itself isn't runnable from the `homeassistant` PyPI package (it
only ships in a full core git checkout); `tests/test_manifest.py` and
`tests/test_portability.py` cover its most relevant checks for this
integration instead. See the milestone log for details.
