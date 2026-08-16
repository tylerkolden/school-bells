from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from bell.audio import AudioInfo
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


def tokens(client: TestClient) -> dict[str, str]:
    page = client.get("/setup")
    assert page.status_code == 200
    return {"csrf": hidden(page, "csrf"), "config_hash": hidden(page, "config_hash")}


def standing_form(auth: dict[str, str], *, index: int = -1, label: str = "Assembly") -> dict:
    return {
        **auth,
        "standing_index": str(index),
        "event_time": "11:30",
        "event_label": label,
        "event_sound": "class-bell.wav",
        "event_zone": "indoors",
        "event_pre_tone": "",
        "event_repeat_count": "1",
        "event_repeat_interval": "0",
        "event_priority": "50",
        "event_busy_policy": "skip",
        "enabled": "on",
    }


def destination_form(
    auth: dict[str, str],
    *,
    name: str,
    original: str = "",
    timeout: str = "5",
) -> dict:
    return {
        **auth,
        "original_name": original,
        "destination_name": name,
        "protocol": "http",
        "port": "443",
        "ttl": "1",
        "wire_format": "",
        "codecs": ["pcmu"],
        "sip_transport": "udp",
        "webhook_url": "https://paging.example.invalid/trigger",
        "healthcheck_url": "https://paging.example.invalid/health",
        "timeout_seconds": timeout,
        "retries": "1",
    }


def test_setup_requires_auth_and_exposes_every_managed_domain(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    assert client.get("/setup", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/setup")
    assert page.status_code == 200
    for heading in (
        "Standing items",
        "Sounds",
        "Zones",
        "Destinations",
        "Safety &amp; settings",
    ):
        assert heading in page.text
    assert "All channels assigned" in page.text
    assert "Multicast IPv4 address" in page.text
    assert "Multicast network contract" in page.text


def test_standing_item_create_update_delete_round_trip(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    original_count = len(load_config(config_tree).standing_items)
    created = client.post(
        "/setup/standing/save", data=standing_form(tokens(client)), follow_redirects=False
    )
    assert created.status_code == 303
    assert load_config(config_tree).standing_items[-1].label == "Assembly"

    updated = client.post(
        "/setup/standing/save",
        data=standing_form(tokens(client), index=original_count, label="Weekly assembly"),
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert load_config(config_tree).standing_items[-1].label == "Weekly assembly"

    auth = tokens(client)
    deleted = client.post(
        "/setup/standing/delete",
        data={**auth, "standing_index": str(original_count)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert len(load_config(config_tree).standing_items) == original_count


def test_zone_crud_and_dependency_guards(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    auth = tokens(client)
    removed = client.post(
        "/setup/zones/delete",
        data={**auth, "zone_name": "everywhere"},
        follow_redirects=False,
    )
    assert removed.status_code == 303

    auth = tokens(client)
    created = client.post(
        "/setup/zones/save",
        data={
            **auth,
            "original_name": "",
            "zone_name": "assembly",
            "channel": "25",
            "description": "Assembly hall",
            "destinations": ["all"],
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert load_config(config_tree).zone_map["assembly"].channel == 25

    auth = tokens(client)
    updated = client.post(
        "/setup/zones/save",
        data={
            **auth,
            "original_name": "assembly",
            "zone_name": "assembly",
            "channel": "25",
            "description": "Main assembly hall",
            "destinations": ["all"],
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert load_config(config_tree).zone_map["assembly"].description == "Main assembly hall"

    auth = tokens(client)
    deleted = client.post(
        "/setup/zones/delete",
        data={**auth, "zone_name": "assembly"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert "assembly" not in load_config(config_tree).zone_map

    auth = tokens(client)
    protected = client.post(
        "/setup/zones/delete", data={**auth, "zone_name": "indoors"}
    )
    assert protected.status_code == 409
    assert "still used" in protected.text


def test_destination_create_update_delete_and_multicast_lock(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    created = client.post(
        "/setup/destinations/save",
        data=destination_form(tokens(client), name="backup-webhook"),
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert load_config(config_tree).destination_map["backup-webhook"].protocol == "http"

    updated = client.post(
        "/setup/destinations/save",
        data=destination_form(
            tokens(client), name="backup-webhook", original="backup-webhook", timeout="7"
        ),
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert load_config(config_tree).destination_map["backup-webhook"].timeout_seconds == 7

    auth = tokens(client)
    deleted = client.post(
        "/setup/destinations/delete",
        data={**auth, "destination_name": "backup-webhook"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert "backup-webhook" not in load_config(config_tree).destination_map

    auth = tokens(client)
    configurable = client.post(
        "/setup/destinations/save",
        data={
            **auth,
            "original_name": "all",
            "destination_name": "all",
            "protocol": "multicast",
            "group": "239.10.20.30",
            "port": "7000",
            "ttl": "2",
            "wire_format": "plain_rtp",
            "codecs": ["pcma"],
            "timeout_seconds": "5",
            "retries": "2",
            "enabled": "on",
            "required": "on",
        },
        follow_redirects=False,
    )
    assert configurable.status_code == 303
    destination = load_config(config_tree).destination_map["all"]
    assert destination.group == "239.10.20.30"
    assert destination.port == 7000
    assert destination.codecs == ["pcmu"]

    rejected = client.post(
        "/setup/destinations/save",
        data={
            **tokens(client),
            "original_name": "all",
            "destination_name": "all",
            "protocol": "multicast",
            "group": "192.168.10.20",
            "port": "7000",
            "ttl": "2",
            "wire_format": "plain_rtp",
            "codecs": ["pcmu"],
            "timeout_seconds": "5",
            "retries": "2",
            "enabled": "on",
            "required": "on",
        },
    )
    assert rejected.status_code == 400
    destination = load_config(config_tree).destination_map["all"]
    assert destination.group == "239.10.20.30"
    assert destination.port == 7000

    invalid_port = client.post(
        "/setup/destinations/save",
        data={
            **tokens(client),
            "original_name": "all",
            "destination_name": "all",
            "protocol": "multicast",
            "group": "239.10.20.30",
            "port": "70000",
            "ttl": "2",
            "wire_format": "plain_rtp",
            "codecs": ["pcmu"],
            "timeout_seconds": "5",
            "retries": "2",
            "enabled": "on",
            "required": "on",
        },
    )
    assert invalid_port.status_code == 400
    destination = load_config(config_tree).destination_map["all"]
    assert destination.group == "239.10.20.30"
    assert destination.port == 7000
    protected = client.post(
        "/setup/destinations/delete",
        data={**tokens(client), "destination_name": "all"},
    )
    assert protected.status_code == 409


def test_sound_create_replace_delete_and_reference_guard(
    config_tree: Path, monkeypatch
) -> None:
    def fake_probe(_path: Path) -> AudioInfo:
        return AudioInfo(duration=1.0, sample_rate=8000, channels=1, peak_dbfs=-3.0)

    def fake_prep(source: Path, destination: Path, **_kwargs) -> Path:
        shutil.copy2(source, destination)
        return destination

    monkeypatch.setattr("bell.web.probe_audio", fake_probe)
    monkeypatch.setattr("bell.web.prep", fake_prep)
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    source = config_tree.parent / "sounds" / "class-bell.wav"
    created = client.post(
        "/setup/sounds/save",
        data={**tokens(client), "desired_name": "test tone", "existing_name": ""},
        files={"audio_file": ("tone.mp3", source.read_bytes(), "audio/mpeg")},
        follow_redirects=False,
    )
    assert created.status_code == 303
    target = config_tree.parent / "sounds" / "test tone.wav"
    assert target.is_file()

    replaced = client.post(
        "/setup/sounds/save",
        data={
            **tokens(client),
            "desired_name": "test tone.wav",
            "existing_name": "test tone.wav",
        },
        files={"audio_file": ("replacement.wav", b"replacement", "audio/wav")},
        follow_redirects=False,
    )
    assert replaced.status_code == 303
    assert target.read_bytes() == b"replacement"

    deleted = client.post(
        "/setup/sounds/delete",
        data={**tokens(client), "sound_name": "test tone.wav"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert not target.exists()

    protected = client.post(
        "/setup/sounds/delete",
        data={**tokens(client), "sound_name": "class-bell.wav"},
    )
    assert protected.status_code == 409
    assert "still used" in protected.text


def test_calendar_defaults_and_date_range_crud(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    auth = tokens(client)
    defaults = client.post(
        "/setup/calendar/defaults/save",
        data={
            **auth,
            "monday": "Half Day",
            "tuesday": "Regular Day",
            "wednesday": "Regular Day",
            "thursday": "Regular Day",
            "friday": "Regular Day",
            "saturday": "",
            "sunday": "",
        },
        follow_redirects=False,
    )
    assert defaults.status_code == 303
    assert load_config(config_tree).calendar.weekday_defaults["monday"] == "Half Day"

    original_count = len(load_config(config_tree).calendar.date_ranges)
    created = client.post(
        "/setup/calendar/ranges/save",
        data={
            **tokens(client),
            "range_index": "-1",
            "start": "2027-03-01",
            "end": "2027-03-05",
            "schedule_name": "Half Day",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert load_config(config_tree).calendar.date_ranges[-1].schedule == "Half Day"

    updated = client.post(
        "/setup/calendar/ranges/save",
        data={
            **tokens(client),
            "range_index": str(original_count),
            "start": "2027-03-02",
            "end": "2027-03-06",
            "schedule_name": "Mass Day",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert load_config(config_tree).calendar.date_ranges[-1].schedule == "Mass Day"

    deleted = client.post(
        "/setup/calendar/ranges/delete",
        data={**tokens(client), "range_index": str(original_count)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert len(load_config(config_tree).calendar.date_ranges) == original_count


def test_settings_update_and_stale_write_protection(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    auth = tokens(client)
    form = {
        **auth,
        "interface_ip": "127.0.0.1",
        "wire_format": "plain_rtp",
        "endpoint_check_interval_seconds": "30",
        "api_rate_limit_per_minute": "20",
        "clock_sync_required": "on",
        "max_audio_seconds": "45",
        "max_page_seconds": "180",
        "allowed_hours_start": "06:30",
        "allowed_hours_end": "17:30",
        "max_events_per_day": "40",
        "max_repeats": "4",
        "emergency_priority_threshold": "90",
        "kill_switch_until": "",
    }
    response = client.post("/setup/settings/save", data=form, follow_redirects=False)
    assert response.status_code == 303
    cfg = load_config(config_tree)
    assert cfg.settings.timezone == "America/Denver"
    assert cfg.settings.max_audio_seconds == 45
    assert cfg.settings.api_rate_limit_per_minute == 20
    stale = client.post("/setup/settings/save", data=form)
    assert stale.status_code == 409
