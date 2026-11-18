"""Auth: authorization code + PKCE login, refresh-token renewal, www session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from custom_components.bitzer_smart_connect.api.auth import (
    BitzerAuth,
    CannotConnect,
    InvalidAuth,
    _code_from_redirect,
    _pkce_pair,
)
from custom_components.bitzer_smart_connect.api.models import Token
from custom_components.bitzer_smart_connect.const import (
    LOGIN_BASE,
    OIDC_REDIRECT_URI,
    WWW_BASE,
)

LOGIN_PAGE = '<form><input name="Password"><input name="__RequestVerificationToken" value="csrf"></form>'
LOGIN_URL = f"{LOGIN_BASE}/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback"


def _tokens(access: str = "access", refresh: str | None = "refresh") -> str:
    data = {"access_token": access, "expires_in": 3600}
    if refresh:
        data["refresh_token"] = refresh
    return json.dumps(data)


@pytest.fixture
async def auth():
    auth = BitzerAuth("user@example.com", "password")
    yield auth
    await auth.async_close()


class FakeIdp:
    """The IdP's side of a login: authorize -> login form -> callback -> token."""

    def __init__(self, *, callback_state: str | None = None, accept_password: bool = True) -> None:
        self.callback_state = callback_state
        self.accept_password = accept_password
        self.query: dict[str, list[str]] = {}
        self.posted: list[tuple[str, dict]] = []

    async def __call__(self, method, url, *, data=None, headers=None):
        if "/connect/authorize?" in url:
            self.query = parse_qs(urlsplit(url).query)
            return 302, {"Location": LOGIN_URL}, ""
        if url == LOGIN_URL and method == "GET":
            return 200, {}, LOGIN_PAGE
        self.posted.append((url, data))
        if url == LOGIN_URL:
            if not self.accept_password:
                return 200, {}, "Invalid username or password"
            state = self.callback_state or self.query["state"][0]
            return 302, {"Location": OIDC_REDIRECT_URI + "?" + urlencode({"code": "the-code", "state": state})}, ""
        assert url == f"{LOGIN_BASE}/connect/token"
        return 200, {}, _tokens()


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = _pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(verifier) <= 128


def test_code_is_read_from_the_custom_scheme_redirect():
    assert _code_from_redirect(f"{OIDC_REDIRECT_URI}?code=abc&state=s1", "s1") == "abc"


@pytest.mark.parametrize("query", ["code=abc&state=other", "error=access_denied&state=s1"])
def test_redirect_without_a_code_for_our_state_is_rejected(query):
    with pytest.raises(CannotConnect):
        _code_from_redirect(f"{OIDC_REDIRECT_URI}?{query}", "s1")


async def test_login_exchanges_the_code_with_its_verifier(auth):
    idp = FakeIdp()
    auth._raw = idp
    token = await auth.async_login()
    assert token.access_token == "access"
    assert auth._refresh_token == "refresh"
    (login_url, form), (_, grant) = idp.posted
    assert login_url.startswith(f"{LOGIN_BASE}/")
    assert (form["Username"], form["Password"]) == ("user@example.com", "password")
    assert grant["code"] == "the-code"
    assert "client_secret" not in grant
    digest = hashlib.sha256(grant["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == idp.query["code_challenge"][0]


async def test_login_rejects_a_callback_with_another_state(auth):
    auth._raw = FakeIdp(callback_state="forged")
    with pytest.raises(CannotConnect):
        await auth.async_login()


async def test_rejected_password_is_invalid_auth(auth):
    auth._raw = FakeIdp(accept_password=False)
    with pytest.raises(InvalidAuth):
        await auth.async_login()


async def test_credentials_are_never_posted_off_the_identity_provider(auth):
    async def raw(method, url, **kwargs):
        assert method == "GET"
        if "/connect/authorize?" in url:
            return 302, {"Location": "https://evil.example/Account/Login"}, ""
        return 200, {}, LOGIN_PAGE

    auth._raw = raw
    with pytest.raises(CannotConnect):
        await auth.async_login()


def _expire(auth: BitzerAuth) -> None:
    auth._token = Token("stale", datetime.now(UTC) - timedelta(seconds=1))


async def test_expired_token_is_renewed_with_the_refresh_token(auth):
    _expire(auth)
    auth._refresh_token = "old"
    auth._raw = AsyncMock(return_value=(200, {}, _tokens("renewed", "rotated")))
    assert await auth.async_access_token() == "renewed"
    assert auth._refresh_token == "rotated"
    assert auth._raw.call_args.kwargs["data"]["refresh_token"] == "old"
    auth._raw.assert_awaited_once()


async def test_refresh_token_is_kept_when_the_idp_does_not_rotate_it(auth):
    _expire(auth)
    auth._refresh_token = "old"
    auth._raw = AsyncMock(return_value=(200, {}, _tokens("renewed", None)))
    await auth.async_access_token()
    assert auth._refresh_token == "old"


async def test_rejected_refresh_token_falls_back_to_a_full_login(auth):
    _expire(auth)
    auth._refresh_token = "revoked"
    idp = FakeIdp()

    async def raw(method, url, *, data=None, headers=None):
        if data and data.get("grant_type") == "refresh_token":
            return 400, {}, '{"error":"invalid_grant"}'
        return await idp(method, url, data=data, headers=headers)

    auth._raw = raw
    assert await auth.async_access_token() == "access"
    assert auth._refresh_token == "refresh"


async def test_unreachable_idp_during_refresh_is_not_a_login(auth):
    _expire(auth)
    auth._refresh_token = "old"
    auth._raw = AsyncMock(side_effect=CannotConnect("down"))
    with pytest.raises(CannotConnect):
        await auth.async_access_token()
    assert auth._refresh_token == "old"
    auth._raw.assert_awaited_once()


async def test_concurrent_callers_share_one_renewal(auth):
    _expire(auth)
    auth._refresh_token = "old"

    async def raw(method, url, **kwargs):
        await asyncio.sleep(0)
        return 200, {}, _tokens("renewed", "rotated")

    auth._raw = AsyncMock(side_effect=raw)
    assert await asyncio.gather(*(auth.async_access_token() for _ in range(3))) == ["renewed"] * 3
    auth._raw.assert_awaited_once()


async def test_www_session_resubmits_credentials_when_the_sso_cookie_lapsed(auth):
    auth._token = Token("valid", datetime.now(UTC) + timedelta(hours=1))
    signin = f'<form action="{WWW_BASE}/signin-oidc" method="post"><input name="code" value="c"></form>'
    calls: list[tuple[str, str]] = []

    async def raw(method, url, *, data=None, headers=None):
        calls.append((method, url))
        if method == "GET" and url.startswith(f"{WWW_BASE}/Configurations"):
            if ("POST", f"{WWW_BASE}/signin-oidc") in calls:
                return 200, {}, "<html>config page</html>"
            return 302, {"Location": LOGIN_URL}, ""
        if url == LOGIN_URL and method == "GET":
            return 200, {}, LOGIN_PAGE
        if url == LOGIN_URL:
            assert data["Password"] == "password"
            return 302, {"Location": "/connect/authorize/callback"}, ""
        if url.endswith("/connect/authorize/callback"):
            return 200, {}, signin
        assert (method, url) == ("POST", f"{WWW_BASE}/signin-oidc")
        return 302, {"Location": "/Configurations?deviceId=0"}, ""

    auth._raw = raw
    await auth.async_ensure_www_session()
    assert calls.count(("POST", LOGIN_URL)) == 1
    assert calls[-1] == ("GET", f"{WWW_BASE}/Configurations?deviceId=0")
