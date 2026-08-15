from __future__ import annotations

import io
import urllib.error

from bell.config import Destination
from bell.protocols.http import WebhookClient


class Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return b"{}"


def test_webhook_retries_503_and_signs_payload(monkeypatch) -> None:
    monkeypatch.setenv("HOOK_SECRET", "secret")
    destination = Destination(
        name="strobe",
        protocol="http",
        port=443,
        webhook_url="https://example.test/trigger",
        webhook_secret_env="HOOK_SECRET",
        retries=2,
    )
    calls = []
    delays = []

    def opener(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO())
        return Response(202)

    result = WebhookClient(opener, delays.append).trigger(
        destination, {"zone": "everywhere"}, "event-1"
    )
    assert result.success and result.attempts == 2
    assert delays == [0.25]
    assert calls[-1][0].headers["Idempotency-key"] == "event-1"
    assert calls[-1][0].headers["X-bell-signature"].startswith("sha256=")


def test_webhook_missing_secret_fails_without_network() -> None:
    destination = Destination(
        name="display",
        protocol="http",
        port=443,
        webhook_url="https://example.test/trigger",
        webhook_secret_env="NOT_SET",
    )
    result = WebhookClient(lambda *_args, **_kwargs: None).trigger(destination, {}, "key")
    assert not result.success and result.attempts == 0


def test_webhook_health_treats_auth_response_as_reachable() -> None:
    destination = Destination(
        name="display",
        protocol="http",
        port=443,
        webhook_url="https://example.test/trigger",
        healthcheck_url="https://example.test/health",
    )

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "auth required", {}, io.BytesIO())

    result = WebhookClient(opener).check(destination)
    assert result.success and result.status == "401"
