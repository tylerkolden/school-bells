"""Versioned receiver evidence; sender health is never proof of receiver behavior."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bell.config import BellConfig

Outcome = Literal["pass", "fail", "not_tested", "not_applicable"]


def zone_fingerprint(config: BellConfig, zone: str) -> str:
    selected = config.zone_map[zone]
    contract = {
        "zone": selected.model_dump(mode="json"),
        "destinations": [config.destination_map[name].model_dump(mode="json")
                         for name in sorted(selected.destinations)],
        "interface": config.settings.interface_ip,
        "wire_format": config.settings.wire_format,
        "calibration": config.settings.poly_group_page_calibration.model_dump(mode="json")
        if config.settings.poly_group_page_calibration else None,
        "caller": config.settings.poly_caller_id,
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


class ReceiverEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    receiver_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    firmware: str = Field(min_length=1, max_length=100)
    provisioning_owner: str = Field(min_length=1, max_length=100)
    kind: Literal["phone", "speaker"]
    call_policy: str = Field(min_length=1, max_length=300)
    observer: str = Field(min_length=1, max_length=100)
    idle: Outcome = "not_tested"
    inbound: Outcome = "not_tested"
    outbound: Outcome = "not_tested"
    reprovision: Outcome = "not_tested"
    emergency: Outcome = "not_tested"
    emergency_path: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid_applicability(self) -> ReceiverEvidence:
        if "not_applicable" in (self.idle, self.reprovision, self.emergency):
            raise ValueError("Playback, reprovision/reboot and emergency checks always apply")
        if self.kind == "phone" and "not_applicable" in (self.inbound, self.outbound):
            raise ValueError("Phones require incoming and outgoing call tests")
        if self.kind == "speaker" and (self.inbound != "not_applicable" or self.outbound != "not_applicable"):
            raise ValueError("Speakers must mark telephone-call checks not applicable")
        return self

    @property
    def result(self) -> str:
        outcomes = (self.idle, self.inbound, self.outbound, self.reprovision, self.emergency)
        if "fail" in outcomes:
            return "Failed checks"
        if "not_tested" in outcomes:
            return "Incomplete"
        return "Current evidence"


class AcceptanceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS receiver_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT, zone TEXT NOT NULL,
                receiver_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                recorded_at TEXT NOT NULL, evidence TEXT NOT NULL)""")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def record(self, zone: str, fingerprint: str, evidence: ReceiverEvidence, now: datetime) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO receiver_evidence
                (zone,receiver_id,fingerprint,recorded_at,evidence) VALUES (?,?,?,?,?)""",
                (zone, evidence.receiver_id, fingerprint, now.isoformat(), evidence.model_dump_json()),
            )

    def history(self, config: BellConfig, now: datetime) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,zone,receiver_id,fingerprint,recorded_at,evidence "
                "FROM receiver_evidence ORDER BY id DESC LIMIT 1000"
            ).fetchall()
        records = []
        seen = set()
        for identity, zone, receiver, fingerprint, recorded, raw in rows:
            evidence = ReceiverEvidence.model_validate_json(raw)
            key = (zone, receiver)
            latest = key not in seen
            seen.add(key)
            when = datetime.fromisoformat(recorded)
            stale = (zone not in config.zone_map or fingerprint != zone_fingerprint(config, zone)
                     or now - when > timedelta(days=90) or when > now)
            records.append({"id": identity, "zone": zone, "receiver_id": receiver,
                            "recorded_at": recorded, "latest": latest, "evidence": evidence,
                            "status": "Needs recheck" if stale else evidence.result})
        return records
