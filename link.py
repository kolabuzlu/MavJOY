"""
The CRSF link thread.

This is the only thread that touches the serial port. Every tick it asks
the mixer for fresh channel values and writes one RC frame. If the
gamepad data is stale it writes *nothing* — ExpressLRS runs a 1 second
UART watchdog on its handset input, so silence makes the TX drop the RF
link and the receiver falls into its own failsafe. That is the behaviour
we want; it is never correct to keep transmitting the last known stick
positions.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time

import serial
import serial.tools.list_ports

import crsf
from gamepad import InputState


def list_serial_ports():
    """Return [(device, description), ...] for every serial port we can see."""
    out = []
    for p in serial.tools.list_ports.comports():
        desc = p.description or ""
        if p.manufacturer and p.manufacturer not in desc:
            desc = f"{desc} ({p.manufacturer})"
        out.append((p.device, desc.strip()))
    return sorted(out)


class _WindowsTimerResolution:
    """time.sleep() granularity on Windows is ~15ms by default; ask for 1ms."""

    def __enter__(self):
        self._winmm = None
        if sys.platform == "win32":
            try:
                self._winmm = ctypes.WinDLL("winmm")
                self._winmm.timeBeginPeriod(1)
            except Exception:
                self._winmm = None
        return self

    def __exit__(self, *exc):
        if self._winmm is not None:
            try:
                self._winmm.timeEndPeriod(1)
            except Exception:
                pass


def _raise_thread_priority():
    if sys.platform == "win32":
        try:
            THREAD_PRIORITY_TIME_CRITICAL = 15
            handle = ctypes.windll.kernel32.GetCurrentThread()
            ctypes.windll.kernel32.SetThreadPriority(handle, THREAD_PRIORITY_TIME_CRITICAL)
        except Exception:
            pass
    else:
        try:
            import os
            os.nice(-5)
        except Exception:
            pass


class LinkStats:
    __slots__ = ("frames_sent", "frames_skipped", "bytes_rx", "telem_frames",
                 "crc_errors", "write_errors", "actual_rate", "last_error",
                 "jitter_ms", "started_at")

    def __init__(self):
        self.frames_sent = 0
        self.frames_skipped = 0
        self.bytes_rx = 0
        self.telem_frames = 0
        self.crc_errors = 0
        self.write_errors = 0
        self.actual_rate = 0.0
        self.jitter_ms = 0.0
        self.last_error = ""
        self.started_at = 0.0


class _ParamJob:
    """One settings operation, driven a step at a time by the link thread."""

    # ExpressLRS applies a write asynchronously, and how long that takes
    # depends on the field: measured on ELRS 4.1, Fan Thresh is already in
    # effect by the first read ~150 ms later, while Packet Rate - which
    # re-keys the RF link - still reports the old value at that point. It
    # sends nothing to announce it either: writing a field and then listening
    # for 3 s produced no unsolicited entry at all.
    #
    # So there is no signal to wait for and no single delay that is right.
    # Read the field back instead, and keep reading until it reports the
    # value we asked for or the deadline passes. A quick field finishes in
    # one round trip; a slow one gets as long as it needs; and a genuine
    # refusal is still a refusal once the deadline expires.
    VERIFY_POLL = 0.12        # gap between read-backs while waiting
    VERIFY_DEADLINE = 3.0     # stop waiting for the value to change

    def __init__(self, kind, index=None, value=None, on_done=None, width=1):
        self.kind = kind
        self.width = width
        self.stage = kind
        self.index = index
        self.value = value
        self.on_done = on_done
        self.reader = crsf.ParamReader(index) if index is not None else None
        self.result = None
        self.complete = False
        self._verify_until = 0.0
        self._next_verify = 0.0

    def request(self):
        now = time.monotonic()

        if self.stage == "ping":
            return crsf.device_ping_frame()

        if self.stage == "write":
            # Sent once, then we watch the field rather than guessing a delay.
            self.stage = "verify"
            self.reader = crsf.ParamReader(self.index)
            self._verify_until = now + self.VERIFY_DEADLINE
            self._next_verify = 0.0
            return crsf.param_write_frame(self.index, self.value, self.width)

        if self.stage == "verify" and now < self._next_verify:
            return None

        if self.stage in ("read", "verify"):
            return self.reader.next_request()

        return None

    def feed(self, ftype, payload):
        if self.stage == "ping":
            if ftype != crsf.FRAMETYPE_DEVICE_INFO:
                return False
            info = crsf.parse_device_info(payload)
            if not info:
                return False
            self.result = info
            self.complete = True
            return True

        if self.stage not in ("read", "verify"):
            return False
        if ftype != crsf.FRAMETYPE_PARAMETER_SETTINGS_ENTRY:
            return False
        if not self.reader.feed(payload):
            return False
        if not self.reader.done:
            return True                     # more chunks to come

        field = self.reader.field()
        self.result = field

        if self.stage == "read":
            self.complete = True
            return True

        # Command fields carry a status rather than a value, so there is
        # nothing to match against: one read-back is the answer.
        if (field is None
                or field.type == crsf.PARAM_COMMAND
                or field.value == self.value
                or time.monotonic() >= self._verify_until):
            self.complete = True
        else:
            self.reader = crsf.ParamReader(self.index)
            self._next_verify = time.monotonic() + self.VERIFY_POLL
        return True


class CrsfLink(threading.Thread):
    def __init__(self, port: str, baud: int, rate_hz: int, mixer, gamepad,
                 sync_byte: int = crsf.CRSF_SYNC_BYTE, on_event=None):
        super().__init__(name="crsf-link", daemon=True)
        self.port_name = port
        self.baud = baud
        self.rate_hz = max(10, min(500, int(rate_hz)))
        self.period = 1.0 / self.rate_hz
        self.mixer = mixer
        self.gamepad = gamepad
        self.sync_byte = sync_byte
        self._on_event = on_event or (lambda level, msg: None)

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.stats = LinkStats()
        self.telemetry = {}
        self.running = False
        self.transmitting = False   # True while we are actually sending frames
        self._parser = crsf.Parser()
        self._ser = None

        # Module settings traffic (packet rate, power, ...). One job at a
        # time: a late reply to a previous request cannot be told apart from
        # the one we are waiting for, so they must never overlap.
        self._jobs = queue.Queue()
        self._job = None
        self._job_next_send = 0.0
        self._job_expiry = 0.0
        self.device_info = None

        # What the module says it wants from us, from its 0x3A sync frames.
        self.sync = None

        # ExpressLRS status: whether a receiver is connected, and the reason
        # it gives when it will not accept a setting.
        self.elrs_status = None
        self._status_next = 0.0
        self._clear_warning = threading.Event()

    def on_event(self, level, message):
        """Report an event. A broken callback must never kill the link."""
        try:
            self._on_event(level, message)
        except Exception:
            pass

    # ---------------------------------------------------------- public API
    def stop(self):
        self._stop_event.set()

    def snapshot(self):
        with self._lock:
            return dict(self.telemetry), self.stats

    def submit(self, kind, index=None, value=None, on_done=None, width=1):
        """Queue a settings operation. `on_done(result, error)` is called on
        the link thread, so a GUI must marshal it back to its own thread.

        kind is "ping" (-> device info dict), "read" (-> ParamField) or
        "write" (-> ParamField, re-read after the write to confirm it took).

        A write is verified by reading the field back until it reports the
        value asked for, or until the job's deadline passes - the module
        gives no signal of its own, and how long it takes to apply depends
        on the field.
        """
        self._jobs.put(_ParamJob(kind, index, value, on_done, width))

    def busy(self):
        return self._job is not None or not self._jobs.empty()

    def set_rate(self, rate_hz):
        """Change the frame rate on a running link. Returns True if it moved."""
        rate = max(10, min(500, int(rate_hz)))
        with self._lock:
            if rate == self.rate_hz:
                return False
            self.rate_hz = rate
            self.period = 1.0 / rate
        return True

    def requested_rate(self):
        """The rate the module last asked for, or None if it has not said."""
        with self._lock:
            sync = self.sync
        if not sync or time.monotonic() - sync.get("_t", 0) > 3.0:
            return None
        return sync["rate_hz"]

    # -------------------------------------------------------------- thread
    def run(self):
        try:
            self._ser = serial.Serial(
                self.port_name, self.baud, timeout=0, write_timeout=0.05,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, rtscts=False, dsrdtr=False,
            )
        except Exception as exc:
            self.stats.last_error = str(exc)
            self.on_event("error", f"Could not open {self.port_name}: {exc}")
            self.running = False
            return

        self.running = True
        self.stats.started_at = time.monotonic()
        self.on_event("info", f"Link open on {self.port_name} @ {self.baud} baud, "
                              f"{self.rate_hz} Hz")
        _raise_thread_priority()

        rate_window_start = time.perf_counter()
        frames_in_window = 0
        worst_late = 0.0

        try:
            with _WindowsTimerResolution():
                next_t = time.perf_counter()
                while not self._stop_event.is_set():
                    next_t += self.period
                    now = self._sleep_until(next_t)

                    late = now - next_t
                    if late > worst_late:
                        worst_late = late
                    if late > 5 * self.period:      # we fell badly behind; resync
                        next_t = now

                    self._tick()

                    frames_in_window += 1
                    elapsed = now - rate_window_start
                    if elapsed >= 0.5:
                        with self._lock:
                            self.stats.actual_rate = frames_in_window / elapsed
                            self.stats.jitter_ms = worst_late * 1000.0
                        frames_in_window = 0
                        worst_late = 0.0
                        rate_window_start = now
        finally:
            self.transmitting = False
            self.running = False
            try:
                if self._ser and self._ser.is_open:
                    self._ser.close()
            except Exception:
                pass
            self.on_event("info", "Link closed")

    # ------------------------------------------------------------ internals
    def _first_stale(self, states):
        """The first device the map needs that is not reporting, if any."""
        for slot in sorted(self.mixer.required_devices()):
            st = states.get(slot) or InputState()
            if not st.is_fresh():
                return slot, st
        return None, None

    @staticmethod
    def _sleep_until(deadline: float) -> float:
        """Sleep most of the way, then spin for the last millisecond."""
        while True:
            now = time.perf_counter()
            remaining = deadline - now
            if remaining <= 0:
                return now
            if remaining > 0.0015:
                time.sleep(remaining - 0.001)
            else:
                time.sleep(0)  # yield, then spin

    def _tick(self):
        # Every device the map reads has to be live, not just the first
        # one. A separate USB throttle fails independently of the pad,
        # and flying on a throttle that stopped reporting is no better
        # than flying on stale sticks.
        states = self.gamepad.states
        stale_slot, st = self._first_stale(states)

        if stale_slot is not None:
            if self.transmitting:
                where = "gamepad" if stale_slot == 0 else f"device {stale_slot}"
                reason = f"{where} disconnected" if not st.connected else \
                         f"{where} data stale ({st.age() * 1000:.0f} ms)"
                self.on_event("warn", f"Stopped transmitting: {reason}. "
                                      f"Receiver will go to failsafe.")
            self.transmitting = False
            with self._lock:
                self.stats.frames_skipped += 1
            # Nothing is written on this path. The whole failsafe design
            # rests on the module hearing silence: ExpressLRS runs a 1 second
            # watchdog on its handset UART, and any well-formed frame - a
            # settings request included - is a frame it heard. Telemetry is
            # read-only, so it stays.
            self._read_telemetry()
            return

        if not self.transmitting:
            self.transmitting = True
            self.on_event("info", "Transmitting channel data")

        values = self.mixer.compute(states)
        frame = crsf.pack_rc_channels(values, sync=self.sync_byte)
        try:
            self._ser.write(frame)
        except serial.SerialTimeoutException:
            with self._lock:
                self.stats.write_errors += 1
        except Exception as exc:
            with self._lock:
                self.stats.write_errors += 1
                self.stats.last_error = str(exc)
            self.on_event("error", f"Serial write failed: {exc}")
            self._stop_event.set()
            return
        else:
            with self._lock:
                self.stats.frames_sent += 1

        self._read_telemetry()
        self._service_jobs()
        self._service_status()

    def _read_telemetry(self):
        try:
            waiting = self._ser.in_waiting
            if not waiting:
                return
            data = self._ser.read(waiting)
        except Exception:
            return
        if not data:
            return

        with self._lock:
            self.stats.bytes_rx += len(data)

        for addr, ftype, payload in self._parser.feed(data):
            if ftype == crsf.FRAMETYPE_RC_CHANNELS_PACKED:
                continue  # our own echo on a half-duplex wire
            if ftype in (crsf.FRAMETYPE_DEVICE_INFO,
                         crsf.FRAMETYPE_PARAMETER_SETTINGS_ENTRY):
                self._feed_job(ftype, payload)
            elif ftype == crsf.FRAMETYPE_ELRS_STATUS:
                status = crsf.parse_elrs_status(payload)
                if status:
                    status["_t"] = time.monotonic()
                    with self._lock:
                        self.elrs_status = status
            elif ftype == crsf.FRAMETYPE_RADIO_ID:
                sync = crsf.parse_opentx_sync(payload)
                if sync:
                    sync["_t"] = time.monotonic()
                    with self._lock:
                        self.sync = sync
            with self._lock:
                self.stats.telem_frames += 1
                self.stats.crc_errors = self._parser.crc_errors
            entry = crsf.FRAME_PARSERS.get(ftype)
            if entry:
                key, parser = entry
                parsed = parser(payload)
                if parsed:
                    with self._lock:
                        self.telemetry[key] = parsed
                        self.telemetry[key]["_t"] = time.monotonic()

    # --------------------------------------------------- module settings
    JOB_RESEND = 0.15     # resend an unanswered request this often
    JOB_TIMEOUT = 5.0     # give up on a job after this long with no progress

    STATUS_INTERVAL = 2.0     # how often to ask the module how it is doing

    def _service_status(self):
        """Poll the ExpressLRS status frame. It carries the reason a setting
        was refused, which is otherwise invisible - the value just reverts."""
        if self._job is not None:
            return                      # never interleave with a settings job
        now = time.monotonic()
        if now < self._status_next:
            return
        self._status_next = now + self.STATUS_INTERVAL
        try:
            if self._clear_warning.is_set():
                self._clear_warning.clear()
                self._ser.write(crsf.elrs_clear_warning_frame())
            self._ser.write(crsf.elrs_status_request_frame())
        except Exception:
            pass

    def status(self):
        """Latest ELRS status, or None if it has not answered recently."""
        with self._lock:
            status = self.elrs_status
        if not status or time.monotonic() - status.get("_t", 0) > 6.0:
            return None
        return status

    def clear_warning(self):
        """Ask the link thread to acknowledge a latched warning.

        Called from the GUI thread, so it only raises a flag: this thread is
        the only one that touches the port, and two threads writing one
        pyserial handle can splice a settings frame into an RC frame.
        """
        self._clear_warning.set()

    def _service_jobs(self):
        now = time.monotonic()

        if self._job is None:
            try:
                self._job = self._jobs.get_nowait()
            except queue.Empty:
                return
            self._job_next_send = 0.0
            self._job_expiry = now + self.JOB_TIMEOUT

        if now >= self._job_expiry:
            self._finish_job(None, "module did not answer")
            return

        if now >= self._job_next_send:
            frame = self._job.request()
            if frame:
                try:
                    self._ser.write(frame)
                except Exception:
                    pass
            self._job_next_send = now + self.JOB_RESEND

    def _feed_job(self, ftype, payload):
        job = self._job
        if job is None:
            return
        if not job.feed(ftype, payload):
            return
        if job.complete:
            self._finish_job(job.result, None)
        else:
            # made progress: ask for the next piece at once, restart the clock
            self._job_next_send = 0.0
            self._job_expiry = time.monotonic() + self.JOB_TIMEOUT

    def _finish_job(self, result, error):
        job, self._job = self._job, None
        self._status_next = 0.0     # re-ask now; a write may have been refused
        if job is None or job.on_done is None:
            return
        try:
            job.on_done(result, error)
        except Exception:
            pass    # a broken callback must never kill the link
