#!/usr/bin/env python3
"""
Live gamepad monitor. Run it, move things, watch the numbers. Ctrl-C to quit.

Use it to confirm the pad is alive and to read off the axis and button
numbers your hardware actually reports, which is what config.json needs.

    python inputs.py          # first gamepad
    python inputs.py 1        # gamepad index 1
"""

from __future__ import annotations

import sys
import time

import gamepad as gp

AXIS_HINT = {0: "left X", 1: "left Y", 2: "right X", 3: "right Y",
             4: "left trig", 5: "right trig"}


def main(argv):
    index = int(argv[0]) if argv else 0

    pad = gp.GamepadThread()
    pad.start()
    time.sleep(0.8)

    devices = pad.devices
    if not devices:
        print("No gamepad found. Check the dongle is plugged in.")
        pad.stop()
        return 1
    print("gamepads seen:")
    for i, name in enumerate(devices):
        print(f"  [{i}] {name}")
    if index >= len(devices):
        print(f"\nno gamepad at index {index}")
        pad.stop()
        return 1

    pad.select(index)
    time.sleep(0.8)
    print(f"\nwatching [{index}] {devices[index]} - move everything. Ctrl-C to stop.\n")

    changes = 0
    last = None
    try:
        while True:
            st = pad.state
            if not st.connected:
                print("\rgamepad not connected" + " " * 40, end="")
                time.sleep(0.1)
                continue

            axes = tuple(round(a, 2) for a in st.axes)
            buttons = tuple(i for i, b in enumerate(st.buttons) if b)
            now = (axes, buttons, st.hats)
            if last is not None and now != last:
                changes += 1
            last = now

            cells = " ".join(
                f"{i}:{v:+.2f}{'*' if AXIS_HINT.get(i) and abs(v) > 0.15 else ' '}"
                for i, v in enumerate(axes))
            print(f"\r{cells}  btn={list(buttons) or '-':<12} "
                  f"hat={st.hats[0] if st.hats else '-'}  "
                  f"changes={changes}   ", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        pad.stop()

    print(f"\n\n{changes} state changes seen.")
    if changes == 0:
        print("Nothing moved. The pad is enumerated but silent - on an F710 that\n"
              "usually means the handset is off, its batteries are flat, or it\n"
              "lost pairing with the dongle. Check the green LED above the D-pad.")
    else:
        print("Pad is alive. Note the axis numbers that moved and set them in\n"
              "config.json (or the Mapping tab).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
