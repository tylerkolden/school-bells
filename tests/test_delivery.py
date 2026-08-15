from __future__ import annotations

import threading
from pathlib import Path

import pytest

import bell.delivery as delivery_module
from bell.config import load_config
from bell.delivery import DeliveryManager, PageDeliveryError
from bell.monitor import EndpointRegistry
from bell.protocols.base import DeliveryOutcome
from bell.transmit import TransmitResult


def test_plain_multicast_delivery_report(
    config_tree: Path, tmp_path: Path, monkeypatch
) -> None:
    config = load_config(config_tree)
    config.settings.interface_ip = "127.0.0.1"
    config.settings.wire_format = "plain_rtp"
    config.destination_map["all"].wire_format = "plain_rtp"
    raw = tmp_path / "one.ulaw"
    raw.write_bytes(b"a" * 160)

    class SuccessfulTransmitter:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def send(self, _frames, channel, _cancel) -> TransmitResult:
            assert channel == 0
            return TransmitResult(1, 0.02, 0.001, 0.001, {"all": 172}, {}, False)

    monkeypatch.setattr(delivery_module, "Transmitter", SuccessfulTransmitter)
    event = config.schedule_map["Regular Day"].events[0]
    report = DeliveryManager(config, EndpointRegistry()).deliver(
        raw,
        event,
        config.zone_map[event.zone],
        threading.Event(),
    )
    assert report.successful
    assert report.outcomes[0].status == "sent"


def test_required_protocol_failure_fails_entire_page(
    config_tree: Path, tmp_path: Path, monkeypatch
) -> None:
    config = load_config(config_tree)
    config.settings.interface_ip = "127.0.0.1"
    config.settings.wire_format = "plain_rtp"
    config.destination_map["all"].wire_format = "plain_rtp"
    raw = tmp_path / "one.ulaw"
    raw.write_bytes(b"a" * 160)

    class BrokenTransmitter:
        def __init__(self, *_args, **_kwargs) -> None:
            raise OSError("network unavailable")

    monkeypatch.setattr(delivery_module, "Transmitter", BrokenTransmitter)
    event = config.schedule_map["Regular Day"].events[0]
    with pytest.raises(PageDeliveryError, match="required delivery failed"):
        DeliveryManager(config, EndpointRegistry()).deliver(
            raw,
            event,
            config.zone_map[event.zone],
            threading.Event(),
        )


def test_all_optional_failures_are_not_reported_as_success(
    config_tree: Path, tmp_path: Path, monkeypatch
) -> None:
    config = load_config(config_tree)
    webhook = config.destination_map["webhook-example"]
    webhook.enabled = True
    config.zone_map["indoors"].destinations = [webhook.name]
    raw = tmp_path / "one.ulaw"
    raw.write_bytes(b"a" * 160)

    def fail(*_args, **_kwargs) -> DeliveryOutcome:
        return DeliveryOutcome(webhook.name, "http", False, "503", "offline", 1, 0.1)

    monkeypatch.setattr(delivery_module.WebhookClient, "trigger", fail)
    event = config.schedule_map["Regular Day"].events[0]
    with pytest.raises(PageDeliveryError, match="no destination accepted"):
        DeliveryManager(config, EndpointRegistry()).deliver(
            raw,
            event,
            config.zone_map[event.zone],
            threading.Event(),
        )
