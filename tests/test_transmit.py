from __future__ import annotations

import socket
import threading
import time
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import bell.transmit as transmit_module
from bell.probe import parse_rtp
from bell.transmit import DestinationEndpoint, TransmitResult, Transmitter
from bell.wire.plain_rtp import PlainRTP
from bell.wire.poly_group_page import PolyGroupPage, PolySpec


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


def test_poly_session_sends_alert_redundant_audio_and_end_packets() -> None:
    class RecordingSocket:
        packets: ClassVar[list[bytes]] = []

        def __init__(self, *_args) -> None:
            return None

        def setsockopt(self, *_args) -> None:
            return None

        def sendto(self, packet, _address) -> int:
            self.packets.append(packet)
            return len(packet)

        def close(self) -> None:
            return None

    wire = PolyGroupPage(PolySpec(25), payload_type=9)
    wire.alert_count = 2
    wire.end_count = 2
    wire.control_interval_seconds = 0
    wire.end_delay_seconds = 0
    destination = DestinationEndpoint("poly", "239.1.1.1", 601)
    with Transmitter(
        wire,
        [destination],
        "127.0.0.1",
        socket_factory=RecordingSocket,
        packet_seconds=0,
    ) as transmitter:
        result = transmitter.send([b"a" * 160, b"b" * 160], 25)

    packets = RecordingSocket.packets
    assert [packet[0] for packet in packets] == [0x0F, 0x0F, 0x10, 0x10, 0xFF, 0xFF]
    assert all(packet[1] == 50 for packet in packets)
    assert [len(packet) for packet in packets] == [20, 20, 186, 346, 20, 20]
    assert packets[3][26:186] == b"a" * 160
    assert packets[3][186:] == b"b" * 160
    assert result.packet_count == 2
    assert result.destination_bytes["poly"] == sum(map(len, packets))


def test_cli_uses_selected_g722_payload_and_clock(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    encoded = tmp_path / "source.g722"
    encoded.write_bytes(b"g" * 320)

    def fake_transcode(path, codec):
        assert path == source
        assert codec == "g722"
        return encoded

    class FakeTransmitter:
        def __init__(self, wire, _endpoints, _iface, **kwargs) -> None:
            assert wire.payload_type == 9
            assert kwargs["timestamp_step"] == 160

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def send(self, frames, channel) -> TransmitResult:
            assert list(frames) == [b"g" * 160, b"g" * 160]
            assert channel == 0
            return TransmitResult(2, 0.04, 0.0, 0.0, {"all": 344}, {}, False)

    monkeypatch.setattr(transmit_module, "transcode", fake_transcode)
    monkeypatch.setattr(transmit_module, "Transmitter", FakeTransmitter)

    result = transmit_module.main(
        [
            "--file",
            str(source),
            "--channel",
            "0",
            "--format",
            "plain_rtp",
            "--codec",
            "g722",
            "--dry-run",
        ]
    )

    assert result == 0
