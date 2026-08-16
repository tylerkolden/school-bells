"""Wire-format contracts and per-destination RTP stream state."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Protocol


class WireFormat(Protocol):
    @property
    def name(self) -> str: ...

    def build_packet(
        self,
        payload: bytes,
        seq: int,
        timestamp: int,
        ssrc: int,
        marker: bool,
        channel: int,
        previous_payload: bytes | None = None,
    ) -> bytes: ...


@dataclass(slots=True)
class StreamState:
    """RTP counters. ``next`` returns current values and then advances them."""

    seq: int = field(default_factory=lambda: secrets.randbelow(1 << 16))
    timestamp: int = field(default_factory=lambda: secrets.randbelow(1 << 32))
    ssrc: int = field(default_factory=lambda: secrets.randbelow((1 << 32) - 1) + 1)
    timestamp_step: int = 160

    def __post_init__(self) -> None:
        self.seq &= 0xFFFF
        self.timestamp &= 0xFFFFFFFF
        self.ssrc &= 0xFFFFFFFF

    def next(self) -> tuple[int, int, int]:
        current = (self.seq, self.timestamp, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + self.timestamp_step) & 0xFFFFFFFF
        return current
