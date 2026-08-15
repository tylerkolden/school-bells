"""Hardened JSON webhook adapter for paging gateways, strobes, and displays."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from bell.config import Destination
from bell.protocols.base import DeliveryOutcome

LOGGER = logging.getLogger(__name__)


class WebhookClient:
    def __init__(
        self,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._opener = opener
        self._sleep = sleeper

    def trigger(
        self,
        destination: Destination,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> DeliveryOutcome:
        if not destination.webhook_url:
            raise ValueError("HTTP destination has no webhook_url")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "bell-system/0.1",
            "Idempotency-Key": idempotency_key,
            "X-Bell-Timestamp": timestamp,
        }
        if destination.webhook_secret_env:
            secret = os.environ.get(destination.webhook_secret_env)
            if not secret:
                return DeliveryOutcome(
                    destination.name,
                    "http",
                    False,
                    "configuration_error",
                    f"environment variable {destination.webhook_secret_env} is not set",
                    0,
                    0.0,
                )
            signature = hmac.new(
                secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
            ).hexdigest()
            headers["X-Bell-Signature"] = f"sha256={signature}"
        request = urllib.request.Request(destination.webhook_url, body, headers, method="POST")
        context = (
            ssl.create_default_context(cafile=str(destination.tls_ca_file))
            if destination.tls_ca_file
            else None
        )
        started = time.monotonic()
        attempts = 0
        last_detail = "no request attempted"
        for attempt in range(destination.retries + 1):
            attempts = attempt + 1
            try:
                options = {"timeout": destination.timeout_seconds}
                if context is not None:
                    options["context"] = context
                with self._opener(request, **options) as response:
                    status = int(response.status)
                    response.read(4096)
                if 200 <= status < 300:
                    return DeliveryOutcome(
                        destination.name,
                        "http",
                        True,
                        str(status),
                        "webhook accepted",
                        attempts,
                        time.monotonic() - started,
                    )
                last_detail = f"unexpected HTTP status {status}"
                retryable = status in {408, 425, 429} or status >= 500
            except urllib.error.HTTPError as exc:
                last_detail = f"HTTP {exc.code}: {exc.reason}"
                retryable = exc.code in {408, 425, 429} or exc.code >= 500
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_detail = str(exc)
                retryable = True
            if not retryable or attempt >= destination.retries:
                break
            delay = min(2.0, 0.25 * (2**attempt))
            LOGGER.warning(
                "webhook_retry",
                extra={"destination": destination.name, "attempt": attempts, "delay": delay},
            )
            self._sleep(delay)
        return DeliveryOutcome(
            destination.name,
            "http",
            False,
            "failed",
            last_detail,
            attempts,
            time.monotonic() - started,
        )

    def check(self, destination: Destination) -> DeliveryOutcome:
        url = destination.healthcheck_url or destination.webhook_url
        if not url:
            raise ValueError("HTTP destination has no URL")
        request = urllib.request.Request(url, headers={"User-Agent": "bell-system/0.1"}, method="HEAD")
        context = (
            ssl.create_default_context(cafile=str(destination.tls_ca_file))
            if destination.tls_ca_file
            else None
        )
        started = time.monotonic()
        try:
            options = {"timeout": destination.timeout_seconds}
            if context is not None:
                options["context"] = context
            with self._opener(request, **options) as response:
                status = int(response.status)
            reachable = status < 500
            return DeliveryOutcome(
                destination.name,
                "http",
                reachable,
                str(status),
                "endpoint reachable" if reachable else "server error",
                1,
                time.monotonic() - started,
            )
        except urllib.error.HTTPError as exc:
            reachable = exc.code < 500
            return DeliveryOutcome(
                destination.name,
                "http",
                reachable,
                str(exc.code),
                str(exc.reason),
                1,
                time.monotonic() - started,
            )
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            return DeliveryOutcome(
                destination.name,
                "http",
                False,
                "unreachable",
                str(exc),
                1,
                time.monotonic() - started,
            )
