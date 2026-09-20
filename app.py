#!/usr/bin/env python3
"""
MavJOY - a ground-side radio for a PC.

Reads a USB gamepad and speaks CRSF to an
ExpressLRS TX module over a serial port, so the module transmits exactly
as if a handset were plugged into it.

    python app.py            normal use
    python app.py --sim      simulated gamepad, for bench testing the link
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

import config as configmod
import crsf
import gamepad as gp
import link as linkmod
import theme

REFRESH_MS = 50          # GUI refresh, 20 Hz
BAR_LEN = 150


def fmt_channel(value: int) -> str:
    return f"{value:4d}  ({crsf.crsf_to_us(value):.0f}\u00b5s)"


class App(tk.Tk):
    def __init__(self, simulate=False):
        super().__init__()
        self.title("MavJOY")
        self.geometry("1000x760")
        self.minsize(900, 700)

        self.cfg, warning = configmod.load()
        self.pal = theme.apply(self, self.cfg.get("theme", theme.DEFAULT))
        self.simulate = simulate
        self.link = None
        self._events = queue.Queue(maxsize=500)
        self._log_lines = 0
        self._ports = []
        self._module_q = queue.Queue()      # settings replies, link thread -> GUI
        self._fields = {}                   # index -> ParamField from the module
        self._field_vars = {}               # index -> the tk var editing it
        self._cmd_index = None              # command currently running
        self._telem_last = ""               # last telemetry text drawn
        self._last_mode = None              # last flight-mode frame decoded
        self._arm_watch = crsf.ArmWatch()   # reads armed from the mode name
        self._armed_report = None           # True, False, or None for unknown
        self._mode_since = 0.0              # first flight-mode frame
        self._said_star_hint = False
        self._pending_write = None          # (field index, value) we asked for
        self._devices_seq = -1              # last device list we drew
        self._device = None                 # DEVICE_INFO from the module

        self.gamepad = (gp.SimGamepadThread() if simulate else gp.GamepadThread())
        self.gamepad.start()
        self.mixer = gp.Mixer(self.cfg)
        self.mixer.reset()
        self._restored = self.mixer.restore_latches(self.cfg.get("latches"))

        self._build_menu()
        self._build_top()
        self._build_notebook()
        self._build_statusbar()

        self.bind("<Escape>", lambda _e: self.stop_link(reason="Esc pressed"))
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._maximize()
        self.refresh_ports()
        self.after(400, self.refresh_gamepads)   # give the pad thread time to scan
        self.after(REFRESH_MS, self._tick)

        if warning:
            self.log("warn", warning)
        if simulate:
            self.log("warn", "SIMULATION MODE - gamepad input is synthetic")
        if self._restored:
            names = ", ".join(f"CH{n}" for n in self._restored)
            self.log("info", f"Carried over from last time: {names}. Anything "
                             f"moved while the app was shut is not in there, "
                             f"so check the Channels tab before starting a "
                             f"link.")
            vals = self.mixer.latched_values()
            high = [n for n in self._restored
                    if vals.get(n, crsf.CHANNEL_MIN) > crsf.CHANNEL_MID]
            if high:
                self.log("warn", "Restored HIGH: "
                                 + ", ".join(f"CH{n}" for n in high)
                                 + ". Starting a link sends that straight out; "
                                   "the confirmation before the first frame "
                                   "lists it again.")
        self.log("info", "Ready. Fit the antenna and power the module before starting a link.")

    # =================================================================== UI
    def _maximize(self):
        """Open filling the screen. Tk spells this differently per platform."""
        try:
            self.state("zoomed")                    # Windows, macOS
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)    # most Linux window managers
            except tk.TclError:
                pass                                # leave it at the set geometry

    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Save configuration", command=self.save_config)
        filemenu.add_command(label="Reload configuration", command=self.reload_config)
        filemenu.add_command(label="Reset to defaults", command=self.reset_config)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.on_close)
        menubar.add_cascade(label="File", menu=filemenu)

        viewmenu = tk.Menu(menubar, tearoff=0)
        self.theme_var = tk.StringVar(
            value=self.cfg.get("theme", theme.DEFAULT))
        for name in ("dark", "light"):
            viewmenu.add_radiobutton(label=name.capitalize(), value=name,
                                     variable=self.theme_var,
                                     command=self.on_theme_changed)
        menubar.add_cascade(label="View", menu=viewmenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.config(menu=menubar)

    LOGO_FILE = "mavjoyback.png"
    LOGO_MARGIN = 3         # breathing room above and below
    LOGO_MIN = 24           # below this it is not worth drawing

    def _load_logo(self, target_px):
        """The logo, scaled to about target_px tall, or None if it is absent.

        Tk scales by whole-number factors only, so the result lands near the
        target rather than on it. A missing or unreadable file is not an
        error: this is decoration, and the app has to start without it.
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            self.LOGO_FILE)
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            return None
        # Round the factor UP, so the result is never taller than asked
        # for: rounding down overshoots, which is exactly the pixel or two
        # that makes the panel grow.
        factor = -(-img.height() // max(1, target_px))
        return img.subsample(factor, factor) if factor > 1 else img

    def _place_logo(self, panel, rows):
        """Put the logo in the gap the controls and the buttons leave.

        Sized from what the two rows of controls already need, so the panel
        never grows a pixel to make room for it: a logo is decoration and
        must not push the working parts of the window about. Tk scales by
        whole-number factors only, so the fit is to the largest factor that
        still comes in under that height.

        Packed last, which puts it inboard of the buttons, and with expand
        so the row's slack goes to it and it sits centred in the gap rather
        than against one end - and stays centred as the window is resized.
        """
        rows.update_idletasks()
        available = rows.winfo_reqheight() - 2 * self.LOGO_MARGIN
        if available < self.LOGO_MIN:
            return

        # Held on self: Tk discards an image nothing references, and the
        # widget then just draws blank.
        self._logo_img = self._load_logo(available)
        if self._logo_img is None:
            return
        # The PNG has a solid black background and no alpha, so the label is
        # told to match rather than leaving the panel colour framing a
        # black square.
        tk.Label(panel, image=self._logo_img, bg="#000000",
                 borderwidth=0, highlightthickness=0).pack(
            side="right", expand=True)

    def _build_top(self):
        top = ttk.LabelFrame(self, text="Link")
        top.pack(fill="x", padx=8, pady=(8, 4))

        # Three things share this panel: the two rows of controls on the
        # left, the buttons on the right, and the logo centred in what is
        # left between them. The buttons sit at panel level rather than in
        # the second row so that they and the logo are both centred on the
        # panel's full height instead of on one row of it.
        rows = ttk.Frame(top)
        rows.pack(side="left")

        buttons = ttk.Frame(top)
        buttons.pack(side="right", padx=4)

        row = ttk.Frame(rows)
        row.pack(fill="x", padx=6, pady=6)

        ttk.Label(row, text="Serial port").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var,
                                       width=34, state="readonly")
        self.port_combo.pack(side="left", padx=(4, 2))
        ttk.Button(row, text="\u21bb", width=3,
                   command=self.refresh_ports).pack(side="left")

        ttk.Label(row, text="Baud").pack(side="left", padx=(12, 0))
        self.baud_var = tk.StringVar(value=str(self.cfg["baud"]))
        ttk.Combobox(row, textvariable=self.baud_var, width=9, state="readonly",
                     values=[str(b) for b in crsf.SUPPORTED_BAUDS]).pack(side="left", padx=4)

        ttk.Label(row, text="CRSF Hz").pack(side="left", padx=(12, 0))
        self.rate_var = tk.IntVar(value=self.cfg["rate_hz"])
        self.rate_spin = ttk.Spinbox(row, from_=50, to=500, increment=25, width=6,
                                     textvariable=self.rate_var)
        self.rate_spin.pack(side="left", padx=4)
        self.rate_auto = tk.BooleanVar(value=bool(self.cfg.get("rate_auto", True)))
        ttk.Checkbutton(row, text="Auto", variable=self.rate_auto,
                        command=self._on_rate_auto).pack(side="left")
        self.rate_hint = ttk.Label(row, foreground=self.pal["muted"],
                                   text="(PC→module, not the RF rate)")
        self.rate_hint.pack(side="left", padx=(6, 0))
        self._rate_warned = None
        self._on_rate_auto()

        row2 = ttk.Frame(rows)
        row2.pack(fill="x", padx=6, pady=(0, 6))

        ttk.Label(row2, text="Gamepad").pack(side="left")
        self.pad_var = tk.StringVar()
        self.pad_combo = ttk.Combobox(row2, textvariable=self.pad_var,
                                      width=28, state="readonly")
        self.pad_combo.pack(side="left", padx=(4, 2))
        self.pad_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self.on_pad_selected(0))

        # A second slot, for a throttle or anything else on its own USB device.
        ttk.Label(row2, text="Device 1").pack(side="left", padx=(10, 0))
        self.pad_var_b = tk.StringVar()
        self.pad_combo_b = ttk.Combobox(row2, textvariable=self.pad_var_b,
                                        width=28, state="readonly")
        self.pad_combo_b.pack(side="left", padx=(4, 2))
        self.pad_combo_b.bind("<<ComboboxSelected>>",
                              lambda _e: self.on_pad_selected(1))
        ttk.Button(row2, text="\u21bb", width=3,
                   command=self.refresh_gamepads).pack(side="left")

        self.start_btn = ttk.Button(buttons, text="START LINK",
                                    command=self.start_link)
        self.start_btn.pack(side="right", padx=4)
        self.stop_btn = ttk.Button(buttons, text="STOP  (Esc)", state="disabled",
                                   command=lambda: self.stop_link(reason="stopped by user"))
        self.stop_btn.pack(side="right", padx=4)

        self._place_logo(top, rows)

        # ---- big live status strip
        status = ttk.Frame(self)
        status.pack(fill="x", padx=8, pady=4)

        self.link_lbl = tk.Label(status, text="LINK STOPPED", width=18,
                                 font=("TkDefaultFont", 13, "bold"),
                                 bg=self.pal["idle"], fg=self.pal["on_accent"], padx=8, pady=8)
        self.link_lbl.pack(side="left")

        self.arm_lbl = tk.Label(status, text="ARM: \u2014", width=14,
                                font=("TkDefaultFont", 13, "bold"),
                                bg=self.pal["idle"], fg=self.pal["on_accent"], padx=8, pady=8)
        self.arm_lbl.pack(side="left", padx=(8, 0))

        self.lq_lbl = tk.Label(status, text="LQ: \u2014", width=14,
                               font=("TkDefaultFont", 13, "bold"),
                               bg=self.pal["idle"], fg=self.pal["on_accent"],
                               padx=8, pady=8)
        self.lq_lbl.pack(side="left", padx=(8, 0))

        # What the model says it is doing, in its own words. Every other
        # chip here is inferred from what we send; this one is reported.
        self.mode_lbl = tk.Label(status, text="MODE: —", width=16,
                                 font=("TkDefaultFont", 13, "bold"),
                                 bg=self.pal["idle"], fg=self.pal["on_accent"],
                                 padx=8, pady=8)
        self.mode_lbl.pack(side="left", padx=(8, 0))

        thr_frame = ttk.Frame(status)
        thr_frame.pack(side="left", padx=16)
        ttk.Label(thr_frame, text="Throttle").pack(anchor="w")
        self.thr_bar = ttk.Progressbar(thr_frame, length=220, maximum=100)
        self.thr_bar.pack(side="left")
        self.thr_lbl = ttk.Label(thr_frame, text="  0 %", width=6)
        self.thr_lbl.pack(side="left", padx=4)

        info = ttk.Frame(status)
        info.pack(side="right")
        self.rate_lbl = ttk.Label(info, text="\u2014 Hz", width=22, anchor="e")
        self.rate_lbl.pack(anchor="e")
        self.rf_lbl = ttk.Label(info, text="no telemetry", width=34, anchor="e")
        self.rf_lbl.pack(anchor="e")

    def _build_notebook(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)
        self._build_channels_tab(nb)
        self._build_throttle_tab(nb)
        self._build_inputs_tab(nb)
        self._build_outputs_tab(nb)
        self._build_module_tab(nb)
        self._build_telemetry_tab(nb)
        self._build_log_tab(nb)

    # ------------------------------------------------------------- outputs
    OUT_MIN_US = crsf.US_MIN
    OUT_MAX_US = crsf.US_MAX

    def _build_outputs_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Outputs")

        ttk.Label(tab, wraplength=900, justify="left",
                  foreground=self.pal["muted"],
                  text="What full travel is worth in microseconds, per "
                       "channel. Everything else in the app works in full "
                       "travel; this is the last thing applied before a "
                       "frame goes out, so these are the numbers the flight "
                       "controller sees.").pack(anchor="w", padx=10,
                                                pady=(10, 2))
        ttk.Label(tab, wraplength=900, justify="left",
                  foreground=self.pal["muted"],
                  text="Centre stays at 1500 and each half is scaled on its "
                       "own, the way a handset's output limits work. Pulling "
                       "max down to 1900 shortens the throw one way without "
                       "moving neutral — scaling the whole range instead "
                       "would drag neutral with it and leave the model "
                       "permanently out of trim.").pack(anchor="w", padx=10,
                                                        pady=(0, 8))

        grid = ttk.Frame(tab)
        grid.pack(fill="both", expand=True, padx=10)
        for col, (title, weight) in enumerate(
                (("", 0), ("", 0), ("min µs", 0), ("max µs", 0),
                 ("sent", 0), ("", 1))):
            grid.columnconfigure(col, weight=weight)
            ttk.Label(grid, text=title, foreground=self.pal["muted"]).grid(
                row=0, column=col, sticky="w", padx=(0, 10), pady=(0, 2))
        ttk.Separator(grid, orient="horizontal").grid(
            row=1, column=0, columnspan=6, sticky="ew", pady=(0, 6))

        self.out_widgets = []
        for i in range(crsf.NUM_CHANNELS):
            ch = self.mixer.channels[i]
            row = i + 2
            ttk.Label(grid, text=f"CH{i + 1}",
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Label(grid, text=configmod.CHANNEL_HINTS[i], width=16,
                      foreground=self.pal["muted"]).grid(
                row=row, column=1, sticky="w", padx=(0, 10))

            lo = tk.StringVar(value=str(ch.out_min))
            lo_spin = ttk.Spinbox(grid, from_=self.OUT_MIN_US, to=self.OUT_MAX_US,
                                  increment=10, width=6, textvariable=lo,
                                  command=lambda n=i: self.on_output_changed(n))
            lo_spin.grid(row=row, column=2, sticky="w", padx=(0, 10))
            lo_spin.bind("<KeyRelease>",
                         lambda _e, n=i: self.on_output_changed(n))

            hi = tk.StringVar(value=str(ch.out_max))
            hi_spin = ttk.Spinbox(grid, from_=self.OUT_MIN_US, to=self.OUT_MAX_US,
                                  increment=10, width=6, textvariable=hi,
                                  command=lambda n=i: self.on_output_changed(n))
            hi_spin.grid(row=row, column=3, sticky="w", padx=(0, 10))
            hi_spin.bind("<KeyRelease>",
                         lambda _e, n=i: self.on_output_changed(n))

            sent = ttk.Label(grid, text="—", width=12, anchor="e")
            sent.grid(row=row, column=4, sticky="e", padx=(0, 10))

            bar = ttk.Progressbar(grid, maximum=1000)
            bar.grid(row=row, column=5, sticky="ew", pady=2)

            self.out_widgets.append({"lo": lo, "hi": hi, "sent": sent,
                                     "bar": bar})

        foot = ttk.Frame(tab)
        foot.pack(fill="x", padx=10, pady=8)
        ttk.Button(foot, text="Full travel on every channel",
                   command=self.reset_outputs).pack(side="left")

    def on_output_changed(self, i):
        """Swap one channel's endpoints in, whole.

        A new ChannelMap replaces the old one in a single assignment, the
        same as a mapping edit: the link thread reads this list while it
        runs, and a half-edited entry would go out on the wire.
        """
        w = self.out_widgets[i]
        old = self.mixer.channels[i]
        try:
            lo = int(float(w["lo"].get()))
            hi = int(float(w["hi"].get()))
        except (TypeError, ValueError):
            return                      # mid-typing; the box is not a number
        lo = max(self.OUT_MIN_US, min(self.OUT_MAX_US, lo))
        hi = max(self.OUT_MIN_US, min(self.OUT_MAX_US, hi))
        if lo == old.out_min and hi == old.out_max:
            return
        self.mixer.channels[i] = dataclasses.replace(old, out_min=lo,
                                                     out_max=hi)
        self.cfg["channels"][i] = self.mixer.channels[i].to_dict()

    def reset_outputs(self):
        for i in range(crsf.NUM_CHANNELS):
            self.out_widgets[i]["lo"].set(str(self.OUT_MIN_US))
            self.out_widgets[i]["hi"].set(str(self.OUT_MAX_US))
            self.on_output_changed(i)
        self.log("info", "Every channel back to full travel.")

    # -------------------------------------------------------------- module
    def _build_module_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Module")

        head = ttk.Frame(tab)
        head.pack(fill="x", padx=10, pady=(10, 2))
        ttk.Label(head, text="ExpressLRS module settings",
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.module_btn = ttk.Button(head, text="Read from module",
                                     command=self.read_module_settings)
        self.module_btn.pack(side="right")
        self.module_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(head, text="show hidden", variable=self.module_hidden,
                        command=self._render_fields).pack(side="right", padx=8)

        self.module_info_lbl = ttk.Label(
            tab, foreground=self.pal["muted"],
            text="Start the link, then read the settings from the module.")
        self.module_info_lbl.pack(anchor="w", padx=10, pady=(0, 6))

        # Everything the module exposes, in its own folder structure - the
        # same list the EdgeTX Lua script walks.
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0,
                           background=self.pal["panel"])
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        self.module_body = ttk.Frame(canvas)
        self.module_body.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.module_body, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        # bind_all would capture the wheel for the whole application, so it
        # is only hooked up while the pointer is actually over this canvas.
        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        self._render_fields()

    # ------------------------------------------------------------ channels
    # One grid holds the headings and every row, so a column is a column and
    # nothing has to be lined up by guessing widths.
    CH_COLUMNS = (
        # heading,   anchor,   pad-left, pad-right, stretch
        ("",         "w",       0,  6, 0),   # 0 CH number
        ("dev",      "w",       0,  6, 0),   # 1 which gamepad
        ("source",   "w",       0,  6, 0),   # 2 source
        ("index",    "w",       0,  6, 0),   # 3 index
        ("inv",      "center",  0,  6, 0),   # 4 invert
        ("steps",    "w",       0, 14, 0),   # 5 steps
        ("reset by",  "w",      0,  6, 0),   # 6 latch reset: watched channel
        ("moves",    "w",       0, 14, 0),   # 7 latch reset: how far, in us
        ("value",    "w",       0,  8, 1),   # 8 bar
        ("",         "e",       0, 10, 0),   # 9 numeric value
        ("",         "w",       0,  0, 0),   # 10 hint
    )

    def _build_channels_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Channels")

        grid = ttk.Frame(tab)
        grid.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        for col, (title, anchor, padl, padr, stretch) in enumerate(self.CH_COLUMNS):
            grid.columnconfigure(col, weight=stretch)
            ttk.Label(grid, text=title, anchor=anchor,
                      foreground=self.pal["muted"]).grid(
                row=0, column=col, sticky="ew", padx=(padl, padr), pady=(0, 2))

        ttk.Separator(grid, orient="horizontal").grid(
            row=1, column=0, columnspan=len(self.CH_COLUMNS),
            sticky="ew", pady=(0, 6))

        self.ch_widgets = []
        for i in range(crsf.NUM_CHANNELS):
            chcfg = self.mixer.channels[i]
            row = i + 2

            def place(widget, col, sticky="w"):
                _t, _a, padl, padr, _s = self.CH_COLUMNS[col]
                widget.grid(row=row, column=col, sticky=sticky,
                            padx=(padl, padr), pady=2)
                return widget

            place(ttk.Label(grid, text=f"CH{i + 1}",
                            font=("TkDefaultFont", 9, "bold")), 0)

            dev = tk.StringVar(value=str(chcfg.dev))
            dev_combo = ttk.Combobox(grid, textvariable=dev, width=3,
                                     state="readonly", values=("0", "1"))
            place(dev_combo, 1)
            dev_combo.bind("<<ComboboxSelected>>",
                           lambda _e, n=i: self.on_channel_changed(n))

            src = tk.StringVar(value=chcfg.src)
            combo = ttk.Combobox(grid, textvariable=src, width=9,
                                 state="readonly", values=list(gp.SOURCES))
            place(combo, 2)
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, n=i: self.on_channel_changed(n))

            idx = tk.StringVar(value=str(chcfg.idx))
            spin = ttk.Spinbox(grid, from_=0, to=31, width=5, textvariable=idx,
                               command=lambda n=i: self.on_channel_changed(n))
            place(spin, 3)
            spin.bind("<KeyRelease>", lambda _e, n=i: self.on_channel_changed(n))

            inv = tk.BooleanVar(value=chcfg.inv)
            place(ttk.Checkbutton(grid, variable=inv,
                                  command=lambda n=i: self.on_channel_changed(n)),
                  4, sticky="")

            steps = tk.StringVar(value=str(chcfg.steps))
            steps_spin = ttk.Spinbox(grid, from_=2, to=6, width=4,
                                     textvariable=steps,
                                     command=lambda n=i: self.on_channel_changed(n))
            place(steps_spin, 5)

            reset_ch = tk.StringVar(value=self._reset_label(chcfg.reset_ch))
            reset_combo = ttk.Combobox(
                grid, textvariable=reset_ch, width=6, state="readonly",
                values=[self.NO_INDEX] + [f"CH{n}" for n in
                                          range(1, crsf.NUM_CHANNELS + 1)])
            place(reset_combo, 6)
            reset_combo.bind("<<ComboboxSelected>>",
                             lambda _e, n=i: self.on_channel_changed(n))

            reset_move = tk.StringVar(value=str(chcfg.reset_move))
            reset_spin = ttk.Spinbox(grid, from_=10, to=500, increment=10,
                                     width=5, textvariable=reset_move,
                                     command=lambda n=i: self.on_channel_changed(n))
            place(reset_spin, 7)
            reset_spin.bind("<KeyRelease>",
                            lambda _e, n=i: self.on_channel_changed(n))

            bar = ttk.Progressbar(grid, maximum=1000)
            place(bar, 8, sticky="ew")

            val = ttk.Label(grid, text="—", anchor="e", width=14)
            place(val, 9, sticky="e")

            place(ttk.Label(grid, text=configmod.CHANNEL_HINTS[i], width=16,
                            foreground=self.pal["muted"]), 10)

            self.ch_widgets.append({"src": src, "idx": idx, "inv": inv,
                                    "steps": steps, "bar": bar, "val": val,
                                    "spin": spin, "steps_spin": steps_spin,
                                    "dev": dev,
                                    "reset_ch": reset_ch, "reset_move": reset_move,
                                    "reset_combo": reset_combo,
                                    "reset_spin": reset_spin})
            self._sync_row_widgets(i)

        ttk.Label(tab, text="Mapping is one input to one channel. No mixing, no expo, "
                            "no curves — do all of that on the flight controller.",
                  foreground=self.pal["muted"]).pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(tab, foreground=self.pal["muted"], wraplength=900, justify="left",
                  text="dev picks which gamepad a channel reads, for setups with a "
                       "separate USB throttle. CH5 is the arm channel: while it "
                       "reads high the app will not change module settings and the "
                       "ARM light is red. That is fixed - no other channel arms, "
                       "whatever it is mapped to, so a latch on a flight mode "
                       "channel is just a flight mode.").pack(
            anchor="w", padx=10, pady=(0, 2))
        self.src_help = ttk.Label(tab, text="", foreground=self.pal["faint"])
        self.src_help.pack(anchor="w", padx=10, pady=(0, 8))

    # ------------------------------------------------------------ throttle
    def _build_throttle_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Throttle")
        t = self.cfg["throttle"]

        frm = ttk.Frame(tab)
        frm.pack(fill="x", padx=12, pady=12)

        ttk.Label(frm, text="Device", width=14).grid(row=0, column=0, sticky="w")
        self.thr_dev = tk.StringVar(value=str(t.get("dev", 0)))
        dev_combo = ttk.Combobox(frm, textvariable=self.thr_dev, width=10,
                                 state="readonly", values=("0", "1"))
        dev_combo.grid(row=0, column=1, sticky="w")
        dev_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_throttle_changed())
        ttk.Label(frm, foreground=self.pal["muted"],
                  text="which gamepad the throttle reads — pick Device 1 for a "
                       "separate USB throttle").grid(row=0, column=2, columnspan=2,
                                                     sticky="w", padx=8)

        ttk.Label(frm, text="Mode", width=14).grid(row=1, column=0, sticky="w")
        self.thr_mode = tk.StringVar(value=t["mode"])
        mode_combo = ttk.Combobox(frm, textvariable=self.thr_mode, width=10,
                                  state="readonly", values=list(gp.THROTTLE_MODES))
        mode_combo.grid(row=1, column=1, sticky="w")
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_throttle_changed())

        self.thr_help = ttk.Label(frm, text="", wraplength=620, justify="left",
                                  foreground=self.pal["faint"])
        self.thr_help.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 14))

        def spin(label, key, row, lo, hi, hint=""):
            ttk.Label(frm, text=label, width=14).grid(row=row, column=0, sticky="w",
                                                      pady=3)
            var = tk.IntVar(value=t.get(key, 0))
            sp = ttk.Spinbox(frm, from_=lo, to=hi, width=5, textvariable=var,
                             command=self.on_throttle_changed)
            sp.grid(row=row, column=1, sticky="w")
            sp.bind("<KeyRelease>", lambda _e: self.on_throttle_changed())
            ttk.Label(frm, text=hint, foreground=self.pal["muted"]).grid(row=row, column=2,
                                                                 sticky="w", padx=8)
            return var

        self.thr_axis = spin("Up / axis", "axis", 3, -1, 31,
                             "axis or button that raises throttle (F710 X mode: RT = axis 5)")
        self.thr_axis_dn = spin("Down", "axis_down", 4, -1, 31,
                                "ramp mode only (LT = axis 2)")
        self.thr_cut = spin("Cut button", "cut_button", 5, -1, 31,
                            "instantly drops throttle to idle (Back = button 6)")

        ttk.Label(frm, text="Ramp rate", width=14).grid(row=6, column=0, sticky="w",
                                                        pady=3)
        self.thr_rate = tk.DoubleVar(value=t.get("ramp_rate", 0.6))
        ttk.Scale(frm, from_=0.1, to=2.0, variable=self.thr_rate, length=200,
                  command=lambda _v: self.on_throttle_changed()
                  ).grid(row=6, column=1, columnspan=2, sticky="w", padx=(0, 8))
        self.thr_rate_lbl = ttk.Label(frm, text="", foreground=self.pal["muted"])
        self.thr_rate_lbl.grid(row=6, column=3, sticky="w")

        ttk.Label(frm, text="Deadzone", width=14).grid(row=7, column=0, sticky="w",
                                                       pady=3)
        self.thr_dz = tk.DoubleVar(value=t.get("deadzone", 0.06))
        ttk.Scale(frm, from_=0.0, to=0.3, variable=self.thr_dz, length=200,
                  command=lambda _v: self.on_throttle_changed()
                  ).grid(row=7, column=1, columnspan=2, sticky="w", padx=(0, 8))
        self.thr_dz_lbl = ttk.Label(frm, text="", foreground=self.pal["muted"])
        self.thr_dz_lbl.grid(row=7, column=3, sticky="w")

        # Stick deadzone lives on the Inputs tab now, per axis, next to the
        # live values it affects. A second slider here would appear to do
        # nothing once any axis had its own value.
        ttk.Label(frm, text="Stick deadzone", width=14).grid(row=8, column=0,
                                                             sticky="w", pady=3)
        ttk.Label(frm, foreground=self.pal["muted"],
                  text="set per axis on the Inputs tab").grid(
            row=8, column=1, columnspan=3, sticky="w")

        ttk.Label(tab, text="A link will not start unless throttle reads 0 %.",
                  foreground=self.pal["muted"]).pack(anchor="w", padx=12, pady=8)
        self.on_throttle_changed()

    # -------------------------------------------------------------- inputs
    MAX_AXES = 10
    MAX_BUTTONS = 20

    def _build_inputs_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Inputs")

        head = ttk.Frame(tab)
        head.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(head, text="Live values straight from the gamepad — use this to "
                             "find the axis and button numbers for the mapping.",
                  foreground=self.pal["muted"]).pack(side="left")
        ttk.Label(head, text="showing").pack(side="right", padx=(0, 4))
        self.input_slot = tk.StringVar(value="0")
        self.input_slot_combo = ttk.Combobox(head, textvariable=self.input_slot,
                                             width=10, state="readonly",
                                             values=("0",))
        self.input_slot_combo.pack(side="right")
        self.input_slot_combo.bind("<<ComboboxSelected>>",
                                   self.refresh_deadzone_boxes)

        self.axis_frame = ttk.LabelFrame(tab, text="Axes")
        self.axis_frame.pack(fill="x", padx=10, pady=4)

        grid = ttk.Frame(self.axis_frame)
        grid.pack(fill="x", padx=8, pady=6)
        grid.columnconfigure(1, weight=1)
        for col, title in enumerate(("", "live", "raw", "deadzone", "sent")):
            ttk.Label(grid, text=title, foreground=self.pal["muted"]).grid(
                row=0, column=col, sticky="w", padx=(0, 8))
        ttk.Separator(grid, orient="horizontal").grid(
            row=1, column=0, columnspan=5, sticky="ew", pady=(0, 4))

        self.axis_widgets = []
        for i in range(self.MAX_AXES):
            row = i + 2
            cells = []
            cells.append(ttk.Label(grid, text=f"axis {i}", width=8))
            bar = ttk.Progressbar(grid, maximum=2000)
            cells.append(bar)
            val = ttk.Label(grid, text="—", width=8, anchor="e")
            cells.append(val)

            dz = tk.StringVar(value=f"{self.mixer.deadzone_for(0, i):.2f}")
            spin = ttk.Spinbox(grid, from_=0.0, to=0.5, increment=0.01, width=6,
                               format="%.2f", textvariable=dz,
                               command=lambda n=i: self.on_deadzone_changed(n))
            spin.bind("<Return>", lambda _e, n=i: self.on_deadzone_changed(n))
            spin.bind("<FocusOut>", lambda _e, n=i: self.on_deadzone_changed(n))
            cells.append(spin)

            out = ttk.Label(grid, text="—", width=8, anchor="e")
            cells.append(out)

            for col, widget in enumerate(cells):
                widget.grid(row=row, column=col, sticky="ew" if col == 1 else "w",
                            padx=(0, 8), pady=1)
                widget.grid_remove()
            self.axis_widgets.append({"cells": cells, "bar": bar, "val": val,
                                      "out": out, "dz": dz, "spin": spin})

        foot = ttk.Frame(self.axis_frame)
        foot.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(foot, foreground=self.pal["muted"], wraplength=820,
                  justify="left",
                  text="Deadzone ignores small movement around centre, for a stick "
                       "that will not sit still. What is left is rescaled so full "
                       "deflection still reaches the end of the channel. It applies "
                       "to axis sources; the throttle engine has its own on the "
                       "Throttle tab.").pack(anchor="w")
        apply_row = ttk.Frame(foot)
        apply_row.pack(anchor="w", pady=(6, 0))
        ttk.Label(apply_row, text="Set every axis to").pack(side="left")
        self.dz_all = tk.StringVar(value=f"{self.cfg.get('deadzone', 0.05):.2f}")
        ttk.Spinbox(apply_row, from_=0.0, to=0.5, increment=0.01, width=6,
                    format="%.2f", textvariable=self.dz_all).pack(side="left", padx=6)
        ttk.Button(apply_row, text="Apply to all",
                   command=self.apply_deadzone_to_all).pack(side="left")

        self.btn_frame = ttk.LabelFrame(tab, text="Buttons")
        self.btn_frame.pack(fill="x", padx=10, pady=8)
        self.btn_widgets = []
        bgrid = ttk.Frame(self.btn_frame)
        bgrid.pack(padx=6, pady=6)
        for i in range(self.MAX_BUTTONS):
            lbl = tk.Label(bgrid, text=str(i), width=3, relief="ridge",
                           bg=self.pal["off"], fg=self.pal["text"], padx=2, pady=2)
            lbl.grid(row=i // 10, column=i % 10, padx=2, pady=2)
            lbl.grid_remove()
            self.btn_widgets.append(lbl)

        self.hat_lbl = ttk.Label(tab, text="hats: —")
        self.hat_lbl.pack(anchor="w", padx=12, pady=4)

        # Traced rather than bound to the combobox event: the shown slot
        # is also changed in code, by _refresh_input_slots when a device
        # disappears, and a programmatic set fires no event. Without this
        # the boxes keep showing one device while edits land on another.
        self.input_slot.trace_add("write",
                                  lambda *_a: self.refresh_deadzone_boxes())

    def _watch_devices(self):
        """Redraw the device pickers when something is plugged or unplugged.

        The input thread bumps a counter when the list changes, so this is a
        cheap integer compare on the GUI tick rather than a rescan.
        """
        seq = getattr(self.gamepad, "devices_seq", 0)
        if seq == self._devices_seq:
            return
        self._devices_seq = seq
        self._fill_gamepads(announce=True)

    def _shown_slot(self):
        """The device slot the Inputs tab is currently displaying."""
        try:
            return int(self.input_slot.get())
        except (tk.TclError, ValueError, AttributeError):
            return 0

    def _set_axis_deadzone(self, slot, axis, value):
        """One writer for both stores, so they cannot drift apart."""
        value = round(max(0.0, min(0.5, value)), 3)
        self.mixer.axis_deadzone[(slot, axis)] = value
        self.cfg.setdefault("axis_deadzone", {})[f"{slot}:{axis}"] = value
        return value

    def on_deadzone_changed(self, n):
        """Per-axis, per-device deadzone. Sticks wear unevenly, so one value
        for the whole pad means over-deadening the good axes to tame the worst
        one - and two devices must not share a number either."""
        slot = self._shown_slot()
        w = self.axis_widgets[n]
        try:
            value = float(w["dz"].get())
        except (tk.TclError, ValueError):
            w["dz"].set(f"{self.mixer.deadzone_for(slot, n):.2f}")
            return
        w["dz"].set(f"{self._set_axis_deadzone(slot, n, value):.2f}")

    def refresh_deadzone_boxes(self, _evt=None):
        """Repoint the boxes when the shown device changes."""
        if not getattr(self, "axis_widgets", None):
            return              # the traced variable can fire before the tab exists
        slot = self._shown_slot()
        for n, w in enumerate(self.axis_widgets):
            w["dz"].set(f"{self.mixer.deadzone_for(slot, n):.2f}")

    def apply_deadzone_to_all(self):
        try:
            value = max(0.0, min(0.5, float(self.dz_all.get())))
        except (tk.TclError, ValueError):
            return
        slot = self._shown_slot()
        for n, w in enumerate(self.axis_widgets):
            w["dz"].set(f"{self._set_axis_deadzone(slot, n, value):.2f}")
        self.dz_all.set(f"{value:.2f}")
        self.log("info", f"Deadzone set to {value:.2f} on every axis of "
                         f"device {slot}.")

    # ----------------------------------------------------------- telemetry
    def _build_telemetry_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Telemetry")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=10, pady=10)
        self.telem_text = tk.Text(wrap, height=24, wrap="none",
                                  font=("TkFixedFont", 10),
                                  background=self.pal["field"],
                                  foreground=self.pal["text"],
                                  insertbackground=self.pal["text"],
                                  selectbackground=self.pal["select_bg"],
                                  highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(wrap, command=self.telem_text.yview)
        self.telem_text.configure(yscrollcommand=bar.set, state="disabled")
        bar.pack(side="right", fill="y")
        self.telem_text.pack(side="left", fill="both", expand=True)

    # ----------------------------------------------------------------- log
    def _build_log_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Log")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tk.Text(wrap, height=20, wrap="word",
                                font=("TkFixedFont", 10),
                                background=self.pal["field"],
                                foreground=self.pal["text"],
                                insertbackground=self.pal["text"],
                                selectbackground=self.pal["select_bg"],
                                highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(wrap, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("error", foreground=self.pal["danger"])
        self.log_text.tag_configure("warn", foreground=self.pal["warn"])
        self.log_text.tag_configure("info", foreground=self.pal["text"])

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    # ============================================================= actions
    def refresh_ports(self):
        self._ports = linkmod.list_serial_ports()
        labels = [f"{dev}   {desc}".strip() for dev, desc in self._ports]
        self.port_combo["values"] = labels
        wanted = self.cfg.get("port", "")
        for i, (dev, _d) in enumerate(self._ports):
            if dev == wanted:
                self.port_combo.current(i)
                break
        else:
            if labels and not self.port_var.get():
                self.port_combo.current(0)
        if not labels:
            self.port_var.set("")
            self.log("warn", "No serial ports found.")

    def selected_port(self):
        label = self.port_var.get()
        for dev, desc in self._ports:
            if label.startswith(dev):
                return dev
        return None

    def refresh_gamepads(self, tries=6):
        """SDL can take over a second to enumerate, longer with several
        devices attached, so keep looking rather than reporting none."""
        self.gamepad.rescan()
        self.after(300, lambda: self._fill_gamepads(tries))

    NO_DEVICE = "none"

    def _slot_widgets(self, slot):
        return ((self.pad_combo, self.pad_var) if slot == 0
                else (self.pad_combo_b, self.pad_var_b))

    def _wanted_devices(self):
        """Saved slots, as identity dicts.

        Older configs stored a bare index. Indexes move when devices come and
        go, so they are only a starting point - once a slot is filled the
        name and GUID are saved instead.
        """
        wanted = list(self.cfg.get("gamepads") or [0, None])
        while len(wanted) < 2:
            wanted.append(None)
        out = []
        for entry in wanted[:2]:
            if entry is None:
                out.append(None)
            elif isinstance(entry, dict):
                out.append(dict(entry))
            else:
                out.append({"index": int(entry)})
        return out

    def _fill_gamepads(self, tries=1, announce=False):
        devices = self.gamepad.device_list
        if not devices and tries > 1:
            self.after(400, lambda: self._fill_gamepads(tries - 1))
            return
        self._devices_seq = getattr(self.gamepad, "devices_seq", 0)
        listing = [self.NO_DEVICE] + [f"{d['index']}: {d['name']}" for d in devices]
        wanted = self._wanted_devices()

        taken = []
        for slot in (0, 1):
            combo, var = self._slot_widgets(slot)
            combo["values"] = listing
            index = self.gamepad.resolve(wanted[slot], taken)
            if index is not None:
                taken.append(index)
            self.gamepad.select_identity(wanted[slot], slot)
            found = next((d for d in devices if d["index"] == index), None)

            if wanted[slot] and index is None:
                name = wanted[slot].get("name", "the saved device")
                if announce:
                    self.log("warn", f"Slot {slot}: {name} is no longer "
                                     f"connected.")
                var.set(self.NO_DEVICE)
                # Keep the saved identity so it is picked up again on replug.
                continue

            if index is None:
                var.set(self.NO_DEVICE)
                continue

            var.set(listing[index + 1])
            entry = {"name": (found or {}).get("name", ""),
                     "guid": (found or {}).get("guid", ""),
                     "index": index}
            wanted[slot] = entry
            if announce:
                self.log("info", f"Slot {slot}: {devices[index]['name']}")

        self.cfg["gamepads"] = wanted
        self._refresh_input_slots()

        if not devices:
            self.log("warn", "No gamepad detected. Check the F710 dongle and the "
                             "X/D switch (use X).")

    def on_pad_selected(self, slot=0):
        _combo, var = self._slot_widgets(slot)
        label = var.get()
        wanted = self._wanted_devices()
        if not label or label == self.NO_DEVICE:
            wanted[slot] = None
            self.gamepad.select(None, slot)
        else:
            index = int(label.split(":", 1)[0])
            self.gamepad.select(index, slot)
            entry = self.gamepad.identity(slot) or {}
            entry["index"] = index
            wanted[slot] = entry
        self.cfg["gamepads"] = wanted
        self._refresh_input_slots()

    NO_INDEX = "none"

    def _reset_label(self, channel):
        return self.NO_INDEX if not channel else f"CH{channel}"

    def _reset_value(self, text):
        text = str(text).strip().lower()
        if text in ("", self.NO_INDEX) or not text.startswith("ch"):
            return 0
        return max(0, min(crsf.NUM_CHANNELS, int(text[2:])))

    def _sync_row_widgets(self, n):
        """Show none and lock the boxes a source does not use, rather than
        leaving a number sitting there implying it does something.

        Index applies to sources that read a numbered input; steps to the
        two that have several positions - cycle, which advances on each
        press, and switch, which reads one button per position."""
        w = self.ch_widgets[n]
        ch = self.mixer.channels[n]

        if ch.src in gp.INDEXED_SOURCES:
            w["spin"].config(state="normal")
            w["idx"].set(str(ch.idx))
        else:
            w["idx"].set(self.NO_INDEX)
            w["spin"].config(state="disabled")

        if ch.src in ("cycle", "switch"):
            w["steps_spin"].config(state="normal")
            w["steps"].set(str(ch.steps))
        else:
            w["steps"].set(self.NO_INDEX)
            w["steps_spin"].config(state="disabled")

        # Only a latch has anything to reset. A switch reads its lever every
        # frame, so there is no stored state to clear.
        if ch.src in ("toggle", "oneway", "cycle"):
            w["reset_combo"].config(state="readonly")
            w["reset_ch"].set(self._reset_label(ch.reset_ch))
            w["reset_spin"].config(
                state="normal" if ch.reset_ch else "disabled")
            w["reset_move"].set(str(ch.reset_move))
        else:
            w["reset_ch"].set(self.NO_INDEX)
            w["reset_move"].set(self.NO_INDEX)
            w["reset_combo"].config(state="disabled")
            w["reset_spin"].config(state="disabled")

    def on_channel_changed(self, n):
        """Rebuild a channel from the widgets and swap it in as one object.

        Everything is parsed before anything is applied. Assigning field by
        field meant a bad index left the channel carrying its new source with
        the old index and device - live, because the link thread reads these
        objects at up to 500 Hz - and skipped the config write, so the saved
        map and the running one silently disagreed. Replacing the list entry
        is a single assignment: the link thread sees the old channel or the
        new one, never a half-built mixture.
        """
        w = self.ch_widgets[n]
        old = self.mixer.channels[n]
        try:
            raw_idx = str(w["idx"].get()).strip().lower()
            raw_steps = str(w["steps"].get()).strip().lower()
            raw_move = str(w["reset_move"].get()).strip().lower()
            # The boxes read "none" for whatever the source does not use;
            # that is the widget being blanked, not a request for zero.
            new = gp.ChannelMap(
                src=w["src"].get(),
                idx=old.idx if raw_idx in ("", self.NO_INDEX)
                    else max(0, int(raw_idx)),
                inv=bool(w["inv"].get()),
                dev=int(w["dev"].get()),
                value=old.value,
                steps=old.steps if raw_steps in ("", self.NO_INDEX)
                      else max(2, min(6, int(raw_steps))),
                # Owned by the Outputs tab; this one must not reset them.
                out_min=old.out_min, out_max=old.out_max,
                reset_ch=self._reset_value(w["reset_ch"].get()),
                # "none" here is the box being blanked for a source that has
                # no latch, exactly as for index and steps - not a value.
                reset_move=old.reset_move if raw_move in ("", self.NO_INDEX)
                           else max(10, min(500, int(raw_move))),
                buttons=old.buttons)
        except (tk.TclError, ValueError):
            return              # nothing applied; the channel is as it was

        self.mixer.forget_latches(old)
        self.mixer.channels[n] = new
        self._sync_row_widgets(n)
        self.cfg["channels"][n] = new.to_dict()
        self.src_help.config(text=f"{new.src}: {gp.SOURCE_HELP.get(new.src, '')}")

    def on_throttle_changed(self, _evt=None):
        t = self.cfg["throttle"]
        try:
            t["mode"] = self.thr_mode.get()
            t["axis"] = int(self.thr_axis.get())
            t["axis_down"] = int(self.thr_axis_dn.get())
            t["cut_button"] = int(self.thr_cut.get())
            t["ramp_rate"] = round(float(self.thr_rate.get()), 2)
            t["dev"] = int(self.thr_dev.get())
            self.mixer.throttle_dev = t["dev"]
            t["deadzone"] = round(float(self.thr_dz.get()), 3)
        except (tk.TclError, ValueError):
            return
        self.mixer.deadzone = self.cfg["deadzone"]
        self.thr_help.config(text=gp.THROTTLE_MODE_HELP.get(t["mode"], ""))
        self.thr_rate_lbl.config(text=f"{t['ramp_rate']:.2f} (idle\u2192full in "
                                      f"{1.0 / max(t['ramp_rate'], 0.01):.1f}s)")
        self.thr_dz_lbl.config(text=f"{t['deadzone']:.2f}")

    # ---------------------------------------------------------------- link
    def start_link(self):
        if self.link and self.link.running:
            return

        port = self.selected_port()
        if not port:
            messagebox.showerror("No serial port", "Select the serial port your "
                                                   "ExpressLRS module is on.")
            return

        # Every device the map reads has to be reporting before we start,
        # not just the first one.
        states = self.gamepad.states
        for slot in sorted(self.mixer.required_devices()):
            st = states.get(slot)
            if st is None or not st.is_fresh():
                where = "gamepad" if slot == 0 else f"device {slot}"
                messagebox.showerror(
                    "No input", f"No live data from {where}. Select it in the "
                                f"toolbar and move a control to confirm it is "
                                f"reporting.")
                return

        # An unmapped channel sends centre, which is ~1500us - mid-throttle
        # on a throttle channel. With nothing mapped the throttle check below
        # has nothing to test, so say so plainly instead of passing silently.
        if all(c.src == "none" for c in self.mixer.channels):
            if not messagebox.askokcancel(
                    "Nothing is mapped",
                    "No channel has a source, so every channel will transmit "
                    "centre - about 1500us, which is mid-throttle on a "
                    "throttle channel. Map your channels first, or start "
                    "anyway to test the link itself?"):
                return

        # Nothing here forces a control to a "safe" value. Clearing the
        # latches would put arm low and the throttle at idle in the very
        # first frame, and when the link is being restarted to recover a
        # model that is still in the air, that frame is a disarm command.
        # The controls are read exactly as they stand, and anything that
        # would surprise the pilot is put to them instead.
        if not self._confirm_first_frame():
            return

        try:
            baud = int(self.baud_var.get())
            rate = int(self.rate_var.get())
        except ValueError:
            messagebox.showerror("Bad settings", "Baud and rate must be numbers.")
            return

        cap = crsf.MAX_RATE_FOR_BAUD.get(baud)
        if cap and rate > cap:
            self.log("warn", f"ExpressLRS caps the packet rate at {cap} Hz on "
                             f"{baud} baud; sending {rate} Hz anyway.")

        self.cfg["port"], self.cfg["baud"], self.cfg["rate_hz"] = port, baud, rate

        self.link = linkmod.CrsfLink(port, baud, rate, self.mixer, self.gamepad,
                                     sync_byte=self.cfg.get("sync_byte", 0xC8),
                                     on_event=self._link_event)
        self.link.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.after(1500, self.read_module_settings)

    def on_theme_changed(self):
        """Tk cannot repaint existing widgets from a style change alone, so
        the new theme is saved and applied on the next start."""
        name = self.theme_var.get()
        if name == self.cfg.get("theme"):
            return
        self.cfg["theme"] = name
        configmod.save(self.cfg)
        self.log("info", f"Theme set to {name}; it applies next time MavJOY starts.")
        messagebox.showinfo(
            "Theme",
            f"Saved the {name} theme. Restart MavJOY to see it.")

    def _on_rate_auto(self):
        """Auto mode drives the spinbox, so stop it being edited by hand."""
        self.rate_spin.config(state="disabled" if self.rate_auto.get() else "normal")
        if not self.rate_auto.get():
            self.rate_hint.config(text="(PC→module, not the RF rate)")

    def _apply_auto_rate(self):
        """Follow the frame interval the module broadcasts in its sync frames."""
        if not (self.link and self.link.running):
            return
        requested = self.link.requested_rate()
        if requested is None:
            if self.rate_auto.get():
                self.rate_hint.config(text="(waiting for the module to say)")
            return

        want = crsf.recommended_crsf_rate(requested, self.link.baud)
        self.rate_hint.config(text=f"(module asks {requested:.0f} Hz)")
        if not self.rate_auto.get():
            return

        if self.link.set_rate(want):
            self.rate_var.set(want)
            self.cfg["rate_hz"] = want
            self.log("info", f"Module asks for {requested:.0f} Hz; sending "
                             f"{want} Hz of CRSF.")
        elif self.rate_var.get() != want:
            self.rate_var.set(want)

        # Oversampling is the whole point; say so if the link cannot manage it.
        if want < requested * 2 and self._rate_warned != (requested, want):
            self._rate_warned = (requested, want)
            self.log("warn", f"{self.link.baud} baud only allows {want} Hz, which "
                             f"is under twice the {requested:.0f} Hz the module "
                             f"wants. Raise the baud to 921600 for more headroom.")

    # ---------------------------------------------------- module settings
    # Commands that take the module off the air. Bind re-pairs it; WiFi and
    # BLE swap the radio out from under CRSF entirely, so the link stops.
    DISRUPTIVE = ("wifi", "ble", "bind")

    def read_module_settings(self):
        """Read the whole settings tree, the way the Lua script does."""
        if not (self.link and self.link.running):
            messagebox.showinfo(
                "Link not running",
                "Start the link first. Module settings travel over the same "
                "serial connection as the channel data.")
            return
        self._fields = {}
        self.module_btn.config(state="disabled")
        self.module_info_lbl.config(text="asking the module to identify itself...")
        self.link.submit("ping", on_done=self._module_cb("ping"))

    def _module_cb(self, tag):
        """Callbacks fire on the link thread; hand them to the GUI thread."""
        return lambda result, error: self._module_q.put((tag, result, error))

    def _request_field(self, index):
        total = (self._device or {}).get("field_count", 0)
        self.module_info_lbl.config(text=f"reading settings {index}/{total} ...")
        self.link.submit("read", index=index, on_done=self._module_cb("field"))

    def _finish_scan(self):
        dev = self._device or {}
        name = dev.get("name", "module")
        version = dev.get("version", "?")
        self.module_info_lbl.config(
            text=f"{name}  -  ExpressLRS {version}  -  {len(self._fields)} settings")
        self.module_btn.config(state="normal")
        self._render_fields()

    def _poll_module(self):
        while True:
            try:
                tag, result, error = self._module_q.get_nowait()
            except queue.Empty:
                return
            if self.link is None or not self.link.running:
                self.module_btn.config(state="normal")
                continue
            if error or result is None:
                self.module_info_lbl.config(
                    text=f"module did not answer ({error or 'no data'})")
                self.module_btn.config(state="normal")
                self.log("warn", f"Module settings: {error or 'no data'}")
                continue

            if tag == "ping":
                self._device = result
                self._request_field(1)
            elif tag == "field":
                self._fields[result.index] = result
                nxt = result.index + 1
                if nxt <= (self._device or {}).get("field_count", 0):
                    self._request_field(nxt)
                else:
                    self._finish_scan()
            elif tag == "written":
                self._after_write(result)
            elif tag == "cmd":
                self._command_reply(result)

    # ------------------------------------------------------------ drawing
    def _render_fields(self):
        if not hasattr(self, "module_body"):
            return
        for child in self.module_body.winfo_children():
            child.destroy()
        self._field_vars = {}
        self._row = 0
        if not self._fields:
            ttk.Label(self.module_body, foreground=self.pal["muted"],
                      text="Nothing read yet.").grid(row=0, column=0,
                                                     sticky="w", padx=12, pady=6)
            return
        for idx in sorted(self._fields):
            if self._fields[idx].parent == 0:
                self._render_field(idx, 0)

    def _render_field(self, index, depth):
        field = self._fields.get(index)
        if field is None:
            return
        if field.hidden and not self.module_hidden.get():
            return

        body = self.module_body
        row = self._row
        self._row += 1
        indent = 12 + depth * 18

        label = ttk.Label(body, text=field.name, width=22, anchor="w")
        label.grid(row=row, column=0, sticky="w", padx=(indent, 8), pady=2)

        if field.type == crsf.PARAM_FOLDER:
            label.config(font=("TkDefaultFont", 9, "bold"))
            for child in field.children:
                self._render_field(child, depth + 1)
            return

        if field.type == crsf.PARAM_SELECT:
            var = tk.StringVar(value=field.current_label)
            combo = ttk.Combobox(body, textvariable=var, state="readonly",
                                 width=24,
                                 values=[lab for _v, lab in field.choices()])
            combo.grid(row=row, column=1, sticky="w")
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, i=index: self._on_select_changed(i))
            self._field_vars[index] = var

        elif field.type in crsf._NUMERIC and field.vmin is not None:
            var = tk.StringVar(value=str(field.value))
            spin = ttk.Spinbox(body, from_=field.vmin, to=field.vmax, width=10,
                               textvariable=var)
            spin.grid(row=row, column=1, sticky="w")
            spin.configure(command=lambda i=index: self._on_number_changed(i))
            spin.bind("<Return>", lambda _e, i=index: self._on_number_changed(i))
            spin.bind("<FocusOut>", lambda _e, i=index: self._on_number_changed(i))
            self._field_vars[index] = var

        elif field.type == crsf.PARAM_COMMAND:
            ttk.Button(body, text="Run", width=10,
                       command=lambda i=index: self.run_command(i)).grid(
                row=row, column=1, sticky="w")

        else:
            ttk.Label(body, text=field.display, width=26, anchor="w").grid(
                row=row, column=1, sticky="w")

        if field.unit and field.type != crsf.PARAM_SELECT:
            ttk.Label(body, text=field.unit, foreground=self.pal["muted"]).grid(
                row=row, column=2, sticky="w", padx=6)

        # ExpressLRS blanks out options the current setup cannot reach rather
        # than reporting an error. The commonest cause by far is the CRSF baud
        # - at 115200 it will not offer 333Hz, 500Hz or the F/D rates at all -
        # so say that instead of quietly showing a short list.
        elif field.type == crsf.PARAM_SELECT:
            missing = sum(1 for lab in field.options if not lab.strip())
            if missing:
                baud = self.link.baud if self.link else 0
                hint = f"{missing} more hidden at this CRSF baud"
                if baud and baud < 921600:
                    hint += " - try Baud 921600"
                ttk.Label(body, text=hint, foreground=self.pal["warn"]).grid(
                    row=row, column=2, sticky="w", padx=6)

    # ------------------------------------------------------------ editing
    def _armed_reason(self):
        """Why a write is barred, in words, or None if it is not.

        Two independent answers, and either one bars it. CH5 is what we are
        commanding and works with no telemetry at all; the model's own
        report is the truth but is only there when telemetry is flowing and
        the sender marks a disarm. Neither supersedes the other.
        """
        if self.mixer.armed_channels():
            return f"CH{gp.Mixer.ARM_CHANNEL} is high, so the model is armed"
        if self._model_armed():
            return "the model reports that it is armed"
        return None

    def _may_write(self, what):
        """Nothing reaches the module while anything says the model is armed."""
        reason = self._armed_reason()
        if reason:
            messagebox.showwarning(
                "Armed",
                f"Cannot change {what}: {reason}. Disarm first - module "
                f"settings can interrupt the RF link.")
            return False
        if not (self.link and self.link.running):
            return False
        # Ask whenever the link is up, not only while frames are flowing. A
        # write reaches the module either way, and the window where they have
        # stopped is the dangerous one: the model is already in failsafe and
        # re-keying the link is the last thing it needs.
        state = ("while the link is live"
                 if self.link.transmitting else
                 "while the input is down and the model is in failsafe")
        return messagebox.askokcancel(
            "Change a module setting?",
            f"Change {what} {state}? Some settings re-key the RF link, so "
            f"the receiver drops out and goes to failsafe for a moment. "
            f"Props off.")

    def _write_field(self, index, value, description):
        self._pending_write = (index, value)
        field = self._fields.get(index)
        # Numeric fields are not all one byte wide; sending one byte for a
        # uint16 leaves the module reading our CRC as half the value.
        width = crsf.param_value_width(field.type) if field else 1
        self.module_info_lbl.config(text=f"writing {description} ...")
        self.log("info", f"Module: setting {description}")
        self.link.submit("write", index=index, value=value, width=width,
                         on_done=self._module_cb("written"))

    def _after_write(self, field):
        """ExpressLRS can change other fields in response, so reload them all."""
        self._fields[field.index] = field
        pending = self._pending_write
        self._pending_write = None

        refused = (pending is not None and pending[0] == field.index
                   and field.value != pending[1])
        if refused:
            # The reason arrives in the next status frame, which the link asks
            # for as soon as a settings job finishes. Give it a moment.
            self.after(600, lambda f=field, want=pending[1]: self._report_refusal(f, want))
        else:
            self.log("info", f"Module: {field.name} is now {field.display}")
        if self._device:
            self._request_field(1)

    def _report_refusal(self, field, wanted_value):
        """Say why a setting did not take. ExpressLRS reports a reason, but
        only if asked - otherwise the value just quietly reverts."""
        wanted = (field.label_for(wanted_value)
                  if field.type == crsf.PARAM_SELECT else str(wanted_value))
        status = self.link.status() if self.link else None
        reason = (status or {}).get("info", "")
        if status and status.get("warning") and self.link:
            self.link.clear_warning()   # latched until acknowledged

        message = (f"The module would not take {field.name} = {wanted} and is "
                   f"still on {field.display}.")
        if reason:
            message += f" It says: {reason}."
        else:
            message += (" ExpressLRS blocks some settings depending on the "
                        "packet rate, the telemetry ratio and whether a "
                        "receiver is connected.")
        self.log("warn", message)
        messagebox.showwarning("Setting refused", message)

    def _on_select_changed(self, index):
        field = self._fields.get(index)
        var = self._field_vars.get(index)
        if field is None or var is None:
            return
        value = next((v for v, lab in field.choices() if lab == var.get()), None)
        if value is None or value == field.value:
            return
        if not self._may_write(field.name):
            var.set(field.current_label)
            return
        self._write_field(index, value, f"{field.name} = {var.get()}")

    def _on_number_changed(self, index):
        field = self._fields.get(index)
        var = self._field_vars.get(index)
        if field is None or var is None:
            return
        try:
            value = int(float(var.get()))
        except (TypeError, ValueError):
            var.set(str(field.value))
            return
        if value == field.value:
            return
        if field.vmin is None or field.vmax is None:
            # A truncated entry parses a value but no limits. Refusing is the
            # only safe answer: without limits there is nothing to check the
            # number against, and this writes straight to the module.
            messagebox.showwarning(
                "Incomplete setting",
                f"{field.name} did not read back completely, so its limits "
                f"are unknown and it cannot be changed. Read the settings "
                f"again.")
            var.set(str(field.value))
            return
        if not (field.vmin <= value <= field.vmax):
            messagebox.showwarning(
                "Out of range",
                f"{field.name} accepts {field.vmin} to {field.vmax}.")
            var.set(str(field.value))
            return
        if not self._may_write(field.name):
            var.set(str(field.value))
            return
        self._write_field(index, value, f"{field.name} = {value}")

    # ----------------------------------------------------------- commands
    def run_command(self, index):
        field = self._fields.get(index)
        if field is None or not self._may_write(field.name):
            return
        lowered = field.name.lower()
        warning = ""
        if any(word in lowered for word in self.DISRUPTIVE):
            warning = (" This takes the module off the air, so the CRSF link "
                       "stops and you will have to start it again.")
        if not messagebox.askokcancel(
                field.name, f"Run {field.name}?{warning} Props off."):
            return
        self._cmd_index = index
        self.module_info_lbl.config(text=f"running {field.name} ...")
        self.log("info", f"Module: running {field.name}")
        self.link.submit("write", index=index, value=crsf.CMD_START,
                         on_done=self._module_cb("cmd"))

    def _command_reply(self, field):
        """Step through the state machine the module drives for commands."""
        self._fields[field.index] = field

        if field.status == crsf.CMD_PROGRESS:
            self.module_info_lbl.config(
                text=f"{field.name}: {field.info or 'working'}")
            self.after(200, lambda i=field.index: self._poll_command(i))
            return

        if field.status == crsf.CMD_CONFIRMATION_NEEDED:
            # Handed to a fresh callback rather than opened here. This runs
            # inside the periodic tick, which only reschedules itself after
            # it returns, so a modal raised on this stack freezes every live
            # indicator - the ARM chip and the LQ chip included - for as long
            # as the box is up.
            self.after(0, lambda f=field: self._confirm_command(f))
            return

        self._cmd_index = None
        outcome = field.info or "done"
        self.module_info_lbl.config(text=f"{field.name}: {outcome}")
        self.log("info", f"Module: {field.name} finished ({outcome})")

    def _first_frame_concerns(self):
        """Re-read the controls and describe what the first frame would carry."""
        states = self.gamepad.states
        self.mixer.resync(states)
        values = self.mixer.compute(states)
        concerns = []
        for i, ch in enumerate(self.mixer.channels):
            if ch.src == "throttle" and values[i] > crsf.CHANNEL_MIN + 20:
                concerns.append(f"CH{i + 1} throttle at "
                                f"{crsf.crsf_to_us(values[i]):.0f} us")
        for n in self.mixer.armed_channels():
            concerns.append(f"CH{n} armed")
        if self._model_armed():
            concerns.append("the model reports it is armed")
        return concerns

    def _confirm_first_frame(self):
        """The one gate before a link goes live.

        Evaluated, asked, and then evaluated AGAIN, because the dialog blocks
        and a throttle can be pushed while it is open - so the sentence the
        pilot agreed to has to still be true. The default button is Cancel:
        this is the only thing standing between a held throttle and spinning
        props, and it should not be dismissable with a reflexive Return.
        """
        concerns = self._first_frame_concerns()
        if not concerns:
            return True
        if not messagebox.askokcancel(
                "Check before starting",
                f"The first frame will carry: {', '.join(concerns)}. "
                f"On the bench, set those controls off and start again. "
                f"If you are reconnecting to a model that is already "
                f"flying, this is exactly what keeps it flying - starting "
                f"with them off would command a disarm. Start now?",
                default=messagebox.CANCEL, icon=messagebox.WARNING):
            return False
        after = self._first_frame_concerns()
        if after != concerns:
            messagebox.showwarning(
                "Controls moved",
                f"The controls changed while that was open - now "
                f"{', '.join(after) if after else 'nothing is flagged'}. "
                f"Nothing was started; press Start again.")
            return False
        return True

    def _confirm_command(self, field):
        """Ask about a command the module is waiting on, off the tick stack."""
        if self.link is None or not self.link.running:
            self._cmd_index = None
            return
        # The link keeps running while the dialog is up, so the model can be
        # armed between Run and OK. Check before, and again after.
        if self._cancel_if_armed(field, "confirm"):
            return
        ok = messagebox.askokcancel(
            field.name, field.info or f"Confirm {field.name}?")
        if ok and self._cancel_if_armed(field, "confirm"):
            return
        if self.link is None or not self.link.running:
            self._cmd_index = None
            return
        reply = crsf.CMD_CONFIRM if ok else crsf.CMD_CANCEL
        self.link.submit("write", index=field.index, value=reply,
                         on_done=self._module_cb("cmd"))

    def _cancel_if_armed(self, field, stage):
        """Abandon a command in progress if the model became armed."""
        reason = self._armed_reason()
        if not reason:
            return False
        self._cmd_index = None
        if self.link and self.link.running:
            self.link.submit("write", index=field.index,
                             value=crsf.CMD_CANCEL)
        message = (f"Armed while {field.name} was waiting to {stage}, so it "
                   f"was cancelled: {reason}.")
        self.module_info_lbl.config(text=f"{field.name}: cancelled, armed")
        self.log("warn", message)
        messagebox.showwarning("Cancelled", message)
        return True

    def _poll_command(self, index):
        if self.link is None or not self.link.running or self._cmd_index != index:
            return
        field = self._fields.get(index)
        if field is not None and self._cancel_if_armed(field, "finish"):
            return
        self.link.submit("write", index=index, value=crsf.CMD_POLL,
                         on_done=self._module_cb("cmd"))

    def stop_link(self, reason=""):
        if self.link:
            self.link.stop()
            self.link.join(timeout=1.0)
            self.link = None
            if reason:
                self.log("info", f"Link stopped ({reason}). Receiver goes to failsafe.")
        self.start_btn.config(state="normal")
        self._device = None
        self._cmd_index = None
        self._fields = {}
        self._render_fields()
        self.module_btn.config(state="normal")
        self.module_info_lbl.config(
            text="Start the link, then read the settings from the module.")
        self.stop_btn.config(state="disabled")

    def _link_event(self, level, message):
        """Called from the link thread. tkinter is not thread safe, so this
        only drops the message into a queue; the GUI tick drains it."""
        try:
            self._events.put_nowait((level, message))
        except queue.Full:
            pass

    # ================================================================ loop
    def _drain_events(self):
        for _ in range(20):
            try:
                level, message = self._events.get_nowait()
            except queue.Empty:
                return
            self.log(level, message)

    def _tick(self):
        try:
            self._poll_module()
            self._drain_events()
            self._update()
        except Exception as exc:  # never let a display bug kill the GUI loop
            self.status_var.set(f"display error: {exc}")
        self.after(REFRESH_MS, self._tick)

    def _update(self):
        self._watch_devices()
        self._apply_auto_rate()
        # a link thread that died (port vanished, write error) must not leave
        # the UI stuck in "started"
        if self.link is not None and not self.link.is_alive():
            # The link ends itself when input is lost; it has already logged
            # why, so do not paper over that with a second vaguer message.
            self.stop_link()

        states = self.gamepad.states
        state = self.gamepad.state
        link_active = self.link is not None
        running = bool(self.link and self.link.running)
        transmitting = bool(running and self.link.transmitting)

        # ---- channel values. While the link is transmitting that thread
        # owns the mixer and the GUI only reads what it produced. Otherwise
        # nothing else is touching it, so recompute here: that keeps
        # last_values - and therefore the arm interlock - current, instead
        # of frozen at whatever it held the instant the input died.
        if link_active:
            # The link thread owns the mixer and computes on every tick, so
            # last_values is always current - including through an outage,
            # where it holds what was last actually seen. The GUI must not
            # compute here: two threads in compute() race on the latches and
            # can swallow an arm press at the moment transmission resumes.
            values = list(self.mixer.output_values)
        else:
            values = self.mixer.compute(states)

        for i, w in enumerate(self.ch_widgets):
            v = values[i]
            w["bar"]["value"] = max(0, min(1000, (v - crsf.CHANNEL_MIN) * 1000 //
                                           (crsf.CHANNEL_MAX - crsf.CHANNEL_MIN)))
            w["val"].config(text=fmt_channel(v))

        for i, w in enumerate(self.out_widgets):
            v = values[i]
            w["bar"]["value"] = max(0, min(1000, (v - crsf.CHANNEL_MIN) * 1000 //
                                           (crsf.CHANNEL_MAX - crsf.CHANNEL_MIN)))
            w["sent"].config(text=f"{crsf.crsf_to_us(v):.0f} µs")

        # ---- throttle + arm
        thr_pct = self.mixer.throttle.value * 100.0
        self.thr_bar["value"] = thr_pct
        self.thr_lbl.config(text=f"{thr_pct:3.0f} %")

        self._set_arm(self._last_mode)

        # ---- link state
        if running and self.link.transmitting:
            # Held channels are not following their sticks, which without a
            # word of explanation reads as a broken controller.
            held = self.mixer.holding()
            if held:
                self.link_lbl.config(text=f"HOLDING {len(held)} CH",
                                     bg=self.pal["warn"])
            else:
                self.link_lbl.config(text="TRANSMITTING", bg=self.pal["ok"])
        elif running:
            self.link_lbl.config(text="PORT OPEN / NO TX", bg=self.pal["warn"])
        else:
            self.link_lbl.config(text="LINK STOPPED", bg=self.pal["idle"])

        if running:
            telem, stats = self.link.snapshot()
            self.rate_lbl.config(text=f"{stats.actual_rate:5.1f} Hz sent  "
                                      f"(jit {stats.jitter_ms:.1f} ms)")
            link = telem.get("link")
            if link and time.monotonic() - link.get("_t", 0) < 2.0:
                self.rf_lbl.config(
                    text=f"LQ {link['up_lq']}%  RSSI {link['up_rssi_1']} dBm  "
                         f"SNR {link['up_snr']}  {link.get('tx_power_mw') or '?'} mW")
                self._set_lq(link["up_lq"])
            else:
                self.rf_lbl.config(text="no telemetry")
                self._set_lq(None)
            self._last_mode = telem.get("mode")
            self._set_mode(self._last_mode)
            held = self.mixer.holding()
            if held:
                names = ", ".join(f"CH{n}" for n in held[:6])
                more = f" +{len(held) - 6} more" if len(held) > 6 else ""
                self.status_var.set(
                    f"holding {names}{more} at the values last sent - move a "
                    f"control to take it back")
            else:
                self.status_var.set(
                    f"sent {stats.frames_sent}   skipped {stats.frames_skipped}   "
                    f"telemetry frames {stats.telem_frames}   crc errors "
                    f"{stats.crc_errors}   write errors {stats.write_errors}")
            self._update_telemetry(telem, stats)
        else:
            self.rate_lbl.config(text="\u2014 Hz")
            self.rf_lbl.config(text="no telemetry")
            self._set_lq(None)
            self._last_mode = None
            self._set_mode(None)
            src = "simulated pad" if self.simulate else (
                state.device_name if state.connected else "no gamepad")
            self.status_var.set(f"idle   |   input: {src}")

        self._update_inputs(self._shown_input_state(state))

    def _shown_input_state(self, fallback):
        """Whichever device slot the Inputs tab is set to show."""
        try:
            slot = int(self.input_slot.get())
        except (tk.TclError, ValueError):
            return fallback
        if slot == 0:
            return fallback
        return self.gamepad.state_for(slot)

    def _refresh_input_slots(self):
        """Offer only the slots that actually have a device in them."""
        if not hasattr(self, "input_slot_combo"):
            return
        wanted = self._wanted_devices()
        slots = [str(i) for i, entry in enumerate(wanted) if entry is not None] or ["0"]
        self.input_slot_combo["values"] = slots
        if self.input_slot.get() not in slots:
            self.input_slot.set(slots[0])

    def _update_inputs(self, state):
        for i, w in enumerate(self.axis_widgets):
            if i < len(state.axes):
                for cell in w["cells"]:
                    cell.grid()
                v = state.axes[i]
                w["bar"]["value"] = (v + 1.0) * 1000
                w["val"].config(text=f"{v:+.3f}")
                out = gp._apply_deadzone(
                    v, self.mixer.deadzone_for(self._shown_slot(), i))
                w["out"].config(text=f"{out:+.3f}")
            else:
                for cell in w["cells"]:
                    cell.grid_remove()

        for i, lbl in enumerate(self.btn_widgets):
            if i < len(state.buttons):
                lbl.grid()
                lbl.config(bg=self.pal["ok"] if state.buttons[i] else self.pal["off"],
                           fg="white" if state.buttons[i] else "black")
            else:
                lbl.grid_remove()

        hats = state.hats if state.hats else "\u2014"
        self.hat_lbl.config(text=f"hats: {hats}")

    # Uplink LQ is the number that says whether the model is still listening.
    # ExpressLRS itself warns below 70 and treats the link as failing well
    # before zero, so the bands are drawn where they start to matter rather
    # than only at total loss.
    LQ_GOOD = 80
    LQ_MARGINAL = 50

    def _set_lq(self, lq):
        """Paint the link-quality chip, or grey it when nothing is coming back."""
        if lq is None:
            self.lq_lbl.config(text="LQ: —", bg=self.pal["idle"])
            return
        if lq >= self.LQ_GOOD:
            colour = self.pal["ok"]
        elif lq >= self.LQ_MARGINAL:
            colour = self.pal["warn"]
        else:
            colour = self.pal["danger"]
        self.lq_lbl.config(text=f"LQ: {lq}%", bg=colour)

    # A flight mode is acted on, so a stale one is worse than none: the
    # model can change mode by itself - a failsafe is exactly that - and a
    # name left over from before the telemetry stopped would read as current.
    MODE_STALE = 3.0
    MODE_MAX_CHARS = 10

    # The model has to have been seen marking a disarm before the absence
    # of that mark means anything.
    STAR_HINT_AFTER = 10.0

    def _model_armed(self):
        """True only when the model itself says it is armed.

        Never a guess: with no telemetry, or a sender that does not mark
        disarm, this is False and the CH5 interlock is what protects a
        settings write.
        """
        return self._armed_report is True

    def _set_arm(self, data):
        """Paint the arm chip with what the MODEL reports.

        CRSF carries no armed frame. What it carries is a convention: the
        sender appends a star to the flight mode while disarmed, and
        ArduPilot only does that with RC_OPTIONS bit 12 set. So a mode with
        no star is as consistent with a sender that never marks disarm as
        it is with a model in the air, and the two are only told apart by
        having seen a star at some point - which happens on the ground,
        before arming, in the normal course of things.

        Until then this says nothing rather than guessing. An arm light
        that reads DISARMED because it cannot tell would be worse than no
        arm light at all.
        """
        fresh = data and time.monotonic() - data.get("_t", 0) < self.MODE_STALE
        if fresh and not self._mode_since:
            self._mode_since = time.monotonic()
        self._armed_report = self._arm_watch.feed(data if fresh else None)

        if self._armed_report is None:
            self.arm_lbl.config(text="ARM: —", bg=self.pal["idle"])
            self._hint_disarm_star(bool(fresh))
        elif self._armed_report:
            self.arm_lbl.config(text="ARMED", bg=self.pal["danger"])
        else:
            self.arm_lbl.config(text="DISARMED", bg=self.pal["ok"])

    def _hint_disarm_star(self, fresh):
        """Say once why the arm chip is blank, when it is worth saying."""
        if self._said_star_hint or not fresh or self._arm_watch.marker_seen:
            return
        if time.monotonic() - self._mode_since < self.STAR_HINT_AFTER:
            return
        self._said_star_hint = True
        self.log("info", "The model reports its flight mode but never marks a "
                         "disarm, so ARMED cannot be read from it. On "
                         "ArduPilot set RC_OPTIONS bit 12 (add 4096) to have "
                         "it append * to the mode name while disarmed.")

    def _set_mode(self, data):
        """Paint the flight-mode chip with what the model reports."""
        name = (data or {}).get("mode")
        fresh = data and time.monotonic() - data.get("_t", 0) < self.MODE_STALE
        if not name or not fresh:
            self.mode_lbl.config(text="MODE: —", bg=self.pal["idle"])
            return
        self.mode_lbl.config(text=f"MODE: {name[:self.MODE_MAX_CHARS]}",
                             bg=self.pal["ok"])

    def _update_telemetry(self, telem, stats):
        lines = []
        now = time.monotonic()
        for key in ("mode", "link", "battery", "attitude", "baro", "vario",
                    "gps", "unknown"):
            data = telem.get(key)
            if not data:
                continue
            age = now - data.get("_t", now)
            if key == "unknown":
                lines.append(f"[sensors this app cannot decode yet]  "
                             f"({age:.1f}s ago)")
            else:
                lines.append(f"[{key}]  ({age:.1f}s ago)")
            for k, v in data.items():
                if k == "_t":
                    continue
                lines.append(f"    {k:16s} {v}")
            lines.append("")
        if not lines:
            lines = ["No telemetry received yet.", "",
                     "Telemetry only flows once the receiver is connected, and only",
                     "if the telemetry ratio is not set to Off in the ELRS settings."]
        lines += ["", f"frames sent      {stats.frames_sent}",
                  f"frames skipped   {stats.frames_skipped}",
                  f"bytes received   {stats.bytes_rx}",
                  f"telemetry frames {stats.telem_frames}",
                  f"crc errors       {stats.crc_errors}",
                  f"write errors     {stats.write_errors}"]
        # This runs 20 times a second. Rewriting the widget every time threw
        # the view straight back to the top, which made the tab impossible to
        # scroll: redraw only when the text actually changed, and put the
        # view back where the reader left it.
        text = "\n".join(lines)
        first, _last = self.telem_text.yview()
        if text != self._telem_last:
            self._telem_last = text
            self.telem_text.configure(state="normal")
            self.telem_text.delete("1.0", "end")
            self.telem_text.insert("1.0", text)
            self.telem_text.configure(state="disabled")
            self.telem_text.yview_moveto(first)

    # ================================================================ misc
    def log(self, level, message):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {message}\n", level)
        self._log_lines += 1
        if self._log_lines > 500:
            self.log_text.delete("1.0", "100.0")
            self._log_lines -= 99
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def save_config(self):
        for i in range(crsf.NUM_CHANNELS):
            self.on_channel_changed(i)
        self.on_throttle_changed()
        try:
            self.cfg["port"] = self.selected_port() or self.cfg.get("port", "")
            self.cfg["baud"] = int(self.baud_var.get())
            self.cfg["rate_hz"] = int(self.rate_var.get())
            self.cfg["rate_auto"] = bool(self.rate_auto.get())
            configmod.save(self.cfg)
            self.log("info", f"Configuration saved to {configmod.CONFIG_PATH}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def reload_config(self):
        if self.link and self.link.running:
            messagebox.showwarning("Link running", "Stop the link first.")
            return
        self.cfg, warning = configmod.load()
        self._apply_config_to_widgets()
        self.log("info", "Configuration reloaded")
        if warning:
            self.log("warn", warning)

    def reset_config(self):
        if self.link and self.link.running:
            messagebox.showwarning("Link running", "Stop the link first.")
            return
        if not messagebox.askyesno("Reset", "Reset all mapping to the defaults?"):
            return
        self.cfg = configmod.default_config()
        self._apply_config_to_widgets()
        self.log("info", "Configuration reset to defaults")

    def _apply_config_to_widgets(self):
        self.mixer = gp.Mixer(self.cfg)
        self.mixer.reset()
        for i, w in enumerate(self.ch_widgets):
            ch = self.mixer.channels[i]
            w["src"].set(ch.src)
            w["inv"].set(ch.inv)
            w["dev"].set(str(ch.dev))
            self.out_widgets[i]["lo"].set(str(ch.out_min))
            self.out_widgets[i]["hi"].set(str(ch.out_max))
            self._sync_row_widgets(i)
        t = self.cfg["throttle"]
        self.thr_mode.set(t["mode"])
        self.thr_dev.set(str(t.get("dev", 0)))
        self.thr_axis.set(t["axis"])
        self.thr_axis_dn.set(t["axis_down"])
        self.thr_cut.set(t["cut_button"])
        self.thr_rate.set(t["ramp_rate"])
        self.thr_dz.set(t["deadzone"])
        self.baud_var.set(str(self.cfg["baud"]))
        for n, w in enumerate(self.axis_widgets):
            w["dz"].set(f"{self.mixer.deadzone_for(self._shown_slot(), n):.2f}")
        self.dz_all.set(f"{self.cfg.get('deadzone', 0.05):.2f}")
        self.rate_var.set(self.cfg["rate_hz"])
        self.rate_auto.set(bool(self.cfg.get("rate_auto", True)))
        self._on_rate_auto()
        self.on_throttle_changed()

    def show_about(self):
        messagebox.showinfo(
            "About",
            "F710 \u2192 CRSF \u2192 ExpressLRS\n\n"
            "Sends CRSF RC frames to an ExpressLRS TX module over a serial port, "
            "so the module behaves exactly as if a handset were driving it.\n\n"
            "If channel data stops (gamepad unplugged, app closed), the module's "
            "UART watchdog drops the RF link and the receiver goes to its own "
            "failsafe. Set that failsafe up on the aircraft before flying.")

    def on_close(self):
        self.stop_link(reason="application closing")
        self._remember_latches()
        self.gamepad.stop()
        self.destroy()

    def _remember_latches(self):
        """Write where the latching channels were left, and nothing else.

        The config on disk is re-read first so that only this is written:
        closing the window is not a Save, and unsaved edits in the tabs
        should not be committed by one.
        """
        try:
            saved, _warning = configmod.load()
            saved["latches"] = self.mixer.latch_state()
            configmod.save(saved)
        except Exception as exc:
            # Closing must not fail over a file that will not be written.
            self.log("warn", f"Could not remember channel positions: {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", action="store_true",
                        help="use a simulated gamepad (bench testing without hardware)")
    args = parser.parse_args()
    app = App(simulate=args.sim)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
