"""Check cloud access without Home Assistant; credentials stay in memory.

Run: python scripts/check_mobile_connection.py
Optionally: --device 1234 --set _USER.Control.TempSet=21.5
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import sys
import types
from pathlib import Path


def load_api():
    # Load the actual integration API without importing its Home Assistant entry point.
    package = types.ModuleType("bitzer_probe")
    package.__path__ = [
        str(
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "bitzer_smart_connect"
        )
    ]
    sys.modules[package.__name__] = package
    return importlib.import_module("bitzer_probe.api")


async def check(args, username: str, password: str) -> int:
    api = load_api()
    auth = api.BitzerAuth(username, password, args.access_mode)
    try:
        await auth.async_login()
        print(f"Login: OK (selected mode: {auth.access_mode})")
        client = api.BitzerClient(auth)
        devices = await client.async_get_user_devices()
        print(f"Discovery: {len(devices)} device(s)")
        for device in devices:
            print(f"  {device.id}: {device.name}")
        selected = [d.id for d in devices] if args.device is None else [args.device]
        if not selected:
            print(
                "No devices discovered. Retry with --device ID if you know your device ID."
            )
            return 1
        if args.set and len(selected) != 1:
            print("Choose one device with --device ID before writing.")
            return 1
        for device_id in selected:
            config = await client.async_get_config(device_id)
            if config.device_id != device_id or not config.parameters:
                print(f"Device {device_id}: no usable configuration")
                return 1
            print(
                f"Device {device_id}: {len(config.parameters)} parameters; online={config.online}"
            )
            for pid in ("_USER.Control.VentSet", "_USER.Control.TempSet"):
                param = config.parameters.get(pid)
                if param:
                    print(f"  {pid} = {param.value}")
            if args.set:
                pid, value = args.set.split("=", 1)
                await client.async_set_parameters(device_id, config, {pid: value})
                print(f"Write confirmed: {pid} = {config.parameters[pid].value}")
        return 0
    except api.BitzerAuthError as err:
        print(f"{type(err).__name__}: {err}")
        return 1
    finally:
        await auth.async_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device", type=int, help="Device ID; otherwise check all discovered devices"
    )
    parser.add_argument(
        "--set",
        metavar="PARAMETER=VALUE",
        help="Write one parameter and confirm readback",
    )
    parser.add_argument(
        "--access-mode",
        choices=("auto", "web", "mobile"),
        default="auto",
        help="Automatic tries web first, then mobile; forced modes use only one method",
    )
    args = parser.parse_args()
    if args.set and ("=" not in args.set or not all(args.set.split("=", 1))):
        parser.error("--set requires PARAMETER=VALUE")
    username = input("Bitzer account email: ").strip()
    password = getpass.getpass("Password (hidden): ")
    return asyncio.run(check(args, username, password))


if __name__ == "__main__":
    raise SystemExit(main())
