"""Constants for the whatsapp_waha integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "whatsapp_waha"

# --- config entry data keys ---
CONF_SESSION: Final = "session"
CONF_HMAC_KEY: Final = "hmac_key"
CONF_CALLBACK_BASE_URL: Final = "callback_base_url"
CONF_LAST_PUT_AT: Final = "last_put_at"

# CONF_HOST, CONF_PORT, CONF_SSL, CONF_API_KEY, CONF_WEBHOOK_ID come from
# homeassistant.const -- do not duplicate them here (scaffold bug S17).

DEFAULT_PORT: Final = 3000
DEFAULT_SESSION: Final = "default"

WEBHOOK_ID_PREFIX: Final = "whatsapp_waha_"

# hass.data key stashing the raw YAML config passed to async_setup, needed by
# discovery.async_load_platform to bootstrap the legacy notify.py platform.
DATA_HASS_CONFIG: Final = f"{DOMAIN}_hass_config"

# --- WAHA session states (NOWEB engine) ---
STATE_STARTING: Final = "STARTING"
STATE_SCAN_QR_CODE: Final = "SCAN_QR_CODE"
STATE_WORKING: Final = "WORKING"
STATE_FAILED: Final = "FAILED"
STATE_STOPPED: Final = "STOPPED"
STATE_PASSKEY_REQUIRED: Final = "PASSKEY_REQUIRED"
STATE_PASSKEY_CONFIRMATION_REQUIRED: Final = "PASSKEY_CONFIRMATION_REQUIRED"

# States for which ensure_webhook is allowed to PUT automatically -- nothing
# to lose because the session is not fully paired/serving traffic yet.
SAFE_TO_PUT_STATES: Final = frozenset(
    {STATE_STOPPED, STATE_FAILED, STATE_SCAN_QR_CODE, STATE_STARTING}
)

# --- WAHA webhook events we subscribe to (permanently -- changing this set
# means a PUT, see webhook_registration.py) ---
WEBHOOK_EVENT_MESSAGE: Final = "message"
WEBHOOK_EVENT_MESSAGE_ANY: Final = "message.any"
WEBHOOK_EVENT_SESSION_STATUS: Final = "session.status"
SUBSCRIBED_EVENTS: Final = (
    WEBHOOK_EVENT_MESSAGE,
    WEBHOOK_EVENT_MESSAGE_ANY,
    WEBHOOK_EVENT_SESSION_STATUS,
)

# --- bus events we fire ---
EVENT_MESSAGE_RECEIVED: Final = "whatsapp_waha_message_received"
EVENT_MESSAGE_SENT: Final = "whatsapp_waha_message_sent"

# --- HMAC / webhook signing ---
HMAC_HEADER: Final = "X-Webhook-Hmac"

# --- timing ---
COORDINATOR_UPDATE_INTERVAL_SECONDS: Final = 60
DISCONNECT_GRACE_SECONDS: Final = 60
WEBHOOK_PUT_LOCKOUT_SECONDS: Final = 600  # 10 minutes
ENVELOPE_DEDUPE_TTL_SECONDS: Final = 1800  # 30 minutes
ENVELOPE_DEDUPE_MAX_SIZE: Final = 4096
QR_REFRESH_INTERVAL_SECONDS: Final = 5
HMAC_FAILURE_WARNING_INTERVAL_SECONDS: Final = 600  # 1 per 10 min
HMAC_FAILURE_REPAIR_THRESHOLD: Final = 20
WEBHOOK_UNREACHABLE_GRACE_MINUTES: Final = 10

# --- options ---
CONF_FIRE_MESSAGE_SENT_EVENTS: Final = "fire_message_sent_events"
DEFAULT_FIRE_MESSAGE_SENT_EVENTS: Final = False

# --- repairs / issue ids ---
ISSUE_NEEDS_LINK: Final = "needs_link"
ISSUE_WEBHOOK_DRIFT: Final = "webhook_drift"
ISSUE_WEBHOOK_UNREACHABLE: Final = "webhook_unreachable"
ISSUE_HMAC_MISMATCH: Final = "hmac_mismatch"
ISSUE_PASSKEY_REQUIRED: Final = "passkey_required"
ISSUE_SEND_RATE_LIMITED: Final = "send_rate_limited"

# --- services ---
SERVICE_REGISTER_WEBHOOK: Final = "register_webhook"
SERVICE_UNREGISTER_WEBHOOK: Final = "unregister_webhook"
SERVICE_REQUEST_PAIRING_CODE: Final = "request_pairing_code"
SERVICE_SEND_MESSAGE: Final = "send_message"
SERVICE_SEND_IMAGE: Final = "send_image"
SERVICE_SEND_FILE: Final = "send_file"
SERVICE_SEND_VOICE: Final = "send_voice"
SERVICE_SEND_VIDEO: Final = "send_video"
SERVICE_SEND_STICKER: Final = "send_sticker"
SERVICE_SEND_POLL: Final = "send_poll"
SERVICE_SEND_LOCATION: Final = "send_location"
SERVICE_SEND_REACTION: Final = "send_reaction"
SERVICE_GET_GROUPS: Final = "get_groups"
SERVICE_REFRESH_GROUPS: Final = "refresh_groups"

ATTR_PHONE_NUMBER: Final = "phone_number"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_CHAT_ID: Final = "chat_id"
ATTR_GROUP_NAME: Final = "group_name"
ATTR_PRIORITY: Final = "priority"
ATTR_WAIT: Final = "wait"
ATTR_MESSAGE: Final = "message"
ATTR_REPLY_TO: Final = "reply_to"
ATTR_LINK_PREVIEW: Final = "link_preview"
ATTR_FILE_PATH: Final = "file_path"
ATTR_URL: Final = "url"
ATTR_CAPTION: Final = "caption"
ATTR_FILENAME: Final = "filename"
ATTR_MIMETYPE: Final = "mimetype"
ATTR_QUESTION: Final = "question"
ATTR_OPTIONS: Final = "options"
ATTR_MULTIPLE_ANSWERS: Final = "multiple_answers"
ATTR_LATITUDE: Final = "latitude"
ATTR_LONGITUDE: Final = "longitude"
ATTR_NAME: Final = "name"
ATTR_ADDRESS: Final = "address"
ATTR_MESSAGE_ID: Final = "message_id"
ATTR_EMOJI: Final = "emoji"

# --- send queue (WI-5) ---
LANE_NORMAL: Final = "normal"
LANE_ALERT: Final = "alert"

PRESET_OFF: Final = "off"
PRESET_BALANCED: Final = "balanced"
PRESET_STRICT: Final = "strict"
DEFAULT_SEND_QUEUE_PRESET: Final = PRESET_OFF

NORMAL_LANE_MAX_DEPTH: Final = 50
ALERT_LANE_MAX_DEPTH: Final = 20
ALERT_TTL_SECONDS: Final = 900  # 15 minutes
ALERT_COALESCE_THRESHOLD: Final = 3
ALERT_HOURLY_CEILING_DEFAULT: Final = 20
NORMAL_HOURLY_CAP_PER_CHAT_DEFAULT: Final = 30
NORMAL_HOURLY_CAP_PER_SESSION_DEFAULT: Final = 200  # provisional, not in the design doc
NORMAL_LANE_MAX_WAIT_SECONDS: Final = 1800  # 30 minutes, then cap_exceeded
SEND_RATE_LIMIT_COOLDOWN_SECONDS: Final = 6 * 3600  # 6h, unverified body/duration
UNREACHABLE_RETRY_MAX_ATTEMPTS: Final = 3
UNREACHABLE_RETRY_BACKOFF_SECONDS: Final = (2.0, 5.0, 10.0)
SEND_WAIT_TIMEOUT_SECONDS: Final = 120
GROUP_REFRESH_MIN_INTERVAL_SECONDS: Final = 300  # 5 minutes
GROUP_CACHE_TTL_SECONDS: Final = 600  # 10 minutes
TYPING_MARKER_STORE_VERSION: Final = 1
WEBHOOK_ECHO_PROBE_TIMEOUT_SECONDS: Final = 120  # 2 minutes to see the echo back

# --- notify (WI-6/D8) ---
CONF_NOTIFY_TARGET: Final = "notify_target"
CONF_NOTIFY_LANE: Final = "notify_lane"
DEFAULT_NOTIFY_LANE: Final = LANE_ALERT
NOTIFY_SERVICE_BASE_NAME: Final = "whatsapp_waha"

# --- send-queue related options ---
CONF_SEND_QUEUE_PRESET: Final = "send_queue_preset"
CONF_NORMAL_HOURLY_CAP: Final = "normal_hourly_cap"
CONF_AUTO_RESTART_ENABLED: Final = "auto_restart_enabled"
DEFAULT_AUTO_RESTART_ENABLED: Final = False
CONF_INTENDED_STATE: Final = "intended_state"
INTENDED_STATE_STOPPED: Final = "stopped"

# --- auto-restart limiter ---
AUTO_RESTART_MAX_ATTEMPTS_PER_DAY: Final = 3
AUTO_RESTART_BACKOFF_SECONDS: Final = (300.0, 1800.0, 7200.0)  # 5m / 30m / 2h

# --- send failure event ---
EVENT_SEND_FAILED: Final = "whatsapp_waha_send_failed"
