"""Minimal systemd readiness and watchdog notifications using the standard library."""

from __future__ import annotations

import os
import socket


def notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.sendto(message.encode(), address)
    return True


def watchdog_interval(default_seconds: float = 30.0) -> float:
    value = os.environ.get("WATCHDOG_USEC")
    if not value:
        return default_seconds
    try:
        return max(1.0, int(value) / 2_000_000)
    except ValueError:
        return default_seconds
