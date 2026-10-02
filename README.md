# screen-sync-lights

Mirrors the dominant colour of the Mac's screen onto a Tuya-based smart bulb
(tested with a Havells TW+RGB bulb) over the local network, with smooth fades.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Fill in `device_id` and `local_key` in `config.json`. Both come from a Tuya
developer project (platform.tuya.com) with the bulb's app account linked:
Cloud → API Explorer → Device Management → Query Device Details.

Notes:

- Branded apps such as Havells DigiTap cannot be linked to a Tuya developer
  project. Pair the bulb in the Smart Life app instead and link that account.
- Re-pairing the bulb changes its local key.
- The Mac and the bulb must be on the same local network.

## Run

```bash
.venv/bin/python screensync.py --dry-run   # preview the colour in the terminal, no bulb
.venv/bin/python screensync.py             # drive the bulb; Ctrl-C restores its previous state
```

macOS needs Screen Recording permission for the app the script is launched from,
otherwise only the wallpaper is captured.

## Config

| Key | Meaning |
|---|---|
| `ip` | Bulb address, or `"Auto"` to find it by broadcast |
| `version` | Tuya protocol version the bulb speaks (3.3, 3.4, 3.5) |
| `monitor` | 1 = main display, 2 = second display, 0 = all |
| `fps` | Colour updates per second |
| `smoothing` | 0..1, higher is snappier |
| `min_brightness` / `max_brightness` | Brightness limits, 0..1 |
| `saturation_boost` | Multiplier on colour saturation |
| `mode` | `"music"` (bulb fades by itself) or `"colour"` |
