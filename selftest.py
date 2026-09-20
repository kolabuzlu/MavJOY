#!/usr/bin/env python3
"""Bench test with no hardware: runs the real link thread against a virtual
serial port, decodes what comes out and checks rate, framing and failsafe.

Works on Windows too. POSIX gets a pty; everywhere else (and on Windows)
the link talks to a loopback TCP socket through pyserial's ``socket://``
URL handler, which exercises the same pyserial read/write paths."""

import os
import socket
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

    # ---- failsafe: kill the input and confirm the link stops transmitting
    print("\nstopping gamepad thread to test the stale-input failsafe...")
    pad.stop()
    time.sleep(0.6)
    before = lk.stats.frames_sent
    while wire.read():          # drain anything still in flight
        pass
    time.sleep(1.0)
    after = lk.stats.frames_sent
    # Count bytes on the wire, not frames_sent. The failsafe contract is that
    # the module hears *silence*, and a settings or status frame is invisible
    # to frames_sent while still being a frame ExpressLRS heard.
    leaked = b""
    t_end = time.time() + 2.5
    while time.time() < t_end:
        leaked += wire.read()
        time.sleep(0.01)
    print(f"frames sent during the 1.0 s after input died: {after - before}")
    print(f"bytes written to the port over the next 2.5 s: {len(leaked)}")
    assert after == before, "link kept transmitting with dead input!"
    assert not lk.transmitting
    assert not leaked, (f"link wrote {len(leaked)} bytes during failsafe: "
                        f"{leaked[:32].hex(' ')} - the module must hear silence")

    # ---- telemetry path
    ls = bytes([45, 50, 100, 8, 0, 6, 5, 40, 98, 3])
    body = bytes([crsf.FRAMETYPE_LINK_STATISTICS]) + ls
    frame = bytes([0xEA, len(body) + 1]) + body + bytes([crsf.crc8(body)])
    wire.inject(frame)
    time.sleep(0.2)
    telem, stats = lk.snapshot()
    print("telemetry decoded:", telem.get("link"))
    assert telem.get("link", {}).get("up_lq") == 100

    lk.stop()
    lk.join(timeout=2)
    print("\nevents:")
    for lvl, m in events:
        print(f"   [{lvl}] {m}")
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
