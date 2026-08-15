from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from bell.simulator import create_server


@contextmanager
def running_simulator() -> Iterator[str]:
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_simulator_accepts_and_displays_page_events() -> None:
    with running_simulator() as base_url:
        request = urllib.request.Request(
            f"{base_url}/page",
            json.dumps({"event": "Test bell", "sound": "bell.wav", "zone": "indoors"}).encode(),
            {"Content-Type": "application/json", "Idempotency-Key": "test-1"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        with urllib.request.urlopen(f"{base_url}/events") as response:
            events = json.load(response)
        assert events["count"] == 1
        assert events["events"][0]["idempotency_key"] == "test-1"
        assert events["events"][0]["payload"]["zone"] == "indoors"
        with urllib.request.urlopen(base_url) as response:
            dashboard = response.read().decode()
        assert "Test bell" in dashboard
        assert "Safe simulator online" in dashboard


def test_simulator_health_clear_and_input_validation() -> None:
    with running_simulator() as base_url:
        health = urllib.request.Request(f"{base_url}/health", method="HEAD")
        with urllib.request.urlopen(health) as response:
            assert response.status == 200
        invalid = urllib.request.Request(
            f"{base_url}/page", b"not json", {"Content-Type": "application/json"}, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(invalid)
        assert error.value.code == 400
        clear = urllib.request.Request(f"{base_url}/clear", b"", method="POST")
        with urllib.request.urlopen(clear) as response:
            assert response.status == 200
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base_url}/missing")
        assert error.value.code == 404
