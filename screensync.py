#!/usr/bin/env python3
"""Mirror the dominant screen colour onto a Tuya-based bulb (Havells DigiTap).

    python screensync.py --dry-run      # no bulb needed, shows the colour in the terminal
    python screensync.py                # drives the bulb over Wi-Fi (same network as this Mac)
    python screensync.py --ble          # drives the bulb over Bluetooth, no shared network needed
"""
import argparse
import asyncio
import colorsys
import json
import signal
import sys
import threading
import time
from pathlib import Path

import mss
import numpy as np

DEFAULTS = {
    "device_id": "",
    "ip": "Auto",            # "Auto" finds the bulb by its LAN broadcast
    "local_key": "",
    "version": 3.3,
    "monitor": 1,            # 1 = main display, 2 = second display, 0 = all displays
    "fps": 8,                # colour updates per second sent to the bulb
    "smoothing": 0.35,       # 0..1, higher = snappier, lower = slower fades
    "min_brightness": 0.08,  # bulb never goes darker than this (0..1)
    "max_brightness": 1.0,
    "saturation_boost": 1.3,
    "mode": "music",         # "music" = DP 27, bulb fades by itself; "colour" = plain DP 24
    "transport": "wifi",     # "wifi" = LAN via tinytuya, "ble" = Bluetooth (same as --ble)
    "uuid": "",              # Tuya uuid of the bulb, only needed for Bluetooth
    "ble_exit_mode": "white",  # what the bulb returns to after a Bluetooth sync: white/colour/keep
}

DP_SWITCH, DP_MODE, DP_COLOUR, DP_MUSIC = 20, 21, 24, 27
WORK_MODES = ["white", "colour", "scene", "music"]  # enum order used over Bluetooth


def load_config(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


def screen_colour(sct, monitor, step=24):
    """Saturation-weighted mean colour of the screen, as linear-ish RGB floats 0..1."""
    shot = sct.grab(monitor)
    px = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
    rgb = px[::step, ::step, 2::-1].reshape(-1, 3).astype(np.float32) / 255.0
    # Vivid pixels count more than grey ones, otherwise everything averages to mud.
    weight = (rgb.max(axis=1) - rgb.min(axis=1)) + 0.05
    colour = (rgb * weight[:, None]).sum(axis=0) / weight.sum()
    # Overall brightness follows the plain mean so dark scenes stay dark.
    return colour, float(rgb.mean())


def to_hsv(colour, luma, cfg):
    h, s, _ = colorsys.rgb_to_hsv(*np.clip(colour, 0, 1))
    s = min(1.0, s * cfg["saturation_boost"])
    lo, hi = cfg["min_brightness"], cfg["max_brightness"]
    v = lo + (hi - lo) * min(1.0, luma * 1.6)
    return int(h * 360), int(s * 1000), int(v * 1000)


class Bulb:
    def __init__(self, cfg):
        import tinytuya

        self.mode = cfg["mode"]
        try:
            self.dev = tinytuya.BulbDevice(
                cfg["device_id"], cfg["ip"], cfg["local_key"], version=float(cfg["version"])
            )
        except RuntimeError:
            sys.exit(
                "Bulb not found on this network. Check that it is switched on, shows online "
                "in Smart Life, and is on the same Wi-Fi as this Mac."
            )
        self.dev.set_socketPersistent(True)
        # The first connection attempt often fails, so try a few times.
        for _ in range(5):
            status = self.dev.status() or {}
            if "dps" in status:
                break
            time.sleep(1)
        else:
            sys.exit(f"Could not reach the bulb: {status}")
        self.original = status["dps"]
        print("Bulb status:", self.original)
        self.dev.set_multiple_values(
            {"20": True, str(DP_MODE): "music" if self.mode == "music" else "colour"}
        )
        self.last_beat = time.time()

    def close(self):
        """Put the bulb back the way it was before the sync started."""
        restore = {k: self.original[k] for k in ("20", "21", "24") if k in self.original}
        self.dev.set_multiple_values(restore)

    def send(self, h, s, v):
        hsv = f"{h:04x}{s:04x}{v:04x}"
        if self.mode == "music":
            # 1 = gradient (bulb fades to the colour), then HSV, then white brightness/temp = 0
            self.dev.set_value(DP_MUSIC, "1" + hsv + "00000000", nowait=True)
        else:
            self.dev.set_value(DP_COLOUR, hsv, nowait=True)
        self.last_beat = time.time()

    def idle(self):
        if time.time() - self.last_beat > 8:
            self.dev.heartbeat(nowait=True)
            self.last_beat = time.time()


class BleBulb:
    """Drives the bulb over Bluetooth LE from a background thread; reconnects if the link drops."""

    def __init__(self, cfg):
        if cfg["ble_exit_mode"] not in ("white", "colour", "keep"):
            sys.exit('ble_exit_mode must be "white", "colour" or "keep".')
        self.cfg = cfg
        self.lock = threading.Lock()
        self.latest = None  # newest colour not yet sent; older ones are simply dropped
        self.current = None  # last colour asked for, re-sent after a reconnect
        self.error = "no Tuya Bluetooth signal seen"
        self.stopping = False
        self.ready = threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self.thread.start()
        print("Looking for the bulb over Bluetooth...")
        if not self.ready.wait(60):
            sys.exit(
                f"Could not connect to the bulb over Bluetooth ({self.error}). Check that it is "
                "switched on and within a few metres, and that this terminal is allowed to use "
                "Bluetooth in System Settings > Privacy & Security."
            )
        print("Connected over Bluetooth.")

    def _mode(self, name):
        import tuya_ble

        return tuya_ble.dp(DP_MODE, tuya_ble.DP_ENUM, bytes([WORK_MODES.index(name)]))

    async def _run(self):
        import tuya_ble

        music = self.cfg["mode"] == "music"
        while not self.stopping:
            try:
                bulb = await tuya_ble.connect(
                    self.cfg["uuid"], self.cfg["device_id"], self.cfg["local_key"]
                )
            except Exception as exc:
                self.error = str(exc)
                if self.ready.is_set():
                    print(f"\nBluetooth: {exc}; retrying")
                await asyncio.sleep(2)
                continue
            try:
                await bulb.write_dps(
                    tuya_ble.dp(DP_SWITCH, tuya_ble.DP_BOOL, b"\x01")
                    + self._mode("music" if music else "colour")
                )
                self.ready.set()
                with self.lock:
                    self.latest = self.current
                while bulb.connected and not self.stopping:
                    with self.lock:
                        colour, self.latest = self.latest, None
                    if not colour:
                        await asyncio.sleep(0.02)
                        continue
                    hsv = "%04x%04x%04x" % colour
                    if music:
                        payload = tuya_ble.dp(DP_MUSIC, tuya_ble.DP_STRING, f"1{hsv}00000000".encode())
                    else:
                        payload = tuya_ble.dp(DP_COLOUR, tuya_ble.DP_STRING, hsv.encode())
                    # Wait for the bulb's acknowledgement before sending the next colour: it
                    # handles about 5 updates a second, and anything faster just queues up as lag.
                    if not await bulb.write_dps(payload, timeout=0.6):
                        with self.lock:
                            self.latest = self.latest or colour  # retry unless a newer one came
                if not self.stopping:
                    print("\nBluetooth link dropped; reconnecting")
                elif bulb.connected and self.cfg["ble_exit_mode"] != "keep":
                    await bulb.write_dps(self._mode(self.cfg["ble_exit_mode"]))
            except Exception as exc:
                print(f"\nBluetooth: {exc}; reconnecting")
            finally:
                try:
                    await bulb.disconnect()
                except Exception:
                    pass

    def send(self, h, s, v):
        with self.lock:
            self.latest = self.current = (h, s, v)

    def idle(self):
        pass

    def close(self):
        self.stopping = True
        self.thread.join(10)
        print()


class Preview:
    def send(self, h, s, v):
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(h / 360, s / 1000, v / 1000))
        sys.stdout.write(f"\r\x1b[48;2;{r};{g};{b}m{' ' * 24}\x1b[0m  h={h:3d} s={s:4d} v={v:4d} ")
        sys.stdout.flush()

    def idle(self):
        pass

    def close(self):
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    ap.add_argument("--dry-run", action="store_true", help="preview in the terminal, no bulb")
    ap.add_argument("--ble", action="store_true", help="talk to the bulb over Bluetooth instead of Wi-Fi")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.ble:
        cfg["transport"] = "ble"

    if args.dry_run:
        out = Preview()
    else:
        if not (cfg["device_id"] and cfg["local_key"]):
            sys.exit(f"Fill in device_id and local_key in {args.config} (or use --dry-run).")
        if cfg["transport"] == "ble":
            if not cfg["uuid"]:
                sys.exit(f"Bluetooth needs the bulb's uuid in {args.config}.")
            out = BleBulb(cfg)
        else:
            out = Bulb(cfg)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    period = 1.0 / cfg["fps"]
    smooth, last_sent = None, None
    with mss.MSS() as sct:
        monitor = sct.monitors[cfg["monitor"]]
        try:
            while True:
                t0 = time.time()
                colour, luma = screen_colour(sct, monitor)
                target = np.append(colour, luma)
                smooth = target if smooth is None else smooth + cfg["smoothing"] * (target - smooth)
                hsv = to_hsv(smooth[:3], float(smooth[3]), cfg)
                # Skip sends when nothing visibly changed (hue wraps at 360).
                if last_sent is None or (
                    min(abs(hsv[0] - last_sent[0]), 360 - abs(hsv[0] - last_sent[0])) > 2
                    or abs(hsv[1] - last_sent[1]) > 15
                    or abs(hsv[2] - last_sent[2]) > 15
                ):
                    out.send(*hsv)
                    last_sent = hsv
                else:
                    out.idle()
                time.sleep(max(0.0, period - (time.time() - t0)))
        except KeyboardInterrupt:
            pass
        finally:
            out.close()


if __name__ == "__main__":
    main()
