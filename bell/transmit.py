"""Precisely paced multicast RTP transmitter."""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bell.audio import load_frames, transcode
from bell.wire import get_wire_format
from bell.wire.base import StreamState, WireFormat

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DestinationEndpoint:
    name: str
    group: str
    port: int
    ttl: int = 1


@dataclass(frozen=True, slots=True)
class TransmitResult:
    packet_count: int
    duration: float
    max_late_seconds: float
    mean_abs_drift_seconds: float
    destination_bytes: dict[str, int]
    destination_errors: dict[str, str]
    cancelled: bool = False


@dataclass(slots=True)
class _Target:
    endpoint: DestinationEndpoint
    sock: socket.socket | None
    state: StreamState
    error: str | None = None


class Transmitter:
    def __init__(
        self,
        wire_format: WireFormat,
        destinations: Sequence[DestinationEndpoint | tuple[str, int]],
        interface_ip: str = "0.0.0.0",
        *,
        loopback: bool = False,
        dry_run: bool = False,
        socket_factory: type[socket.socket] = socket.socket,
    ) -> None:
        self.wire_format = wire_format
        self.interface_ip = interface_ip
        self.dry_run = dry_run
        self._targets: list[_Target] = []
        for index, item in enumerate(destinations):
            endpoint = (
                item
                if isinstance(item, DestinationEndpoint)
                else DestinationEndpoint(f"destination-{index + 1}", item[0], item[1])
            )
            sock: socket.socket | None = None
            if not dry_run:
                sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, endpoint.ttl)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, int(loopback))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
            self._targets.append(_Target(endpoint, sock, StreamState()))

    def close(self) -> None:
        for target in self._targets:
            if target.sock is not None:
                target.sock.close()

    def __enter__(self) -> Transmitter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(
        self,
        frames: Iterable[bytes],
        channel: int,
        cancel_event: threading.Event | None = None,
    ) -> TransmitResult:
        start = time.monotonic()
        packet_count = 0
        drift_values: list[float] = []
        byte_counts = {target.endpoint.name: 0 for target in self._targets}
        cancelled = False
        for index, frame in enumerate(frames):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            target_time = start + index * 0.020
            remaining = target_time - time.monotonic()
            if remaining > 0:
                if cancel_event is not None:
                    if cancel_event.wait(remaining):
                        cancelled = True
                        break
                else:
                    time.sleep(remaining)
            drift = time.monotonic() - target_time
            drift_values.append(drift)
            if drift > 0.100:
                LOGGER.warning("transmit_pacing_late", extra={"drift_seconds": drift, "packet": index})
            for target in self._targets:
                if target.error is not None:
                    continue
                seq, timestamp, ssrc = target.state.next()
                packet = self.wire_format.build_packet(
                    frame, seq, timestamp, ssrc, index == 0, channel
                )
                if target.sock is not None:
                    try:
                        sent = target.sock.sendto(
                            packet, (target.endpoint.group, target.endpoint.port)
                        )
                        if sent != len(packet):
                            raise OSError(f"short UDP send: {sent} of {len(packet)} bytes")
                    except OSError as exc:
                        target.error = str(exc)
                        LOGGER.error(
                            "destination_send_failed",
                            extra={"destination": target.endpoint.name, "detail": str(exc)},
                        )
                        continue
                byte_counts[target.endpoint.name] += len(packet)
            packet_count += 1
        duration = time.monotonic() - start
        return TransmitResult(
            packet_count=packet_count,
            duration=duration,
            max_late_seconds=max(drift_values, default=0.0),
            mean_abs_drift_seconds=(
                sum(abs(value) for value in drift_values) / len(drift_values)
                if drift_values
                else 0.0
            ),
            destination_bytes=byte_counts,
            destination_errors={
                target.endpoint.name: target.error
                for target in self._targets
                if target.error is not None
            },
            cancelled=cancelled,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--channel", required=True, type=int)
    parser.add_argument("--iface", default="0.0.0.0")
    parser.add_argument("--format", default="poly_group_page", choices=("plain_rtp", "poly_group_page"))
    parser.add_argument("--group", default="239.255.255.255")
    parser.add_argument("--port", default=601, type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    raw = transcode(args.file)
    endpoint = DestinationEndpoint("all", args.group, args.port)
    with Transmitter(
        get_wire_format(args.format), [endpoint], args.iface, dry_run=args.dry_run
    ) as transmitter:
        result = transmitter.send(load_frames(raw), args.channel)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
