"""A small moving map for the Telemetry tab.

Two layers over one set of coordinates. The plot - home, the aircraft, its
track, range rings - is drawn by this file and always correct. Map tiles
are painted behind it when they happen to be available.

That order is deliberate. A flying field usually has no internet, and a
map that answers "no signal" with blank grey squares is worse than no map
at all: it looks like the aircraft has vanished. Here the tiles are the
decoration and the plot is the instrument, so losing the internet costs
you the scenery and nothing else.

Both layers share one Web Mercator transform at a single zoom, which is
what keeps them registered: the aircraft sits over the right piece of
ground, not merely somewhere on the canvas.
"""

from __future__ import annotations

import math
import os
import queue
import threading
import tkinter as tk
from collections import deque
from tkinter import ttk

TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# openstreetmap.org asks that applications identify themselves and not
# bulk-download. This fetches only the handful of tiles under the current
# view, once each, and keeps them on disk afterwards.
USER_AGENT = "MavJOY/1.1 (+https://github.com/kolabuzlu/MavJOY)"

MIN_ZOOM, MAX_ZOOM = 9, 17
TRAIL_MAX = 900                  # about 15 minutes at 1 Hz
FETCH_TIMEOUT = 6.0

# Ring distances in metres. The smallest one that contains the aircraft,
# doubled, sets the view - so the plane sits comfortably inside rather
# than on the edge.
RING_STEPS = (50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000)


def deg2px(lat, lon, zoom):
    """Longitude and latitude to Web Mercator pixels at `zoom`."""
    n = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n
    return x, y


def metres_per_pixel(lat, zoom):
    return 156543.03392804097 * math.cos(math.radians(lat)) / (2 ** zoom)


def distance_bearing(lat1, lon1, lat2, lon2):
    """Great-circle metres and initial bearing, home to aircraft."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    dist = 2 * r * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return dist, (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


class TileStore:
    """Tiles from disk, and from the network when that is possible.

    Every miss is remembered as well as every hit, so a field with no
    signal costs one attempt per tile rather than one per redraw.
    """

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.images = {}             # (z,x,y) -> PhotoImage
        self._failed = set()
        self._asked = set()
        self._queue = queue.Queue()
        self._done = queue.Queue()
        self._worker = None
        self.enabled = True
        self.online = None           # None until the first attempt

    def _path(self, z, x, y):
        return os.path.join(self.cache_dir, str(z), str(x), f"{y}.png")

    def _start(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="map-tiles")
            self._worker.start()

    def _run(self):
        import urllib.request
        while True:
            try:
                key = self._queue.get(timeout=30)
            except queue.Empty:
                return
            z, x, y = key
            try:
                req = urllib.request.Request(
                    TILE_URL.format(z=z, x=x, y=y),
                    headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                    blob = r.read()
                path = self._path(z, x, y)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(blob)
                os.replace(tmp, path)
                self._done.put((key, blob, None))
            except Exception as exc:
                self._done.put((key, None, exc))

    def get(self, z, x, y):
        """A PhotoImage if one is to hand, else None; never blocks."""
        key = (z, x, y)
        if key in self.images:
            return self.images[key]
        if key in self._failed:
            return None

        path = self._path(z, x, y)
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    img = tk.PhotoImage(data=fh.read())
                self.images[key] = img
                return img
            except Exception:
                self._failed.add(key)
                return None

        if self.enabled and key not in self._asked:
            self._asked.add(key)
            self._queue.put(key)
            self._start()
        return None

    def drain(self):
        """Take in whatever the worker finished. Returns True if anything
        arrived, so the caller knows a redraw is worthwhile."""
        got = False
        while True:
            try:
                key, blob, err = self._done.get_nowait()
            except queue.Empty:
                return got
            if blob is None:
                self._failed.add(key)
                if self.online is None:
                    self.online = False
                continue
            try:
                self.images[key] = tk.PhotoImage(data=blob)
                self.online = True
                got = True
            except Exception:
                self._failed.add(key)

    def forget_failures(self):
        """Let tiles that failed be tried again - after the internet
        arrives, say, or when the user asks for tiles a second time."""
        self._failed.clear()
        self._asked -= self._failed
        self._asked.clear()


class MapView(ttk.Frame):
    """Canvas with the tiles behind and the aircraft plot in front."""

    def __init__(self, parent, palette, cache_dir, width=430, height=330):
        super().__init__(parent)
        self.pal = palette
        self.tiles = TileStore(cache_dir)
        # Not _w/_h: Widget._w is Tkinter's own path name for this
        # widget, and overwriting it breaks every child made after.
        self._cw, self._ch = width, height

        self.home = None             # (lat, lon)
        self.pos = None              # (lat, lon)
        self.heading = 0.0
        self.sats = 0
        self.alt = 0.0
        self.speed = 0.0
        self.trail = deque(maxlen=TRAIL_MAX)
        self._zoom = 15
        self._ink = palette["text"]
        self._ink_soft = palette["muted"]
        self._images = []            # anchors, or Tk frees them mid-draw

        head = ttk.Frame(self)
        head.pack(fill="x")
        self.show_tiles = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text="map tiles", variable=self.show_tiles,
                        command=self._tiles_toggled).pack(side="left")
        ttk.Button(head, text="Set home", width=9,
                   command=self.set_home).pack(side="right")

        self.canvas = tk.Canvas(self, width=width, height=height,
                                background=palette["field"],
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True, pady=(4, 2))

        self.status = tk.StringVar(value="no GPS")
        ttk.Label(self, textvariable=self.status,
                  foreground=palette["muted"]).pack(anchor="w")
        self.draw()

    # ------------------------------------------------------------- inputs
    def set_home(self):
        if self.pos:
            self.home = self.pos
            self.trail.clear()
            self.draw()

    def _tiles_toggled(self):
        self.tiles.enabled = self.show_tiles.get()
        if self.tiles.enabled:
            self.tiles.forget_failures()
        self.draw()

    def update_position(self, gps):
        """Feed one GPS frame; None means the aircraft has gone quiet."""
        if not gps:
            return
        lat, lon = gps.get("lat"), gps.get("lon")
        # 0,0 is the Atlantic, and it is what a receiver reports before it
        # has a fix. Plotting it would put the aircraft off Africa.
        if lat is None or lon is None or (abs(lat) < 0.001 and abs(lon) < 0.001):
            return
        self.pos = (lat, lon)
        self.heading = gps.get("heading", 0.0) or 0.0
        self.sats = gps.get("sats", 0) or 0
        self.alt = gps.get("altitude_m", 0) or 0
        self.speed = gps.get("speed_kmh", 0.0) or 0.0
        if self.home is None:
            self.home = self.pos
        if not self.trail or self.trail[-1] != self.pos:
            self.trail.append(self.pos)
        self.draw()

    def poll(self):
        """Called on the GUI tick; redraws if tiles have landed."""
        if self.tiles.drain():
            self.draw()

    # ------------------------------------------------------------ drawing
    def _size(self):
        """The canvas as it actually is, not as it was asked to be.

        It is packed to fill, so it ends up taller than the height given at
        construction. Choosing a zoom from the requested size picks one
        step too far out and leaves the whole flight in a knot in the
        middle.
        """
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1 or h <= 1:          # before the first layout pass
            return self._cw, self._ch
        return w, h

    def _pick_zoom(self, centre_lat, span_m):
        """The largest zoom that still fits the flight on the canvas.

        The view is centred between home and the aircraft, so the ground
        that has to fit is the distance between them plus a margin - not
        twice it, which is what centring on home would need.
        """
        w, h = self._size()
        want = max(span_m, 60.0) * 1.5
        for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
            if metres_per_pixel(centre_lat, z) * min(w, h) >= want:
                return z
        return MIN_ZOOM

    def draw(self):
        c = self.canvas
        c.delete("all")
        self._images = []
        w, h = self._size()

        if self.pos is None and self.home is None:
            c.create_text(w / 2, h / 2, text="waiting for a GPS position",
                          fill=self.pal["muted"])
            self.status.set("no GPS")
            return

        anchor = self.pos or self.home
        dist = bearing = 0.0
        if self.home and self.pos:
            dist, bearing = distance_bearing(*self.home, *self.pos)

        centre_lat = (anchor[0] + self.home[0]) / 2 if self.home else anchor[0]
        centre_lon = (anchor[1] + self.home[1]) / 2 if self.home else anchor[1]
        self._zoom = self._pick_zoom(centre_lat, dist)

        cx, cy = deg2px(centre_lat, centre_lon, self._zoom)
        ox, oy = cx - w / 2, cy - h / 2

        def to_canvas(lat, lon):
            px, py = deg2px(lat, lon, self._zoom)
            return px - ox, py - oy

        # Rings, labels and the scale bar have to read against whatever is
        # behind them, and that is either a dark empty canvas or a light
        # street map. Palette colours are picked for the first and vanish
        # on the second, so the ink follows whichever actually got drawn.
        painted = self._draw_tiles(c, ox, oy) if self.show_tiles.get() else 0
        self._ink = "#16161c" if painted else self.pal["text"]
        self._ink_soft = "#45454f" if painted else self.pal["muted"]

        self._draw_rings(c, to_canvas, centre_lat)
        self._draw_trail(c, to_canvas)
        self._draw_markers(c, to_canvas)
        self._draw_scale(c, centre_lat)

        if self.home and self.pos:
            self.status.set(
                f"{self._fmt_m(dist)}   bearing {bearing:03.0f}°   "
                f"alt {self.alt:.0f} m   {self.speed:.0f} km/h   "
                f"{self.sats} sats")
        else:
            self.status.set(f"{self.sats} sats")

    @staticmethod
    def _fmt_m(m):
        return f"{m/1000:.2f} km" if m >= 1000 else f"{m:.0f} m"

    def _draw_tiles(self, c, ox, oy):
        """Paint what tiles are to hand; returns how many landed."""
        painted = 0
        n = 2 ** self._zoom
        x0, y0 = int(ox // TILE_SIZE), int(oy // TILE_SIZE)
        w, h = self._size()
        x1 = int((ox + w) // TILE_SIZE)
        y1 = int((oy + h) // TILE_SIZE)
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                if not (0 <= tx < n and 0 <= ty < n):
                    continue
                img = self.tiles.get(self._zoom, tx, ty)
                if img is None:
                    continue
                c.create_image(tx * TILE_SIZE - ox, ty * TILE_SIZE - oy,
                               image=img, anchor="nw")
                self._images.append(img)
                painted += 1
        return painted

    def _draw_rings(self, c, to_canvas, lat):
        if not self.home:
            return
        hx, hy = to_canvas(*self.home)
        mpp = metres_per_pixel(lat, self._zoom)
        for metres in RING_STEPS:
            r = metres / mpp
            if r < 18 or r > max(self._size()):
                continue
            c.create_oval(hx - r, hy - r, hx + r, hy + r,
                          outline=self._ink_soft, dash=(3, 4))
            c.create_text(hx, hy - r - 7, text=self._fmt_m(metres),
                          fill=self._ink_soft, font=("TkDefaultFont", 7))

    def _draw_trail(self, c, to_canvas):
        if len(self.trail) < 2:
            return
        pts = []
        for lat, lon in self.trail:
            pts.extend(to_canvas(lat, lon))
        c.create_line(*pts, fill=self.pal["accent"], width=2, smooth=True)

    def _draw_markers(self, c, to_canvas):
        if self.home:
            hx, hy = to_canvas(*self.home)
            c.create_line(hx - 7, hy, hx + 7, hy, fill=self.pal["ok"], width=2)
            c.create_line(hx, hy - 7, hx, hy + 7, fill=self.pal["ok"], width=2)
            c.create_oval(hx - 5, hy - 5, hx + 5, hy + 5,
                          outline=self.pal["ok"], width=2)
        if not self.pos:
            return
        px, py = to_canvas(*self.pos)
        # An arrowhead pointing where the aircraft is going. Heading is
        # degrees clockwise from north; canvas y grows downward.
        a = math.radians(self.heading - 90.0)
        nose = 11.0
        tip = (px + nose * math.cos(a), py + nose * math.sin(a))
        left = (px + 7 * math.cos(a + 2.5), py + 7 * math.sin(a + 2.5))
        right = (px + 7 * math.cos(a - 2.5), py + 7 * math.sin(a - 2.5))
        c.create_polygon(*tip, *left, (px, py), *right,
                         fill=self.pal["warn"], outline=self.pal["text"])

    def _draw_scale(self, c, lat):
        mpp = metres_per_pixel(lat, self._zoom)
        for metres in RING_STEPS:
            px = metres / mpp
            if 40 <= px <= self._size()[0] * 0.4:
                break
        else:
            return
        x, y = 12, self._size()[1] - 14
        c.create_line(x, y, x + px, y, fill=self._ink, width=2)
        c.create_line(x, y - 4, x, y + 4, fill=self._ink, width=2)
        c.create_line(x + px, y - 4, x + px, y + 4, fill=self._ink, width=2)
        c.create_text(x + px / 2, y - 9, text=self._fmt_m(metres),
                      fill=self._ink, font=("TkDefaultFont", 8))
