"""Endpoint health registry, circuit state, and background protocol probes."""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from bell.config import BellConfig, Destination
from bell.protocols.base import DeliveryOutcome
from bell.protocols.http import WebhookClient
from bell.protocols.sip import SIPClient
from bell.wire.poly_group_page import PolyGroupPage

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EndpointHealth:
    name: str
    protocol: str
    state: str = "unknown"
    detail: str = "not checked"
    last_checked: str | None = None
    last_success: str | None = None
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0


class EndpointRegistry:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._items: dict[str, EndpointHealth] = {}

    def record(self, outcome: DeliveryOutcome) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            health = self._items.setdefault(
                outcome.destination, EndpointHealth(outcome.destination, outcome.protocol)
            )
            health.last_checked = now
            health.detail = outcome.detail
            if outcome.success:
                health.state = "healthy"
                health.last_success = now
                health.consecutive_failures = 0
                health.circuit_open_until = 0.0
            else:
                health.state = "unhealthy"
                health.consecutive_failures += 1
                if health.consecutive_failures >= self.failure_threshold:
                    health.state = "circuit_open"
                    health.circuit_open_until = time.monotonic() + self.cooldown_seconds

    def should_attempt(self, destination: Destination) -> bool:
        if destination.required:
            return True
        with self._lock:
            health = self._items.get(destination.name)
            return health is None or health.circuit_open_until <= time.monotonic()

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            items = [asdict(item) for item in self._items.values()]
        for item in items:
            until = float(item.pop("circuit_open_until", 0.0))
            item["circuit_open"] = until > time.monotonic()
        return sorted(items, key=lambda item: str(item["name"]))


class EndpointMonitor:
    def __init__(self, config: BellConfig, registry: EndpointRegistry) -> None:
        self.config = config
        self.registry = registry
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="endpoint-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def update_config(self, config: BellConfig) -> None:
        self.config = config

    def check_once(self) -> list[DeliveryOutcome]:
        outcomes: list[DeliveryOutcome] = []
        for destination in self.config.destinations:
            if not destination.enabled:
                continue
            started = time.monotonic()
            try:
                if destination.protocol == "multicast":
                    outcome = self._check_multicast(destination)
                elif destination.protocol == "sip":
                    outcome = SIPClient(destination, self.config.settings.interface_ip).options()
                else:
                    outcome = WebhookClient().check(destination)
            except Exception as exc:
                LOGGER.exception(
                    "endpoint_probe_failed", extra={"destination": destination.name}
                )
                outcome = DeliveryOutcome(
                    destination.name,
                    destination.protocol,
                    False,
                    "probe_error",
                    str(exc),
                    1,
                    time.monotonic() - started,
                )
            self.registry.record(outcome)
            outcomes.append(outcome)
        return outcomes

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                LOGGER.exception("endpoint_monitor_cycle_failed")
            self._stop.wait(self.config.settings.endpoint_check_interval_seconds)

    def _check_multicast(self, destination: Destination) -> DeliveryOutcome:
        started = time.monotonic()
        wire_name = destination.wire_format or self.config.settings.wire_format
        if wire_name == "poly_group_page" and not PolyGroupPage().calibrated:
            return DeliveryOutcome(
                destination.name,
                "multicast",
                False,
                "not_calibrated",
                "Poly Group Page is not calibrated - follow docs/CAPTURE.md",
                1,
                time.monotonic() - started,
            )
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.bind((self.config.settings.interface_ip, 0))
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(self.config.settings.interface_ip),
                )
            finally:
                sock.close()
            return DeliveryOutcome(
                destination.name,
                "multicast",
                True,
                "configured",
                "sender interface is multicast-capable; receiver health is not observable",
                1,
                time.monotonic() - started,
            )
        except OSError as exc:
            return DeliveryOutcome(
                destination.name,
                "multicast",
                False,
                "unreachable",
                str(exc),
                1,
                time.monotonic() - started,
            )
