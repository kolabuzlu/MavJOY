#!/usr/bin/env python3
"""Build the application icon from the logo.

mavjoyback.png is the full lockup: the monitor, the MavJOY wordmark beneath
it, and a black background. An icon is a small square, where a wordmark is
illegible and only wastes the room the picture needs, so this takes the
monitor alone - the same shape MavGCS uses - on transparency.

    python make_icon.py

Writes mavjoy_icon.png (the cropped, keyed artwork, used in the window too)
and mavjoy.ico (the same at seven sizes, for Explorer and the taskbar).
"""

from __future__ import annotations

import sys
from collections import deque

from PIL import Image

SOURCE = "mavjoyback.png"
ICON_PNG = "mavjoy_icon.png"
ICON_ICO = "mavjoy.ico"

# Anything at or below this is background rather than artwork. It has to sit
# above the soft grey edging around the monitor - around 50 - so that the
# edging goes with the background it was drawn against, and below the navy of
# the screen, which is artwork and must stay.
BACKGROUND_MAX = 90

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48),
             (64, 64), (128, 128), (256, 256)]


def content_bands(im, threshold=30):
    """The y ranges that hold anything, top to bottom."""
    w, h = im.size
    px = im.convert("RGB").load()
    bands, start = [], None
    for y in range(h):
        filled = any(max(px[x, y]) > threshold for x in range(0, w, 3))
        if filled and start is None:
            start = y
        elif not filled and start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, h - 1))
    return bands


def key_background(im):
    """Clear the background, and only the background.

    Flood filled inward from the edges rather than thresholded, because the
    screen of the monitor is a dark navy that a threshold dark enough to
    catch the surround would punch straight through. Fill cannot reach it:
    the white bezel encloses it completely. The soft edging around the
    monitor goes too, which is the point - it is shading drawn for a black
    background that is no longer there, and left behind it reads as dirt.
    """
    w, h = im.size
    px = im.load()
    seen = bytearray(w * h)
    queue = deque()

    def consider(x, y):
        if 0 <= x < w and 0 <= y < h and not seen[y * w + x]:
            seen[y * w + x] = 1
            if max(px[x, y][:3]) <= BACKGROUND_MAX:
                queue.append((x, y))
                return True
        return False

    for x in range(w):
        consider(x, 0)
        consider(x, h - 1)
    for y in range(h):
        consider(0, y)
        consider(w - 1, y)

    cleared = 0
    while queue:
        x, y = queue.popleft()
        px[x, y] = (0, 0, 0, 0)
        cleared += 1
        consider(x + 1, y)
        consider(x - 1, y)
        consider(x, y + 1)
        consider(x, y - 1)
    return cleared


def main():
    im = Image.open(SOURCE).convert("RGBA")
    bands = content_bands(im)
    if not bands:
        print(f"{SOURCE} looks empty")
        return 1
    top, bottom = bands[0]          # the monitor; the wordmark is below it
    print(f"content bands: {bands} -> taking {top}-{bottom}")

    band = im.crop((0, top, im.width, bottom + 1))
    box = band.convert("RGB").point(lambda v: 255 if v > 30 else 0) \
              .convert("L").getbbox()
    art = band.crop(box)
    print(f"monitor cropped to {art.size}")

    print(f"background cleared: {key_background(art)} px")

    # Square, so nothing is stretched when it is scaled down, with a little
    # room around it - an icon pressed against its own edges looks wrong at
    # every size.
    side = int(max(art.size) * 1.06)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(art, ((side - art.width) // 2, (side - art.height) // 2))
    canvas.save(ICON_PNG)
    canvas.save(ICON_ICO, format="ICO", sizes=ICO_SIZES)
    print(f"wrote {ICON_PNG} at {canvas.size} and {ICON_ICO} at "
          f"{len(ICO_SIZES)} sizes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
