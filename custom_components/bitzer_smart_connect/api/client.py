"""Bitzer cloud reads and writes through mobile API or legacy web forms."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from decimal import Decimal, InvalidOperation

import aiohttp

from ..const import (
    ACCESS_MOBILE,
    API_BASE,
    WRITE_CONFIRM_TIMEOUT,
    WRITE_HTTP_TIMEOUT,
    WWW_BASE,
)
from .auth import BitzerAuth, CannotConnect, InvalidAuth
from .models import Boundary, Device, DeviceConfig

_LOGGER = logging.getLogger(__name__)


class RequestTimeout(CannotConnect):
    """A cloud request timed out; a submitted write may still take effect."""


class BitzerClient:
    """High-level cloud operations on top of :class:`BitzerAuth`."""

    def __init__(self, auth: BitzerAuth) -> None:
        self._auth = auth
        self._write_lock = asyncio.Lock()

    @property
    def auth(self) -> BitzerAuth:
        return self._auth

    # -- reads (Bearer) ---------------------------------------------------------

    async def async_get_boundaries(self) -> list[Boundary]:
        uid = self._auth.user_id
        await self._auth.async_access_token()
        uid = uid or self._auth.user_id
        if not uid:
            raise CannotConnect("no user id in token")
        data = await self._get_json(f"{API_BASE}/Boundary/GetUserBoundaries/{uid}")
        return [Boundary.from_json(b) for b in (data or [])]

    async def async_get_user_devices(self) -> list[Device]:
        """Discover named devices on mobile, or device IDs on the legacy web API."""
        await self._auth.async_access_token()
        if self._auth.access_mode == ACCESS_MOBILE:
            data = await self._get_json(f"{API_BASE}/Device/GetUserDevices")
            return [Device.from_json(d) for d in (data or [])]
        uid = self._auth.user_id
        if not uid:
            return []
        try:
            data = await self._get_json(
                f"{API_BASE}/UserDevice/GetUserDevicesWithClaimTypes/{uid}"
            )
        except CannotConnect:
            return []
        by_id: dict[int, Device] = {}
        for entry in data or []:
            did = entry.get("deviceId") if isinstance(entry, dict) else None
            if did is None or did in by_id:
                continue
            by_id[did] = Device(id=did, name=f"Device {did}")
        return list(by_id.values())

    async def async_get_config(self, device_id: int) -> DeviceConfig:
        """One configuration snapshot: parameter schema + current values (www, Bearer-authed)."""
        url = f"{WWW_BASE}/Configurations/AsJSON/?deviceId={device_id}&_=0"
        headers = await self._auth.async_bearer_headers()
        headers["X-Requested-With"] = "XMLHttpRequest"
        data = await self._get_json(url, headers=headers)
        return DeviceConfig.from_json(data or {})

    async def async_get_status(self, device_id: int) -> bool:
        data = await self._get_json(f"{API_BASE}/devicedata/{device_id}/status")
        return bool(data)

    async def async_get_localization(self, product_id: str) -> dict[str, str]:
        try:
            data = await self._get_json(
                f"{API_BASE}/Localization/ProductStrings/{product_id}"
            )
        except CannotConnect:
            return {}
        return data if isinstance(data, dict) else {}

    # -- write (www MVC form: cookie session + antiforgery) ---------------------

    async def async_set_parameters(
        self, device_id: int, config: DeviceConfig, changes: dict[str, str]
    ) -> None:
        """Write using the selected access mode.

        ``changes`` maps Parameter.id to its new string value. Mobile writes are
        serialized and confirmed by readback; web writes use Configurations/Save.
        """
        await self._auth.async_access_token()
        if self._auth.access_mode == ACCESS_MOBILE:
            async with self._write_lock:
                await self._mobile_write(device_id, config, changes)
            return
        token = await self._auth.async_csrf_token(device_id)
        body: dict[str, str] = {"btnSubmit": "", "hdnDeviceId": str(device_id)}
        for param in config.parameters.values():
            body[param.save_field()] = "-1" if param.is_combo else ""
        for pid, value in changes.items():
            param = config.parameters.get(pid)
            if param is None:
                raise ValueError(f"unknown parameter {pid}")
            body[param.save_field()] = str(value)
        body["__RequestVerificationToken"] = token

        headers = await self._auth.async_bearer_headers()
        headers["Origin"] = WWW_BASE
        headers["Referer"] = f"{WWW_BASE}/main/configurations/{device_id}"
        status, resp_headers, text = await self._auth._raw(  # noqa: SLF001 - shared session
            "POST",
            f"{WWW_BASE}/Configurations/Save",
            data=body,
            headers=headers,
        )
        if status not in (200, 302):
            # a lapsed www session yields a redirect to login; retry once fresh
            self._auth.invalidate_www_session()
            token = await self._auth.async_csrf_token(device_id)
            body["__RequestVerificationToken"] = token
            status, resp_headers, text = await self._auth._raw(  # noqa: SLF001
                "POST", f"{WWW_BASE}/Configurations/Save", data=body, headers=headers
            )
        if status not in (200, 302):
            raise CannotConnect(f"Save returned {status}")

    async def _mobile_write(
        self, device_id: int, config: DeviceConfig, changes: dict[str, str]
    ) -> None:
        for pid in changes:
            if pid not in config.parameters or config.parameters[pid].read_only:
                raise ValueError(f"unknown or read-only parameter {pid}")
        for pid, value in changes.items():
            try:
                await self._request_json(
                    "POST",
                    f"{API_BASE}/GoogleHome/update-device-parameter",
                    json_body={
                        "DeviceId": device_id,
                        "ParameterId": pid,
                        "Value": str(value),
                    },
                    response_json=False,
                    timeout=WRITE_HTTP_TIMEOUT,
                )
            except RequestTimeout:
                # Never resubmit an ambiguous write. It may have reached the unit.
                # Check the reported value before deciding whether it failed.
                pass
            # An HTTP success (or boolean response) is not device confirmation.
            deadline = asyncio.get_running_loop().time() + WRITE_CONFIRM_TIMEOUT
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    async with asyncio.timeout(max(remaining, 0)):
                        data = await self._get_json(
                            f"{API_BASE}/GoogleHome/get-device-parameter/{device_id}?"
                            + urllib.parse.urlencode({"parameterId": pid})
                        )
                except TimeoutError as err:
                    raise CannotConnect(
                        f"write confirmation timed out for {pid}"
                    ) from err
                if isinstance(data, dict) and _same_value(
                    data.get("actualValue"), value
                ):
                    return_value = str(data["actualValue"])
                    config.parameters[pid].value = return_value
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    actual = data.get("actualValue") if isinstance(data, dict) else None
                    pending = data.get("userValue") if isinstance(data, dict) else None
                    raise CannotConnect(
                        f"write was not confirmed for {pid}: requested {value}, "
                        f"reported {actual}, pending {pending}; cloud online status "
                        "does not guarantee the controller accepted the command"
                    )
                await asyncio.sleep(
                    min(1, max(0, deadline - asyncio.get_running_loop().time()))
                )

    # -- low level --------------------------------------------------------------

    async def _get_json(self, url: str, headers: dict | None = None):
        if self._auth.access_mode == ACCESS_MOBILE:
            return await self._request_json("GET", url, headers=headers)
        if headers is None:
            headers = await self._auth.async_bearer_headers()
        headers.setdefault("Accept", "application/json")
        try:
            async with self._auth.session.get(
                url, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    raise CannotConnect(f"{url} -> {resp.status} (auth?)")
                if resp.status == 401:
                    raise CannotConnect(f"{url} -> 401")
                resp.raise_for_status()
                text = await resp.text()
                if not text:
                    return None
                try:
                    return _loads(text)
                except ValueError as err:
                    raise CannotConnect(f"{url}: bad JSON") from err
        except aiohttp.ClientError as err:
            raise CannotConnect(str(err)) from err

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json_body=None,
        response_json: bool = True,
        timeout: float | None = None,
    ):
        endpoint = f"{method} {urllib.parse.urlsplit(url).path}"
        for attempt in range(2):
            request_headers = {
                **(headers or {}),
                **await self._auth.async_bearer_headers(),
                "Accept": "application/json",
            }
            try:
                request_options = {}
                if timeout is not None:
                    request_options["timeout"] = aiohttp.ClientTimeout(total=timeout)
                async with self._auth.session.request(
                    method,
                    url,
                    headers=request_headers,
                    json=json_body,
                    allow_redirects=False,
                    **request_options,
                ) as resp:
                    if resp.status == 401:
                        self._auth.invalidate_token()
                        if attempt == 0:
                            continue
                        raise InvalidAuth("mobile API rejected renewed credentials")
                    if resp.status == 403:
                        raise InvalidAuth("mobile API access denied")
                    if resp.status != 200:
                        raise CannotConnect(f"{endpoint}: HTTP {resp.status}")
                    text = await resp.text()
                    if not response_json:
                        # The write endpoint's acknowledgement need not be JSON.
                        # Success is checked separately against the reported value.
                        return text
                    try:
                        return json.loads(text) if text else None
                    except ValueError as err:
                        raise CannotConnect(
                            f"{endpoint}: HTTP {resp.status}, invalid JSON "
                            f"(content type {resp.content_type}, {len(text)} characters)"
                        ) from err
            except TimeoutError as err:
                raise RequestTimeout(f"{endpoint}: request timed out") from err
            except aiohttp.ClientError as err:
                raise CannotConnect(
                    f"{endpoint}: transport error ({type(err).__name__})"
                ) from err


def _loads(text: str):
    import json

    return json.loads(text)


def _same_value(actual: object, expected: object) -> bool:
    if actual is None:
        return False
    if str(actual) == str(expected):
        return True
    try:
        a, b = Decimal(str(actual)), Decimal(str(expected))
        return a.is_finite() and b.is_finite() and a == b
    except InvalidOperation:
        return False
