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
import sys

import crsf

# The one place the version is written down. It was in three - the window
# title, SAFETY.txt and the user-agent the map sends to openstreetmap.org -
# and the third had already drifted a release behind the other two.
VERSION = "1.2.0"


def base_dir():
    """Where the app keeps the files it writes.

    Frozen by PyInstaller, __file__ points inside a temporary directory
    that is deleted when the app exits, so a config written there would be
    thrown away every single time - taking the port, the mapping and the
    remembered latch positions with it. Beside the executable is what a
    build you unzip and run expects.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def asset_path(name):
    """Where a file shipped with the app lives.

    The opposite case: PyInstaller unpacks bundled data into _MEIPASS, so
    read-only assets come from there when frozen and from the source tree
    otherwise.
    """
    root = getattr(sys, "_MEIPASS", None)
    if root:
        return os.path.join(root, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


CONFIG_PATH = os.path.join(base_dir(), "config.json")

DEFAULT_CONFIG = {
    "port": "",
    "baud": 921600,
    "rate_hz": 250,
    "rate_auto": True,    # follow the rate the module asks for in its sync frames
    "sync_byte": 0xC8,
    # One entry per device slot: slot 0 is the sticks, slot 1 can be a
    # separate USB throttle. null leaves a slot empty.
    # Which firmware is flying. The telemetry frames are the same either
    # way, but the two say "armed" differently - see crsf.ArmWatch.
    "firmware": "ardupilot",
    "gamepads": [0, None],
    # Where the ExpressLRS layout file for the TX module was last found.
    # Only the TX module tab uses it; it has nothing to do with flying.
    "layout_path": "",
    # Latch positions carried over from the last run, keyed by channel
    # number. Written when the app closes, not by Save, because it is a
    # record of where the controls were rather than a setting.
    "latches": {},
    "deadzone": 0.05,
    "axis_deadzone": {},  # per-axis overrides, keyed by axis number
    "throttle": {
        "dev": 0,             # which gamepad slot the throttle reads
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

# The conventional use of each channel, shown beside it. CH5 says "arm"
# because that one is enforced - it is the arm channel and nothing else can
# be. The rest are only conventions, so CH6 carries no label: calling it the
# flight mode channel suggested something the app does not actually do.
CHANNEL_HINTS = [
    "roll / aileron", "pitch / elevator", "throttle", "yaw / rudder",
    "arm", "", "", "", "", "", "", "", "", "", "", "",
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


# Bumped only if the file's shape changes in a way an older MavJOY would
# read wrongly. Missing keys are filled from the defaults, so simply adding
# a setting is not such a change.
CONFIG_FORMAT = 1

# Keys that record what this machine was doing rather than how the model is
# set up, so they do not travel with an exported configuration.
NOT_PORTABLE = ("latches", "layout_path")


def _normalise(data):
    """Fill a configuration out from the defaults and tidy its shape."""
    cfg = _merge(DEFAULT_CONFIG, data)
    # Configs written before multi-device support named a single pad.
    if "gamepads" not in data and "gamepad_index" in data:
        cfg["gamepads"] = [data["gamepad_index"], None]
    cfg["gamepads"] = list(cfg.get("gamepads") or [0, None])
    while len(cfg["gamepads"]) < 2:
        cfg["gamepads"].append(None)
    channels = data.get("channels")
    if isinstance(channels, list):
        cfg["channels"] = [dict(DEFAULT_CONFIG["channels"][i] if i < 16 else {},
                                **(channels[i] if i < len(channels) else {}))
                           for i in range(crsf.NUM_CHANNELS)]
    return cfg


def export(cfg, path):
    """Write the setup out as a file another copy of MavJOY can read.

    Latch positions stay behind. They record where the controls happened to
    be left, not how the model is set up, and carrying them across would
    put a channel high on the other machine because it was high on this
    one - including an arm channel.
    """
    out = {k: v for k, v in copy.deepcopy(cfg).items() if k not in NOT_PORTABLE}
    out["mavjoy_config"] = CONFIG_FORMAT
    save(out, path)


def read_file(path):
    """Read an exported configuration, or raise ValueError saying why not.

    Merged over the defaults, so a file from an older MavJOY is filled in
    rather than refused. Latches are dropped coming in as well as going
    out, since the file may be a copied config.json rather than an export.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"could not be opened ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("does not hold a configuration")
    if not isinstance(data.get("channels"), list):
        raise ValueError("has no channel mapping, so it is not a MavJOY "
                         "configuration")
    # The two checks above catch the shapes worth naming; this catches the
    # rest, so that a file which is valid JSON but malformed inside comes
    # back as the import dialog's error rather than as a traceback with no
    # console to print to.
    try:
        cfg = _normalise(data)
    except Exception as exc:
        raise ValueError(f"could not be understood ({exc})") from exc
    for key in NOT_PORTABLE:
        cfg[key] = copy.deepcopy(DEFAULT_CONFIG[key])
    return cfg


def load(path: str = CONFIG_PATH):
    if not os.path.exists(path):
        return default_config(), None
    # _normalise has to sit inside the guard, not after it. A file can be
    # perfectly good JSON and still be the wrong shape - a scalar where a
    # list belongs, a null in the channel array - and the shaping below
    # unpacks those without checking. This runs from App.__init__, before
    # there is any window to show an error in, so anything escaping here
    # means the program does not start at all rather than starting on the
    # defaults it already knows how to fall back to.
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = _normalise(data)
    except Exception as exc:
        return default_config(), f"config.json could not be read ({exc}); using defaults"

    return cfg, None


def save(cfg, path: str = CONFIG_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, path)
