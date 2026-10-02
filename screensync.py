#!/usr/bin/env python3
"""Mirror the dominant screen colour onto a Tuya-based bulb (Havells DigiTap).

    python screensync.py --dry-run      # no bulb needed, shows the colour in the terminal
    python screensync.py                # drives the bulb over Wi-Fi (same network as this Mac)
    python screensync.py --ble          # drives the bulb over Bluetooth, no shared network needed
    python screensync.py --ble --gui    # same, plus a window to tune the settings live
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

import numpy as np

DEFAULTS = {
    "device_id": "",
    "ip": "Auto",            # "Auto" finds the bulb by its LAN broadcast
    "local_key": "",
    "version": 3.3,
    "monitor": 1,            # 1 = main display, 2 = second display
    "fps": 8,                # how often the screen is sampled, per second
    "smoothing": 0.35,       # 0..1, higher = snappier, lower = slower fades
    "min_brightness": 0.08,  # bulb never goes darker than this (0..1)
    "max_brightness": 1.0,
    "saturation_boost": 1.3,
    "mode": "music",         # "music" = DP 27, bulb fades by itself; "colour" = plain DP 24
    "transport": "wifi",     # "wifi" = LAN via tinytuya, "ble" = Bluetooth (same as --ble)
    "uuid": "",              # Tuya uuid of the bulb, only needed for Bluetooth
    "ble_exit_mode": "white",  # what the bulb returns to after a Bluetooth sync: white/colour/keep
    # Fine tuning; --gui changes these (and the ones above) live, for that run only.
    "brightness_gain": 1.6,  # screen brightness is multiplied by this before clipping to the range
    "rgb_gain": [1.0, 1.0, 1.0],  # per-channel tint applied to the picked colour
    "hue_shift": 0,          # degrees added to the hue
    "grey_weight": 0.05,     # how much grey pixels count next to vivid ones
    "rival_weight": 0.5,     # how much hues other than the dominant one dilute it
    "hue_stickiness": 1.25,  # advantage given to the current hue, to stop flip-flopping
    "black_level": 16,       # 8-bit level below which a border row or column counts as a black bar
    "max_bar": 0.25,         # a bar can take at most this share of the frame on each side
    "cut_threshold": 0.3,    # a jump bigger than this counts as a scene cut...
    "cut_speed": 0.7,        # ...and closes this share of the gap in one step
    "hue_step": 2,           # smallest hue change (degrees) worth sending
    "level_step": 15,        # smallest saturation/brightness change (out of 1000) worth sending
    # Weight of each ninth of the picture, rows top to bottom, columns left to right.
    "region_weights": [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
}

DP_SWITCH, DP_MODE, DP_COLOUR, DP_MUSIC = 20, 21, 24, 27
WORK_MODES = ["white", "colour", "scene", "music"]  # enum order used over Bluetooth


def load_config(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


FRAME_WIDTH = 64     # frames are analysed at 64 pixels wide; more adds cost, not accuracy
HUE_BINS = 24

_c = np.arange(256, dtype=np.float32) / 255.0
SRGB_TO_LINEAR = np.where(_c <= 0.04045, _c / 12.92, ((_c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def crop_black_bars(px, cfg=DEFAULTS):
    """Trim letterbox and pillarbox bars so they don't dim the result. px is (h, w, 3) uint8."""
    for axis in (0, 1):
        lit = np.flatnonzero(px.max(axis=(1 - axis, 2)) > cfg["black_level"])
        size = px.shape[axis]
        if lit.size == 0:
            return px
        start = lit[0] if lit[0] <= size * cfg["max_bar"] else 0
        stop = lit[-1] + 1 if size - lit[-1] - 1 <= size * cfg["max_bar"] else size
        px = px[start:stop] if axis == 0 else px[:, start:stop]
    return px


def region_weights(shape, grid):
    """Per-pixel weight from the grid of picture areas, or None when every area counts equally."""
    grid = np.asarray(grid, dtype=np.float32)
    if np.all(grid == 1) or grid.sum() <= 0:
        return None
    rows = np.arange(shape[0]) * grid.shape[0] // shape[0]
    cols = np.arange(shape[1]) * grid.shape[1] // shape[1]
    return grid[rows][:, cols].reshape(-1)


def frame_colour(px, previous_bin=None, cfg=DEFAULTS):
    """Dominant colour of a small RGB frame.

    Returns (rgb floats 0..1, brightness 0..1, hue bin). Pixels are weighted by how vivid
    they are, as before, but only the strongest hue keeps its colour: rival hues are counted
    as grey, so two competing colours give a paler dominant colour rather than a muddy mix.
    """
    cropped = crop_black_bars(px, cfg)
    flat = cropped.reshape(-1, 3)
    area = region_weights(cropped.shape, cfg["region_weights"])
    if area is None:
        brightness = float(flat.mean()) / 255.0
    else:
        brightness = float((flat.mean(axis=1) * area).sum() / max(float(area.sum()), 1e-9)) / 255.0
    rgb = SRGB_TO_LINEAR[flat]  # average in linear light, the way the light itself adds up
    high, low = rgb.max(axis=1), rgb.min(axis=1)
    chroma = high - low
    weight = chroma + cfg["grey_weight"]
    if area is not None:
        chroma, weight = chroma * area, weight * area
    hue_bin = None
    if float(chroma.sum()) > 1e-3 * len(rgb):
        r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
        span = np.where(chroma > 0, chroma, 1)
        hue = np.where(high == r, (g - b) / span % 6, np.where(high == g, (b - r) / span + 2, (r - g) / span + 4))
        bins = (hue * (HUE_BINS / 6)).astype(np.intp) % HUE_BINS
        hist = np.bincount(bins, weights=chroma, minlength=HUE_BINS)
        hist = hist + np.roll(hist, 1) + np.roll(hist, -1)
        if previous_bin is not None:
            hist[previous_bin] *= cfg["hue_stickiness"]  # stick with the current hue unless another clearly wins
        hue_bin = int(hist.argmax())
        rival = np.abs((bins - hue_bin + HUE_BINS // 2) % HUE_BINS - HUE_BINS // 2) > 1
        rgb = np.where(rival[:, None], rgb.mean(axis=1, keepdims=True), rgb)
        weight = np.where(rival, weight * cfg["rival_weight"], weight)
    linear = (rgb * weight[:, None]).sum(axis=0) / max(float(weight.sum()), 1e-9)
    encoded = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)
    return encoded, brightness, hue_bin


class StreamCapture:
    """Small frames pushed by macOS (ScreenCaptureKit).

    The system scales the screen down on the GPU and only delivers a frame when something on
    screen changed, so a static screen costs nothing.
    """

    def __init__(self, monitor, fps):
        import CoreMedia
        import Quartz
        import ScreenCaptureKit as SCK
        from Foundation import NSObject

        capture = self
        self.lock = threading.Lock()
        self.frame = None

        class FrameSink(NSObject):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
                try:
                    buf = CoreMedia.CMSampleBufferGetImageBuffer(sample)
                    if buf is None:  # "nothing changed" notification
                        return
                    Quartz.CVPixelBufferLockBaseAddress(buf, 1)
                    try:
                        w, h = Quartz.CVPixelBufferGetWidth(buf), Quartz.CVPixelBufferGetHeight(buf)
                        stride = Quartz.CVPixelBufferGetBytesPerRow(buf)
                        raw = Quartz.CVPixelBufferGetBaseAddress(buf).as_buffer(stride * h)
                        bgra = np.frombuffer(raw, dtype=np.uint8).reshape(h, stride)[:, : w * 4]
                        frame = bgra.reshape(h, w, 4)[:, :, 2::-1].copy()
                    finally:
                        Quartz.CVPixelBufferUnlockBaseAddress(buf, 1)
                    with capture.lock:
                        capture.frame = frame
                except Exception:
                    pass  # never let an error escape into the system's capture thread

        def wait(start):
            done, result = threading.Event(), []

            def finished(*args):
                result.extend(args)
                done.set()

            start(finished)
            if not done.wait(10):
                raise RuntimeError("screen capture did not respond")
            return result

        content, error = wait(SCK.SCShareableContent.getShareableContentWithCompletionHandler_)
        if error is not None or not content.displays():
            raise RuntimeError("screen recording is not permitted")
        _, ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
        wanted = ids[monitor - 1] if 1 <= monitor <= count else ids[0]
        display = next((d for d in content.displays() if d.displayID() == wanted), content.displays()[0])

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(FRAME_WIDTH)
        config.setHeight_(max(1, round(FRAME_WIDTH * display.height() / display.width())))
        config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, int(fps)))
        config.setPixelFormat_(int.from_bytes(b"BGRA", "big"))
        config.setShowsCursor_(False)
        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        self.sink = FrameSink.alloc().init()
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(content_filter, config, None)
        ok, error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self.sink, SCK.SCStreamOutputTypeScreen, None, None
        )
        if not ok:
            raise RuntimeError(f"could not attach to the screen stream: {error}")
        (error,) = wait(self.stream.startCaptureWithCompletionHandler_)
        if error is not None:
            raise RuntimeError(f"could not start the screen stream: {error}")

    def latest(self):
        """Newest frame since the last call, or None if the screen has not changed."""
        with self.lock:
            frame, self.frame = self.frame, None
        return frame

    def close(self):
        done = threading.Event()
        self.stream.stopCaptureWithCompletionHandler_(lambda error: done.set() and None)
        done.wait(3)


class GrabCapture:
    """Fallback: full-screen grabs through mss, sampled down to the same small grid."""

    def __init__(self, monitor, fps):
        import mss

        self.sct = mss.MSS()
        self.monitor = self.sct.monitors[monitor]
        self.previous = None

    def latest(self):
        shot = self.sct.grab(self.monitor)
        step = max(1, shot.width // FRAME_WIDTH)
        px = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        frame = px[step // 2::step, step // 2::step, 2::-1]
        if self.previous is not None and np.array_equal(frame, self.previous):
            return None
        self.previous = frame.copy()
        return self.previous

    def close(self):
        self.sct.close()


def open_capture(cfg):
    try:
        return StreamCapture(cfg["monitor"], cfg["fps"])
    except Exception as exc:
        print(f"Low-power screen capture unavailable ({exc}); using full-screen grabs instead.")
        return GrabCapture(cfg["monitor"], cfg["fps"])


def to_hsv(colour, luma, cfg):
    h, s, _ = colorsys.rgb_to_hsv(*np.clip(np.asarray(colour) * cfg["rgb_gain"], 0, 1))
    h = (h + cfg["hue_shift"] / 360) % 1
    s = min(1.0, s * cfg["saturation_boost"])
    lo, hi = cfg["min_brightness"], cfg["max_brightness"]
    v = lo + (hi - lo) * min(1.0, luma * cfg["brightness_gain"])
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
    ap.add_argument("--gui", action="store_true", help="open a window to tune the settings live (this run only)")
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

    stop = threading.Event()
    if not args.gui:  # the tuning window installs its own handlers once it exists
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # The tuning window can raise the sampling rate, so let frames arrive at its maximum.
    capture = open_capture({**cfg, "fps": 20} if args.gui else cfg)
    try:
        if args.gui:
            import tuner

            shown = {}
            worker = threading.Thread(
                target=run_sync, args=(cfg, out, capture, stop, lambda hsv: shown.update(hsv=hsv)), daemon=True
            )
            worker.start()
            tuner.run(cfg, DEFAULTS, shown, stop)  # returns when the window is closed
            stop.set()
            worker.join(5)
        else:
            run_sync(cfg, out, capture, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        out.close()
        capture.close()


def run_sync(cfg, out, capture, stop, on_colour=None):
    """Follow the screen until `stop` is set. Settings are re-read from cfg on every step."""
    target = smooth = last_sent = hue_bin = last_frame = None
    revision = cfg.get("_revision")
    while not stop.is_set():
        t0 = time.time()
        period = 1.0 / cfg["fps"]
        frame = capture.latest()
        if frame is None and cfg.get("_revision") != revision:
            frame = last_frame  # a setting changed in the tuning window: redo the current picture
        revision = cfg.get("_revision")
        if frame is not None:
            last_frame = frame
            colour, luma, hue_bin = frame_colour(frame, hue_bin, cfg)
            target = np.append(colour, luma)
        elif smooth is not None and float(np.abs(target - smooth).max()) < 0.002:
            # Static screen and the fade has finished: nothing to compute or send.
            out.idle()
            stop.wait(period)
            continue
        if target is not None:
            if smooth is None:
                smooth = target
            else:
                # "smoothing" is the share of the gap closed per step; a scene cut closes it faster.
                cut = float(np.abs(target - smooth).max()) > cfg["cut_threshold"]
                smooth = smooth + max(cfg["smoothing"], cfg["cut_speed"] if cut else 0.0) * (target - smooth)
            hsv = to_hsv(smooth[:3], float(smooth[3]), cfg)
            if on_colour:
                on_colour(hsv)
            # Skip sends when nothing visibly changed (hue wraps at 360).
            if last_sent is None or (
                min(abs(hsv[0] - last_sent[0]), 360 - abs(hsv[0] - last_sent[0])) > cfg["hue_step"]
                or abs(hsv[1] - last_sent[1]) > cfg["level_step"]
                or abs(hsv[2] - last_sent[2]) > cfg["level_step"]
            ):
                out.send(*hsv)
                last_sent = hsv
            else:
                out.idle()
        stop.wait(max(0.0, period - (time.time() - t0)))


if __name__ == "__main__":
    main()
