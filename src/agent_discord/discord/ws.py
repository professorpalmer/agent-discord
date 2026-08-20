"""Minimal masked WebSocket client (RFC 6455). Stdlib only."""

from __future__ import annotations

import base64
import os
import select
import socket
import ssl
from typing import Optional
from urllib.parse import urlparse


class WebSocketError(RuntimeError):
    """Handshake or framing failed."""


def encode_frame(payload: bytes, *, opcode: int = 1) -> bytes:
    """Client-to-server frame. Always masked."""

    mask = os.urandom(4)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def decode_frame(buffer: bytearray) -> Optional[tuple[int, bytes]]:
    """Pop one complete frame. Returns (opcode, payload) or None if incomplete."""

    if len(buffer) < 2:
        return None
    opcode = buffer[0] & 0x0F
    masked = bool(buffer[1] & 0x80)
    length = buffer[1] & 0x7F
    index = 2
    if length == 126:
        if len(buffer) < 4:
            return None
        length = int.from_bytes(buffer[2:4], "big")
        index = 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length = int.from_bytes(buffer[2:10], "big")
        index = 10
    if masked:
        if len(buffer) < index + 4 + length:
            return None
        mask = bytes(buffer[index : index + 4])
        index += 4
        raw = bytes(buffer[index : index + length])
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
    else:
        if len(buffer) < index + length:
            return None
        payload = bytes(buffer[index : index + length])
    del buffer[: index + length]
    return opcode, payload


class WebSocketClient:
    """Blocking text WebSocket over TLS. Close is best-effort."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = bytearray()

    @classmethod
    def connect(cls, url: str, *, timeout: float = 30.0) -> "WebSocketClient":
        parsed = urlparse(url)
        if parsed.scheme not in {"wss", "ws"}:
            raise WebSocketError(f"unsupported websocket scheme {parsed.scheme!r}")
        host = parsed.hostname or ""
        if not host:
            raise WebSocketError("websocket URL missing host")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        raw = socket.create_connection((host, port), timeout=timeout)
        sock: socket.socket = raw
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("websocket handshake closed")
            header += chunk
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise WebSocketError(f"websocket handshake failed: {status!r}")
        leftover = header.split(b"\r\n\r\n", 1)[1]
        client = cls(sock)
        client._buffer.extend(leftover)
        sock.settimeout(None)
        return client

    def send_text(self, text: str) -> None:
        self._sock.sendall(encode_frame(text.encode("utf-8"), opcode=1))

    def send_pong(self, payload: bytes = b"") -> None:
        self._sock.sendall(encode_frame(payload, opcode=10))

    def send_close(self) -> None:
        try:
            self._sock.sendall(encode_frame(b"", opcode=8))
        except OSError:
            pass

    def recv_text(self, *, timeout: float) -> Optional[str]:
        """Return next text payload, handle ping, or None on timeout."""

        deadline = timeout
        while True:
            frame = decode_frame(self._buffer)
            if frame is None:
                ready, _, _ = select.select([self._sock], [], [], max(0.0, deadline))
                if not ready:
                    return None
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise WebSocketError("websocket closed")
                self._buffer.extend(chunk)
                continue
            opcode, payload = frame
            if opcode == 8:
                raise WebSocketError("websocket closed")
            if opcode == 9:
                self.send_pong(payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                return payload.decode("utf-8")
            if opcode == 2:
                raise WebSocketError("binary websocket frames are not used")

    def close(self) -> None:
        self.send_close()
        try:
            self._sock.close()
        except OSError:
            pass
