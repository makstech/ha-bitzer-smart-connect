"""Diagnostics support for Bitzer Smart Connect."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import BitzerConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BitzerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    parameters = coordinator.data or {}
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "active_access_mode": entry.runtime_data.auth.access_mode,
        "device_id": coordinator.device_id,
        "device_name": coordinator.device_name,
        "device_online": coordinator.device_online,
        "hub_connected": coordinator.hub_connected,
        "parameter_count": len(parameters),
        "parameters": [
            {
                "id": param.id,
                "key": param.key,
                "value": param.value,
                "min_value": param.min_value,
                "max_value": param.max_value,
                "read_only": param.read_only,
            }
            for param in parameters.values()
        ],
    }
