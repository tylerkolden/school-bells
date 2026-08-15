from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from bell.config import Safety
from bell.safety import check_allowed_hours, check_daily_cap, check_kill_switch, check_sound


def safety(**changes) -> Safety:
    data = {
        "allowed_hours_start": time(6, 30),
        "allowed_hours_end": time(17, 30),
        "max_events_per_day": 3,
        "kill_switch_enabled": False,
        "kill_switch_until": None,
    }
    data.update(changes)
    return Safety(**data)


def test_kill_switch_blocks_and_expires() -> None:
    active = safety(kill_switch_enabled=True, kill_switch_until=date(2027, 1, 2))
    assert not check_kill_switch(date(2027, 1, 2), active)
    assert check_kill_switch(date(2027, 1, 3), active)


def test_manual_hours_override_is_explicit() -> None:
    now = datetime(2027, 1, 2, 3, 0, tzinfo=ZoneInfo("America/Denver"))
    assert not check_allowed_hours(now, safety(), manual=True)
    assert check_allowed_hours(now, safety(), manual=True, override=True)


def test_daily_cap_and_sound(tmp_path: Path) -> None:
    assert not check_daily_cap(3, safety())
    missing = check_sound(tmp_path / "missing.wav")
    assert not missing and "missing" in missing.reason
