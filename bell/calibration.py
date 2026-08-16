"""Strict derivation of a Poly Group Page header from known live channel captures."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bell.probe import RTPPacket, parse_rtp
from bell.wire.poly_group_page import PolyGroupPage, PolySpec

MIN_CHANNEL_CAPTURES = 3
MIN_PACKETS_PER_CHANNEL = 8


class CalibrationError(ValueError):
    """Raised when captures cannot prove one unambiguous supported Poly layout."""


@dataclass(frozen=True, slots=True)
class CaptureEvidence:
    channel: int
    packet_count: int
    header_sha256: str


@dataclass(frozen=True, slots=True)
class DerivedCalibration:
    spec: PolySpec
    evidence: tuple[CaptureEvidence, ...]


def header_only(packet: bytes) -> bytes:
    """Validate a captured packet and discard its audio payload immediately."""
    parsed = parse_rtp(packet)
    if parsed.version != 2:
        raise CalibrationError("capture is not RTP version 2")
    if parsed.payload_type != 0:
        raise CalibrationError("capture is not PCMU RTP payload type 0")
    if parsed.padding or parsed.csrc_count:
        raise CalibrationError("capture uses unsupported RTP padding or CSRC fields")
    if not parsed.extension or parsed.extension_profile is None or parsed.extension_words is None:
        raise CalibrationError("capture has no RTP header extension")
    if parsed.extension_words < 1:
        raise CalibrationError("capture has an empty RTP header extension")
    return bytes(packet[: parsed.header_length])


def sanitize_capture(packets: Sequence[bytes]) -> list[bytes]:
    """Return validated header-only packets, never retained voice payload."""
    if len(packets) < MIN_PACKETS_PER_CHANNEL:
        raise CalibrationError(
            f"capture needs at least {MIN_PACKETS_PER_CHANNEL} packets; received {len(packets)}"
        )
    return [header_only(packet) for packet in packets]


def derive_poly_calibration(captures: Mapping[int, Sequence[bytes]]) -> DerivedCalibration:
    """Derive a spec only when three known channels prove one exact byte mapping."""
    if len(captures) < MIN_CHANNEL_CAPTURES:
        raise CalibrationError(
            f"capture at least {MIN_CHANNEL_CAPTURES} distinct known Poly channels"
        )
    channels = sorted(captures)
    if any(not 1 <= channel <= 25 for channel in channels):
        raise CalibrationError("known Poly channels must be between 1 and 25")

    parsed_by_channel: dict[int, list[RTPPacket]] = {}
    evidence: list[CaptureEvidence] = []
    profile: int | None = None
    words: int | None = None
    for channel in channels:
        headers = sanitize_capture(captures[channel])
        parsed_packets = [parse_rtp(packet) for packet in headers]
        identities = {(packet.extension_profile, packet.extension_words) for packet in parsed_packets}
        if len(identities) != 1:
            raise CalibrationError(f"channel {channel} contains mixed RTP extension layouts")
        current_profile, current_words = identities.pop()
        if profile is None:
            profile, words = current_profile, current_words
        elif (current_profile, current_words) != (profile, words):
            raise CalibrationError("known channels use different RTP extension layouts")
        parsed_by_channel[channel] = parsed_packets
        digest = hashlib.sha256()
        for header in headers:
            digest.update(len(header).to_bytes(4, "big"))
            digest.update(header)
        evidence.append(CaptureEvidence(channel, len(headers), digest.hexdigest()))

    assert profile is not None and words is not None
    extension_size = words * 4
    candidates: list[int] = []
    for offset in range(extension_size):
        if all(
            {packet.extension_data[offset] for packet in parsed_by_channel[channel]}
            == {channel}
            for channel in channels
        ):
            candidates.append(offset)
    if len(candidates) != 1:
        raise CalibrationError(
            "captures do not prove exactly one full-byte channel position; "
            "repeat with clean traffic on three distinct channels"
        )
    channel_offset = candidates[0]

    mappings: list[tuple[int, str | int]] = []
    for offset in range(extension_size):
        if offset == channel_offset:
            mappings.append((offset, "channel"))
            continue
        values = {
            packet.extension_data[offset]
            for parsed_packets in parsed_by_channel.values()
            for packet in parsed_packets
        }
        if len(values) != 1:
            raise CalibrationError(
                f"extension byte {offset} changes independently of the known channel; "
                "this layout requires manual protocol review"
            )
        mappings.append((offset, values.pop()))

    spec = PolySpec(profile, words, tuple(mappings))
    wire = PolyGroupPage(spec)
    for channel, packets in parsed_by_channel.items():
        for packet in packets:
            rebuilt = wire.build_packet(
                b"",
                packet.sequence,
                packet.timestamp,
                packet.ssrc,
                packet.marker,
                channel,
            )
            if rebuilt != packet_to_header(packet):
                raise CalibrationError("derived header does not reproduce every captured RTP header")
    return DerivedCalibration(spec, tuple(evidence))


def packet_to_header(packet: RTPPacket) -> bytes:
    """Recreate the exact captured RTP header for proof comparison."""
    first = 0x80 | 0x10
    second = (0x80 if packet.marker else 0) | packet.payload_type

    return (
        struct.pack(
            "!BBHII",
            first,
            second,
            packet.sequence,
            packet.timestamp,
            packet.ssrc,
        )
        + struct.pack("!HH", packet.extension_profile, packet.extension_words)
        + packet.extension_data
    )
