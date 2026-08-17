"""Optional deduplicated signed operational alert webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bell.config import Settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    success: bool
    detail: str


class AlertDispatcher:
    def __init__(self, settings: Settings, *, dedupe_seconds: float = 300.0) -> None:
        self.settings = settings
        self.dedupe_seconds = dedupe_seconds
        self._lock = threading.Lock()
        self._sent: dict[str, float] = {}

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def send(
        self,
        kind: str,
        message: str,
        *,
        severity: str = "warning",
        details: dict[str, Any] | None = None,
        force: bool = False,
    ) -> AlertOutcome:
        url = self.settings.alert_webhook_url
        if not url:
            return AlertOutcome(False, "operational alert webhook is not configured")
        key = f"{kind}|{message}"
        now_monotonic = time.monotonic()
        with self._lock:
            last = self._sent.get(key)
            if not force and last is not None and now_monotonic - last < self.dedupe_seconds:
                return AlertOutcome(False, "duplicate alert suppressed")
            self._sent[key] = now_monotonic
        payload = json.dumps(
            {
                "schema": 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "kind": kind,
                "severity": severity,
                "message": message,
                "details": details or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "bell-system-alerts/1"}
        secret_env = self.settings.alert_webhook_secret_env
        if secret_env:
            secret = os.environ.get(secret_env)
            if not secret:
                return AlertOutcome(False, f"environment variable {secret_env} is not set")
            headers["X-Bell-Signature"] = (
                "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            )
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = int(response.status)
            if 200 <= status < 300:
                LOGGER.info("operational_alert_sent", extra={"kind": kind, "status": status})
                return AlertOutcome(True, f"webhook returned HTTP {status}")
            return AlertOutcome(False, f"webhook returned HTTP {status}")
        except (OSError, urllib.error.URLError) as exc:
            LOGGER.warning("operational_alert_failed", extra={"kind": kind, "detail": str(exc)})
            return AlertOutcome(False, str(exc))
