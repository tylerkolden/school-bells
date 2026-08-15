from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from bell.config import load_config
from bell.delivery import DeliveryReport
from bell.protocols.base import DeliveryOutcome
from bell.service import ServiceRuntime, interface_present, load_environment_file, validate_startup


def test_loopback_interface_and_startup_validation(config_tree: Path, monkeypatch) -> None:
    settings = config_tree / "settings.yaml"
    text = settings.read_text(encoding="utf-8").replace(
        "interface_ip: 192.168.10.20", "interface_ip: 127.0.0.1"
    )
    settings.write_text(text, encoding="utf-8")
    config = load_config(config_tree)
    monkeypatch.setattr("bell.service.clock_sync_status", lambda: (True, "synchronized"))
    assert interface_present("127.0.0.1")[0]
    assert validate_startup(config) == []


def test_health_and_readiness_shape(config_tree: Path, monkeypatch) -> None:
    monkeypatch.setattr("bell.service.clock_sync_status", lambda: (False, "not synchronized"))
    runtime = ServiceRuntime(load_config(config_tree))
    client = TestClient(runtime.health_app)
    ready = client.get("/ready")
    assert ready.status_code == 503 and ready.json()["ready"] is False
    health = client.get("/health").json()
    assert health["status"] == "degraded"
    assert "clock not synchronized" in health["readiness_reasons"]
    assert "scheduler not running" in health["readiness_reasons"]
    assert "endpoint monitor not running" in health["readiness_reasons"]
    assert health["config_valid"] is True
    assert {"last_fire", "next_scheduled_fire", "config_hash", "kill_switch", "uptime_seconds", "clock"} <= health.keys()
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "bell_ready 0" in metrics.text
    assert "bell_uptime_seconds" in metrics.text


def test_pre_tone_and_repeats_use_one_coordinated_page(config_tree: Path) -> None:
    config = load_config(config_tree)
    runtime = ServiceRuntime(config)
    calls: list[str] = []

    class FakeDelivery:
        def deliver(self, _raw, _event, _zone, _cancel, *, sound_name=None, idempotency_key=None):
            calls.append(sound_name or "main")
            outcome = DeliveryOutcome("all", "multicast", True, "sent", idempotency_key or "", 1, 0.01)
            return DeliveryReport((outcome,), False, 0.01)

    runtime.delivery = FakeDelivery()
    event = config.schedule_map["Regular Day"].events[0].model_copy(
        update={
            "pre_tone": "recess-bell.wav",
            "repeat_count": 2,
            "repeat_interval_seconds": 0.0,
        }
    )
    runtime.transmit_event(event, config, "Test")
    assert calls == ["recess-bell.wav", "main", "main"]


def test_startup_rejects_short_or_shared_control_keys(config_tree: Path, monkeypatch) -> None:
    config = load_config(config_tree)
    config.settings.interface_ip = "127.0.0.1"
    monkeypatch.setenv("BELL_UI_PASSWORD", "short")
    monkeypatch.setenv("BELL_API_KEY", "same-key-that-is-long-enough")
    monkeypatch.setenv("BELL_EMERGENCY_API_KEY", "same-key-that-is-long-enough")
    errors = validate_startup(config)
    assert any("BELL_UI_PASSWORD" in item for item in errors)
    assert any("must be different" in item for item in errors)


def test_page_duration_limit_is_enforced_before_delivery(config_tree: Path) -> None:
    config = load_config(config_tree)
    config.settings.max_page_seconds = 0.1
    runtime = ServiceRuntime(config)
    event = config.schedule_map["Regular Day"].events[0]
    try:
        runtime.transmit_event(event, config, "Test")
    except Exception as exc:
        assert "max_page_seconds" in str(exc)
    else:
        raise AssertionError("overlong page was not rejected")


def test_environment_file_is_parsed_without_shell_evaluation(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "bell.env"
    path.write_text("# comment\nBELL_SAFE_VALUE='literal $(touch nope)'\n", encoding="utf-8")
    monkeypatch.delenv("BELL_SAFE_VALUE", raising=False)
    load_environment_file(path)
    assert os.environ["BELL_SAFE_VALUE"] == "literal $(touch nope)"
    assert not (tmp_path / "nope").exists()
