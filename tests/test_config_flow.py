"""Config flow: user (login) step, auto single-device add, device dropdown, manual fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bitzer_smart_connect.api.auth import CannotConnect, InvalidAuth
from custom_components.bitzer_smart_connect.api.models import Device, DeviceConfig
from custom_components.bitzer_smart_connect.const import (
    ACCESS_AUTO,
    ACCESS_MOBILE,
    ACCESS_WEB,
    CONF_ACCESS_MODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

AUTH_LOGIN = "custom_components.bitzer_smart_connect.api.auth.BitzerAuth.async_login"
GET_USER_DEVICES = "custom_components.bitzer_smart_connect.api.client.BitzerClient.async_get_user_devices"
SETUP_ENTRY = "custom_components.bitzer_smart_connect.async_setup_entry"
UNLOAD_ENTRY = "custom_components.bitzer_smart_connect.async_unload_entry"

CREDS = {CONF_USERNAME: "max@example.com", CONF_PASSWORD: "hunter2"}
DEVICE = Device(id=1234, name="HRV")
DEVICE2 = Device(id=2677, name="Device 2677")


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_step_invalid_auth(hass):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    with patch(AUTH_LOGIN, new=AsyncMock(side_effect=InvalidAuth("bad"))):
        out = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert out["type"] is FlowResultType.FORM
    assert out["errors"] == {"base": "invalid_auth"}


async def test_user_step_cannot_connect(hass):
    result = await _start(hass)
    with patch(AUTH_LOGIN, new=AsyncMock(side_effect=CannotConnect("down"))):
        out = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert out["type"] is FlowResultType.FORM
    assert out["errors"] == {"base": "cannot_connect"}


async def test_single_device_auto_creates_entry(hass):
    """One discovered device -> entry created straight from the user step."""
    result = await _start(hass)
    with (
        patch(AUTH_LOGIN, new=AsyncMock(return_value=None)),
        patch(GET_USER_DEVICES, new=AsyncMock(return_value=[DEVICE])),
        patch(SETUP_ENTRY, new=AsyncMock(return_value=True)),
        patch(UNLOAD_ENTRY, new=AsyncMock(return_value=True)),
    ):
        out = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
        assert out["type"] is FlowResultType.CREATE_ENTRY
        await hass.config_entries.async_unload(out["result"].entry_id)
    assert out["title"] == "HRV"
    assert out["data"][CONF_ACCESS_MODE] == ACCESS_AUTO
    assert out["data"][CONF_USERNAME] == CREDS[CONF_USERNAME]
    assert out["data"][CONF_DEVICE_ID] == DEVICE.id
    assert out["result"].unique_id == str(DEVICE.id)


async def test_multiple_devices_show_dropdown(hass):
    result = await _start(hass)
    with (
        patch(AUTH_LOGIN, new=AsyncMock(return_value=None)),
        patch(GET_USER_DEVICES, new=AsyncMock(return_value=[DEVICE, DEVICE2])),
    ):
        step = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
        assert step["type"] is FlowResultType.FORM
        assert step["step_id"] == "device"
        with (
            patch(SETUP_ENTRY, new=AsyncMock(return_value=True)),
            patch(UNLOAD_ENTRY, new=AsyncMock(return_value=True)),
        ):
            out = await hass.config_entries.flow.async_configure(
                step["flow_id"], {CONF_DEVICE_ID: str(DEVICE2.id)}
            )
            assert out["type"] is FlowResultType.CREATE_ENTRY
            await hass.config_entries.async_unload(out["result"].entry_id)
    assert out["data"][CONF_DEVICE_ID] == DEVICE2.id
    assert out["result"].unique_id == str(DEVICE2.id)


async def test_manual_entry_when_discovery_empty(hass):
    result = await _start(hass)
    with (
        patch(AUTH_LOGIN, new=AsyncMock(return_value=None)),
        patch(GET_USER_DEVICES, new=AsyncMock(return_value=[])),
    ):
        step = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
        assert step["step_id"] == "device"
        keys = {str(k) for k in step["data_schema"].schema}
        assert CONF_DEVICE_ID in keys
        assert CONF_DEVICE_NAME in keys
        with (
            patch(SETUP_ENTRY, new=AsyncMock(return_value=True)),
            patch(UNLOAD_ENTRY, new=AsyncMock(return_value=True)),
        ):
            out = await hass.config_entries.flow.async_configure(
                step["flow_id"], {CONF_DEVICE_ID: 9999, CONF_DEVICE_NAME: "Manual HRV"}
            )
            assert out["type"] is FlowResultType.CREATE_ENTRY
            await hass.config_entries.async_unload(out["result"].entry_id)
    assert out["data"][CONF_DEVICE_ID] == 9999
    assert out["data"][CONF_DEVICE_NAME] == "Manual HRV"
    assert out["result"].unique_id == "9999"


@pytest.fixture(autouse=True)
def mock_config_read():
    async def config(device_id):
        return DeviceConfig(
            device_id=device_id,
            product_id=None,
            online=True,
            parameters={"test": object()},
        )

    with patch(
        "custom_components.bitzer_smart_connect.api.client.BitzerClient.async_get_config",
        side_effect=config,
    ) as mocked:
        yield mocked


async def test_configuration_failure_does_not_create_entry(hass, mock_config_read):
    result = await _start(hass)
    mock_config_read.side_effect = CannotConnect("denied")
    with (
        patch(AUTH_LOGIN, new=AsyncMock()),
        patch(GET_USER_DEVICES, new=AsyncMock(return_value=[DEVICE])),
    ):
        out = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert out["step_id"] == "device"
    assert out["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_switches_existing_web_entry_to_mobile(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1234", data={**CREDS, CONF_DEVICE_ID: 1234}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["step_id"] == "reconfigure"
    assert result["data_schema"]({**CREDS})[CONF_ACCESS_MODE] == ACCESS_WEB
    with (
        patch(AUTH_LOGIN, new=AsyncMock()),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            new=AsyncMock(return_value=True),
        ),
    ):
        out = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDS, CONF_ACCESS_MODE: ACCESS_MOBILE}
        )
    assert out["type"] is FlowResultType.ABORT
    assert out["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ACCESS_MODE] == ACCESS_MOBILE
    assert entry.unique_id == "1234"


async def test_explicit_web_mode_is_saved(hass):
    result = await _start(hass)
    with (
        patch(AUTH_LOGIN, new=AsyncMock()),
        patch(GET_USER_DEVICES, new=AsyncMock(return_value=[DEVICE])),
        patch(SETUP_ENTRY, new=AsyncMock(return_value=True)),
        patch(UNLOAD_ENTRY, new=AsyncMock(return_value=True)),
    ):
        out = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CREDS, CONF_ACCESS_MODE: ACCESS_WEB}
        )
        assert out["data"][CONF_ACCESS_MODE] == ACCESS_WEB
        await hass.config_entries.async_unload(out["result"].entry_id)


async def test_auto_choice_is_saved_after_mobile_fallback(hass):
    from datetime import datetime, timedelta, timezone

    from custom_components.bitzer_smart_connect.api.models import Token

    result = await _start(hass)
    with (
        patch(
            "custom_components.bitzer_smart_connect.api.auth.BitzerAuth._implicit_login",
            new=AsyncMock(side_effect=CannotConnect("web not allowed")),
        ),
        patch(
            "custom_components.bitzer_smart_connect.api.auth.BitzerAuth._mobile_login",
            new=AsyncMock(
                return_value=Token(
                    "token", datetime.now(timezone.utc) + timedelta(hours=1)
                )
            ),
        ),
        patch(GET_USER_DEVICES, new=AsyncMock(return_value=[DEVICE])),
        patch(SETUP_ENTRY, new=AsyncMock(return_value=True)),
        patch(UNLOAD_ENTRY, new=AsyncMock(return_value=True)),
    ):
        out = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
        assert out["data"][CONF_ACCESS_MODE] == ACCESS_AUTO
        await hass.config_entries.async_unload(out["result"].entry_id)
