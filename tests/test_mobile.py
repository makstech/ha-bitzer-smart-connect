"""Mobile cloud transport and PKCE regression tests, without live credentials."""

from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from custom_components.bitzer_smart_connect.api.auth import (
    BitzerAuth,
    CannotConnect,
    InvalidAuth,
)
from custom_components.bitzer_smart_connect.api.client import BitzerClient
from custom_components.bitzer_smart_connect.api.models import DeviceConfig
from custom_components.bitzer_smart_connect.api.realtime import BitzerHub
from custom_components.bitzer_smart_connect.const import (
    ACCESS_MOBILE,
    ACCESS_WEB,
    MOBILE_REDIRECT_URI,
)


async def test_mobile_pkce_login():
    auth = BitzerAuth("user", "password", ACCESS_MOBILE)
    state = ""
    challenge = ""

    async def raw(method, url, **kwargs):
        nonlocal state, challenge
        if "/connect/authorize?" in url:
            q = parse_qs(urlsplit(url).query)
            state, challenge = q["state"][0], q["code_challenge"][0]
            assert q["client_id"] == ["Bitzer.Balder.Code"]
            return 302, {"Location": "/Account/Login"}, ""
        if method == "GET":
            return (
                200,
                {},
                '<form><input name="Password"><input name="__RequestVerificationToken" value="csrf"></form>',
            )
        if "/Account/Login" in url:
            assert kwargs["data"]["Password"] == "password"
            return (
                302,
                {
                    "Location": MOBILE_REDIRECT_URI
                    + "?"
                    + urlencode({"state": state, "code": "code"})
                },
                "",
            )
        verifier = kwargs["data"]["code_verifier"]
        assert (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
            == challenge
        )
        return (
            200,
            {},
            json.dumps(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                }
            ),
        )

    auth._raw = AsyncMock(side_effect=raw)
    try:
        assert (await auth.async_login()).access_token == "access"
        assert auth._refresh_token == "refresh"
    finally:
        await auth.async_close()


@pytest.mark.parametrize(
    "callback", ["?code=c&state=wrong", "?error=access_denied&state=wrong"]
)
async def test_mobile_rejects_bad_callback(callback):
    auth = BitzerAuth("u", "p", ACCESS_MOBILE)
    auth._raw = AsyncMock(
        return_value=(302, {"Location": MOBILE_REDIRECT_URI + callback}, "")
    )
    try:
        with pytest.raises(InvalidAuth):
            await auth.async_login()
        assert auth._raw.await_count == 1
    finally:
        await auth.async_close()


async def test_mobile_rejects_foreign_redirect():
    auth = BitzerAuth("u", "p", ACCESS_MOBILE)
    auth._raw = AsyncMock(
        return_value=(302, {"Location": "https://example.com/Account/Login"}, "")
    )
    try:
        with pytest.raises(CannotConnect):
            await auth.async_login()
        assert auth._raw.await_count == 1
    finally:
        await auth.async_close()


async def test_refresh_rotates_token():
    auth = BitzerAuth("u", "p", ACCESS_MOBILE)
    auth._refresh_token = "old"
    auth._raw = AsyncMock(
        return_value=(
            200,
            {},
            json.dumps(
                {"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
            ),
        )
    )
    try:
        assert await auth.async_access_token() == "new"
        assert auth._refresh_token == "rotated"
        assert auth._raw.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    finally:
        await auth.async_close()


async def test_expired_refresh_falls_back_to_login():
    auth = BitzerAuth("u", "p", ACCESS_MOBILE)
    auth._refresh_token = "expired"
    auth._raw = AsyncMock(return_value=(400, {}, '{"error":"invalid_grant"}'))
    from datetime import datetime, timedelta, timezone

    from custom_components.bitzer_smart_connect.api.models import Token

    auth._mobile_login = AsyncMock(
        return_value=Token("fresh", datetime.now(timezone.utc) + timedelta(hours=1))
    )
    try:
        assert await auth.async_access_token() == "fresh"
        auth._mobile_login.assert_awaited_once()
    finally:
        await auth.async_close()


async def test_mobile_discovery_preserves_names():
    auth = _mock_auth()
    client = BitzerClient(auth)
    client._get_json = AsyncMock(
        return_value=[{"deviceId": 1234, "deviceName": "InoVent"}]
    )
    devices = await client.async_get_user_devices()
    assert devices[0].name == "InoVent"
    assert client._get_json.call_args.args[0].endswith("/Device/GetUserDevices")


async def test_mobile_write_confirms_actual_value(config_sample_json):
    auth = _mock_auth()
    client = BitzerClient(auth)
    client._request_json = AsyncMock(return_value=False)
    client._get_json = AsyncMock(return_value={"actualValue": "22.00"})
    config = DeviceConfig.from_json(config_sample_json)
    await client.async_set_parameters(1234, config, {"_USER.Control.TempSet": "22"})
    assert client._request_json.call_args.kwargs["json_body"] == {
        "DeviceId": 1234,
        "ParameterId": "_USER.Control.TempSet",
        "Value": "22",
    }
    assert config.parameters["_USER.Control.TempSet"].value == "22.00"
    auth.async_csrf_token.assert_not_called()


async def test_mobile_write_unconfirmed_raises(config_sample_json, monkeypatch):
    client = BitzerClient(_mock_auth())
    client._request_json = AsyncMock(return_value=True)
    client._get_json = AsyncMock(return_value={"actualValue": "21"})
    monkeypatch.setattr(
        "custom_components.bitzer_smart_connect.api.client.WRITE_CONFIRM_TIMEOUT", 0.01
    )
    with pytest.raises(CannotConnect):
        await client.async_set_parameters(
            1234,
            DeviceConfig.from_json(config_sample_json),
            {"_USER.Control.TempSet": "22"},
        )


async def test_mobile_write_rejects_readonly_before_request(config_sample_json):
    client = BitzerClient(_mock_auth())
    client._request_json = AsyncMock()
    with pytest.raises(ValueError):
        await client.async_set_parameters(
            1234,
            DeviceConfig.from_json(config_sample_json),
            {"_USER.Control.RunAct": "0"},
        )
    client._request_json.assert_not_awaited()


@pytest.mark.parametrize(
    "statuses,expected",
    [([401, 401], InvalidAuth), ([403], InvalidAuth), ([302], CannotConnect)],
)
async def test_mobile_api_auth_failures(statuses, expected):
    auth = _mock_auth()
    auth.async_bearer_headers = AsyncMock(
        return_value={"Authorization": "Bearer token"}
    )
    responses = []
    for status in statuses:
        response = MagicMock(status=status)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        responses.append(response)
    auth.session.request.side_effect = responses
    with pytest.raises(expected):
        await BitzerClient(auth)._request_json(
            "POST", "https://api.example/", json_body={}
        )
    assert auth.session.request.call_count == len(statuses)


@pytest.mark.parametrize("mode,count", [(ACCESS_MOBILE, 1), (ACCESS_WEB, 4)])
async def test_hub_subscription_mode(mode, count):
    hub = BitzerHub(MagicMock(access_mode=mode), 1234, AsyncMock())
    hub._invoke = AsyncMock()
    await hub._subscribe()
    assert hub._invoke.await_count == count
    assert hub._invoke.call_args_list[0].args == ("Subscribe", ["1234_Updates"])


async def test_mobile_api_retries_with_new_bearer():
    auth = _mock_auth()
    auth.async_bearer_headers = AsyncMock(
        side_effect=[{"Authorization": "Bearer old"}, {"Authorization": "Bearer fresh"}]
    )
    responses = []
    for status in (401, 200):
        response = MagicMock(status=status)
        response.text = AsyncMock(return_value='{"actualValue":22}')
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        responses.append(response)
    auth.session.request.side_effect = responses
    assert await BitzerClient(auth)._get_json("https://api.example/") == {
        "actualValue": 22
    }
    auth.invalidate_token.assert_called_once()
    assert (
        auth.session.request.call_args.kwargs["headers"]["Authorization"]
        == "Bearer fresh"
    )


async def test_mobile_writes_wait_for_previous_confirmation(config_sample_json):
    import asyncio

    client = BitzerClient(_mock_auth())
    first_read = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def request(method, url, **kwargs):
        calls.append(kwargs["json_body"]["Value"])

    async def read(url):
        if len(calls) == 1:
            first_read.set()
            await release.wait()
            return {"actualValue": "22"}
        return {"actualValue": "23"}

    client._request_json = AsyncMock(side_effect=request)
    client._get_json = AsyncMock(side_effect=read)
    config = DeviceConfig.from_json(config_sample_json)
    first = asyncio.create_task(
        client.async_set_parameters(1234, config, {"_USER.Control.TempSet": "22"})
    )
    await asyncio.wait_for(first_read.wait(), 1)
    second = asyncio.create_task(
        client.async_set_parameters(1234, config, {"_USER.Control.TempSet": "23"})
    )
    await asyncio.sleep(0)
    assert calls == ["22"]
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert calls == ["22", "23"]


async def test_concurrent_token_requests_share_login():
    import asyncio
    from datetime import datetime, timedelta, timezone

    from custom_components.bitzer_smart_connect.api.models import Token

    auth = BitzerAuth("u", "p", ACCESS_MOBILE)

    async def login():
        await asyncio.sleep(0)
        return Token("fresh", datetime.now(timezone.utc) + timedelta(hours=1))

    auth._mobile_login = AsyncMock(side_effect=login)
    try:
        assert await asyncio.gather(
            auth.async_access_token(), auth.async_access_token()
        ) == ["fresh", "fresh"]
        auth._mobile_login.assert_awaited_once()
    finally:
        await auth.async_close()


@pytest.mark.parametrize("body", ["True", "OK", ""])
async def test_write_acknowledgement_does_not_require_json(body):
    auth = _mock_auth()
    auth.async_bearer_headers = AsyncMock(
        return_value={"Authorization": "Bearer token"}
    )
    response = MagicMock(status=200)
    response.text = AsyncMock(return_value=body)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    auth.session.request.return_value = response
    result = await BitzerClient(auth)._request_json(
        "POST", "https://api.example/update", response_json=False
    )
    assert result == body


async def test_invalid_json_error_identifies_endpoint_without_response_body():
    auth = _mock_auth()
    auth.async_bearer_headers = AsyncMock(return_value={})
    response = MagicMock(status=200, content_type="text/html")
    response.text = AsyncMock(return_value="private response")
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    auth.session.request.return_value = response
    with pytest.raises(
        CannotConnect, match="GET /read: HTTP 200, invalid JSON"
    ) as error:
        await BitzerClient(auth)._get_json("https://api.example/read?private=query")
    assert "private" not in str(error.value)


async def test_mobile_write_timeout_checks_readback_without_resubmitting(
    config_sample_json,
):
    from custom_components.bitzer_smart_connect.api.client import RequestTimeout

    client = BitzerClient(_mock_auth())
    client._request_json = AsyncMock(side_effect=RequestTimeout("timed out"))
    client._get_json = AsyncMock(return_value={"actualValue": "24.0"})
    config = DeviceConfig.from_json(config_sample_json)
    await client.async_set_parameters(1234, config, {"_USER.Control.TempSet": "24"})
    client._request_json.assert_awaited_once()
    assert client._request_json.call_args.kwargs["timeout"] == 75
    assert config.parameters["_USER.Control.TempSet"].value == "24.0"


async def test_write_request_uses_separate_timeout():
    from custom_components.bitzer_smart_connect.api.client import RequestTimeout

    auth = _mock_auth()
    auth.async_bearer_headers = AsyncMock(return_value={})
    response = MagicMock()
    response.__aenter__ = AsyncMock(side_effect=TimeoutError())
    auth.session.request.return_value = response
    with pytest.raises(RequestTimeout):
        await BitzerClient(auth)._request_json(
            "POST", "https://api.example/write", timeout=75
        )
    assert auth.session.request.call_args.kwargs["timeout"].total == 75
    assert auth.session.request.call_count == 1


async def test_pending_cloud_value_is_not_device_confirmation(
    config_sample_json, monkeypatch
):
    client = BitzerClient(_mock_auth())
    client._request_json = AsyncMock(return_value="false")
    client._get_json = AsyncMock(
        return_value={"actualValue": "23.0", "userValue": "24.0"}
    )
    monkeypatch.setattr(
        "custom_components.bitzer_smart_connect.api.client.WRITE_CONFIRM_TIMEOUT", 0.01
    )
    with pytest.raises(
        CannotConnect, match="requested 24, reported 23.0, pending 24.0"
    ):
        await client.async_set_parameters(
            1234,
            DeviceConfig.from_json(config_sample_json),
            {"_USER.Control.TempSet": "24"},
        )


def _mock_auth():
    auth = MagicMock(access_mode=ACCESS_MOBILE)
    auth.async_access_token = AsyncMock(return_value="token")
    return auth


async def test_discovery_routes_after_automatic_login_selects_mode():
    auth = _mock_auth()
    auth.user_id = "user"

    async def select_web():
        auth.access_mode = ACCESS_WEB
        return "web-token"

    auth.async_access_token = AsyncMock(side_effect=select_web)
    client = BitzerClient(auth)
    client._get_json = AsyncMock(return_value=[{"deviceId": 1234}])
    assert (await client.async_get_user_devices())[0].id == 1234
    assert client._get_json.call_args.args[0].endswith(
        "/UserDevice/GetUserDevicesWithClaimTypes/user"
    )


async def test_write_routes_after_automatic_login_selects_mode(config_sample_json):
    auth = _mock_auth()
    auth.access_mode = ACCESS_WEB

    async def select_mobile():
        auth.access_mode = ACCESS_MOBILE
        return "mobile-token"

    auth.async_access_token = AsyncMock(side_effect=select_mobile)
    client = BitzerClient(auth)
    client._mobile_write = AsyncMock()
    config = DeviceConfig.from_json(config_sample_json)
    await client.async_set_parameters(1234, config, {"_USER.Control.TempSet": "22"})
    client._mobile_write.assert_awaited_once()
    auth.async_csrf_token.assert_not_called()
