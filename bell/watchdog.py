"""Run on a separate host: detect missing/invalid health and own outage acknowledgment."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bell.alerts import AlertDispatcher
from bell.config import Settings
from bell.logging_setup import JsonFormatter

LOGGER = logging.getLogger(__name__)


class WatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    health_url: str
    owner: str = Field(min_length=1, max_length=100)
    escalation: str = Field(min_length=1, max_length=300)
    api_key_env: str = "BELL_MONITOR_API_KEY"
    webhook_env: str = "BELL_WATCH_WEBHOOK_URL"
    signing_secret_env: str = "BELL_WATCH_SECRET"
    failure_threshold: int = Field(default=3, ge=1, le=20)
    recovery_threshold: int = Field(default=2, ge=1, le=20)
    reminder_seconds: int = Field(default=600, ge=60, le=86400)
    escalation_seconds: int = Field(default=1800, ge=120, le=86400)

    @field_validator("health_url")
    @classmethod
    def secure_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Independent monitor requires an HTTPS URL without embedded credentials")
        return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise OSError("Health redirects are not permitted")


def probe(config: WatchConfig, *, now: float | None = None) -> tuple[bool, str]:
    key = os.environ.get(config.api_key_env)
    if not key:
        return False, "Monitor credential unavailable"
    request = Request(config.health_url, headers={"X-Bell-API-Key": key, "Cache-Control": "no-cache"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                return False, "Invalid health response"
        health = json.loads(raw)
        observed = datetime.fromisoformat(health["observed_at"])
        if observed.tzinfo is None:
            return False, "Health timestamp has no timezone"
        age = (time.time() if now is None else now) - observed.timestamp()
        if not -10 <= age <= 90:
            return False, "Stale health response or clock disagreement"
        if health.get("ready") is not True:
            return False, "Bell service reports not ready"
        return True, "Fresh healthy response"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"Health unavailable ({type(exc).__name__})"


class WatchState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS watch (id INTEGER PRIMARY KEY, document TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO watch VALUES(1, '{}')")
        path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def snapshot(self) -> dict:
        with self.connect() as db:
            return json.loads(db.execute("SELECT document FROM watch WHERE id=1").fetchone()[0])

    def observe(self, healthy: bool, detail: str, config: WatchConfig, now: float) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT document FROM watch WHERE id=1").fetchone()[0])
            state["last_probe"] = now
            state["detail"] = detail
            state["failures"] = 0 if healthy else state.get("failures", 0) + 1
            state["successes"] = state.get("successes", 0) + 1 if healthy else 0
            incident = state.get("incident")
            kind = None
            if not healthy and not incident and state["failures"] >= config.failure_threshold:
                incident = {"id": uuid.uuid4().hex, "opened": now, "last_notice": now, "acknowledged": None}
                state["incident"] = incident
                kind = "outage"
            elif healthy and incident and state["successes"] >= config.recovery_threshold:
                kind = "recovered"
                state["last_incident"] = {**incident, "recovered": now}
                state["incident"] = None
            elif incident and now - incident["opened"] >= config.escalation_seconds and not incident.get("escalated"):
                incident["escalated"] = now
                kind = "escalation"
            elif incident and not incident.get("acknowledged") and now - incident["last_notice"] >= config.reminder_seconds:
                kind = "reminder"
            if kind and incident:
                incident["last_notice"] = now
                state.setdefault("pending", []).append({"kind": kind, "incident": incident["id"], "at": now,
                                                        "detail": detail, "owner": config.owner,
                                                        "escalation": config.escalation})
            db.execute("UPDATE watch SET document=? WHERE id=1", (json.dumps(state),))
            return state

    def acknowledge(self, identity: str, observer: str, note: str, now: float) -> None:
        if not observer.strip() or not note.strip() or len(observer) > 100 or len(note) > 500:
            raise ValueError("Enter an owner and a brief response plan")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT document FROM watch WHERE id=1").fetchone()[0])
            if not state.get("incident") or state["incident"]["id"] != identity:
                raise ValueError("That incident is no longer active")
            state["incident"]["acknowledged"] = {"by": observer, "note": note, "at": now}
            db.execute("UPDATE watch SET document=? WHERE id=1", (json.dumps(state),))

    def flush(self, alerts: AlertDispatcher) -> None:
        # Preserve pending transition on crash; the outbox deduplicates the exact event on retry.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT document FROM watch WHERE id=1").fetchone()[0])
            remaining = []
            for event in state.get("pending", []):
                outcome = alerts.enqueue("external_" + event["kind"],
                                         f"School Bell {event['kind']} / {event['incident']} / {event['at']}",
                                         severity="info" if event["kind"] == "recovered" else "critical", details=event)
                if not outcome.success and outcome.detail not in {"alert already queued", "duplicate alert suppressed"}:
                    remaining.append(event)
            state["pending"] = remaining
            db.execute("UPDATE watch SET document=? WHERE id=1", (json.dumps(state),))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--ack", metavar="INCIDENT_ID")
    parser.add_argument("--by", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    config = WatchConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    state = WatchState(args.state_dir / "watch.sqlite3")
    if args.ack:
        state.acknowledge(args.ack, args.by, args.note, time.time())
        return 0
    if args.status:
        LOGGER.info("watch_status", extra={"state": state.snapshot()})
        # CLI result, intentionally readable outside structured service logging.
        parser.exit(0, json.dumps(state.snapshot(), indent=2) + "\n")
    webhook = os.environ.get(config.webhook_env)
    if not webhook:
        raise ValueError("Independent monitor webhook is not configured")
    alerts = AlertDispatcher(Settings(interface_ip="127.0.0.1", alert_webhook_url=webhook,
                                      alert_webhook_secret_env=config.signing_secret_env),
                             dedupe_seconds=86400, outbox_path=args.state_dir / "alerts.sqlite3")
    healthy, detail = probe(config)
    observed = state.observe(healthy, detail, config, time.time())
    state.flush(alerts)
    alerts.retry_pending(limit=1)
    LOGGER.info("independent_probe", extra={"healthy": healthy, "detail": detail,
                                           "incident": observed.get("incident"), "notifications": alerts.snapshot()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
