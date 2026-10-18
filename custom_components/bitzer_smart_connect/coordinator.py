"""Data coordinator: reconcile poll + real-time SignalR pushes."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BitzerAuth, BitzerClient, BitzerHub, CannotConnect, InvalidAuth
from .api.models import DeviceConfig, Parameter
from .const import ACCESS_MOBILE, DEVICE_OFFLINE_GRACE, DOMAIN

_LOGGER = logging.getLogger(__name__)

type BitzerConfigEntry = ConfigEntry["BitzerRuntimeData"]


@dataclass
class BitzerRuntimeData:
    """Objects shared across the config entry's platforms."""

    auth: BitzerAuth
    client: BitzerClient
    coordinator: "BitzerDeviceCoordinator"


class BitzerDeviceCoordinator(DataUpdateCoordinator[dict[str, Parameter]]):
    """Holds one device's parameters; refreshed by a gentle poll and by hub pushes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BitzerConfigEntry,
        client: BitzerClient,
        device_id: int,
        device_name: str,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {device_id}",
            update_interval=timedelta(seconds=scan_interval),
            config_entry=entry,
        )
        self.client = client
        self.device_id = device_id
        self.device_name = device_name
        self.config: DeviceConfig | None = None
        self.device_online: bool = False
        self.localization: dict[str, str] = {}
        self._hub: BitzerHub | None = None
        self._offline_timer: CALLBACK_TYPE | None = None

    # -- polling (reconcile) ----------------------------------------------------

    async def _async_update_data(self) -> dict[str, Parameter]:
        try:
            config = await self.client.async_get_config(self.device_id)
            online = (
                config.online
                if self.client.auth.access_mode == ACCESS_MOBILE
                else await self.client.async_get_status(self.device_id)
            )
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            raise UpdateFailed(str(err)) from err
        if not self.config and config.product_id:
            self.localization = await self.client.async_get_localization(config.product_id)
        self.config = config
        self._apply_online_signal(online)
        return config.parameters

    @callback
    def _apply_online_signal(self, online: bool) -> None:
        """Track device presence, debouncing offline so brief dropouts don't flap availability."""
        if online:
            if self._offline_timer is not None:
                self._offline_timer()
                self._offline_timer = None
            if not self.device_online:
                self.device_online = True
                self.async_update_listeners()
            return
        if not self.device_online or self._offline_timer is not None:
            return

        @callback
        def _commit_offline(_now) -> None:
            self._offline_timer = None
            self.device_online = False
            self.async_update_listeners()

        self._offline_timer = async_call_later(self.hass, DEVICE_OFFLINE_GRACE, _commit_offline)

    # -- realtime (hub) ---------------------------------------------------------

    async def async_start_hub(self) -> None:
        self._hub = BitzerHub(self.client.auth, self.device_id, self._handle_hub_event)
        self._hub.on_connection_change = self._on_hub_connection_change
        self._hub.start()

    async def async_stop_hub(self) -> None:
        if self._offline_timer is not None:
            self._offline_timer()
            self._offline_timer = None
        if self._hub is not None:
            await self._hub.stop()
            self._hub = None

    @property
    def hub_connected(self) -> bool:
        return self._hub is not None and self._hub.connected

    @callback
    def _on_hub_connection_change(self, connected: bool) -> None:
        if not connected:
            # ask for an immediate reconcile; the periodic cadence continues as the safety net
            self.hass.async_create_task(self.async_request_refresh())

    async def _handle_hub_event(self, target: str, args: list) -> None:
        if target == "parametersUpdatedJSON":
            self._apply_parameter_deltas(args)
        elif target == "deviceStatusChanged" and args:
            self._apply_online_signal(bool(args[0]))
        elif target in ("deviceUpdated", "alarmsUpdated", "parametersUpdated"):
            # covered by parametersUpdatedJSON / the reconcile poll; nudge listeners for availability
            self.async_update_listeners()

    @callback
    def _apply_parameter_deltas(self, args: list) -> None:
        if not args or self.data is None:
            return
        try:
            deltas = json.loads(args[0]) if isinstance(args[0], str) else args[0]
        except (ValueError, TypeError):
            return
        changed = False
        for d in deltas or []:
            name = d.get("Name")
            if not name:
                continue
            param = self._param_by_key(name)
            if param is None:
                continue
            new_val = d.get("ActualValueAsString")
            if new_val is None and d.get("ActualValue") is not None:
                new_val = str(d["ActualValue"])
            if new_val is not None and str(new_val) != (param.value or ""):
                param.value = str(new_val)
                changed = True
        if changed:
            self.async_set_updated_data(self.data)

    def _param_by_key(self, key: str) -> Parameter | None:
        if self.data is None:
            return None
        return self.data.get(f"_USER.{key}") or next(
            (p for p in self.data.values() if p.key == key), None
        )
