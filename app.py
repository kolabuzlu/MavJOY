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
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

import config as configmod
import crsf
import gamepad as gp
import link as linkmod

REFRESH_MS = 50          # GUI refresh, 20 Hz
BAR_LEN = 150


def fmt_channel(value: int) -> str:
    return f"{value:4d}  ({crsf.crsf_to_us(value):.0f}\u00b5s)"


class App(tk.Tk):
    def __init__(self, simulate=False):
        super().__init__()
        self.title("MavJOY \u2014 gamepad \u2192 CRSF \u2192 ExpressLRS")
        self.geometry("1000x760")
        self.minsize(900, 700)

        self.cfg, warning = configmod.load()
        self.simulate = simulate
        self.link = None
        self._events = queue.Queue(maxsize=500)
        self._log_lines = 0
        self._ports = []
        self._module_q = queue.Queue()      # settings replies, link thread -> GUI
        self._module_scan = None            # index being probed during a scan
        self._rate_field = None             # the module's Packet Rate field
        self._rate_pending = None           # value we asked the module to take
        self._device = None                 # DEVICE_INFO from the module

        self.gamepad = (gp.SimGamepadThread() if simulate else gp.GamepadThread())
        self.gamepad.start()
        self.mixer = gp.Mixer(self.cfg)
        self.mixer.reset()

        self._build_menu()
        self._build_top()
        self._build_notebook()
        self._build_statusbar()

        self.bind("<Escape>", lambda _e: self.stop_link(reason="Esc pressed"))
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.refresh_ports()
        self.after(400, self.refresh_gamepads)   # give the pad thread time to scan
        self.after(REFRESH_MS, self._tick)

        if warning:
            self.log("warn", warning)
        if simulate:
            self.log("warn", "SIMULATION MODE - gamepad input is synthetic")
        self.log("info", "Ready. Fit the antenna and power the module before starting a link.")

    # =================================================================== UI
    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Save configuration", command=self.save_config)
        filemenu.add_command(label="Reload configuration", command=self.reload_config)
        filemenu.add_command(label="Reset to defaults", command=self.reset_config)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.on_close)
        menubar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.config(menu=menubar)

    def _build_top(self):
        top = ttk.LabelFrame(self, text="Link")
        top.pack(fill="x", padx=8, pady=(8, 4))

        row = ttk.Frame(top)
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
        self.rate_hint = ttk.Label(row, foreground="#777777",
                                   text="(PC→module, not the RF rate)")
        self.rate_hint.pack(side="left", padx=(6, 0))
        self._rate_warned = None
        self._on_rate_auto()

        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=6, pady=(0, 6))

        ttk.Label(row2, text="Gamepad").pack(side="left")
        self.pad_var = tk.StringVar()
        self.pad_combo = ttk.Combobox(row2, textvariable=self.pad_var,
                                      width=40, state="readonly")
        self.pad_combo.pack(side="left", padx=(4, 2))
        self.pad_combo.bind("<<ComboboxSelected>>", self.on_pad_selected)
        ttk.Button(row2, text="\u21bb", width=3,
                   command=self.refresh_gamepads).pack(side="left")

        self.start_btn = ttk.Button(row2, text="START LINK", command=self.start_link)
        self.start_btn.pack(side="right", padx=4)
        self.stop_btn = ttk.Button(row2, text="STOP  (Esc)", state="disabled",
                                   command=lambda: self.stop_link(reason="stopped by user"))
        self.stop_btn.pack(side="right", padx=4)

        # ---- big live status strip
        status = ttk.Frame(self)
        status.pack(fill="x", padx=8, pady=4)

        self.link_lbl = tk.Label(status, text="LINK STOPPED", width=18,
                                 font=("TkDefaultFont", 13, "bold"),
                                 bg="#555555", fg="white", padx=8, pady=8)
        self.link_lbl.pack(side="left")

        self.arm_lbl = tk.Label(status, text="ARM: \u2014", width=14,
                                font=("TkDefaultFont", 13, "bold"),
                                bg="#555555", fg="white", padx=8, pady=8)
        self.arm_lbl.pack(side="left", padx=(8, 0))

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
        self._build_module_tab(nb)
        self._build_telemetry_tab(nb)
        self._build_log_tab(nb)

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

        self.module_info_lbl = ttk.Label(
            tab, foreground="#777777",
            text="Start the link, then read the settings from the module.")
        self.module_info_lbl.pack(anchor="w", padx=10, pady=(0, 8))

        frm = ttk.LabelFrame(tab, text="RF packet rate")
        frm.pack(fill="x", padx=10, pady=4)

        ttk.Label(frm, wraplength=900, justify="left", foreground="#555555",
                  text="How fast the module transmits over the air. This lives "
                       "inside the module, and it is NOT the CRSF Hz box in the "
                       "toolbar — that one only sets how often this PC hands "
                       "frames to the module over USB. Sending CRSF faster will "
                       "never change the rate shown here.").pack(
            anchor="w", padx=10, pady=(8, 6))

        row = ttk.Frame(frm)
        row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(row, text="Packet rate", width=14).pack(side="left")
        self.rf_rate_var = tk.StringVar()
        self.rf_rate_combo = ttk.Combobox(row, textvariable=self.rf_rate_var,
                                          width=26, state="disabled")
        self.rf_rate_combo.pack(side="left", padx=4)
        self.rf_rate_combo.bind("<<ComboboxSelected>>", self.on_rf_rate_selected)
        self.rf_rate_lbl = ttk.Label(row, text="", foreground="#777777")
        self.rf_rate_lbl.pack(side="left", padx=8)

        ttk.Label(frm, wraplength=900, justify="left", foreground="#b36b00",
                  text="Changing this re-keys the RF link: the receiver drops "
                       "out and failsafes for a moment while both ends resync. "
                       "Never do it in flight. The app refuses while armed.").pack(
            anchor="w", padx=10, pady=(0, 10))

    # ------------------------------------------------------------ channels
    def _build_channels_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Channels")

        hdr = ttk.Frame(tab)
        hdr.pack(fill="x", padx=8, pady=(8, 2))
        for text, width in (("", 5), ("source", 10), ("index", 7), ("inv", 5),
                            ("steps", 7), ("value", 40), ("", 18)):
            ttk.Label(hdr, text=text, width=width).pack(side="left")

        self.ch_widgets = []
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=8)

        for i in range(crsf.NUM_CHANNELS):
            chcfg = self.mixer.channels[i]
            row = ttk.Frame(body)
            row.pack(fill="x", pady=1)

            ttk.Label(row, text=f"CH{i + 1}", width=5,
                      font=("TkDefaultFont", 9, "bold")).pack(side="left")

            src = tk.StringVar(value=chcfg.src)
            combo = ttk.Combobox(row, textvariable=src, width=9, state="readonly",
                                 values=list(gp.SOURCES))
            combo.pack(side="left", padx=1)
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, n=i: self.on_channel_changed(n))

            idx = tk.IntVar(value=chcfg.idx)
            spin = ttk.Spinbox(row, from_=0, to=31, width=4, textvariable=idx,
                               command=lambda n=i: self.on_channel_changed(n))
            spin.pack(side="left", padx=(6, 8))
            spin.bind("<KeyRelease>", lambda _e, n=i: self.on_channel_changed(n))

            inv = tk.BooleanVar(value=chcfg.inv)
            ttk.Checkbutton(row, variable=inv,
                            command=lambda n=i: self.on_channel_changed(n)
                            ).pack(side="left", padx=(6, 10))

            steps = tk.IntVar(value=chcfg.steps)
            sspin = ttk.Spinbox(row, from_=2, to=6, width=3, textvariable=steps,
                                command=lambda n=i: self.on_channel_changed(n))
            sspin.pack(side="left", padx=(0, 14))

            bar = ttk.Progressbar(row, length=220, maximum=1000)
            bar.pack(side="left")
            val = ttk.Label(row, text="\u2014", width=16, anchor="w")
            val.pack(side="left", padx=6)
            ttk.Label(row, text=configmod.CHANNEL_HINTS[i], width=16,
                      foreground="#777777").pack(side="left")

            self.ch_widgets.append({"src": src, "idx": idx, "inv": inv,
                                    "steps": steps, "bar": bar, "val": val})

        ttk.Label(tab, text="Mapping is one input to one channel. No mixing, no expo, "
                            "no curves \u2014 do all of that on the flight controller.",
                  foreground="#777777").pack(anchor="w", padx=8, pady=(10, 2))
        self.src_help = ttk.Label(tab, text="", foreground="#555555")
        self.src_help.pack(anchor="w", padx=8)

    # ------------------------------------------------------------ throttle
    def _build_throttle_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Throttle")
        t = self.cfg["throttle"]

        frm = ttk.Frame(tab)
        frm.pack(fill="x", padx=12, pady=12)

        ttk.Label(frm, text="Mode", width=14).grid(row=0, column=0, sticky="w")
        self.thr_mode = tk.StringVar(value=t["mode"])
        mode_combo = ttk.Combobox(frm, textvariable=self.thr_mode, width=10,
                                  state="readonly", values=list(gp.THROTTLE_MODES))
        mode_combo.grid(row=0, column=1, sticky="w")
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_throttle_changed())

        self.thr_help = ttk.Label(frm, text="", wraplength=620, justify="left",
                                  foreground="#555555")
        self.thr_help.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 14))

        def spin(label, key, row, lo, hi, hint=""):
            ttk.Label(frm, text=label, width=14).grid(row=row, column=0, sticky="w",
                                                      pady=3)
            var = tk.IntVar(value=t.get(key, 0))
            sp = ttk.Spinbox(frm, from_=lo, to=hi, width=5, textvariable=var,
                             command=self.on_throttle_changed)
            sp.grid(row=row, column=1, sticky="w")
            sp.bind("<KeyRelease>", lambda _e: self.on_throttle_changed())
            ttk.Label(frm, text=hint, foreground="#777777").grid(row=row, column=2,
                                                                 sticky="w", padx=8)
            return var

        self.thr_axis = spin("Up / axis", "axis", 2, -1, 31,
                             "axis or button that raises throttle (F710 X mode: RT = axis 5)")
        self.thr_axis_dn = spin("Down", "axis_down", 3, -1, 31,
                                "ramp mode only (LT = axis 2)")
        self.thr_cut = spin("Cut button", "cut_button", 4, -1, 31,
                            "instantly drops throttle to idle (Back = button 6)")

        ttk.Label(frm, text="Ramp rate", width=14).grid(row=5, column=0, sticky="w",
                                                        pady=3)
        self.thr_rate = tk.DoubleVar(value=t.get("ramp_rate", 0.6))
        ttk.Scale(frm, from_=0.1, to=2.0, variable=self.thr_rate, length=200,
                  command=lambda _v: self.on_throttle_changed()
                  ).grid(row=5, column=1, columnspan=2, sticky="w", padx=(0, 8))
        self.thr_rate_lbl = ttk.Label(frm, text="", foreground="#777777")
        self.thr_rate_lbl.grid(row=5, column=3, sticky="w")

        ttk.Label(frm, text="Deadzone", width=14).grid(row=6, column=0, sticky="w",
                                                       pady=3)
        self.thr_dz = tk.DoubleVar(value=t.get("deadzone", 0.06))
        ttk.Scale(frm, from_=0.0, to=0.3, variable=self.thr_dz, length=200,
                  command=lambda _v: self.on_throttle_changed()
                  ).grid(row=6, column=1, columnspan=2, sticky="w", padx=(0, 8))
        self.thr_dz_lbl = ttk.Label(frm, text="", foreground="#777777")
        self.thr_dz_lbl.grid(row=6, column=3, sticky="w")

        ttk.Label(frm, text="Stick deadzone", width=14).grid(row=7, column=0,
                                                             sticky="w", pady=3)
        self.stick_dz = tk.DoubleVar(value=self.cfg.get("deadzone", 0.05))
        ttk.Scale(frm, from_=0.0, to=0.3, variable=self.stick_dz, length=200,
                  command=lambda _v: self.on_throttle_changed()
                  ).grid(row=7, column=1, columnspan=2, sticky="w", padx=(0, 8))
        self.stick_dz_lbl = ttk.Label(frm, text="", foreground="#777777")
        self.stick_dz_lbl.grid(row=7, column=3, sticky="w")

        ttk.Label(tab, text="A link will not start unless throttle reads 0 %.",
                  foreground="#777777").pack(anchor="w", padx=12, pady=8)
        self.on_throttle_changed()

    # -------------------------------------------------------------- inputs
    def _build_inputs_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Inputs")

        ttk.Label(tab, text="Live values straight from the gamepad \u2014 use this to "
                            "find the axis and button numbers for the mapping.",
                  foreground="#777777").pack(anchor="w", padx=10, pady=(10, 6))

        self.axis_frame = ttk.LabelFrame(tab, text="Axes")
        self.axis_frame.pack(fill="x", padx=10, pady=4)
        self.axis_widgets = []
        for i in range(10):
            row = ttk.Frame(self.axis_frame)
            row.pack(fill="x", padx=6, pady=1)
            lbl = ttk.Label(row, text=f"axis {i}", width=8)
            lbl.pack(side="left")
            bar = ttk.Progressbar(row, length=260, maximum=2000)
            bar.pack(side="left")
            val = ttk.Label(row, text="\u2014", width=10)
            val.pack(side="left", padx=6)
            row.pack_forget()
            self.axis_widgets.append({"row": row, "bar": bar, "val": val})

        self.btn_frame = ttk.LabelFrame(tab, text="Buttons")
        self.btn_frame.pack(fill="x", padx=10, pady=8)
        self.btn_widgets = []
        grid = ttk.Frame(self.btn_frame)
        grid.pack(padx=6, pady=6)
        for i in range(20):
            lbl = tk.Label(grid, text=str(i), width=3, relief="ridge",
                           bg="#dddddd", padx=2, pady=2)
            lbl.grid(row=i // 10, column=i % 10, padx=2, pady=2)
            lbl.grid_remove()
            self.btn_widgets.append(lbl)

        self.hat_lbl = ttk.Label(tab, text="hats: \u2014")
        self.hat_lbl.pack(anchor="w", padx=12, pady=4)

    # ----------------------------------------------------------- telemetry
    def _build_telemetry_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Telemetry")
        self.telem_text = tk.Text(tab, height=24, wrap="none",
                                  font=("TkFixedFont", 10))
        self.telem_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.telem_text.configure(state="disabled")

    # ----------------------------------------------------------------- log
    def _build_log_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Log")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tk.Text(wrap, height=20, wrap="word",
                                font=("TkFixedFont", 10))
        sb = ttk.Scrollbar(wrap, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("error", foreground="#cc0000")
        self.log_text.tag_configure("warn", foreground="#b36b00")
        self.log_text.tag_configure("info", foreground="#333333")

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

    def refresh_gamepads(self):
        self.gamepad.rescan()
        self.after(300, self._fill_gamepads)

    def _fill_gamepads(self):
        devices = self.gamepad.devices
        self.pad_combo["values"] = [f"{i}: {n}" for i, n in enumerate(devices)]
        if devices:
            idx = min(self.cfg.get("gamepad_index", 0), len(devices) - 1)
            self.pad_combo.current(idx)
            self.gamepad.select(idx)
            self.log("info", f"Gamepad selected: {devices[idx]}")
        else:
            self.pad_var.set("")
            self.log("warn", "No gamepad detected. Check the F710 dongle and the "
                             "X/D switch (use X).")

    def on_pad_selected(self, _evt=None):
        label = self.pad_var.get()
        if not label:
            return
        idx = int(label.split(":", 1)[0])
        self.cfg["gamepad_index"] = idx
        self.gamepad.select(idx)

    def on_channel_changed(self, n):
        w = self.ch_widgets[n]
        ch = self.mixer.channels[n]
        try:
            ch.src = w["src"].get()
            ch.idx = int(w["idx"].get())
            ch.inv = bool(w["inv"].get())
            ch.steps = int(w["steps"].get())
        except (tk.TclError, ValueError):
            return
        self.cfg["channels"][n] = ch.to_dict()
        self.src_help.config(text=f"{ch.src}: {gp.SOURCE_HELP.get(ch.src, '')}")

    def on_throttle_changed(self, _evt=None):
        t = self.cfg["throttle"]
        try:
            t["mode"] = self.thr_mode.get()
            t["axis"] = int(self.thr_axis.get())
            t["axis_down"] = int(self.thr_axis_dn.get())
            t["cut_button"] = int(self.thr_cut.get())
            t["ramp_rate"] = round(float(self.thr_rate.get()), 2)
            t["deadzone"] = round(float(self.thr_dz.get()), 3)
            self.cfg["deadzone"] = round(float(self.stick_dz.get()), 3)
        except (tk.TclError, ValueError):
            return
        self.mixer.deadzone = self.cfg["deadzone"]
        self.thr_help.config(text=gp.THROTTLE_MODE_HELP.get(t["mode"], ""))
        self.thr_rate_lbl.config(text=f"{t['ramp_rate']:.2f} (idle\u2192full in "
                                      f"{1.0 / max(t['ramp_rate'], 0.01):.1f}s)")
        self.thr_dz_lbl.config(text=f"{t['deadzone']:.2f}")
        self.stick_dz_lbl.config(text=f"{self.cfg['deadzone']:.2f}")

    # ---------------------------------------------------------------- link
    def start_link(self):
        if self.link and self.link.running:
            return

        port = self.selected_port()
        if not port:
            messagebox.showerror("No serial port", "Select the serial port your "
                                                   "ExpressLRS module is on.")
            return

        state = self.gamepad.state
        if not state.is_fresh():
            messagebox.showerror("No gamepad", "No live gamepad data. Select a "
                                               "gamepad and move a stick to confirm "
                                               "it is reporting.")
            return

        # safety: reset every latch, then verify the throttle really is at idle
        self.mixer.reset()
        values = self.mixer.compute(state)
        thr_channels = [i for i, c in enumerate(self.mixer.channels)
                        if c.src == "throttle"]
        for i in thr_channels:
            if values[i] > crsf.CHANNEL_MIN + 20:
                messagebox.showwarning(
                    "Throttle not at idle",
                    f"CH{i + 1} reads {values[i]} ({crsf.crsf_to_us(values[i]):.0f}\u00b5s).\n\n"
                    "Release the throttle input and try again.")
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
    def read_module_settings(self):
        """Ask the module what it is, then hunt down its Packet Rate field."""
        if not (self.link and self.link.running):
            messagebox.showinfo("Link not running",
                                "Start the link first. Module settings travel "
                                "over the same serial connection as the "
                                "channel data.")
            return
        self.module_btn.config(state="disabled")
        self.module_info_lbl.config(text="asking the module to identify itself...")
        self.link.submit("ping", on_done=self._module_cb("ping"))

    def _module_cb(self, tag):
        """Callbacks fire on the link thread; hand them to the GUI thread."""
        return lambda result, error: self._module_q.put((tag, result, error))

    def _poll_module(self):
        while True:
            try:
                tag, result, error = self._module_q.get_nowait()
            except queue.Empty:
                return
            if error or result is None:
                self.module_info_lbl.config(
                    text=f"module did not answer ({error or 'no data'})")
                self.module_btn.config(state="normal")
                if self._rate_field is not None:
                    self.rf_rate_combo.config(state="readonly")
                self.log("warn", f"Module settings: {error or 'no data'}")
                continue
            if self.link is None or not self.link.running:
                self.module_btn.config(state="normal")
                continue
            if tag == "ping":
                self._device = result
                self.module_info_lbl.config(
                    text=f"{result['name']}  •  ExpressLRS {result['version']}"
                         f"  •  {result['field_count']} settings")
                self._module_scan = 1
                self.link.submit("read", index=1, on_done=self._module_cb("field"))
            elif tag == "field":
                self._on_module_field(result)

    def _on_module_field(self, field):
        if "packet rate" in field.name.lower():
            refused = (self._rate_pending is not None
                       and field.value != self._rate_pending)
            wanted = (field.label_for(self._rate_pending)
                      if self._rate_pending is not None else "")
            self._rate_pending = None
            self._rate_field = field
            self.rf_rate_combo["values"] = [label for _v, label in field.choices()]
            self.rf_rate_combo.config(state="readonly")
            self.rf_rate_var.set(field.current_label)
            self.rf_rate_lbl.config(text=f"reported by the module")
            self.module_btn.config(state="normal")
            self.module_info_lbl.config(
                text=f"{(self._device or {}).get('name', 'module')}  •  "
                     f"ExpressLRS {(self._device or {}).get('version', '?')}"
                     f"  •  packet rate {field.current_label}")
            if refused:
                self.log("warn", f"The module refused {wanted} and stayed on "
                                 f"{field.current_label}. ExpressLRS rejects some "
                                 f"rates depending on telemetry ratio and switch "
                                 f"mode.")
                messagebox.showwarning(
                    "Rate refused",
                    f"The module would not take {wanted} and is still on "
                    f"{field.current_label}. ExpressLRS blocks some rates "
                    f"depending on the telemetry ratio and switch mode.")
            else:
                self.log("info", f"Module packet rate: {field.current_label}")
            return

        # Not it. Packet Rate is field 1 on current firmware, but walk the
        # list rather than trusting that, in case the layout ever moves.
        nxt = (self._module_scan or 1) + 1
        limit = (self._device or {}).get("field_count", 0)
        if nxt > limit or self.link is None:
            self.module_info_lbl.config(text="this module has no Packet Rate setting")
            self.module_btn.config(state="normal")
            return
        self._module_scan = nxt
        self.link.submit("read", index=nxt, on_done=self._module_cb("field"))

    def on_rf_rate_selected(self, _evt=None):
        field = self._rate_field
        if field is None or not (self.link and self.link.running):
            return
        label = self.rf_rate_var.get()
        value = next((v for v, l in field.choices() if l == label), None)
        if value is None or value == field.value:
            return

        armed = self.mixer.armed_channels()
        if armed:
            messagebox.showwarning(
                "Armed",
                f"CH{armed[0]} is armed.\n\nDisarm before changing "
                f"the packet rate - the RF link drops while both ends "
                f"resync.")
            self.rf_rate_var.set(field.current_label)
            return

        if not messagebox.askokcancel(
                "Change packet rate",
                f"Set the module to {label}?\n\nThe RF link drops and "
                f"the receiver goes to failsafe for a moment while the module "
                f"and receiver resync. Props off."):
            self.rf_rate_var.set(field.current_label)
            return

        self._rate_pending = value
        self.rf_rate_combo.config(state="disabled")
        self.module_info_lbl.config(text=f"writing packet rate {label}...")
        self.log("info", f"Setting module packet rate to {label}")
        self.link.submit("write", index=field.index, value=value,
                         on_done=self._module_cb("field"))

    def stop_link(self, reason=""):
        if self.link:
            self.link.stop()
            self.link.join(timeout=1.0)
            self.link = None
            if reason:
                self.log("info", f"Link stopped ({reason}). Receiver goes to failsafe.")
        self.start_btn.config(state="normal")
        self._rate_field = None
        self._device = None
        self.rf_rate_combo.config(state="disabled")
        self.rf_rate_var.set("")
        self.rf_rate_lbl.config(text="")
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
        self._apply_auto_rate()
        # a link thread that died (port vanished, write error) must not leave
        # the UI stuck in "started"
        if self.link is not None and not self.link.is_alive():
            self.stop_link(reason="link thread ended")

        state = self.gamepad.state
        link_active = self.link is not None
        running = bool(self.link and self.link.running)

        # ---- channel values. While a link exists the link thread owns the
        # mixer; the GUI only ever reads what it last produced.
        if link_active:
            values = list(self.mixer.last_values)
        else:
            values = self.mixer.compute(state) if state.connected else \
                self.mixer.failsafe_values()

        for i, w in enumerate(self.ch_widgets):
            v = values[i]
            w["bar"]["value"] = max(0, min(1000, (v - crsf.CHANNEL_MIN) * 1000 //
                                           (crsf.CHANNEL_MAX - crsf.CHANNEL_MIN)))
            w["val"].config(text=fmt_channel(v))

        # ---- throttle + arm
        thr_pct = self.mixer.throttle.value * 100.0
        self.thr_bar["value"] = thr_pct
        self.thr_lbl.config(text=f"{thr_pct:3.0f} %")

        armed = self.mixer.armed_channels()
        if armed:
            self.arm_lbl.config(text=f"ARM CH{armed[0]}: ON", bg="#cc0000")
        else:
            self.arm_lbl.config(text="ARM: off", bg="#2e7d32")

        # ---- link state
        if running and self.link.transmitting:
            self.link_lbl.config(text="TRANSMITTING", bg="#2e7d32")
        elif running:
            self.link_lbl.config(text="PORT OPEN / NO TX", bg="#b36b00")
        else:
            self.link_lbl.config(text="LINK STOPPED", bg="#555555")

        if running:
            telem, stats = self.link.snapshot()
            self.rate_lbl.config(text=f"{stats.actual_rate:5.1f} Hz sent  "
                                      f"(jit {stats.jitter_ms:.1f} ms)")
            link = telem.get("link")
            if link and time.monotonic() - link.get("_t", 0) < 2.0:
                self.rf_lbl.config(
                    text=f"LQ {link['up_lq']}%  RSSI {link['up_rssi_1']} dBm  "
                         f"SNR {link['up_snr']}  {link.get('tx_power_mw') or '?'} mW")
            else:
                self.rf_lbl.config(text="no telemetry")
            self.status_var.set(
                f"sent {stats.frames_sent}   skipped {stats.frames_skipped}   "
                f"telemetry frames {stats.telem_frames}   crc errors {stats.crc_errors}"
                f"   write errors {stats.write_errors}")
            self._update_telemetry(telem, stats)
        else:
            self.rate_lbl.config(text="\u2014 Hz")
            self.rf_lbl.config(text="no telemetry")
            src = "simulated pad" if self.simulate else (
                state.device_name if state.connected else "no gamepad")
            self.status_var.set(f"idle   |   input: {src}")

        self._update_inputs(state)

    def _update_inputs(self, state):
        for i, w in enumerate(self.axis_widgets):
            if i < len(state.axes):
                w["row"].pack(fill="x", padx=6, pady=1)
                v = state.axes[i]
                w["bar"]["value"] = (v + 1.0) * 1000
                w["val"].config(text=f"{v:+.3f}")
            else:
                w["row"].pack_forget()

        for i, lbl in enumerate(self.btn_widgets):
            if i < len(state.buttons):
                lbl.grid()
                lbl.config(bg="#2e7d32" if state.buttons[i] else "#dddddd",
                           fg="white" if state.buttons[i] else "black")
            else:
                lbl.grid_remove()

        hats = state.hats if state.hats else "\u2014"
        self.hat_lbl.config(text=f"hats: {hats}")

    def _update_telemetry(self, telem, stats):
        lines = []
        now = time.monotonic()
        for key in ("link", "battery", "attitude", "gps"):
            data = telem.get(key)
            if not data:
                continue
            age = now - data.get("_t", now)
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
        self.telem_text.configure(state="normal")
        self.telem_text.delete("1.0", "end")
        self.telem_text.insert("1.0", "\n".join(lines))
        self.telem_text.configure(state="disabled")

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
            w["idx"].set(ch.idx)
            w["inv"].set(ch.inv)
            w["steps"].set(ch.steps)
        t = self.cfg["throttle"]
        self.thr_mode.set(t["mode"])
        self.thr_axis.set(t["axis"])
        self.thr_axis_dn.set(t["axis_down"])
        self.thr_cut.set(t["cut_button"])
        self.thr_rate.set(t["ramp_rate"])
        self.thr_dz.set(t["deadzone"])
        self.stick_dz.set(self.cfg["deadzone"])
        self.baud_var.set(str(self.cfg["baud"]))
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
        self.gamepad.stop()
        self.destroy()


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
