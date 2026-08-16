from __future__ import annotations

import struct

import pytest

from bell.wire import get_wire_format
from bell.wire.base import StreamState
from bell.wire.plain_rtp import PlainRTP
from bell.wire.poly_group_page import PolyFormatNotCalibrated, PolyGroupPage, PolySpec


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
        get_wire_format("poly_group_page").build_packet(b"\0" * 160, 1, 160, 1, True, 23)


def test_poly_table_builder_with_explicit_test_spec() -> None:
    spec = PolySpec(0xABCD, 1, ((0, 0x44), (1, "channel"), (2, 0x55)))
    packet = PolyGroupPage(spec).build_packet(b"payload", 1, 160, 2, False, 24)
    assert packet[:12] == struct.pack("!BBHII", 0x90, 0, 1, 160, 2)
    assert packet[12:20] == bytes.fromhex("ab cd 00 01 44 18 55 00")
    assert packet[20:] == b"payload"


def test_poly_table_builder_uses_selected_static_payload_type() -> None:
    spec = PolySpec(0xABCD, 1, ((0, 0x44), (1, "channel"), (2, 0x55)))
    packet = PolyGroupPage(spec, payload_type=9).build_packet(
        b"g722", 1, 160, 2, False, 25
    )

    assert packet[1] & 0x7F == 9
    assert packet[17] == 25
