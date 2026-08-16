"""Fail-closed Poly Group Page RTP extension builder.

Live traffic establishes that Poly/Yealink group paging is RTP-based with the RTP X bit set.
The paging channel (1-25) is carried somewhere in the RTP header extension, and compatible
receivers filter on that value; channel 0 means "any third-party device" in receiver settings.
The exact extension profile and byte layout are proprietary and must be derived from a packet
capture. This module deliberately ships without a speculative default. See ``docs/CAPTURE.md``.

Once captured, populate ``SPEC`` with the observed profile, extension length, and byte mappings.
Mappings are ``(offset, source)`` pairs where source is ``"channel"`` or an integer constant.
Offsets address the extension data immediately after the four-byte RTP extension header.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Literal


class PolyFormatNotCalibrated(RuntimeError):
    """Raised rather than emitting an invented Poly header."""


MappingSource = Literal["channel"] | int


@dataclass(frozen=True, slots=True)
class PolySpec:
    extension_profile_id: int
    extension_word_count: int
    mappings: tuple[tuple[int, MappingSource], ...]

    def __post_init__(self) -> None:
        if not 0 <= self.extension_profile_id <= 0xFFFF:
            raise ValueError("extension_profile_id must fit in 16 bits")
        if not 1 <= self.extension_word_count <= 0xFFFF:
            raise ValueError("extension_word_count must be positive")
        extension_size = self.extension_word_count * 4
        for offset, source in self.mappings:
            if not 0 <= offset < extension_size:
                raise ValueError(f"mapping offset {offset} is outside extension data")
            if source != "channel" and not isinstance(source, int):
                raise ValueError(f"invalid mapping source {source!r}")
            if isinstance(source, int) and not 0 <= source <= 0xFF:
                raise ValueError("constant mapping bytes must fit in one byte")


SPEC: PolySpec | None = None


class PolyGroupPage:
    def __init__(self, spec: PolySpec | None = None, payload_type: int = 0) -> None:
        if not 0 <= payload_type <= 127:
            raise ValueError("payload_type must fit in 7 bits")
        self.spec = SPEC if spec is None else spec
        self.payload_type = payload_type

    @property
    def name(self) -> str:
        return "poly_group_page"

    @property
    def calibrated(self) -> bool:
        return self.spec is not None

    def build_packet(
        self,
        payload: bytes,
        seq: int,
        timestamp: int,
        ssrc: int,
        marker: bool,
        channel: int,
    ) -> bytes:
        if self.spec is None:
            raise PolyFormatNotCalibrated(
                "Poly wire format not yet derived from capture — see docs/CAPTURE.md"
            )
        if not 1 <= channel <= 25:
            raise ValueError("Poly Group Page channel must be between 1 and 25")
        second = (0x80 if marker else 0) | self.payload_type
        rtp = struct.pack(
            "!BBHII", 0x90, second, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF
        )
        extension = bytearray(self.spec.extension_word_count * 4)
        for offset, source in self.spec.mappings:
            extension[offset] = channel if source == "channel" else source
        ext_header = struct.pack(
            "!HH", self.spec.extension_profile_id, self.spec.extension_word_count
        )
        return rtp + ext_header + extension + bytes(payload)
