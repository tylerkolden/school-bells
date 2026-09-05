"""Durable, bounded operational notification outbox with success-only deduplication."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bell.config import Settings

LOGGER = logging.getLogger(__name__)


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise OSError("Webhook redirects are not permitted")


def open_webhook(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(RejectRedirect()).open(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    success: bool
    detail: str


class AlertDispatcher:
    def __init__(self, settings: Settings, *, dedupe_seconds: float = 300.0,
                 outbox_path: Path | None = None) -> None:
        self.settings = settings
        self.dedupe_seconds = dedupe_seconds
        self._lock = threading.RLock()
        if outbox_path:
            outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(outbox_path) if outbox_path else ":memory:",
                                   timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, dedupe_key TEXT, payload TEXT, url TEXT, secret_env TEXT,
                attempts INTEGER DEFAULT 0, due REAL, lease REAL DEFAULT 0,
                status TEXT DEFAULT 'pending', detail TEXT DEFAULT 'Queued', updated REAL)""")
        if outbox_path:
            outbox_path.chmod(0o600)

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def _enqueue(self, kind: str, message: str, severity: str,
                 details: dict[str, Any] | None, force: bool) -> tuple[str | None, str]:
        settings = self.settings
        if not settings.alert_webhook_url:
            return None, "operational alert webhook is not configured"
        now = time.time()
        key = f"{kind}|{message}"
        identity = uuid.uuid4().hex
        payload = json.dumps({"schema": 1, "id": identity,
                              "timestamp": datetime.now(UTC).isoformat(), "kind": kind,
                              "severity": severity, "message": message, "details": details or {}},
                             sort_keys=True, separators=(",", ":"))
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._db.execute(
                "SELECT status FROM notifications WHERE dedupe_key=? AND "
                "(status='pending' OR (status='sent' AND updated>?)) LIMIT 1",
                (key, now - self.dedupe_seconds)).fetchone()
            if not force and prior:
                return None, "alert already queued" if prior["status"] == "pending" else "duplicate alert suppressed"
            # Keep terminal history bounded; never silently drop a pending notification.
            self._db.execute("DELETE FROM notifications WHERE status!='pending' AND id NOT IN "
                             "(SELECT id FROM notifications ORDER BY updated DESC LIMIT 500)")
            if self._db.execute("SELECT count(*) FROM notifications WHERE status='pending'").fetchone()[0] >= 500:
                LOGGER.error("notification_outbox_full")
                return None, "notification outbox is full; operator intervention required"
            self._db.execute(
                "INSERT INTO notifications(id,dedupe_key,payload,url,secret_env,due,updated) VALUES(?,?,?,?,?,?,?)",
                (identity, key, payload, settings.alert_webhook_url,
                 settings.alert_webhook_secret_env, now, now))
        return identity, "Queued for delivery"

    def enqueue(self, kind: str, message: str, *, severity: str = "warning",
                details: dict[str, Any] | None = None, force: bool = False) -> AlertOutcome:
        """Persist without network I/O, safe for the audio failure path."""
        identity, detail = self._enqueue(kind, message, severity, details, force)
        return AlertOutcome(identity is not None, detail)

    def send(self, kind: str, message: str, *, severity: str = "warning",
             details: dict[str, Any] | None = None, force: bool = False) -> AlertOutcome:
        identity, detail = self._enqueue(kind, message, severity, details, force)
        return self._deliver(identity) if identity else AlertOutcome(False, detail)

    def _deliver(self, identity: str) -> AlertOutcome:
        now = time.time()
        with self._lock, self._db:
            # Cross-process lease prevents the web console and service retrying simultaneously.
            claimed = self._db.execute(
                "UPDATE notifications SET lease=?,attempts=attempts+1 WHERE id=? AND status='pending' "
                "AND attempts<5 AND due<=? AND lease<=?",
                (now + 60, identity, now, now)).rowcount
            if not claimed:
                return AlertOutcome(False, "notification is not due or is being delivered")
            row = self._db.execute("SELECT * FROM notifications WHERE id=?", (identity,)).fetchone()
        if row["url"] != self.settings.alert_webhook_url:
            outcome = AlertOutcome(False, "Webhook changed or disabled; old notification cancelled")
            terminal = True
        else:
            outcome = self._post(row)
            terminal = False
        attempts = row["attempts"]
        state = "sent" if outcome.success else "exhausted" if terminal or attempts >= 5 else "pending"
        # 15s, 30s, 60s, 120s; five total tries, persistent across restarts.
        with self._lock, self._db:
            self._db.execute(
                "UPDATE notifications SET attempts=?,status=?,detail=?,updated=?,due=?,lease=0 WHERE id=?",
                (attempts, state, outcome.detail, time.time(), time.time() + min(120, 15 * 2 ** (attempts - 1)), identity))
        LOGGER.log(logging.INFO if outcome.success else logging.WARNING,
                   "notification_delivery", extra={"notification_id": identity, "result": state,
                                                   "attempts": attempts})
        return outcome

    def _post(self, row: sqlite3.Row) -> AlertOutcome:
        payload = row["payload"].encode()
        headers = {"Content-Type": "application/json", "User-Agent": "bell-system-alerts/1",
                   "X-Bell-Notification-ID": row["id"]}
        if row["secret_env"]:
            secret = os.environ.get(row["secret_env"])
            if not secret:
                return AlertOutcome(False, "Signing secret is unavailable")
            headers["X-Bell-Signature"] = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        request = urllib.request.Request(row["url"], data=payload, headers=headers, method="POST")
        try:
            with open_webhook(request, timeout=5) as response:
                status = int(response.status)
            return AlertOutcome(200 <= status < 300, f"webhook returned HTTP {status}")
        except (OSError, urllib.error.URLError) as exc:
            # URLs or response text may contain credentials. Do not persist exception text.
            return AlertOutcome(False, f"Webhook delivery failed ({type(exc).__name__})")

    def retry_pending(self, limit: int = 1) -> list[AlertOutcome]:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE notifications SET status='exhausted',detail='Attempt budget exhausted after interruption',updated=? "
                "WHERE status='pending' AND attempts>=5 AND lease<=?", (time.time(), time.time()))
            rows = self._db.execute(
                "SELECT id FROM notifications WHERE status='pending' AND due<=? AND lease<=? ORDER BY due LIMIT ?",
                (time.time(), time.time(), limit)).fetchall()
        return [self._deliver(row["id"]) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(self._db.execute("SELECT status,count(*) FROM notifications GROUP BY status"))
            last = self._db.execute(
                "SELECT status,detail,attempts,updated FROM notifications ORDER BY updated DESC LIMIT 1").fetchone()
        return {"configured": bool(self.settings.alert_webhook_url),
                "pending": counts.get("pending", 0), "exhausted": counts.get("exhausted", 0),
                "last": dict(last) if last else None}
