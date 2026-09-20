#!/usr/bin/env python3
"""
Watch what actually happens when a USB device is unplugged and plugged back in.

The app's failsafe rests on one thing: when a device goes away, its slot must
stop looking live. SDL does not make that automatic - an open handle survives
the unplug and keeps returning zeros - so this prints the state as it changes
and says, in plain terms, whether the link would have stopped.

    python usbtest.py          # watch slot 0
    python usbtest.py 1        # watch slot 1

Unplug the device, wait a couple of seconds, plug it back in. Ctrl-C to stop.
"""

from __future__ import annotations

import sys
import time

import gamepad as gp


def classify(state):
    """The state that matters, without the age - so only real changes print."""
    if not state.connected:
        return "NOT REPORTING"
    if not state.is_fresh():
        return "STALE"
    return "live"


def describe(state):
    kind = classify(state)
    if kind == "live":
        return f"live, {len(state.axes)} axes, {state.age() * 1000:.0f} ms old"
    if kind == "STALE":
        return f"STALE ({state.age() * 1000:.0f} ms old)"
    return kind


def main(argv):
    slot = int(argv[0]) if argv else 0

    pad = gp.GamepadThread()
    pad.start()
    time.sleep(1.5)

    devices = pad.device_list
    if not devices:
        print("No devices found at all. Plug one in and try again.")
        pad.stop()
        return 1

    print("devices seen:")
    for d in devices:
        print(f"  [{d['index']}] {d['name']}")
    pad.select(slot, slot) if slot < len(devices) else pad.select(0, slot)
    time.sleep(1.0)

    held = pad.identity(slot)
    print(f"\nwatching slot {slot}: {(held or {}).get('name', '?')}")
    print("unplug it, wait, then plug it back in.  Ctrl-C to stop.\n")

    last = None
    transmitting = None
    try:
        while True:
            state = pad.state_for(slot)
            now = classify(state)
            if now != last:
                stamp = time.strftime("%H:%M:%S")
                print(f"{stamp}  slot {slot}: {describe(state)}")
                last = now

            # This is the test the link itself makes before sending a frame.
            would_send = state.is_fresh()
            if would_send != transmitting:
                if transmitting is not None:
                    print(f"          -> link would {'RESUME sending' if would_send else 'STOP sending'}"
                          f"{'' if would_send else ' (receiver goes to failsafe)'}")
                transmitting = would_send
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        pad.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
