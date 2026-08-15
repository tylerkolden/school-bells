"""Front-office web application."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ruamel.yaml import YAML
from starlette.middleware.sessions import SessionMiddleware

from bell.config import BellConfig, ConfigLoadError, load_config
from bell.safety import evaluate_fire
from bell.scheduler import BellScheduler, PlannedEvent, resolve_day

LOGGER = logging.getLogger(__name__)
ReloadCallback = Callable[[], None]
HealthProvider = Callable[[], dict[str, Any]]


class APITrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sound: str
    zone: str
    label: str = Field(default="Automation trigger", min_length=1, max_length=100)
    priority: int = Field(default=50, ge=0, le=100)
    repeat_count: int = Field(default=1, ge=1, le=10)
    repeat_interval_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    override_hours: bool = False

    @field_validator("sound")
    @classmethod
    def sound_is_library_name(cls, value: str) -> str:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("sound must be a filename in the configured sound library")
        return value


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, identity: str) -> bool:
        now = time.monotonic()
        with self._lock:
            requests = self._requests[identity]
            while requests and requests[0] <= now - self.window_seconds:
                requests.popleft()
            if len(requests) >= self.limit:
                return False
            requests.append(now)
            return True


def create_app(
    config_dir: Path | str = Path("config"),
    *,
    scheduler: BellScheduler | None = None,
    reload_callback: ReloadCallback | None = None,
    health_provider: HealthProvider | None = None,
    password: str | None = None,
) -> FastAPI:
    directory = Path(config_dir).resolve()
    configured_password = password if password is not None else os.environ.get("BELL_UI_PASSWORD")
    if not configured_password:
        raise RuntimeError("BELL_UI_PASSWORD must be set before starting the front-office UI")
    secret = os.environ.get("BELL_UI_SESSION_SECRET") or secrets.token_urlsafe(32)
    secure_transport = bool(os.environ.get("BELL_TLS_CERTFILE"))
    signer = URLSafeTimedSerializer(secret, salt="bell-manual-confirm")
    app = FastAPI(title="School Bell Office", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        same_site="strict",
        https_only=secure_transport,
    )
    assets = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=assets / "static"), name="static")
    templates = Jinja2Templates(directory=assets / "templates")
    app.state.config_dir = directory
    app.state.scheduler = scheduler
    rate_limiter = RateLimiter(load_config(directory).settings.api_rate_limit_per_minute)
    login_limiter = RateLimiter(5, 300.0)
    calendar_lock = threading.Lock()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/") or request.session.get("authenticated"):
            response.headers["Cache-Control"] = "no-store"
        if secure_transport:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def config() -> BellConfig:
        return load_config(directory)

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not isinstance(token, str):
            token = secrets.token_urlsafe(32)
            request.session["csrf_token"] = token
        return token

    def verify_csrf(request: Request, supplied: str) -> None:
        expected = request.session.get("csrf_token")
        if not isinstance(expected, str) or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="Invalid or missing form security token")

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {"authenticated": True, "csrf_token": csrf_token(request), **context},
        )

    def require_auth(request: Request) -> None:
        if not request.session.get("authenticated"):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    def api_scope(request: Request) -> str:
        supplied = request.headers.get("X-Bell-API-Key", "")
        normal = os.environ.get("BELL_API_KEY", "")
        emergency = os.environ.get("BELL_EMERGENCY_API_KEY", "")
        if emergency and secrets.compare_digest(supplied, emergency):
            scope = "emergency"
        elif normal and secrets.compare_digest(supplied, normal):
            scope = "normal"
        else:
            raise HTTPException(status_code=401, detail="A valid Bell API key is required")
        identity = hashlib.sha256(supplied.encode()).hexdigest()
        if not rate_limiter.allow(identity):
            raise HTTPException(status_code=429, detail="Automation rate limit exceeded")
        return scope

    @app.exception_handler(ConfigLoadError)
    async def config_error(request: Request, exc: ConfigLoadError) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"authenticated": bool(request.session.get("authenticated")), "message": str(exc)},
            status_code=400,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers.get("Location", "/login"), status_code=303)
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "authenticated": bool(request.session.get("authenticated")),
                "csrf_token": csrf_token(request),
                "message": str(exc.detail),
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, failed: bool = False) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"authenticated": False, "failed": failed, "csrf_token": csrf_token(request)},
        )

    @app.post("/login")
    def login(
        request: Request,
        submitted_password: str = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        verify_csrf(request, csrf)
        client_address = request.client.host if request.client else "unknown"
        if not login_limiter.allow(client_address):
            LOGGER.warning(
                "ui_action",
                extra={"action": "login", "result": "rate_limited", "client": client_address},
            )
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.")
        if not secrets.compare_digest(submitted_password, configured_password):
            LOGGER.warning(
                "ui_action",
                extra={"action": "login", "result": "denied", "client": client_address},
            )
            return RedirectResponse("/login?failed=true", status_code=303)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        LOGGER.info(
            "ui_action",
            extra={"action": "login", "result": "success", "client": client_address},
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        plan = resolve_day(now.date(), cfg)
        next_event = next((item for item in plan.events if item.scheduled_at > now), None)
        return render(
            request,
            "today.html",
            plan=plan,
            now=now,
            next_event=next_event,
            kill_switch=cfg.safety.kill_switch_enabled,
        )

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page(request: Request, selected: date | None = None) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        today_local = datetime.now(ZoneInfo(cfg.settings.timezone)).date()
        chosen = selected or today_local
        days = [resolve_day(today_local + timedelta(days=offset), cfg) for offset in range(14)]
        direct_schedule = cfg.calendar.overrides.get(chosen, "")
        direct_reason = cfg.calendar.no_bell_dates.get(chosen, "")
        return render(
            request,
            "calendar.html",
            selected=chosen,
            days=days,
            schedules=cfg.schedules,
            current=resolve_day(chosen, cfg),
            direct_schedule=direct_schedule,
            direct_reason=direct_reason,
            config_hash=cfg.hash,
            tomorrow=today_local + timedelta(days=1),
        )

    def write_calendar(
        day: date,
        schedule_name: str | None,
        reason: str | None,
        expected_hash: str,
    ) -> None:
        path = directory / "calendar.yaml"
        with calendar_lock:
            cfg = config()
            if not expected_hash or not secrets.compare_digest(expected_hash, cfg.hash):
                raise HTTPException(
                    status_code=409,
                    detail="The calendar changed after this page loaded. Refresh and try again.",
                )
            backup_dir = cfg.state_path / "config-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / (
                f"calendar-{datetime.now(ZoneInfo(cfg.settings.timezone)):%Y%m%dT%H%M%S%f}.yaml"
            )
            shutil.copy2(path, backup)
            yaml = YAML()
            yaml.preserve_quotes = True
            with path.open("r", encoding="utf-8") as handle:
                document = yaml.load(handle)
            calendar = document["calendar"]
            calendar.setdefault("overrides", {})
            calendar.setdefault("no_bell_dates", {})
            calendar["overrides"].pop(day, None)
            calendar["overrides"].pop(day.isoformat(), None)
            calendar["no_bell_dates"].pop(day, None)
            calendar["no_bell_dates"].pop(day.isoformat(), None)
            if reason:
                calendar["no_bell_dates"][day] = reason
            elif schedule_name:
                calendar["overrides"][day] = schedule_name
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix="calendar-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary_name = handle.name
                    yaml.dump(document, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temporary_name).replace(path)
                load_config(directory)  # validate before announcing success/reload
            except Exception:
                shutil.copy2(backup, path)
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
                raise
            backups = sorted(
                backup_dir.glob("calendar-*.yaml"), key=lambda item: item.stat().st_mtime
            )
            for old_backup in backups[:-30]:
                old_backup.unlink()
        if reload_callback:
            reload_callback()

    @app.post("/calendar")
    def calendar_save(
        request: Request,
        selected: date = Form(),  # noqa: B008
        schedule_name: str = Form(default=""),
        no_bell_reason: str = Form(default=""),
        calendar_action: str = Form(default=""),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        if schedule_name and schedule_name not in cfg.schedule_map:
            raise HTTPException(400, "That schedule does not exist")
        if not calendar_action:
            calendar_action = "no_bells" if no_bell_reason.strip() else "schedule"
        if calendar_action not in {"schedule", "no_bells", "default"}:
            raise HTTPException(400, "Choose a valid calendar action")
        if calendar_action == "schedule" and not schedule_name:
            raise HTTPException(400, "Choose a schedule or enter a no-bell reason")
        if calendar_action == "no_bells" and not no_bell_reason.strip():
            raise HTTPException(400, "Enter a reason for the no-bell day")
        write_calendar(
            selected,
            schedule_name if calendar_action == "schedule" else None,
            no_bell_reason.strip() if calendar_action == "no_bells" else None,
            config_hash,
        )
        LOGGER.info(
            "ui_action",
            extra={"action": "calendar_change", "target": selected.isoformat(), "result": "success"},
        )
        return RedirectResponse(f"/calendar?selected={selected.isoformat()}", status_code=303)

    @app.get("/manual", response_class=HTMLResponse)
    def manual_page(request: Request, message: str | None = None) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        sounds = sorted(path.name for path in cfg.sounds_path.iterdir() if path.is_file())
        return render(request, "manual.html", config=cfg, sounds=sounds, message=message)

    @app.post("/manual/prepare", response_class=HTMLResponse)
    def manual_prepare(
        request: Request,
        sound: str = Form(),
        zone: str = Form(),
        override_hours: bool = Form(default=False),
        csrf: str = Form(),
    ) -> HTMLResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        if zone not in cfg.zone_map or sound not in {path.name for path in cfg.sounds_path.iterdir()}:
            raise HTTPException(400, "Choose a valid sound and zone")
        payload = {"sound": sound, "zone": zone, "override_hours": override_hours}
        token = signer.dumps(payload)
        return render(
            request,
            "confirm.html",
            token=token,
            sound=sound,
            zone=cfg.zone_map[zone],
            override_hours=override_hours,
        )

    @app.post("/manual/fire")
    def manual_fire(
        request: Request,
        confirm_token: str = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        try:
            payload = signer.loads(confirm_token, max_age=120)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(400, "Confirmation expired or is invalid. Please start again.") from exc
        cfg = config()
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        sound = cfg.sounds_path / payload["sound"]
        count = scheduler.state.attempt_count(now.date()) if scheduler else 0
        decision = evaluate_fire(
            now,
            cfg.safety,
            sound,
            count,
            manual=True,
            override_hours=bool(payload.get("override_hours")),
        )
        if not decision.allowed:
            LOGGER.warning(
                "ui_action",
                extra={"action": "manual_fire", "target": payload, "result": "blocked", "reason": decision.reason},
            )
            return RedirectResponse(f"/manual?message={decision.reason}", status_code=303)
        if scheduler is None:
            result_reason = "validated (test mode; no scheduler attached)"
        else:
            event_time = now.timetz().replace(second=0, microsecond=0, tzinfo=None)
            from bell.config import BellEvent

            event = BellEvent(time=event_time, sound=payload["sound"], zone=payload["zone"], label="Manual office trigger")
            planned = PlannedEvent(event, "Manual", now)
            decision = scheduler.fire(
                planned,
                now=now,
                manual=True,
                override_hours=bool(payload.get("override_hours")),
            )
            result_reason = decision.reason
        LOGGER.info(
            "ui_action",
            extra={"action": "manual_fire", "target": payload, "result": result_reason},
        )
        return RedirectResponse(f"/manual?message={result_reason}", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        recent = scheduler.state.recent() if scheduler else []
        health = health_provider() if health_provider else None
        return render(request, "status.html", config=cfg, recent=recent, health=health)

    @app.get("/api/v1/health")
    def api_health(request: Request) -> dict[str, Any]:
        api_scope(request)
        cfg = config()
        return health_provider() if health_provider else {
            "status": "ok",
            "config_valid": True,
            "config_hash": cfg.hash,
        }

    @app.get("/api/v1/today")
    def api_today(request: Request) -> dict[str, Any]:
        api_scope(request)
        cfg = config()
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        plan = resolve_day(now.date(), cfg)
        return {
            "date": now.date().isoformat(),
            "schedule": plan.schedule_name,
            "reason": plan.reason,
            "events": [
                {
                    "time": item.scheduled_at.isoformat(),
                    "label": item.event.label,
                    "zone": item.event.zone,
                    "priority": item.event.priority,
                }
                for item in plan.events
            ],
        }

    @app.post("/api/v1/trigger")
    def api_trigger(request: Request, trigger: APITrigger) -> JSONResponse:
        scope = api_scope(request)
        if scheduler is None:
            raise HTTPException(status_code=503, detail="Scheduler is not attached")
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not idempotency_key or len(idempotency_key) > 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in idempotency_key
        ):
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key is required and may contain only letters, numbers, dot, dash, and underscore",
            )
        existing = scheduler.state.api_result(idempotency_key)
        if existing:
            request_hash = hashlib.sha256(trigger.model_dump_json().encode()).hexdigest()
            if existing["request_hash"] and not secrets.compare_digest(
                existing["request_hash"], request_hash
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different request payload",
                )
            scheduler.state.expire_stale_api_request(idempotency_key, datetime.now(ZoneInfo(config().settings.timezone)))
            existing = scheduler.state.api_result(idempotency_key) or existing
            status_code = 409 if existing["status"] == "indeterminate" else 200
            return JSONResponse(
                {"idempotent_replay": True, **existing}, status_code=status_code
            )
        cfg = config()
        if trigger.zone not in cfg.zone_map:
            raise HTTPException(status_code=400, detail="Unknown zone")
        if not (cfg.sounds_path / trigger.sound).is_file():
            raise HTTPException(status_code=400, detail="Unknown sound")
        if trigger.repeat_count > cfg.safety.max_repeats:
            raise HTTPException(status_code=400, detail="repeat_count exceeds the configured safety limit")
        emergency = trigger.priority >= cfg.safety.emergency_priority_threshold or trigger.override_hours
        if emergency and scope != "emergency":
            raise HTTPException(
                status_code=403,
                detail="The emergency API key is required for emergency priority or hours override",
            )
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        request_hash = hashlib.sha256(trigger.model_dump_json().encode()).hexdigest()
        if not scheduler.state.claim_api_request(idempotency_key, now, request_hash):
            result = scheduler.state.api_result(idempotency_key) or {
                "status": "processing",
                "detail": "another worker claimed this request",
            }
            return JSONResponse({"idempotent_replay": True, **result}, status_code=200)
        from bell.config import BellEvent

        event = BellEvent(
            time=now.timetz().replace(second=0, microsecond=0, tzinfo=None),
            sound=trigger.sound,
            zone=trigger.zone,
            label=f"{trigger.label} [{idempotency_key[:8]}]",
            priority=trigger.priority,
            repeat_count=trigger.repeat_count,
            repeat_interval_seconds=trigger.repeat_interval_seconds,
            busy_policy="preempt" if emergency else "queue",
        )
        planned = PlannedEvent(event, "Automation API", now)
        decision = scheduler.fire(
            planned,
            now=now,
            manual=True,
            override_hours=trigger.override_hours,
        )
        status = "success" if decision.allowed else "blocked"
        scheduler.state.finish_api_request(idempotency_key, status, decision.reason)
        LOGGER.warning(
            "ui_action" if emergency else "api_action",
            extra={
                "action": "api_trigger",
                "target": {"zone": trigger.zone, "sound": trigger.sound},
                "priority": trigger.priority,
                "result": status,
            },
        )
        return JSONResponse(
            {"idempotent_replay": False, "status": status, "detail": decision.reason},
            status_code=200 if decision.allowed else 409,
        )

    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    app = create_app(args.config_dir)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
