"""Small always-on-top window for tuning screensync live (python screensync.py --gui).

Every control writes straight into the running settings; nothing is saved to config.json.
"""
import colorsys
import copy
import json
import signal
import tkinter as tk
from tkinter import ttk

# (settings key, label, from, to, step, what the number means)
TABS = {
    "Brightness": [
        ("min_brightness", "Minimum brightness", 0.0, 1.0, 0.01,
         "The dimmest the bulb ever goes, as a share of full power. 0 lets it go almost dark on a "
         "black screen; 0.08 (default) keeps a faint glow; 0.3 never drops below 30%."),
        ("max_brightness", "Maximum brightness", 0.0, 1.0, 0.01,
         "The brightest the bulb ever goes. 1 (default) is full power; 0.5 caps it at half, useful "
         "at night. The screen's brightness is mapped between the minimum and this."),
        ("brightness_gain", "Brightness gain", 0.5, 4.0, 0.05,
         "The screen's average brightness (0 to 1) is multiplied by this. At 1.6 (default) a "
         "screen that is 62% bright already gives maximum. Higher = bright sooner, so dark films "
         "look livelier; 1 = follow the screen exactly."),
    ],
    "Colour": [
        ("saturation_boost", "Saturation boost", 0.0, 3.0, 0.05,
         "Multiplies how vivid the colour is. 1 = as on screen, 1.3 (default) = 30% more vivid, "
         "0 = always white light, 2+ = nearly everything fully saturated."),
        ("hue_shift", "Hue shift (degrees)", -180, 180, 1,
         "Rotates the colour around the colour wheel. 0 (default) = same colour as the screen; "
         "+120 turns red into green; 180 gives the opposite colour. Use small values to correct "
         "a bulb whose colours look off."),
        ("rgb_gain.0", "Red gain", 0.0, 2.0, 0.02,
         "Strength of red in the picked colour. 1 (default) = unchanged, 0.5 = half the red, "
         "above 1 = warmer, redder light."),
        ("rgb_gain.1", "Green gain", 0.0, 2.0, 0.02,
         "Strength of green in the picked colour. 1 (default) = unchanged. Lower it if the bulb "
         "looks too green."),
        ("rgb_gain.2", "Blue gain", 0.0, 2.0, 0.02,
         "Strength of blue in the picked colour. 1 (default) = unchanged. Lower it for a warmer, "
         "easier-on-the-eyes light."),
        ("grey_weight", "Grey pixels count", 0.0, 1.0, 0.01,
         "How much grey, white and black pixels count next to colourful ones. At 0.05 (default) "
         "a vivid pixel counts about 20 times more than a grey one, so a small colourful object "
         "can tint the light. 0 = only colourful pixels matter; 1 = close to a plain average, "
         "so pale scenes give pale light."),
        ("rival_weight", "Other colours dilute the main one", 0.0, 1.0, 0.05,
         "The light follows the strongest colour on screen. This sets how much the other colours "
         "wash it out. 0 = ignore them (always a pure colour); 0.5 (default) = a half-red, "
         "half-blue screen gives a paler red or blue; 1 = strongest dilution."),
        ("hue_stickiness", "Stay on the current colour", 1.0, 3.0, 0.05,
         "Advantage given to the colour the bulb already shows when two colours compete. "
         "1 = switch the moment another colour is slightly ahead (can flicker); 1.25 (default) = "
         "the new colour must be 25% stronger; 2 = it must be twice as strong."),
    ],
    "Speed": [
        ("smoothing", "Fade speed", 0.02, 1.0, 0.01,
         "Share of the gap to the new colour closed at each step. 0.35 (default) = 35% per step, "
         "a fade of under a second at 8 samples a second; 1 = jump straight to the new colour; "
         "0.05 = slow drift over several seconds."),
        ("cut_threshold", "Scene-cut sensitivity", 0.05, 1.0, 0.01,
         "How big a change counts as a scene cut (0 to 1, where 1 is black to white). At 0.3 "
         "(default) a 30% jump triggers the fast response below. Lower = more changes treated "
         "as cuts; 1 = never."),
        ("cut_speed", "Speed on a scene cut", 0.02, 1.0, 0.01,
         "Share of the gap closed in one step when a cut is detected. 0.7 (default) = nearly "
         "instant; set it equal to Fade speed to treat cuts like any other change."),
        ("fps", "Screen samples per second", 1, 20, 1,
         "How many times a second the screen is checked. 8 is the default. Over Bluetooth the "
         "bulb accepts about 5 changes a second, so more than 8 mostly adds CPU use; lower "
         "values feel laggier."),
        ("hue_step", "Ignore small hue changes (degrees)", 0, 30, 1,
         "The bulb is only updated when the hue moved more than this many degrees. 2 (default) "
         "filters invisible changes; higher = fewer updates, steadier light; 0 = send everything."),
        ("level_step", "Ignore small brightness changes", 0, 200, 1,
         "The bulb is only updated when brightness or saturation moved more than this, on a "
         "0 to 1000 scale. 15 (default) is 1.5%; 100 = only react to 10% changes."),
    ],
    "Black bars": [
        ("black_level", "Black level", 0, 80, 1,
         "A row or column at the edge counts as a black bar if every pixel in it is darker than "
         "this (0 = pure black, 255 = white). 16 (default) tolerates slightly grey bars; raise "
         "it if bars are not being detected."),
        ("max_bar", "Largest bar on each side", 0.0, 0.45, 0.01,
         "Bars are cut off so they do not dim the light, but only up to this share of the "
         "picture on each side. 0.25 (default) = up to a quarter; 0 = never crop."),
    ],
}
AREA_NAMES = [["Top left", "Top", "Top right"], ["Left", "Centre", "Right"], ["Bottom left", "Bottom", "Bottom right"]]
AREA_HELP = (
    "The picture is split into nine areas, laid out here as on screen. Each number is how much "
    "that area counts: 1 = normal, 0 = ignored, 2 = counts double, 4 = four times. For example, "
    "set the bottom row to 0 to ignore subtitles, or raise Centre to follow the main subject."
)
START_HELP = "Hover over or click a ? to see what a setting means. Changes apply at once and last for this run only."


def _get(cfg, key):
    name, _, index = key.partition(".")
    return cfg[name][int(index)] if index else cfg[name]


def _set(cfg, key, value):
    name, _, index = key.partition(".")
    if index:
        values = list(cfg[name])
        values[int(index)] = value
        cfg[name] = values  # swap the whole list so the sync thread never sees a half-edit
    else:
        cfg[name] = value
    cfg["_revision"] = cfg.get("_revision", 0) + 1


def run(cfg, defaults, shown, stop):
    """Show the window and block until it is closed or `stop` is set.

    `shown["hsv"]` is the colour being sent.
    """
    initial = copy.deepcopy(cfg)
    root = tk.Tk()
    # Tk installs its own handlers when it starts, so ours must go in afterwards. They only
    # raise a flag: the window polls it and closes itself, and the caller then cleans up.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    root.title("Screen-bulb sync tuner")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    help_text = tk.StringVar(value=START_HELP)
    sliders = {}

    def helper(parent, text):
        mark = tk.Label(parent, text="?", width=2, relief="groove", cursor="question_arrow")
        mark.bind("<Enter>", lambda _: help_text.set(text))
        mark.bind("<Button-1>", lambda _: help_text.set(text))
        return mark

    for title, rows in TABS.items():
        page = ttk.Frame(tabs, padding=6)
        tabs.add(page, text=title)
        for line, (key, label, lo, hi, step, meaning) in enumerate(rows):
            integer = isinstance(step, int)
            # A traced variable fires for drags and for the Defaults / Undo buttons alike.
            var = tk.DoubleVar(value=_get(cfg, key))
            var.trace_add(
                "write",
                lambda *_, key=key, var=var, integer=integer: _set(
                    cfg, key, int(var.get()) if integer else float(var.get())
                ),
            )
            sliders[key] = var
            scale = tk.Scale(page, label=label, from_=lo, to=hi, resolution=step, orient="horizontal",
                             length=300, variable=var)
            scale.grid(row=line, column=0, sticky="w")
            scale.bind("<Button-1>", lambda _, meaning=meaning: help_text.set(meaning), add="+")
            helper(page, meaning).grid(row=line, column=1, padx=(6, 0), sticky="s", pady=6)

    # Screen areas: nine sliders laid out like the picture itself.
    page = ttk.Frame(tabs, padding=6)
    tabs.add(page, text="Screen areas")
    ttk.Label(page, text="How much each part of the picture counts").grid(row=0, column=0, columnspan=2, sticky="w")
    helper(page, AREA_HELP).grid(row=0, column=2, sticky="e")

    def area_slider(r, c):
        def changed(*_):
            grid = [list(line) for line in cfg["region_weights"]]
            grid[r][c] = float(var.get())
            cfg["region_weights"] = grid
            cfg["_revision"] = cfg.get("_revision", 0) + 1

        var = tk.DoubleVar(value=cfg["region_weights"][r][c])
        var.trace_add("write", changed)
        sliders[f"area.{r}.{c}"] = var
        scale = tk.Scale(page, label=AREA_NAMES[r][c], from_=0.0, to=4.0, resolution=0.1, orient="horizontal",
                         length=110, variable=var)
        scale.grid(row=r + 1, column=c, padx=2, pady=2)
        scale.bind("<Button-1>", lambda _: help_text.set(AREA_HELP), add="+")

    for r in range(3):
        for c in range(3):
            area_slider(r, c)

    def load(values):
        for key, var in sliders.items():
            if key.startswith("area."):
                _, r, c = key.split(".")
                var.set(values["region_weights"][int(r)][int(c)])
            else:
                var.set(_get(values, key))

    def tuned():
        names = {key.partition(".")[0] for rows in TABS.values() for key, *_ in rows} | {"region_weights"}
        return {name: cfg[name] for name in sorted(names)}

    tk.Label(root, textvariable=help_text, wraplength=560, justify="left", anchor="nw", height=4).pack(
        fill="x", padx=10)
    bar = ttk.Frame(root, padding=(8, 0, 8, 8))
    bar.pack(fill="x")
    swatch = tk.Label(bar, width=6, relief="sunken")
    swatch.pack(side="left")
    readout = ttk.Label(bar, text="", width=24)
    readout.pack(side="left", padx=6)
    ttk.Button(bar, text="Print", command=lambda: print("\n" + json.dumps(tuned()))).pack(side="right")
    ttk.Button(bar, text="Defaults", command=lambda: load(defaults)).pack(side="right")
    ttk.Button(bar, text="Undo all", command=lambda: load(initial)).pack(side="right")

    def refresh():
        if stop.is_set():  # Ctrl-C in the terminal
            root.destroy()
            return
        hsv = shown.get("hsv")
        if hsv:
            h, s, v = hsv
            r, g, b = (int(x * 255) for x in colorsys.hsv_to_rgb(h / 360, s / 1000, v / 1000))
            swatch.configure(background=f"#{r:02x}{g:02x}{b:02x}")
            readout.configure(text=f"hue {h}°  sat {s // 10}%  bright {v // 10}%")
        root.after(150, refresh)

    refresh()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
