from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_web import hidden, login

from bell.config import load_config
from bell.manual import ManualActions
from bell.scheduler import BellScheduler, FireState
from bell.web import create_app


def test_manual_retries_restart_and_distinct_actions(config_tree: Path, monkeypatch) -> None:
    class Clock(datetime):
        minute = 45

        @classmethod
        def now(cls, tz=None):
            return cls(2027, 2, 2, 7, cls.minute, tzinfo=tz or ZoneInfo("America/Denver"))

    monkeypatch.setattr("bell.web.datetime", Clock)
    monkeypatch.setenv("BELL_UI_SESSION_SECRET", "stable-test-session-secret")
    sent = []
    scheduler = BellScheduler(load_config(config_tree), lambda *args: sent.append(args))
    client = TestClient(create_app(config_tree, password="test", scheduler=scheduler))
    login(client)

    def prepare(sound="class-bell.wav"):
        page = client.post("/manual/prepare", data={
            "sound": sound, "zone": "indoors", "csrf": hidden(client.get("/manual"), "csrf")})
        return {"confirm_token": hidden(page, "confirm_token"), "csrf": hidden(page, "csrf")}

    payload = prepare()
    client.post("/manual/fire", data=payload)
    Clock.minute = 46
    client.post("/manual/fire", data=payload)
    assert len(sent) == 1
    restarted = TestClient(create_app(config_tree, password="test", scheduler=scheduler))
    restarted.cookies.update(client.cookies)
    restarted.post("/manual/fire", data=payload)
    assert len(sent) == 1
    client.post("/manual/fire", data=prepare())
    client.post("/manual/fire", data=prepare("prayer.wav"))
    assert len(sent) == 3
    changed_config = prepare()
    settings = config_tree / "settings.yaml"
    settings.write_text(settings.read_text(encoding="utf-8").replace(
        "school_name: School Bell", "school_name: Updated School"), encoding="utf-8")
    client.post("/manual/fire", data=changed_config)
    assert len(sent) == 3
    stale = prepare()
    sound = config_tree.parent / "sounds/class-bell.wav"
    sound.write_bytes(sound.read_bytes() + b"changed")
    result = client.post("/manual/fire", data=stale)
    assert "Configuration or audio changed" in result.text
    assert len(sent) == 3


def test_claim_is_atomic_across_connections_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "actions.sqlite3"
    stores = [ManualActions(path) for _ in range(8)]
    now = datetime.now(ZoneInfo("America/Denver"))
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(lambda store: store.claim("same-action", now), stores))
    assert sum(claimed) == 1
    assert not ManualActions(path).claim("same-action", now)
    assert "interrupted" in stores[0].result("same-action")


def test_unknown_runtime_is_not_ready(config_tree: Path) -> None:
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    assert client.get("/operations/snapshot").json()["ready"] is False


def test_daily_cap_is_atomic_for_distinct_simultaneous_actions(tmp_path: Path) -> None:
    now = datetime.now(ZoneInfo("America/Denver"))
    states = [FireState(tmp_path / "fire.sqlite3") for _ in range(8)]
    def claim(index):
        return states[index].record_once(
            now.date(), f"action-{index}", "started", "claimed", now, max_attempts=1)
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(claim, range(8))) == 1
