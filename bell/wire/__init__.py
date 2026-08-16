"""Wire-format factory."""

from __future__ import annotations

from bell.wire.base import WireFormat
from bell.wire.plain_rtp import PlainRTP
from bell.wire.poly_group_page import PolyGroupPage, PolySpec


def get_wire_format(name: str, poly_spec: PolySpec | None = None) -> WireFormat:
    normalized = name.strip().lower().replace("-", "_")
    if normalized == "plain_rtp":
        return PlainRTP()
    if normalized == "poly_group_page":
        return PolyGroupPage(poly_spec)
    raise ValueError(f"unknown wire format {name!r}; expected plain_rtp or poly_group_page")


__all__ = ["WireFormat", "get_wire_format"]
