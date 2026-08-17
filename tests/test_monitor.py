from __future__ import annotations

import threading
import time

from bell.config import Destination, load_config
from bell.monitor import EndpointMonitor, EndpointRegistry
from bell.protocols.base import DeliveryOutcome


def test_optional_endpoint_circuit_breaker_and_recovery() -> None:
    registry = EndpointRegistry(failure_threshold=2, cooldown_seconds=60)
    destination = Destination(
        name="optional",
        protocol="http",
        port=443,
        webhook_url="https://example.test",
        required=False,
    )
    failure = DeliveryOutcome("optional", "http", False, "failed", "offline", 1, 0.1)
    registry.record(failure)
    assert registry.should_attempt(destination)
    registry.record(failure)
    assert not registry.should_attempt(destination)
    assert registry.snapshot()[0]["circuit_open"] is True
    registry.record(DeliveryOutcome("optional", "http", True, "200", "ok", 1, 0.1))
    assert registry.should_attempt(destination)


def test_required_poly_destination_reports_uncalibrated(config_tree) -> None:
    config = load_config(config_tree)
    registry = EndpointRegistry()
    outcomes = EndpointMonitor(config, registry).check_once()
    poly = next(item for item in outcomes if item.destination == "all")
    assert not poly.success and poly.status == "not_calibrated"


def test_config_reconcile_replaces_stale_protocol_and_removed_destinations() -> None:
    registry = EndpointRegistry(failure_threshold=1, cooldown_seconds=60)
    registry.record(DeliveryOutcome("all", "multicast", False, "failed", "bad", 1, 0.1))
    registry.record(DeliveryOutcome("removed", "http", True, "200", "ok", 1, 0.1))
    http = Destination(
        name="all",
        protocol="http",
        port=9000,
        webhook_url="http://receiver:9000/page",
        allow_insecure_http=True,
        required=False,
    )

    registry.reconcile([http])

    assert registry.should_attempt(http)
    assert registry.snapshot() == [
        {
            "name": "all",
            "protocol": "http",
            "state": "unknown",
            "detail": "not checked",
            "last_checked": None,
            "last_success": None,
            "consecutive_failures": 0,
            "circuit_open": False,
        }
    ]


def test_probe_exception_becomes_health_outcome(config_tree, monkeypatch) -> None:
    config = load_config(config_tree)
    monitor = EndpointMonitor(config, EndpointRegistry())

    def fail(_destination):
        raise RuntimeError("simulated probe crash")

    monkeypatch.setattr(monitor, "_check_multicast", fail)
    outcome = next(item for item in monitor.check_once() if item.destination == "all")
    assert not outcome.success
    assert outcome.status == "probe_error"
    assert "simulated probe crash" in outcome.detail


def test_endpoint_probes_run_concurrently(config_tree, monkeypatch) -> None:
    config = load_config(config_tree)
    config.destinations = [
        Destination(
            name=name,
            protocol="http",
            port=443,
            webhook_url=f"https://{name}.example.test/hook",
        )
        for name in ("one", "two")
    ]
    lock = threading.Lock()
    active = 0
    peak = 0

    def check(_client, destination):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return DeliveryOutcome(destination.name, "http", True, "200", "ok", 1, 0.05)

    monkeypatch.setattr("bell.monitor.WebhookClient.check", check)
    outcomes = EndpointMonitor(config, EndpointRegistry()).check_once()
    assert len(outcomes) == 2 and peak == 2
