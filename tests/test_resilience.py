from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from test_web import hidden, login

from bell.alerts import AlertDispatcher
from bell.config import load_config
from bell.continuity import ContinuityPlan, ContinuityStore, copy_to_backup_volume
from bell.readiness import range_config, review
from bell.recovery import RecoveryError
from bell.watchdog import NoRedirect, WatchConfig, WatchState, probe
from bell.web import create_app


def alerts(config_tree, monkeypatch):
    settings = load_config(config_tree).settings.model_copy(update={"alert_webhook_url": "https://example.test/alerts"})
    clock = [1000.0]
    monkeypatch.setattr("bell.alerts.time.time", lambda: clock[0])
    return settings, clock


def test_notification_retry_survives_restart_and_is_bounded(config_tree, monkeypatch):
    settings, clock = alerts(config_tree, monkeypatch)
    post = MagicMock(side_effect=OSError("https://secret-password@example.test"))
    monkeypatch.setattr("bell.alerts.open_webhook", post)
    path = config_tree.parent / "alerts.sqlite3"
    dispatcher = AlertDispatcher(settings, outbox_path=path)
    assert not dispatcher.send("failed", "Bell failed").success
    assert dispatcher.snapshot()["pending"] == 1
    assert "secret-password" not in str(dispatcher.snapshot())
    assert dispatcher.send("failed", "Bell failed").detail == "alert already queued"
    assert post.call_count == 1
    # Restart does not lose pending work or reset its attempt budget.
    dispatcher = AlertDispatcher(settings, outbox_path=path)
    assert dispatcher.retry_pending() == []
    for _ in range(4):
        clock[0] += 121
        dispatcher.retry_pending()
    assert post.call_count == 5
    assert dispatcher.snapshot()["exhausted"] == 1
    clock[0] += 10000
    assert dispatcher.retry_pending() == []


def test_notification_recovery_keeps_payload_id_and_dedupes_only_success(config_tree, monkeypatch):
    settings, clock = alerts(config_tree, monkeypatch)
    ok = MagicMock()
    ok.__enter__.return_value.status = 204
    post = MagicMock(side_effect=[OSError("offline"), ok])
    monkeypatch.setattr("bell.alerts.open_webhook", post)
    dispatcher = AlertDispatcher(settings)
    dispatcher.send("failed", "Page failed")
    clock[0] += 15
    assert dispatcher.retry_pending()[0].success
    first, second = [call.args[0] for call in post.call_args_list]
    assert first.data == second.data
    assert first.get_header("X-bell-notification-id") == second.get_header("X-bell-notification-id")
    assert dispatcher.send("failed", "Page failed").detail == "duplicate alert suppressed"
    assert dispatcher.snapshot()["pending"] == 0


def test_notification_lease_secret_and_disabled_destination(config_tree, monkeypatch):
    settings, clock = alerts(config_tree, monkeypatch)
    settings.alert_webhook_secret_env = "UNSET_TEST_SECRET"
    monkeypatch.delenv("UNSET_TEST_SECRET", raising=False)
    post = MagicMock()
    monkeypatch.setattr("bell.alerts.open_webhook", post)
    dispatcher = AlertDispatcher(settings)
    assert not dispatcher.send("test", "Missing secret").success
    assert dispatcher.snapshot()["pending"] == 1
    settings.alert_webhook_url = None
    clock[0] += 15
    dispatcher.retry_pending()
    assert dispatcher.snapshot()["exhausted"] == 1
    post.assert_not_called()


def test_watcher_outage_ack_escalation_recovery_and_restart(tmp_path):
    config = WatchConfig(health_url="https://bell.example/api/v1/health", owner="Office", escalation="Principal")
    store = WatchState(tmp_path / "watch.sqlite3")
    for now in [1000, 1030]:
        assert not store.observe(False, "offline", config, now).get("incident")
    incident = store.observe(False, "offline", config, 1060)["incident"]
    store = WatchState(tmp_path / "watch.sqlite3")
    with pytest.raises(ValueError):
        store.acknowledge("wrong", "Tyler", "Investigating", 1100)
    store.acknowledge(incident["id"], "Tyler", "Check power", 1100)
    state = store.observe(False, "offline", config, 1700)
    assert [event["kind"] for event in state["pending"]] == ["outage"]
    state = store.observe(False, "offline", config, 2900)
    assert state["pending"][-1]["kind"] == "escalation"
    assert store.observe(True, "ready", config, 2930)["incident"]
    state = store.observe(True, "ready", config, 2960)
    assert state["incident"] is None and state["pending"][-1]["kind"] == "recovered"
    assert state["last_incident"]["acknowledged"]["by"] == "Tyler"


def test_watcher_flush_keeps_undeliverable_transition(tmp_path, config_tree):
    config = WatchConfig(health_url="https://bell.example", owner="Office", escalation="Principal", failure_threshold=1)
    store = WatchState(tmp_path / "watch.sqlite3")
    store.observe(False, "offline", config, 1000)
    settings = load_config(config_tree).settings
    dispatcher = AlertDispatcher(settings)
    store.flush(dispatcher)
    assert len(store.snapshot()["pending"]) == 1
    settings.alert_webhook_url = "https://example.test/alerts"
    store.flush(dispatcher)
    assert not store.snapshot()["pending"]
    assert dispatcher.snapshot()["pending"] == 1


@pytest.mark.parametrize("body", [b'{}', b'[]', b'not json', b'{"ready":true,"observed_at":"2020-01-01T00:00:00+00:00"}',
                                  b'{"ready":"true","observed_at":"2027-01-01T00:00:00+00:00"}'])
def test_monitor_rejects_invalid_or_stale_health(body, monkeypatch):
    config = WatchConfig(health_url="https://bell.example", owner="Office", escalation="Principal")
    monkeypatch.setenv("BELL_MONITOR_API_KEY", "test")
    response = MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.return_value = body
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr("bell.watchdog.build_opener", lambda *args: opener)
    assert not probe(config, now=datetime(2027, 1, 1, tzinfo=UTC).timestamp())[0]


def test_monitor_fresh_health_no_credentials_or_redirects(monkeypatch):
    config = WatchConfig(health_url="https://bell.example", owner="Office", escalation="Principal")
    monkeypatch.delenv("BELL_MONITOR_API_KEY", raising=False)
    assert not probe(config)[0]
    with pytest.raises(OSError):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker")
    with pytest.raises(ValueError):
        config.model_validate({**config.model_dump(), "health_url": "http://bell.example"})
    monkeypatch.setenv("BELL_MONITOR_API_KEY", "test")
    response = MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.return_value = b'{"ready":true,"observed_at":"2027-01-01T00:00:00+00:00"}'
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr("bell.watchdog.build_opener", lambda *args: opener)
    assert probe(config, now=datetime(2027, 1, 1, tzinfo=UTC).timestamp())[0]


def test_monitor_key_cannot_transmit(config_tree, monkeypatch):
    monkeypatch.setenv("BELL_MONITOR_API_KEY", "read-only-test")
    client = TestClient(create_app(config_tree, password="test"))
    headers = {"X-Bell-API-Key": "read-only-test"}
    health = client.get("/api/v1/health", headers=headers)
    assert health.status_code == 200 and health.json()["ready"] is False
    assert client.get("/api/v1/today", headers=headers).status_code == 401
    assert client.post("/api/v1/trigger", headers=headers, json={"zone": "indoors", "sound": "class-bell.wav"}).status_code == 401
    # A misconfigured shared value must still fail closed, even if it matches a write key.
    monkeypatch.setenv("BELL_API_KEY", "read-only-test")
    monkeypatch.setenv("BELL_EMERGENCY_API_KEY", "read-only-test")
    assert client.post("/api/v1/trigger", headers=headers, json={"zone": "indoors", "sound": "class-bell.wav"}).status_code == 401



def plan(**changes):
    return ContinuityPlan(owner="Tyler", monitoring_host="Other host", monitoring_owner="IT",
                          escalation="Principal", backup_destination="NAS", backup_owner="IT", **changes)


def test_continuity_stale_future_failed_and_conflicting_records(tmp_path):
    store = ContinuityStore(tmp_path / "continuity.sqlite3")
    now = datetime(2027, 1, 1, tzinfo=UTC)
    assert store.snapshot(now.date())["issues"]
    with pytest.raises(ValueError):
        store.record(plan(last_offdevice_copy=date(2027, 1, 2)), now, 0)
    store.record(plan(last_offdevice_copy=now.date(), last_restore=now.date(), restore_result="pass", restore_observer="Staff"), now, 0)
    assert not store.snapshot(now.date())["issues"]
    assert len(store.snapshot(now.date() + timedelta(days=91))["issues"]) == 2
    with pytest.raises(ValueError):
        store.record(plan(), now, 0)
    store.record(plan(last_restore=now.date(), restore_result="fail", restore_observer="Staff"), now, 1)
    assert store.snapshot(now.date())["issues"]
    with pytest.raises(ValueError):
        plan(restore_result="pass")


def test_offdevice_copy_refuses_missing_mount_and_verifies(config_tree, tmp_path, monkeypatch):
    cfg = load_config(config_tree)
    mount = tmp_path / "mounted"
    mount.mkdir()
    with pytest.raises(RecoveryError):
        copy_to_backup_volume(cfg, mount)
    monkeypatch.setattr("bell.continuity.os.path.ismount", lambda path: path == mount.resolve())
    archive = copy_to_backup_volume(cfg, mount)
    assert archive.is_file()
    assert not list(mount.glob("*.partial"))


def test_calendar_review_preserves_exceptions_and_rejects_tampering(config_tree):
    cfg = load_config(config_tree)
    day = date(2027, 3, 1)
    cfg.calendar.no_bell_dates[day] = "Holiday"
    proposed = range_config(cfg, day, day + timedelta(days=2), "schedule", cfg.schedules[0].name, "")
    assert review(cfg, day, day)[0]["plan"].reason == "Holiday"
    assert review(proposed, day, day)[0]["plan"].events
    assert cfg.calendar.no_bell_dates[day] == "Holiday"
    with pytest.raises(ValueError):
        review(cfg, day, day + timedelta(days=371))
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    page = client.get("/calendar")
    fields = {"start": "2027-03-01", "end": "2027-03-03", "bulk_action": "no_bells",
              "no_bell_reason": "Holiday", "config_hash": hidden(page, "config_hash"), "csrf": hidden(page, "csrf")}
    preview = client.post("/calendar/bulk", data=fields)
    assert preview.status_code == 200 and "Before" in preview.text and "After" in preview.text
    token = hidden(preview, "review_token")
    assert client.post("/calendar/bulk", data={**fields, "end": "2027-03-04", "review_token": token}).status_code == 409
    assert client.post("/calendar/bulk", data={**fields, "review_token": "forged"}).status_code == 400
    settings = config_tree / "settings.yaml"
    settings.write_text(settings.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert client.post("/calendar/bulk", data={**fields, "review_token": token}).status_code == 409
    assert not load_config(config_tree).calendar.no_bell_dates.get(day)


def test_continuity_web_csrf_and_readiness(config_tree):
    client = TestClient(create_app(config_tree, password="test"))
    login(client)
    page = client.get("/recovery")
    assert "Assign continuity owners" in page.text
    fields = {**plan().model_dump(exclude_none=True), "revision": 0, "csrf": hidden(page, "csrf")}
    assert client.post("/recovery/ownership", data={**fields, "csrf": "wrong"}).status_code == 403
    assert client.post("/recovery/ownership", data=fields, follow_redirects=False).status_code == 303
    assert "Other host" in client.get("/recovery").text
    result = client.get("/calendar/readiness?start=2027-03-01&end=2027-03-05")
    assert result.status_code == 200 and "Weekday default" in result.text
    assert client.get("/calendar/readiness?start=2027-03-01&end=2030-03-05").status_code == 400


def test_concurrent_retry_cannot_send_same_outbox_item_twice(config_tree, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    settings, _clock = alerts(config_tree, monkeypatch)
    path = config_tree.parent / "concurrent-alerts.sqlite3"
    first, second = AlertDispatcher(settings, outbox_path=path), AlertDispatcher(settings, outbox_path=path)
    first.enqueue("failure", "Page failed")
    entered, release = Event(), Event()
    response = MagicMock()
    response.__enter__.return_value.status = 204
    def post(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return response
    monkeypatch.setattr("bell.alerts.open_webhook", post)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(first.retry_pending)
        assert entered.wait(5)
        assert second.retry_pending() == []
        release.set()
        assert pending.result()[0].success


def test_backup_corruption_never_publishes_archive(config_tree, tmp_path, monkeypatch):
    cfg = load_config(config_tree)
    mount = tmp_path / "mounted"
    mount.mkdir()
    monkeypatch.setattr("bell.continuity.os.path.ismount", lambda path: path == mount.resolve())
    import shutil
    original_copy = shutil.copyfileobj
    def corrupt(source, output, *args, **kwargs):
        if str(output.name).endswith(".partial"):
            return output.write(b"corrupt")
        return original_copy(source, output, *args, **kwargs)
    monkeypatch.setattr("bell.continuity.shutil.copyfileobj", corrupt)
    with pytest.raises(RecoveryError):
        copy_to_backup_volume(cfg, mount)
    assert not list(mount.iterdir())


def test_backup_sidecar_contains_consistent_records_without_claims(config_tree, tmp_path, monkeypatch):
    import zipfile
    cfg = load_config(config_tree)
    store = ContinuityStore(cfg.state_path / "continuity.sqlite3")
    store.record(plan(), datetime.now(UTC), 0)
    (cfg.state_path / "manual-actions.sqlite3").write_text("must never export", encoding="utf-8")
    mount = tmp_path / "mounted"
    mount.mkdir()
    monkeypatch.setattr("bell.continuity.os.path.ismount", lambda path: path == mount.resolve())
    archive = copy_to_backup_volume(cfg, mount)
    with zipfile.ZipFile(archive.with_name(archive.name + ".records.zip")) as records:
        assert records.namelist() == ["continuity.sqlite3"]


def test_readiness_flags_missing_weekdays_but_respects_explicit_closures(config_tree):
    cfg = load_config(config_tree)
    cfg.calendar.weekday_defaults.clear()
    cfg.calendar.date_ranges.clear()
    cfg.calendar.overrides.clear()
    start = date(2027, 3, 1)
    cfg.calendar.no_bell_dates[start] = "Approved holiday"
    rows = review(cfg, start, date(2027, 3, 7))
    assert not rows[0]["issue"]
    assert len([row for row in rows if row["issue"]]) == 4


def test_review_crosses_dst_in_school_wall_time(config_tree):
    cfg = load_config(config_tree)
    start, end = date(2027, 3, 12), date(2027, 3, 15)
    rows = review(cfg, start, end)
    before, after = rows[0]["plan"].events[0], rows[-1]["plan"].events[0]
    assert before.scheduled_at.hour == after.scheduled_at.hour
    assert before.scheduled_at.utcoffset() != after.scheduled_at.utcoffset()


def test_notification_crash_cannot_reset_attempt_budget(config_tree, monkeypatch):
    settings, clock = alerts(config_tree, monkeypatch)
    path = config_tree.parent / "interrupted-alerts.sqlite3"
    dispatcher = AlertDispatcher(settings, outbox_path=path)
    dispatcher.enqueue("failure", "Interrupted")
    monkeypatch.setattr("bell.alerts.open_webhook", MagicMock(side_effect=SystemExit("process died")))
    for _ in range(5):
        with pytest.raises(SystemExit):
            dispatcher.retry_pending()
        clock[0] += 61
        dispatcher = AlertDispatcher(settings, outbox_path=path)
    assert dispatcher.retry_pending() == []
    assert dispatcher.snapshot()["pending"] == 0
    assert dispatcher.snapshot()["exhausted"] == 1


def test_webhook_redirect_never_forwards_signed_request():
    from bell.alerts import RejectRedirect
    with pytest.raises(OSError):
        RejectRedirect().redirect_request(None, None, 307, "", {}, "https://attacker.example")
