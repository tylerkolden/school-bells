"""Listen for multicast RTP/PCMU and write a playable WAV file."""

from __future__ import annotations

import argparse
import socket
import struct
import time
import wave
from collections.abc import Sequence
from pathlib import Path


def _join_socket(group: str, port: int, interface: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    membership = socket.inet_aton(group) + socket.inet_aton(interface)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.settimeout(timeout)
    return sock


def _pcmu_payload(packet: bytes) -> bytes | None:
    if len(packet) < 12:
        return None
    first, second = packet[0], packet[1]
    if first >> 6 != 2 or second & 0x7F != 0:
        return None
    offset = 12 + (first & 0x0F) * 4
    if len(packet) < offset:
        return None
    if first & 0x10:
        if len(packet) < offset + 4:
            return None
        words = struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
        offset += 4 + words * 4
    if len(packet) < offset:
        return None
    end = len(packet)
    if first & 0x20:
        padding = packet[-1]
        if padding == 0 or padding > end - offset:
            return None
        end -= padding
    return packet[offset:end]


def _ulaw_to_pcm16(payload: bytes) -> bytes:
    """Decode G.711 mu-law without third-party packages or deprecated ``audioop``."""
    samples: list[int] = []
    for encoded in payload:
        value = (~encoded) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        samples.append(-sample if sign else sample)
    return struct.pack(f"<{len(samples)}h", *samples)


def listen(group: str, port: int, interface: str, output: Path, seconds: float) -> int:
    deadline = time.monotonic() + seconds
    packets = 0
    with wave.open(str(output), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        with _join_socket(group, port, interface, min(2.0, seconds)) as sock:
            while time.monotonic() < deadline:
                try:
                    packet, _address = sock.recvfrom(65535)
                except TimeoutError:
                    continue
                payload = _pcmu_payload(packet)
                if payload is None:
                    continue
                wav_file.writeframes(_ulaw_to_pcm16(payload))
                packets += 1
    return packets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="239.255.255.255")
    parser.add_argument("--port", type=int, default=601)
    parser.add_argument("--iface", required=True)
    parser.add_argument("--output", type=Path, default=Path("capture.wav"))
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    count = listen(args.group, args.port, args.iface, args.output, args.seconds)
    print(f"wrote {count} PCMU packets to {args.output}")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
