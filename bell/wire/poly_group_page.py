"""Poly Group Page packet construction proven by live Yealink traffic.

The on-wire format is the 20-byte Poly PTT/Page header documented in Engineering
Advisory 70568, followed (for transmit packets) by a six-byte audio header and one
or two codec frames. A calibration is still required before transmission: it
proves that the site's receivers use this format and its paging-channel bias.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


class PolyFormatNotCalibrated(RuntimeError):
    """Raised rather than emitting Poly packets without site evidence."""


POLY_ALERT = 0x0F
POLY_TRANSMIT = 0x10
POLY_END = 0xFF
POLY_CALLER_ID_BYTES = 13
POLY_CONTROL_HEADER_BYTES = 20
POLY_AUDIO_HEADER_BYTES = 6
POLY_CODEC_TYPES = {0: "PCMU", 9: "G722"}


@dataclass(frozen=True, slots=True)
class PolySpec:
    """The site-specific relationship proven by controlled live captures."""

    channel_bias: int
    control_header_bytes: int = POLY_CONTROL_HEADER_BYTES
    audio_header_bytes: int = POLY_AUDIO_HEADER_BYTES

    def __post_init__(self) -> None:
        if self.channel_bias != 25:
            raise ValueError("Poly Page groups must use the verified +25 channel bias")
        if self.control_header_bytes != POLY_CONTROL_HEADER_BYTES:
            raise ValueError("unsupported Poly control-header size")
        if self.audio_header_bytes != POLY_AUDIO_HEADER_BYTES:
            raise ValueError("unsupported Poly audio-header size")


SPEC: PolySpec | None = None


def _caller_id_field(caller_id: str) -> bytes:
    try:
        encoded = caller_id.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Poly caller ID must contain ASCII characters only") from exc
    if not encoded or len(encoded) > POLY_CALLER_ID_BYTES:
        raise ValueError("Poly caller ID must contain 1 to 13 ASCII bytes")
    return encoded.ljust(POLY_CALLER_ID_BYTES, b"\0")


class PolyGroupPage:
    alert_count = 31
    end_count = 12
    control_interval_seconds = 0.030
    end_delay_seconds = 0.050
    session_overhead_seconds = (
        alert_count * control_interval_seconds
        + end_delay_seconds
        + end_count * control_interval_seconds
    )

    def __init__(
        self,
        spec: PolySpec | None = None,
        payload_type: int = 0,
        caller_id: str = "School Bells",
    ) -> None:
        if payload_type not in POLY_CODEC_TYPES:
            raise ValueError("Poly Group Page supports only PCMU (0) or G722 (9)")
        self.spec = SPEC if spec is None else spec
        self.payload_type = payload_type
        self.caller_id = _caller_id_field(caller_id)

    @property
    def name(self) -> str:
        return "poly_group_page"

    @property
    def calibrated(self) -> bool:
        return self.spec is not None

    def _identity_header(self, opcode: int, channel: int, host_serial: int) -> bytes:
        if self.spec is None:
            raise PolyFormatNotCalibrated(
                "Poly wire format not yet verified by capture — see docs/CAPTURE.md"
            )
        if not 1 <= channel <= 25:
            raise ValueError("Poly Group Page channel must be between 1 and 25")
        encoded_channel = channel + self.spec.channel_bias
        if encoded_channel > 0xFF:
            raise ValueError("calibrated Poly channel mapping exceeds one byte")
        return struct.pack(
            "!BBIB13s",
            opcode,
            encoded_channel,
            host_serial & 0xFFFFFFFF,
            POLY_CALLER_ID_BYTES,
            self.caller_id,
        )

    def build_control_packet(self, opcode: int, channel: int, host_serial: int) -> bytes:
        if opcode not in {POLY_ALERT, POLY_END}:
            raise ValueError("Poly control opcode must be alert or end")
        return self._identity_header(opcode, channel, host_serial)

    def build_packet(
        self,
        payload: bytes,
        seq: int,
        timestamp: int,
        ssrc: int,
        marker: bool,
        channel: int,
        previous_payload: bytes | None = None,
    ) -> bytes:
        del seq, marker
        current = bytes(payload)
        if len(current) != 160:
            raise ValueError("Poly Group Page requires 160-byte/20 ms codec frames")
        if previous_payload is not None and len(previous_payload) != len(current):
            raise ValueError("Poly redundant frame must match the current frame size")
        audio_header = struct.pack("!BBI", self.payload_type, 0, timestamp & 0xFFFFFFFF)
        redundant = b"" if previous_payload is None else bytes(previous_payload)
        return (
            self._identity_header(POLY_TRANSMIT, channel, ssrc)
            + audio_header
            + redundant
            + current
        )
