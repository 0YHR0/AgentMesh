"""Generate the checked-in AgentMesh Office employee sprite sheet.

The generator uses only the Python standard library so artwork remains
deterministic and never depends on an external image-generation service.
"""

import struct
import zlib
from pathlib import Path

WIDTH = 256
HEIGHT = 32
FRAME_WIDTH = 32
OUTPUT = (
    Path(__file__).parents[1]
    / "src"
    / "agentmesh"
    / "api"
    / "console_assets"
    / "world-employee.png"
)


def color(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = value.removeprefix("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha


pixels = bytearray(WIDTH * HEIGHT * 4)


def rectangle(frame: int, x: int, y: int, width: int, height: int, fill: str) -> None:
    rgba = color(fill)
    offset_x = frame * FRAME_WIDTH
    for row in range(max(0, y), min(HEIGHT, y + height)):
        for column in range(max(0, x), min(FRAME_WIDTH, x + width)):
            index = (row * WIDTH + offset_x + column) * 4
            pixels[index : index + 4] = bytes(rgba)


def employee(frame: int, direction: str, step: int) -> None:
    lift = 1 if step else 0
    rectangle(frame, 8, 28, 17, 2, "#02060c")
    rectangle(frame, 8, 20 - lift, 8, 9, "#18263d")
    rectangle(frame, 18, 20, 7, 9 - lift, "#223451")
    rectangle(frame, 7, 14 - lift, 18, 10, "#35e7ff")
    rectangle(frame, 5, 16 - lift, 4, 7, "#1fb0c9")
    rectangle(frame, 23, 16 - lift, 4, 7, "#1fb0c9")
    rectangle(frame, 10, 5 - lift, 12, 10, "#ffc99f")
    if direction == "up":
        rectangle(frame, 8, 3 - lift, 16, 11, "#26334d")
        rectangle(frame, 10, 13 - lift, 12, 3, "#26334d")
    else:
        rectangle(frame, 8, 3 - lift, 16, 5, "#26334d")
        if direction == "down":
            rectangle(frame, 12, 10 - lift, 2, 2, "#172239")
            rectangle(frame, 18, 10 - lift, 2, 2, "#172239")
        elif direction == "right":
            rectangle(frame, 19, 10 - lift, 2, 2, "#172239")
        else:
            rectangle(frame, 11, 10 - lift, 2, 2, "#172239")


for direction_index, direction in enumerate(("down", "right", "left", "up")):
    for step in range(2):
        employee(direction_index * 2 + step, direction, step)


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


raw = b"".join(
    b"\x00" + bytes(pixels[row * WIDTH * 4 : (row + 1) * WIDTH * 4])
    for row in range(HEIGHT)
)
png = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(raw, level=9))
    + chunk(b"IEND", b"")
)
OUTPUT.write_bytes(png)
