from __future__ import annotations

import re
import struct
from pathlib import Path

from fastapi.testclient import TestClient

from bell.config import load_config
from bell.probe import load_capture
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


def captured_packet(channel: int, sequence: int) -> bytes:
    return (
        struct.pack("!BBHII", 0x90, 0x80 if sequence == 1 else 0, sequence, sequence * 160, 99)
        + struct.pack("!HH", 0xABCD, 1)
        + bytes((0x44, channel, 0x55, 0))
        + b"voice payload must not persist"
    )


def enable_capture_mode(config_tree: Path) -> None:
    settings = config_tree / "settings.yaml"
    text = settings.read_text(encoding="utf-8")
    text = text.replace("kill_switch_enabled: false", "kill_switch_enabled: true")
    settings.write_text(text, encoding="utf-8")


def test_guided_capture_requires_auth_and_kill_switch(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    assert client.get("/setup/poly-calibration", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/setup/poly-calibration")
    assert page.status_code == 200
    assert "Enable the kill switch first" in page.text
    response = client.post(
        "/setup/poly-calibration/capture",
        data={
            "csrf": hidden(page, "csrf"),
            "destination_name": "all",
            "known_channel": "23",
        },
    )
    assert response.status_code == 409


def test_three_header_only_captures_activate_persisted_spec(
    config_tree: Path, monkeypatch
) -> None:
    enable_capture_mode(config_tree)
    channels = iter((23, 24, 25))

    def fake_capture(_group: str, _port: int, _interface: str, count: int):
        channel = next(channels)
        packets = [captured_packet(channel, sequence) for sequence in range(1, count + 1)]
        return packets, [float(index) for index in range(count)]

    monkeypatch.setattr("bell.web.capture_rtp", fake_capture)
    reloads: list[str] = []
    client = TestClient(
        create_app(
            config_tree,
            password="test",
            reload_callback=lambda: reloads.append("reload"),
        )
    )
    login(client)
    for channel in (23, 24, 25):
        page = client.get("/setup/poly-calibration")
        response = client.post(
            "/setup/poly-calibration/capture",
            data={
                "csrf": hidden(page, "csrf"),
                "destination_name": "all",
                "known_channel": str(channel),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    review = client.get("/setup/poly-calibration")
    assert "Verified candidate ready" in review.text
    assert "channels 23, 24, 25" in review.text
    workspace = config_tree.parent / "state" / "poly-calibration"
    for channel in (23, 24, 25):
        stored = load_capture(workspace / f"channel-{channel}.bin")
        assert len(stored) == 32
        assert all(len(packet) == 20 and b"voice" not in packet for packet in stored)

    missing_confirmation = client.post(
        "/setup/poly-calibration/activate",
        data={
            "csrf": hidden(review, "csrf"),
            "config_hash": hidden(review, "config_hash"),
        },
    )
    assert missing_confirmation.status_code == 400
    activated = client.post(
        "/setup/poly-calibration/activate",
        data={
            "csrf": hidden(review, "csrf"),
            "config_hash": hidden(review, "config_hash"),
            "confirm_evidence": "true",
        },
        follow_redirects=False,
    )
    assert activated.status_code == 303
    assert reloads == ["reload"]
    config = load_config(config_tree)
    assert config.settings.poly_group_page_calibration is not None
    assert config.settings.poly_group_page_calibration.captured_channels == [23, 24, 25]
    assert config.poly_spec is not None
    assert config.poly_spec.mappings[1] == (1, "channel")
    evidence = (
        workspace
        / "verified"
        / config.settings.poly_group_page_calibration.evidence_id
    )
    assert (evidence / "manifest.json").is_file()
    assert (evidence / "calibration.json").is_file()
    assert sorted(path.name for path in evidence.glob("channel-*.bin")) == [
        "channel-23.bin",
        "channel-24.bin",
        "channel-25.bin",
    ]
    assert len(list(workspace.glob("channel-*.bin"))) == 3


def test_capture_timeout_is_explained_without_storing_evidence(
    config_tree: Path, monkeypatch
) -> None:
    enable_capture_mode(config_tree)

    def timeout(*_args):
        raise TimeoutError

    monkeypatch.setattr("bell.web.capture_rtp", timeout)
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    page = client.get("/setup/poly-calibration")
    response = client.post(
        "/setup/poly-calibration/capture",
        data={
            "csrf": hidden(page, "csrf"),
            "destination_name": "all",
            "known_channel": "23",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "No+page+packets+arrived" in response.headers["location"]
    workspace = config_tree.parent / "state" / "poly-calibration"
    assert not list(workspace.glob("channel-*.bin"))
