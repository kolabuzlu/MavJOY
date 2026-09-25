"""A small moving map for the Telemetry tab.

Two layers over one set of coordinates. The plot - the marker, the
aircraft, its track, range rings - is drawn here and always correct. Map tiles
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
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

import config

TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# openstreetmap.org asks that applications identify themselves and not
# bulk-download. This fetches only the handful of tiles under the current
# view, once each, and keeps them on disk afterwards.
USER_AGENT = f"MavJOY/{config.VERSION} (+https://github.com/kolabuzlu/MavJOY)"

MIN_ZOOM, MAX_ZOOM = 9, 17

# The marker sets itself from the first fix with at least this many
# satellites. A receiver reports positions long before it has a solid fix,
# and the first ones can be tens of metres out - and the marker is what
# every bearing and, when there is no baro frame, every altitude is
# measured from. HITL never shows this: a simulator hands over a perfect
# fix at once. Six is where both INAV and ArduPilot start trusting GPS.
MIN_SATS_FOR_MARKER = 6

# How long a baro altitude stays good enough to show. It has to outlast
# the slowest schedule either firmware uses - ArduPilot drops the baro
# frame to one every 3 s when telemetry bandwidth is short - or the
# readout would flick between sources on every gap.
BARO_FRESH_S = 10.0

# About nine tiles cover the canvas. The rest is slack for panning and for
# the zoom changing as the aircraft moves out and back, which is what
# makes an unbounded cache grow: a long session over several flights keeps
# visiting new (z,x,y) and never lets one go.
MAX_TILES_IN_MEMORY = 160
MAX_CACHE_BYTES = 80 * 1024 * 1024
TRAIL_MAX = 900                  # about 15 minutes at 1 Hz
FETCH_TIMEOUT = 6.0

# Ring distances in metres. The smallest one that contains the aircraft,
# doubled, sets the view - so the plane sits comfortably inside rather
# than on the edge.
RING_STEPS = (50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000)

# What the worker hands back for a request it dropped because the tile had
# left the view before its turn came.
_SKIPPED = object()


def deg2px(lat, lon, zoom):
    """Longitude and latitude to Web Mercator pixels at `zoom`."""
    n = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n
    return x, y


def wrap_lon(lon, near):
    """`lon` expressed as the value within 180 degrees of `near`.

    Longitude is a circle cut at the antimeridian, and every sum or
    average of two of them is wrong across that cut. An aircraft at
    -179.9 and a marker at 179.9 are 17 km apart, but averaged naively
    they place the view at 0 - half a world away, with both markers
    billions of pixels off-canvas and the map apparently empty.
    """
    return near + (((lon - near + 180.0) % 360.0) - 180.0)


def mid_lon(a, b):
    """The midpoint of two longitudes, across the antimeridian or not."""
    return ((wrap_lon(b, a) + a) / 2.0 + 180.0) % 360.0 - 180.0


def metres_per_pixel(lat, zoom):
    return 156543.03392804097 * math.cos(math.radians(lat)) / (2 ** zoom)


def distance_bearing(lat1, lon1, lat2, lon2):
    """Great-circle metres, and the initial bearing from 1 to 2.

    Point 1 is the map marker and point 2 the aircraft, so the bearing is
    where to look from the marker to see the aircraft. That is not the
    aircraft's heading, which is where its nose is going and comes from
    the GPS frame; the two agree only by coincidence.
    """
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
        # Newest first, and only what is still on screen. In flight the view
        # moves and the zoom changes faster than one worker can fetch, so
        # requests pile up. First come, first served then spends the whole
        # flight on tiles the view has already left, while the ones it
        # shows wait behind them: one INAV SITL flight saved 885 tiles and
        # put almost none of them on screen.
        self._queue = queue.LifoQueue()
        self._wanted = frozenset()
        self._lock = threading.Lock()
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

    def set_wanted(self, keys):
        """The tiles the view shows now. A queued request for any other
        is dropped when its turn comes, rather than fetched."""
        with self._lock:
            self._wanted = frozenset(keys)

    def _run(self):
        self._prune_disk()
        while True:
            try:
                key = self._queue.get(timeout=30)
            except queue.Empty:
                return
            self._work(key)

    def _work(self, key):
        """Fetch one tile to disk, or drop it if the view has moved on."""
        with self._lock:
            wanted = key in self._wanted
        if not wanted:
            self._done.put((key, None, _SKIPPED))
            return
        z, x, y = key
        try:
            blob = self._fetch(z, x, y)
            path = self._path(z, x, y)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, path)
            self._done.put((key, blob, None))
        except Exception as exc:
            self._done.put((key, None, exc))

    def _fetch(self, z, x, y):
        import urllib.request
        req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y),
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            return r.read()

    def _prune_disk(self):
        """Keep the tile folder under a size, oldest out first.

        Runs once on the worker thread rather than at start-up, so walking
        the folder never delays the window appearing. Tiles are 5-20 kB
        each, so the limit is thousands of them - a cap rather than a
        policy, and enough that a season of flying from one field never
        reaches it.
        """
        try:
            files = []
            total = 0
            for root, _dirs, names in os.walk(self.cache_dir):
                for name in names:
                    p = os.path.join(root, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            if total <= MAX_CACHE_BYTES:
                return
            files.sort()                     # oldest first
            for _mtime, size, p in files:
                if total <= MAX_CACHE_BYTES:
                    break
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass
        except Exception:
            pass                             # a cache is never worth a crash

    def _remember(self, key, img):
        """Keep the tile, and drop the least recently wanted one."""
        self.images[key] = img
        while len(self.images) > MAX_TILES_IN_MEMORY:
            # Dicts keep insertion order, so the first key is the oldest.
            # Anything still on the canvas is held by MapView._images, so
            # evicting it here does not blank the map.
            self.images.pop(next(iter(self.images)))

    def get(self, z, x, y):
        """A PhotoImage if one is to hand, else None; never blocks."""
        key = (z, x, y)
        if key in self.images:
            img = self.images.pop(key)      # pop and re-add = move to newest
            self.images[key] = img
            return img
        if key in self._failed:
            return None

        path = self._path(z, x, y)
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    img = tk.PhotoImage(data=fh.read())
                self._remember(key, img)
                return img
            except Exception:
                self._failed.add(key)
                return None

        if self.enabled and key not in self._asked:
            self._asked.add(key)
            self._queue.put(key)
        # Started whenever something is waiting, not only when this call
        # queued it. The worker leaves after 30 s idle, and a tile queued in
        # the instant it was leaving would otherwise wait for the next new
        # tile - for good, if the view never moved.
        if self.enabled and not self._queue.empty():
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
            if err is _SKIPPED:
                self._asked.discard(key)     # asked again if it comes back into view
                continue
            if blob is None:
                self._failed.add(key)
                if self.online is None:
                    self.online = False
                continue
            try:
                self._remember(key, tk.PhotoImage(data=blob))
                self.online = True
                got = True
            except Exception:
                self._failed.add(key)

    def forget_failures(self):
        """Let tiles that failed be tried again - after the internet
        arrives, say, or when the user asks for tiles a second time.

        Only the failures. These two lines used to run the other way
        round, subtracting an already-emptied set and then clearing
        _asked wholesale, which also forgot the tiles still sitting in
        the queue and asked for them a second time.
        """
        self._asked -= self._failed
        self._failed.clear()


class MapView(ttk.Frame):
    """Canvas with the tiles behind and the aircraft plot in front."""

    def __init__(self, parent, palette, cache_dir, width=430, height=330):
        super().__init__(parent)
        self.pal = palette
        self.tiles = TileStore(cache_dir)
        # Not _w/_h: Widget._w is Tkinter's own path name for this
        # widget, and overwriting it breaks every child made after.
        self._cw, self._ch = width, height

        self.origin = None           # (lat, lon) of the marker
        self.origin_alt = None       # GPS altitude at the marker
        self.pos = None              # (lat, lon)
        self.heading = 0.0
        self.sats = 0
        self.gps_alt = None          # as the GPS frame gave it - see altitude()
        self.speed = 0.0
        self.trail = deque(maxlen=TRAIL_MAX)
        self._baro_alt = None
        self._baro_t = None
        self._dist = 0.0
        self._bearing = 0.0
        self._zoom = 15
        self._ink = palette["text"]
        self._ink_soft = palette["muted"]
        self._images = []            # anchors, or Tk frees them mid-draw

        head = ttk.Frame(self)
        head.pack(fill="x")
        self.show_tiles = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text="map tiles", variable=self.show_tiles,
                        command=self._tiles_toggled).pack(side="left")
        ttk.Button(head, text="Reset marker", width=12,
                   command=self.reset_marker).pack(side="right")

        ttk.Label(self, foreground=palette["muted"], wraplength=width,
                  justify="left",
                  text=("The marker is only what this screen measures from. "
                        "The aircraft's RTL home is set by the flight "
                        "controller when it arms, and nothing here changes "
                        "it.")).pack(anchor="w", pady=(2, 0))

        self.canvas = tk.Canvas(self, width=width, height=height,
                                background=palette["field"],
                                highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True, pady=(4, 2))

        self.status = tk.StringVar(value="no GPS")
        ttk.Label(self, textvariable=self.status,
                  foreground=palette["muted"]).pack(anchor="w")
        self.draw()

    # ------------------------------------------------------------- inputs
    def reset_marker(self):
        """Measure from where the aircraft is now, and drop the old track.

        Deliberately not called "home". A flight controller's home is the
        point it returns to, set by the aircraft when it arms, and nothing
        here can move it - this marker only says where distances and
        bearings are measured from on this screen. A button that looked
        like it moved the RTL point would be worth pressing in an
        emergency, and would do nothing.
        """
        # Honoured whatever the satellite count: pressing it is the pilot
        # saying this is the point to measure from, which the automatic
        # marker has no business second-guessing.
        if self.pos:
            self._set_marker()
            self.draw()

    def _tiles_toggled(self):
        self.tiles.enabled = self.show_tiles.get()
        if self.tiles.enabled:
            self.tiles.forget_failures()
        else:
            self.tiles.set_wanted(())     # drop what is queued: nothing will show it
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
        self.gps_alt = gps.get("altitude_m")
        self.speed = gps.get("speed_kmh", 0.0) or 0.0

        # The aircraft is drawn from its first position, so there is
        # something to see while the receiver settles. The marker is not:
        # it waits for a fix good enough to measure everything else from,
        # and the track starts with it so the warm-up wander is not drawn
        # as though the aircraft had flown it.
        if self.origin is None and self.sats >= MIN_SATS_FOR_MARKER:
            self._set_marker()
        if self.origin is not None and (not self.trail or self.trail[-1] != self.pos):
            self.trail.append(self.pos)
        self.draw()

    def update_baro(self, baro):
        """Feed one baro-altitude frame. Only the readout changes, so the
        canvas is left alone - these arrive up to five times a second."""
        if not baro or baro.get("altitude_m") is None:
            return
        self._baro_alt = baro["altitude_m"]
        self._baro_t = baro.get("_t", time.monotonic())
        self._update_status()

    def altitude(self):
        """Height above home, the same way for either firmware.

        The two firmwares disagree about what the GPS frame's altitude
        means. INAV sends height above the point it armed at; ArduPilot
        sends raw GPS altitude above sea level. At a field 890 m up the
        same flight reads 75 m from one and 965 m from the other.

        Their baro frames agree, though. ArduPilot's carries
        get_nav_alt_m(ABOVE_HOME), its EKF height above home; INAV's is
        the same value it puts in its GPS frame. So that is used whenever
        one is arriving - and on ArduPilot it is also the fused figure,
        where raw GPS altitude is the noisiest thing a receiver reports.

        INAV sends a baro frame only if the aircraft has a barometer. For
        that case the GPS altitude is taken relative to the marker's.
        Subtracting cancels whichever zero the firmware used, so that is
        right for both as well: 965 - 890 and 75 - 0 are both 75.

        Deliberately not keyed off the firmware dropdown. That setting
        only decides how arming is read, and a map that depended on it
        would show a wrong altitude whenever someone forgot to change it.
        """
        if (self._baro_alt is not None and self._baro_t is not None
                and time.monotonic() - self._baro_t < BARO_FRESH_S):
            return self._baro_alt
        if self.gps_alt is not None and self.origin_alt is not None:
            return self.gps_alt - self.origin_alt
        return None

    def _set_marker(self):
        self.origin = self.pos
        self.origin_alt = self.gps_alt
        self.trail.clear()

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

        if self.pos is None and self.origin is None:
            c.create_text(w / 2, h / 2, text="waiting for a GPS position",
                          fill=self.pal["muted"])
            self.status.set("no GPS")
            return

        anchor = self.pos or self.origin
        dist = bearing = 0.0
        if self.origin and self.pos:
            dist, bearing = distance_bearing(*self.origin, *self.pos)

        centre_lat = (anchor[0] + self.origin[0]) / 2 if self.origin else anchor[0]
        centre_lon = (mid_lon(self.origin[1], anchor[1]) if self.origin
                      else anchor[1])
        self._zoom = self._pick_zoom(centre_lat, dist)

        cx, cy = deg2px(centre_lat, centre_lon, self._zoom)
        ox, oy = cx - w / 2, cy - h / 2

        def to_canvas(lat, lon):
            # Unwrapped against the centre first. Centring correctly is not
            # enough on its own: projected independently, 179.9 and -179.9
            # land at opposite ends of the world map, and the track would
            # be drawn straight across it.
            px, py = deg2px(lat, wrap_lon(lon, centre_lon), self._zoom)
            return px - ox, py - oy

        # Rings, labels and the scale bar have to read against whatever is
        # behind them, and that is either a dark empty canvas or a light
        # street map. Palette colours are picked for the first and vanish
        # on the second, so the ink follows whichever actually got drawn.
        # No tiles until the marker is set, which waits for a fix good enough
        # to measure from. Before that a receiver - or a simulator still
        # starting up - can report positions tens of kilometres apart from
        # one frame to the next. The map followed them, and asking for a
        # fresh screenful at every jump buried the tiles the flight needed
        # later: one INAV SITL session spent six minutes like that and
        # downloaded 858 tiles of open sea. The aircraft is drawn as ever.
        want_tiles = self.show_tiles.get() and self.origin is not None
        if not want_tiles:
            self.tiles.set_wanted(())
        painted = self._draw_tiles(c, ox, oy) if want_tiles else 0
        self._ink = "#16161c" if painted else self.pal["text"]
        self._ink_soft = "#45454f" if painted else self.pal["muted"]

        self._draw_rings(c, to_canvas, centre_lat)
        self._draw_trail(c, to_canvas)
        self._draw_markers(c, to_canvas)
        self._draw_scale(c, centre_lat)

        self._dist, self._bearing = dist, bearing
        self._update_status()

    def _update_status(self):
        if self.pos is None and self.origin is None:
            self.status.set("no GPS")
            return
        alt = self.altitude()
        alt_txt = f"alt {alt:.0f} m" if alt is not None else "alt —"
        if self.origin is None:
            self.status.set(
                f"{self.sats} sats   marker waits for {MIN_SATS_FOR_MARKER}   "
                f"{alt_txt}   {self.speed:.0f} km/h")
            return
        self.status.set(
            f"{self._fmt_m(self._dist)}   to plane {self._bearing:03.0f}°   "
            f"hdg {self.heading:03.0f}°   {alt_txt}   "
            f"{self.speed:.0f} km/h   {self.sats} sats")

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
        keys = [(self._zoom, tx, ty)
                for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)
                if 0 <= tx < n and 0 <= ty < n]
        self.tiles.set_wanted(keys)
        for key in keys:
            img = self.tiles.get(*key)
            if img is None:
                continue
            _z, tx, ty = key
            c.create_image(tx * TILE_SIZE - ox, ty * TILE_SIZE - oy,
                           image=img, anchor="nw")
            self._images.append(img)
            painted += 1
        return painted

    def _draw_rings(self, c, to_canvas, lat):
        if not self.origin:
            return
        hx, hy = to_canvas(*self.origin)
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
        if self.origin:
            hx, hy = to_canvas(*self.origin)
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
