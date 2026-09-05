from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from bell import __version__
from bell.config import load_config
from bell.scheduler import BellScheduler
from bell.update import load_update_status
from bell.web import APITrigger, create_app


def hidden(response, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def csrf(client: TestClient, path: str = "/") -> str:
    return hidden(client.get(path), "csrf")


def login(client: TestClient) -> None:
    token = hidden(client.get("/login"), "csrf")
    response = client.post(
        "/login",
        data={"submitted_password": "test", "csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_auth_is_required(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert len(response.headers["x-request-id"]) == 16


def test_no_bell_round_trip_preserves_yaml(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    target = date(2027, 2, 2)
    calendar = client.get(f"/calendar?selected={target.isoformat()}")
    response = client.post(
        "/calendar",
        data={
            "selected": target.isoformat(),
            "schedule_name": "",
            "no_bell_reason": "Weather closure",
            "calendar_action": "no_bells",
            "config_hash": hidden(calendar, "config_hash"),
            "csrf": hidden(calendar, "csrf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_config(config_tree).calendar.no_bell_dates[target] == "Weather closure"
    assert "Worked example" in (config_tree / "calendar.yaml").read_text(encoding="utf-8")


def test_manual_requires_signed_confirmation(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    csrf_token = csrf(client, "/manual")
    response = client.post(
        "/manual/fire", data={"confirm_token": "invented", "csrf": csrf_token}
    )
    assert response.status_code == 400
    prepared = client.post(
        "/manual/prepare",
        data={"sound": "class-bell.wav", "zone": "indoors", "csrf": csrf_token},
    )
    assert prepared.status_code == 200
    assert "Yes, play now" in prepared.text


def test_out_of_hours_manual_is_blocked_without_override(config_tree: Path, monkeypatch) -> None:
    class EarlyMorning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 2, 2, 3, 0, tzinfo=tz or ZoneInfo("America/Denver"))

    monkeypatch.setattr("bell.web.datetime", EarlyMorning)
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    csrf_token = csrf(client, "/manual")
    prepared = client.post(
        "/manual/prepare",
        data={"sound": "class-bell.wav", "zone": "indoors", "csrf": csrf_token},
    )
    token = hidden(prepared, "confirm_token")
    result = client.post(
        "/manual/fire",
        data={"confirm_token": token, "csrf": hidden(prepared, "csrf")},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert "outside+allowed+bell+hours" in result.headers["location"]


def test_manual_explains_after_hours_and_links_local_receiver(
    config_tree: Path, monkeypatch
) -> None:
    class LateNight(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 2, 2, 23, 0, tzinfo=tz or ZoneInfo("America/Denver"))

    monkeypatch.setattr("bell.web.datetime", LateNight)
    monkeypatch.setenv("BELL_RECEIVER_DASHBOARD_URL", "http://localhost:9000/")
    client = TestClient(create_app(config_tree, password="test"))
    login(client)

    page = client.get("/manual?message=transmission%20completed")

    assert page.status_code == 200
    assert "It is outside normal bell hours" in page.text
    assert "select the logged emergency-hours override" in page.text
    assert 'href="http://localhost:9000/"' in page.text
    assert "View local receiver result" in page.text


def test_invalid_receiver_dashboard_url_is_not_rendered(
    config_tree: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BELL_RECEIVER_DASHBOARD_URL", "javascript:alert(1)")
    client = TestClient(create_app(config_tree, password="test"))
    login(client)

    page = client.get("/manual?message=transmission%20completed")

    assert page.status_code == 200
    assert "View local receiver result" not in page.text


def test_update_requires_auth_csrf_and_two_step_confirmation(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    assert client.get("/updates", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/updates")
    assert page.status_code == 200
    assert "Updates are never automatic" in page.text
    assert client.post("/updates/check", data={"csrf": "wrong"}).status_code == 403
    queued = client.post(
        "/updates/check",
        data={"csrf": hidden(page, "csrf")},
        follow_redirects=False,
    )
    assert queued.status_code == 303
    status = load_update_status(config_tree.parent / "state")
    assert status["phase"] == "idle"
    request = config_tree.parent / "state" / "update" / "request.json"
    assert json.loads(request.read_text(encoding="utf-8"))["action"] == "check"


def test_updates_are_disabled_safely_for_local_docker(config_tree: Path, monkeypatch) -> None:
    monkeypatch.setenv("BELL_OTA_ENABLED", "false")
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    page = client.get("/updates")
    assert page.status_code == 200
    assert "Production OTA is disabled in Docker" in page.text
    assert __version__ in page.text
    assert "Check for updates" not in page.text
    response = client.post(
        "/updates/check",
        data={"csrf": hidden(page, "csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert not (config_tree.parent / "state" / "update" / "request.json").exists()


def test_update_confirmation_is_bound_to_checked_release(config_tree: Path) -> None:
    state = config_tree.parent / "state" / "update"
    state.mkdir(parents=True)
    digest = "sha256:" + "a" * 64
    (state / "status.json").write_text(
        json.dumps(
            {
                "phase": "update_available",
                "message": "Release available",
                "installed_version": "0.1.0",
                "release": {
                    "tag": "v0.2.0",
                    "digest": digest,
                    "published_at": "2026-08-15T12:00:00Z",
                    "notes": "Hardened update",
                },
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    page = client.get("/updates")
    prepared = client.post(
        "/updates/prepare",
        data={"tag": "v0.2.0", "digest": digest, "csrf": hidden(page, "csrf")},
    )
    assert prepared.status_code == 200
    assert "Final confirmation" in prepared.text
    result = client.post(
        "/updates/install",
        data={
            "confirm_token": hidden(prepared, "confirm_token"),
            "csrf": hidden(prepared, "csrf"),
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    request = json.loads((state / "request.json").read_text(encoding="utf-8"))
    assert request["action"] == "install"
    assert request["tag"] == "v0.2.0"
    assert request["digest"] == digest


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
    conflict = client.post(
        "/api/v1/trigger",
        json={**payload, "zone": "indoors"},
        headers={"X-Bell-API-Key": "emergency-key", "Idempotency-Key": "drill-1"},
    )
    assert conflict.status_code == 409


def test_automation_api_rejects_sound_path_escape(config_tree: Path, monkeypatch) -> None:
    monkeypatch.setenv("BELL_API_KEY", "normal-key")
    client = TestClient(create_app(config_tree, password="test"))
    response = client.post(
        "/api/v1/trigger",
        json={"sound": str((config_tree.parent / "outside.wav").resolve()), "zone": "indoors"},
        headers={"X-Bell-API-Key": "normal-key", "Idempotency-Key": "path-escape"},
    )
    assert response.status_code == 422


def test_automation_fields_reject_control_characters() -> None:
    for field in ("sound", "zone", "label"):
        values = {"sound": "bell.wav", "zone": "indoors", "label": "Automation"}
        values[field] += "\nforged-entry"
        try:
            APITrigger(**values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{field} accepted a log control character")


def test_form_posts_require_csrf(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    response = client.post(
        "/manual/prepare", data={"sound": "class-bell.wav", "zone": "indoors"}
    )
    assert response.status_code == 422
    response = client.post(
        "/manual/prepare",
        data={"sound": "class-bell.wav", "zone": "indoors", "csrf": "wrong"},
    )
    assert response.status_code == 403


def test_login_is_rate_limited(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    token = hidden(client.get("/login"), "csrf")
    for _ in range(5):
        response = client.post(
            "/login",
            data={"submitted_password": "wrong", "csrf": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
    response = client.post("/login", data={"submitted_password": "test", "csrf": token})
    assert response.status_code == 429


def test_calendar_can_restore_default_and_rejects_stale_edit(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    target = date(2027, 1, 15)
    page = client.get(f"/calendar?selected={target.isoformat()}")
    form = {
        "selected": target.isoformat(),
        "schedule_name": "",
        "no_bell_reason": "",
        "calendar_action": "default",
        "config_hash": hidden(page, "config_hash"),
        "csrf": hidden(page, "csrf"),
    }
    response = client.post("/calendar", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert target not in load_config(config_tree).calendar.no_bell_dates
    response = client.post("/calendar", data=form, follow_redirects=False)
    assert response.status_code == 409
