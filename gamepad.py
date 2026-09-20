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

SOURCES = ("none", "axis", "throttle", "button", "toggle", "cycle", "switch",
           "hat_x", "hat_y", "fixed")

# Sources that read a numbered input. The rest ignore the index: none
# sends centre, throttle comes from its own engine, fixed uses value.
INDEXED_SOURCES = ("axis", "button", "toggle", "cycle", "switch", "hat_x", "hat_y")

SOURCE_HELP = {
    "none": "sends centre (992)",
    "axis": "analog axis, -1..+1 -> 172..1811",
    "throttle": "the throttle engine configured above",
    "button": "momentary: low when released, high while held",
    "toggle": "latching: each press flips low/high (use for ARM)",
    "cycle": "each press steps to the next position, then wraps",
    "switch": "multi-position switch wired as one button per position: index "
              "is the first button, steps is how many",
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
    dev: int = 0                   # which gamepad slot this reads
    arm: bool = False              # treat high on this channel as armed
    value: int = crsf.CHANNEL_MID  # for src == "fixed"
    steps: int = 3                 # positions, for "cycle" and "switch"
    buttons: tuple = ()            # for "switch": explicit, non-consecutive

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
                   dev=int(d.get("dev", 0)), arm=bool(d.get("arm", False)),
                   value=int(d.get("value", crsf.CHANNEL_MID)),
                   steps=int(d.get("steps", 3)),
                   buttons=tuple(d.get("buttons") or ()))

    def to_dict(self):
        out = {"src": self.src, "idx": self.idx, "inv": self.inv,
               "dev": self.dev, "arm": self.arm,
               "value": self.value, "steps": self.steps}
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
        self._switches = {}
        self._prev_buttons = {}
        self.throttle_dev = int(config.get("throttle", {}).get("dev", 0))
        self._last_t = None
        self.last_values = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS

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
        self._switches.clear()
        self._prev_buttons = {}
        self._last_t = None
        self.throttle.reset()
        self.last_values = self.failsafe_values()

    def failsafe_values(self):
        vals = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS
        for i, ch in enumerate(self.channels):
            if ch.src == "throttle":
                vals[i] = crsf.CHANNEL_MIN
            elif ch.src in ("button", "toggle", "cycle", "switch"):
                vals[i] = crsf.CHANNEL_MIN
            elif ch.src == "fixed":
                vals[i] = ch.value
        return vals

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
        self.last_values = vals
        return vals

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

    def armed_channels(self):
        """Channels that count as armed, for the display and the interlocks.

        A toggle or a cycle off its first position counts on its own, since
        that is what an arm switch normally is. Any other source counts once
        the channel is flagged as an arm channel, so arming from a
        three-position switch or a held button is caught by the same
        interlocks. The flagged test reads the value actually computed, so
        inv and the source's own rules are already accounted for.
        """
        out = []
        for i, ch in enumerate(self.channels):
            key = (ch.dev, ch.idx)
            armed = False
            if ch.src == "toggle":
                armed = self._toggles.get(key, False) != ch.inv
            elif ch.src == "cycle":
                armed = self._cycles.get(key, 0) != 0
            if not armed and ch.arm:
                armed = (i < len(self.last_values)
                         and self.last_values[i] > crsf.CHANNEL_MID)
            if armed:
                out.append(i + 1)
        return out
