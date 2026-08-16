from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from bell.config import load_config
from bell.web import create_app


def hidden(response, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', response.text)
    assert match
    return match.group(1)


def login(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"submitted_password": "test", "csrf": hidden(page, "csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303


def event_form(page, *, original: str = "Regular Day", name: str = "Regular Day") -> dict:
    return {
        "csrf": hidden(page, "csrf"),
        "config_hash": hidden(page, "config_hash"),
        "original_name": original,
        "schedule_name": name,
        "event_time": ["08:10", "14:45"],
        "event_label": ["Opening bell", "Pack up"],
        "event_sound": ["class-bell.wav", "dismissal-bell.wav"],
        "event_zone": ["indoors", "outdoors"],
        "event_pre_tone": ["prayer.wav", ""],
        "event_repeat_count": ["2", "1"],
        "event_repeat_interval": ["1.5", "0"],
        "event_priority": ["60", "40"],
        "event_busy_policy": ["queue", "skip"],
    }


def test_builder_requires_auth_and_renders_all_controls(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    assert client.get("/schedules", follow_redirects=False).status_code == 303
    assert client.get("/sounds/class-bell.wav", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/schedules?selected=Regular%20Day")
    assert page.status_code == 200
    for field in (
        "event_time",
        "event_sound",
        "event_zone",
        "event_repeat_count",
        "event_priority",
        "event_pre_tone",
        "event_busy_policy",
    ):
        assert f'name="{field}"' in page.text
    assert "Save &amp; activate" in page.text
    assert "Reload saved version" in page.text
    assert 'class="builder-main"' in page.text
    sound = client.get("/sounds/class-bell.wav")
    assert sound.status_code == 200
    assert sound.headers["content-type"] in {"audio/wav", "audio/x-wav"}
    assert sound.headers["cache-control"] == "no-store"
    assert client.get("/sounds/not-there.wav").status_code == 404


def test_save_is_validated_backed_up_activated_and_conflict_safe(config_tree: Path) -> None:
    reloads: list[str] = []
    client = TestClient(
        create_app(config_tree, password="test", reload_callback=lambda: reloads.append("reload"))
    )
    login(client)
    page = client.get("/schedules?selected=Regular%20Day")
    form = event_form(page)
    response = client.post("/schedules/save", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/schedules?selected=Regular+Day")
    schedule = load_config(config_tree).schedule_map["Regular Day"]
    assert [event.label for event in schedule.events] == ["Opening bell", "Pack up"]
    assert schedule.events[0].pre_tone == "prayer.wav"
    assert schedule.events[0].repeat_count == 2
    assert schedule.events[0].repeat_interval_seconds == 1.5
    assert schedule.events[0].priority == 60
    assert schedule.events[0].busy_policy == "queue"
    assert reloads == ["reload"]
    backups = list((config_tree.parent / "state" / "config-backups").glob("schedules-*.yaml"))
    assert len(backups) == 1
    stale = client.post("/schedules/save", data=form)
    assert stale.status_code == 409
    assert reloads == ["reload"]


def test_builder_rejects_duplicate_times_and_tampered_choices(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    page = client.get("/schedules?selected=Regular%20Day")
    duplicate = event_form(page)
    duplicate["event_time"] = ["08:10", "08:10"]
    response = client.post("/schedules/save", data=duplicate)
    assert response.status_code == 400
    assert "duplicate event times" in response.text

    page = client.get("/schedules?selected=Regular%20Day")
    tampered = event_form(page)
    tampered["event_sound"] = ["../secret.wav", "dismissal-bell.wav"]
    assert client.post("/schedules/save", data=tampered).status_code == 400

    page = client.get("/schedules?selected=Regular%20Day")
    tampered = event_form(page)
    tampered["event_zone"] = ["unknown", "outdoors"]
    assert client.post("/schedules/save", data=tampered).status_code == 400

    page = client.get("/schedules?selected=Regular%20Day")
    tampered = event_form(page)
    tampered["event_repeat_count"] = ["5", "1"]
    response = client.post("/schedules/save", data=tampered)
    assert response.status_code == 400
    assert "safety maximum" in response.text


def test_create_duplicate_and_safe_delete(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    duplicate_page = client.get("/schedules?copy=Regular%20Day")
    assert duplicate_page.status_code == 200
    assert 'value="Regular Day Copy"' in duplicate_page.text
    form = event_form(duplicate_page, original="", name="Storm Day")
    created = client.post("/schedules/save", data=form, follow_redirects=False)
    assert created.status_code == 303
    assert "Storm Day" in load_config(config_tree).schedule_map

    page = client.get("/schedules?selected=Storm%20Day")
    deleted = client.post(
        "/schedules/delete",
        data={
            "csrf": hidden(page, "csrf"),
            "config_hash": hidden(page, "config_hash"),
            "schedule_name": "Storm Day",
        },
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert "Storm Day" not in load_config(config_tree).schedule_map

    page = client.get("/schedules?selected=Regular%20Day")
    protected = client.post(
        "/schedules/delete",
        data={
            "csrf": hidden(page, "csrf"),
            "config_hash": hidden(page, "config_hash"),
            "schedule_name": "Regular Day",
        },
    )
    assert protected.status_code == 409
    assert "still assigned" in protected.text


def test_failed_live_reload_restores_previous_schedule(config_tree: Path) -> None:
    original = (config_tree / "schedules.yaml").read_bytes()
    attempts = 0

    def flaky_reload() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated activation failure")

    client = TestClient(create_app(config_tree, password="test", reload_callback=flaky_reload))
    login(client)
    page = client.get("/schedules?selected=Regular%20Day")
    response = client.post("/schedules/save", data=event_form(page))
    assert response.status_code == 500
    assert "previous configuration was restored" in response.text
    assert (config_tree / "schedules.yaml").read_bytes() == original
    assert attempts == 2
    load_config(config_tree)
