"""RFC 3550 RTP packet construction for G.711 PCMU."""

from __future__ import annotations

import struct


class PlainRTP:
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
        second = (0x80 if marker else 0) | 0
        return struct.pack(
            "!BBHII", 0x80, second, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF
        ) + bytes(payload)
