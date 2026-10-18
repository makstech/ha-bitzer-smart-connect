<p align="center">
  <img src="https://raw.githubusercontent.com/makstech/ha-bitzer-smart-connect/main/custom_components/bitzer_smart_connect/brand/logo.png" alt="Bitzer Smart Connect" width="520">
</p>

<h3 align="center">🌬️ Bitzer Smart Connect for Home Assistant</h3>

<p align="center">
Bring your <b>Ensy InoVent</b> heat-recovery ventilation — and other <b>Bitzer Smart Connect</b> (Lodam LMC311) HVAC devices — into <a href="https://www.home-assistant.io/">Home Assistant</a>. 🏡
</p>

<p align="center">
<a href="https://hacs.xyz/"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
<a href="https://github.com/makstech/ha-bitzer-smart-connect/actions/workflows/validate.yml"><img src="https://github.com/makstech/ha-bitzer-smart-connect/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
</p>

> ℹ️ **Unofficial** — an independent, community integration, not affiliated with or endorsed by BITZER, Lodam, or Ensy. It works by replaying the vendor mobile and web clients' cloud calls — a reverse-engineered client, not a documented API — so it can break if the platform changes. Use at your own risk.

Sign in with your existing Bitzer Smart Connect account and your ventilation unit shows up in Home Assistant with live, native entities — ready for dashboards, history, and automations.

## 🧭 Why this exists

If you own an Ensy InoVent, you may have noticed the app quietly stopped controlling it. Ensy moved these units onto the Bitzer Smart Connect platform, but the old Ensy app and its cloud (`app.ensy.no`) were left pointing at the previous backend — it still shows stale readings, yet commands go nowhere.

The earlier [_makstech/ensy-home-assistant_](https://github.com/makstech/ensy-home-assistant) integration talked to that same old cloud, so it stopped working too. This project talks to the backend the units actually use now — and it's built generically, so it isn't tied to Ensy: any device on the Bitzer Smart Connect / Lodam platform should work.

## ✨ Features

- 🌡️ **Climate control** — target temperature and fan speed in one card.
- 📈 **Live sensors** — supply, extract, outdoor and other temperatures, plus **humidity** and **CO₂**.
- 🔔 **Alarms & status** — filter-change and fault alerts, using the device's own readable labels.
- ⚡ **Real-time** — changes appear within a few seconds over the cloud's live connection, backed by a gentle refresh so state stays in sync.
- 🎛️ **Per-model profiles** — sensible fan/temperature ranges for known units (like the Ensy InoVent), with a generic fallback for everything else.
- 🔄 **Reauth** — if your password changes, Home Assistant just asks you to sign in again.

## 📦 Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=makstech&repository=ha-bitzer-smart-connect&category=integration)

Click the badge above, or add it manually:

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/makstech/ha-bitzer-smart-connect` with category **Integration**.
3. Search for **Bitzer Smart Connect**, download, and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration → Bitzer Smart Connect**.

## ⚙️ Configuration

1. Enter your **Bitzer Smart Connect email and password**.
2. Leave **Automatic** selected to try web login first, then mobile login if it fails. You can also force **Mobile app** or **Web** to use only that method.
3. Pick your device from the list, or enter its numeric device ID if discovery is empty. Setup checks that its configuration can be read before adding it.

Existing installations keep their web connection until changed. Open the integration entry's menu → **Reconfigure** to switch access mode without removing the device or its entities. Reauthentication also offers the mode selector. Automatic mode keeps using the successful method for token renewal, with the other method available if login later fails. It does not retry a failed or unconfirmed device write using another method.

Mobile mode uses the mobile app's OAuth registration with authorization code + PKCE and refresh tokens. Home Assistant connects directly to Bitzer; your phone and the mobile app do not need to stay open.

Add the integration again for each additional device.

## 🎛️ Options

There are settings worth knowing about — and they live in an easy-to-miss spot. Open **Settings → Devices & Services → Bitzer Smart Connect → ⚙️ Configure**:

- **Fan / temperature limit overrides** — if your unit's real range is narrower than the controller's raw range, set the exact min/max/step here.
- **Poll interval** — how often the background refresh runs (the live connection still delivers instant updates).
- **Expose all parameters** — also create entities for every configuration parameter the controller has. Off by default; they arrive disabled, enable the ones you want.

Prefer to fix your model's limits for *everyone*? Add a device profile:

1. Find your **product ID** — the GUID in the app's `…/api/Localization/ProductStrings/<id>` request (also reported as `productId`).
2. Add an entry keyed by it to `custom_components/bitzer_smart_connect/profiles.py`.

Pull requests with new device profiles are very welcome. 🙌

## 🔧 How it works

- **Sign-in** uses the selected mobile or web OAuth client with your Bitzer account.
- **Live updates** arrive over the platform's SignalR WebSocket — whether the change came from you, another app, or the unit itself — within a few seconds.
- **Background refresh** re-reads the full parameter set on a gentle interval as a safety net, even while the live connection is healthy.
- **Mobile commands** use the cloud parameter API, one parameter at a time. Writes are serialized. Each request allows up to 75 seconds for the cloud response (the server can wait around 60 seconds for a gateway reply), followed by up to 20 seconds of reported-value checks per parameter. A timeout can mean the command is still pending; it does not roll back earlier parameters in a multi-parameter action.
- **Web commands** retain the original batched configuration form endpoint.
- **Mobile live updates** subscribe to device changes without holding a configuration view open.

### Test cloud access locally

With Python 3.12+ and `aiohttp` installed, run:

```powershell
python -m pip install aiohttp
python scripts/check_mobile_connection.py
```

The script prompts for your email and hidden password, keeps credentials in memory, and checks login, discovery, and configuration reads. It defaults to Automatic; pass `--access-mode web` or `--access-mode mobile` to force one method. It does not change settings by default. To test a control change, specify one device and a supported parameter explicitly:

```powershell
python scripts/check_mobile_connection.py --device 1234 --set _USER.Control.TempSet=21.5
```

Use your own device ID and desired temperature; a successful write prints the confirmed readback. Mobile protocol details were identified by [ensy-homey](https://github.com/Gawdfrey/ensy-homey/blob/2e1f63f/lib/bitzer-client.ts).

## ⚠️ Limitations & privacy

- **Cloud-dependent** — there's no local/offline mode; if the cloud is unreachable, so is this.
- **Unofficial API** — reverse-engineered from the web app, not a published one; it can change without notice.
- **Your login is stored** encrypted by Home Assistant and sent only to `bitzersmartconnect.com`.
- **Command lag** — expect a few seconds (~3–8s) before a change takes effect; the cloud confirms it.
- **Your own account and devices** — please don't use it to manage others' equipment or hammer the servers.

## 🤝 Contributing

Issues and pull requests are welcome — especially reports and device profiles from other Bitzer Smart Connect / Lodam devices beyond the Ensy InoVent this was built against.

## Trademarks

"Bitzer Smart Connect", "Ensy", "InoVent" and related names are trademarks of their respective owners, used here only to describe what this integration connects to.

## License

MIT — see [LICENSE](LICENSE).
