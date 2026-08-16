from __future__ import annotations

import struct

import pytest

from bell.wire import get_wire_format
from bell.wire.base import StreamState
from bell.wire.plain_rtp import PlainRTP
from bell.wire.poly_group_page import (
    POLY_ALERT,
    POLY_END,
    PolyFormatNotCalibrated,
    PolyGroupPage,
    PolySpec,
)


def test_plain_rtp_exact_bytes() -> None:
    payload = bytes(range(16))
    packet = PlainRTP().build_packet(payload, 0x1234, 0x12345678, 0x89ABCDEF, True, 0)
    assert packet == bytes.fromhex("80 80 12 34 12 34 56 78 89 ab cd ef") + payload


def test_plain_rtp_rejects_channel() -> None:
    with pytest.raises(ValueError, match="cannot express"):
        PlainRTP().build_packet(b"x", 0, 0, 1, False, 23)


def test_plain_rtp_uses_selected_static_payload_type() -> None:
    packet = PlainRTP(payload_type=9).build_packet(b"g722", 1, 2, 3, False, 0)
    assert packet[1] & 0x7F == 9


def test_stream_state_wraps() -> None:
    state = StreamState(seq=65535, timestamp=0xFFFFFF60, ssrc=7)
    assert state.next() == (65535, 0xFFFFFF60, 7)
    assert state.next() == (0, 0, 7)


def test_stream_state_uses_configured_rtp_clock_step() -> None:
    state = StreamState(seq=1, timestamp=100, ssrc=7, timestamp_step=960)
    state.next()
    assert state.next()[1] == 1060


def test_poly_refuses_uncalibrated_output() -> None:
    with pytest.raises(PolyFormatNotCalibrated, match="CAPTURE"):
        get_wire_format("poly_group_page").build_packet(
            b"\0" * 160, 1, 160, 1, True, 23
        )


def test_poly_builds_exact_live_observed_g722_packets() -> None:
    wire = PolyGroupPage(PolySpec(25), payload_type=9, caller_id="School Bells")
    alert = wire.build_control_packet(POLY_ALERT, 25, 0x8899AABB)
    ended = wire.build_control_packet(POLY_END, 25, 0x8899AABB)
    first = wire.build_packet(b"a" * 160, 1, 160, 0x8899AABB, True, 25)
    second = wire.build_packet(
        b"b" * 160, 2, 320, 0x8899AABB, False, 25, b"a" * 160
    )

    identity = struct.pack("!BBIB13s", 0x10, 50, 0x8899AABB, 13, b"School Bells\0\0")
    assert alert == bytes((0x0F, 50)) + identity[2:]
    assert ended == bytes((0xFF, 50)) + identity[2:]
    assert first == identity + struct.pack("!BBI", 9, 0, 160) + b"a" * 160
    assert second == (
        identity + struct.pack("!BBI", 9, 0, 320) + b"a" * 160 + b"b" * 160
    )
    assert len(alert) == 20
    assert len(first) == 186
    assert len(second) == 346


def test_poly_rejects_unpublished_pcma_codec() -> None:
    with pytest.raises(ValueError, match=r"PCMU.*G722"):
        PolyGroupPage(PolySpec(25), payload_type=8)


def test_poly_validates_channel_frame_and_caller_id() -> None:
    wire = PolyGroupPage(PolySpec(25))
    with pytest.raises(ValueError, match="between 1 and 25"):
        wire.build_control_packet(POLY_ALERT, 0, 1)
    with pytest.raises(ValueError, match="160-byte"):
        wire.build_packet(b"short", 1, 1, 1, False, 23)
    with pytest.raises(ValueError, match="1 to 13"):
        PolyGroupPage(PolySpec(25), caller_id="caller id is too long")
