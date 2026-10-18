"""Headless OIDC login for Bitzer Smart Connect.

Supports mobile authorization-code + PKCE and the legacy web SPA session.
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
    ACCESS_AUTO,
    ACCESS_MOBILE,
    ACCESS_WEB,
    HTTP_TIMEOUT,
    LOGIN_BASE,
    MOBILE_CLIENT_ID,
    MOBILE_CLIENT_SECRET,
    MOBILE_REDIRECT_URI,
    MOBILE_SCOPE,
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

    def __init__(
        self, username: str, password: str, access_mode: str = ACCESS_AUTO
    ) -> None:
        if access_mode not in (ACCESS_AUTO, ACCESS_WEB, ACCESS_MOBILE):
            raise ValueError("unknown access mode")
        self.requested_access_mode = access_mode
        self.access_mode = ACCESS_WEB if access_mode == ACCESS_AUTO else access_mode
        self._login_lock = asyncio.Lock()
        self._refresh_token: str | None = None
        self._username = username
        self._password = password
        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(),
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            headers={"User-Agent": "ha-bitzer-smart-connect"},
        )
        self._token: Token | None = None
        self._www_ready = False

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def async_close(self) -> None:
        await self._session.close()

    # -- public -----------------------------------------------------------------

    async def async_login(self) -> Token:
        """Log in using the selected cloud client."""
        async with self._login_lock:
            return await self._login()

    async def _login(self) -> Token:
        preferred_mode = self.access_mode
        modes = [preferred_mode]
        if self.requested_access_mode == ACCESS_AUTO:
            modes.append(
                ACCESS_WEB if preferred_mode == ACCESS_MOBILE else ACCESS_MOBILE
            )
        failures: list[tuple[str, BitzerAuthError]] = []
        self._token = None
        self._www_ready = False
        for mode in modes:
            if mode != self.access_mode:
                self._session.cookie_jar.clear()
            self.access_mode = mode
            self._refresh_token = None
            try:
                self._token = await (
                    self._mobile_login()
                    if mode == ACCESS_MOBILE
                    else self._implicit_login()
                )
            except BitzerAuthError as err:
                if self.requested_access_mode != ACCESS_AUTO:
                    raise
                failures.append((mode, err))
                continue
            return self._token
        self.access_mode = preferred_mode
        # Preserve a transport failure rather than reporting an outage as bad credentials.
        error_type = (
            InvalidAuth
            if all(isinstance(err, InvalidAuth) for _, err in failures)
            else CannotConnect
        )
        detail = ", ".join(f"{mode}: {type(err).__name__}" for mode, err in failures)
        raise error_type(f"automatic login failed ({detail})") from failures[-1][1]

    def invalidate_token(self) -> None:
        """Force renewal after the service rejects a bearer token."""
        self._token = None

    async def async_access_token(self) -> str:
        """Return a valid Bearer token, re-logging in if needed."""
        async with self._login_lock:
            if self._token is None or not self._token.is_valid(
                datetime.now(timezone.utc)
            ):
                if self.access_mode == ACCESS_MOBILE and self._refresh_token:
                    self._token = await self._mobile_token(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self._refresh_token,
                        },
                        allow_expired_refresh=True,
                    )
                if self._token is None or not self._token.is_valid(
                    datetime.now(timezone.utc)
                ):
                    await self._login()
        assert self._token is not None
        return self._token.access_token

    async def async_bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.async_access_token()}"}

    @property
    def user_id(self) -> str | None:
        """The OIDC subject (used for boundary lookups), from the id/access token claims."""
        if self._token is None:
            return None
        claims = _jwt_claims(self._token.id_token) or _jwt_claims(
            self._token.access_token
        )
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
            html = await self._text(
                "GET", f"{WWW_BASE}/Configurations?deviceId={device_id}"
            )
            m = _TOKEN_RE.search(html)
        if not m:
            raise CannotConnect("could not obtain antiforgery token")
        return m.group(1)

    def invalidate_www_session(self) -> None:
        self._www_ready = False

    # -- mobile authorization code + PKCE ---------------------------------------

    async def _mobile_token(
        self, fields: dict[str, str], *, allow_expired_refresh: bool = False
    ) -> Token | None:
        status, _, body = await self._raw(
            "POST",
            f"{LOGIN_BASE}/connect/token",
            data={
                **fields,
                "client_id": MOBILE_CLIENT_ID,
                "client_secret": MOBILE_CLIENT_SECRET,
            },
        )
        try:
            data = json.loads(body)
        except ValueError as err:
            raise CannotConnect(
                f"token endpoint returned invalid JSON ({status})"
            ) from err
        if not isinstance(data, dict):
            raise CannotConnect("invalid token response")
        if status != 200:
            if allow_expired_refresh and data.get("error") == "invalid_grant":
                self._refresh_token = None
                return None
            if status in (400, 401, 403):
                raise InvalidAuth(f"mobile token exchange rejected ({status})")
            raise CannotConnect(f"mobile token endpoint returned {status}")
        try:
            token = Token(
                access_token=data["access_token"],
                id_token=data.get("id_token", ""),
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=int(data["expires_in"])),
            )
            if not isinstance(token.access_token, str) or not token.access_token:
                raise ValueError("empty access token")
        except (KeyError, TypeError, ValueError, OverflowError) as err:
            raise CannotConnect("invalid token response") from err
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        return token

    async def _mobile_login(self) -> Token:
        verifier = secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode(
            {
                "client_id": MOBILE_CLIENT_ID,
                "redirect_uri": MOBILE_REDIRECT_URI,
                "response_type": "code",
                "scope": MOBILE_SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        current = f"{LOGIN_BASE}/connect/authorize?{query}"
        self._refresh_token = None
        for _ in range(20):
            if current.split("?", 1)[0] == MOBILE_REDIRECT_URI:
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(current).query)
                if params.get("state") != [state]:
                    raise InvalidAuth("mobile callback state mismatch")
                if "error" in params or not params.get("code"):
                    raise InvalidAuth("mobile authorization denied")
                token = await self._mobile_token(
                    {
                        "grant_type": "authorization_code",
                        "code": params["code"][0],
                        "redirect_uri": MOBILE_REDIRECT_URI,
                        "code_verifier": verifier,
                    }
                )
                assert token is not None
                return token
            # Never send credentials or follow callbacks outside the identity provider.
            if (
                urllib.parse.urlsplit(current)[:2]
                != urllib.parse.urlsplit(LOGIN_BASE)[:2]
            ):
                raise CannotConnect("unexpected mobile authorization redirect")
            status, headers, body = await self._raw("GET", current)
            if (
                status == 200
                and "/account/login" in urllib.parse.urlsplit(current).path.lower()
            ):
                fields = _scrape_login_form(body)
                if not fields.get("__RequestVerificationToken"):
                    raise CannotConnect("login form has no antiforgery token")
                offset, encoded_offset = _tz_fields()
                fields.update(
                    Username=self._username,
                    Password=self._password,
                    RememberLogin="true",
                    ClientTimezoneOffsetMinutes=offset,
                    ClientTimeZoneOffset=encoded_offset,
                )
                status, headers, _ = await self._raw(
                    "POST",
                    current,
                    data=fields,
                    headers={"Origin": LOGIN_BASE, "Referer": current},
                )
                if status == 200:
                    raise InvalidAuth("mobile login rejected")
            location = headers.get("Location", "")
            if status not in _REDIRECT_CODES or not location:
                raise CannotConnect(f"mobile authorization returned {status}")
            current = _abs(current, location)
        raise CannotConnect("too many mobile authorization redirects")

    # -- legacy web implicit login ----------------------------------------------

    async def _implicit_login(self) -> Token:
        state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        q = urllib.parse.urlencode(
            {
                "client_id": OIDC_CLIENT_ID,
                "redirect_uri": OIDC_REDIRECT_URI,
                "response_type": "id_token token",
                "scope": OIDC_SCOPE,
                "state": state,
                "nonce": nonce,
                "response_mode": "fragment",
            }
        )
        # 1) authorize -> a token fragment when the SSO session is still valid (silent refresh),
        #    else the login page. IdentityServer may bounce through /connect/authorize/callback first,
        #    and the fragment leads with id_token, so follow redirects and match order-independently.
        url = f"{LOGIN_BASE}/connect/authorize?{q}"
        login_url = None
        for _ in range(8):
            status, headers, _ = await self._raw("GET", url)
            loc = headers.get("Location", "")
            if status == 302 and _has_token_fragment(loc):
                return _token_from_fragment(loc)
            if status != 302 or not loc:
                raise CannotConnect(f"unexpected authorize response {status}")
            if "/account/login" in loc.lower():
                login_url = _abs(url, loc)
                break
            url = _abs(url, loc)
        if login_url is None:
            raise CannotConnect("authorize did not resolve to a token or login page")

        # 2) login form
        html = await self._text("GET", login_url)
        form = _scrape_login_form(html)
        m = _TOKEN_RE.search(html)
        if not m:
            raise CannotConnect("login form has no antiforgery token")
        form["__RequestVerificationToken"] = form.get(
            "__RequestVerificationToken"
        ) or m.group(1)
        form["Username"] = self._username
        form["Password"] = self._password
        form["RememberLogin"] = "true"
        off_min, off_enc = _tz_fields()
        form["ClientTimezoneOffsetMinutes"] = off_min
        form["ClientTimeZoneOffset"] = off_enc

        # 3) submit credentials
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
        authf = _abs(login_url, headers.get("Location", ""))

        # 4) authorize (authenticated) -> token fragment
        status, headers, _ = await self._raw("GET", authf)
        loc = headers.get("Location", "")
        if status == 302 and "#" not in loc and loc:
            status, headers, _ = await self._raw("GET", _abs(authf, loc))
            loc = headers.get("Location", "")
        if "access_token" not in loc:
            raise CannotConnect("no token in final redirect")
        return _token_from_fragment(loc)

    # -- www code flow ----------------------------------------------------------

    async def _establish_www_session(self) -> None:
        cur = f"{WWW_BASE}/Configurations?deviceId=0"
        for _ in range(12):
            status, headers, body = await self._raw("GET", cur)
            loc = headers.get("Location", "")
            if status in _REDIRECT_CODES and loc:
                cur = _abs(cur, loc)
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
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
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


def _has_token_fragment(loc: str) -> bool:
    """True when a redirect Location carries an access token in its URL fragment."""
    return "#" in loc and "access_token=" in loc.split("#", 1)[1]


def _token_from_fragment(loc: str) -> Token:
    frag = dict(urllib.parse.parse_qsl(loc.split("#", 1)[1]))
    expires_in = int(frag.get("expires_in", "3600"))
    return Token(
        access_token=frag["access_token"],
        id_token=frag.get("id_token", ""),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )


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
