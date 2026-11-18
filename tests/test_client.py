"""BitzerClient: bearer transport and device listing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bitzer_smart_connect.api.auth import CannotConnect
from custom_components.bitzer_smart_connect.api.client import BitzerClient


def _auth(*responses: tuple[int, str]) -> MagicMock:
    auth = MagicMock(user_id="user-1")
    auth.async_access_token = AsyncMock(return_value="token")
    auth.async_bearer_headers = AsyncMock(return_value={"Authorization": "Bearer token"})
    contexts = []
    for status, body in responses:
        resp = MagicMock(status=status)
        resp.text = AsyncMock(return_value=body)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=resp)
        context.__aexit__ = AsyncMock(return_value=False)
        contexts.append(context)
    auth.session.get.side_effect = contexts
    return auth


async def test_rejected_token_is_renewed_once():
    auth = _auth((401, ""), (200, '{"ok": true}'))
    assert await BitzerClient(auth)._get_json("https://api.example/read") == {"ok": True}
    auth.invalidate_token.assert_called_once()


@pytest.mark.parametrize("statuses", [(401, 401), (302,), (500,)])
async def test_unusable_response_is_a_connection_error(statuses):
    auth = _auth(*((status, "") for status in statuses))
    with pytest.raises(CannotConnect):
        await BitzerClient(auth)._get_json("https://api.example/read")
    assert auth.session.get.call_count == len(statuses)


async def test_timeout_is_a_connection_error():
    auth = _auth()
    auth.session.get.side_effect = TimeoutError
    with pytest.raises(CannotConnect):
        await BitzerClient(auth)._get_json("https://api.example/read")


async def test_device_listing_has_names():
    auth = _auth((200, '[{"deviceId": 1234, "deviceName": "HRV"}]'))
    devices = await BitzerClient(auth).async_get_user_devices()
    assert [(d.id, d.name) for d in devices] == [(1234, "HRV")]
    assert auth.session.get.call_args.args[0].endswith("/Device/GetUserDevices")


@pytest.mark.parametrize("first", [(200, "[]"), (500, "")])
async def test_device_listing_falls_back_to_the_claim_types_endpoint(first):
    auth = _auth(first, (200, '[{"deviceId": 1234}, {"deviceId": 1234}]'))
    devices = await BitzerClient(auth).async_get_user_devices()
    assert [(d.id, d.name) for d in devices] == [(1234, "Device 1234")]
    assert auth.session.get.call_args.args[0].endswith("/UserDevice/GetUserDevicesWithClaimTypes/user-1")
