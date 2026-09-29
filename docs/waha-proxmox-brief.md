# WAHA on Proxmox — Build Brief

**Date:** 2026-09-28
**Target:** Proxmox box at `192.168.0.4` (SSH `root@192.168.0.4`, web UI `:8006`)
**Goal:** Standalone WhatsApp HTTP API (WAHA) gateway on a Debian LXC, integrated with Home Assistant via native services.

---

## 1. Decision recap (from research)

- **WAHA** (`devlikeapro/waha`, Apache-2.0) — self-hosted REST API + web dashboard over unofficial WhatsApp Web engines. Free Core = 1 session, WEBJS/NOWEB engines. Docs: https://waha.devlike.pro
- **Hosting:** dedicated LXC on Proxmox (NOT inside HA) — HA reboots never touch the WhatsApp session; Chromium/websocket load isolated; LAN-only, no cloud.
- **Engine:** `NOWEB` (websocket, no browser) — ~0.1 CPU / 200 MB per session vs WEBJS which runs headless Chromium.
- **HA integration:** no existing HACS integration talks to WAHA directly (only a raw `rest_command` add-on and the unrelated `ha-wa-bridge`). We will write `whatsapp_waha` — a custom integration (Phase 3). Interim: `rest_command` + webhook (Phase 2).
- ⚠️ **All unofficial-API caveats apply:** violates WhatsApp ToS; ban risk. Use a **secondary number**, low volume, warm up gradually. Docs on avoiding blocks: https://waha.devlike.pro/docs/overview/avoid-blocking (read before going live).

---

## 2. Phase 1 — Create the LXC and install WAHA

### 2.1 Create LXC (Proxmox web UI: `https://192.168.0.4:8006`)

| Setting | Value |
|---|---|
| CT ID | `120` (next free) |
| Template | `debian-12-standard` (or latest debian-13) |
| Disk | 8 GB (thin) |
| CPU | 2 cores |
| RAM | 2048 MB (ballooning on, min 512) |
| Network | `vmbr0`, DHCP — then set static lease **waha** / `192.168.0.60` in your router |
| Options → Features | **Nesting ON** (required for Docker in LXC) |

Or via CLI on the Proxmox host:

```bash
pct create 120 local:vztmpl/debian-12-standard_12.x-x_amd64.tar.zst \
  --hostname waha --cores 2 --memory 2048 --swap 512 \
  --rootfs local-lvm:8 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1 --unprivileged 1 --onboot 1 --start 1
```

### 2.2 Install Docker inside the LXC

```bash
pct enter 120
# or: ssh root@192.168.0.60 once DHCP lease is reserved

apt update && apt install -y docker.io docker-compose-v2 curl
systemctl enable --now docker
```

### 2.3 WAHA docker-compose

```bash
mkdir -p /opt/waha && cd /opt/waha
```

`/opt/waha/docker-compose.yml`:

```yaml
services:
  waha:
    image: devlikeapro/waha:latest
    container_name: waha
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      WHATSAPP_DEFAULT_ENGINE: NOWEB
      WAHA_DASHBOARD_USERNAME: admin
      WAHA_DASHBOARD_PASSWORD: <generate-long-random>
      WAHA_API_KEY: <generate-long-random-uuid4>
      # Phase 2 — point at HA:
      WHATSAPP_HOOK_URL: http://192.168.0.8:8123/api/webhook/waha_incoming
      WHATSAPP_HOOK_EVENTS: message
    volumes:
      - ./sessions:/app/.sessions
```

```bash
docker compose up -d
docker logs -f waha    # wait for "WhatsApp HTTP API is running"
```

### 2.4 Link the WhatsApp number

1. Open `http://192.168.0.60:3000/dashboard` → login with admin creds → connect with API key.
2. Create/start session `default` → show QR → **scan with the secondary phone's WhatsApp** (Linked Devices).
3. Confirm status `WORKING`. Session persists in `./sessions` across restarts.

### 2.5 Verify send

```bash
curl -X POST http://192.168.0.60:3000/api/sendText \
  -H "X-Api-Key: <key>" -H "Content-Type: application/json" \
  -d '{"session":"default","chatId":"27XXXXXXXXX@c.us","text":"WAHA on Proxmox is live"}'
```

**Group:** same endpoint, `chatId` = `<group-id>@g.us`. List groups:
`GET /api/default/groups` (also how the HA integration resolves group names → IDs).

---

## 3. Phase 2 — Interim Home Assistant wiring (YAML, ~15 min)

In HA `configuration.yaml`:

```yaml
rest_command:
  whatsapp_send:
    url: "http://192.168.0.60:3000/api/sendText"
    method: POST
    headers:
      Content-Type: application/json
      X-Api-Key: "<WAHA_API_KEY>"
    payload: '{"session":"default","chatId":"{{ chat_id }}","text":"{{ text }}"}'

automation:
  - alias: "WhatsApp incoming"
    trigger:
      - platform: webhook
        webhook_id: waha_incoming
        allowed_methods: [POST]
        local_only: true
    action:
      - service: persistent_notification.create
        data:
          title: "WhatsApp from {{ trigger.json.payload.from.split('@')[0] }}"
          message: "{{ trigger.json.payload.body }}"
```

Test: Developer Tools → Actions → `rest_command.whatsapp_send` with `chat_id: 27XXXXXXXXX@c.us`, `text: hello`.

---

## 4. Phase 3 — Custom `whatsapp_waha` integration (the "write our own" part)

Native services instead of YAML. Fills a real gap (nothing on HACS does this).

**Architecture:** HA integration (Python, aiohttp) → WAHA REST on the LXC. No WhatsApp protocol code in HA.

**Components:**

| Piece | Purpose |
|---|---|
| `config_flow.py` | Host + API key; validate via `GET /api/sessions` |
| `__init__.py` | aiohttp session, webhook registration (`/api/webhook/waha_{entry_id}`), unload |
| `notify.py` / services | `whatsapp.send_message` (text, image via `sendImage`, group by name resolved through cached `GET /api/{session}/groups`) |
| `binary_sensor` | Session connected (`GET /api/sessions/{name}` → `WORKING`) |
| QR linking | While status = `SCAN_QR_CODE`, expose `GET /api/screenshot` as a camera/image entity; notify to scan |
| Triggers | Fire `whatsapp_message_received` events from webhook payloads |

**Build order:** hardcoded-config send + webhook events first → config flow → QR entity → group-name resolution → polish (retries, reconnection handling, media download).

**Deliver later:** publishable HACS repo.

---

## 5. Security & ops notes

- LXC is LAN-only; do **not** port-forward :3000. If remote access is ever needed, use VPN/Tailscale, not an exposed port.
- Long random `WAHA_API_KEY` + dashboard password; WAHA also supports Swagger behind auth.
- `local_only: true` on the HA webhook; WAHA key never leaves the LAN.
- HA ↔ LXC: add a DNS/hosts entry (`waha` → 192.168.0.60) or use the static IP everywhere.
- **Backup:** `/opt/waha/sessions/` is the only state that matters — include it in Proxmox backups (the linked-device session dies if you lose it and must re-scan QR).
- Healthcheck: Uptime Kuma (already supports WAHA as a notification provider) can both monitor `http://192.168.0.60:3000/api/sessions` and *send alerts through* this same gateway.
- Update path: `docker compose pull && docker compose up -d` — engine/protocol changes come from the image, integration untouched.

## 6. Rollback

```bash
cd /opt/waha && docker compose down
pct destroy 120   # LXC gone; nothing on HA touched until Phase 2/3
```
On HA: remove the Phase 2 YAML block / delete the integration entry.
