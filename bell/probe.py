"""Capture and analyze RTP/Poly Group Page packets."""

from __future__ import annotations

import argparse
import socket
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from bell.wire.poly_group_page import POLY_ALERT, POLY_END, POLY_TRANSMIT


@dataclass(frozen=True, slots=True)
class RTPPacket:
    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    header_length: int
    payload: bytes
    extension_profile: int | None = None
    extension_words: int | None = None
    extension_data: bytes = b""


def parse_rtp(packet: bytes) -> RTPPacket:
    if len(packet) < 12:
        raise ValueError("packet is shorter than the 12-byte RTP header")
    first, second, sequence, timestamp, ssrc = struct.unpack("!BBHII", packet[:12])
    csrc_count = first & 0x0F
    offset = 12 + csrc_count * 4
    if len(packet) < offset:
        raise ValueError("packet has a truncated CSRC list")
    extension = bool(first & 0x10)
    profile: int | None = None
    words: int | None = None
    extension_data = b""
    if extension:
        if len(packet) < offset + 4:
            raise ValueError("packet has a truncated RTP extension header")
        profile, words = struct.unpack("!HH", packet[offset : offset + 4])
        extension_end = offset + 4 + words * 4
        if len(packet) < extension_end:
            raise ValueError("packet has truncated RTP extension data")
        extension_data = packet[offset + 4 : extension_end]
        offset = extension_end
    payload_end = len(packet)
    if first & 0x20:
        padding = packet[-1]
        if padding == 0 or padding > len(packet) - offset:
            raise ValueError("invalid RTP padding")
        payload_end -= padding
    return RTPPacket(
        version=first >> 6,
        padding=bool(first & 0x20),
        extension=extension,
        csrc_count=csrc_count,
        marker=bool(second & 0x80),
        payload_type=second & 0x7F,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        header_length=offset,
        payload=packet[offset:payload_end],
        extension_profile=profile,
        extension_words=words,
        extension_data=extension_data,
    )


def hexdump(data: bytes, limit: int = 48) -> str:
    return " ".join(f"{byte:02x}" for byte in data[:limit])


def save_capture(path: Path, packets: Sequence[bytes]) -> None:
    with path.open("wb") as handle:
        for packet in packets:
            handle.write(struct.pack("!I", len(packet)))
            handle.write(packet)


def load_capture(path: Path) -> list[bytes]:
    packets: list[bytes] = []
    with path.open("rb") as handle:
        while length_data := handle.read(4):
            if len(length_data) != 4:
                raise ValueError(f"truncated packet length in {path}")
            length = struct.unpack("!I", length_data)[0]
            packet = handle.read(length)
            if len(packet) != length:
                raise ValueError(f"truncated packet in {path}")
            packets.append(packet)
    return packets


def compare_packets(left: bytes, right: bytes) -> list[tuple[int, int | None, int | None]]:
    ignored = set(range(2, 12))  # sequence, timestamp and SSRC
    differences: list[tuple[int, int | None, int | None]] = []
    try:
        comparison_length = max(parse_rtp(left).header_length, parse_rtp(right).header_length)
    except ValueError:
        comparison_length = max(len(left), len(right))
    for offset in range(comparison_length):
        if offset in ignored:
            continue
        a = left[offset] if offset < len(left) else None
        b = right[offset] if offset < len(right) else None
        if a != b:
            differences.append((offset, a, b))
    return differences


def join_socket(group: str, port: int, interface: str, timeout: float = 10.0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    membership = socket.inet_aton(group) + socket.inet_aton(interface)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.settimeout(timeout)
    return sock


def capture(
    group: str,
    port: int,
    interface: str,
    count: int,
    timeout: float = 10.0,
    transform: Callable[[bytes], bytes | None] | None = None,
) -> tuple[list[bytes], list[float]]:
    packets: list[bytes] = []
    arrivals: list[float] = []
    deadline = time.monotonic() + timeout
    with join_socket(group, port, interface, timeout) as sock:
        while len(packets) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("capture window expired")
            sock.settimeout(remaining)
            packet, _address = sock.recvfrom(65535)
            retained = transform(packet) if transform is not None else packet
            if retained is None:
                continue
            arrivals.append(time.monotonic())
            packets.append(retained)
    return packets, arrivals


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="239.255.255.255")
    parser.add_argument("--port", type=int, default=601)
    parser.add_argument("--iface", help="local IPv4 interface for multicast membership")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--save", type=Path)
    parser.add_argument("--compare", nargs=2, metavar=("A.bin", "B.bin"), type=Path)
    args = parser.parse_args(argv)
    if args.compare:
        left_packets, right_packets = load_capture(args.compare[0]), load_capture(args.compare[1])
        if not left_packets or not right_packets:
            parser.error("both captures must contain at least one packet")
        differences = compare_packets(left_packets[0], right_packets[0])
        print("offset  capture-A  capture-B")
        for offset, left, right in differences:
            print(f"{offset:6d}  {left if left is not None else '--':>9}  {right if right is not None else '--':>9}")
        return 0
    if not args.iface:
        parser.error("--iface is required when capturing")
    packets, arrivals = capture(args.group, args.port, args.iface, args.count)
    for index, packet in enumerate(packets, 1):
        if (
            len(packet) >= 20
            and packet[0] in {POLY_ALERT, POLY_TRANSMIT, POLY_END}
            and 26 <= packet[1] <= 50
            and packet[6] == 13
        ):
            opcode_names = {POLY_ALERT: "alert", POLY_TRANSMIT: "transmit", POLY_END: "end"}
            detail = (
                f" codec={packet[20]} sample_count={int.from_bytes(packet[22:26], 'big')} "
                f"audio_bytes={len(packet) - 26}"
                if packet[0] == POLY_TRANSMIT and len(packet) >= 26
                else ""
            )
            print(
                f"packet {index}: Poly Page {opcode_names[packet[0]]} "
                f"group={packet[1] - 25} encoded_channel={packet[1]}{detail}"
            )
            continue
        parsed = parse_rtp(packet)
        print(f"packet {index}: {hexdump(packet)}")
        print(
            f"  RTP v={parsed.version} P={int(parsed.padding)} X={int(parsed.extension)} "
            f"CC={parsed.csrc_count} M={int(parsed.marker)} PT={parsed.payload_type} "
            f"seq={parsed.sequence} ts={parsed.timestamp} ssrc={parsed.ssrc:#010x}"
        )
        if parsed.extension:
            print(
                f"  EXT profile={parsed.extension_profile:#06x} words={parsed.extension_words} "
                f"likely-channel-data=[{hexdump(parsed.extension_data, len(parsed.extension_data))}]"
            )
        print(f"  payload={len(parsed.payload)} bytes codec={'PCMU' if parsed.payload_type == 0 else 'unknown'}")
    if len(arrivals) > 1:
        deltas = [(b - a) * 1000 for a, b in pairwise(arrivals)]
        print(f"observed ptime: {sum(deltas) / len(deltas):.2f} ms average")
    if args.save:
        save_capture(args.save, packets)
        print(f"saved {len(packets)} packets to {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
