"""Headless OIDC login for Bitzer Smart Connect.

Signs in as the mobile app's client (authorization code + PKCE, renewed by refresh token); yields its
bearer access token plus the www app's cookie session (silent authorization_code flow).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import html as html_lib
import json
import logging
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp

from ..const import (
    HTTP_TIMEOUT,
    LOGIN_BASE,
    OIDC_CLIENT_ID,
    OIDC_REDIRECT_URI,
    OIDC_SCOPE,
    WWW_BASE,
)
from .models import Token

_LOGGER = logging.getLogger(__name__)

_REDIRECT_CODES = (301, 302, 303, 307, 308)
_TOKEN_RE = re.compile(
    r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']'
)


class BitzerAuthError(Exception):
    """Base error for the auth layer."""


class CannotConnect(BitzerAuthError):
    """Unable to reach the service."""


class InvalidAuth(BitzerAuthError):
    """Credentials were rejected."""


class BitzerAuth:
    """Owns the credentialed session and hands out valid access tokens."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(),
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            headers={"User-Agent": "ha-bitzer-smart-connect"},
        )
        self._token: Token | None = None
        self._refresh_token: str | None = None
        self._lock = asyncio.Lock()  # one renewal at a time: a refresh token is single-use
        self._www_ready = False

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def async_close(self) -> None:
        await self._session.close()

    # -- public -----------------------------------------------------------------

    async def async_login(self) -> Token:
        """Log in with the credentials and store a fresh access token."""
        async with self._lock:
            return await self._login()

    async def async_access_token(self) -> str:
        """Return a valid Bearer token, renewing it if needed."""
        async with self._lock:
            expired = self._token is None or not self._token.is_valid(datetime.now(timezone.utc))
            if expired and not await self._refresh():
                await self._login()
        assert self._token is not None
        return self._token.access_token

    def invalidate_token(self) -> None:
        """Drop a token the API rejected, so the next request renews it."""
        self._token = None

    async def async_bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.async_access_token()}"}

    @property
    def user_id(self) -> str | None:
        """The OIDC subject (used for boundary lookups), from the id/access token claims."""
        if self._token is None:
            return None
        claims = _jwt_claims(self._token.id_token) or _jwt_claims(self._token.access_token)
        return claims.get("sub") or claims.get("userid") or claims.get("user_id")

    async def async_ensure_www_session(self) -> None:
        """Establish the ``www`` MVC cookie session (needed for Configurations/Save)."""
        await self.async_access_token()  # ensures IdP session cookie exists
        if self._www_ready:
            return
        await self._establish_www_session()
        self._www_ready = True

    async def async_csrf_token(self, device_id: int) -> str:
        """Fetch a fresh antiforgery token from the config page (www session must exist)."""
        await self.async_ensure_www_session()
        html = await self._text(
            "GET", f"{WWW_BASE}/Configurations?deviceId={device_id}"
        )
        m = _TOKEN_RE.search(html)
        if not m:
            # session may have lapsed; re-establish once and retry
            self._www_ready = False
            await self.async_ensure_www_session()
            html = await self._text("GET", f"{WWW_BASE}/Configurations?deviceId={device_id}")
            m = _TOKEN_RE.search(html)
        if not m:
            raise CannotConnect("could not obtain antiforgery token")
        return m.group(1)

    def invalidate_www_session(self) -> None:
        self._www_ready = False

    # -- login ------------------------------------------------------------------

    async def _login(self) -> Token:
        """Authorization code + PKCE, submitting the credentials when the IdP asks for them."""
        self._www_ready = False  # a fresh token means the www session must be re-established
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        q = urllib.parse.urlencode(
            {
                "client_id": OIDC_CLIENT_ID,
                "redirect_uri": OIDC_REDIRECT_URI,
                "response_type": "code",
                "scope": OIDC_SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        url = f"{LOGIN_BASE}/connect/authorize?{q}"
        submitted = False
        for _ in range(12):
            if url.startswith(OIDC_REDIRECT_URI):
                granted = await self._token_grant(
                    {
                        "grant_type": "authorization_code",
                        "code": _code_from_redirect(url, state),
                        "redirect_uri": OIDC_REDIRECT_URI,
                        "code_verifier": verifier,
                    }
                )
                if not granted:
                    raise CannotConnect("authorization code rejected")
                return self._token
            status, headers, body = await self._raw("GET", url)
            if status == 200 and _is_login_page(url) and not submitted:
                loc = await self._submit_credentials(url, body)
                submitted = True
            else:
                loc = headers.get("Location", "")
            if not loc:
                raise CannotConnect(f"unexpected authorize response {status}")
            url = _abs(url, loc)
        raise CannotConnect("authorize did not resolve to a code")

    async def _submit_credentials(self, login_url: str, html: str) -> str:
        """POST the login form; returns the redirect that continues the flow."""
        form = _scrape_login_form(html)
        m = _TOKEN_RE.search(html)
        if not m:
            raise CannotConnect("login form has no antiforgery token")
        form["__RequestVerificationToken"] = form.get("__RequestVerificationToken") or m.group(1)
        form["Username"] = self._username
        form["Password"] = self._password
        form["RememberLogin"] = "true"
        off_min, off_enc = _tz_fields()
        form["ClientTimezoneOffsetMinutes"] = off_min
        form["ClientTimeZoneOffset"] = off_enc

        status, headers, body = await self._raw(
            "POST",
            login_url,
            data=form,
            headers={"Origin": LOGIN_BASE, "Referer": login_url},
        )
        if status not in _REDIRECT_CODES:
            low = body.lower()
            if "invalid" in low or ("password" in low and "error" in low):
                raise InvalidAuth("credentials rejected")
            raise CannotConnect(f"login POST returned {status}")
        return headers.get("Location", "")

    async def _refresh(self) -> bool:
        """Renew the token with the refresh token; False once the IdP no longer accepts it."""
        if self._refresh_token is None:
            return False
        if await self._token_grant({"grant_type": "refresh_token", "refresh_token": self._refresh_token}):
            return True
        self._refresh_token = None
        return False

    async def _token_grant(self, grant: dict[str, str]) -> bool:
        """Exchange a grant for tokens and store them; False when the IdP rejects the grant."""
        status, _, body = await self._raw(
            "POST", f"{LOGIN_BASE}/connect/token", data={**grant, "client_id": OIDC_CLIENT_ID}
        )
        if status in (400, 401):
            return False
        if status != 200:
            raise CannotConnect(f"token endpoint returned {status}")
        try:
            data = json.loads(body)
            self._token = Token(
                access_token=data["access_token"],
                id_token=data.get("id_token", ""),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600))),
            )
        except (ValueError, KeyError, TypeError) as err:
            raise CannotConnect("invalid token response") from err
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        return True

    # -- www code flow ----------------------------------------------------------

    async def _establish_www_session(self) -> None:
        cur = f"{WWW_BASE}/Configurations?deviceId=0"
        resubmitted = False
        for _ in range(12):
            status, headers, body = await self._raw("GET", cur)
            loc = headers.get("Location", "")
            if status in _REDIRECT_CODES and loc:
                cur = _abs(cur, loc)
                continue
            if status == 200 and _is_login_page(cur) and not resubmitted:
                # a refresh-token grant renews the bearer but not the IdP's SSO cookie
                cur = _abs(cur, await self._submit_credentials(cur, body))
                resubmitted = True
                continue
            if status == 200:
                fm = re.search(
                    r'<form\b[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>',
                    body,
                    re.S | re.I,
                )
                if fm and "signin-oidc" in fm.group(1):
                    action = _abs(cur, fm.group(1))
                    status, headers, _ = await self._raw(
                        "POST", action, data=_form_inputs(fm.group(2))
                    )
                    loc = headers.get("Location", "")
                    if loc:
                        cur = _abs(action, loc)
                        continue
            return

    # -- low-level --------------------------------------------------------------

    async def _raw(
        self,
        method: str,
        url: str,
        *,
        data: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, "aiohttp.typedefs.LooseHeaders", str]:
        try:
            async with self._session.request(
                method, url, data=data, headers=headers, allow_redirects=False
            ) as resp:
                text = await resp.text()
                return resp.status, resp.headers, text
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err

    async def _text(self, method: str, url: str, **kw) -> str:
        _, _, text = await self._raw(method, url, **kw)
        return text


# -- module helpers -------------------------------------------------------------


def _abs(current: str, loc: str) -> str:
    return loc if loc.startswith("http") else urllib.parse.urljoin(current, loc)


def _scrape_login_form(html: str) -> dict[str, str]:
    """Return all inputs of the login form (the <form> containing Password), HTML-unescaped."""
    forms = re.findall(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I)
    form_html = next((f for f in forms if 'name="Password"' in f), html)
    return _form_inputs(form_html)


def _form_inputs(form_html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", form_html):
        nm = re.search(r'name=["\']([^"\']*)["\']', tag)
        vl = re.search(r'value=["\']([^"\']*)["\']', tag)
        if nm:
            out[nm.group(1)] = html_lib.unescape(vl.group(1)) if vl else ""
    return out


def _is_login_page(url: str) -> bool:
    """The IdP's own login page: the only place the credentials may be posted."""
    return url.startswith(f"{LOGIN_BASE}/") and "/account/login" in urllib.parse.urlsplit(url).path.lower()


def _pkce_pair() -> tuple[str, str]:
    """A PKCE code verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _code_from_redirect(url: str, state: str) -> str:
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    if params.get("state") != state or "code" not in params:
        raise CannotConnect("authorization was not granted")
    return params["code"]


def _tz_fields() -> tuple[str, str]:
    """Reproduce the login page's JS: offset minutes + encodeURIComponent('+HH:MM')."""
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    utc_min = int(off.total_seconds() // 60)
    sign = "+" if utc_min >= 0 else "-"
    off_str = f"{sign}{abs(utc_min) // 60:02d}:{abs(utc_min) % 60:02d}"
    return str(-utc_min), urllib.parse.quote(off_str, safe="")


def _jwt_claims(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return {}
