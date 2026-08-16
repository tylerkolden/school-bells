from __future__ import annotations

from pathlib import Path

import pytest

from bell.config import BellEvent, ConfigLoadError, Destination, PolyCalibration, load_config


def test_example_config_is_complete(config_tree: Path) -> None:
    config = load_config(config_tree)
    assert config.settings.timezone == "America/Denver"
    assert {zone.channel for zone in config.zones} == {23, 24, 25}
    assert set(config.schedule_map) == {"Regular Day", "Mass Day", "Half Day"}
    assert {item.protocol for item in config.destinations} == {"multicast", "sip", "http"}


def test_cross_validation_collects_multiple_errors(config_tree: Path) -> None:
    schedules = config_tree / "schedules.yaml"
    text = schedules.read_text(encoding="utf-8")
    text = text.replace("sound: class-bell.wav, zone: indoors", "sound: missing.wav, zone: nowhere", 1)
    schedules.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigLoadError) as captured:
        load_config(config_tree)
    message = str(captured.value)
    assert "unknown zone 'nowhere'" in message
    assert "missing or unreadable" in message


def test_out_of_hours_event_is_rejected(config_tree: Path) -> None:
    schedules = config_tree / "schedules.yaml"
    text = schedules.read_text(encoding="utf-8").replace('time: "08:00"', 'time: "03:00"', 1)
    schedules.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="outside safety window"):
        load_config(config_tree)


def test_repeat_safety_cap_is_validated(config_tree: Path) -> None:
    schedules = config_tree / "schedules.yaml"
    text = schedules.read_text(encoding="utf-8").replace(
        "label: First bell}", "label: First bell, repeat_count: 5}", 1
    )
    schedules.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigLoadError, match="exceeds safety max_repeats"):
        load_config(config_tree)


def test_protocol_configuration_rejects_credential_leaks_and_tls_downgrade() -> None:
    with pytest.raises(ValueError, match="plain HTTP"):
        Destination(name="hook", protocol="http", port=80, webhook_url="http://device/trigger")
    with pytest.raises(ValueError, match="embedded"):
        Destination(
            name="hook",
            protocol="http",
            port=443,
            webhook_url="https://user:password@device/trigger",
        )
    with pytest.raises(ValueError, match="Poly Group Page requires PCMU"):
        Destination(
            name="paging",
            protocol="multicast",
            port=5004,
            group="239.1.2.3",
            wire_format="poly_group_page",
            codecs=["g722"],
        )


def test_sound_names_cannot_escape_library() -> None:
    for sound in ("../secret.wav", "/tmp/secret.wav", "folder/tone.wav"):
        with pytest.raises(ValueError, match="sound library"):
            BellEvent(time="08:00", sound=sound, zone="indoors", label="Unsafe")
    with pytest.raises(ValueError, match="require sip_transport: tls"):
        Destination(
            name="sip",
            protocol="sip",
            port=5060,
            sip_uri="sips:page@example.test",
            sip_host="example.test",
        )


def test_poly_calibration_requires_complete_unique_evidence() -> None:
    with pytest.raises(ValueError, match="cover every extension byte"):
        PolyCalibration(
            extension_profile_id=1,
            extension_word_count=1,
            mappings=[{"offset": 0, "source": "channel"}],
            captured_channels=[23, 24, 25],
            capture_sha256=["a" * 64, "b" * 64, "c" * 64],
            captured_at="2026-08-15T12:00:00Z",
            evidence_id="20260815T120000000000Z-aaaaaaaaaaaa",
        )
