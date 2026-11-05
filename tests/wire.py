"""Build protobuf messages by hand, using the v2 field numbers."""

from __future__ import annotations

import struct


def varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def int_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def double(number: int, value: float) -> bytes:
    return varint((number << 3) | 1) + struct.pack("<d", value)


def msg(number: int, payload: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(payload)) + payload


def text(number: int, value: str) -> bytes:
    return msg(number, value.encode())


def timestamp(number: int, seconds: int) -> bytes:
    return msg(number, int_field(1, seconds))


def frame(payload: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def trailer(status: str, message: str = "") -> bytes:
    lines = f"grpc-status: {status}\r\n"
    if message:
        lines += f"grpc-message: {message}\r\n"
    data = lines.encode()
    return b"\x80" + struct.pack(">I", len(data)) + data
