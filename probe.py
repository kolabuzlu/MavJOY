#!/usr/bin/env python3
"""
Find out whether CRSF is actually reaching the TX module, and at which baud.

Sends a CRSF device ping and prints the module's own name if it answers.
Channel data is sent alongside it so ExpressLRS can lock its baud detector,
with throttle at idle and every switch low, so nothing can arm.

    python probe.py                 # try every port that looks like a module
    python probe.py COM24           # one port, every candidate baud
    python probe.py COM24 115200    # one port, one baud
"""

from __future__ import annotations

import sys
import time

import serial

import crsf
import link

# Ordered by how likely they are to work on a USB-serial bridge. 400000 is
# what ExpressLRS documents, but CP210x bridges mis-divide it; see README.
CANDIDATE_BAUDS = (115200, 921600, 400000, 1870000, 2250000)

FRAMETYPE_DEVICE_PING = 0x28
FRAMETYPE_DEVICE_INFO = 0x29
ADDRESS_BROADCAST = 0x00


def _safe_rc_frame() -> bytes:
    """Throttle idle, every switch low: the module cannot arm anything."""
    channels = [crsf.CHANNEL_MID] * crsf.NUM_CHANNELS
    channels[2] = crsf.CHANNEL_MIN      # throttle
    channels[4] = crsf.CHANNEL_MIN      # arm
    return crsf.pack_rc_channels(channels)


def _ping_frame() -> bytes:
    body = bytes([FRAMETYPE_DEVICE_PING, ADDRESS_BROADCAST,
                  crsf.ADDRESS_RADIO_TRANSMITTER])
    return bytes([crsf.CRSF_SYNC_BYTE, len(body) + 1]) + body + bytes([crsf.crc8(body)])


def _device_name(payload: bytes) -> str:
    """DEVICE_INFO payload: dest, origin, then a NUL-terminated name."""
    return payload[2:].split(b"\x00")[0].decode("ascii", "replace")


def try_baud(port: str, baud: int, seconds: float = 4.0) -> dict | None:
    """Return what the module said, or None if it stayed silent."""
    ser = serial.Serial()
    ser.port, ser.baudrate = port, baud
    ser.timeout, ser.write_timeout = 0, 0.1
    try:
        ser.open()
    except Exception as exc:
        print(f"  {baud:>8}  cannot open: {exc}")
        return None

    rc, ping = _safe_rc_frame(), _ping_frame()
    parser = crsf.Parser()
    seen: dict[int, list[bytes]] = {}

    try:
        # Opening the port toggles DTR/RTS, which resets an ESP-based module.
        time.sleep(1.5)
        ser.reset_input_buffer()

        sent = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            ser.write(rc)
            sent += 1
            if sent % 25 == 0:
                ser.write(ping)
            time.sleep(0.004)
            if ser.in_waiting:
                for _addr, ftype, payload in parser.feed(ser.read(ser.in_waiting)):
                    seen.setdefault(ftype, []).append(payload)
    except Exception as exc:
        print(f"  {baud:>8}  failed: {exc}")
        return None
    finally:
        ser.close()

    if not seen:
        print(f"  {baud:>8}  silent")
        return None

    name = ""
    if FRAMETYPE_DEVICE_INFO in seen:
        name = _device_name(seen[FRAMETYPE_DEVICE_INFO][0])
    types = " ".join(f"0x{t:02X}x{len(v)}" for t, v in sorted(seen.items()))
    print(f"  {baud:>8}  REPLIED  {name or '(no name)'}   [{types}]"
          f"{f'  crc_err={parser.crc_errors}' if parser.crc_errors else ''}")

    result = {"baud": baud, "name": name, "types": seen,
              "crc_errors": parser.crc_errors}
    for ftype, entries in seen.items():
        entry = crsf.FRAME_PARSERS.get(ftype)
        if entry:
            key, fn = entry
            parsed = fn(entries[-1])
            if parsed:
                result[key] = parsed
    return result


def main(argv):
    ports = [argv[0]] if argv else [d for d, _ in link.list_serial_ports()]
    bauds = (int(argv[1]),) if len(argv) > 1 else CANDIDATE_BAUDS

    if not ports:
        print("no serial ports found")
        return 1

    if not argv:
        print("ports seen:")
        for dev, desc in link.list_serial_ports():
            print(f"  {dev:<8} {desc}")
        print()

    working = []
    for port in ports:
        print(f"{port}:")
        for baud in bauds:
            found = try_baud(port, baud)
            if found:
                working.append((port, found))
        print()

    if not working:
        print("No module answered. Things worth checking, in order:")
        print("  - CRSF RX/TX pins set to the ESP's UART0 pins (3 and 1) on")
        print("    the module's /hardware.html page, with the backpack disabled")
        print("  - the DIP switch on the back, if the module has one")
        print("  - the module powered and out of WiFi mode")
        return 1

    port, best = working[0]
    print(f"Use port {port} at {best['baud']} baud"
          + (f" ({best['name']})" if best["name"] else ""))
    if best.get("link"):
        d = best["link"]
        print(f"  link: uplink LQ {d['up_lq']}%, downlink LQ {d['dn_lq']}%, "
              f"TX power {d['tx_power_mw']} mW")
    if best.get("battery"):
        d = best["battery"]
        print(f"  battery telemetry from the model: {d['voltage']:.1f} V, "
              f"{d['current']:.1f} A, {d['remaining']}% remaining")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
