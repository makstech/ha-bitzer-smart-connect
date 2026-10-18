"""Constants for the Bitzer Smart Connect integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "bitzer_smart_connect"

# --- Cloud endpoints (Bitzer Smart Connect / Lodam "Balder" platform) ---
LOGIN_BASE = "https://login.bitzersmartconnect.com"
API_BASE = "https://api.bitzersmartconnect.com/api"
WWW_BASE = "https://www.bitzersmartconnect.com"
SIGNALR_HUB = "https://api.bitzersmartconnect.com/signalr/deviceHub"

# --- OIDC (IdentityServer) client used by the official SPA ---
OIDC_CLIENT_ID = "BitzerIoC.SPA"
OIDC_REDIRECT_URI = f"{WWW_BASE}/auth/callback/"
OIDC_SCOPE = "openid profile email webapi_scope"

# Mobile app registration; these identify the distributed app, not a user's account.
MOBILE_CLIENT_ID = "Bitzer.Balder.Code"
MOBILE_CLIENT_SECRET = "j435jgjg345kgj34k5jgf32sv354"
MOBILE_REDIRECT_URI = "de.bitzer.balder:/oauth2redirect"
MOBILE_SCOPE = "webapi_scope openid offline_access profile email balder_scope"
CONF_ACCESS_MODE = "access_mode"
ACCESS_AUTO = "auto"
ACCESS_WEB = "web"
ACCESS_MOBILE = "mobile"

# --- Config entry keys ---
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105 - config key name, not a secret value
CONF_BOUNDARY_ID = "boundary_id"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_EXPOSE_ALL = (
    "expose_all"  # create entities for every config parameter, not just live read-outs
)
DEFAULT_EXPOSE_ALL = False
# Optional manual limit overrides (highest priority: override > product profile > API range).
CONF_TEMP_MIN = "temp_min"
CONF_TEMP_MAX = "temp_max"
CONF_TEMP_STEP = "temp_step"
CONF_FAN_MIN = "fan_min"
CONF_FAN_MAX = "fan_max"

# --- Behaviour / timing ---
# WebSocket-first: no steady polling. A single snapshot at setup, then the hub drives updates.
# Polling is only a bounded fallback while the hub is down.
HTTP_TIMEOUT = 30
# The parameter-write endpoint can wait about 60 seconds for the gateway reply.
WRITE_HTTP_TIMEOUT = 75
TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)  # re-login this early before expiry
DEFAULT_SCAN_INTERVAL = (
    120  # seconds; gentle reconcile poll that runs alongside the hub
)
MIN_SCAN_INTERVAL = 30
HUB_RECONNECT_MIN = 5  # seconds
HUB_RECONNECT_MAX = 300  # seconds (cap for exponential backoff)
HUB_RX_TIMEOUT = (
    45  # seconds without any frame (server pings ~16s) => connection is dead, reconnect
)
DEVICE_OFFLINE_GRACE = (
    90  # seconds the device must stay offline before entities go unavailable
)
POLL_FALLBACK_MIN = 5
POLL_FALLBACK_MAX = 300
WRITE_CONFIRM_TIMEOUT = 20  # seconds to await a hub echo after a write

MANUFACTURER = "Bitzer / Lodam"

__all__ = [
    "DOMAIN",
    "LOGIN_BASE",
    "API_BASE",
    "WWW_BASE",
    "SIGNALR_HUB",
    "OIDC_CLIENT_ID",
    "OIDC_REDIRECT_URI",
    "OIDC_SCOPE",
    "CONF_USERNAME",
    "CONF_PASSWORD",
    "CONF_BOUNDARY_ID",
    "CONF_DEVICE_ID",
    "CONF_DEVICE_NAME",
]
