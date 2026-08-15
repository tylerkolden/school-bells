from __future__ import annotations

import socket
import threading
import time
from itertools import pairwise

from bell.probe import parse_rtp
from bell.transmit import DestinationEndpoint, Transmitter
from bell.wire.plain_rtp import PlainRTP


def test_loopback_payload_counters_marker_and_pacing() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(3)
    destination = DestinationEndpoint("test", "127.0.0.1", listener.getsockname()[1])
    frames = [bytes([index]) * 160 for index in range(100)]
    received: list[tuple[bytes, float]] = []

    def receive() -> None:
        while len(received) < len(frames):
            packet, _ = listener.recvfrom(2048)
            received.append((packet, time.monotonic()))

    thread = threading.Thread(target=receive)
    thread.start()
    with Transmitter(PlainRTP(), [destination], "127.0.0.1", loopback=True) as transmitter:
        result = transmitter.send(frames, 0)
    thread.join(timeout=4)
    listener.close()
    assert result.packet_count == len(frames)
    assert result.destination_errors == {}
    assert len(received) == len(frames)
    parsed = [parse_rtp(packet) for packet, _ in received]
    assert [item.payload for item in parsed] == frames
    assert parsed[0].marker and not any(item.marker for item in parsed[1:])
    assert all((b.sequence - a.sequence) & 0xFFFF == 1 for a, b in pairwise(parsed))
    assert all((b.timestamp - a.timestamp) & 0xFFFFFFFF == 160 for a, b in pairwise(parsed))
    intervals = [b[1] - a[1] for a, b in pairwise(received)]
    assert abs(sum(intervals) / len(intervals) - 0.020) <= 0.003


def test_one_failed_destination_does_not_stop_healthy_peer() -> None:
    class FakeSocket:
        created = 0

        def __init__(self, *_args) -> None:
            self.number = FakeSocket.created
            FakeSocket.created += 1

        def setsockopt(self, *_args) -> None:
            return None

        def sendto(self, packet, _address) -> int:
            if self.number == 0:
                raise OSError("simulated interface failure")
            return len(packet)

        def close(self) -> None:
            return None

    destinations = [
        DestinationEndpoint("failed", "239.1.1.1", 5000),
        DestinationEndpoint("healthy", "239.1.1.2", 5000),
    ]
    with Transmitter(
        PlainRTP(), destinations, "127.0.0.1", socket_factory=FakeSocket
    ) as transmitter:
        result = transmitter.send([b"a" * 160, b"b" * 160], 0)
    assert result.destination_errors == {"failed": "simulated interface failure"}
    assert result.destination_bytes["healthy"] == 2 * 172
