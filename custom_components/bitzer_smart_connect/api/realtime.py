"""Minimal SignalR (JSON hub protocol) client for the Bitzer ``deviceHub`` over aiohttp WS.

Dispatches server->client invocations via ``on_event(target, arguments)``; reconnects with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from ..const import (
    ACCESS_MOBILE,
    HUB_RECONNECT_MAX,
    HUB_RECONNECT_MIN,
    HUB_RX_TIMEOUT,
    SIGNALR_HUB,
)
from .auth import BitzerAuth

_LOGGER = logging.getLogger(__name__)

RS = "\x1e"  # SignalR record separator
_HANDSHAKE = json.dumps({"protocol": "json", "version": 1}) + RS
_PING = json.dumps({"type": 6}) + RS
_KEEPALIVE = 15  # seconds

EventCallback = Callable[[str, list], Awaitable[None] | None]


class BitzerHub:
    """Maintains a SignalR connection for one device and dispatches its events."""

    def __init__(
        self, auth: BitzerAuth, device_id: int, on_event: EventCallback
    ) -> None:
        self._auth = auth
        self._device_id = device_id
        self._on_event = on_event
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._runner: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._invocation_id = 0
        self._closing = False
        self.connected = False
        self.on_connection_change: Callable[[bool], None] | None = None

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._closing = False
        self._runner = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._closing = True
        if self._keepalive:
            self._keepalive.cancel()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = HUB_RECONNECT_MIN
        while not self._closing:
            try:
                await self._connect_and_listen()
                backoff = HUB_RECONNECT_MIN  # reset after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("hub session ended: %s", err)
            self._set_connected(False)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, HUB_RECONNECT_MAX)

    # -- connection -------------------------------------------------------------

    async def _connect_and_listen(self) -> None:
        token = await self._auth.async_access_token()
        conn_token = await self._negotiate(token)
        ws_url = (
            SIGNALR_HUB.replace("https://", "wss://", 1)
            + f"?id={conn_token}&access_token={token}"
        )
        async with self._auth.session.ws_connect(
            ws_url, headers={"Authorization": f"Bearer {token}"}, heartbeat=None
        ) as ws:
            self._ws = ws
            await ws.send_str(_HANDSHAKE)
            await self._await_handshake(ws)
            await self._subscribe()
            self._set_connected(True)
            self._keepalive = asyncio.ensure_future(self._keepalive_loop(ws))
            try:
                while not self._closing:
                    # The server pings ~every 16s; a longer silence means a half-open
                    # socket (no close frame), so time out and let _run reconnect.
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive(), timeout=HUB_RX_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        _LOGGER.debug(
                            "hub: no frame for %ss, reconnecting", HUB_RX_TIMEOUT
                        )
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_text(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                if self._keepalive:
                    self._keepalive.cancel()

    async def _negotiate(self, token: str) -> str:
        async with self._auth.session.post(
            f"{SIGNALR_HUB}/negotiate?negotiateVersion=1",
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
        ) as resp:
            if resp.status == 401 and self._auth.access_mode == ACCESS_MOBILE:
                self._auth.invalidate_token()
            resp.raise_for_status()
            data = await resp.json()
        return data.get("connectionToken") or data["connectionId"]

    async def _await_handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive(timeout=15)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError("no handshake response")
        for part in _split(msg.data):
            obj = json.loads(part)
            if obj.get("error"):
                raise ConnectionError(f"handshake error: {obj['error']}")

    async def _subscribe(self) -> None:
        # Mirror the official client's opening sequence (it calls both Subscribe casings).
        dev = self._device_id
        await self._invoke("Subscribe", [f"{dev}_Updates"])
        if self._auth.access_mode == ACCESS_MOBILE:
            return
        await self._invoke("subscribe", [f"{dev}_Updates"])
        await self._invoke("DeviceActivated", [str(dev)])
        await self._invoke("SetActiveViewString", [dev, "Configuration"])

    async def _invoke(self, target: str, args: list) -> None:
        assert self._ws is not None
        self._invocation_id += 1
        await self._ws.send_str(
            json.dumps(
                {
                    "arguments": args,
                    "invocationId": str(self._invocation_id),
                    "target": target,
                    "type": 1,
                }
            )
            + RS
        )

    async def _keepalive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while not ws.closed:
                await asyncio.sleep(_KEEPALIVE)
                await ws.send_str(_PING)
        except (asyncio.CancelledError, aiohttp.ClientError):
            pass

    async def _handle_text(self, data: str) -> None:
        for part in _split(data):
            if not part:
                continue
            try:
                obj = json.loads(part)
            except ValueError:
                continue
            if obj.get("type") == 1 and "target" in obj:  # server -> client invocation
                res = self._on_event(obj["target"], obj.get("arguments", []))
                if asyncio.iscoroutine(res):
                    await res

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            if self.on_connection_change:
                self.on_connection_change(value)


def _split(data: str) -> list[str]:
    return [p for p in data.split(RS) if p]
