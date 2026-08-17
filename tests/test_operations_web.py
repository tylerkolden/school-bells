from __future__ import annotations

import base64
import re
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from bell.auth import AuthStore
from bell.config import BellEvent, load_config
from bell.scheduler import BellScheduler, PlannedEvent
from bell.web import create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def hidden(response, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def login(client: TestClient, password: str = "test", username: str = "admin") -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": username,
            "submitted_password": password,
            "csrf": hidden(page, "csrf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_today_snapshot_pause_resume_and_stop(config_tree: Path, monkeypatch) -> None:
    class SchoolMorning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 2, 2, 7, 45, tzinfo=tz or ZoneInfo("America/Denver"))

    monkeypatch.setattr("bell.web.datetime", SchoolMorning)
    cancelled: list[str] = []
    health = {
        "ready": True,
        "readiness_reasons": [],
        "last_fire": {"result": "success", "label": "Arrival"},
        "active_page": {"zone": "indoors", "label": "Test page"},
    }
    client = TestClient(
        create_app(
            config_tree,
            password="test",
            health_provider=lambda: health,
            cancel_callback=lambda reason: cancelled.append(reason) is None,
        )
    )
    login(client)

    page = client.get("/")
    snapshot = client.get("/operations/snapshot").json()
    assert page.status_code == 200
    assert "Next bell" in page.text
    assert snapshot["server_time"].startswith("2027-02-02T07:45:00")
    assert len(snapshot["upcoming"]) == 5
    assert snapshot["active_page"]["zone"] == "indoors"

    pause = client.post(
        "/operations/pause",
        data={
            "duration": "30",
            "reason": "Fire drill",
            "config_hash": hidden(page, "config_hash"),
            "csrf": hidden(page, "csrf"),
        },
        follow_redirects=False,
    )
    assert pause.status_code == 303
    paused = load_config(config_tree)
    assert paused.safety.pause_reason == "Fire drill"
    assert paused.safety.pause_until.isoformat().startswith("2027-02-02T08:15:00")  # type: ignore[union-attr]
    assert cancelled == ["bells paused: Fire drill"]

    page = client.get("/")
    resume = client.post(
        "/operations/pause",
        data={
            "duration": "resume",
            "reason": "",
            "config_hash": hidden(page, "config_hash"),
            "csrf": hidden(page, "csrf"),
        },
        follow_redirects=False,
    )
    assert resume.status_code == 303
    assert load_config(config_tree).safety.pause_until is None

    stop = client.post(
        "/operations/stop",
        data={"csrf": hidden(client.get("/"), "csrf")},
        follow_redirects=False,
    )
    assert stop.status_code == 303
    assert cancelled[-1] == "front-office operator stop"


def test_calendar_bulk_export_history_and_commissioning(config_tree: Path) -> None:
    config = load_config(config_tree)
    scheduler = BellScheduler(config, lambda *_args: None)
    client = TestClient(create_app(config_tree, password="test", scheduler=scheduler))
    login(client)

    calendar = client.get("/calendar?selected=2027-03-01")
    response = client.post(
        "/calendar/bulk",
        data={
            "start": "2027-03-01",
            "end": "2027-03-03",
            "bulk_action": "no_bells",
            "schedule_name": "",
            "no_bell_reason": "Spring break",
            "config_hash": hidden(calendar, "config_hash"),
            "csrf": hidden(calendar, "csrf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/calendar?selected=2027-03-01&month=2027-03"
    changed = load_config(config_tree)
    assert changed.calendar.no_bell_dates[date(2027, 3, 2)] == "Spring break"
    export = client.get("/calendar/export.csv?year=2027")
    assert export.status_code == 200
    assert "2027-03-02,Tuesday,,Spring break,0," in export.text

    now = datetime(2027, 2, 2, 8, 0, tzinfo=ZoneInfo("America/Denver"))
    planned = PlannedEvent(
        BellEvent(
            time=time(8, 0),
            sound="class-bell.wav",
            zone="indoors",
            label="First bell",
        ),
        "Regular Day",
        now,
    )
    scheduler.state.record_once(now.date(), planned.key, "success", "sent", now, planned)
    history = client.get("/history?result=success&zone=indoors")
    assert history.status_code == 200
    assert "First bell" in history.text
    assert "Regular Day" in client.get("/history/export.csv").text

    commissioning = client.get("/commissioning")
    accepted = client.post(
        "/commissioning/confirm",
        data={
            "zone": "indoors",
            "observer": "Facilities Manager",
            "note": "Phones and horn audible",
            "heard": "true",
            "csrf": hidden(commissioning, "csrf"),
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert scheduler.state.zone_confirmations()["indoors"]["observer"] == "Facilities Manager"


def test_branding_upload_and_recovery_downloads(config_tree: Path, monkeypatch) -> None:
    def fake_normalize(_source: Path, destination: Path) -> None:
        destination.write_bytes(PNG)

    monkeypatch.setattr("bell.web.normalize_logo", fake_normalize)
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    setup = client.get("/setup")
    response = client.post(
        "/setup/branding/save",
        data={
            "school_name": "Our Lady of Victory",
            "console_subtitle": "Bell Operations",
            "config_hash": hidden(setup, "config_hash"),
            "csrf": hidden(setup, "csrf"),
        },
        files={"logo_file": ("crest.png", PNG, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_config(config_tree).settings.school_name == "Our Lady of Victory"
    assert client.get("/branding/logo.png").content == PNG
    assert "Our Lady of Victory" in client.get("/").text

    recovery = client.get("/recovery")
    exported = client.post("/recovery/export", data={"csrf": hidden(recovery, "csrf")})
    support = client.post("/recovery/support", data={"csrf": hidden(recovery, "csrf")})
    assert exported.status_code == 200
    assert exported.content.startswith(b"\x1f\x8b")
    assert support.status_code == 200
    assert support.content.startswith(b"PK")


def test_operator_role_cannot_access_administration(config_tree: Path) -> None:
    state_path = load_config(config_tree).state_path
    store = AuthStore(state_path / "auth" / "users.json", "bootstrap-only")
    store.set_password("admin", "admin", "administrator-password")
    store.set_password("operator", "operator", "operator-password")
    client = TestClient(create_app(config_tree, password="bootstrap-only"))
    login(client, "operator-password", "operator")

    assert client.get("/calendar").status_code == 200
    assert client.get("/manual").status_code == 200
    denied = client.get("/setup")
    assert denied.status_code == 403
    assert "Administrator access is required" in denied.text
    assert "Setup" not in client.get("/").text
