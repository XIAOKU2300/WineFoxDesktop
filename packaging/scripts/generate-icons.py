#!/usr/bin/env python3
"""Generate WineFox app icons (stdlib only): PNGs + multi-size ICO.

Writes into desktop_shell/src-tauri/icons/:
  icon.png, 32x32.png, 128x128.png, 128x128@2x.png, icon.ico

Design: rounded-square gold gradient with a dark "W".
Run:  python packaging/scripts/generate-icons.py
"""

from __future__ import annotations

import os
import struct
import zlib

ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
ICON_DIR = os.path.join(ROOT, "desktop_shell", "src-tauri", "icons")

GOLD_TOP = (0xE8, 0xC4, 0x7A)
GOLD_BOTTOM = (0xB9, 0x8A, 0x3E)
INK = (0x10, 0x13, 0x1C)


def make_rgba(size: int) -> bytearray:
    """Rounded-square gradient + 'W' glyph, RGBA bytes."""
    px = bytearray(size * size * 4)
    r = max(1.0, size * 0.22)
    sw = max(2.0, size * 0.075)
    # W glyph as 4 line segments in normalized coords.
    segs = [
        (0.27, 0.68, 0.36, 0.33),
        (0.36, 0.33, 0.50, 0.58),
        (0.50, 0.58, 0.64, 0.33),
        (0.64, 0.33, 0.73, 0.68),
    ]

    def dist_seg(x, y, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        cx, cy = x1 + t * dx, y1 + t * dy
        return ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5

    for y in range(size):
        for x in range(size):
            # rounded rect mask
            nx, ny = x / size, y / size
            dx = max(abs(nx - 0.5) * size - (size / 2 - r), 0.0)
            dy = max(abs(ny - 0.5) * size - (size / 2 - r), 0.0)
            if dx * dx + dy * dy > r * r:
                continue
            # vertical gradient
            t = ny
            cr = int(GOLD_TOP[0] + (GOLD_BOTTOM[0] - GOLD_TOP[0]) * t)
            cg = int(GOLD_TOP[1] + (GOLD_BOTTOM[1] - GOLD_TOP[1]) * t)
            cb = int(GOLD_TOP[2] + (GOLD_BOTTOM[2] - GOLD_TOP[2]) * t)
            # glyph: min distance to any segment
            d = min(dist_seg(nx, ny, *s) for s in segs)
            if d <= sw / size:
                cr, cg, cb = INK
            i = (y * size + x) * 4
            px[i] = cr
            px[i + 1] = cg
            px[i + 2] = cb
            px[i + 3] = 255
    return px


def encode_png(size: int, rgba: bytearray) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b""
    for y in range(size):
        raw += b"\x00" + bytes(rgba[y * size * 4 : (y + 1) * size * 4])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def encode_bmp_entry(size: int, rgba: bytearray) -> bytes:
    """Uncompressed 32bpp BGRA bitmap for ICO entries < 256px."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):  # bottom-up
        for x in range(size):
            i = (y * size + x) * 4
            xor += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))
    mask_size = ((size + 31) // 32) * 4 * size
    mask = bytearray(mask_size)
    return header + bytes(xor) + bytes(mask)


def encode_ico(sizes: list[int], pngs: dict[int, bytes], bmps: dict[int, bytes]) -> bytes:
    entries = []
    blobs = []
    for s in sizes:
        data = pngs.get(s) or bmps.get(s)
        if data is None:
            continue
        width = 0 if s == 256 else s
        height = 0 if s == 256 else s
        entries.append(struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(data), 6 + 16 * len(sizes) + sum(len(b) for b in blobs)))
        blobs.append(data)
    header = struct.pack("<HHH", 0, 1, len(entries))
    body = b"".join(blobs)
    out = header + b"".join(entries) + body
    # fix offsets
    off = 6 + 16 * len(entries)
    entries2 = []
    pos = 0
    for s in sizes:
        data = pngs.get(s) or bmps.get(s)
        if data is None:
            continue
        width = 0 if s == 256 else s
        height = 0 if s == 256 else s
        entries2.append(struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(data), off + pos))
        pos += len(data)
    return header + b"".join(entries2) + body


def main() -> None:
    os.makedirs(ICON_DIR, exist_ok=True)
    rgba256 = make_rgba(256)
    pngs = {s: encode_png(s, make_rgba(s)) for s in (16, 32, 48, 128, 256, 512)}
    bmps = {s: encode_bmp_entry(s, make_rgba(s)) for s in (16, 32, 48)}
    # Classic BMP entries for small sizes + PNG for 256 (Vista+).
    ico = encode_ico([16, 32, 48, 256], {256: pngs[256]}, bmps)
    with open(os.path.join(ICON_DIR, "icon.ico"), "wb") as f:
        f.write(ico)
    with open(os.path.join(ICON_DIR, "icon.png"), "wb") as f:
        f.write(pngs[512])
    with open(os.path.join(ICON_DIR, "32x32.png"), "wb") as f:
        f.write(pngs[32])
    with open(os.path.join(ICON_DIR, "128x128.png"), "wb") as f:
        f.write(pngs[128])
    with open(os.path.join(ICON_DIR, "128x128@2x.png"), "wb") as f:
        f.write(pngs[256])
    print("icons written to", ICON_DIR)


if __name__ == "__main__":
    main()
