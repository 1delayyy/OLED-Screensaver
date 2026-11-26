"""Creates oled.ico (a white crescent moon on a dark rounded square) using only the standard library.
build_exe.bat runs this before building the .exe."""
import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)
BACKGROUND = (16, 16, 18)
MOON = (236, 236, 242)


def coverage(x, y, size):
    """(background coverage, moon coverage) for one pixel, using 3x3 sub-samples for smooth edges."""
    bg = moon = 0
    radius = size * 0.22                                   # corner radius of the rounded square
    cx, cy, r1 = size * 0.46, size * 0.52, size * 0.30     # the full moon
    bx, by, r2 = size * 0.60, size * 0.42, size * 0.27     # the circle that bites the crescent out of it
    for sy in range(3):
        for sx in range(3):
            px, py = x + (sx + 0.5) / 3, y + (sy + 0.5) / 3
            qx, qy = min(px, size - px), min(py, size - py)
            inside = True
            if qx < radius and qy < radius:                # rounded corner
                inside = (radius - qx) ** 2 + (radius - qy) ** 2 <= radius ** 2
            if not inside:
                continue
            bg += 1
            in_moon = (px - cx) ** 2 + (py - cy) ** 2 <= r1 ** 2
            bitten = (px - bx) ** 2 + (py - by) ** 2 <= r2 ** 2
            if in_moon and not bitten:
                moon += 1
    return bg / 9, moon / 9


def png_bytes(size):
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter type 0 (none)
        for x in range(size):
            bg, moon = coverage(x, y, size)
            if bg == 0:
                row += bytes((0, 0, 0, 0))
                continue
            blend = moon / bg                              # how much of the covered area is moon
            colour = [round(BACKGROUND[i] * (1 - blend) + MOON[i] * blend) for i in range(3)]
            row += bytes(colour + [round(255 * bg)])
        rows.append(bytes(row))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def build_ico(path):
    images = [(size, png_bytes(size)) for size in SIZES]
    offset = 6 + 16 * len(images)
    directory, blobs = b"", b""
    for size, data in images:
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    Path(path).write_bytes(struct.pack("<HHH", 0, 1, len(images)) + directory + blobs)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "oled.ico"
    build_ico(target)
    print(f"Created {target} ({target.stat().st_size} bytes)")
