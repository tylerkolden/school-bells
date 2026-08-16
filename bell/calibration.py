"""Strict validation of Poly Group Page framing from known live captures."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bell.wire.poly_group_page import (
    POLY_AUDIO_HEADER_BYTES,
    POLY_CALLER_ID_BYTES,
    POLY_CODEC_TYPES,
    POLY_CONTROL_HEADER_BYTES,
    POLY_TRANSMIT,
    PolyGroupPage,
    PolySpec,
)

MIN_CHANNEL_CAPTURES = 3
MIN_PACKETS_PER_CHANNEL = 8


class CalibrationError(ValueError):
    """Raised when captures cannot prove one unambiguous supported Poly layout."""


@dataclass(frozen=True, slots=True)
class PolyPacket:
    opcode: int
    encoded_channel: int
    host_serial: int
    caller_id_length: int
    caller_id: bytes
    codec_type: int
    flags: int
    sample_count: int
    audio: bytes


@dataclass(frozen=True, slots=True)
class CaptureEvidence:
    channel: int
    packet_count: int
    header_sha256: str


@dataclass(frozen=True, slots=True)
class DerivedCalibration:
    spec: PolySpec
    evidence: tuple[CaptureEvidence, ...]


def parse_poly_transmit(packet: bytes, expected_payload_type: int = 0) -> PolyPacket:
    """Parse one Poly transmit packet or sanitized 26-byte transmit header."""
    codec = POLY_CODEC_TYPES.get(expected_payload_type)
    if codec is None:
        raise CalibrationError(
            f"Poly Group Page does not support payload type {expected_payload_type}"
        )
    minimum = POLY_CONTROL_HEADER_BYTES + POLY_AUDIO_HEADER_BYTES
    if len(packet) < minimum:
        raise CalibrationError(f"capture is shorter than the {minimum}-byte Poly audio header")
    opcode, channel, host_serial, caller_length, caller_id = struct.unpack_from(
        "!BBIB13s", packet
    )
    if opcode != POLY_TRANSMIT:
        raise CalibrationError("capture is not a Poly transmit packet")
    if not 26 <= channel <= 50:
        raise CalibrationError("capture does not use a Poly paging channel from 26 through 50")
    if caller_length != POLY_CALLER_ID_BYTES:
        raise CalibrationError("capture does not use the fixed 13-byte Poly caller ID")
    codec_type, flags, sample_count = struct.unpack_from("!BBI", packet, 20)
    if codec_type != expected_payload_type:
        raise CalibrationError(
            f"capture is not configured Poly {codec} codec type {expected_payload_type}"
        )
    if flags != 0:
        raise CalibrationError("capture uses unsupported Poly audio flags")
    audio = bytes(packet[minimum:])
    if audio and len(audio) not in {160, 320}:
        raise CalibrationError(
            "Poly audio payload must contain one or two 160-byte/20 ms frames"
        )
    return PolyPacket(
        opcode,
        channel,
        host_serial,
        caller_length,
        caller_id,
        codec_type,
        flags,
        sample_count,
        audio,
    )


def header_only(packet: bytes, expected_payload_type: int = 0) -> bytes:
    """Validate a live Poly audio packet and discard its audio immediately."""
    parsed = parse_poly_transmit(packet, expected_payload_type)
    if not parsed.audio:
        raise CalibrationError("capture contains a header without live audio evidence")
    return bytes(packet[: POLY_CONTROL_HEADER_BYTES + POLY_AUDIO_HEADER_BYTES])


def valid_header_or_none(packet: bytes, expected_payload_type: int = 0) -> bytes | None:
    """Discard unrelated datagrams and return only a validated Poly audio header."""
    try:
        return header_only(packet, expected_payload_type)
    except CalibrationError:
        return None


def sanitize_capture(
    packets: Sequence[bytes], expected_payload_type: int = 0
) -> list[bytes]:
    """Return validated, header-only Poly transmit packets."""
    if expected_payload_type not in POLY_CODEC_TYPES:
        raise CalibrationError(
            f"Poly Group Page does not support payload type {expected_payload_type}"
        )
    if len(packets) < MIN_PACKETS_PER_CHANNEL:
        raise CalibrationError(
            f"capture needs at least {MIN_PACKETS_PER_CHANNEL} packets; received {len(packets)}"
        )
    headers: list[bytes] = []
    rejected: list[str] = []
    for packet in packets:
        try:
            parsed = parse_poly_transmit(packet, expected_payload_type)
            headers.append(bytes(packet[:26]))
            if parsed.audio:
                headers[-1] = header_only(packet, expected_payload_type)
        except CalibrationError as exc:
            rejected.append(str(exc))
    if len(headers) < MIN_PACKETS_PER_CHANNEL:
        detail = f" Last rejected packet: {rejected[-1]}" if rejected else ""
        raise CalibrationError(
            f"capture needs at least {MIN_PACKETS_PER_CHANNEL} valid Poly "
            f"{POLY_CODEC_TYPES[expected_payload_type]} transmit packets; "
            f"accepted {len(headers)} of {len(packets)}.{detail}"
        )
    return headers


def derive_poly_calibration(
    captures: Mapping[int, Sequence[bytes]], expected_payload_type: int = 0
) -> DerivedCalibration:
    """Derive the page-channel bias only when three known channels prove it."""
    if len(captures) < MIN_CHANNEL_CAPTURES:
        raise CalibrationError(
            f"capture at least {MIN_CHANNEL_CAPTURES} distinct known Poly channels"
        )
    channels = sorted(captures)
    if any(not 1 <= channel <= 25 for channel in channels):
        raise CalibrationError("known Poly channels must be between 1 and 25")

    evidence: list[CaptureEvidence] = []
    biases: set[int] = set()
    parsed_by_channel: dict[int, list[PolyPacket]] = {}
    for channel in channels:
        headers = sanitize_capture(captures[channel], expected_payload_type)
        parsed_packets = [parse_poly_transmit(packet, expected_payload_type) for packet in headers]
        encoded_channels = {packet.encoded_channel for packet in parsed_packets}
        if len(encoded_channels) != 1:
            raise CalibrationError(f"channel {channel} capture contains mixed Poly channels")
        encoded_channel = encoded_channels.pop()
        biases.add(encoded_channel - channel)
        parsed_by_channel[channel] = parsed_packets
        digest = hashlib.sha256()
        for header in headers:
            digest.update(len(header).to_bytes(4, "big"))
            digest.update(header)
        evidence.append(CaptureEvidence(channel, len(headers), digest.hexdigest()))

    if len(biases) != 1:
        raise CalibrationError(
            "known captures do not prove one consistent Poly paging-channel mapping"
        )
    channel_bias = biases.pop()
    try:
        spec = PolySpec(channel_bias)
    except ValueError as exc:
        raise CalibrationError(
            "captures do not confirm the required Poly Page group-to-channel +25 mapping"
        ) from exc

    for channel, packets in parsed_by_channel.items():
        for packet in packets:
            try:
                caller_id = packet.caller_id.rstrip(b"\0").decode("ascii", "strict")
                rebuilt = PolyGroupPage(spec, packet.codec_type, caller_id).build_packet(
                    b"\0" * 160,
                    0,
                    packet.sample_count,
                    packet.host_serial,
                    False,
                    channel,
                )[:26]
            except (UnicodeDecodeError, ValueError) as exc:
                raise CalibrationError(
                    "captured Poly caller ID must contain 1 to 13 ASCII bytes"
                ) from exc
            expected = (
                struct.pack(
                    "!BBIB13s",
                    packet.opcode,
                    packet.encoded_channel,
                    packet.host_serial,
                    packet.caller_id_length,
                    packet.caller_id,
                )
                + struct.pack("!BBI", packet.codec_type, packet.flags, packet.sample_count)
            )
            if rebuilt != expected:
                raise CalibrationError("derived Poly builder does not reproduce captured headers")
    return DerivedCalibration(spec, tuple(evidence))
