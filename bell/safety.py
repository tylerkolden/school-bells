"""Runtime safety checks. Configuration validation is not a substitute for these checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from bell.config import Safety

LOGGER = logging.getLogger(__name__)
MAINTENANCE_MARKER = Path("/opt/bell/.upgrade-incomplete")


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def within_allowed_hours(value: time, start: time, end: time) -> bool:
    return start <= value <= end if start <= end else value >= start or value <= end


def check_allowed_hours(
    now: datetime, safety: Safety, *, manual: bool = False, override: bool = False
) -> SafetyDecision:
    if within_allowed_hours(
        now.timetz().replace(tzinfo=None), safety.allowed_hours_start, safety.allowed_hours_end
    ):
        return SafetyDecision(True, "within allowed hours")
    if manual and override:
        LOGGER.warning("manual_allowed_hours_override", extra={"at": now.isoformat()})
        return SafetyDecision(True, "manual allowed-hours override accepted and logged")
    return SafetyDecision(False, "outside allowed bell hours")


def check_kill_switch(on_date: date, safety: Safety) -> SafetyDecision:
    if not safety.kill_switch_enabled:
        return SafetyDecision(True, "kill switch is off")
    if safety.kill_switch_until is not None and on_date > safety.kill_switch_until:
        return SafetyDecision(True, "kill switch has expired")
    suffix = f" through {safety.kill_switch_until}" if safety.kill_switch_until else ""
    return SafetyDecision(False, f"kill switch is enabled{suffix}")


def check_pause(now: datetime, safety: Safety) -> SafetyDecision:
    if safety.pause_until is None or now >= safety.pause_until:
        return SafetyDecision(True, "temporary pause is inactive")
    reason = safety.pause_reason or "operator pause"
    return SafetyDecision(
        False, f"bell service paused until {safety.pause_until.isoformat()}: {reason}"
    )


def check_daily_cap(attempt_count: int, safety: Safety) -> SafetyDecision:
    if attempt_count >= safety.max_events_per_day:
        return SafetyDecision(False, f"daily event cap ({safety.max_events_per_day}) reached")
    return SafetyDecision(True, "daily event cap not reached")


def check_sound(sound: Path) -> SafetyDecision:
    if not sound.is_file():
        return SafetyDecision(False, f"sound file is missing: {sound}")
    try:
        with sound.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return SafetyDecision(False, f"sound file is unreadable: {exc}")
    return SafetyDecision(True, "sound file exists and is readable")


def evaluate_fire(
    now: datetime,
    safety: Safety,
    sound: Path,
    attempt_count: int,
    *,
    manual: bool = False,
    override_hours: bool = False,
) -> SafetyDecision:
    if MAINTENANCE_MARKER.exists():
        return SafetyDecision(False, "upgrade maintenance; transmissions are blocked")
    checks = (
        check_kill_switch(now.date(), safety),
        check_pause(now, safety),
        check_allowed_hours(now, safety, manual=manual, override=override_hours),
        check_daily_cap(attempt_count, safety),
        check_sound(sound),
    )
    for decision in checks:
        if not decision.allowed:
            return decision
    return SafetyDecision(True, "all fire-time safety checks passed")
