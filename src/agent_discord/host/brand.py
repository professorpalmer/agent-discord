"""Local Discord OS mark. No emoji. Best-effort avatar upload."""

from __future__ import annotations

import struct
import zlib


def default_icon_png(*, size: int = 128) -> bytes:
    width = max(32, min(int(size), 512))
    pixels = bytearray()
    bar = max(8, width // 10)
    for _y in range(width):
        for x in range(width):
            if x < bar:
                pixels.extend((194, 124, 14, 255))
            else:
                pixels.extend((43, 45, 49, 255))
    return _png_rgba(width, width, bytes(pixels))


def apply_bot_avatar(token: str, *, opener=None) -> bool:
    if not token.strip():
        return False
    try:
        from agent_discord.discord.rest import patch_bot_avatar

        patch_bot_avatar(token=token, png_bytes=default_icon_png(), opener=opener)
    except Exception:
        return False
    return True


def _png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    raw = b""
    stride = width * 4
    for row in range(height):
        raw += b"\x00" + pixels[row * stride : (row + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
