from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bell.config import load_config
from bell.scheduler import BellScheduler, FireState, resolve_day


def test_resolve_no_bell_and_override(config_tree: Path) -> None:
    config = load_config(config_tree)
    snow = resolve_day(date(2027, 1, 15), config)
    assert snow.events == () and snow.reason == "Snow day example"
    mass = resolve_day(date(2026, 9, 8), config)
    assert mass.schedule_name == "Mass Day"
    weekend = resolve_day(date(2027, 1, 16), config)
    assert weekend.events == () and weekend.reason == "no schedule assigned"


def test_standing_item_preserves_advanced_delivery_fields(config_tree: Path) -> None:
    config = load_config(config_tree)
    item = config.standing_items[0]
    item.pre_tone = "class-bell.wav"
    item.repeat_count = 3
    item.repeat_interval_seconds = 2
    item.priority = 95
    item.busy_policy = "preempt"
    event = next(
        planned.event
        for planned in resolve_day(date(2027, 3, 15), config).events
        if planned.source == "Standing" and planned.event.label == item.label
    )
    assert event.pre_tone == "class-bell.wav"
    assert event.repeat_count == 3
    assert event.repeat_interval_seconds == 2
    assert event.priority == 95
    assert event.busy_policy == "preempt"


def test_dst_wall_clock_times_do_not_shift(config_tree: Path) -> None:
    config = load_config(config_tree)
    # Assign schedules on the exact 2027 US transition dates (both Sundays) so the test checks
    # those dates directly, as well as adjacent Mondays whose UTC offsets differ.
    config.calendar.overrides[date(2027, 3, 14)] = "Regular Day"
    config.calendar.overrides[date(2027, 11, 7)] = "Regular Day"
    spring_transition = resolve_day(date(2027, 3, 14), config).events[0].scheduled_at
    fall_transition = resolve_day(date(2027, 11, 7), config).events[0].scheduled_at
    before_spring = resolve_day(date(2027, 3, 8), config).events[0].scheduled_at
    after_spring = resolve_day(date(2027, 3, 15), config).events[0].scheduled_at
    before_fall = resolve_day(date(2027, 11, 1), config).events[0].scheduled_at
    after_fall = resolve_day(date(2027, 11, 8), config).events[0].scheduled_at
    assert {
        item.hour
        for item in (
            spring_transition,
            fall_transition,
            before_spring,
            after_spring,
            before_fall,
            after_fall,
        )
    } == {8}
    assert before_spring.utcoffset() != after_spring.utcoffset()
    assert before_fall.utcoffset() != after_fall.utcoffset()


def test_fire_state_prevents_double_attempt_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    now = datetime.now(ZoneInfo("America/Denver"))
    assert FireState(path).record_once(now.date(), "event", "started", "claimed", now)
    assert not FireState(path).record_once(now.date(), "event", "started", "claimed", now)


def test_ninety_second_late_event_is_skipped(config_tree: Path) -> None:
    config = load_config(config_tree)
    config.settings.state_dir = config_tree.parent / "test-state"
    plan = resolve_day(date(2027, 3, 15), config)
    planned = plan.events[0]
    calls: list[str] = []
    scheduler = BellScheduler(config, lambda event, _config, _source: calls.append(event.label))
    decision = scheduler.fire(planned, now=planned.scheduled_at + timedelta(seconds=90))
    assert not decision and "60 seconds late" in decision.reason
    assert calls == []


def test_fire_checks_kill_switch_at_runtime(config_tree: Path) -> None:
    config = load_config(config_tree)
    config.settings.state_dir = config_tree.parent / "kill-state"
    config.safety.kill_switch_enabled = True
    planned = resolve_day(date(2027, 3, 15), config).events[0]
    calls: list[str] = []
    scheduler = BellScheduler(config, lambda event, _config, _source: calls.append(event.label))
    decision = scheduler.fire(planned, now=planned.scheduled_at)
    assert not decision and "kill switch" in decision.reason
    assert calls == []


def test_success_is_persisted_and_second_scheduler_cannot_refire(config_tree: Path) -> None:
    config = load_config(config_tree)
    config.settings.state_dir = config_tree.parent / "shared-state"
    planned = resolve_day(date(2027, 3, 15), config).events[0]
    calls: list[str] = []

    def transmit(event, _config, source) -> None:
        calls.append(f"{source}:{event.label}")

    first = BellScheduler(config, transmit)
    assert first.fire(planned, now=planned.scheduled_at)
    assert first.state.recent(1)[0]["result"] == "success"
    restarted = BellScheduler(config, transmit)
    second = restarted.fire(planned, now=planned.scheduled_at)
    assert not second and "already attempted" in second.reason
    assert len(calls) == 1
