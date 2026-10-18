"""Auth helpers: silent-refresh token fragment detection and parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.bitzer_smart_connect.api.auth import (
    BitzerAuth,
    CannotConnect,
    InvalidAuth,
    _has_token_fragment,
    _token_from_fragment,
)
from custom_components.bitzer_smart_connect.api.models import Token
from custom_components.bitzer_smart_connect.const import (
    ACCESS_AUTO,
    ACCESS_MOBILE,
    ACCESS_WEB,
)

# response_type "id_token token" -> the fragment leads with id_token, access_token follows.
SILENT_REFRESH = (
    "https://www.bitzersmartconnect.com/auth/callback/"
    "#id_token=aaa.bbb.ccc&access_token=xxx.yyy.zzz&token_type=Bearer&expires_in=3600&scope=openid"
)


def test_detects_token_fragment_when_id_token_leads():
    assert _has_token_fragment(SILENT_REFRESH) is True


def test_ignores_login_redirect():
    assert (
        _has_token_fragment(
            "https://login.bitzersmartconnect.com/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize"
        )
        is False
    )


def test_ignores_intermediate_callback_without_fragment():
    assert (
        _has_token_fragment(
            "https://login.bitzersmartconnect.com/connect/authorize/callback?client_id=BitzerIoC.SPA"
        )
        is False
    )


def test_parses_token_regardless_of_fragment_order():
    tok = _token_from_fragment(SILENT_REFRESH)
    assert tok.access_token == "xxx.yyy.zzz"
    assert tok.id_token == "aaa.bbb.ccc"


def _valid_token():
    return Token("valid", datetime.now(timezone.utc) + timedelta(hours=1))


async def test_auto_prefers_web():
    auth = BitzerAuth("u", "p", ACCESS_AUTO)
    auth._implicit_login = AsyncMock(return_value=_valid_token())
    auth._mobile_login = AsyncMock()
    try:
        await auth.async_login()
        assert auth.requested_access_mode == ACCESS_AUTO
        assert auth.access_mode == ACCESS_WEB
        auth._mobile_login.assert_not_awaited()
    finally:
        await auth.async_close()


@pytest.mark.parametrize(
    "failure", [InvalidAuth("denied"), CannotConnect("unsupported")]
)
async def test_auto_falls_back_to_mobile_and_reuses_it(failure):
    auth = BitzerAuth("u", "p", ACCESS_AUTO)
    auth._implicit_login = AsyncMock(side_effect=failure)
    auth._mobile_login = AsyncMock(side_effect=[_valid_token(), _valid_token()])
    try:
        assert (await auth.async_login()).access_token == "valid"
        assert auth.requested_access_mode == ACCESS_AUTO
        assert auth.access_mode == ACCESS_MOBILE
        auth.invalidate_token()
        assert await auth.async_access_token() == "valid"
        assert auth._implicit_login.await_count == 1
        assert auth._mobile_login.await_count == 2
    finally:
        await auth.async_close()


@pytest.mark.parametrize("mode", [ACCESS_MOBILE, ACCESS_WEB])
async def test_forced_mode_never_tries_other_method(mode):
    auth = BitzerAuth("u", "p", mode)
    auth._mobile_login = AsyncMock(side_effect=InvalidAuth("denied"))
    auth._implicit_login = AsyncMock(side_effect=InvalidAuth("denied"))
    try:
        with pytest.raises(InvalidAuth):
            await auth.async_login()
        assert auth._mobile_login.await_count == (mode == ACCESS_MOBILE)
        assert auth._implicit_login.await_count == (mode == ACCESS_WEB)
    finally:
        await auth.async_close()


@pytest.mark.parametrize(
    "mobile,web,expected",
    [
        (InvalidAuth("denied"), InvalidAuth("denied"), InvalidAuth),
        (CannotConnect("outage"), InvalidAuth("denied"), CannotConnect),
        (InvalidAuth("denied"), CannotConnect("outage"), CannotConnect),
    ],
)
async def test_auto_exhausts_modes_and_classifies_error(mobile, web, expected):
    auth = BitzerAuth("u", "p", ACCESS_AUTO)
    auth._mobile_login = AsyncMock(side_effect=mobile)
    auth._implicit_login = AsyncMock(side_effect=web)
    try:
        with pytest.raises(expected, match="automatic login failed"):
            await auth.async_login()
        auth._mobile_login.assert_awaited_once()
        auth._implicit_login.assert_awaited_once()
        assert auth._token is None
        assert auth.access_mode == ACCESS_WEB
    finally:
        await auth.async_close()
