from __future__ import annotations

import struct

import pytest

from bell.calibration import (
    CalibrationError,
    derive_poly_calibration,
    header_only,
    parse_poly_transmit,
    sanitize_capture,
)


def captured_packet(
    channel: int,
    sample_count: int,
    *,
    payload_type: int = 0,
    encoded_channel: int | None = None,
) -> bytes:
    return (
        struct.pack(
            "!BBIB13sBBI",
            0x10,
            encoded_channel if encoded_channel is not None else channel + 25,
            0x8899AABB,
            13,
            b"Headmaster\0\0\0",
            payload_type,
            0,
            sample_count,
        )
        + b"v" * 320
    )


def captures(payload_type: int = 0) -> dict[int, list[bytes]]:
    return {
        channel: [captured_packet(channel, sequence * 160, payload_type=payload_type) for sequence in range(1, 9)]
        for channel in (23, 24, 25)
    }


def test_derives_live_proven_page_channel_bias() -> None:
    result = derive_poly_calibration(captures())

    assert result.spec.channel_bias == 25
    assert result.spec.control_header_bytes == 20
    assert result.spec.audio_header_bytes == 6
    assert [item.channel for item in result.evidence] == [23, 24, 25]
    assert all(item.packet_count == 8 and len(item.header_sha256) == 64 for item in result.evidence)


def test_discards_captured_voice_payload() -> None:
    packet = captured_packet(23, 160)
    header = header_only(packet)

    assert header == packet[:26]
    assert b"v" * 20 not in header


def test_parses_live_observed_g722_packet_shape() -> None:
    packet = captured_packet(25, 320, payload_type=9)
    parsed = parse_poly_transmit(packet, 9)

    assert parsed.encoded_channel == 50
    assert parsed.codec_type == 9
    assert parsed.sample_count == 320
    assert len(parsed.audio) == 320


def test_ignores_control_and_rejects_too_few_transmit_packets() -> None:
    alert = struct.pack("!BBIB13s", 0x0F, 48, 1, 13, b"Headmaster\0\0\0")
    packets = [alert, *captures()[23][:7]]

    with pytest.raises(CalibrationError, match=r"accepted 7 of 8"):
        sanitize_capture(packets)


def test_rejects_fewer_than_three_known_channels() -> None:
    with pytest.raises(CalibrationError, match=r"three|3 distinct"):
        derive_poly_calibration({23: captures()[23], 24: captures()[24]})


def test_rejects_inconsistent_page_channel_mapping() -> None:
    ambiguous = captures()
    ambiguous[25] = [
        captured_packet(25, sequence * 160, encoded_channel=49)
        for sequence in range(1, 9)
    ]

    with pytest.raises(CalibrationError, match="consistent"):
        derive_poly_calibration(ambiguous)


def test_rejects_consistent_but_nonstandard_channel_bias() -> None:
    wrong = {
        channel: [
            captured_packet(channel, sequence * 160, encoded_channel=channel + 24)
            for sequence in range(1, 9)
        ]
        for channel in (23, 24, 25)
    }

    with pytest.raises(CalibrationError, match=r"\+25 mapping"):
        derive_poly_calibration(wrong)


def test_accepts_configured_g722_and_rejects_codec_mismatch() -> None:
    packet = captured_packet(25, 160, payload_type=9)

    assert header_only(packet, expected_payload_type=9) == packet[:26]
    with pytest.raises(CalibrationError, match="configured Poly PCMU"):
        header_only(packet)
    assert derive_poly_calibration(captures(9), 9).spec.channel_bias == 25


@pytest.mark.parametrize("length", [1, 159, 161, 319, 321])
def test_rejects_unsupported_audio_frame_lengths(length: int) -> None:
    packet = captured_packet(25, 160)[:26] + b"x" * length
    with pytest.raises(CalibrationError, match="one or two"):
        parse_poly_transmit(packet)
