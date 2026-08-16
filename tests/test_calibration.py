from __future__ import annotations

import struct

import pytest

from bell.calibration import CalibrationError, derive_poly_calibration, header_only


def captured_packet(channel: int, sequence: int, *, changing: int = 0x55) -> bytes:
    return (
        struct.pack("!BBHII", 0x90, 0x80 if sequence == 1 else 0, sequence, sequence * 160, 99)
        + struct.pack("!HH", 0xABCD, 1)
        + bytes((0x44, channel, changing, 0))
        + b"private voice payload"
    )


def captures() -> dict[int, list[bytes]]:
    return {
        channel: [captured_packet(channel, sequence) for sequence in range(1, 9)]
        for channel in (23, 24, 25)
    }


def test_derives_exact_spec_from_three_known_channels() -> None:
    result = derive_poly_calibration(captures())

    assert result.spec.extension_profile_id == 0xABCD
    assert result.spec.extension_word_count == 1
    assert result.spec.mappings == ((0, 0x44), (1, "channel"), (2, 0x55), (3, 0))
    assert [item.channel for item in result.evidence] == [23, 24, 25]
    assert all(item.packet_count == 8 and len(item.header_sha256) == 64 for item in result.evidence)


def test_discards_captured_voice_payload() -> None:
    packet = captured_packet(23, 1)
    header = header_only(packet)

    assert header == packet[:20]
    assert b"voice" not in header


def test_rejects_fewer_than_three_known_channels() -> None:
    with pytest.raises(CalibrationError, match=r"three|3 distinct"):
        derive_poly_calibration({23: captures()[23], 24: captures()[24]})


def test_rejects_ambiguous_or_independently_changing_extension_bytes() -> None:
    ambiguous = captures()
    ambiguous[25] = [
        captured_packet(25, sequence, changing=sequence) for sequence in range(1, 9)
    ]

    with pytest.raises(CalibrationError, match="changes independently"):
        derive_poly_calibration(ambiguous)


def test_rejects_non_pcmu_and_payload_without_extension() -> None:
    plain = struct.pack("!BBHII", 0x80, 8, 1, 160, 99) + b"audio"

    with pytest.raises(CalibrationError, match="PCMU"):
        header_only(plain)
