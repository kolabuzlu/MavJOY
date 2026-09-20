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

from PIL import Image, ImageFilter

SOURCE = "mavjoyback.png"
ICON_PNG = "mavjoy_icon.png"
ICON_ICO = "mavjoy.ico"

# Anything at or below this is background rather than artwork. It has to sit
# above the soft grey edging around the monitor - around 50 - so that the
# edging goes with the background it was drawn against, and below the navy of
# the screen, which is artwork and must stay.
BACKGROUND_MAX = 90

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# Sizes the window itself uses, of the WHOLE logo - monitor, wordmark and
# black background, exactly as drawn. The icon is the monitor alone because
# a wordmark is illegible at 32 px, but inside the app the full lockup is
# what belongs there.
#
# They are made here rather than in the app because Tk can only resample by
# whole-number decimation - it takes every Nth pixel and throws the rest
# away. On the wordmark, whose strokes are about twenty pixels wide in a
# source reduced twentyfold, that means each stroke either survives whole or
# vanishes. These are a plain resize of the original and nothing else.
LOGO_SIZES = [48, 64, 80, 96, 128]
LOGO_PATTERN = "mavjoy_lockup_%d.png"


def scaled(art, size):
    """One size, resampled well and then given its edges back.

    Reducing by this much is a low-pass filter however good the filter is,
    and the result reads as soft next to artwork drawn at the size it is
    shown. An unsharp pass afterwards gives back the edge contrast the
    reduction took out. Small sizes lose proportionally more, so they get
    proportionally more of it back.

    The radius is kept tight. A wider one sharpens harder but rings: white
    against the red of the joystick overshoots into cyan, and a halo shows
    round the base - which at icon sizes reads as grubby rather than crisp.
    """
    out = art.resize((size, size), Image.Resampling.LANCZOS)
    percent = 140 if size <= 32 else (115 if size <= 64 else 100)
    return out.filter(ImageFilter.UnsharpMask(radius=0.6, percent=percent,
                                              threshold=0))


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

    # Square, so nothing is stretched when it is scaled down, and only just
    # bigger than the artwork. Padding is wasted at icon sizes:
    # every pixel spent on empty margin is one the monitor does not get, and
    # at 32 px there are not many to spare.
    side = int(max(art.size) * 1.02)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(art, ((side - art.width) // 2, (side - art.height) // 2))
    canvas.save(ICON_PNG)

    # Built one at a time rather than left to the ICO writer, which resamples
    # well but cannot sharpen afterwards.
    frames = [scaled(canvas, n) for n in ICO_SIZES]
    frames[-1].save(ICON_ICO, format="ICO",
                    sizes=[(n, n) for n in ICO_SIZES],
                    append_images=frames[:-1])
    print(f"wrote {ICON_PNG} at {canvas.size} and {ICON_ICO} at "
          f"{len(ICO_SIZES)} sizes")

    # The window's copies come from the source as drawn, black background
    # and all - not from the cropped artwork above, and NOT sharpened. The
    # icon is sharpened because it is reduced to 32 px and needs the help;
    # the logo in the window is the original file resized and nothing else,
    # because anything more shows as an edit of artwork nobody asked to
    # have edited.
    for n in LOGO_SIZES:
        im.resize((n, n), Image.Resampling.LANCZOS)           .convert("RGB").save(LOGO_PATTERN % n)
    print("wrote " + ", ".join(LOGO_PATTERN % n for n in LOGO_SIZES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
