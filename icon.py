#!/usr/bin/env python3
"""Draws the app icon into a PNG. Standard library only.

There is no Pillow, no cairo and no pip on this box, so everything is
done by hand: shapes are described as "is this point inside?" functions,
sampled at SS x SS per pixel for antialiasing, and the result is packed
into a PNG with zlib.

    ./icon.py            -> all variants, 512 px, into icons/
    ./icon.py 3          -> only variant 3
    ./icon.py sheet      -> one contact sheet with every variant
"""
import math
import pathlib
import struct
import sys
import zlib

BASE = pathlib.Path(__file__).resolve().parent
OUT = BASE / "icons"
SS = 3          # supersampling: each pixel is averaged from SS x SS samples

PAPER = (0xF7, 0xF6, 0xF4)
GREEN = (0x2F, 0x6F, 0x4E)
INK = (0x1A, 0x17, 0x14)
WHITE = (0xFF, 0xFF, 0xFF)
LIGHT = (0xE3, 0xDE, 0xD7)


# --- geometry -------------------------------------------------------------
# Every helper takes coordinates in a 0..1 square and answers "inside?".

def squircle(x, y, n=4.0):
    """Superellipse |x|^n + |y|^n <= 1 — the shape Android and iOS mask to."""
    dx, dy = abs(x - .5) * 2, abs(y - .5) * 2
    return dx ** n + dy ** n <= 1.0


def seg(x, y, x1, y1, x2, y2, w):
    """Thick line with round caps: distance from the point to the segment."""
    vx, vy = x2 - x1, y2 - y1
    px, py = x - x1, y - y1
    L = vx * vx + vy * vy
    t = 0.0 if L == 0 else max(0.0, min(1.0, (px * vx + py * vy) / L))
    dx, dy = px - vx * t, py - vy * t
    return dx * dx + dy * dy <= (w / 2) ** 2


def check(x, y, w, cx=.5, cy=.52, s=1.0):
    """The tick: short stroke down-right, long stroke up-right."""
    ax, ay = cx - .30 * s, cy + .02 * s
    bx, by = cx - .09 * s, cy + .23 * s
    ex, ey = cx + .32 * s, cy - .26 * s
    return seg(x, y, ax, ay, bx, by, w) or seg(x, y, bx, by, ex, ey, w)


def rows(x, y, ys, x1, x2, w):
    """Horizontal list lines at the given heights."""
    return any(seg(x, y, x1, yy, x2, yy, w) for yy in ys)


# --- variants -------------------------------------------------------------
# Each returns a colour for a point, or None for "transparent".

def v1(x, y):
    """Green tile, white tick. Plain and readable at 48 px."""
    if not squircle(x, y):
        return None
    return WHITE if check(x, y, .115) else GREEN


def v2(x, y):
    """Paper tile, green tick — matches the page background."""
    if not squircle(x, y):
        return None
    return GREEN if check(x, y, .115) else PAPER


def v3(x, y):
    """Checklist: three lines, the top one ticked off."""
    if not squircle(x, y):
        return None
    if check(x, y, .072, cx=.325, cy=.325, s=.56):
        return WHITE
    if rows(x, y, (.325,), .575, .755, .060):
        return WHITE
    if rows(x, y, (.535, .715), .255, .755, .060):
        return (0x8F, 0xB6, 0xA2)     # unfinished lines: dimmer green
    return GREEN


def v4(x, y):
    """Dark tile, green tick — quiet on a light wallpaper."""
    if not squircle(x, y):
        return None
    return GREEN if check(x, y, .115) else INK


def v5(x, y):
    """Green tick alone inside a ring on paper."""
    if not squircle(x, y):
        return None
    d = math.hypot(x - .5, y - .5)
    if .345 < d <= .373:
        return LIGHT
    return GREEN if check(x, y, .095, s=.72) else PAPER


VARIANTS = {"1": v1, "2": v2, "3": v3, "4": v4, "5": v5}


# --- rendering ------------------------------------------------------------

def render(fn, size, pad=0.0):
    """RGBA bytes, row by row. pad shrinks the art inside the canvas."""
    px = bytearray()
    step = 1.0 / (size * SS)
    scale = 1.0 - 2 * pad
    for row in range(size):
        line = bytearray()
        for col in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = (col * SS + sx + .5) * step
                    y = (row * SS + sy + .5) * step
                    c = fn((x - pad) / scale, (y - pad) / scale) if scale else None
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 255
            n = SS * SS
            if a:
                # Colour is averaged over covered samples only, so edges
                # keep their hue instead of fading towards black.
                k = a // 255
                line += bytes((r // k, g // k, b // k, a // n))
            else:
                line += b"\0\0\0\0"
        px += line
    return bytes(px)


def png(path, pixels, size):
    """Minimal PNG writer: signature, IHDR, IDAT, IEND."""
    raw = bytearray()
    stride = size * 4
    for row in range(size):
        raw += b"\0"                     # filter type 0 for every scanline
        raw += pixels[row * stride:(row + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    head = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", head)
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                     + chunk(b"IEND", b""))


def sheet(size=232, gap=26):
    """All variants side by side on a mid-grey field, for picking one."""
    keys = sorted(VARIANTS)
    cols = 3
    rows_n = (len(keys) + cols - 1) // cols
    w = cols * size + (cols + 1) * gap
    h = rows_n * size + (rows_n + 1) * gap
    canvas = bytearray()
    for _ in range(w * h):
        canvas += bytes((0x9A, 0x9A, 0x9E, 255))
    for i, key in enumerate(keys):
        art = render(VARIANTS[key], size)
        ox = gap + (i % cols) * (size + gap)
        oy = gap + (i // cols) * (size + gap)
        for row in range(size):
            for col in range(size):
                s = (row * size + col) * 4
                a = art[s + 3]
                if not a:
                    continue
                d = ((oy + row) * w + ox + col) * 4
                for c in range(3):        # blend the antialiased edge
                    under = canvas[d + c]
                    canvas[d + c] = (art[s + c] * a + under * (255 - a)) // 255
    raw = bytearray()
    for row in range(h):
        raw += b"\0" + canvas[row * w * 4:(row + 1) * w * 4]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    (OUT / "sheet.png").write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b""))
    print("icons/sheet.png")


def build(key):
    """The three files a manifest needs, from one variant."""
    fn = VARIANTS[key]
    for size in (192, 512):
        png(OUT / f"icon-{size}.png", render(fn, size), size)
        print(f"icons/icon-{size}.png")
    # Maskable: Android crops to its own shape, so the art must fill the
    # square edge to edge and stay inside the middle 80%.
    def full(x, y):
        c = fn(x, y)
        return c or ((GREEN if key in ("1", "3") else
                      INK if key == "4" else PAPER))
    png(OUT / "icon-mask.png", render(full, 512, pad=.10), 512)
    print("icons/icon-mask.png")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    arg = sys.argv[1] if len(sys.argv) > 1 else "sheet"
    if arg == "sheet":
        sheet()
    elif arg in VARIANTS:
        build(arg)
    else:
        sys.exit(f"unknown variant: {arg}")
