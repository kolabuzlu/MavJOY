"""
Gamepad reading and channel mapping.

The gamepad is polled on its own thread so that a stalled GUI can never
affect the control path, and so that a stalled *input* thread is visible
to the link thread (which then stops transmitting, dropping the RX into
failsafe rather than flying on stale stick positions).

Mapping is deliberately 1:1 — one physical input to one RC channel, with
no mixing, no expo and no curves. All of that belongs on the flight
controller.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

import crsf

# SDL needs *a* video driver to pump joystick events; "dummy" avoids
# opening a window (Tk owns the real one).
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# SDL >= 2.0.16 reads XInput-class pads through RawInput by default, and
# RawInput delivers state as WM_INPUT messages to a window. The dummy video
# driver above never creates one, so every axis sits frozen at its power-on
# value: sticks 0.0, triggers -1.0, forever, while the pad is working fine.
# Measured on an F710 with SDL 2.32: 0 pygame updates against 382 XInput
# updates over the same 7 seconds. Turning RawInput off drops SDL back to
# the XInput/DirectInput path, which polls and needs no window.
#
# That backend also numbers the axes differently - triggers land on 2 and 5
# rather than 4 and 5 - which is the layout config.py documents.
os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")

import pygame  # noqa: E402  (must come after the env vars above)

STALE_INPUT_SEC = 0.15  # no fresh gamepad data for this long -> cut the link


@dataclass
class InputState:
    axes: tuple = ()
    buttons: tuple = ()
    hats: tuple = ()
    timestamp: float = 0.0
    device_name: str = ""
    connected: bool = False

    def age(self) -> float:
        return time.monotonic() - self.timestamp

    def is_fresh(self) -> bool:
        return self.connected and self.age() < STALE_INPUT_SEC


class GamepadThread(threading.Thread):
    """Owns pygame, polls the selected pad at `rate_hz`, publishes InputState."""

    def __init__(self, rate_hz: int = 250):
        super().__init__(name="gamepad", daemon=True)
        self._period = 1.0 / rate_hz
        self._lock = threading.Lock()
        self._state = InputState()
        self._devices = []
        self._wanted = None          # index the GUI asked for
        self._rescan = threading.Event()
        self._stop_event = threading.Event()
        self._joy = None
        self._open_index = None
        self.error = ""

    # ---------------------------------------------------------- public API
    @property
    def state(self) -> InputState:
        with self._lock:
            return self._state

    @property
    def devices(self):
        with self._lock:
            return list(self._devices)

    @property
    def open_index(self):
        return self._open_index

    def select(self, index):
        self._wanted = index
        self._rescan.set()

    def rescan(self):
        self._rescan.set()

    def stop(self):
        self._stop_event.set()

    # -------------------------------------------------------------- thread
    def run(self):
        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as exc:  # pragma: no cover - platform dependent
            self.error = f"pygame init failed: {exc}"
            return

        self._scan()
        next_t = time.perf_counter()
        while not self._stop_event.is_set():
            pygame.event.pump()

            if self._rescan.is_set():
                self._rescan.clear()
                self._scan()
                self._open(self._wanted)

            self._poll()

            next_t += self._period
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()

        self._close()
        pygame.quit()

    # ------------------------------------------------------------ internal
    def _scan(self):
        pygame.joystick.quit()
        pygame.joystick.init()
        names = []
        for i in range(pygame.joystick.get_count()):
            try:
                j = pygame.joystick.Joystick(i)
                names.append(j.get_name())
            except Exception:
                names.append(f"device {i}")
        with self._lock:
            self._devices = names
        # a rescan invalidates the handle we had
        self._joy = None
        self._open_index = None

    def _open(self, index):
        self._close()
        if index is None:
            return
        try:
            self._joy = pygame.joystick.Joystick(index)
            self._joy.init()
            self._open_index = index
            self.error = ""
        except Exception as exc:
            self._joy = None
            self._open_index = None
            self.error = f"could not open gamepad {index}: {exc}"

    def _close(self):
        if self._joy is not None:
            try:
                self._joy.quit()
            except Exception:
                pass
        self._joy = None
        self._open_index = None

    def _poll(self):
        if self._joy is None:
            with self._lock:
                self._state = InputState(connected=False, timestamp=time.monotonic())
            return
        try:
            axes = tuple(self._joy.get_axis(i) for i in range(self._joy.get_numaxes()))
            buttons = tuple(bool(self._joy.get_button(i))
                            for i in range(self._joy.get_numbuttons()))
            hats = tuple(self._joy.get_hat(i) for i in range(self._joy.get_numhats()))
            name = self._joy.get_name()
        except Exception as exc:
            self.error = f"gamepad read failed: {exc}"
            self._close()
            with self._lock:
                self._state = InputState(connected=False, timestamp=time.monotonic())
            return

        with self._lock:
            self._state = InputState(axes=axes, buttons=buttons, hats=hats,
                                     timestamp=time.monotonic(), device_name=name,
                                     connected=True)


class SimGamepadThread(threading.Thread):
    """
    Drop-in replacement for GamepadThread that synthesises input, so the
    serial link and the mapping can be tested with no hardware attached.
    Axis/button layout matches an F710 in X mode.
    """

    def __init__(self, rate_hz: int = 250):
        super().__init__(name="gamepad-sim", daemon=True)
        self._period = 1.0 / rate_hz
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state = InputState()
        self.error = ""
        self._t0 = time.monotonic()

    @property
    def state(self) -> InputState:
        with self._lock:
            return self._state

    @property
    def devices(self):
        return ["Simulated F710 (X mode)"]

    @property
    def open_index(self):
        return 0

    def select(self, index):
        pass

    def rescan(self):
        pass

    def stop(self):
        self._stop_event.set()

    def run(self):
        import math
        while not self._stop_event.is_set():
            t = time.monotonic() - self._t0
            axes = (
                0.6 * math.sin(t * 0.7),          # 0 left X   (rudder)
                0.0,                              # 1 left Y
                -1.0,                             # 2 left trigger (released)
                0.8 * math.sin(t * 1.1),          # 3 right X  (aileron)
                0.5 * math.cos(t * 0.9),          # 4 right Y  (elevator)
                -1.0,                             # 5 right trigger (released)
            )
            buttons = tuple(False for _ in range(10))
            with self._lock:
                self._state = InputState(axes=axes, buttons=buttons, hats=((0, 0),),
                                         timestamp=time.monotonic(),
                                         device_name="Simulated F710 (X mode)",
                                         connected=True)
            time.sleep(self._period)


# ===========================================================================
#  Channel mapping
# ===========================================================================

SOURCES = ("none", "axis", "throttle", "button", "toggle", "cycle", "hat_x", "hat_y", "fixed")

SOURCE_HELP = {
    "none": "sends centre (992)",
    "axis": "analog axis, -1..+1 -> 172..1811",
    "throttle": "the throttle engine configured above",
    "button": "momentary: low when released, high while held",
    "toggle": "latching: each press flips low/high (use for ARM)",
    "cycle": "each press steps to the next position, then wraps",
    "hat_x": "d-pad left/right",
    "hat_y": "d-pad up/down",
    "fixed": "constant value",
}

THROTTLE_MODES = ("trigger", "ramp", "axis")

THROTTLE_MODE_HELP = {
    "trigger": "Analog trigger. Released = idle. You hold it for the whole flight, "
               "but letting go always means idle.",
    "ramp": "Ratcheting. Hold up-source to increase, down-source to decrease; the "
            "setting stays where you left it, like a real throttle stick.",
    "axis": "Raw stick axis. WARNING: the F710 sticks self-centre, so this means "
            "throttle snaps to 50% whenever you let go.",
}


def _apply_deadzone(value: float, deadzone: float) -> float:
    if deadzone <= 0.0:
        return value
    if abs(value) <= deadzone:
        return 0.0
    # rescale so the usable range still reaches full deflection
    return (value - deadzone * (1 if value > 0 else -1)) / (1.0 - deadzone)


class ThrottleEngine:
    """Turns whatever the gamepad offers into a 0.0-1.0 throttle demand."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.value = 0.0
        self._last_cut = False

    def reset(self):
        self.value = 0.0

    def update(self, st: InputState, dt: float) -> float:
        cfg = self.cfg
        mode = cfg.get("mode", "trigger")

        cut_btn = cfg.get("cut_button", -1)
        if 0 <= cut_btn < len(st.buttons) and st.buttons[cut_btn]:
            self.value = 0.0
            if mode == "ramp":
                return 0.0

        if mode == "trigger":
            self.value = self._trigger_value(st, cfg.get("axis", 5))
        elif mode == "axis":
            raw = self._axis(st, cfg.get("axis", 1))
            if cfg.get("reverse", True):
                raw = -raw
            self.value = (raw + 1.0) / 2.0
        elif mode == "ramp":
            up = self._ramp_demand(st, cfg.get("up_source", "axis"),
                                   cfg.get("axis", 5))
            down = self._ramp_demand(st, cfg.get("down_source", "axis"),
                                     cfg.get("axis_down", 4))
            rate = float(cfg.get("ramp_rate", 0.6))  # full travel per second
            self.value += (up - down) * rate * dt

        self.value = 0.0 if self.value < 0.0 else (1.0 if self.value > 1.0 else self.value)
        return self.value

    def _axis(self, st: InputState, idx: int) -> float:
        if 0 <= idx < len(st.axes):
            return _apply_deadzone(st.axes[idx], float(self.cfg.get("deadzone", 0.04)))
        return 0.0

    def _trigger_value(self, st: InputState, idx: int) -> float:
        """SDL reports an untouched trigger as -1.0 and fully pressed as +1.0."""
        if not (0 <= idx < len(st.axes)):
            return 0.0
        v = (st.axes[idx] + 1.0) / 2.0
        dz = float(self.cfg.get("deadzone", 0.04))
        return 0.0 if v < dz else min(1.0, (v - dz) / (1.0 - dz))

    def _ramp_demand(self, st: InputState, source: str, idx: int) -> float:
        if source == "button":
            return 1.0 if (0 <= idx < len(st.buttons) and st.buttons[idx]) else 0.0
        return self._trigger_value(st, idx)


@dataclass
class ChannelMap:
    """How one RC channel gets its value."""
    src: str = "none"
    idx: int = 0
    inv: bool = False
    value: int = crsf.CHANNEL_MID  # for src == "fixed"
    steps: int = 3                 # for src == "cycle"

    @classmethod
    def from_dict(cls, d):
        return cls(src=d.get("src", "none"), idx=int(d.get("idx", 0)),
                   inv=bool(d.get("inv", False)),
                   value=int(d.get("value", crsf.CHANNEL_MID)),
                   steps=int(d.get("steps", 3)))

    def to_dict(self):
        return {"src": self.src, "idx": self.idx, "inv": self.inv,
                "value": self.value, "steps": self.steps}


class Mixer:
    """
    Evaluates the channel map. Holds the latch state for toggle/cycle
    sources, so it also owns the edge detection for buttons.
    """

    def __init__(self, config: dict):
        self.config = config
        self.channels = [ChannelMap.from_dict(c) for c in config["channels"]]
        self.throttle = ThrottleEngine(config["throttle"])
        self.deadzone = float(config.get("deadzone", 0.04))
        self._toggles = {}
        self._cycles = {}
        self._prev_buttons = ()
        self._last_t = None
        self.last_values = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS

    def reset(self):
        """Called before a link is started: everything back to a safe state."""
        self._toggles.clear()
        self._cycles.clear()
        self._prev_buttons = ()
        self._last_t = None
        self.throttle.reset()
        self.last_values = self.failsafe_values()

    def failsafe_values(self):
        vals = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS
        for i, ch in enumerate(self.channels):
            if ch.src == "throttle":
                vals[i] = crsf.CHANNEL_MIN
            elif ch.src in ("button", "toggle", "cycle"):
                vals[i] = crsf.CHANNEL_MIN
            elif ch.src == "fixed":
                vals[i] = ch.value
        return vals

    def compute(self, st: InputState):
        now = time.monotonic()
        dt = 0.0 if self._last_t is None else min(now - self._last_t, 0.1)
        self._last_t = now

        pressed = self._rising_edges(st.buttons)
        thr = self.throttle.update(st, dt)

        vals = []
        for ch in self.channels:
            vals.append(self._channel_value(ch, st, thr, pressed))
        self.last_values = vals
        return vals

    # ------------------------------------------------------------ internals
    def _rising_edges(self, buttons):
        prev = self._prev_buttons
        edges = set()
        for i, b in enumerate(buttons):
            if b and not (i < len(prev) and prev[i]):
                edges.add(i)
        self._prev_buttons = tuple(buttons)
        return edges

    def _channel_value(self, ch: ChannelMap, st: InputState, thr: float, pressed):
        src = ch.src

        if src == "none":
            return crsf.CHANNEL_MID

        if src == "fixed":
            return crsf.clamp_channel(ch.value)

        if src == "throttle":
            v = thr
            return crsf.unit_to_crsf(1.0 - v if ch.inv else v)

        if src == "axis":
            raw = st.axes[ch.idx] if ch.idx < len(st.axes) else 0.0
            raw = _apply_deadzone(raw, self.deadzone)
            return crsf.norm_to_crsf(-raw if ch.inv else raw)

        if src == "button":
            on = ch.idx < len(st.buttons) and st.buttons[ch.idx]
            if ch.inv:
                on = not on
            return crsf.CHANNEL_MAX if on else crsf.CHANNEL_MIN

        if src == "toggle":
            if ch.idx in pressed:
                self._toggles[ch.idx] = not self._toggles.get(ch.idx, False)
            on = self._toggles.get(ch.idx, False)
            if ch.inv:
                on = not on
            return crsf.CHANNEL_MAX if on else crsf.CHANNEL_MIN

        if src == "cycle":
            steps = max(2, min(6, ch.steps))
            if ch.idx in pressed:
                self._cycles[ch.idx] = (self._cycles.get(ch.idx, 0) + 1) % steps
            pos = self._cycles.get(ch.idx, 0)
            if ch.inv:
                pos = steps - 1 - pos
            return crsf.CHANNEL_MIN + round(pos * (crsf.CHANNEL_MAX - crsf.CHANNEL_MIN)
                                            / (steps - 1))

        if src in ("hat_x", "hat_y"):
            if ch.idx < len(st.hats):
                hx, hy = st.hats[ch.idx]
                raw = float(hx if src == "hat_x" else hy)
            else:
                raw = 0.0
            return crsf.norm_to_crsf(-raw if ch.inv else raw)

        return crsf.CHANNEL_MID

    # ---------------------------------------------------------- introspection
    def toggle_states(self):
        return dict(self._toggles)

    def armed_channels(self):
        """Channels currently sitting at high on a latching source."""
        out = []
        for i, ch in enumerate(self.channels):
            if ch.src == "toggle" and self._toggles.get(ch.idx, False) != ch.inv:
                out.append(i + 1)
            elif ch.src == "cycle" and self._cycles.get(ch.idx, 0) != 0:
                out.append(i + 1)
        return out
