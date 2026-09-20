# MavJOY

A ground-side "radio" for a PC. It reads a Logitech F710 (or any USB gamepad)
and speaks CRSF to your BetaFPV 1W Micro TX module over a serial port, so the
module transmits exactly as if a handset were plugged into its JR bay.

Mapping is strictly one input to one channel — no mixing, no expo, no curves.
All of that stays on the flight controller, which is where you said you want it.

```
F710  ──USB──▶  PC  ──serial (CRSF)──▶  ELRS 1W Micro TX  ))) 2.4 GHz (((  SuperD  ──▶  FC
```

---

## Install

Needs Python 3.9+. On Python 3.13 and newer, `requirements.txt` pulls
`pygame-ce` instead of `pygame` — pygame has no wheels for those versions and
cannot build without a C toolchain. It is a drop-in fork; the `import pygame`
in this project is unchanged. Do not install both.

```bash
pip install -r requirements.txt
python app.py
```

`--sim` runs it with a synthetic gamepad so you can test the serial link and
watch the channels move without any hardware attached.

Check the whole chain offline first:

```bash
python selftest.py       # no hardware needed; works on Windows, Linux, macOS
```

---

## Connecting the module

**USB-C (recommended for the BetaFPV Micro).** Put the module in WiFi mode,
open its `/hardware.html` page and set the CRSF RX/TX pins to the ESP's UART0
pins — **3 and 1** on this module. The trick for finding them on any module:
the Backpack/Logging section on the same page already uses the USB RX/TX pins,
so copy those values into the CRSF fields and disable the backpack. You may
also need the DIP switch on the back set to firmware-upgrade mode. The module
then appears as a normal COM port.

**FTDI on the JR-bay pin.** Wire the adapter's TX to the module's CRSF
(S.Port) pin. That pin is an *inverted half-duplex* UART, so you need a real
FT232R reprogrammed with FT_PROG to invert TXD/RXD — a CP2102 or CH340 cannot
do this. Don't attach a LiPo to the module while it shares a ground with your
motherboard.

Baud: **115200 over a CP210x USB-C connection** — not 400000. ExpressLRS
auto-detects from `{400000, 115200, 5250000, 3750000, 1870000, 921600,
2250000}`, but the CP2102 bridge on the BetaFPV Micro does not divide 400000
cleanly, so the module never sees a valid frame and stays silent. Measured on
a BFPV 2G4Micro1W: 115200 and 921600 both give clean telemetry with zero CRC
errors; 400000 returns nothing at all. 115200 caps the packet rate at 250 Hz,
which is the default here. Use 921600 if you want 500 Hz.

Verify with `python probe.py` — it pings the module and prints its name if
CRSF is getting through.

**If the module reboots every minute:** ExpressLRS falls back to WiFi mode
after 60 s without CRSF on its handset UART. On a module whose WiFi is faulty
that turns into a reset loop. Start the link within a minute of power-up and
keep it running, and the module stays out of WiFi mode.

Power: fit the antenna **before** powering on or the PA chip dies. USB power
alone browns out above roughly 100 mW; use the XT30 with a 2S pack for
anything more (never 3S or above).

---

## Default mapping (F710 with the rear switch in **X**)

| CH | Source | F710 control |
|----|--------|--------------|
| 1 | axis 3 | right stick X — roll / aileron |
| 2 | axis 4 | right stick Y — pitch / elevator |
| 3 | throttle engine | triggers (see below) |
| 4 | axis 0 | left stick X — yaw / rudder |
| 5 | toggle, button 7 | **Start = ARM** (latching) |
| 6 | cycle, button 3, 3 steps | Y = flight mode, steps low → mid → high |
| 7 | button 5 | RB, momentary |

**Windows: SDL's RawInput backend silently breaks joystick input here.**
Since SDL 2.0.16 it is the default for XInput-class pads, and it receives
state through `WM_INPUT` messages delivered to a window. This app runs SDL
with the dummy video driver so Tk keeps the only window, so no `WM_INPUT`
ever arrives and every axis stays pinned at its power-on value — sticks
0.0, triggers -1.0 — while Windows' own Game Controllers panel shows the
pad working perfectly. `gamepad.py` sets `SDL_JOYSTICK_RAWINPUT=0` to force
the polling path instead. Measured on an F710: 0 pygame updates against 382
XInput updates over the same 7 seconds.

That backend choice also sets the axis numbering, which is why the table
above puts the triggers on 2 and 5. Run `python inputs.py` to confirm.

Use **X mode**, not D. In D mode the two triggers share one axis, which makes
a ratcheting throttle impossible. The **Inputs** tab shows live axis and button
numbers, so you can confirm what your pad actually reports and fix the mapping
if it differs.

Channel sources: `axis`, `button` (momentary), `toggle` (latching — use for
arm), `cycle` (steps through 2–6 positions, for flight modes), `hat_x`/`hat_y`
(d-pad), `fixed`, `throttle`, `none`.

### Throttle

The F710's sticks self-centre, which is why throttle gets its own engine:

- **ramp** (default) — hold RT to increase, LT to decrease; the setting stays
  where you leave it, like a real throttle stick. Back button = instant idle.
- **trigger** — RT is throttle directly. Releasing always means idle, but you
  hold it for the entire flight.
- **axis** — raw stick. Only sane if you physically defeat the spring.

---

## Two different rates

These get confused constantly, so plainly:

| | What it is | Where you set it |
|---|---|---|
| **CRSF Hz** | how often this PC hands a frame to the module over USB | the toolbar box, or **Auto** |
| **Packet rate** | how fast the module transmits over the air | the **Module** tab |

Sending CRSF faster will never change the RF rate. The packet rate lives
inside the module, alongside TX power and telemetry ratio, and is changed
through the CRSF parameter protocol — the same one the EdgeTX Lua script
speaks. The Module tab reads the list straight from your module, so the
options shown are whatever your firmware actually supports rather than a
hardcoded guess.

### Auto

Leave **Auto** ticked and the app works the rate out for itself. ExpressLRS
broadcasts the frame interval it wants in its `0x3A` sync frames — the same
ones EdgeTX uses to line a handset up with the RF slots — so the app reads
that and follows it. Change the packet rate in the Module tab and the CRSF
rate re-targets within a second, with no restart.

It deliberately does **not** match the requested rate 1:1. The PC clock and
the module's RF clock free-run against each other: you can watch the sync
frame's own offset field drift, about 16 µs per second on this hardware. At
1:1 that drift means some RF slots find no new frame and go out with stale
stick positions. So the app aims for roughly 3x the requested rate, clamped
to whatever the baud allows, which keeps every RF packet carrying data only
a few milliseconds old. At 115200 with a module asking 100 Hz, that lands on
250 Hz.

If the baud cannot give at least double the requested rate the app says so
in the log and suggests raising it to 921600.

Untick Auto to type a rate by hand. Either way the rate can now be changed
on a running link — it no longer needs a stop and start.

ExpressLRS refuses some rates depending on the telemetry ratio and switch
mode. On a BFPV 2G4Micro1W running ELRS 4.1, 150 / 250 / D250 all take, and
50Hz is rejected. When that happens the app tells you and shows what the
module is actually on, rather than pretending the change landed.

Changing the packet rate re-keys the link, so the receiver failsafes for a
moment while both ends resync. The app refuses to do it while armed, and
asks before doing it at all.

A write is also not instant — ExpressLRS applies and saves it
asynchronously, so the app waits before reading the value back. Read it too
soon and you get the *old* value, which looks exactly like a rejected write.

## Failsafe

If channel data stops — gamepad unplugged, dongle jammed, app closed, Esc
pressed — the app **stops writing frames entirely**. ExpressLRS runs a 1 second
watchdog on its handset UART, so the TX drops the RF link and your receiver
falls into its own failsafe. It is never correct to keep transmitting the last
known stick positions, so the app doesn't.

That means **the receiver's failsafe is the real failsafe**. Set it up in
Betaflight/INAV/ArduPilot and test it on the bench (props off) before flying.

Other safety behaviour:

- The gamepad is polled on its own thread, so a frozen GUI cannot affect
  control, and input older than 150 ms counts as dead.
- A link will not start unless throttle reads 0 % and every latch is reset.
- **Esc** stops the link instantly from anywhere in the app.
- The link thread runs at raised priority, and on Windows it requests 1 ms
  timer resolution (otherwise `sleep()` granularity is ~15 ms and the frame
  rate collapses). Watch the jitter figure next to the rate.

---

## Before the first flight

1. Props off. Bench test everything below before anything spins.
2. Confirm each surface moves the right way in the FC's receiver tab — the app
   sends raw axes, so all reversing happens on the FC.
3. Confirm 172 / 992 / 1811 show as 988 / 1500 / 2012 µs on the FC.
4. Pull the F710's USB dongle out mid-test and confirm the RX goes to failsafe.
5. Close the app while "armed" and confirm the same.
6. **Your gamepad dongle and your TX are both on 2.4 GHz.** A 1 W transmitter
   next to that dongle will desense it, and you'll lose input while the link to
   the plane still looks perfect. Put the dongle on a USB extension well away
   from and below the TX antenna, and test at your intended power level on the
   ground first.

---

## Files

| | |
|---|---|
| `app.py` | GUI, entry point |
| `crsf.py` | protocol: CRC-8/0xD5, frame packing, telemetry decode |
| `gamepad.py` | input thread and the channel mapper / throttle engine |
| `link.py` | serial link thread, timing, failsafe watchdog |
| `config.py` | defaults; `config.json` is written next to it when you save |
| `selftest.py` | hardware-free end-to-end test |
| `probe.py` | asks the TX module to identify itself; finds the working baud |
| `inputs.py` | live gamepad monitor; confirms the pad is alive and reads axis numbers |

Protocol constants were taken from the ExpressLRS firmware source
(`src/include/crsf_protocol.h`, `src/lib/Handset/CRSFHandset.cpp`) rather than
from documentation, so the sync bytes, baud list and channel scaling match what
the module actually accepts.
