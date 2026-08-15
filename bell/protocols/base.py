"""Common protocol delivery result types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    destination: str
    protocol: str
    success: bool
    status: str
    detail: str
    attempts: int
    duration_seconds: float
