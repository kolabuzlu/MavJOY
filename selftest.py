#!/usr/bin/env python3
"""Bench test with no hardware: runs the real link thread against a virtual
serial port, decodes what comes out and checks rate, framing and failsafe.

Works on Windows too. POSIX gets a pty; everywhere else (and on Windows)
the link talks to a loopback TCP socket through pyserial's ``socket://``
URL handler, which exercises the same pyserial read/write paths."""

import os
import json
import socket
import tempfile
import sys
import threading
import time

import serial

import math
import theme

import config as configmod
import crsf
import gamepad as gp
import link as linkmod


class _Wire:
    """A virtual serial port: whatever the link writes lands in .rx,
    and .inject() pushes bytes back at it."""

    def read(self, n=65536):
        raise NotImplementedError

    def close(self):
        pass


class _PtyWire(_Wire):
    def __init__(self):
        import pty
        self._master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        os.set_blocking(self._master, False)

    def read(self, n=65536):
        try:
            return os.read(self._master, n)
        except BlockingIOError:
            return b""

    def inject(self, data):
        os.write(self._master, data)


class _SocketWire(_Wire):
    """TCP loopback. The link connects as a client via socket://host:port."""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        host, port = self._srv.getsockname()
        self.port = f"socket://{host}:{port}"
        self._conn = None
        self._ready = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        conn, _ = self._srv.accept()
        conn.setblocking(False)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn = conn
        self._ready.set()

    def wait(self, timeout=5.0):
        if not self._ready.wait(timeout):
            raise RuntimeError("link never connected to the virtual port")

    def read(self, n=65536):
        if self._conn is None:
            return b""
        try:
            return self._conn.recv(n)
        except (BlockingIOError, InterruptedError):
            return b""

    def inject(self, data):
        self._conn.sendall(data)

    def close(self):
        for s in (self._conn, self._srv):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def _open_wire():
    """pty where it exists, TCP loopback otherwise (Windows)."""
    if hasattr(os, "openpty") and sys.platform != "win32":
        return _PtyWire(), False
    return _SocketWire(), True


class _FakePad:
    """A gamepad this test positions by hand, so a stick can be held still
    or moved at an exact moment. The sim pad sweeps on a timer, which is no
    use when the whole question is what a channel did across one event."""

    def __init__(self):
        self._axes = [0.0] * 8
        self.error = ""

    def set_axis(self, i, v):
        self._axes[i] = v

    @property
    def states(self):
        return {0: gp.InputState(axes=tuple(self._axes),
                                 buttons=(False,) * 12, hats=((0, 0),),
                                 timestamp=time.monotonic(),
                                 device_name="fake", connected=True)}

    def stop(self):
        pass


def _link_stats(lq):
    """A LINK_STATISTICS frame reporting this link quality both ways."""
    payload = bytes([0, 0, lq, 0, 0, 0, 0, 0, lq, 0])
    body = bytes([crsf.FRAMETYPE_LINK_STATISTICS]) + payload
    return (bytes([crsf.CRSF_SYNC_BYTE, len(body) + 1]) + body
            + bytes([crsf.crc8(body)]))


def _pump(wire, lk, seconds, lq):
    """Hold the reported link quality at `lq` for a while, and return the
    channels from the last RC frame the link actually wrote."""
    last = None
    t_end = time.time() + seconds
    while time.time() < t_end:
        wire.inject(_link_stats(lq))
        time.sleep(0.05)
        data = wire.read()
        if data:
            for _addr, ftype, payload in crsf.Parser().feed(data):
                if ftype == crsf.FRAMETYPE_RC_CHANNELS_PACKED:
                    last = crsf.unpack_rc_channels(payload)
    return last


def _check_rf_hold(wire, lk, mixer):
    """Losing the RF link must not hand control over when it comes back.

    The model cannot hear anything during an RF dropout, so whatever the
    sticks did meanwhile never reached it. If those positions went out the
    moment the link returned, a flight mode moved during the outage would
    take effect at once and the model would leave the failsafe it was
    holding. It has to stay there until the pilot moves that channel.

    This drives the real link thread over the real wire. Nothing here
    reimplements the decision under test - a reimplementation is free to be
    right while the app is wrong, which is exactly how this was missed.
    """
    print("\n-- RF dropout --")
    pad = _FakePad()
    lk.gamepad = pad

    # CH6 is held across a recovery; CH4 is a primary flight control and is
    # never held. Both read the same stick, so one run shows both.
    pad.set_axis(0, 0.0)                      # stick centred, link healthy
    before = _pump(wire, lk, 1.5, 100)
    assert before, "no RC frames while the link is up"
    assert 6 not in mixer.holding(), "CH6 was still frozen before the test"
    centred = before[5]
    print(f"   link up, stick centred:      CH6 = {centred}  CH4 = {before[3]}")

    _pump(wire, lk, 1.2, 0)                   # link down, and confirmed down
    pad.set_axis(0, 1.0)                      # pilot moves it while deaf
    during = _pump(wire, lk, 0.8, 0)
    print(f"   link down, stick moved:      CH6 = {during[5]}  "
          f"CH4 = {during[3]} (neither reaches the model)")
    assert during[5] != centred, "the test did not actually move the stick"
    moved = during[3]

    after = _pump(wire, lk, 1.5, 100)         # back on the air
    print(f"   link back, stick still over: CH6 = {after[5]}  "
          f"CH4 = {after[3]}")
    assert after[5] == centred, (
        f"the model would have been handed the new position: "
        f"CH6 came back as {after[5]}, not the {centred} it had when the "
        f"link dropped")
    assert 6 in mixer.holding(), "CH6 should be frozen after an RF recovery"

    # The whole point of the exemption: a pilot who has just got the link
    # back needs the sticks, so CH1-4 follow them at once.
    assert after[3] == moved, (
        f"CH4 is a primary flight control and must not be held: it came "
        f"back as {after[3]}, not the {moved} the stick is actually at")
    assert not ({1, 2, 3, 4} & set(mixer.holding())), (
        f"CH1-4 must never be frozen, got {mixer.holding()}")
    print(f"   ... CH4 followed the stick, and is not in {mixer.holding()}")

    pad.set_axis(0, -1.0)                     # pilot takes the channel back
    released = _pump(wire, lk, 1.0, 100)
    print(f"   pilot moves it:              CH6 = {released[5]} (released)")
    assert released[5] != centred, "the channel never released"
    assert 6 not in mixer.holding(), "CH6 should have released once moved"


def _check_oneway():
    """A one-way toggle latches high and refuses to come back on its own.

    The mixer is the whole of this behaviour, so it is driven directly. The
    point of the source is that a second press cannot undo the first, which
    means the test has to press more than once and insist nothing happened.
    """
    print("")
    print("-- one-way toggle --")
    cfg = configmod.default_config()
    cfg["channels"][0] = {"src": "axis", "idx": 0, "inv": False}
    cfg["channels"][1] = {"src": "oneway", "idx": 3, "inv": False,
                          "reset_ch": 1, "reset_move": 100}
    mixer = gp.Mixer(cfg)
    mixer.reset()

    def frame(axis=0.0, button=False):
        buttons = [False] * 8
        buttons[3] = button
        st = gp.InputState(axes=(axis, 0.0, 0.0, 0.0),
                           buttons=tuple(buttons), hats=((0, 0),),
                           timestamp=time.monotonic(),
                           device_name="fake", connected=True)
        return mixer.compute({0: st})[1]

    assert frame() == crsf.CHANNEL_MIN, "should start low"
    print(f"   at rest:            CH2 = {frame()}")

    frame(button=True)                      # press
    latched = frame(button=False)           # release
    print(f"   pressed:            CH2 = {latched}")
    assert latched == crsf.CHANNEL_MAX, "a press must latch it high"

    for _ in range(3):                      # and again, and again
        frame(button=True)
        frame(button=False)
    print(f"   pressed 3x more:    CH2 = {frame()}")
    assert frame() == crsf.CHANNEL_MAX, "a second press must NOT bring it back"

    frame(axis=0.0)                         # baseline for the reset watcher
    frame(axis=1.0)                         # move the watched channel
    after = frame(axis=1.0)
    print(f"   reset channel moved: CH2 = {after}")
    assert after == crsf.CHANNEL_MIN, "the reset channel must bring it back"

    # The watched channel has to stay put: a reset fires on its movement,
    # and moving it back in the same frame as the press would clear the
    # latch the press just set.
    frame(axis=1.0, button=True)
    again = frame(axis=1.0, button=False)
    print(f"   pressed again:      CH2 = {again}")
    assert again == crsf.CHANNEL_MAX, "it must latch again after a reset"


def _check_arm_is_ch5():
    """Only CH5 arms. A latch on any other channel is just a latch.

    The interlock used to infer "armed" from the source type, so a flight
    mode latched high on CH6 - or a one-way on CH7 - announced itself as an
    arm channel and then blocked every settings write.
    """
    print("")
    print("-- arm is CH5 --")
    cfg = configmod.default_config()
    cfg["channels"][4] = {"src": "toggle", "idx": 1, "inv": False}   # CH5
    cfg["channels"][5] = {"src": "toggle", "idx": 2, "inv": False}   # CH6
    cfg["channels"][6] = {"src": "oneway", "idx": 3, "inv": False}   # CH7
    mixer = gp.Mixer(cfg)
    mixer.reset()

    def frame(*down):
        buttons = [False] * 8
        for b in down:
            buttons[b] = True
        st = gp.InputState(axes=(0.0,) * 4, buttons=tuple(buttons),
                           hats=((0, 0),), timestamp=time.monotonic(),
                           device_name="fake", connected=True)
        mixer.compute({0: st})
        return mixer.armed_channels()

    assert frame() == [], "nothing pressed must not read as armed"

    frame(2)
    frame()                      # CH6 toggle now latched high
    frame(3)
    frame()                      # CH7 one-way now latched high
    other = frame()
    print(f"   CH6 and CH7 latched high: armed = {other}")
    assert other == [], f"only CH5 may count as armed, got {other}"

    frame(1)
    armed = frame()              # CH5 toggle now latched high
    print(f"   CH5 latched high:         armed = {armed}")
    assert armed == [5], f"CH5 high must read as armed, got {armed}"


def _check_arm_from_model():
    """Armed comes from the model, and says nothing when it cannot tell.

    CRSF carries no armed frame. The convention is a star appended to the
    flight mode while disarmed, and ArduPilot only does that with
    RC_OPTIONS bit 12 set - so a mode with no star, before any star has
    ever been seen, means nothing at all and must not read as DISARMED.
    """
    print("")
    print("-- armed, as the model reports it --")
    w = crsf.ArmWatch()

    assert w.feed(None) is None, "no telemetry cannot mean disarmed"
    first = w.feed(crsf.parse_flight_mode(b"FBWA" + bytes([0])))
    print(f"   FBWA  (no star ever seen): {first}")
    assert first is None, "a mode with no star proves nothing on its own"

    disarmed = w.feed(crsf.parse_flight_mode(b"FBWA*" + bytes([0])))
    print(f"   FBWA* (star):              {disarmed}")
    assert disarmed is False, "a star means disarmed"

    armed = w.feed(crsf.parse_flight_mode(b"AUTO" + bytes([0])))
    print(f"   AUTO  (star seen before):  {armed}")
    assert armed is True, "no star, once the marker is known, means armed"

    assert w.feed(None) is None, "losing telemetry must not read as armed"

    # INAV says it differently. Its armed branch is checked first and always
    # replaces the string with a real mode, so OK, WAIT and !ERR are
    # reachable only on the ground - and it appends no star, which is why
    # reading it with ArduPilot's rules leaves the arm state unknown for
    # ever.
    print("   -- as INAV --")
    inav = crsf.ArmWatch("inav")
    for mode, want in (("OK", False), ("WAIT", False), ("!ERR", False),
                       ("ANGL", True), ("RTH", True), ("MANU", True)):
        got = inav.feed(crsf.parse_flight_mode(mode.encode() + bytes([0])))
        print(f"   {mode:6} -> {{True: 'ARMED', False: 'DISARMED'}}[got]"
              .replace("{True: 'ARMED', False: 'DISARMED'}[got]",
                       "ARMED" if got else "DISARMED"))
        assert got is want, f"INAV {mode} should be {want}, got {got}"
    assert inav.feed(None) is None, "no telemetry is still unknown"

    # And the same strings under ArduPilot's rules must NOT be read as
    # disarmed: there, a name without a star means nothing on its own.
    assert crsf.ArmWatch("ardupilot").feed(
        crsf.parse_flight_mode(b"OK" + bytes([0]))) is None, (
        "OK is an INAV convention and must not be read as one elsewhere")


def _check_latch_memory():
    """Latching channels come back where they were left, CH1-4 excepted.

    The round trip goes through the config file itself, because "remembered
    across a restart" is a claim about what survives being written to disk
    and read back, not about two objects in one process.
    """
    print("")
    print("-- remembering latches --")
    cfg = configmod.default_config()
    cfg["channels"][0] = {"src": "toggle", "idx": 1}            # CH1, exempt
    cfg["channels"][4] = {"src": "toggle", "idx": 2}            # CH5
    cfg["channels"][5] = {"src": "oneway", "idx": 3}            # CH6
    cfg["channels"][6] = {"src": "cycle", "idx": 4, "steps": 3}  # CH7

    m = gp.Mixer(cfg)
    m.reset()
    m._toggles[(0, 1)] = True          # CH1: must NOT be remembered
    m._toggles[(0, 2)] = True
    m._oneway[(0, 3)] = True
    m._cycles[(0, 4)] = 2

    path = os.path.join(tempfile.gettempdir(), "mavjoy_latch_test.json")
    cfg["latches"] = m.latch_state()
    configmod.save(cfg, path)
    reloaded, _warn = configmod.load(path)
    os.unlink(path)
    print(f"   written and read back: {reloaded['latches']}")
    assert "1" not in reloaded["latches"], "CH1 must never be remembered"

    back = gp.Mixer(cfg)
    back.reset()
    restored = back.restore_latches(reloaded["latches"])
    print(f"   restored:              {restored}")
    print(f"   values:                {back.latched_values()}")
    assert restored == [5, 6, 7], f"expected CH5-7, got {restored}"
    assert back.latched_values()[5] == crsf.CHANNEL_MAX
    assert back.latched_values()[6] == crsf.CHANNEL_MAX
    assert back._cycles[(0, 4)] == 2, "a cycle must come back on its position"

    # Remap CH5 to something else: its old state is not about that control
    # any more, so it must be dropped rather than applied to what took over.
    moved = configmod.default_config()
    moved["channels"][4] = {"src": "axis", "idx": 2}
    other = gp.Mixer(moved)
    other.reset()
    kept = other.restore_latches(reloaded["latches"])
    print(f"   after remapping CH5:   {kept}")
    assert 5 not in kept, "a remapped channel must not take the old latch"


def _check_endpoints():
    """Endpoints scale the output, hold centre, and do not compound.

    The compounding case is the one worth testing: the scaled value is what
    goes on the wire, but the full-travel value is what the next frame
    reasons from. Confuse the two and a channel holding its previous value
    gets scaled again every frame, creeping toward centre a few
    microseconds at a time - slowly enough to look like drift rather than a
    bug.
    """
    print("")
    print("-- output endpoints --")
    cfg = configmod.default_config()
    cfg["channels"][0] = {"src": "axis", "idx": 0,
                          "out_min": 1100, "out_max": 1900}
    # The ends of the range are the flight controller's numbers, worked out
    # its way: mult 5, div 8, offset 880, integer division. Anything else
    # reads a microsecond out at both ends and the two cannot be compared.
    assert crsf.crsf_to_us(crsf.CHANNEL_MIN) == 987
    assert crsf.crsf_to_us(crsf.CHANNEL_MID) == 1500
    assert crsf.crsf_to_us(crsf.CHANNEL_MAX) == 2011
    for us in (987, 1000, 1500, 1900, 2011):
        assert crsf.crsf_to_us(crsf.us_to_crsf(us)) == us, (
            f"{us} us does not survive the round trip")
    cfg["channels"][1] = {"src": "axis", "idx": 0}        # left at full travel
    m = gp.Mixer(cfg)
    m.reset()

    def frame(axis=0.0, connected=True):
        st = gp.InputState(axes=(axis, 0.0, 0.0, 0.0), buttons=(False,) * 8,
                           hats=((0, 0),), timestamp=time.monotonic(),
                           device_name="fake", connected=connected)
        return m.compute({0: st})

    for axis, want in ((-1.0, 1100), (0.0, 1500), (1.0, 1900)):
        v = frame(axis)
        got, plain = crsf.crsf_to_us(v[0]), crsf.crsf_to_us(v[1])
        print(f"   stick {axis:+.0f}: CH1 = {got:4.0f} us   "
              f"CH2 (full travel) = {plain:4.0f} us")
        assert abs(got - want) < 2, f"CH1 should be {want}, got {got:.0f}"

    assert abs(crsf.crsf_to_us(frame(0.0)[0]) - 1500) < 2, (
        "centre must not move when an endpoint does")

    # And the midpoint moves centre without touching either end.
    trimmed = configmod.default_config()
    trimmed["channels"][0] = {"src": "axis", "idx": 0, "out_min": 1100,
                              "out_mid": 1550, "out_max": 1900}
    t = gp.Mixer(trimmed)
    t.reset()
    for axis, want in ((-1.0, 1100), (0.0, 1550), (1.0, 1900)):
        st = gp.InputState(axes=(axis, 0.0, 0.0, 0.0), buttons=(False,) * 8,
                           hats=((0, 0),), timestamp=time.monotonic(),
                           device_name="fake", connected=True)
        got = crsf.crsf_to_us(t.compute({0: st})[0])
        print(f"   mid 1550, stick {axis:+.0f}: {got:4.0f} us")
        assert abs(got - want) < 2, f"expected {want}, got {got}"

    # A midpoint outside the ends would run half the throw backwards, so it
    # is held between them.
    silly = configmod.default_config()
    silly["channels"][0] = {"src": "axis", "idx": 0, "out_min": 1100,
                            "out_mid": 1990, "out_max": 1900}
    assert gp.Mixer(silly).channels[0].mid_units == crsf.us_to_crsf(1900), (
        "a midpoint past the top end must be held at it")
    print("   a midpoint outside the ends is held between them")

    frame(1.0)
    for _ in range(200):
        held = m.compute({0: gp.InputState(connected=False,
                                           timestamp=time.monotonic())})
    crept = crsf.crsf_to_us(held[0])
    print(f"   200 frames with the device gone: {crept:.0f} us")
    assert abs(crept - 1900) < 2, f"the value crept to {crept:.0f} us"


def _check_rate_warning():
    """The rate warning gives advice that is true for the baud in use.

    It used to tell everyone to raise the baud to 921600 - including people
    already at 921600, where the baud is not the limit at all: MavJOY's own
    ceiling is, and only a slower packet rate buys the margin back.
    """
    import app as appmod

    print("")
    print("-- rate warning --")
    for baud in (115200, 400000, 921600):
        for req in (100, 150, 250, 333, 500, 1000):
            want = crsf.recommended_crsf_rate(req, baud)
            if want >= req * 2:
                continue
            msg = appmod.App._rate_headroom_warning(req, want, baud)
            baud_limited = crsf.recommended_crsf_rate(0, baud) < \
                crsf.recommended_crsf_rate(0, 10 ** 9)
            says_raise = "Raise the baud" in msg
            assert says_raise == baud_limited, (
                f"at {baud} baud asking {req} Hz the message "
                f"{'says' if says_raise else 'does not say'} to raise the "
                f"baud, but the baud {'is' if baud_limited else 'is not'} "
                f"the limit: {msg}")
    print("   raise the baud: said at 115200, never at 400000 or 921600")

    # The case the pilot actually flies must stay silent: 100Hz Full at
    # 921600 is three times oversampled and there is nothing to warn about.
    assert crsf.recommended_crsf_rate(100, 921600) == 300
    print("   100 Hz asked at 921600: sends 300, no warning")


def _check_config_writes():
    """Only Save and Import may write the live configuration to disk.

    self.cfg is live: the channel widgets write into it as they are
    touched, long before anyone presses Save. So any other action that
    writes the whole of it commits a mapping that was only being tried
    out - possibly an arm channel - because the user did something
    unrelated in another tab. Picking a layout file in the Module prep
    tab did exactly that.

    Checked by reading app.py rather than by calling anything, because
    calling it would write over the real config.json sitting next to this
    file - load() and save() bind their default path at import.
    """
    import ast

    print("")
    print("-- configuration writes --")
    ALLOWED = {"save_config", "import_config_file"}

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "app.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    def writes_live_cfg(node):
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            if not (isinstance(f, ast.Attribute) and f.attr == "save"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "configmod"):
                continue
            for arg in call.args:
                if (isinstance(arg, ast.Attribute) and arg.attr == "cfg"
                        and isinstance(arg.value, ast.Name)
                        and arg.value.id == "self"):
                    return True
        return False

    offenders = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef) and writes_live_cfg(fn):
                if fn.name not in ALLOWED:
                    offenders.append(f"{fn.name} (line {fn.lineno})")
                else:
                    print(f"   {fn.name}: allowed, it is the explicit action")
    assert not offenders, (
        "these write the live self.cfg and should re-read from disk and "
        f"patch one key instead: {', '.join(offenders)}")
    print(f"   nothing else writes the live configuration")


def _check_map():
    """The moving map: its projection, and what it refuses to plot."""
    import mapview as mv

    print("")
    print("-- map --")

    # tkinter is imported here, not at the top, and the root is built
    # before the try. This file promises a bench test with no hardware,
    # and it was true of a display too until the map arrived: on a
    # headless machine tk.Tk() raises and would take the whole run down
    # with it, including the failsafe checks, which are the ones least
    # worth skipping quietly.
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        print(f"   skipped, no display: {exc}")
        return

    # The projection has to be the real Web Mercator one, or the aircraft
    # sits over the wrong piece of ground - which is worse than no map,
    # because it looks authoritative. Checked against the formulation on
    # the OSM wiki, written a different way.
    def reference(lat, lon, z):
        n = 2 ** z
        r = math.radians(lat)
        return ((lon + 180.0) / 360.0 * n,
                (1.0 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2.0 * n)

    for lat, lon in ((51.5007, -0.1246), (39.9334, 32.8597),
                     (-33.8568, 151.2153), (0.0, 0.0)):
        for z in (12, 15, 17):
            px, py = mv.deg2px(lat, lon, z)
            rx, ry = reference(lat, lon, z)
            assert abs(px / mv.TILE_SIZE - rx) < 1e-9, f"x wrong at {lat},{lon} z{z}"
            assert abs(py / mv.TILE_SIZE - ry) < 1e-9, f"y wrong at {lat},{lon} z{z}"
    print("   projection matches the reference at 12 points")

    # One degree of latitude is about 111 km anywhere.
    d, b = mv.distance_bearing(39.0, 32.0, 40.0, 32.0)
    assert 110000 < d < 112000, f"a degree of latitude came out {d:.0f} m"
    assert b < 0.01 or b > 359.99, f"due north came out as {b:.1f} deg"
    d2, b2 = mv.distance_bearing(39.0, 32.0, 39.0, 33.0)
    assert 86000 < d2 < 87500, f"a degree of longitude at 39N came out {d2:.0f} m"
    assert 89 < b2 < 91, f"due east came out as {b2:.1f} deg"
    print(f"   1 deg north {d/1000:.1f} km bearing {b:.0f}, "
          f"1 deg east {d2/1000:.1f} km bearing {b2:.0f}")

    root.withdraw()
    try:
        m = mv.MapView(root, theme.DARK,
                       os.path.join(tempfile.gettempdir(), "mavjoy_map_test"))
        m.tiles.enabled = False          # no network from the selftest
        root.update()

        # A receiver with no fix reports 0,0 - which is in the Atlantic.
        # Plotting it would draw the aircraft off Africa and, worse, set
        # home there, so every distance afterwards would be nonsense.
        m.update_position({"lat": 0.0, "lon": 0.0, "sats": 0})
        assert m.origin is None and m.pos is None, "0,0 must not be plotted"
        print("   a 0,0 'fix' is refused")

        m.update_position({"lat": 39.9334, "lon": 32.8597, "heading": 90,
                           "altitude_m": 100, "speed_kmh": 60, "sats": 12})
        assert m.origin == m.pos, "the marker should be set from the first real fix"
        for i in range(1, 12):
            m.update_position({"lat": 39.9334 + i * 0.001, "lon": 32.8597,
                               "heading": 0, "sats": 12})
        assert len(m.trail) == 12, f"the trail should have 12 points, has {len(m.trail)}"
        dist, _ = mv.distance_bearing(*m.origin, *m.pos)
        assert 1200 < dist < 1250, f"11 * 0.001 deg should be ~1225 m, got {dist:.0f}"
        print(f"   marker set from the first fix, trail {len(m.trail)} points, "
              f"{dist:.0f} m out")

        # The view is centred between home and the aircraft, so the ground
        # it must cover is the gap between them - and it has to be zoomed
        # in far enough that a small circuit is not a dot.
        near = m._pick_zoom(39.93, 400)
        far = m._pick_zoom(39.93, 6000)
        assert near > far, "a closer aircraft must give a closer zoom"
        assert mv.MIN_ZOOM <= far and near <= mv.MAX_ZOOM
        across = mv.metres_per_pixel(39.93, near) * min(m._size())
        assert across < 400 * 4, \
            f"400 m out should not be shown across {across:.0f} m of ground"
        print(f"   zoom {near} at 400 m ({across:.0f} m across), {far} at 6 km")

        m.reset_marker()
        assert m.origin == m.pos and not m.trail, (
            "Reset marker must recentre and clear the trail")
        print("   Reset marker recentres and clears the trail")

        # Longitude is a circle cut at the antimeridian, and a plain
        # average of two of them lands half a world from both. The
        # distance was always right - haversine does not care - so the
        # zoom stayed tight while the centre was wrong, and the map came
        # up empty with everything projected billions of pixels away.
        assert abs(mv.mid_lon(179.9, -179.9) - 180.0) < 1e-9 or \
            abs(mv.mid_lon(179.9, -179.9) + 180.0) < 1e-9, \
            f"the dateline midpoint came out {mv.mid_lon(179.9, -179.9)}"
        assert abs(mv.mid_lon(10.0, 20.0) - 15.0) < 1e-9, "ordinary case moved"
        assert abs(mv.wrap_lon(-179.9, 179.9) - 180.1) < 1e-9, \
            "wrap_lon must carry a point past the cut, not back around it"

        m.tiles.enabled = False
        m.origin = (17.0, 179.9)
        m.pos = (17.0, -179.9)
        m.trail.clear()
        m.trail.append(m.pos)
        m.draw()
        w, h = m._size()
        placed = [m.canvas.coords(i) for i in m.canvas.find_all()
                  if m.canvas.type(i) == "polygon"]
        assert placed, "the aircraft was not drawn at all across the dateline"
        xs = placed[0][0::2]
        assert all(-w < x < 2 * w for x in xs), (
            f"the aircraft landed at x={xs[:2]} on a {w}px canvas")
        d, _b = mv.distance_bearing(*m.origin, *m.pos)
        print(f"   across the antimeridian: {d/1000:.1f} km apart, "
              f"aircraft drawn at x={xs[0]:.0f} on a {w}px canvas")

        # The marker waits for a real fix. A receiver reports positions
        # long before it has one, and those can be tens of metres out -
        # and the marker is what every bearing is measured from. HITL
        # never exercises this: a simulator hands over a perfect fix.
        w = mv.MapView(root, theme.DARK,
                       os.path.join(tempfile.gettempdir(), "mavjoy_map_test"))
        w.tiles.enabled = False
        strip = (39.9334, 32.8597)
        for sats in (3, 4, 5):
            w.update_position({"lat": strip[0] + 0.0006, "lon": strip[1],
                               "altitude_m": 915, "sats": sats})
        assert w.origin is None, "the marker must not set from a 3-5 satellite fix"
        assert w.pos is not None, "the aircraft should still be drawn meanwhile"
        assert not w.trail, "the warm-up wander must not be drawn as a track"
        w.update_position({"lat": strip[0], "lon": strip[1],
                           "altitude_m": 890, "sats": mv.MIN_SATS_FOR_MARKER})
        assert w.origin == strip, f"the marker should set on the strip, got {w.origin}"
        assert w.origin_alt == 890, "the marker should take the settled altitude"
        print(f"   marker ignored 3-5 satellites 67 m out, set at "
              f"{mv.MIN_SATS_FOR_MARKER} on the strip")

        # Height above home, the same whichever firmware is flying. The
        # GPS frame cannot give that directly: ArduPilot puts sea-level
        # altitude in it and INAV height above arming. Their baro frames
        # agree, so that wins when it is arriving; failing that, GPS
        # altitude is taken from the marker's, which cancels either zero.
        w.update_position({"lat": strip[0] + 0.002, "lon": strip[1],
                           "altitude_m": 965, "sats": 14})
        got = w.altitude()
        assert got == 75, f"ArduPilot, no baro: 965 - 890 should be 75, got {got}"
        w.update_baro({"altitude_m": 74.6, "_t": time.monotonic()})
        assert w.altitude() == 74.6, "a fresh baro altitude should be used"
        w._baro_t = time.monotonic() - (mv.BARO_FRESH_S + 1)
        assert w.altitude() == 75, "a stale baro altitude should give way to GPS"
        print("   altitude: baro when fresh, GPS above the marker when not")

        i = mv.MapView(root, theme.DARK,
                       os.path.join(tempfile.gettempdir(), "mavjoy_map_test"))
        i.tiles.enabled = False
        i.update_position({"lat": strip[0], "lon": strip[1], "altitude_m": 0,
                           "sats": 9})
        i.update_position({"lat": strip[0] + 0.002, "lon": strip[1],
                           "altitude_m": 75, "sats": 14})
        assert i.altitude() == 75, f"INAV, no baro: 75 - 0 should be 75, got {i.altitude()}"
        print("   965 m above the sea and 75 m above arming both read 75 m")

        # Pressing Reset marker is the pilot saying where to measure from;
        # the satellite count has no business overruling that.
        r = mv.MapView(root, theme.DARK,
                       os.path.join(tempfile.gettempdir(), "mavjoy_map_test"))
        r.tiles.enabled = False
        r.update_position({"lat": strip[0], "lon": strip[1], "altitude_m": 900,
                           "sats": 3})
        assert r.origin is None
        r.reset_marker()
        assert r.origin == strip and r.origin_alt == 900, \
            "Reset marker must be honoured whatever the satellite count"
        print("   Reset marker is honoured with 3 satellites")

        # An unbounded tile cache is a leak in a program left open for a
        # day of flying: every new zoom and position adds images nothing
        # ever removes.
        store = mv.TileStore(os.path.join(tempfile.gettempdir(), "mavjoy_lru"))
        for i in range(mv.MAX_TILES_IN_MEMORY + 40):
            store._remember((15, i, 0), f"tile{i}")
        assert len(store.images) == mv.MAX_TILES_IN_MEMORY, (
            f"the cache held {len(store.images)} tiles, cap is "
            f"{mv.MAX_TILES_IN_MEMORY}")
        assert (15, 0, 0) not in store.images, "the oldest tile should have gone"
        assert (15, mv.MAX_TILES_IN_MEMORY + 39, 0) in store.images, \
            "the newest tile should have stayed"
        print(f"   tile cache capped at {mv.MAX_TILES_IN_MEMORY}, oldest evicted")

        # forget_failures must forget only the failures. Clearing _asked
        # as well re-queued tiles that were still in flight.
        store._failed.add((15, 1, 1))
        store._asked.update({(15, 1, 1), (15, 2, 2)})
        store.forget_failures()
        assert store._failed == set(), "failures should be forgotten"
        assert store._asked == {(15, 2, 2)}, (
            f"only the failed tile should be re-askable, got {store._asked}")
        print("   forget_failures drops the failures and keeps the pending")
    finally:
        root.destroy()


def _check_module_prep():
    """The TX module tab's logic, as far as it goes without a module.

    Flashing needs hardware, but everything that decides WHAT gets flashed
    does not, and that is the half worth guarding: a layout built wrongly
    would be written to a module that then comes up with no radio.
    """
    import module_prep as mp

    print("")
    print("-- module prep --")

    # Skipped rather than fatal when the flashing tools are absent.
    # They are needed only by the Module prep tab, and a machine set up
    # from requirements.txt alone would otherwise abort the whole run
    # here - before the failsafe checks below it.
    try:
        import esptool  # noqa: F401
        from littlefs import LittleFS  # noqa: F401
    except ImportError as exc:
        print(f"   skipped: {exc}")
        print("   install with: pip install esptool littlefs-python")
        return

    stock = {
        "serial_rx": 13, "serial_tx": 13,
        "radio_miso": 19, "radio_mosi": 23, "radio_sck": 18, "radio_nss": 5,
        "screen_type": 1, "misc_fan_en": 17, "power_values": [-18, -15, 2],
        "use_backpack": True,
        "debug_backpack_baud": 460800,
        "debug_backpack_rx": 3, "debug_backpack_tx": 1,
    }
    path = os.path.join(tempfile.gettempdir(), "mavjoy_layout_test.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(stock, fh)

    out = mp.resolve_layout(path)
    print(f"   {len(stock)} fields in -> {len(out)} out")
    assert out["serial_rx"] == 3 and out["serial_tx"] == 1, \
        "CRSF must move onto UART0"
    assert out["use_backpack"] is False, "the backpack must be switched off"
    for key in mp.PREPARE_DROPS:
        assert key not in out, f"{key} should have been dropped"
    # Everything that is not ours must survive: hardware.json replaces the
    # built-in layout, so a field lost here is a module with no radio.
    for key in ("radio_miso", "radio_mosi", "radio_sck", "radio_nss",
                "screen_type", "misc_fan_en", "power_values"):
        assert out[key] == stock[key], f"{key} must be carried across"
    print("   radio, screen, fan and power table carried across untouched")

    # A layout with no radio pins is not a layout, and saying so here is
    # much cheaper than finding out after it is on the module.
    for body, why in (({"hello": 1}, "not a layout"),
                      ([1, 2, 3], "not an object"),
                      ("nonsense", "not JSON at all")):
        with open(path, "w", encoding="utf-8") as fh:
            if body == "nonsense":
                fh.write("nonsense")
            else:
                json.dump(body, fh)
        try:
            mp.resolve_layout(path)
            raise AssertionError(f"{why} should have been refused")
        except mp.PrepError as exc:
            print(f"   refused {why}: {str(exc)[:44]}...")
    os.unlink(path)

    # The image has to be mountable, or the module reformats it and keeps
    # its built-in layout - the change silently not taking.
    size = 0x20000
    image, payload = mp.build_image(out, size)
    assert len(image) == size, "the image must fill the partition"
    from littlefs import LittleFS
    fs = LittleFS(block_size=mp.LFS_BLOCK_SIZE, block_count=size // mp.LFS_BLOCK_SIZE,
                  read_size=mp.LFS_READ_SIZE, prog_size=mp.LFS_PROG_SIZE,
                  name_max=mp.LFS_NAME_MAX, disk_version=mp.LFS_DISK_VERSION,
                  mount=False)
    fs.context.buffer = bytearray(image)
    fs.mount()
    with fs.open("/hardware.json", "rb") as fh:
        back = json.loads(fh.read().decode("utf-8"))
    assert back == out, "the layout must survive the filesystem image"
    print(f"   {len(image)} byte image mounts and reads back identical")

    # esptool's progress meter redraws one line with a carriage return. It
    # was reaching the log twice over: once per redraw, and once more at
    # the end, because flush did not filter what write did.
    lines = []
    tee = mp._Tee(lines.append)
    tee.write("Connecting...\n")
    tee.write("Reading from 0x00009000\n")
    tee.write("====>  50.0% 2.00kB/4.00kB [1s]\r")
    tee.write("=========> 100.0% 4.00kB/4.00kB [2s]")
    tee.flush()
    tee.write("Read 4096 bytes from 0x00008000\n")
    tee.flush()
    assert lines == ["Connecting...", "Read 4096 bytes from 0x00008000"], \
        f"the meter must be filtered on write and on flush, got {lines}"
    print("   progress meter filtered, summary lines kept")

    # esptool 5 warns on every deprecated spelling; esptool 4 knows only
    # those. Either way the command has to be one the installed one likes.
    import esptool
    major = int(str(esptool.__version__).split(".")[0])
    want = "read-flash" if major >= 5 else "read_flash"
    assert mp._command_name("read_flash") == want, \
        f"esptool {major} wants {want}"
    print(f"   esptool {esptool.__version__}: uses {want!r}")

    # Progress comes from overriding esptool's own progress_bar, because
    # its drawn meter appears only when rich believes it is writing to a
    # terminal - true enough from source, false in a windowed build, where
    # a twelve second read would otherwise look like a hang. What matters
    # here is that the swap goes in and comes back out: a logger left
    # installed would keep every later esptool run reporting into a dead
    # callback.
    # Asked through the proxy, which is where esptool itself looks. The
    # obvious spelling, EsptoolLogger.instance, is a different attribute
    # that set_logger never updates, and reading it says nothing changed
    # while the swap is plainly working.
    from esptool.logger import log as esplog
    live = lambda: type(esplog.progress_bar.__self__).__name__
    before = live()
    undo = mp._install_progress(lambda *a: None, lambda _s: None)
    during = live()
    undo()
    after = live()
    assert during != before, "the progress logger did not go in"
    assert after == before, \
        f"the original logger was not put back ({before} -> {after})"
    print(f"   progress logger installs and restores "
          f"({before} -> {during} -> {after})")


def _check_throttle_cut():
    """The cut button drops the throttle to idle in every throttle mode.

    It only ever worked in ramp mode. The zero was written before the mode
    was looked at, but only ramp returned on it, so trigger and axis - both
    of which recompute from the live control - overwrote it on the next
    line and the button did nothing, with the Throttle tab still calling it
    an instant drop to idle. Ramp is the shipped default, which is why this
    went unnoticed: the mode most people fly was the one that worked.
    """
    print("")
    print("-- throttle cut --")
    # "Fully open" is a different stick position in each mode, so this is
    # not one tuple for all three. All of them read axis 5, the right
    # trigger; ramp also reads axis 2 as the down demand, which has to be
    # left at rest or it cancels the up; and axis mode is reversed by
    # default, so there full open is the other end of the same travel.
    OPEN = {"trigger": {5: 1.0, 2: -1.0},
            "ramp":    {5: 1.0, 2: -1.0},
            "axis":    {5: -1.0, 2: -1.0}}

    def state(mode, cut):
        axes = [0.0] * 8
        for idx, value in OPEN[mode].items():
            axes[idx] = value
        buttons = [False] * 16
        buttons[6] = cut                      # button 6 = Back = the cut
        return gp.InputState(axes=tuple(axes), buttons=tuple(buttons),
                             hats=((0, 0),), timestamp=time.monotonic(),
                             connected=True)

    for mode in gp.THROTTLE_MODES:
        cfg = configmod.default_config()["throttle"]
        cfg["mode"] = mode
        eng = gp.ThrottleEngine(cfg)

        # Wind it up first, so the cut has something to cut.
        for _ in range(60):
            eng.update(state(mode, cut=False), 0.05)
        opened = eng.value
        cut = eng.update(state(mode, cut=True), 0.05)
        print(f"   {mode:8s} open {opened * 100:5.1f} %   cut {cut * 100:5.1f} %")
        assert opened > 0.5, f"{mode}: the throttle should be open before the cut"
        assert cut == 0.0, f"{mode}: the cut button must drop the throttle to idle"

        # Still idle while it is held, and only then handed back.
        assert eng.update(state(mode, cut=True), 0.05) == 0.0, \
            f"{mode}: the throttle must stay down while the cut is held"
        assert eng.update(state(mode, cut=False), 0.05) > 0.0, \
            f"{mode}: releasing the cut must give the throttle back"
    print("   held down it stays at idle, released it comes back")


def _check_guarded_reset():
    """A reset channel cannot throw a guarded latch either.

    The guard was checked where the button is read and nowhere else, so a
    channel carrying both a guard and a reset could still be flipped by the
    watched control on its own - the exact accident the guard is there to
    prevent, arriving by the one route that was not watched.
    """
    print("")
    print("-- guarded reset channel --")
    cfg = configmod.default_config()
    cfg["channels"][0] = {"src": "axis", "idx": 0, "inv": False}      # watched
    cfg["channels"][1] = {"src": "oneway", "idx": 3, "inv": False,    # guarded
                          "guard": 5, "reset_ch": 1, "reset_move": 100}
    cfg["channels"][2] = {"src": "oneway", "idx": 4, "inv": False,    # not guarded
                          "reset_ch": 1, "reset_move": 100}
    m = gp.Mixer(cfg)
    m.reset()

    def frame(axis=0.0, *down):
        b = [False] * 16
        for i in down:
            b[i] = True
        st = gp.InputState(axes=(axis, 0.0, 0.0, 0.0, 0.0), buttons=tuple(b),
                           hats=((0, 0),), timestamp=time.monotonic(),
                           connected=True)
        v = m.compute({0: st})
        return v[1], v[2]

    # Latch both high. The guarded one needs its guard held to accept the
    # press; the plain one does not.
    frame(0.0, 5, 3, 4)
    guarded, plain = frame(0.0)
    assert guarded == crsf.CHANNEL_MAX and plain == crsf.CHANNEL_MAX, \
        "both channels should be latched high to start"
    print(f"   latched:                    CH2 = {guarded}   CH3 = {plain}")

    # Sweep the watched channel with no guard held. The unguarded latch is
    # meant to fall - that is the way back the source is built around - and
    # the guarded one is meant to sit exactly where it is.
    for axis in (1.0, -1.0, 1.0):
        guarded, plain = frame(axis)
    print(f"   swept, guard released:      CH2 = {guarded}   CH3 = {plain}")
    assert plain == crsf.CHANNEL_MIN, "an unguarded reset must still work"
    assert guarded == crsf.CHANNEL_MAX, \
        "a reset must not throw a guarded latch with the guard released"

    # Press the guard with the watched control left exactly where the sweep
    # above put it. Nothing has moved since, so nothing may fire: the travel
    # that happened while the guard was off must not be banked up and spent
    # the moment it goes down.
    guarded, _ = frame(1.0, 5)
    print(f"   guard pressed, no new move: CH2 = {guarded}")
    assert guarded == crsf.CHANNEL_MAX, \
        "holding the guard must not reset on movement made before it was held"

    # Moving it with the guard held is the way back.
    guarded, _ = frame(-1.0, 5)
    print(f"   swept with guard held:      CH2 = {guarded}")
    assert guarded == crsf.CHANNEL_MIN, \
        "with the guard held, the reset channel must work"


def _check_hold_snapshot():
    """holding() survives the link thread releasing channels under it.

    holding() is the one thing the GUI thread reads out of the mixer while
    the link thread owns it, and _apply_hold deletes from the same dict as
    each channel is taken back. Walking it without a copy raised
    "dictionary changed size during iteration" - caught by the guard around
    the display tick, so the cost was a dropped frame and an error in the
    status line, at the moment after a failsafe when the pilot is reading
    that line to see what is still held.
    """
    print("")
    print("-- holding() across threads --")
    size, reads = 1000, 3000
    mixer = gp.Mixer(configmod.default_config())
    mixer._held = {i: 1000 for i in range(size)}
    mixer._hold_ref = {}

    stop = threading.Event()

    def churn():                      # stands in for the link thread
        while not stop.is_set():
            for i in range(size):
                mixer._held[i] = 1000
            for i in list(mixer._held):
                del mixer._held[i]

    # Left to itself this reproduced the fault about two runs in three,
    # which is no guard at all - a third of the time it would wave the bug
    # straight through. Cutting the switch interval makes the interpreter
    # change threads often enough to catch it every time: measured 20 runs
    # out of 20 against the unfixed code, and 0 out of 20 against this one.
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    try:
        for _ in range(reads):
            assert all(isinstance(n, int) for n in mixer.holding())
    finally:
        stop.set()
        worker.join(timeout=2)
        sys.setswitchinterval(interval)
    print(f"   {reads} reads while channels were being released: no error")


def _check_config_file():
    """A configuration survives a trip through a file, minus the latches.

    The point of exporting is that another machine ends up set up the same
    way, so the test is a round trip rather than a look at what was
    written. Latches are the exception and are checked for by name: they
    say where the controls were left, and carrying an armed channel to
    another machine because it was armed here is the one thing this must
    not do.
    """
    print("")
    print("-- configuration files --")
    cfg = configmod.default_config()
    cfg["baud"] = 921600
    cfg["rate_hz"] = 333
    cfg["channels"][4] = {"src": "toggle", "idx": 7, "inv": False}
    cfg["channels"][5] = {"src": "oneway", "idx": 3, "out_max": 1900,
                          "reset_ch": 1, "reset_move": 120}
    cfg["throttle"]["mode"] = "ramp"
    cfg["firmware"] = "inav"
    cfg["latches"] = {"5": {"src": "toggle", "state": True}}

    path = os.path.join(tempfile.gettempdir(), "mavjoy_export_test.mavjoy.json")
    configmod.export(cfg, path)
    raw = json.load(open(path, encoding="utf-8"))
    print(f"   exported {len(raw)} keys, marker {raw.get('mavjoy_config')}")
    assert "latches" not in raw, "latch positions must not be exported"

    # Everything else must be there, or "import and fly" is not true.
    for key in ("firmware", "port", "baud", "rate_hz", "throttle", "channels"):
        assert key in raw, f"{key} must travel with an exported config"

    back = configmod.read_file(path)
    os.unlink(path)
    assert back["latches"] == {}, "latches must not come back either"
    print(f"   baud {back['baud']}   rate {back['rate_hz']}   "
          f"throttle {back['throttle']['mode']}")
    print(f"   CH6 {back['channels'][5]}")
    assert back["baud"] == 921600
    assert back["rate_hz"] == 333
    assert back["throttle"]["mode"] == "ramp"

    assert back["firmware"] == "inav", "the firmware must survive the trip"

    # The mixer built from the imported file must behave like the original.
    a, b = gp.Mixer(cfg), gp.Mixer(back)
    for i in range(crsf.NUM_CHANNELS):
        assert a.channels[i].to_dict() == b.channels[i].to_dict(), (
            f"CH{i + 1} differs after the round trip")

    # An output midpoint has to survive a mapping edit as well as a file.
    # It did not: on_channel_changed carried out_min and out_max across and
    # left out_mid behind, so every edit - and every Save, which edits all
    # sixteen - quietly put the midpoint back to centre.
    ch = gp.ChannelMap.from_dict({"src": "axis", "idx": 0, "out_min": 1100,
                                  "out_mid": 1550, "out_max": 1900})
    assert ch.to_dict()["out_mid"] == 1550, "a midpoint must survive to_dict"
    print(f"   all {crsf.NUM_CHANNELS} channels identical after the round trip")

    # The import dialog catches ValueError and nothing else, so a file it
    # cannot use has to arrive as one. The last two are valid JSON of the
    # wrong shape, which the named checks above let through to the shaping
    # code, where unpacking them raised TypeError straight past the dialog.
    for body, why in ((b"not json at all", "garbage"),
                      (b'{"baud": 921600}', "no channel mapping"),
                      (b"[1, 2, 3]", "not an object"),
                      (b'{"channels": [null]}', "a null channel"),
                      (b'{"channels": [], "gamepads": 5}', "a scalar gamepads")):
        with open(path, "wb") as fh:
            fh.write(body)
        try:
            configmod.read_file(path)
            raise AssertionError(f"{why} should have been refused")
        except ValueError as exc:
            print(f"   refused {why}: {str(exc)[:44]}...")

    # load() has no dialog to fall back on: it runs from App.__init__,
    # before there is a window at all, so anything it cannot make sense of
    # has to come back as the defaults and a warning rather than as an
    # exception that stops the program starting.
    for body, why in ((b"[1, 2, 3]", "a list"),
                      (b"5", "a bare number"),
                      (b'{"gamepads": 5}', "a scalar gamepads"),
                      (b'{"channels": ["ch1"]}', "a string channel")):
        with open(path, "wb") as fh:
            fh.write(body)
        started, warning = configmod.load(path)
        assert warning, f"load must warn about {why}"
        assert len(started["channels"]) == crsf.NUM_CHANNELS, \
            f"load must still hand back a usable config for {why}"
        print(f"   started on defaults for {why}")
    os.unlink(path)


def _check_fixed_value():
    """A "fixed" channel sends the constant it was given; "none" sends centre.

    "fixed" could always do this, but had no box to type a value into, so
    the feature existed and could not be reached. The other half of the
    test is that "none" is untouched: it sends centre and has nothing to
    set, which is the whole difference between the two.
    """
    print("")
    print("-- fixed channel values --")
    cfg = configmod.default_config()
    cfg["channels"][7] = {"src": "none"}
    # A value on a "none" channel is ignored: none means centre, full stop.
    cfg["channels"][8] = {"src": "none", "value": crsf.us_to_crsf(1750)}
    cfg["channels"][9] = {"src": "fixed", "value": crsf.us_to_crsf(1200)}
    m = gp.Mixer(cfg)
    m.reset()

    st = gp.InputState(axes=(0.0,) * 4, buttons=(False,) * 8, hats=((0, 0),),
                       timestamp=time.monotonic(), connected=True)
    v = m.compute({0: st})
    for n, want in ((8, 1500), (9, 1500), (10, 1200)):
        got = crsf.crsf_to_us(v[n - 1])
        print(f"   CH{n:<2} {got} us")
        assert got == want, f"CH{n} should send {want} us, got {got}"

    # Failsafe values are what the mixer reports before anything is
    # computed; a channel parked at a value should read as that value there
    # too, not as centre.
    fs = m.failsafe_values()
    assert crsf.crsf_to_us(fs[9]) == 1200, "a fixed channel must hold at rest"
    assert crsf.crsf_to_us(fs[8]) == 1500, "none is centre, value or no value"
    print("   and they hold in the failsafe values too")


def _check_guard_button():
    """A guarded channel does not move unless the guard is held.

    For an arm switch this has to hold in BOTH directions. Guarding only
    the way in would leave the interesting failure untouched: a knock that
    disarms in the air is worse than one that arms on the bench.
    """
    print("")
    print("-- guard button --")
    cfg = configmod.default_config()
    cfg["channels"][4] = {"src": "toggle", "idx": 11, "guard": 5}   # CH5
    cfg["channels"][5] = {"src": "toggle", "idx": 12}               # CH6, no guard
    cfg["channels"][6] = {"src": "button", "idx": 13, "guard": 5}   # CH7 momentary
    cfg["channels"][7] = {"src": "switch", "idx": 0, "steps": 3, "guard": 5}
    m = gp.Mixer(cfg)
    m.reset()

    def frame(*down):
        b = [False] * 16
        for i in down:
            b[i] = True
        st = gp.InputState(axes=(0.0,) * 4, buttons=tuple(b), hats=((0, 0),),
                           timestamp=time.monotonic(), connected=True)
        v = m.compute({0: st})
        return [crsf.crsf_to_us(v[i]) for i in (4, 5, 6, 7)]

    low, high = crsf.US_MIN, crsf.US_MAX
    assert frame() == [low, low, low, low], "everything starts low"

    # The arm button on its own, twice, must do nothing at all.
    for _ in range(2):
        assert frame(11)[0] == low, "an unguarded press must not arm"
        assert frame()[0] == low, "and must not arm on release either"
    print(f"   arm button alone, twice:    CH5 = {frame()[0]:.0f} us")

    # The neighbour on its own button is untouched by any of this.
    frame(12)
    assert frame()[1] == high, "CH6 has no guard and must still work"

    frame(5, 11)
    armed = frame(5)[0]
    print(f"   guard + arm button:         CH5 = {armed:.0f} us")
    assert armed == high, "guard held, the press must arm"

    # And it must not be possible to disarm by accident either.
    frame(11)
    assert frame()[0] == high, "an unguarded press must not disarm"
    print(f"   arm button alone again:     CH5 = {frame()[0]:.0f} us (held)")

    frame(5, 11)
    assert frame(5)[0] == low, "guard held, the press must disarm"
    print("   guard + arm button:         disarmed")

    # A momentary button reads low without the guard, and a switch keeps
    # the position it had rather than snapping anywhere.
    assert frame(13)[2] == low, "a guarded momentary button must read low"
    assert frame(5, 13)[2] == high, "with the guard it must read high"
    frame(5, 1)                       # move the switch to position 2, guarded
    pos2 = frame(5)[3]
    assert frame(0)[3] == pos2, "an unguarded switch must hold its position"
    print("   momentary and switch:       both held shut")


def main():
    wire, needs_url = _open_wire()
    print(f"virtual serial port: {wire.port}")

    # The link calls serial.Serial(port, baud, ...) directly. For a URL port
    # we route that through serial_for_url instead; production code untouched.
    original = serial.Serial
    if needs_url:
        def _factory(port, baud, **kw):
            kw.pop("rtscts", None)
            kw.pop("dsrdtr", None)
            return serial.serial_for_url(port, baud, **kw)
        serial.Serial = _factory

    try:
        _run(wire)
    finally:
        serial.Serial = original
        wire.close()


def _run(wire):
    cfg = configmod.default_config()
    cfg["throttle"]["mode"] = "ramp"
    # The shipped default maps nothing at all, so build the map this test
    # needs here rather than leaning on whatever the defaults happen to be.
    cfg["channels"][0] = {"src": "axis", "idx": 3, "inv": False}
    cfg["channels"][1] = {"src": "axis", "idx": 4, "inv": False}
    cfg["channels"][2] = {"src": "throttle", "idx": 0, "inv": False}
    cfg["channels"][3] = {"src": "axis", "idx": 0, "inv": False}
    cfg["channels"][4] = {"src": "toggle", "idx": 7, "inv": False}
    # CH6 reads the same axis as CH4 on purpose: one is exempt from the
    # resume hold and the other is not, so the same stick shows both.
    cfg["channels"][5] = {"src": "axis", "idx": 0, "inv": False}
    mixer = gp.Mixer(cfg)
    mixer.reset()

    pad = gp.SimGamepadThread()
    pad.start()
    time.sleep(0.2)

    events = []
    lk = linkmod.CrsfLink(wire.port, 400000, 250, mixer, pad,
                          on_event=lambda lvl, m: events.append((lvl, m)))
    lk.start()
    if hasattr(wire, "wait"):
        wire.wait()
    time.sleep(2.0)

    while wire.read():        # drain whatever accumulated during start-up
        pass
    data = b""
    t_end = time.time() + 1.0
    while time.time() < t_end:
        chunk = wire.read()
        if chunk:
            data += chunk
        else:
            time.sleep(0.005)
    print(f"captured {len(data)} bytes in 1.0 s "
          f"({len(data) / 26:.0f} frames -> {len(data) / 26:.0f} Hz)")

    parser = crsf.Parser()
    frames = parser.feed(data)
    rc = [f for f in frames if f[1] == crsf.FRAMETYPE_RC_CHANNELS_PACKED]
    print(f"decoded {len(rc)} RC frames, {parser.crc_errors} crc errors")
    assert rc, "no RC frames decoded"
    assert parser.crc_errors == 0

    values = crsf.unpack_rc_channels(rc[-1][2])
    print("last frame channels:")
    for i, v in enumerate(values[:8]):
        print(f"   CH{i+1:<2} {v:4d}  {crsf.crsf_to_us(v):7.1f} us  "
              f"{configmod.CHANNEL_HINTS[i]}")
    assert values[2] == crsf.CHANNEL_MIN, "throttle should be idle (nothing pressed)"
    assert all(crsf.CHANNEL_MIN <= v <= crsf.CHANNEL_MAX for v in values)

    _t, stats = lk.snapshot()
    print(f"rate measured by link thread: {stats.actual_rate:.1f} Hz, "
          f"worst lateness {stats.jitter_ms:.2f} ms, "
          f"sent {stats.frames_sent}, skipped {stats.frames_skipped}, "
          f"write errors {stats.write_errors}")
    assert 230 < stats.actual_rate < 270, "rate out of tolerance"

    # ---- telemetry path, while the link is still up
    ls = bytes([45, 50, 100, 8, 0, 6, 5, 40, 98, 3])
    body = bytes([crsf.FRAMETYPE_LINK_STATISTICS]) + ls
    frame = bytes([0xEA, len(body) + 1]) + body + bytes([crsf.crc8(body)])
    wire.inject(frame)
    time.sleep(0.2)
    telem, stats = lk.snapshot()
    print("telemetry decoded:", telem.get("link"))
    assert telem.get("link", {}).get("up_lq") == 100

    # Battery fields are SIGNED, every one of them. A flight controller with
    # no battery monitor reports small negatives from sensor noise, and read
    # unsigned those came back as 6553.5 A and 16777192 mAh - readings
    # alarming enough to abort a flight over, from an aircraft sitting still.
    noise = crsf.parse_battery(bytes([0x00, 0x03, 0xFF, 0xFF,
                                      0xFF, 0xFF, 0xE8, 0x64]))
    assert noise["current"] == -0.1, noise["current"]
    assert noise["capacity_used"] == -24, noise["capacity_used"]
    assert noise["voltage"] == 0.3, noise["voltage"]
    real = crsf.parse_battery((1250).to_bytes(2, "big")
                              + (125).to_bytes(2, "big")
                              + (480).to_bytes(3, "big") + bytes([62]))
    assert real == {"voltage": 125.0, "current": 12.5,
                    "capacity_used": 480, "remaining": 62}, real
    print(f"battery: noise frame -> {noise['current']} A, "
          f"{noise['capacity_used']} mAh; real pack -> {real['voltage']} V, "
          f"{real['current']} A")
    # RSSI is a uint8 holding dBm * -1, so 45 means -45 dBm.
    assert telem["link"]["up_rssi_1"] == -45, telem["link"]["up_rssi_1"]
    assert telem["link"]["up_rssi"] == -45, "antenna 0 is active in this frame"

    # Zero is not 0 dBm. A link that is down reports zero across the frame,
    # and showing that as 0 dBm claims the strongest possible signal at the
    # exact moment there is none.
    assert crsf.parse_link_statistics(bytes(10))["up_rssi"] is None
    # A sender that puts a signed int8 in the field instead of the magnitude
    # must not come back as an impossible -211 dBm.
    assert crsf.rssi_dbm(211) == -45, crsf.rssi_dbm(211)
    # The receiver may be listening on the other antenna.
    two = crsf.parse_link_statistics(bytes([45, 60, 100, 8, 1, 6, 5, 40, 98, 3]))
    assert two["up_rssi"] == -60, two["up_rssi"]
    print(f"rssi: {telem['link']['up_rssi']} dBm, "
          f"zero -> {crsf.parse_link_statistics(bytes(10))['up_rssi']}, "
          f"signed byte 211 -> {crsf.rssi_dbm(211)} dBm, "
          f"antenna 1 -> {two['up_rssi']} dBm")

    # ---- the sensors that used to be dropped on the floor
    def sensor(ftype, payload):
        body = bytes([ftype]) + payload
        return bytes([0xEA, len(body) + 1]) + body + bytes([crsf.crc8(body)])

    wire.inject(sensor(crsf.FRAMETYPE_FLIGHT_MODE, b"RTL" + bytes([0])))
    wire.inject(sensor(crsf.FRAMETYPE_VARIO, bytes([0xFF, 0x9C])))
    wire.inject(sensor(crsf.FRAMETYPE_BARO_ALTITUDE, bytes([0x27, 0x1A])))
    time.sleep(0.3)
    telem, _stats = lk.snapshot()
    print(f"flight mode: {telem.get('mode')}")
    print(f"vario:       {telem.get('vario')}")
    print(f"baro:        {telem.get('baro')}")
    assert telem.get("mode", {}).get("mode") == "RTL", "flight mode not decoded"
    assert telem.get("vario", {}).get("vertical_speed_ms") == -1.0
    assert telem.get("baro", {}).get("altitude_m") == 1.0

    # An RPM frame: a real sensor this app cannot read yet. It must be
    # counted rather than dropped, so the Telemetry tab can say what the
    # aircraft is actually sending instead of leaving it to guesswork.
    wire.inject(sensor(0x0C, bytes([0, 0, 1, 0])))
    time.sleep(0.3)
    telem, _stats = lk.snapshot()
    print(f"undecoded:   {dict((k, v) for k, v in telem.get('unknown', {}).items() if k != '_t')}")
    assert telem.get("unknown", {}).get("0x0C") == 1, "unknown frame not counted"

    # ---- failsafe: no pulses, then pulses again.
    # A transmitter does not shut down because a stick stopped reporting.
    # It stops putting frames out, the receiver falls into its own
    # failsafe, and when the input returns it simply transmits again.
    print("\nstopping gamepad thread to test the stale-input failsafe...")
    before = lk.stats.frames_sent
    pad.stop()
    time.sleep(0.6)
    assert lk.is_alive(), "the link shut itself down instead of going quiet"
    assert lk.running, "the serial port was closed"
    assert not lk.transmitting

    # Count bytes, not frames_sent: a settings or status frame is invisible
    # to a frame counter while still being a frame ExpressLRS heard.
    while wire.read():
        pass
    leaked = b""
    t_end = time.time() + 2.0
    while time.time() < t_end:
        leaked += wire.read()
        time.sleep(0.01)
    print(f"frames sent after input died: {lk.stats.frames_sent - before}")
    print(f"bytes written to the port over the next 2.0 s: {len(leaked)}")
    assert not leaked, (f"link wrote {len(leaked)} bytes during failsafe: "
                        f"{leaked[:32].hex(chr(32))} - must hear silence")

    # ---- and now the input comes back, with nobody pressing anything
    print("\nrestoring input; the link must resume on its own...")
    pad2 = gp.SimGamepadThread()
    pad2.start()
    lk.gamepad = pad2
    time.sleep(1.0)
    resumed = b""
    t_end = time.time() + 0.5
    while time.time() < t_end:
        resumed += wire.read()
        time.sleep(0.005)
    print(f"transmitting again: {lk.transmitting}, "
          f"{len(resumed)} bytes in 0.5 s")
    assert lk.transmitting, "the link did not resume when input returned"
    assert len(resumed) > 1000, "frames are not flowing again"
    pad2.stop()

    _check_rf_hold(wire, lk, mixer)
    _check_oneway()
    _check_arm_is_ch5()
    _check_arm_from_model()
    _check_latch_memory()
    _check_throttle_cut()
    _check_guarded_reset()
    _check_rate_warning()
    _check_config_writes()
    _check_map()
    _check_module_prep()
    _check_hold_snapshot()
    _check_endpoints()
    _check_config_file()
    _check_fixed_value()
    _check_guard_button()

    lk.stop()
    lk.join(timeout=2)
    print("\nevents:")
    for lvl, m in events:
        print(f"   [{lvl}] {m}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
