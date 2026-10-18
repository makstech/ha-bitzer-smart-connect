"""The Bitzer Smart Connect integration."""

from __future__ import annotations

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import BitzerAuth, BitzerClient, CannotConnect, InvalidAuth
from .const import (
    ACCESS_WEB,
    CONF_ACCESS_MODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import BitzerConfigEntry, BitzerDeviceCoordinator, BitzerRuntimeData

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: BitzerConfigEntry) -> bool:
    """Set up Bitzer Smart Connect from a config entry."""
    auth = BitzerAuth(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_ACCESS_MODE, ACCESS_WEB),
    )
    try:
        await auth.async_login()
    except InvalidAuth as err:
        await auth.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except CannotConnect as err:
        await auth.async_close()
        raise ConfigEntryNotReady(str(err)) from err

    client = BitzerClient(auth)
    device_id = int(entry.data[CONF_DEVICE_ID])
    scan = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = BitzerDeviceCoordinator(
        hass,
        entry,
        client,
        device_id,
        entry.data.get(CONF_DEVICE_NAME) or f"Device {device_id}",
        scan,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await auth.async_close()
        raise
    await coordinator.async_start_hub()

    entry.runtime_data = BitzerRuntimeData(auth, client, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BitzerConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = entry.runtime_data
    await runtime.coordinator.async_stop_hub()
    await runtime.auth.async_close()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: BitzerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
