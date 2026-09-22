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

# Shared singletons: compute() runs up to 500 times a second and these
# were being rebuilt on every call.
_NO_EDGES = frozenset()


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


_BLANK_STATE = InputState()


class GamepadThread(threading.Thread):
    """Owns pygame and polls every selected device, publishing one InputState
    per slot.

    More than one device is normal: a pad for the sticks and a separate USB
    throttle, say. Each slot is polled independently and carries its own
    timestamp, so one device dying is visible on its own rather than being
    hidden behind another that is still reporting. The link thread checks
    every slot the map actually uses.
    """

    def __init__(self, rate_hz: int = 250):
        super().__init__(name="gamepad", daemon=True)
        self._period = 1.0 / rate_hz
        self._lock = threading.Lock()
        self._states = {}            # slot -> InputState
        self._devices = []
        self._wanted = {}            # slot -> device index the GUI asked for
        self._wanted_id = {}         # slot -> {"name", "guid"} it was given
        self.devices_seq = 0         # bumped whenever the device list changes
        self._rescan = threading.Event()
        self._stop_event = threading.Event()
        self._joys = {}              # slot -> pygame joystick
        self._open_index = {}        # slot -> device index actually open
        self.error = ""

    # ---------------------------------------------------------- public API
    @property
    def state(self) -> InputState:
        """Slot 0, for callers that only care about the primary device."""
        return self.state_for(0)

    def state_for(self, slot: int) -> InputState:
        with self._lock:
            return self._states.get(slot) or InputState()

    @property
    def states(self):
        with self._lock:
            return dict(self._states)

    @property
    def devices(self):
        with self._lock:
            return [d["name"] for d in self._devices]

    @property
    def device_list(self):
        """Every device seen, with the identity a slot is matched on."""
        with self._lock:
            return [dict(d) for d in self._devices]

    @property
    def open_index(self):
        return self._open_index.get(0)

    @property
    def open_indexes(self):
        return dict(self._open_index)

    def select(self, index, slot: int = 0):
        """Put device `index` in `slot`; None empties the slot.

        The device's identity is remembered alongside the index, because
        indexes are reassigned as devices come and go.
        """
        ident = None
        if index is not None:
            for d in self.device_list:
                if d["index"] == index:
                    ident = {"name": d["name"], "guid": d["guid"]}
                    break
        self._wanted[slot] = index
        self._wanted_id[slot] = ident
        self._rescan.set()

    def select_identity(self, ident, slot: int = 0):
        """Restore a slot from a saved identity, resolving the index now."""
        self._wanted_id[slot] = ident or None
        self._wanted[slot] = (ident or {}).get("index")
        self._rescan.set()

    def identity(self, slot: int):
        """What slot is holding, in a form that survives a replug."""
        ident = self._wanted_id.get(slot)
        return dict(ident) if ident else None

    def resolve(self, ident, taken=()):
        """The index this identity maps to right now, or None if it is gone.

        Public because the GUI needs the answer before the thread has acted
        on a rescan, and it should not be reading this object's internals.
        """
        return self._match(self.device_list, ident,
                           (ident or {}).get("index"), set(taken))

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
            # get(), not pump(): pump processes the queue and throws it away,
            # and SDL is already telling us when a device arrives or leaves.
            for event in pygame.event.get():
                if event.type == pygame.JOYDEVICEREMOVED:
                    # Close the handle here and now. SDL does NOT invalidate
                    # an open joystick when its device leaves: reads keep
                    # succeeding and quietly return zeros, so a slot would go
                    # on publishing fresh-looking data for hardware that is
                    # not there - and nothing downstream would ever see it as
                    # stale, so the link would keep transmitting and the model
                    # would never reach failsafe.
                    self._device_removed(getattr(event, "instance_id", None))
                    self._rescan.set()
                elif event.type == pygame.JOYDEVICEADDED:
                    self._rescan.set()

            if self._rescan.is_set():
                # Cleared before the work, never after: clearing afterwards
                # would swallow a select() the GUI raised while the scan was
                # running, leaving the picker showing a device the thread
                # never opened.
                self._rescan.clear()
                self._scan()
                self._resolve_slots()
                # _scan tears the subsystem down and back up, and SDL
                # announces every device again when it does. Those are echoes
                # of the scan just finished, not hardware changing - and
                # treating them as real closes every slot and schedules
                # another scan, forever. A genuine change that lands in this
                # window is caught by the count check below instead.
                pygame.event.clear(pygame.JOYDEVICEADDED)
                pygame.event.clear(pygame.JOYDEVICEREMOVED)

            # Independent of the events above, because losing a removal
            # would leave a slot reporting hardware that is gone. get_count()
            # is a cheap SDL call and it disagrees the moment a device goes.
            try:
                if pygame.joystick.get_count() != len(self._devices):
                    self._rescan.set()
            except Exception:
                pass

            self._poll()

            next_t += self._period
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()

        self._close_all()
        pygame.quit()

    # ------------------------------------------------------------ internal
    def _scan(self):
        """Refresh the device list.

        This tears the joystick subsystem down and brings it back up, which
        is slower than enumerating in place and briefly starves the polling
        loop. It is done that way deliberately. SDL does not invalidate an
        open handle when its device is unplugged - reads keep succeeding and
        return zeros - so without the teardown a slot goes on publishing
        fresh-looking data for hardware that is not there, nothing ever reads
        as stale, and the model never reaches failsafe. Caching handles to
        avoid the cost was tried and produced exactly that, plus a segfault
        when two owners quit the same handle. Correctness first; the removal
        event below catches the common case long before this runs.
        """
        pygame.joystick.quit()
        pygame.joystick.init()
        self._joys = {}
        self._open_index = {}
        devices = []
        for i in range(pygame.joystick.get_count()):
            name, guid = f"device {i}", ""
            try:
                joy = pygame.joystick.Joystick(i)
                name, guid = joy.get_name(), joy.get_guid()
            except Exception:
                pass
            devices.append({"index": i, "name": name, "guid": guid})
        with self._lock:
            if devices != self._devices:
                self.devices_seq += 1
            self._devices = devices

    def _resolve_slots(self):
        """Re-point every slot at the device it was told to hold.

        Indexes are reassigned when devices come and go, so a slot is matched
        on identity - GUID first, then name - and only falls back to a raw
        index when it was never given one (an older config). A device already
        claimed by a lower slot is never handed to a second one.
        """
        devices = self.device_list
        taken = set()
        for slot in sorted(set(self._wanted) | set(self._wanted_id)):
            index = self._match(devices, self._wanted_id.get(slot),
                                self._wanted.get(slot), taken)
            self._wanted[slot] = index
            if index is not None:
                taken.add(index)
            self._open(slot, index)

    @staticmethod
    def _match(devices, ident, fallback_index, taken):
        named = False
        for key in ("guid", "name"):
            want = (ident or {}).get(key)
            if not want:
                continue
            named = True
            for d in devices:
                if d[key] == want and d["index"] not in taken:
                    return d["index"]
        if named:
            return None          # it was named, and it is not here

        # No identity, or one carrying nothing but an index - which is what a
        # config written before identity matching holds, and what the shipped
        # default is. Fall back to the position, then remember what was found
        # there so the next start matches on identity instead.
        if fallback_index is not None:
            for d in devices:
                if d["index"] == fallback_index and d["index"] not in taken:
                    return d["index"]
        return None

    @staticmethod
    def _instance_of(joy):
        try:
            return joy.get_instance_id()
        except Exception:
            return None

    def _device_removed(self, instance_id):
        """Close the slot holding a device that has just been removed.

        A fast path ahead of the rescan: the slot reads as disconnected on
        the very next poll rather than waiting for the re-enumeration, so the
        link stops transmitting promptly.
        """
        for slot, joy in list(self._joys.items()):
            if instance_id is None or self._instance_of(joy) == instance_id:
                self._close(slot)

    def _open(self, slot, index):
        self._close(slot)
        if index is None:
            return
        try:
            joy = pygame.joystick.Joystick(index)
            joy.init()
            self._joys[slot] = joy
            self._open_index[slot] = index
            self.error = ""
        except Exception as exc:
            self.error = f"could not open gamepad {index}: {exc}"

    def _close(self, slot):
        joy = self._joys.pop(slot, None)
        if joy is not None:
            try:
                joy.quit()
            except Exception:
                pass
        self._open_index.pop(slot, None)

    def _close_all(self):
        for slot in list(self._joys):
            self._close(slot)

    def _poll(self):
        fresh = {}
        for slot in set(self._wanted) | set(self._joys):
            joy = self._joys.get(slot)
            if joy is None:
                fresh[slot] = InputState(connected=False,
                                         timestamp=time.monotonic())
                continue
            try:
                fresh[slot] = InputState(
                    axes=tuple(joy.get_axis(i) for i in range(joy.get_numaxes())),
                    buttons=tuple(bool(joy.get_button(i))
                                  for i in range(joy.get_numbuttons())),
                    hats=tuple(joy.get_hat(i) for i in range(joy.get_numhats())),
                    timestamp=time.monotonic(),
                    device_name=joy.get_name(),
                    connected=True)
            except Exception as exc:
                self.error = f"gamepad read failed: {exc}"
                self._close(slot)
                fresh[slot] = InputState(connected=False,
                                         timestamp=time.monotonic())
        with self._lock:
            self._states = fresh


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
        self._slots = {0}
        self.devices_seq = 0

    @property
    def state(self) -> InputState:
        with self._lock:
            return self._state

    def state_for(self, slot: int) -> InputState:
        # Only slots that were actually selected report. Claiming every slot
        # is live while `states` held one entry made a channel on device 1
        # look connected in the Inputs tab and then fail to start.
        return self.state if slot in self._slots else InputState()

    @property
    def states(self):
        st = self.state
        return {slot: st for slot in sorted(self._slots)}

    @property
    def devices(self):
        return ["Simulated F710 (X mode)"]

    @property
    def device_list(self):
        return [{"index": 0, "name": "Simulated F710 (X mode)", "guid": "sim"}]

    def select_identity(self, ident, slot: int = 0):
        self.select(None if ident is None else 0, slot)

    def resolve(self, ident, taken=()):
        return None if ident is None or 0 in set(taken) else 0

    def identity(self, slot: int):
        if slot not in self._slots:
            return None
        return {"name": "Simulated F710 (X mode)", "guid": "sim", "index": 0}

    @property
    def open_index(self):
        return 0

    @property
    def open_indexes(self):
        return {slot: 0 for slot in sorted(self._slots)}

    def select(self, index, slot: int = 0):
        if index is None:
            self._slots.discard(slot)
        else:
            self._slots.add(slot)

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

SOURCES = ("none", "axis", "throttle", "button", "toggle", "oneway",
           "cycle", "switch", "hat_x", "hat_y", "fixed")

# Sources that read a numbered input. The rest ignore the index: none
# sends centre, throttle comes from its own engine, fixed uses value.
INDEXED_SOURCES = ("axis", "button", "toggle", "oneway", "cycle", "switch",
                   "hat_x", "hat_y")

# Sources a guard button can hold shut. All of them are driven by buttons,
# which is what makes a guard meaningful: there is a discrete moment the
# channel would otherwise change, and the guard decides whether it counts.
# An axis has no such moment, so a guard on one would be a mute, not a guard.
GUARDED_SOURCES = ("button", "toggle", "oneway", "cycle", "switch")

SOURCE_HELP = {
    "none": "unused: sends centre (1500 µs)",
    "axis": "analog axis, -1..+1 -> 172..1811",
    "throttle": "the throttle engine configured above",
    "button": "momentary: low when released, high while held",
    "toggle": "latching: each press flips low/high (use for ARM)",
    "oneway": "one-way toggle: a press sets it high and it stays high, however "
              "often it is pressed. Only a reset channel brings it back",
    "cycle": "each press steps to the next position, then wraps",
    "switch": "multi-position switch wired as one button per position: index "
              "is the first button, steps is how many",
    "hat_x": "d-pad left/right",
    "hat_y": "d-pad up/down",
    "fixed": "a constant you type, in microseconds",
}

THROTTLE_MODES = ("trigger", "ramp", "axis")

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


def _endpoint(value, default):
    """Read an endpoint from config, carrying the old figures forward.

    The Outputs tab first shipped with 988 and 2012, the round numbers the
    CRSF range is usually quoted with. A flight controller reports 987 and
    2011, so those were a microsecond out at both ends. Anything still
    carrying the old pair meant full travel and is read as full travel,
    rather than being left very slightly scaled for no reason anybody asked
    for.
    """
    if value is None:
        return default
    try:
        us = int(value)
    except (TypeError, ValueError):
        return default
    if us == 988:
        return crsf.US_MIN
    if us == 2012:
        return crsf.US_MAX
    return max(crsf.US_MIN, min(crsf.US_MAX, us))


@dataclass
class ChannelMap:
    """How one RC channel gets its value."""
    src: str = "none"
    idx: int = 0
    inv: bool = False
    dev: int = 0                   # which gamepad slot this reads
    reset_ch: int = 0              # 1-16: a latch drops low when that moves
    reset_move: int = 100          # how far it must move to count, in us
    value: int = crsf.CHANNEL_MID  # for src == "fixed"
    steps: int = 3                 # positions, for "cycle" and "switch"
    guard: int = -1                # button that must be held to change it
    out_min: int = crsf.US_MIN     # endpoint, in microseconds
    out_mid: int = crsf.US_MID     # where centre sits, in microseconds
    out_max: int = crsf.US_MAX     # endpoint, in microseconds
    buttons: tuple = ()            # for "switch": explicit, non-consecutive

    def __post_init__(self):
        # Worked out once here rather than per frame: the map is replaced
        # wholesale whenever anything about it is edited, so there is no
        # such thing as a stale copy.
        # Snapped at the ends rather than converted: more than one channel
        # value reads back as the same microsecond, so converting 2011 can
        # land a unit short of the top and leave a channel fractionally
        # scaled when it was meant to be left alone.
        self.lo_units = (crsf.CHANNEL_MIN if self.out_min <= crsf.US_MIN
                         else crsf.clamp_channel(crsf.us_to_crsf(self.out_min)))
        self.hi_units = (crsf.CHANNEL_MAX if self.out_max >= crsf.US_MAX
                         else crsf.clamp_channel(crsf.us_to_crsf(self.out_max)))
        self.mid_units = crsf.clamp_channel(crsf.us_to_crsf(self.out_mid))
        # Held inside the endpoints, so travel stays in one direction. A
        # centre outside them would run one half of the throw backwards,
        # which is not a trim, it is a fault nobody asked for.
        low, high = min(self.lo_units, self.hi_units), max(self.lo_units,
                                                           self.hi_units)
        self.mid_units = max(low, min(high, self.mid_units))
        self.untouched = (self.lo_units == crsf.CHANNEL_MIN
                          and self.hi_units == crsf.CHANNEL_MAX
                          and self.mid_units == crsf.CHANNEL_MID)

    def switch_buttons(self):
        """The buttons a switch watches, one per position.

        Most multi-position switches report as a consecutive block, so the
        map only needs the first button and a count. An explicit list in
        config.json covers the ones that do not."""
        if self.buttons:
            return list(self.buttons)
        count = max(2, min(6, int(self.steps)))
        return [self.idx + i for i in range(count)]

    @classmethod
    def from_dict(cls, d):
        return cls(src=d.get("src", "none"), idx=int(d.get("idx", 0)),
                   inv=bool(d.get("inv", False)),
                   dev=int(d.get("dev", 0)),
                   reset_ch=int(d.get("reset_ch", 0)),
                   reset_move=int(d.get("reset_move", 100)),
                   value=int(d.get("value", crsf.CHANNEL_MID)),
                   steps=int(d.get("steps", 3)),
                   guard=int(d.get("guard", -1)),
                   out_min=_endpoint(d.get("out_min"), crsf.US_MIN),
                   out_mid=_endpoint(d.get("out_mid"), crsf.US_MID),
                   out_max=_endpoint(d.get("out_max"), crsf.US_MAX),
                   buttons=tuple(d.get("buttons") or ()))

    def to_dict(self):
        out = {"src": self.src, "idx": self.idx, "inv": self.inv,
               "dev": self.dev,
               "reset_ch": self.reset_ch, "reset_move": self.reset_move,
               "value": self.value, "steps": self.steps, "guard": self.guard,
               "out_min": self.out_min, "out_mid": self.out_mid,
               "out_max": self.out_max}
        if self.buttons:
            out["buttons"] = list(self.buttons)
        return out


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
        # Per-axis overrides. Sticks wear unevenly, so a single value for the
        # whole pad means deadening the good axes to tame the worst one.
        self.axis_deadzone = self._load_axis_deadzone(config)
        # Latches are keyed by (device slot, input number): button 3 on the
        # pad and button 3 on the throttle are different switches.
        self._toggles = {}
        self._cycles = {}
        self._oneway = {}
        self._switches = {}
        self._reset_ref = {}         # channel -> where its watched channel was
        self._held = {}              # channel -> value frozen at a resume
        self._hold_ref = {}          # channel -> where its input was then
        self._prev_buttons = {}
        self.throttle_dev = int(config.get("throttle", {}).get("dev", 0))
        self._last_t = None
        self.last_values = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS
        # What actually goes on the wire: last_values with the endpoints
        # applied. Kept apart so the two are never confused for each other.
        self.output_values = list(self.last_values)

    @staticmethod
    def _load_axis_deadzone(config):
        """Read the saved overrides, keyed "device:axis".

        Entries written before deadzone was per-device are a bare axis
        number, and belong to device 0.
        """
        out = {}
        for key, value in (config.get("axis_deadzone") or {}).items():
            text = str(key)
            dev, _, axis = text.rpartition(":")
            try:
                out[(int(dev or 0), int(axis))] = float(value)
            except ValueError:
                continue
        return out

    def forget_latches(self, ch):
        """Drop the latch state belonging to a channel that is being replaced.

        Latch keys are derived from the channel - a switch keys on its
        resolved button list - so editing steps or the index orphans the old
        entry, and the channel then falls back to a default instead of the
        position it was actually holding. Clearing it means the next compute
        re-reads the hardware, which for switch and button sources is the
        real position anyway.
        """
        self._toggles.pop((ch.dev, ch.idx), None)
        self._cycles.pop((ch.dev, ch.idx), None)
        self._oneway.pop((ch.dev, ch.idx), None)
        try:
            self._switches.pop((ch.dev, tuple(ch.switch_buttons())), None)
        except Exception:
            pass

    def deadzone_for(self, dev: int, axis: int) -> float:
        """The deadzone for one axis of one device, falling back to the
        pad-wide value. Keyed by device for the same reason the latches are:
        a worn stick on one pad must not deaden another device's axis."""
        return self.axis_deadzone.get((dev, axis), self.deadzone)

    def resync(self, states):
        """Prepare to start transmitting without moving a single control.

        Deliberately does NOT clear the latches or the throttle. Doing that
        puts arm low and throttle at idle in the very first frame, and when
        the link is being restarted to recover a model that is already in
        the air - the whole point of restarting after a failsafe - that
        first frame is a disarm command. The controls are read exactly as
        they stand; it is for the pilot to decide whether that is what they
        want, and start_link asks them.

        Only the edge detector and the timebase are refreshed, so a button
        held across the gap does not read as a fresh press and flip a latch.
        """
        if isinstance(states, InputState):
            states = {0: states}
        self._prev_buttons = {slot: tuple(st.buttons)
                              for slot, st in states.items()}
        self._last_t = None

    def reset(self):
        """Wipe every latch and the throttle back to their power-on state.

        For a genuinely fresh start only - opening the app, loading a config.
        Never on the way into a link: see resync().
        """
        self._toggles.clear()
        self._cycles.clear()
        self._oneway.clear()
        self._switches.clear()
        self._reset_ref.clear()
        self._held.clear()
        self._hold_ref.clear()
        self._prev_buttons = {}
        self._last_t = None
        self.throttle.reset()
        self.last_values = self.failsafe_values()
        self.output_values = self._apply_endpoints(self.last_values)

    def failsafe_values(self):
        vals = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS
        for i, ch in enumerate(self.channels):
            if ch.src == "throttle":
                vals[i] = crsf.CHANNEL_MIN
            elif ch.src in ("button", "toggle", "oneway", "cycle", "switch"):
                vals[i] = crsf.CHANNEL_MIN
            elif ch.src == "fixed":
                vals[i] = ch.value
        return vals

    def _apply_endpoints(self, vals):
        """Scale each channel onto its endpoints; returns a new list.

        The output stage, and the last thing that happens. Everything above
        works in full travel and this is where full travel is told what it
        is worth in microseconds.

        A centred stick lands on the midpoint and each half is scaled onto
        its own end independently, the way a handset's output limits and
        subtrim work together. Scaling the whole range instead would drag
        neutral along with the endpoint, so trimming the top of an aileron
        throw would leave the model in a permanent turn.

        The result is returned rather than written back, because the full
        travel values are what the next frame reasons from - feeding scaled
        values back in would scale them again, and a held channel would
        creep toward centre a little more every frame.
        """
        out = list(vals)
        for i, ch in enumerate(self.channels):
            if ch.untouched or i >= len(out):
                continue
            v = out[i]
            if v >= crsf.CHANNEL_MID:
                span = crsf.CHANNEL_MAX - crsf.CHANNEL_MID
                out[i] = ch.mid_units + round(
                    (v - crsf.CHANNEL_MID) * (ch.hi_units - ch.mid_units)
                    / span)
            else:
                span = crsf.CHANNEL_MID - crsf.CHANNEL_MIN
                out[i] = ch.mid_units - round(
                    (crsf.CHANNEL_MID - v) * (ch.mid_units - ch.lo_units)
                    / span)
            out[i] = crsf.clamp_channel(out[i])
        return out

    def compute(self, states):
        """Evaluate every channel. `states` is {slot: InputState}; a bare
        InputState is accepted and treated as slot 0.

        A device that is not reporting is SKIPPED, not read as a device with
        nothing pressed. Reading it would overwrite that slot's edge memory
        with "no buttons held", so every button still physically down would
        look like a fresh press the moment it came back - and on an arm
        toggle a fresh press means a disarm. Its channels hold the value
        last seen instead: "we do not know" is nearer the truth than
        "everything is off", and the arm interlock reads these values.
        """
        if isinstance(states, InputState):
            states = {0: states}
        now = time.monotonic()
        dt = 0.0 if self._last_t is None else min(now - self._last_t, 0.1)
        self._last_t = now

        live = {slot: st for slot, st in states.items() if st.is_fresh()}
        edges = {slot: self._rising_edges(slot, st.buttons)
                 for slot, st in live.items()}

        thr_state = live.get(self.throttle_dev)
        if thr_state is None:
            # Hold. Integrating a throttle nobody is touching would ramp it
            # up through the outage and transmit that on the first frame back.
            thr = self.throttle.value
        else:
            thr = self.throttle.update(thr_state, dt)

        previous = self.last_values
        vals = []
        for i, ch in enumerate(self.channels):
            st = live.get(ch.dev)
            if st is None and ch.src not in ("none", "fixed"):
                vals.append(previous[i] if i < len(previous) else crsf.CHANNEL_MID)
                continue
            vals.append(self._channel_value(ch, st or _BLANK_STATE, thr,
                                            edges.get(ch.dev, _NO_EDGES)))
        self._apply_resets(vals)
        self._apply_hold(vals)
        self.last_values = vals
        self.output_values = self._apply_endpoints(vals)
        return self.output_values

    @staticmethod
    def _move_units(microseconds):
        """A movement in microseconds, as channel units."""
        return max(1, crsf.us_to_crsf(crsf.US_MIN + float(microseconds))
                   - crsf.CHANNEL_MIN)

    # How far an input has to move after a link comes back before its
    # channel starts following it again. About 2% of travel: enough that a
    # resting stick does not release itself, small enough that a deliberate
    # nudge does.
    RESUME_RELEASE = 32

    # The primary flight controls are never frozen. A pilot who has just got
    # the link back needs the sticks to answer at once, and a held elevator
    # or a throttle stuck where it was is its own emergency - worse than the
    # thing the hold exists to prevent. What must not jump is the switches:
    # a flight mode moved during the outage is what would take the model out
    # of the failsafe it is sitting in, and that is the whole point of this.
    HOLD_EXEMPT = (1, 2, 3, 4)      # roll, pitch, throttle, yaw

    def hold_on_resume(self, values=None):
        """Freeze every channel at the value the model last actually had.

        Called the moment frames start flowing again after a dropout, and
        before the fresh input is read. Without it, anything moved while the
        link was down takes effect the instant it returns - and if that
        included the flight mode, the aircraft leaves the failsafe it was
        holding, which is the one thing recovery must not do on its own.

        Each channel stays frozen until its own input moves again. That move
        is the pilot deliberately taking the channel back, so it is the only
        thing that should hand control over. CH1-4 are never frozen at all;
        see HOLD_EXEMPT.

        `values` is what the model last actually received. It defaults to the
        last values computed, which is right when the input itself went away:
        those were frozen for the whole outage anyway. When the RF link was
        what broke, the sticks never stopped moving and the computed values
        followed them, so the caller passes the snapshot it took while the
        link was still up.
        """
        src = self.last_values if values is None else values
        exempt = {n - 1 for n in self.HOLD_EXEMPT}
        self._held = {i: v for i, v in enumerate(src) if i not in exempt}
        self._hold_ref = {}

    def holding(self):
        """Channel numbers still frozen since the last resume."""
        return sorted(i + 1 for i in self._held)

    def _apply_hold(self, vals):
        if not self._held:
            return
        for i in list(self._held):
            if i >= len(vals):
                del self._held[i]
                continue
            # Measured against where the input sat when the link returned,
            # not against the frozen value - the two differ precisely when
            # something was moved during the outage, which must NOT count.
            ref = self._hold_ref.setdefault(i, vals[i])
            if abs(vals[i] - ref) >= self.RESUME_RELEASE:
                del self._held[i]
                self._hold_ref.pop(i, None)
                continue
            vals[i] = self._held[i]

    def _apply_resets(self, vals):
        """Drop a latch back to low when another channel moves.

        Watches the channel named by reset_ch and fires once it has moved
        further than reset_move from where it was the last time this fired.
        A floor is needed because a resting stick is never perfectly still;
        without one an axis would hold the latch down permanently. Firing is
        a one-shot - the button can turn the latch straight back on - which
        is what resetting means, as opposed to an interlock that holds it.
        """
        for i, ch in enumerate(self.channels):
            if ch.src not in ("toggle", "oneway", "cycle") or not ch.reset_ch:
                continue
            watched = ch.reset_ch - 1
            if not (0 <= watched < len(vals)) or watched == i:
                continue

            moved = self._move_units(ch.reset_move)
            ref = self._reset_ref.get(i)
            if ref is None:
                self._reset_ref[i] = vals[watched]   # first frame: baseline
                continue
            if abs(vals[watched] - ref) < moved:
                continue
            self._reset_ref[i] = vals[watched]

            key = (ch.dev, ch.idx)
            if ch.src == "toggle":
                if self._toggles.get(key, False) == ch.inv:
                    continue                         # already low
                self._toggles[key] = ch.inv          # latch xor inv -> low
            elif ch.src == "oneway":
                # Back to un-pressed, which is what "bring it back" means
                # here. That is the low end for a plain channel, and the
                # resting end for an inverted one - either way it is the
                # state the channel had before the press that latched it.
                if not self._oneway.get(key, False):
                    continue                         # never latched
                self._oneway[key] = False
            else:
                if self._cycles.get(key, 0) == 0:
                    continue
                self._cycles[key] = 0
            vals[i] = self._channel_value(ch, _BLANK_STATE, 0.0, _NO_EDGES)

    # ------------------------------------------------- remembering latches
    def latch_state(self):
        """What every latching channel is holding, keyed by channel number.

        Keyed by the channel rather than by the latch's own key, because
        those keys are made of device and button numbers: saving them and
        putting them back blind would drop an old state onto whatever
        happens to be mapped there next. Channel plus source is enough to
        say "this is the same control", and to notice when it is not.

        CH1-4 are left out, the same four as HOLD_EXEMPT and for the same
        reason: the sticks are never anything but where they are now.
        """
        exempt = set(self.HOLD_EXEMPT)
        out = {}
        for i, ch in enumerate(self.channels):
            n = i + 1
            if n in exempt:
                continue
            state = self._latch_for(ch)
            if state is None:
                continue
            out[str(n)] = {"src": ch.src, "state": state}
        return out

    def _latch_for(self, ch):
        if ch.src == "toggle":
            return self._toggles.get((ch.dev, ch.idx))
        if ch.src == "oneway":
            return self._oneway.get((ch.dev, ch.idx))
        if ch.src == "cycle":
            return self._cycles.get((ch.dev, ch.idx))
        if ch.src == "switch":
            return self._switches.get((ch.dev, tuple(ch.switch_buttons())))
        return None

    def latched_values(self):
        """What each latching channel would send from its stored state alone.

        Read from the latch rather than from last_values, which at startup
        is still the failsafe set: nothing has been computed yet, and the
        whole question here is what the stored state will put on the wire
        the moment something is.
        """
        out = {}
        for i, ch in enumerate(self.channels):
            if self._latch_for(ch) is None:
                continue
            out[i + 1] = self._channel_value(ch, _BLANK_STATE, 0.0, _NO_EDGES)
        return out

    def restore_latches(self, saved):
        """Put back what latch_state saved; returns the channels restored.

        An entry is ignored unless the channel still reads the same kind of
        source. Remap a channel and its old state is not about the same
        control any more, so it is dropped rather than applied to whatever
        took its place.
        """
        exempt = set(self.HOLD_EXEMPT)
        restored = []
        for key, entry in (saved or {}).items():
            try:
                n = int(key)
            except (TypeError, ValueError):
                continue
            if n in exempt or not 1 <= n <= len(self.channels):
                continue
            if not isinstance(entry, dict):
                continue
            ch = self.channels[n - 1]
            if entry.get("src") != ch.src:
                continue
            state = entry.get("state")
            try:
                if ch.src == "toggle":
                    self._toggles[(ch.dev, ch.idx)] = bool(state)
                elif ch.src == "oneway":
                    self._oneway[(ch.dev, ch.idx)] = bool(state)
                elif ch.src == "cycle":
                    self._cycles[(ch.dev, ch.idx)] = max(0, int(state))
                elif ch.src == "switch":
                    self._switches[(ch.dev, tuple(ch.switch_buttons()))] =                         max(0, int(state))
                else:
                    continue
            except (TypeError, ValueError):
                continue
            restored.append(n)
        return sorted(restored)

    def required_devices(self):
        """Slots the map actually reads.

        The link stops if any of these goes quiet. Flying on a throttle that
        stopped reporting is no better than flying on stale sticks, and the
        two devices fail independently.
        """
        used = set()
        for ch in self.channels:
            if ch.src == "throttle":
                used.add(self.throttle_dev)
            elif ch.src not in ("none", "fixed"):
                used.add(ch.dev)
        return used or {0}

    # ------------------------------------------------------------ internals
    def _rising_edges(self, slot, buttons):
        prev = self._prev_buttons.get(slot, ())
        edges = set()
        for i, b in enumerate(buttons):
            if b and not (i < len(prev) and prev[i]):
                edges.add(i)
        self._prev_buttons[slot] = tuple(buttons)
        return edges

    def _channel_value(self, ch: ChannelMap, st: InputState, thr: float, pressed):
        src = ch.src

        # A guard button, for a control that must not move by being brushed
        # against - an arm switch above all. While the guard is not held the
        # channel is evaluated as though nothing were pressed at all, which
        # does the right thing for every source it applies to: a press
        # raises no edge, so a toggle does not flip, a one-way does not
        # latch and a cycle does not step; a momentary button reads low; and
        # a switch, seeing no position lit, holds the one it already had.
        #
        # It guards BOTH ways. Being unable to disarm by accident is the
        # point of it in the air, and on the ground stopping the link is
        # always there if the guard itself fails.
        if ch.guard >= 0 and src in GUARDED_SOURCES:
            held = (ch.guard < len(st.buttons)) and st.buttons[ch.guard]
            if not held:
                st = _BLANK_STATE
                pressed = _NO_EDGES

        if src == "none":
            return crsf.CHANNEL_MID

        if src == "fixed":
            return crsf.clamp_channel(ch.value)

        if src == "throttle":
            v = thr
            return crsf.unit_to_crsf(1.0 - v if ch.inv else v)

        if src == "axis":
            raw = st.axes[ch.idx] if 0 <= ch.idx < len(st.axes) else 0.0
            raw = _apply_deadzone(raw, self.deadzone_for(ch.dev, ch.idx))
            return crsf.norm_to_crsf(-raw if ch.inv else raw)

        if src == "button":
            on = 0 <= ch.idx < len(st.buttons) and st.buttons[ch.idx]
            if ch.inv:
                on = not on
            return crsf.CHANNEL_MAX if on else crsf.CHANNEL_MIN

        if src == "toggle":
            key = (ch.dev, ch.idx)
            if ch.idx in pressed:
                self._toggles[key] = not self._toggles.get(key, False)
            on = self._toggles.get(key, False)
            if ch.inv:
                on = not on
            return crsf.CHANNEL_MAX if on else crsf.CHANNEL_MIN

        if src == "oneway":
            # Set-only. A press latches it and further presses do nothing;
            # the sole way back is a reset channel, handled in _apply_resets.
            # Useful for anything that must not be undone by a fumbled
            # second press - the press is the commitment, and taking it back
            # is made deliberately awkward.
            key = (ch.dev, ch.idx)
            if ch.idx in pressed:
                self._oneway[key] = True
            on = self._oneway.get(key, False)
            if ch.inv:
                on = not on
            return crsf.CHANNEL_MAX if on else crsf.CHANNEL_MIN

        if src == "cycle":
            steps = max(2, min(6, ch.steps))
            key = (ch.dev, ch.idx)
            if ch.idx in pressed:
                self._cycles[key] = (self._cycles.get(key, 0) + 1) % steps
            pos = self._cycles.get(key, 0)
            if ch.inv:
                pos = steps - 1 - pos
            return crsf.CHANNEL_MIN + round(pos * (crsf.CHANNEL_MAX - crsf.CHANNEL_MIN)
                                            / (steps - 1))

        if src == "switch":
            buttons = ch.switch_buttons()
            steps = max(2, len(buttons))
            # Keyed on the buttons themselves: with an explicit list, idx is
            # ignored by switch_buttons, so two such switches would otherwise
            # share a latch and yank each other between detents.
            key = (ch.dev, tuple(buttons))
            pos = None
            for i, btn in enumerate(buttons):
                if 0 <= btn < len(st.buttons) and st.buttons[btn]:
                    pos = i
                    break
            if pos is None:
                # Nothing lit. Real switches pass through a gap between
                # detents, so hold the last position rather than snapping the
                # channel to an end stop mid-move. With nothing held yet -
                # including when no button is ever in range - fall back to
                # whichever end reads low once inv is applied, never to full
                # deflection.
                pos = self._switches.get(key)
                if pos is None:
                    pos = steps - 1 if ch.inv else 0
            else:
                self._switches[key] = pos
            if ch.inv:
                pos = steps - 1 - pos
            return crsf.CHANNEL_MIN + round(pos * (crsf.CHANNEL_MAX - crsf.CHANNEL_MIN)
                                            / (steps - 1))

        if src in ("hat_x", "hat_y"):
            if 0 <= ch.idx < len(st.hats):
                hx, hy = st.hats[ch.idx]
                raw = float(hx if src == "hat_x" else hy)
            else:
                raw = 0.0
            return crsf.norm_to_crsf(-raw if ch.inv else raw)

        return crsf.CHANNEL_MID

    # ---------------------------------------------------------- introspection
    def toggle_states(self):
        return dict(self._toggles)

    # CH5 is the arm channel. Always, and nothing else ever is.
    ARM_CHANNEL = 5

    def armed_channels(self):
        """The arm channel, if it is currently reading armed.

        CH5, and only CH5. This used to work the other way round - anything
        that looked like an arm switch counted, so a latched flight mode on
        CH6 or a one-way on CH7 announced itself as armed and the interlock
        refused to write settings. Guessing which channel means "armed" from
        the source type is not something that can be got right, because the
        same sources are used for everything else too.

        The value actually computed is what is read, so inv and the source's
        own rules are already accounted for. An unmapped CH5 sits at centre
        and so never reads armed.
        """
        i = self.ARM_CHANNEL - 1
        if i < len(self.last_values) and self.last_values[i] > crsf.CHANNEL_MID:
            return [self.ARM_CHANNEL]
        return []
