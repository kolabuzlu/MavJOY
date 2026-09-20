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

Baud: **921600 over a CP210x USB-C connection** — not 400000, which is
what the ExpressLRS documentation tells you to use. ExpressLRS auto-detects
from `{400000, 115200, 5250000, 3750000, 1870000, 921600, 2250000}`, but the
CP2102 bridge on the BetaFPV Micro does not divide 400000 cleanly, so the
module never sees a valid frame and stays silent. Measured on a
BFPV 2G4Micro1W: 921600 and 115200 both give clean telemetry with zero CRC
errors; 400000 returns nothing at all.

921600 is the default because the baud also decides which packet rates the
module will offer. At 115200 it lists five and blanks out two; at 921600 the
same module lists ten, including 333Hz Full, 500Hz, D500, F500 and F1000.
115200 still works if you need it, but it caps the packet rate at 250 Hz and
hides everything above.

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

## Mapping

**Nothing is mapped out of the box.** Every channel starts at source
`none`, index `none`, and you build the map yourself in the **Channels**
tab. Gamepads differ enough that a guessed default could put arm or
throttle on the wrong control, which is not a mistake worth risking on a
1 W transmitter.

The index only applies to sources that read a numbered input — `axis`,
`button`, `toggle`, `oneway`, `cycle`, `hat_x`, `hat_y`. For `none`,
`throttle` and
`fixed` it reads `none` and the box is locked, rather than showing a `0`
that does nothing.

A sane starting point for an F710 with the rear switch in **X**:

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
numbers, so you can confirm what your pad actually reports and build the
mapping to match.

Channel sources: `axis`, `button` (momentary), `toggle` (latching — use for
arm), `oneway` (one-way toggle: a press sets it high and it stays high — see
below), `cycle` (steps through 2–6 positions on each press), `switch` (a real
multi-position switch — see below), `hat_x`/`hat_y` (d-pad), `fixed`,
`throttle`, `none`.

### More than one device

A separate USB throttle is normal, so each channel picks the device it reads
with the **dev** column, and the throttle engine has its own **Device**
setting on the Throttle tab. Slot 0 is the pad; slot 1 is whatever else you
plug in, chosen in the toolbar. The Inputs tab shows one slot at a time, so
you can read off the axis numbers for either device.

The important part is the failsafe. The two devices fail independently, and
flying on a throttle that stopped reporting is no better than flying on
stale sticks, so the link checks **every** device the map actually reads and
stops transmitting if any of them goes quiet — not just the first one.
Starting a link checks all of them too.

Latches are keyed by device as well as input number, so button 3 on the pad
and button 3 on the throttle are different switches.

### Plugging and unplugging

Devices are picked up as they arrive. SDL reports arrivals and departures as
events, the input thread watches for them, and the device pickers redraw on
their own — there is no need to press the refresh button, which is still
there for when you want to force a rescan.

A slot remembers *which device* it was given, by GUID and name, not the
position it happened to occupy. Indexes are reassigned as devices come and
go, so a slot that stored only an index could silently end up pointing at a
different piece of hardware after a replug: the pad's axes read from the
throttle and the throttle engine reads the pad. Matched on identity, a
device returns to the slot it left, and a slot whose device is absent stays
empty rather than grabbing whatever is nearest.

**Coming back does not hand control over on its own.** When frames start
flowing again, every channel keeps sending the value it was last actually
transmitting, and only starts following its stick again once you move that
stick. Move nothing and nothing changes; move the flight mode switch and
that channel — and only that one — takes effect.

This exists because of what a flight controller does with the first frame
after a dropout. ArduPilot holds its failsafe, RTL, until the mode channel
changes. If something was knocked while the link was down, the old code
sent that new position the instant the link returned, the FC read it as a
deliberate mode change, and the aircraft left the failsafe it should have
been holding. Now the aircraft stays in RTL until you actually move the
switch, which is the only thing that should ever take it out.

While channels are held the link chip reads **HOLDING n CH** in amber and
the status bar names them, because a stick that is not moving its channel
otherwise looks like a broken controller. A channel releases as soon as its
input moves about 2% of travel, so a resting stick will not release itself
and a deliberate nudge will.

**Losing a device stops the pulses; it does not shut the transmitter
down.** This is the behaviour of any RC transmitter: frames stop going out,
the receiver sees no pulses and falls into its own failsafe, and when the
input comes back the frames simply resume. Nothing has to be pressed, and
nothing is asked.

Nothing is reset on the way back either. A latched arm switch that was on
is still on, and clearing it would put arm low in the first frame after
recovery, which to a model still in the air is a disarm command. The
controls are transmitted as they read, and it is the flight controller that
decides whether to leave its failsafe — on ArduPilot, RTL is held until the
mode channel changes. That decision belongs to the aircraft, not to this
app.

The device itself is picked up automatically on plug-in, so the slot fills
back in without touching refresh.

### Arming

**CH5 is the arm channel.** Always, and nothing else ever is. While CH5
reads high the app refuses to change module settings and the ARM light is
red.

There is no per-channel arm flag, and there used to be one. Alongside it the
app inferred "armed" from the source type, so a flight mode latched high on
CH6 — or a one-way on CH7 — announced itself as an arm channel and blocked
every settings write. Which channel means armed cannot be guessed from how a
channel is mapped, because the same sources are used for everything else, so
it is fixed instead. Map CH5 to whatever you arm with; map anything else to
whatever you like and it stays out of the interlock.

### Resetting a latch from another channel

A `toggle` remembers its state, which is the point of it — but sometimes you
want it dropped back to low by something else entirely, without reaching for
the button. Set **reset by** on the toggle's row to the channel that should
do it, and **moves** to how far that channel has to move, in microseconds.

Movement, not a position: the latch is cleared whenever the watched channel
travels further than that from where it was the last time it fired. A floor
is needed because a resting stick is never perfectly still — at the default
100 µs a deliberate move triggers it and normal jitter does not. Lower it if
the watched channel is a switch and you want a hair trigger; raise it if the
channel is a noisy axis.

It fires **once** per movement. The button can turn the latch straight back
on immediately, which is what resetting means — this is not an interlock
that holds the channel down while the condition lasts.

Both boxes are locked unless the source is `toggle`, `oneway` or `cycle`,
the sources that carry a latch. A `switch` reads its lever every frame, so
there is nothing stored to reset. On a `cycle`, a reset returns it to
position 1; on a `oneway`, it returns the channel to un-pressed.

### One-way toggles

A `oneway` is a toggle that only goes one way. The first press sets it high
and it stays high, however many times it is pressed after that — there is no
second press that takes it back. The only way back is a **reset by** channel,
set up exactly as above.

It is for the things a fumbled second press must not undo. A `toggle` used
for something consequential is one stray press away from being switched off
again, often without you noticing; a `oneway` cannot be, and getting it back
takes a deliberate move on a different control. It arms nothing on its own:
only CH5 does that, whatever a channel is mapped to.

### Multi-position switches

A three-way switch on a gamepad usually reports as three separate buttons
with exactly one lit at a time — position 1 lights button 8, position 2
lights 9, position 3 lights 10. That is not a `cycle`, which advances on
each press, and not a `button`, which only knows on and off.

Use `switch`: **index** is the first button and **steps** is how many
positions. For the example above, `switch`, index 8, steps 3 gives
988 / 1500 / 2012 µs — exactly the three values a flight controller expects
from a 3-position mode switch. `inv` reverses the order.

Unlike `cycle` this reads the button state rather than counting presses, so
it cannot drift out of step and picks up the switch's real position as soon
as the link starts. While a switch is between detents no button is lit, so
the channel holds its last position instead of snapping to an end stop.

If your switch's buttons are not consecutive, put the list in
`config.json` directly:

```json
{ "src": "switch", "idx": 0, "steps": 3, "buttons": [4, 7, 11] }
```

### Deadzone

The **Inputs** tab carries a deadzone per axis, next to the live values it
affects. Each row shows the raw reading and what the mapper will actually
send, so you can wind the number up until a resting stick reads zero and no
further.

Deadzone is set per axis **and per device**, because sticks wear unevenly
and a worn axis on the pad must not deaden the same-numbered axis on a
separate throttle. The boxes follow the `showing` selector, so they always
edit the device whose live values are beside them. **Apply to all** covers
every axis of the shown device.

Whatever is left outside the deadzone is rescaled, so full deflection still
reaches the end of the channel travel. The throttle engine keeps its own
deadzone on the Throttle tab, since it reads triggers rather than a
self-centring stick.

### Throttle

The F710's sticks self-centre, which is why throttle gets its own engine:

- **ramp** (default) — hold RT to increase, LT to decrease; the setting stays
  where you leave it, like a real throttle stick. Back button = instant idle.
- **trigger** — RT is throttle directly. Releasing always means idle, but you
  hold it for the entire flight.
- **axis** — raw stick. Only sane if you physically defeat the spring.

---

## Module settings

The **Module** tab is a full settings editor: everything the EdgeTX Lua
script can show or change, MavJOY can too. It walks the module's parameter
list over CRSF, folders and all, and builds the page from what comes back —
nothing is hardcoded, so whatever your firmware exposes is what you see.

On a BFPV 2G4Micro1W running ELRS 4.1 that is 21 fields, read in about a
second:

```
Packet Rate, Telem Ratio, Switch Mode, Link Mode, Model Match
TX Power/        Max Power, Dynamic, Fan Thresh
VTX Administrator/ Band/Enable, Channel, Pwr Lvl, Pitmode, Send VTx
WiFi Connectivity/ Enable WiFi, Enable Rx WiFi
BLE Joystick, Bind, 4.1.0 ISM2G4
```

Dropdowns for selections, spin boxes for numbers, buttons for commands,
and folders grouped the way the module reports them. **show hidden** reveals
the fields ExpressLRS marks hidden, such as the VTX channel and pit mode.

After any change the whole list is re-read, because ExpressLRS adjusts other
fields in response — change the packet rate and the telemetry ratio's
available options move with it.

Three safety rules apply. Nothing is written while a latch says the model is
armed. Commands ask first. And **Enable WiFi**, **Enable Rx WiFi**, **BLE
Joystick** and **Bind** take the module off the air, so the CRSF link stops
and has to be started again — the app says so before running them.

ExpressLRS refuses settings it considers invalid for the current
configuration rather than reporting an error, so the app always shows what
the module says it is on, not what was asked for.

### Settings the module will not change

Two constraints cause most of the confusion, and neither produces an error
message — ExpressLRS simply keeps the old value:

**The CRSF baud decides which packet rates exist.** At 115200 a
BFPV 2G4Micro1W offers 50, 100 Full, 150, 250 and D250, with two entries
blanked out. At 921600 the same module offers ten: 333Hz Full, 500Hz, D500,
F500 and F1000 appear as well. Nothing is wrong with the module — the
handset link cannot feed those rates, so it hides them. The Module tab says
how many are hidden and suggests raising the baud.

**Switch Mode cannot be changed while a receiver is connected.** This is
the one that looks most like a bug: you pick a mode, nothing happens, no
error. Power the model down, change it, power back up. Switch Mode also
depends on the packet rate and changes with it — at 100Hz Full the options
are `8ch`, `16ch Rate/2`, `12ch Mixed`, while at 150Hz the same field
offers `Wide` and `Hybrid`.

MavJOY asks the module why. ExpressLRS reports its state in `0x2E` status
frames, carrying flags and a sentence such as `Not while connected`, but
only when something asks for them — nothing does by default, which is why a
refused setting normally just reverts in silence. The app polls that frame,
and when a write does not take it repeats the module's own words back to
you. Warnings latch until acknowledged, so it clears them after showing
them, the same way the Lua script does.



## Appearance

MavJOY starts dark. **View > Light** switches to the system look; the choice
is saved and applied on the next start, because Tk cannot repaint widgets
that already exist from a style change alone.

Colours live in `theme.py` as named roles — `panel`, `field`, `muted`,
`accent`, `ok`, `warn`, `danger` — rather than being spelled out at each
widget, so a new theme is one more dict. The channel and throttle bars use
`accent`.

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

A write is not instant, and how long it takes depends on the field.
Measured on ELRS 4.1: Fan Thresh is already in effect 164 ms later, while
Packet Rate — which re-keys the RF link — takes about 1.7 s. The module
announces nothing when it is done; writing a field and listening for three
seconds produces no unsolicited reply at all.

So the app does not guess a delay. It writes once, then reads the field
back until it reports the value asked for or a three second deadline
passes. A quick field finishes in one round trip, a slow one gets as long
as it needs, and a genuine refusal is still reported once the deadline
expires. A fixed wait was wrong in both directions — too slow for quick
fields, and short enough that a Packet Rate change that had actually
succeeded was reported as refused.

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
- Losing an input device stops the pulses and nothing more. The link stays
  up, the receiver failsafes, and transmission resumes by itself when the
  input returns — no prompt, no button.
- While input is stale the link writes **nothing at all** — not telemetry
  requests, not settings traffic. Any well-formed frame is a frame the
  module heard, so anything on the wire undermines the watchdog that is
  meant to drop the link. `selftest.py` asserts on bytes written rather
  than on frames counted, because a settings frame is invisible to a frame
  counter while still being audible to the module.
- Module settings are refused while CH5, the arm channel, reads high,
  and a write attempted while the link is live asks first. A command that
  pauses for confirmation re-checks on the way through, so it cannot
  complete against a model armed while the dialog was open.
- **Starting a link never moves a control.** It is the only gate, and it
  only happens when you press Start — the equivalent of EdgeTX's throttle
  and switch warnings at power-on, which never fire again once you are
  flying. It reads the sticks and
  switches as they stand and shows you what the first frame will carry if
  anything looks wrong — a throttle off idle, a channel already armed — for
  you to accept or cancel. It deliberately does not force them to a safe
  value: clearing the latches would put arm low and throttle at idle in
  that first frame, and when you are restarting the link to recover a model
  that is still in the air, that frame is a disarm command. On the bench,
  set the controls off and start again; in the air, those readings are what
  keeps it flying.
- A button held down across a restart does not read as a fresh press, so a
  latch cannot flip itself while you are re-establishing the link.
- **Esc** stops the link instantly from anywhere in the app, except
  while a modal dialog is open — a dialog takes the keyboard, so cancel
  it first. The link thread is unaffected either way, and the window's
  Stop button is always live.
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
