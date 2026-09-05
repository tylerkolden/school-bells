from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_web import hidden, login

from bell.acceptance import AcceptanceStore, ReceiverEvidence, zone_fingerprint
from bell.config import load_config
from bell.web import create_app


def evidence(**changes):
    return ReceiverEvidence.model_validate({
        "receiver_id": "Office 1", "model": "T31P", "firmware": "test-version",
        "kind": "phone", "provisioning_owner": "Provider", "call_policy": "Barge 0",
        "observer": "Office staff", "idle": "pass", "inbound": "pass", "outbound": "pass",
        "reprovision": "pass", "emergency": "pass", "emergency_path": "Dedicated office speaker",
        **changes,
    })


def test_relevant_changes_and_age_invalidate_evidence(config_tree: Path):
    cfg = load_config(config_tree)
    now = datetime.now(ZoneInfo("America/Denver"))
    store = AcceptanceStore(cfg.state_path / "evidence.sqlite3")
    fingerprint = zone_fingerprint(cfg, "indoors")
    store.record("indoors", fingerprint, evidence(), now)
    assert store.history(cfg, now)[0]["status"] == "Current evidence"
    cfg.settings.school_name = "New display name"
    assert zone_fingerprint(cfg, "indoors") == fingerprint
    cfg.zone_map["indoors"].channel = 22
    assert store.history(cfg, now)[0]["status"] == "Needs recheck"
    cfg.zone_map["indoors"].channel = 23
    assert store.history(cfg, now + timedelta(days=91))[0]["status"] == "Needs recheck"
    store.record("indoors", fingerprint, evidence(inbound="fail"), now)
    records = store.history(cfg, now)
    assert records[0]["latest"] and records[0]["status"] == "Failed checks"
    assert not records[1]["latest"]
    assert len(records) == 2


def test_phone_cannot_bypass_call_tests():
    with pytest.raises(ValidationError):
        evidence(inbound="not_applicable")
    assert evidence(emergency="not_tested").result == "Incomplete"
    with pytest.raises(ValidationError):
        evidence(emergency="not_applicable")


def test_evidence_requires_auth_csrf_current_contract(config_tree: Path):
    client = TestClient(create_app(config_tree, password="test"))
    unauthenticated = client.post("/commissioning/record", data={}, follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert "/login" in unauthenticated.headers["location"]
    login(client)
    page = client.get("/commissioning")
    fields = {**evidence().model_dump(), "zone": "indoors", "csrf": hidden(page, "csrf"),
              "receiver_fingerprint": zone_fingerprint(load_config(config_tree), "indoors")}
    assert client.post("/commissioning/record", data={**fields, "csrf": "wrong"}).status_code == 403
    assert client.post("/commissioning/record", data={**fields, "receiver_fingerprint": "stale"}).status_code == 409
    assert client.post("/commissioning/record", data=fields, follow_redirects=False).status_code == 303
    result = client.get("/commissioning")
    assert "Office 1" in result.text and "Current evidence" in result.text
    assert client.post("/commissioning/record", data={**fields, "inbound": "not_applicable"}).status_code == 400


def test_speaker_applicability_and_future_evidence(config_tree: Path):
    speaker = evidence(kind="speaker", inbound="not_applicable", outbound="not_applicable")
    assert speaker.result == "Current evidence"
    with pytest.raises(ValidationError):
        evidence(kind="speaker")
    cfg = load_config(config_tree)
    now = datetime.now(ZoneInfo("America/Denver"))
    store = AcceptanceStore(cfg.state_path / "evidence.sqlite3")
    store.record("indoors", zone_fingerprint(cfg, "indoors"), speaker, now + timedelta(days=1))
    assert store.history(cfg, now)[0]["status"] == "Needs recheck"
    before = zone_fingerprint(cfg, "indoors")
    cfg.destination_map[cfg.zone_map["indoors"].destinations[0]].port = 602
    assert zone_fingerprint(cfg, "indoors") != before
