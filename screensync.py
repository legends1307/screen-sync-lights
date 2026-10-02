#!/usr/bin/env python3
"""Mirror the dominant screen colour onto a Tuya-based bulb (Havells DigiTap) over the LAN.

    python screensync.py --dry-run      # no bulb needed, shows the colour in the terminal
    python screensync.py                # drives the bulb described in config.json
"""
import argparse
import colorsys
import json
import signal
import sys
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
}

DP_MODE, DP_COLOUR, DP_MUSIC = 21, 24, 27


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
        self.dev = tinytuya.BulbDevice(
            cfg["device_id"], cfg["ip"], cfg["local_key"], version=float(cfg["version"])
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
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.dry_run:
        out = Preview()
    else:
        if not (cfg["device_id"] and cfg["local_key"]):
            sys.exit(f"Fill in device_id and local_key in {args.config} (or use --dry-run).")
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
