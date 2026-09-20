"""
Colour themes.

Tk has no dark mode of its own, so this sets one up by hand: a palette of
named roles, plus ttk styles built on the "clam" theme, which is the only
built-in one that lets its colours be overridden properly. Widgets ask the
palette for a role rather than naming a colour, so a second theme is just
another dict.

    self.pal = theme.apply(root, "dark")
    ttk.Label(parent, foreground=self.pal["muted"])
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# Roles, not colours. Anything drawing itself looks one of these up.
DARK = {
    "name": "dark",
    "bg": "#1b1f27",          # window
    "panel": "#232936",       # frames, notebook pages
    "field": "#2b3242",       # entries, spin boxes, combo boxes
    "text": "#dfe3ea",
    "muted": "#8b94a7",       # secondary labels
    "faint": "#6d7688",       # help text
    "border": "#39414f",
    "accent": "#4c8dff",
    "on_accent": "#ffffff",
    "ok": "#2f9e44",
    "warn": "#c07f12",
    "danger": "#d6455d",
    "idle": "#454d5e",        # status chip with nothing to report
    "off": "#2b3242",         # button indicator, released
    "select_bg": "#33507e",
    "bar": "#4c8dff",
    "bar_trough": "#2b3242",
}

LIGHT = {
    "name": "light",
    "bg": "#f0f0f0",
    "panel": "#f0f0f0",
    "field": "#ffffff",
    "text": "#1a1a1a",
    "muted": "#777777",
    "faint": "#555555",
    "border": "#bfbfbf",
    "accent": "#0a5cd6",
    "on_accent": "#ffffff",
    "ok": "#2e7d32",
    "warn": "#b36b00",
    "danger": "#cc0000",
    "idle": "#555555",
    "off": "#dddddd",
    "select_bg": "#cce4ff",
    "bar": "#0a5cd6",
    "bar_trough": "#dddddd",
}

PALETTES = {"dark": DARK, "light": LIGHT}
DEFAULT = "dark"


def palette(name: str) -> dict:
    return PALETTES.get(name, PALETTES[DEFAULT])


def apply(root: tk.Misc, name: str = DEFAULT) -> dict:
    """Paint the whole widget tree in `name` and return its palette."""
    pal = palette(name)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass    # fall back to whatever is available; colours still mostly take

    bg, panel, field = pal["bg"], pal["panel"], pal["field"]
    text, border, accent = pal["text"], pal["border"], pal["accent"]

    root.configure(background=bg)

    style.configure(".", background=panel, foreground=text,
                    fieldbackground=field, bordercolor=border,
                    darkcolor=panel, lightcolor=panel,
                    troughcolor=pal["bar_trough"], focuscolor=accent,
                    insertcolor=text)

    style.configure("TFrame", background=panel)
    style.configure("TLabel", background=panel, foreground=text)
    style.configure("TLabelframe", background=panel, foreground=text,
                    bordercolor=border)
    style.configure("TLabelframe.Label", background=panel, foreground=pal["muted"])
    style.configure("TCheckbutton", background=panel, foreground=text)
    style.map("TCheckbutton",
              background=[("active", panel)],
              indicatorcolor=[("selected", accent), ("!selected", field)])

    style.configure("TButton", background=field, foreground=text,
                    bordercolor=border, focusthickness=1, padding=(8, 3))
    style.map("TButton",
              background=[("pressed", accent), ("active", pal["select_bg"]),
                          ("disabled", panel)],
              foreground=[("disabled", pal["faint"])])

    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=field, background=field,
                        foreground=text, bordercolor=border,
                        arrowcolor=pal["muted"], insertcolor=text)
        style.map(widget,
                  fieldbackground=[("readonly", field), ("disabled", panel)],
                  foreground=[("disabled", pal["faint"])],
                  arrowcolor=[("disabled", pal["faint"])])

    # The combo box drop-down is a classic Tk listbox, not a ttk widget.
    root.option_add("*TCombobox*Listbox.background", field)
    root.option_add("*TCombobox*Listbox.foreground", text)
    root.option_add("*TCombobox*Listbox.selectBackground", pal["select_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", text)

    style.configure("TNotebook", background=bg, bordercolor=border)
    style.configure("TNotebook.Tab", background=panel, foreground=pal["muted"],
                    padding=(12, 5), bordercolor=border)
    style.map("TNotebook.Tab",
              background=[("selected", field)],
              foreground=[("selected", text)])

    style.configure("TProgressbar", background=pal["bar"],
                    troughcolor=pal["bar_trough"], bordercolor=border,
                    lightcolor=pal["bar"], darkcolor=pal["bar"])
    style.configure("TScrollbar", background=field, troughcolor=bg,
                    bordercolor=border, arrowcolor=pal["muted"])
    style.map("TScrollbar", background=[("active", pal["select_bg"])])
    style.configure("TSeparator", background=border)

    # Menus are classic Tk too.
    root.option_add("*Menu.background", panel)
    root.option_add("*Menu.foreground", text)
    root.option_add("*Menu.activeBackground", pal["select_bg"])
    root.option_add("*Menu.activeForeground", text)
    root.option_add("*Menu.borderWidth", 1)

    return pal
