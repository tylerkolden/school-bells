"""Browser-only test harness: all traffic intercepted; no transmitter runs."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from bell.auth import AuthStore
from bell.config import load_config
from bell.web import create_app


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "timezone_id": "Pacific/Honolulu"}


@pytest.fixture
def console(page, tmp_path, monkeypatch, request):
    root = Path(__file__).resolve().parents[1]
    for name in ("config", "sounds"):
        shutil.copytree(root / name, tmp_path / name)

    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 2, 2, 7, 45, tzinfo=tz or ZoneInfo("America/Denver"))

    monkeypatch.setattr("bell.web.datetime", Morning)
    role = getattr(request, "param", "admin")
    if role == "operator":
        store = AuthStore(load_config(tmp_path / "config").state_path / "auth/users.json", "test")
        store.set_password("admin", "admin", "administrator-password")
        store.set_password("office", "operator", "operator-password")
    client = TestClient(create_app(tmp_path / "config", password="test"))
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text)[1]
    client.post("/login", data={"submitted_password": "operator-password" if role == "operator" else "test",
                               "username": "office" if role == "operator" else "admin", "csrf": csrf})
    snapshot = client.get("/operations/snapshot").json()
    snapshot.update(ready=True, blocked_reasons=[])
    controls = {"status": 200, "snapshot": snapshot}

    def serve(route):
        path = route.request.url.split("http://testserver", 1)[-1]
        if route.request.method != "GET":
            route.fulfill(status=405, body="Browser fixture never transmits")
        elif path == "/operations/snapshot":
            route.fulfill(status=controls["status"], json=controls["snapshot"])
        elif route.request.url.startswith("http://testserver/"):
            response = client.get(path)
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get("content-type", "text/plain"))
        else:
            route.abort()

    page.route("**/*", serve)
    return controls
