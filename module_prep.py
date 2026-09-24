"""Prepare an ExpressLRS TX module to take CRSF over its own USB port.

A stock module expects CRSF on its module-bay pin, the one a handset feeds:
GPIO 13 on the BetaFPV 1W Micro, GPIO 4 on the RadioMaster Nomad. That is
no use to a PC program: talking to it would need a USB-TTL adapter soldered
to the bay pin. Moving CRSF onto GPIO 3 and 1 - UART0, the USB bridge -
makes the module's own USB socket the CRSF port, so MavJOY can open it like
any serial device.

What that costs depends on where the backpack is wired. The BetaFPV keeps
its ESP8285 backpack on GPIO 3 and 1 too, and there is only one UART0, so
the backpack and its WiFi go. The Nomad's backpack has pins of its own,
GPIO 18 and 5, and ExpressLRS runs it on another UART, so it stays.
Restore puts either module back as it came.

Nothing here rebuilds firmware. ExpressLRS reads /hardware.json from its
LittleFS partition at boot and only falls back to the layout compiled into
the binary when that file is absent - see lib/OPTIONS/hardware.cpp, which
is also what the module's own web UI writes when you save its hardware
page. So preparing is writing one small filesystem image, and restoring is
erasing it.

The failure mode is mild by design. options_init() calls
LittleFS.begin(true), so an image the module cannot mount is reformatted
and the built-in layout comes back on its own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import struct
import tempfile

# What preparing always changes. Everything else in the layout is the
# module's own wiring - radio pins, screen, fan, power table - and is
# carried across untouched, because hardware.json REPLACES the built-in
# layout rather than merging over it. A partial file would leave the
# module with no radio.
CRSF_SETS = {
    "serial_rx": 3,        # UART0 RX, the USB bridge
    "serial_tx": 1,        # UART0 TX
}

# What it changes as well when the backpack's port sits on those same pins.
# Turning the flag off is not enough there: ExpressLRS opens a port on the
# debug_backpack pins whenever they are set, whatever use_backpack says
# (setupSerial in tx_main.cpp), and that port and CRSF cannot share GPIO 3
# and 1. So the pins go too. A backpack on pins of its own is left alone.
BACKPACK_OFF_SETS = {"use_backpack": False}   # both want UART0; CRSF wins
BACKPACK_PINS = ("debug_backpack_rx", "debug_backpack_tx")
PREPARE_DROPS = ("debug_backpack_baud", "debug_backpack_rx", "debug_backpack_tx")

# Fields a real ELRS TX layout always carries. Checked before writing so
# that pointing this at the wrong JSON is caught here rather than by a
# module that comes back up with no radio.
REQUIRED = ("radio_miso", "radio_mosi", "radio_sck", "radio_nss")

PART_TABLE_OFFSET = 0x8000
PART_TABLE_SIZE = 0x1000
PART_MAGIC = b"\xaa\x50"
TYPE_DATA = 0x01
SUBTYPE_SPIFFS = 0x82
SUBTYPE_LITTLEFS = 0x83

# esp_littlefs geometry as arduino-esp32 formats it. If an image will not
# mount on the module, these are the numbers to question first.
LFS_BLOCK_SIZE = 4096
LFS_READ_SIZE = 128
LFS_PROG_SIZE = 128
LFS_NAME_MAX = 32
LFS_DISK_VERSION = 0x00020000

LAYOUT_URL = "https://github.com/ExpressLRS/targets/tree/master/TX"


class PrepError(Exception):
    """Anything that went wrong, with a sentence fit to show the user."""


class _ByteSink(io.RawIOBase):
    """The .buffer half of a stdout stand-in.

    esptool 5 prints through rich, which writes bytes to sys.stdout.buffer
    rather than going through the text layer. A text-only replacement
    therefore fails with AttributeError on the first line of output.
    """

    def __init__(self, tee):
        self._tee = tee

    def writable(self):
        return True

    def write(self, data):
        self._tee.write(bytes(data).decode("utf-8", "replace"))
        return len(data)

    def flush(self):
        self._tee.flush()


class _Tee(io.TextIOBase):
    """Collects esptool's printing and hands it over a line at a time.

    esptool writes progress to stdout, and a windowed build has no stdout
    at all - sys.stdout is None once PyInstaller builds with console=False,
    so the first print would end the operation rather than report it.

    Carriage returns become newlines because the progress meter redraws
    one line in place, which would otherwise arrive as a single enormous
    line once the whole read had finished.
    """

    def __init__(self, log):
        self._log = log
        self._part = ""
        self.buffer = _ByteSink(self)

    @property
    def encoding(self):
        return "utf-8"

    def isatty(self):
        return False

    def writable(self):
        return True

    @staticmethod
    def _is_meter(line):
        """The progress bar, which is noise once it is not redrawing.

        esptool redraws one line with a carriage return. Turning those into
        newlines makes a 128 kB read arrive as about thirty near-identical
        lines, burying the summary that follows. The summary is kept.
        """
        stripped = line.strip()
        if stripped.startswith(("Reading from 0x", "Writing at 0x")):
            return True
        return "%" in stripped and ("=" in stripped or ">" in stripped)

    def write(self, text):
        self._part += text.replace("\r", "\n")
        while "\n" in self._part:
            line, self._part = self._part.split("\n", 1)
            if line.strip() and not self._is_meter(line):
                self._log(line.rstrip())
        return len(text)

    def flush(self):
        # Filtered here too. A run ends on whatever was left without a
        # trailing newline, which is exactly when the last progress redraw
        # is still sitting in the buffer.
        if self._part.strip() and not self._is_meter(self._part):
            self._log(self._part.rstrip())
        self._part = ""


def _command_name(name):
    """read_flash or read-flash, depending on which esptool is here.

    esptool 5 renamed every subcommand with hyphens and still answers to
    the old spelling, but prints a deprecation warning each time. esptool 4
    knows only the old one. Asking the version is cheaper than asking
    forgiveness halfway through a flash write.
    """
    try:
        import esptool
        major = int(str(esptool.__version__).split(".")[0])
    except Exception:
        return str(name)
    return str(name).replace("_", "-") if major >= 5 else str(name)


def _install_progress(on_progress, log):
    """Send esptool's progress figures to `on_progress`, and give back an undo.

    esptool draws its meter with rich, which renders only when it believes
    it is writing to a terminal. Run from source that is near enough true
    and the bar appears; in a windowed build it is not, so the meter
    vanishes and a twelve second read looks like the app has hung.

    progress_bar is the method esptool calls with the real numbers, before
    any of that rendering is decided, so overriding it reports the same
    figures either way. set_logger type-checks its argument, hence the
    subclass rather than a stand-in object.
    """
    try:
        import esptool.logger as eslog
        from esptool.logger import log as esplog
    except Exception:
        return lambda: None

    base = getattr(eslog, "EsptoolLogger", None)
    if base is None or not hasattr(esplog, "set_logger"):
        return lambda: None

    class _Progress(base):
        def progress_bar(self, cur_iter, total_iters, prefix="", suffix="",
                         bar_length=30):
            try:
                on_progress(int(cur_iter), int(total_iters),
                            str(prefix).strip(), str(suffix).strip())
            except Exception:
                pass

    # Ask the proxy which logger is live rather than reading a class
    # attribute. The singleton is held by esp_pylib's EspLog, and
    # EsptoolLogger carries an `instance` of its own that set_logger never
    # touches - so saving that one restores whatever happened to be there
    # at import, which is right only by accident.
    saved = None
    try:
        bound = esplog.progress_bar
        saved = getattr(bound, "__self__", None)
    except Exception:
        pass

    try:
        esplog.set_logger(_Progress())
    except Exception as exc:
        log(f"(progress reporting unavailable: {exc})")
        return lambda: None

    def undo():
        if saved is None:
            return
        try:
            esplog.set_logger(saved)
        except Exception:
            pass

    return undo


def _capture_esptool_console(tee, log):
    """Point esptool's own console at `tee`, and give back an undo.

    esptool 5 prints through a rich Console created when the module was
    imported, holding the real stdout. Redirecting stdout afterwards does
    not move it, so its progress and its errors would bypass the log
    entirely - and in a windowed build, where stdout is None, writing to
    it is what would fail rather than the flashing.

    set_console_options is esptool's supported way in. Older versions do
    not have it, and they print the plain way, which the redirect already
    covers - so failing to find it is not an error.
    """
    try:
        from esptool.logger import log as esplog
    except Exception:
        return lambda: None

    def undo():
        for call in (lambda: esplog.set_console_options(file=None, no_color=None,
                                                        force_terminal=None),
                     lambda: esplog.set_info_stream(None)):
            try:
                call()
            except Exception:
                pass

    try:
        esplog.set_console_options(file=tee, no_color=True,
                                   force_terminal=False, width=96)
    except Exception as exc:
        log(f"(could not capture esptool's console: {exc})")
        return lambda: None
    try:
        esplog.set_info_stream(tee)
    except Exception:
        pass
    return undo


def _esptool(port, *args, log, progress=None):
    """Run esptool inside this process.

    Not as a subprocess: the obvious spelling is sys.executable -m esptool,
    and in a frozen build sys.executable is MavJOY.exe, so that launches a
    second copy of MavJOY instead of esptool. Underscored command names are
    used because esptool 5 renamed them with hyphens but kept the old
    spellings working, while esptool 4 only knows the old ones.
    """
    try:
        import esptool
    except ImportError as exc:
        raise PrepError(
            "esptool is not installed. Run: pip install esptool littlefs-python"
        ) from exc

    argv = ["--chip", "esp32"]
    if port:
        argv += ["--port", port]
    argv += [_command_name(a) if i == 0 else str(a)
             for i, a in enumerate(args)]
    log("$ esptool " + " ".join(argv))

    tee = _Tee(log)
    # The logger goes in first: the console options below apply to
    # whichever logger is current, so replacing it afterwards would throw
    # them away and send the output back to the real stdout.
    undo_logger = _install_progress(progress, log) if progress else (lambda: None)
    restore = _capture_esptool_console(tee, log)
    try:
        # redirect_stdout on its own is not enough - esptool prints through
        # a rich Console that bound the real stdout when it was imported,
        # so its output would go straight past this and, in a windowed
        # build where stdout is None, take the operation down with it.
        # _capture_esptool_console repoints that Console; this catches
        # anything that still prints the ordinary way.
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            esptool.main(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise PrepError(f"esptool stopped with exit code {exc.code}. "
                            f"See the log above.") from exc
    except PrepError:
        raise
    except Exception as exc:
        raise PrepError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        tee.flush()
        restore()
        undo_logger()


def find_filesystem(port, log, progress=None):
    """Read the chip's partition table and locate its filesystem.

    Read rather than assumed, because the offset differs between ELRS
    builds and writing a filesystem image over the wrong partition would
    land it on the firmware.
    """
    with tempfile.TemporaryDirectory() as tmp:
        blob = os.path.join(tmp, "ptable.bin")
        _esptool(port, "read_flash", hex(PART_TABLE_OFFSET),
                 hex(PART_TABLE_SIZE), blob, log=log, progress=progress)
        with open(blob, "rb") as fh:
            raw = fh.read()

    entries = []
    for i in range(0, len(raw), 32):
        entry = raw[i:i + 32]
        if len(entry) < 32 or entry[:2] != PART_MAGIC:
            break
        ptype, subtype, offset, size = struct.unpack("<BBII", entry[2:12])
        label = entry[12:28].rstrip(b"\x00").decode("utf-8", "replace")
        entries.append((ptype, subtype, offset, size, label))

    if not entries:
        raise PrepError("Could not read a partition table from the module. "
                        "Check the port, and on a module with DIP switches, "
                        "that they route USB to the ESP32.")

    log("")
    log("Partition table:")
    for ptype, subtype, offset, size, label in entries:
        log(f"   {label:<10} type=0x{ptype:02x} subtype=0x{subtype:02x} "
            f"offset=0x{offset:06X} size=0x{size:06X}")

    for ptype, subtype, offset, size, label in entries:
        if ptype == TYPE_DATA and subtype in (SUBTYPE_SPIFFS, SUBTYPE_LITTLEFS):
            log("")
            log(f"Filesystem partition '{label}' at 0x{offset:06X}, "
                f"0x{size:06X} bytes")
            return offset, size

    raise PrepError("This module has no filesystem partition, so the layout "
                    "cannot be overridden this way.")


def read_layout(path):
    """The stock layout in `path`, checked to be an ExpressLRS TX layout."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            layout = json.load(fh)
    except OSError as exc:
        raise PrepError(f"Could not open the layout file ({exc}).") from exc
    except json.JSONDecodeError as exc:
        raise PrepError(f"The layout file is not valid JSON ({exc}).") from exc

    if not isinstance(layout, dict):
        raise PrepError("The layout file does not hold a layout.")
    missing = [k for k in REQUIRED if k not in layout]
    if missing:
        raise PrepError(
            f"This does not look like an ExpressLRS TX layout - it has no "
            f"{', '.join(missing)}. Download the one for your module from "
            f"{LAYOUT_URL}.")
    return layout


def backpack_pins(layout):
    """The GPIOs the layout gives the backpack's port: none, one or two."""
    return {layout.get(key) for key in BACKPACK_PINS} - {None}


def backpack_clashes(layout):
    """Whether the backpack's port sits on the pins CRSF is moving to.

    Asked whatever use_backpack says. Several stock layouts keep a logging
    port on GPIO 3 and 1 with the flag off, and the firmware opens that
    port all the same.
    """
    return bool(backpack_pins(layout) & set(CRSF_SETS.values()))


def plan_layout(stock):
    """What preparing writes for this stock layout, and the changes made.

    Returns (layout, sets, drops). The changes come back as well because
    the read-back is checked against them - see check_read_back.
    """
    sets = dict(CRSF_SETS)
    drops = ()
    if backpack_clashes(stock):
        sets.update(BACKPACK_OFF_SETS)
        drops = PREPARE_DROPS
    layout = dict(stock)
    layout.update(sets)
    for key in drops:
        layout.pop(key, None)
    return layout, sets, drops


def resolve_layout(path):
    """The stock layout with the CRSF-over-USB changes applied."""
    return plan_layout(read_layout(path))[0]


def backpack_note(stock):
    """One sentence on what preparing does to this module's backpack."""
    pins = " and ".join(str(p) for p in sorted(backpack_pins(stock)))
    if backpack_clashes(stock):
        if stock.get("use_backpack"):
            return (f"Its backpack shares GPIO {pins} with the USB port, so "
                    f"the backpack and its WiFi are switched off.")
        return (f"Its backpack port shares GPIO {pins} with the USB port, "
                f"so that port is removed. The backpack was off already.")
    if stock.get("use_backpack") and pins:
        return f"Its backpack has pins of its own, GPIO {pins}, so it stays on."
    return "Its backpack is not on the USB port's pins, so it is left as it is."


def check_read_back(got, sets, drops):
    """What the module holds that preparing did not ask for, as {key: value}.

    Checked against the changes rather than against the numbers written out
    again. Two copies of 3 and 1 is two places to change, and the one that
    gets missed is this one - which would then either call every good flash
    a failure or wave a bad one through. Empty means the flash took.
    """
    wrong = {k: got.get(k) for k, v in sets.items() if got.get(k) != v}
    wrong.update({k: got[k] for k in drops if k in got})
    return wrong


def build_image(layout, size):
    """A LittleFS image holding nothing but /hardware.json."""
    try:
        from littlefs import LittleFS
    except ImportError as exc:
        raise PrepError(
            "littlefs-python is not installed. Run: pip install esptool "
            "littlefs-python") from exc

    payload = json.dumps(layout, separators=(",", ":")).encode()
    fs = LittleFS(block_size=LFS_BLOCK_SIZE,
                  block_count=size // LFS_BLOCK_SIZE,
                  read_size=LFS_READ_SIZE,
                  prog_size=LFS_PROG_SIZE,
                  name_max=LFS_NAME_MAX,
                  disk_version=LFS_DISK_VERSION)
    with fs.open("/hardware.json", "wb") as fh:
        fh.write(payload)
    return bytes(fs.context.buffer), payload


def read_back(port, offset, size, log, progress=None):
    """What /hardware.json on the module actually says, or None."""
    try:
        from littlefs import LittleFS
    except ImportError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        blob = os.path.join(tmp, "fs.bin")
        _esptool(port, "read_flash", hex(offset), hex(size), blob, log=log,
                 progress=progress)
        with open(blob, "rb") as fh:
            raw = fh.read()
    fs = LittleFS(block_size=LFS_BLOCK_SIZE,
                  block_count=size // LFS_BLOCK_SIZE,
                  read_size=LFS_READ_SIZE,
                  prog_size=LFS_PROG_SIZE,
                  name_max=LFS_NAME_MAX,
                  disk_version=LFS_DISK_VERSION,
                  mount=False)
    fs.context.buffer = bytearray(raw)
    try:
        fs.mount()
        with fs.open("/hardware.json", "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except Exception:
        return None


def prepare(port, layout_path, log, progress=None):
    """Write the CRSF-over-USB layout, then read it back and check it."""
    stock = read_layout(layout_path)
    layout, sets, drops = plan_layout(stock)
    log(f"Layout: {os.path.basename(layout_path)}")
    log(f"   serial_rx {layout['serial_rx']}, serial_tx {layout['serial_tx']}, "
        f"use_backpack {layout.get('use_backpack', False)}")
    log(f"   {backpack_note(stock)}")
    log("")

    offset, size = find_filesystem(port, log, progress)
    image, payload = build_image(layout, size)
    log("")
    log(f"Built a {len(image)} byte image holding {len(payload)} bytes "
        f"of hardware.json")

    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "littlefs.bin")
        with open(img, "wb") as fh:
            fh.write(image)
        log("")
        log("Writing the filesystem partition...")
        _esptool(port, "write_flash", hex(offset), img, log=log,
                 progress=progress)

    log("")
    log("Reading it back to check...")
    got = read_back(port, offset, size, log, progress)
    if got is None:
        raise PrepError("The image was written but could not be read back. "
                        "The module will reformat anything it cannot mount "
                        "and use its built-in layout, so it is not harmed - "
                        "but the change has not taken. Try Restore.")
    wrong = check_read_back(got, sets, drops)
    if wrong:
        raise PrepError(f"The layout read back does not match what was "
                        f"written ({wrong}). Try Restore and report this.")

    log("")
    log("Confirmed on the module: CRSF is on GPIO 3/1, "
        + ("backpack off." if drops else "backpack untouched."))
    log("Power-cycle the module, then open its port in the Link tab.")
    log("ExpressLRS accepts only certain bauds - 921600 is a good one.")
    return got


def restore(port, log, progress=None):
    """Erase the filesystem, putting the built-in layout back."""
    offset, size = find_filesystem(port, log, progress)
    log("")
    log("Erasing the filesystem partition...")
    _esptool(port, "erase_region", hex(offset), hex(size), log=log,
             progress=progress)
    log("")
    log("Done. The module is back on the layout built into its firmware:")
    log("CRSF on the module-bay pin, backpack enabled.")
    log("Power-cycle the module.")
