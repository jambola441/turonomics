"""Generate the app icons, so they are reproducible rather than mystery binaries.

A home-screen icon is required on iOS before web push works at all, and a
notification without an icon shows the browser's own. Both wanted PNGs and this
box has no image library, so the pixels are written directly: a dark rounded
square with a "T" on it, in the palette the fleet view already uses.

Run from the repository root:

    python3 tools/make_icons.py
"""

from __future__ import annotations

import pathlib
import struct
import zlib

BACKGROUND = (0x0D, 0x0F, 0x12)
MARK = (0x3D, 0xDC, 0x97)
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "fleet" / "icons"
SIZES = (192, 512)
# Also the apple-touch-icon size iOS asks for; it scales the 192 otherwise and
# the result is soft on a retina screen.
APPLE = 180


def _rounded(x: int, y: int, size: int) -> bool:
    """Whether a pixel is inside a squircle-ish rounded square."""
    radius = size * 0.22
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _is_mark(x: int, y: int, size: int) -> bool:
    """A sans-serif T, as two rectangles."""
    unit = size / 16.0
    bar = 3.2 * unit <= y <= 5.0 * unit and 3.2 * unit <= x <= 12.8 * unit
    stem = 3.2 * unit <= y <= 12.8 * unit and 7.1 * unit <= x <= 8.9 * unit
    return bar or stem


def render(size: int) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter byte: none
        for x in range(size):
            if not _rounded(x, y, size):
                row += bytes((0, 0, 0, 0))
            elif _is_mark(x, y, size):
                row += bytes(MARK) + b"\xff"
            else:
                row += bytes(BACKGROUND) + b"\xff"
        rows.append(bytes(row))
    return _png(size, size, b"".join(rows))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, raw: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (*SIZES, APPLE):
        path = OUT / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"{path.relative_to(OUT.parent.parent.parent)} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
