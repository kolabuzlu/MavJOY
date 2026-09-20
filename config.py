"""
Configuration handling.

The default map assumes a Logitech F710 with the rear slider in the **X**
(XInput) position. SDL reports that layout as:

    axis 0 = left stick X      button 0 = A      button 6 = Back
    axis 1 = left stick Y      button 1 = B      button 7 = Start
    axis 2 = left trigger      button 2 = X      button 8 = left stick click
    axis 3 = right stick X     button 3 = Y      button 9 = right stick click
    axis 4 = right stick Y     button 4 = LB     button 10 = Guide
    axis 5 = right trigger     button 5 = RB     hat 0     = d-pad

These numbers depend on which backend SDL picks. gamepad.py sets
SDL_JOYSTICK_RAWINPUT=0, which pins it to the XInput/DirectInput path and
gives the order above; SDL's RawInput backend numbers them differently
(both sticks first, both triggers last). Sanity check: untouched, axes 2
and 5 read -1.0 and the rest read 0.0. Run `python inputs.py` to see what
your pad actually reports.

In D (DirectInput) mode the two triggers share a single axis, which makes
a ratcheting throttle impossible. Use X mode. The Inputs tab shows live
axis and button numbers so you can confirm what your pad actually reports.
"""

from __future__ import annotations

import copy
import json
import os

import crsf

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "port": "",
    "baud": 400000,
    "rate_hz": 250,
    "theme": "dark",
    "rate_auto": True,    # follow the rate the module asks for in its sync frames
    "sync_byte": 0xC8,
    "gamepad_index": 0,
    "deadzone": 0.05,
    "throttle": {
        "mode": "ramp",
        "axis": 5,            # right trigger = throttle up
        "axis_down": 2,       # left trigger  = throttle down
        "up_source": "axis",
        "down_source": "axis",
        "ramp_rate": 0.6,     # full travel in ~1.7 s
        "cut_button": 6,      # Back = instant idle
        "deadzone": 0.06,
        "reverse": True,
    },
    "channels": [
        # Nothing is mapped by default. Gamepads differ, and a map that
        # guessed wrong could put arm or throttle on the wrong control,
        # so every channel starts at none and you build it in the
        # Channels tab. The hints below say what usually goes where.
        {"src": "none", "idx": 0, "inv": False},   # 1
        {"src": "none", "idx": 0, "inv": False},   # 2
        {"src": "none", "idx": 0, "inv": False},   # 3
        {"src": "none", "idx": 0, "inv": False},   # 4
        {"src": "none", "idx": 0, "inv": False},   # 5
        {"src": "none", "idx": 0, "inv": False},   # 6
        {"src": "none", "idx": 0, "inv": False},   # 7
        {"src": "none", "idx": 0, "inv": False},   # 8
        {"src": "none", "idx": 0, "inv": False},   # 9
        {"src": "none", "idx": 0, "inv": False},   # 10
        {"src": "none", "idx": 0, "inv": False},   # 11
        {"src": "none", "idx": 0, "inv": False},   # 12
        {"src": "none", "idx": 0, "inv": False},   # 13
        {"src": "none", "idx": 0, "inv": False},   # 14
        {"src": "none", "idx": 0, "inv": False},   # 15
        {"src": "none", "idx": 0, "inv": False},   # 16
    ],
}

CHANNEL_HINTS = [
    "roll / aileron", "pitch / elevator", "throttle", "yaw / rudder",
    "arm", "flight mode", "", "", "", "", "", "", "", "", "", "",
]


def default_config():
    return copy.deepcopy(DEFAULT_CONFIG)


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str = CONFIG_PATH):
    if not os.path.exists(path):
        return default_config(), None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return default_config(), f"config.json could not be read ({exc}); using defaults"

    cfg = _merge(DEFAULT_CONFIG, data)
    channels = data.get("channels")
    if isinstance(channels, list):
        cfg["channels"] = [dict(DEFAULT_CONFIG["channels"][i] if i < 16 else {},
                                **(channels[i] if i < len(channels) else {}))
                           for i in range(crsf.NUM_CHANNELS)]
    return cfg, None


def save(cfg, path: str = CONFIG_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, path)
