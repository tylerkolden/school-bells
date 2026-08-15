from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from bell.config import load_config
from bell.scheduler import BellScheduler
from bell.web import create_app


def login(client: TestClient) -> None:
    response = client.post("/login", data={"submitted_password": "test"}, follow_redirects=False)
    assert response.status_code == 303


def test_auth_is_required(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_no_bell_round_trip_preserves_yaml(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    target = date(2027, 2, 2)
    response = client.post(
        "/calendar",
        data={"selected": target.isoformat(), "schedule_name": "", "no_bell_reason": "Weather closure"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_config(config_tree).calendar.no_bell_dates[target] == "Weather closure"
    assert "Worked example" in (config_tree / "calendar.yaml").read_text(encoding="utf-8")


def test_manual_requires_signed_confirmation(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    response = client.post("/manual/fire", data={"confirm_token": "invented"})
    assert response.status_code == 400
    prepared = client.post(
        "/manual/prepare",
        data={"sound": "class-bell.wav", "zone": "indoors"},
    )
    assert prepared.status_code == 200
    assert "Yes — ring now" in prepared.text


def test_out_of_hours_manual_is_blocked_without_override(config_tree: Path, monkeypatch) -> None:
    class EarlyMorning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 2, 2, 3, 0, tzinfo=tz or ZoneInfo("America/Denver"))

    monkeypatch.setattr("bell.web.datetime", EarlyMorning)
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    prepared = client.post(
        "/manual/prepare",
        data={"sound": "class-bell.wav", "zone": "indoors"},
    )
    token = prepared.text.split('name="confirm_token" value="', 1)[1].split('"', 1)[0]
    result = client.post("/manual/fire", data={"confirm_token": token}, follow_redirects=False)
    assert result.status_code == 303
    assert "outside%20allowed%20bell%20hours" in result.headers["location"]


def test_automation_api_is_authenticated_emergency_scoped_and_idempotent(
    config_tree: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BELL_API_KEY", "normal-key")
    monkeypatch.setenv("BELL_EMERGENCY_API_KEY", "emergency-key")
    config = load_config(config_tree)
    config.settings.state_dir = config_tree.parent / "api-state"
    calls: list[str] = []
    scheduler = BellScheduler(
        config, lambda event, _config, source: calls.append(f"{source}:{event.zone}")
    )
    client = TestClient(create_app(config_tree, password="test", scheduler=scheduler))
    payload = {
        "sound": "class-bell.wav",
        "zone": "everywhere",
        "label": "Lockdown drill",
        "priority": 100,
        "override_hours": True,
    }
    assert client.post("/api/v1/trigger", json=payload).status_code == 401
    forbidden = client.post(
        "/api/v1/trigger",
        json=payload,
        headers={"X-Bell-API-Key": "normal-key", "Idempotency-Key": "drill-1"},
    )
    assert forbidden.status_code == 403
    first = client.post(
        "/api/v1/trigger",
        json=payload,
        headers={"X-Bell-API-Key": "emergency-key", "Idempotency-Key": "drill-1"},
    )
    assert first.status_code == 200 and first.json()["status"] == "success"
    replay = client.post(
        "/api/v1/trigger",
        json=payload,
        headers={"X-Bell-API-Key": "emergency-key", "Idempotency-Key": "drill-1"},
    )
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert calls == ["Automation API:everywhere"]
