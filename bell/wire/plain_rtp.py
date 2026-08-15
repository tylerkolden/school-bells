"""RFC 3550 RTP packet construction for G.711 PCMU."""

from __future__ import annotations

import struct


class PlainRTP:
    def __init__(self, payload_type: int = 0) -> None:
        if not 0 <= payload_type <= 127:
            raise ValueError("RTP payload type must be between 0 and 127")
        self.payload_type = payload_type

    @property
    def name(self) -> str:
        return "plain_rtp"

    def build_packet(
        self,
        payload: bytes,
        seq: int,
        timestamp: int,
        ssrc: int,
        marker: bool,
        channel: int,
    ) -> bytes:
        if channel != 0:
            raise ValueError("plain RTP cannot express a paging channel; channel must be 0")
        second = (0x80 if marker else 0) | self.payload_type
        return struct.pack(
            "!BBHII", 0x80, second, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF
        ) + bytes(payload)
