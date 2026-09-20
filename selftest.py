#!/usr/bin/env python3
"""Bench test with no hardware: runs the real link thread against a virtual
serial port, decodes what comes out and checks rate, framing and failsafe.

Works on Windows too. POSIX gets a pty; everywhere else (and on Windows)
the link talks to a loopback TCP socket through pyserial's ``socket://``
URL handler, which exercises the same pyserial read/write paths."""

import os
import socket
import tempfile
import sys
import threading
import time

import serial

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

    frame(1.0)
    for _ in range(200):
        held = m.compute({0: gp.InputState(connected=False,
                                           timestamp=time.monotonic())})
    crept = crsf.crsf_to_us(held[0])
    print(f"   200 frames with the device gone: {crept:.0f} us")
    assert abs(crept - 1900) < 2, f"the value crept to {crept:.0f} us"


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
    _check_endpoints()

    lk.stop()
    lk.join(timeout=2)
    print("\nevents:")
    for lvl, m in events:
        print(f"   [{lvl}] {m}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
