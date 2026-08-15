from __future__ import annotations

import struct
from pathlib import Path

from bell.listen import _pcmu_payload, _ulaw_to_pcm16
from bell.probe import compare_packets, load_capture, parse_rtp, save_capture
from bell.wire.plain_rtp import PlainRTP


def test_parse_synthetic_plain_rtp() -> None:
    packet = PlainRTP().build_packet(b"abc", 42, 320, 99, True, 0)
    parsed = parse_rtp(packet)
    assert (parsed.version, parsed.padding, parsed.extension, parsed.csrc_count) == (2, False, False, 0)
    assert (parsed.marker, parsed.payload_type, parsed.sequence, parsed.timestamp, parsed.ssrc) == (True, 0, 42, 320, 99)
    assert parsed.header_length == 12
    assert parsed.payload == b"abc"


def test_compare_ignores_changing_rtp_counters_and_finds_known_byte() -> None:
    left = bytearray.fromhex("90 00 00 01 00 00 00 02 00 00 00 03 ab cd 00 01 17 00 00 00") + b"voice-a"
    right = bytearray.fromhex("90 00 00 09 00 00 00 14 00 00 00 1e ab cd 00 01 18 00 00 00") + b"voice-b"
    assert compare_packets(left, right) == [(16, 23, 24)]


def test_capture_round_trip(tmp_path: Path) -> None:
    packets = [b"one", b"two"]
    path = tmp_path / "capture.bin"
    save_capture(path, packets)
    assert load_capture(path) == packets


def test_listener_extracts_and_decodes_pcmu() -> None:
    packet = PlainRTP().build_packet(b"\xff" * 160, 1, 2, 3, False, 0)
    assert _pcmu_payload(packet) == b"\xff" * 160
    assert struct.unpack("<h", _ulaw_to_pcm16(b"\xff"))[0] == 0
