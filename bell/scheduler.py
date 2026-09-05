"""Daily wall-clock schedule resolution, persistence, and execution guards."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from bell.config import BellConfig, BellEvent, load_config
from bell.safety import SafetyDecision, evaluate_fire

LOGGER = logging.getLogger(__name__)
TransmitCallback = Callable[[BellEvent, BellConfig, str], object]


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    event: BellEvent
    source: str
    scheduled_at: datetime
    action_id: str | None = None

    @property
    def key(self) -> str:
        if self.action_id:
            return f"manual:{self.action_id}"
        return f"{self.scheduled_at:%Y-%m-%d}|{self.event.time:%H:%M}|{self.event.label}|{self.event.zone}"


@dataclass(frozen=True, slots=True)
class DayPlan:
    day: date
    schedule_name: str | None
    events: tuple[PlannedEvent, ...]
    reason: str | None = None


def resolve_day(day: date, config: BellConfig) -> DayPlan:
    calendar = config.calendar
    if day in calendar.no_bell_dates:
        return DayPlan(day, None, (), calendar.no_bell_dates[day])
    schedule_name: str | None = None
    if day in calendar.overrides:
        schedule_name = calendar.overrides[day]
    else:
        for rule in calendar.date_ranges:
            if rule.start <= day <= rule.end:
                schedule_name = rule.schedule
                break
    if schedule_name is None:
        schedule_name = calendar.weekday_defaults.get(day.strftime("%A").lower())
    timezone = ZoneInfo(config.settings.timezone)
    events: list[PlannedEvent] = []
    if schedule_name:
        for event in config.schedule_map[schedule_name].events:
            events.append(
                PlannedEvent(event, schedule_name, datetime.combine(day, event.time, timezone))
            )
    for item in config.standing_items:
        if schedule_name and item.enabled:
            event = BellEvent.model_validate(item.model_dump(exclude={"enabled"}))
            events.append(
                PlannedEvent(event, "Standing", datetime.combine(day, item.time, timezone))
            )
    events.sort(key=lambda item: item.scheduled_at)
    reason = None if events else "no schedule assigned"
    return DayPlan(day, schedule_name, tuple(events), reason)


def upcoming_events(
    config: BellConfig,
    now: datetime,
    *,
    limit: int = 5,
    search_days: int = 370,
) -> tuple[PlannedEvent, ...]:
    """Return future school events across date boundaries in deterministic wall-clock order."""
    if limit < 1:
        return ()
    found: list[PlannedEvent] = []
    for offset in range(search_days + 1):
        plan = resolve_day(now.date() + timedelta(days=offset), config)
        found.extend(item for item in plan.events if item.scheduled_at > now)
        if len(found) >= limit:
            break
    found.sort(key=lambda item: item.scheduled_at)
    return tuple(found[:limit])


class FireState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS fire_attempts (
                day TEXT NOT NULL, event_key TEXT NOT NULL, attempted_at TEXT NOT NULL,
                result TEXT NOT NULL, detail TEXT NOT NULL,
                PRIMARY KEY(day, event_key)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS zone_acceptance (
                zone TEXT PRIMARY KEY, confirmed_at TEXT NOT NULL,
                observer TEXT NOT NULL, note TEXT NOT NULL
                )"""
            )
            fire_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(fire_attempts)").fetchall()
            }
            for name in ("source", "label", "zone", "sound", "scheduled_at"):
                if name not in fire_columns:
                    connection.execute(
                        f"ALTER TABLE fire_attempts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS api_requests (
                idempotency_key TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, request_hash TEXT NOT NULL DEFAULT ''
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL
                )"""
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(api_requests)").fetchall()
            }
            if "request_hash" not in columns:
                connection.execute(
                    "ALTER TABLE api_requests ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "DELETE FROM api_requests WHERE julianday(created_at) < julianday('now', '-30 days')"
            )
            connection.execute(
                "DELETE FROM fire_attempts WHERE julianday(attempted_at) < julianday('now', '-400 days')"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def has_attempt(self, day: date, event_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM fire_attempts WHERE day=? AND event_key=?",
                (day.isoformat(), event_key),
            ).fetchone()
        return row is not None

    def attempt_count(self, day: date) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM fire_attempts WHERE day=?", (day.isoformat(),)
            ).fetchone()
        return int(row[0]) if row else 0

    def record_once(
        self,
        day: date,
        event_key: str,
        result: str,
        detail: str,
        now: datetime,
        planned: PlannedEvent | None = None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO fire_attempts
                (day,event_key,attempted_at,result,detail,source,label,zone,sound,scheduled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    day.isoformat(),
                    event_key,
                    now.isoformat(),
                    result,
                    detail,
                    planned.source if planned else "",
                    planned.event.label if planned else "",
                    planned.event.zone if planned else "",
                    planned.event.sound if planned else "",
                    planned.scheduled_at.isoformat() if planned else now.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def update_result(self, day: date, event_key: str, result: str, detail: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE fire_attempts SET result=?, detail=? WHERE day=? AND event_key=?",
                (result, detail, day.isoformat(), event_key),
            )

    def recent(
        self,
        limit: int = 20,
        *,
        result: str | None = None,
        zone: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, str]]:
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (("result", result), ("zone", zone), ("source", source)):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(min(max(limit, 1), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT day,event_key,attempted_at,result,detail,source,label,zone,sound,scheduled_at
                FROM fire_attempts"""
                + where
                + " ORDER BY attempted_at DESC LIMIT ?",
                values,
            ).fetchall()
        keys = (
            "day",
            "event_key",
            "attempted_at",
            "result",
            "detail",
            "source",
            "label",
            "zone",
            "sound",
            "scheduled_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def record_audit(self, action: str, target: str, detail: str, now: datetime) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_events (occurred_at,action,target,detail) VALUES (?, ?, ?, ?)",
                (now.isoformat(), action, target, detail),
            )

    def recent_audit(self, limit: int = 100) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT occurred_at,action,target,detail FROM audit_events
                ORDER BY id DESC LIMIT ?""",
                (min(max(limit, 1), 1000),),
            ).fetchall()
        keys = ("occurred_at", "action", "target", "detail")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def confirm_zone(self, zone: str, observer: str, note: str, now: datetime) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO zone_acceptance (zone,confirmed_at,observer,note)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(zone) DO UPDATE SET confirmed_at=excluded.confirmed_at,
                observer=excluded.observer,note=excluded.note""",
                (zone, now.isoformat(), observer, note),
            )

    def zone_confirmations(self) -> dict[str, dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT zone,confirmed_at,observer,note FROM zone_acceptance"
            ).fetchall()
        return {
            row[0]: {"confirmed_at": row[1], "observer": row[2], "note": row[3]} for row in rows
        }

    def api_result(self, idempotency_key: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at,status,detail,request_hash FROM api_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "created_at": row[0],
            "status": row[1],
            "detail": row[2],
            "request_hash": row[3],
        }

    def claim_api_request(
        self, idempotency_key: str, now: datetime, request_hash: str = ""
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO api_requests
                (idempotency_key,created_at,status,detail,request_hash) VALUES (?, ?, ?, ?, ?)""",
                (idempotency_key, now.isoformat(), "processing", "request claimed", request_hash),
            )
        return cursor.rowcount == 1

    def expire_stale_api_request(
        self, idempotency_key: str, now: datetime, max_age_seconds: int = 300
    ) -> bool:
        cutoff = (now - timedelta(seconds=max_age_seconds)).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE api_requests SET status='indeterminate',
                detail='request was interrupted; use a new key after verifying receivers'
                WHERE idempotency_key=? AND status='processing' AND created_at<?""",
                (idempotency_key, cutoff),
            )
        return cursor.rowcount == 1

    def finish_api_request(self, idempotency_key: str, status: str, detail: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE api_requests SET status=?,detail=? WHERE idempotency_key=?",
                (status, detail, idempotency_key),
            )


class BellScheduler:
    def __init__(self, config: BellConfig, transmit: TransmitCallback) -> None:
        self.config = config
        self.transmit = transmit
        self.timezone = ZoneInfo(config.settings.timezone)
        self.state = FireState(config.state_path / "fires.sqlite3")
        self.scheduler = BackgroundScheduler(timezone=self.timezone)

    def start(self) -> None:
        self.scheduler.add_job(
            self.register_day,
            CronTrigger(hour=0, minute=5, timezone=self.timezone),
            id="daily-resolution",
            replace_existing=True,
        )
        self.register_day(datetime.now(self.timezone).date())
        self.scheduler.start()

    def shutdown(self, wait: bool = True) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)

    def register_day(
        self, day: date | None = None, *, include_recent_misfires: bool = True
    ) -> DayPlan:
        actual_day = day or datetime.now(self.timezone).date()
        current = datetime.now(self.timezone)
        plan = resolve_day(actual_day, self.config)
        prefix = f"bell-{actual_day.isoformat()}-"
        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                self.scheduler.remove_job(job.id)
        for index, planned in enumerate(plan.events):
            if not include_recent_misfires and planned.scheduled_at <= current:
                continue
            self.scheduler.add_job(
                self.fire,
                DateTrigger(run_date=planned.scheduled_at),
                args=(planned,),
                id=f"{prefix}{index}",
                misfire_grace_time=60,
                replace_existing=True,
            )
        return plan

    def fire(
        self,
        planned: PlannedEvent,
        *,
        now: datetime | None = None,
        manual: bool = False,
        override_hours: bool = False,
        expected_config_hash: str | None = None,
    ) -> SafetyDecision:
        config = self.config
        if expected_config_hash is not None and config.hash != expected_config_hash:
            return SafetyDecision(False, "Configuration changed; review a new page")
        current = now or datetime.now(self.timezone)
        if not manual and current - planned.scheduled_at > timedelta(seconds=60):
            reason = "event is more than 60 seconds late; skipped"
            self.state.record_once(current.date(), planned.key, "missed", reason, current, planned)
            LOGGER.warning("bell_missed", extra={"event": planned.key, "reason": reason})
            return SafetyDecision(False, reason)
        if self.state.has_attempt(current.date(), planned.key):
            return SafetyDecision(False, "event already attempted today")
        sound = config.sounds_path / planned.event.sound
        decision = evaluate_fire(
            current,
            config.safety,
            sound,
            self.state.attempt_count(current.date()),
            manual=manual,
            override_hours=override_hours,
        )
        if not decision.allowed:
            self.state.record_once(
                current.date(), planned.key, "blocked", decision.reason, current, planned
            )
            LOGGER.warning("bell_blocked", extra={"event": planned.key, "reason": decision.reason})
            return decision
        # Claim the event before transmission so a concurrent/restarted executor cannot double-fire.
        if not self.state.record_once(
            current.date(), planned.key, "started", "transmission claimed", current, planned
        ):
            return SafetyDecision(False, "event already attempted today")
        try:
            self.transmit(planned.event, config, planned.source)
        except Exception as exc:
            self.state.update_result(current.date(), planned.key, "failed", str(exc))
            LOGGER.exception("bell_transmit_failed", extra={"event": planned.key})
            return SafetyDecision(False, f"transmission failed: {exc}")
        self.state.update_result(current.date(), planned.key, "success", "transmission completed")
        return SafetyDecision(True, "transmission completed")


def show_plan(config_dir: Path, day: date) -> str:
    plan = resolve_day(day, load_config(config_dir))
    lines = [f"{day}: {plan.schedule_name or 'No bells'}"]
    if plan.reason:
        lines.append(f"Reason: {plan.reason}")
    lines.extend(
        f"{planned.scheduled_at:%H:%M}  {planned.event.label}  [{planned.event.zone}]"
        for planned in plan.events
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show")
    show.add_argument("--date", type=date.fromisoformat, default=date.today())
    show.add_argument("--config-dir", type=Path, default=Path("config"))
    args = parser.parse_args(argv)
    print(show_plan(args.config_dir, args.date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
