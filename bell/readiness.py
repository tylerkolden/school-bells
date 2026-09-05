"""School-year review and side-effect-free calendar range previews."""
from __future__ import annotations

from datetime import date, timedelta

from bell.config import BellConfig
from bell.scheduler import DayPlan, resolve_day


def dates(start: date, end: date) -> list[date]:
    if not 2000 <= start.year <= 2100 or not 2000 <= end.year <= 2100:
        raise ValueError("Choose dates between 2000 and 2100")
    if end < start or (end - start).days > 370:
        raise ValueError("Date range must be ordered and no longer than 371 days")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def range_config(config: BellConfig, start: date, end: date, action: str,
                 schedule: str, reason: str) -> BellConfig:
    days = dates(start, end)
    if action not in {"schedule", "no_bells", "default"}:
        raise ValueError("Choose a valid calendar action")
    if action == "schedule" and schedule not in config.schedule_map:
        raise ValueError("Choose a valid schedule")
    if action == "no_bells" and not reason.strip():
        raise ValueError("Enter a reason for the no-bell range")
    if len(reason) > 200:
        raise ValueError("Keep the no-bell reason within 200 characters")
    preview = config.model_copy(deep=True)
    for day in days:
        preview.calendar.overrides.pop(day, None)
        preview.calendar.no_bell_dates.pop(day, None)
        if action == "schedule":
            preview.calendar.overrides[day] = schedule
        elif action == "no_bells":
            preview.calendar.no_bell_dates[day] = reason.strip()
    return preview


def review(config: BellConfig, start: date, end: date) -> list[dict]:
    result = []
    for day in dates(start, end):
        plan: DayPlan = resolve_day(day, config)
        explicit_silence = day in config.calendar.no_bell_dates
        source = "No-bell date" if explicit_silence else "Override" if day in config.calendar.overrides else (
            "Date range" if any(rule.start <= day <= rule.end for rule in config.calendar.date_ranges)
            else "Weekday default")
        issue = ""
        if not plan.events and not explicit_silence and (day.weekday() < 5 or plan.schedule_name):
            issue = "No bell events: confirm closure or assign a schedule"
        result.append({"day": day, "plan": plan, "source": source, "issue": issue})
    return result
