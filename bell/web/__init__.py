"""Front-office web application."""

from __future__ import annotations

import argparse
import calendar as calendar_module
import csv
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from starlette.middleware.sessions import SessionMiddleware

from bell import __version__
from bell.acceptance import AcceptanceStore, ReceiverEvidence, zone_fingerprint
from bell.alerts import AlertDispatcher
from bell.audio import AudioProcessingError, AudioToolMissing, codec_spec, prep, probe_audio
from bell.auth import AuthError, AuthStore
from bell.branding import BrandingError, normalize_logo
from bell.calibration import (
    CalibrationError,
    derive_poly_calibration,
    sanitize_capture,
    valid_header_or_none,
)
from bell.config import (
    BellConfig,
    BellEvent,
    BellSchedule,
    ConfigLoadError,
    DateRangeRule,
    Destination,
    PolyCalibration,
    Safety,
    Settings,
    StandingItem,
    Zone,
    load_config,
)
from bell.continuity import ContinuityPlan, ContinuityStore
from bell.manual import ManualActions, sound_digest
from bell.probe import capture as capture_rtp
from bell.probe import load_capture, save_capture
from bell.readiness import range_config, review
from bell.recovery import (
    RecoveryError,
    create_portable_backup,
    create_support_bundle,
    restore_portable_backup,
)
from bell.safety import check_kill_switch, evaluate_fire, within_allowed_hours
from bell.scheduler import BellScheduler, PlannedEvent, resolve_day, upcoming_events
from bell.update import UpdateRequestError, load_update_status, queue_update_request

LOGGER = logging.getLogger(__name__)
ReloadCallback = Callable[[], None]
HealthProvider = Callable[[], dict[str, Any]]
CancelCallback = Callable[[str], bool]


class APITrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sound: str = Field(min_length=1, max_length=255)
    zone: str = Field(min_length=1, max_length=100)
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

    @field_validator("sound", "zone", "label")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("must not contain control characters")
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
    cancel_callback: CancelCallback | None = None,
    password: str | None = None,
) -> FastAPI:
    directory = Path(config_dir).resolve()
    configured_password = password if password is not None else os.environ.get("BELL_UI_PASSWORD")
    if not configured_password:
        raise RuntimeError("BELL_UI_PASSWORD must be set before starting the front-office UI")
    secret = os.environ.get("BELL_UI_SESSION_SECRET") or secrets.token_urlsafe(32)
    secure_transport = bool(os.environ.get("BELL_TLS_CERTFILE"))
    updates_enabled = os.environ.get("BELL_OTA_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    receiver_dashboard_url = os.environ.get("BELL_RECEIVER_DASHBOARD_URL", "").strip()
    if receiver_dashboard_url:
        parsed_dashboard = urlsplit(receiver_dashboard_url)
        if (
            parsed_dashboard.scheme not in {"http", "https"}
            or not parsed_dashboard.netloc
            or parsed_dashboard.username
            or parsed_dashboard.password
        ):
            LOGGER.warning("invalid_receiver_dashboard_url")
            receiver_dashboard_url = ""
    signer = URLSafeTimedSerializer(secret, salt="bell-manual-confirm")
    update_signer = URLSafeTimedSerializer(secret, salt="bell-update-confirm")
    calendar_signer = URLSafeTimedSerializer(secret, salt="bell-calendar-review")
    app = FastAPI(title="School Bell Office", docs_url=None, redoc_url=None)
    assets = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=assets / "static"), name="static")
    templates = Jinja2Templates(directory=assets / "templates")
    auth_store = AuthStore(
        load_config(directory).state_path / "auth" / "users.json", configured_password
    )
    app.state.config_dir = directory
    app.state.scheduler = scheduler
    manual_actions = ManualActions(load_config(directory).state_path / "manual-actions.sqlite3")
    continuity = ContinuityStore(load_config(directory).state_path / "continuity.sqlite3")
    acceptance = AcceptanceStore(load_config(directory).state_path / "receiver-acceptance.sqlite3")
    rate_limiter = RateLimiter(load_config(directory).settings.api_rate_limit_per_minute)
    login_limiter = RateLimiter(5, 300.0)
    capture_limiter = RateLimiter(6, 300.0)
    config_lock = threading.Lock()
    calibration_lock = threading.Lock()
    update_lock = threading.Lock()

    def require_updates_enabled() -> None:
        if not updates_enabled:
            raise HTTPException(
                status_code=503,
                detail="Production OTA is disabled in this local test environment",
            )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.monotonic()
        request_id = secrets.token_hex(8)
        restricted = request.url.path.startswith(("/setup", "/updates", "/recovery", "/account"))
        if (
            restricted
            and request.session.get("authenticated")
            and request.session.get("role", "admin") != "admin"
        ):
            response = templates.TemplateResponse(
                request,
                "error.html",
                {
                    **template_identity(request),
                    "message": "Administrator access is required for this area.",
                },
                status_code=403,
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
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
        LOGGER.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_seconds": round(time.monotonic() - started, 6),
                "client": request.client.host if request.client else "unknown",
            },
        )
        return response

    # Added after the HTTP middleware so session data is available to its pre-route role guard.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        same_site="strict",
        https_only=secure_transport,
        max_age=8 * 60 * 60,
    )

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
        cfg = config()
        return templates.TemplateResponse(
            request,
            name,
            {
                "authenticated": True,
                "csrf_token": csrf_token(request),
                "receiver_dashboard_url": receiver_dashboard_url or None,
                "brand": {
                    "school_name": cfg.settings.school_name,
                    "subtitle": cfg.settings.console_subtitle,
                    "has_logo": bool(cfg.logo_path and cfg.logo_path.is_file()),
                },
                "current_user": {
                    "username": request.session.get("username", "admin"),
                    "role": request.session.get("role", "admin"),
                },
                **context,
            },
        )

    def require_auth(request: Request) -> None:
        if not request.session.get("authenticated"):
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        if request.session.get("auth_revision", 0) != auth_store.revision:
            request.session.clear()
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    def record_audit(action: str, target: str, detail: str) -> None:
        if scheduler:
            try:
                scheduler.state.record_audit(
                    action,
                    target,
                    detail,
                    datetime.now(ZoneInfo(config().settings.timezone)),
                )
            except (OSError, sqlite3.Error):
                # A full or temporarily unavailable state volume must not turn an
                # already-successful configuration write into a misleading failure.
                LOGGER.exception("audit_record_failed")

    def template_identity(request: Request) -> dict[str, Any]:
        """Common safe template context, including for handled error responses."""
        try:
            cfg = config()
            brand = {
                "school_name": cfg.settings.school_name,
                "subtitle": cfg.settings.console_subtitle,
                "has_logo": bool(cfg.logo_path and cfg.logo_path.is_file()),
            }
        except ConfigLoadError:
            brand = {
                "school_name": "School Bell",
                "subtitle": "Operations Console",
                "has_logo": False,
            }
        return {
            "authenticated": bool(request.session.get("authenticated")),
            "csrf_token": csrf_token(request),
            "brand": brand,
            "current_user": {
                "username": request.session.get("username", "admin"),
                "role": request.session.get("role", "admin"),
            },
        }

    def operations_snapshot(cfg: BellConfig | None = None) -> dict[str, Any]:
        current = cfg or config()
        now = datetime.now(ZoneInfo(current.settings.timezone))
        plan = resolve_day(now.date(), current)
        upcoming = upcoming_events(current, now, limit=5)
        health = (
            health_provider()
            if health_provider
            else {
                "ready": False,
                "readiness_reasons": ["Runtime health unavailable"],
                "last_fire": None,
                "active_page": None,
            }
        )
        pause_active = bool(current.safety.pause_until and now < current.safety.pause_until)
        events = [
            {
                "time": item.scheduled_at.isoformat(),
                "display_time": item.scheduled_at.strftime("%I:%M %p").lstrip("0"),
                "display_day": (
                    "Today"
                    if item.scheduled_at.date() == now.date()
                    else item.scheduled_at.strftime("%a, %b %d")
                ),
                "label": item.event.label,
                "zone": item.event.zone,
                "sound": item.event.sound,
                "source": item.source,
                "priority": item.event.priority,
            }
            for item in upcoming
        ]
        blocked_reasons: list[str] = []
        kill_active = not check_kill_switch(now.date(), current.safety).allowed
        if kill_active:
            blocked_reasons.append("kill switch enabled")
        if pause_active:
            blocked_reasons.append(f"paused: {current.safety.pause_reason}")
        if not health.get("ready", False):
            blocked_reasons.extend(str(item) for item in health.get("readiness_reasons", []))
        return {
            "server_time": now.isoformat(),
            "timezone": current.settings.timezone,
            "date": now.date().isoformat(),
            "display_date": now.strftime("%A · %B %d, %Y"),
            "schedule": plan.schedule_name,
            "today_reason": plan.reason,
            "today_event_count": len(plan.events),
            "next_bell": events[0] if events else None,
            "upcoming": events,
            "ready": bool(health.get("ready", False)) and not blocked_reasons,
            "blocked_reasons": blocked_reasons,
            "last_fire": health.get("last_fire"),
            "active_page": health.get("active_page"),
            "kill_switch": kill_active,
            "pause": {
                "active": pause_active,
                "until": current.safety.pause_until.isoformat()
                if current.safety.pause_until
                else None,
                "reason": current.safety.pause_reason,
            },
            "config_hash": current.hash,
        }

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
        if not rate_limiter.allow(scope):
            raise HTTPException(status_code=429, detail="Automation rate limit exceeded")
        return scope

    @app.exception_handler(ConfigLoadError)
    async def config_error(request: Request, exc: ConfigLoadError) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            {**template_identity(request), "message": str(exc)},
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
                **template_identity(request),
                "message": str(exc.detail),
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, failed: bool = False) -> HTMLResponse:
        cfg = config()
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "authenticated": False,
                "failed": failed,
                "csrf_token": csrf_token(request),
                "brand": {
                    "school_name": cfg.settings.school_name,
                    "subtitle": cfg.settings.console_subtitle,
                    "has_logo": bool(cfg.logo_path and cfg.logo_path.is_file()),
                },
            },
        )

    @app.get("/branding/logo.png")
    def branding_logo() -> FileResponse:
        cfg = config()
        if cfg.logo_path is None or not cfg.logo_path.is_file():
            raise HTTPException(404, "No school logo is configured")
        return FileResponse(
            cfg.logo_path, media_type="image/png", content_disposition_type="inline"
        )

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(default="admin"),
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
            raise HTTPException(
                status_code=429, detail="Too many sign-in attempts. Try again later."
            )
        try:
            authenticated_user = auth_store.verify(username, submitted_password)
        except AuthError as exc:
            LOGGER.exception("account_database_unreadable")
            raise HTTPException(503, str(exc)) from exc
        if authenticated_user is None:
            LOGGER.warning(
                "ui_action",
                extra={"action": "login", "result": "denied", "client": client_address},
            )
            return RedirectResponse("/login?failed=true", status_code=303)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        request.session["username"] = authenticated_user.username
        request.session["role"] = authenticated_user.role
        request.session["auth_revision"] = auth_store.revision
        LOGGER.info(
            "ui_action",
            extra={"action": "login", "result": "success", "client": client_address},
        )
        return RedirectResponse("/", status_code=303)

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request, message: str | None = None) -> HTMLResponse:
        require_auth(request)
        return render(request, "account.html", users=auth_store.users(), message=message)

    @app.post("/account/password")
    def account_password(
        request: Request,
        current_password: str = Form(),
        new_password: str = Form(),
        confirm_password: str = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        username = str(request.session.get("username", "admin"))
        role = str(request.session.get("role", "admin"))
        if auth_store.verify(username, current_password) is None:
            raise HTTPException(400, "Current password is incorrect")
        if new_password != confirm_password:
            raise HTTPException(400, "New password confirmation does not match")
        try:
            auth_store.set_password(username, role, new_password)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.post("/account/operator")
    def account_operator(
        request: Request,
        current_password: str = Form(),
        operator_password: str = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        if auth_store.verify("admin", current_password) is None:
            raise HTTPException(400, "Administrator password is incorrect")
        try:
            if not auth_store.path.is_file():
                auth_store.set_password("admin", "admin", current_password)
            auth_store.set_password("operator", "operator", operator_password)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.post("/account/operator/delete")
    def account_operator_delete(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        auth_store.delete_user("operator")
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

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
        snapshot = operations_snapshot(cfg)
        now = datetime.fromisoformat(snapshot["server_time"])
        plan = resolve_day(now.date(), cfg)
        return render(
            request,
            "today.html",
            config=cfg,
            snapshot=snapshot,
            plan=plan,
            now=now,
            kill_switch=cfg.safety.kill_switch_enabled,
            message=request.query_params.get("message"),
        )

    @app.get("/operations/snapshot")
    def operations_snapshot_route(request: Request) -> JSONResponse:
        require_auth(request)
        return JSONResponse(operations_snapshot())

    @app.post("/operations/stop")
    def operations_stop(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        if cancel_callback is None:
            raise HTTPException(503, "Active-page control is unavailable")
        stopped = cancel_callback("front-office operator stop")
        LOGGER.warning(
            "ui_action",
            extra={"action": "active_page_stop", "result": "requested" if stopped else "idle"},
        )
        record_audit(
            "active_page_stop", "active page", "requested" if stopped else "no page active"
        )
        message = (
            "Stop requested for the active page." if stopped else "No page is currently active."
        )
        return RedirectResponse(f"/?{urlencode({'message': message})}", status_code=303)

    @app.post("/operations/pause")
    def operations_pause(
        request: Request,
        duration: str = Form(),
        reason: str = Form(default=""),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        normalized_reason = reason.strip()
        if duration == "resume":
            pause_until = None
            normalized_reason = ""
        elif duration == "today":
            pause_until = datetime.combine(
                now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo
            )
        elif duration in {"15", "30", "60"}:
            pause_until = now + timedelta(minutes=int(duration))
        else:
            raise HTTPException(400, "Choose a valid pause duration")
        if pause_until is not None and not normalized_reason:
            raise HTTPException(400, "Enter a reason for pausing bells")

        def mutate(document: Any, _current: BellConfig) -> None:
            safety = document.setdefault("safety", {})
            safety["pause_until"] = pause_until.isoformat() if pause_until else None
            safety["pause_reason"] = normalized_reason or None

        write_config_document("settings.yaml", "settings", config_hash, mutate)
        if pause_until is not None and cancel_callback:
            cancel_callback(f"bells paused: {normalized_reason}")
        LOGGER.warning(
            "ui_action",
            extra={
                "action": "bells_resume" if pause_until is None else "bells_pause",
                "result": "success",
                "until": pause_until.isoformat() if pause_until else None,
            },
        )
        record_audit(
            "bells_resume" if pause_until is None else "bells_pause",
            "scheduler",
            normalized_reason or "temporary pause cleared",
        )
        message = "Bell transmission resumed." if pause_until is None else "Bell transmission paused."
        return RedirectResponse(f"/?{urlencode({'message': message})}", status_code=303)

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page(
        request: Request,
        selected: date | None = None,
        month: str | None = None,
    ) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        today_local = datetime.now(ZoneInfo(cfg.settings.timezone)).date()
        chosen = selected or today_local
        if month:
            try:
                month_start = date.fromisoformat(f"{month}-01")
            except ValueError as exc:
                raise HTTPException(400, "Month must use YYYY-MM format") from exc
        else:
            month_start = chosen.replace(day=1)
        grid_start = month_start - timedelta(days=(month_start.weekday() + 1) % 7)
        month_days = [resolve_day(grid_start + timedelta(days=offset), cfg) for offset in range(42)]
        previous_month = (month_start - timedelta(days=1)).replace(day=1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
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
            today=today_local,
            month_days=month_days,
            month_start=month_start,
            month_label=month_start.strftime("%B %Y"),
            previous_month=previous_month.strftime("%Y-%m"),
            next_month=next_month.strftime("%Y-%m"),
            current_month=month_start.month,
            selected_plan=resolve_day(chosen, cfg),
        )

    def write_config_document(
        filename: str,
        backup_prefix: str,
        expected_hash: str,
        mutation: Callable[[Any, BellConfig], None],
    ) -> BellConfig:
        """Atomically mutate, validate, reload, and if needed roll back one YAML file."""
        path = directory / filename
        with config_lock:
            cfg = config()
            if not expected_hash or not secrets.compare_digest(expected_hash, cfg.hash):
                raise HTTPException(
                    status_code=409,
                    detail="Configuration changed after this page loaded. Reload and try again.",
                )
            backup_dir = cfg.state_path / "config-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / (
                f"{backup_prefix}-"
                f"{datetime.now(ZoneInfo(cfg.settings.timezone)):%Y%m%dT%H%M%S%f}.yaml"
            )
            yaml = YAML()
            yaml.preserve_quotes = True
            with path.open("r", encoding="utf-8") as handle:
                document = yaml.load(handle)
            mutation(document, cfg)
            shutil.copy2(path, backup)
            temporary_name: str | None = None
            replaced = False
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f"{backup_prefix}-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary_name = handle.name
                    yaml.dump(document, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temporary_name).replace(path)
                replaced = True
                refreshed = load_config(directory)
                rate_limiter.limit = refreshed.settings.api_rate_limit_per_minute
                if reload_callback:
                    reload_callback()
            except Exception as exc:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
                if replaced:
                    shutil.copy2(backup, path)
                    try:
                        load_config(directory)
                        rate_limiter.limit = cfg.settings.api_rate_limit_per_minute
                        if reload_callback:
                            reload_callback()
                    except Exception:
                        LOGGER.exception(
                            "configuration_rollback_reload_failed",
                            extra={"config_file": filename},
                        )
                if isinstance(exc, (HTTPException, ConfigLoadError)):
                    raise
                LOGGER.exception("configuration_save_failed", extra={"config_file": filename})
                raise HTTPException(
                    status_code=500,
                    detail="The change could not be activated. The previous configuration was restored.",
                ) from exc
            backups = sorted(
                backup_dir.glob(f"{backup_prefix}-*.yaml"), key=lambda item: item.stat().st_mtime
            )
            for old_backup in backups[:-30]:
                try:
                    old_backup.unlink()
                except OSError:
                    LOGGER.warning(
                        "configuration_backup_prune_failed",
                        extra={"path": str(old_backup)},
                        exc_info=True,
                    )
            record_audit("configuration_change", backup_prefix, f"activated {refreshed.hash}")
            return refreshed

    def write_calendar(
        day: date,
        schedule_name: str | None,
        reason: str | None,
        expected_hash: str,
    ) -> None:
        def mutate(document: Any, _cfg: BellConfig) -> None:
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

        write_config_document("calendar.yaml", "calendar", expected_hash, mutate)

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
            extra={
                "action": "calendar_change",
                "target": selected.isoformat(),
                "result": "success",
            },
        )
        return RedirectResponse(f"/calendar?selected={selected.isoformat()}", status_code=303)

    @app.post("/calendar/bulk")
    def calendar_bulk_save(
        request: Request,
        start: date = Form(),  # noqa: B008
        end: date = Form(),  # noqa: B008
        bulk_action: str = Form(),
        schedule_name: str = Form(default=""),
        no_bell_reason: str = Form(default=""),
        config_hash: str = Form(default=""),
        review_token: str = Form(default=""),
        csrf: str = Form(),
    ) -> Response:
        require_auth(request)
        verify_csrf(request, csrf)
        with config_lock:
            cfg = config()
            if cfg.hash != config_hash:
                raise HTTPException(409, "Configuration changed; reload and review the range again")
            reason = no_bell_reason.strip()
            try:
                proposed = range_config(cfg, start, end, bulk_action, schedule_name, reason)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            payload = {"start": start.isoformat(), "end": end.isoformat(), "bulk_action": bulk_action,
                       "schedule_name": schedule_name, "no_bell_reason": reason, "config_hash": config_hash}
            if not review_token:
                return render(request, "calendar_review.html", fields=payload,
                              rows=list(zip(review(cfg, start, end), review(proposed, start, end), strict=True)),
                              review_token=calendar_signer.dumps(payload))
            try:
                reviewed = calendar_signer.loads(review_token, max_age=600)
            except BadSignature as exc:
                raise HTTPException(400, "Calendar review expired or is invalid; preview again") from exc
            if reviewed != payload:
                raise HTTPException(409, "Calendar changes differ from the reviewed range; preview again")

        def mutate(document: Any, _current: BellConfig) -> None:
            calendar = document["calendar"]
            overrides = calendar.setdefault("overrides", {})
            no_bells = calendar.setdefault("no_bell_dates", {})
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                for key in (day, day.isoformat()):
                    overrides.pop(key, None)
                    no_bells.pop(key, None)
                if bulk_action == "schedule":
                    overrides[day] = schedule_name
                elif bulk_action == "no_bells":
                    no_bells[day] = reason

        write_config_document("calendar.yaml", "calendar", config_hash, mutate)
        record_audit("calendar_bulk_change", f"{start} through {end}", bulk_action)
        query = urlencode({"selected": start.isoformat(), "month": start.strftime("%Y-%m")})
        return RedirectResponse(f"/calendar?{query}", status_code=303)

    @app.get("/calendar/readiness", response_class=HTMLResponse)
    def calendar_readiness(request: Request, start: date | None = None,
                           end: date | None = None) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        start = start or datetime.now(ZoneInfo(cfg.settings.timezone)).date()
        try:
            end = end or start + timedelta(days=365)
            rows = review(cfg, start, end)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return render(request, "readiness.html", start=start, end=end, rows=rows,
                      issues=[row for row in rows if row["issue"]])

    @app.get("/calendar/export.csv")
    def calendar_export(request: Request, year: int | None = None) -> Response:
        require_auth(request)
        cfg = config()
        selected_year = year or datetime.now(ZoneInfo(cfg.settings.timezone)).year
        if not 2000 <= selected_year <= 2100:
            raise HTTPException(400, "Year must be between 2000 and 2100")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=["date", "weekday", "schedule", "reason", "event_count", "events"],
        )
        writer.writeheader()
        for month_number in range(1, 13):
            for day_number in range(
                1, calendar_module.monthrange(selected_year, month_number)[1] + 1
            ):
                day = date(selected_year, month_number, day_number)
                plan = resolve_day(day, cfg)
                writer.writerow(
                    {
                        "date": day.isoformat(),
                        "weekday": day.strftime("%A"),
                        "schedule": plan.schedule_name or "",
                        "reason": plan.reason or "",
                        "event_count": len(plan.events),
                        "events": "; ".join(
                            f"{item.scheduled_at:%H:%M} {item.event.label} [{item.event.zone}]"
                            for item in plan.events
                        ),
                    }
                )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=school-bell-calendar-{selected_year}.csv"
            },
        )

    def schedule_references(cfg: BellConfig, schedule_name: str) -> list[str]:
        references = [
            f"{weekday.title()} default"
            for weekday, name in cfg.calendar.weekday_defaults.items()
            if name == schedule_name
        ]
        references.extend(
            f"{day.isoformat()} override"
            for day, name in cfg.calendar.overrides.items()
            if name == schedule_name
        )
        references.extend(
            f"{item.start.isoformat()} to {item.end.isoformat()} date range"
            for item in cfg.calendar.date_ranges
            if item.schedule == schedule_name
        )
        return references

    def safe_sound_names(cfg: BellConfig) -> list[str]:
        return sorted(
            path.name
            for path in cfg.sounds_path.iterdir()
            if path.is_file() and os.access(path, os.R_OK)
        )

    def sound_references(cfg: BellConfig, sound_name: str) -> list[str]:
        references: list[str] = []
        for schedule in cfg.schedules:
            for event in schedule.events:
                if event.sound == sound_name:
                    references.append(f"{schedule.name}: {event.label}")
                if event.pre_tone == sound_name:
                    references.append(f"{schedule.name}: {event.label} pre-tone")
        for item in cfg.standing_items:
            if item.sound == sound_name:
                references.append(f"Standing item: {item.label}")
            if item.pre_tone == sound_name:
                references.append(f"Standing item: {item.label} pre-tone")
        return references

    def zone_references(cfg: BellConfig, zone_name: str) -> list[str]:
        references = [
            f"{schedule.name}: {event.label}"
            for schedule in cfg.schedules
            for event in schedule.events
            if event.zone == zone_name
        ]
        references.extend(
            f"Standing item: {item.label}" for item in cfg.standing_items if item.zone == zone_name
        )
        return references

    def destination_references(cfg: BellConfig, destination_name: str) -> list[str]:
        return [zone.name for zone in cfg.zones if destination_name in zone.destinations]

    def event_document(event: BellEvent) -> dict[str, Any]:
        document: dict[str, Any] = {
            "time": DoubleQuotedScalarString(event.time.strftime("%H:%M")),
            "sound": event.sound,
            "zone": event.zone,
            "label": event.label,
            "repeat_count": event.repeat_count,
            "repeat_interval_seconds": event.repeat_interval_seconds,
            "priority": event.priority,
            "busy_policy": event.busy_policy,
        }
        if event.pre_tone:
            document["pre_tone"] = event.pre_tone
        return document

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(
        request: Request,
        selected: str | None = None,
        new: bool = False,
        copy: str | None = None,
        message: str | None = None,
    ) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        schedule: BellSchedule | None
        original_name = ""
        is_new = new or copy is not None
        if copy is not None:
            source = cfg.schedule_map.get(copy)
            if source is None:
                raise HTTPException(404, "The schedule to duplicate does not exist")
            schedule = source.model_copy(deep=True, update={"name": f"{source.name} Copy"})
        elif new:
            schedule = BellSchedule(name="", events=[])
        elif selected is not None:
            schedule = cfg.schedule_map.get(selected)
            if schedule is None:
                raise HTTPException(404, "That schedule does not exist")
            original_name = schedule.name
        else:
            schedule = cfg.schedules[0] if cfg.schedules else BellSchedule(name="", events=[])
            is_new = not cfg.schedules
            if cfg.schedules:
                original_name = schedule.name
        enabled_standing_items = sum(item.enabled for item in cfg.standing_items)
        return render(
            request,
            "schedules.html",
            config=cfg,
            schedule=schedule,
            original_name=original_name,
            is_new=is_new,
            sounds=safe_sound_names(cfg),
            zones=cfg.zones,
            config_hash=cfg.hash,
            references=schedule_references(cfg, original_name) if original_name else [],
            enabled_standing_items=enabled_standing_items,
            event_limit=max(0, cfg.safety.max_events_per_day - enabled_standing_items),
            message=message,
        )

    @app.post("/schedules/save")
    async def schedules_save(request: Request) -> RedirectResponse:
        require_auth(request)
        try:
            form = await request.form(max_fields=1000, max_part_size=64 * 1024)
        except Exception as exc:
            raise HTTPException(400, "The schedule form is too large or malformed") from exc

        def scalar(name: str) -> str:
            value = form.get(name, "")
            if not isinstance(value, str):
                raise HTTPException(400, f"Invalid {name.replace('_', ' ')}")
            return value

        def values(name: str) -> list[str]:
            result = form.getlist(name)
            if any(not isinstance(value, str) for value in result):
                raise HTTPException(400, f"Invalid {name.replace('_', ' ')}")
            return list(result)  # type: ignore[arg-type]

        verify_csrf(request, scalar("csrf"))
        expected_hash = scalar("config_hash")
        original_name = scalar("original_name").strip()
        schedule_name = scalar("schedule_name").strip()
        if not schedule_name or len(schedule_name) > 100:
            raise HTTPException(400, "Schedule name must be between 1 and 100 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in schedule_name):
            raise HTTPException(400, "Schedule name must not contain control characters")
        if original_name and schedule_name != original_name:
            raise HTTPException(
                400,
                "Existing schedule names cannot be changed in place. Use Duplicate, then delete the old schedule after updating its calendar assignments.",
            )

        field_names = (
            "event_time",
            "event_label",
            "event_sound",
            "event_zone",
            "event_pre_tone",
            "event_repeat_count",
            "event_repeat_interval",
            "event_priority",
            "event_busy_policy",
        )
        columns = {name: values(name) for name in field_names}
        lengths = {len(column) for column in columns.values()}
        if len(lengths) != 1:
            raise HTTPException(400, "Schedule rows are incomplete. Reload the page and try again.")
        event_count = lengths.pop()
        if event_count < 1:
            raise HTTPException(400, "Add at least one bell event before saving")

        cfg = config()
        enabled_standing_items = sum(item.enabled for item in cfg.standing_items)
        event_limit = max(0, cfg.safety.max_events_per_day - enabled_standing_items)
        if event_count > event_limit:
            raise HTTPException(
                400,
                f"This schedule has {event_count} events; the safe limit is {event_limit} because "
                f"{enabled_standing_items} standing items are enabled.",
            )
        known_sounds = set(safe_sound_names(cfg))
        events: list[BellEvent] = []
        for index in range(event_count):
            label = columns["event_label"][index].strip()
            if not label or len(label) > 100:
                raise HTTPException(400, f"Row {index + 1}: label must be 1 to 100 characters")
            if any(ord(character) < 32 or ord(character) == 127 for character in label):
                raise HTTPException(400, f"Row {index + 1}: label contains control characters")
            sound = columns["event_sound"][index]
            pre_tone = columns["event_pre_tone"][index] or None
            zone = columns["event_zone"][index]
            if sound not in known_sounds:
                raise HTTPException(400, f"Row {index + 1}: choose a valid sound")
            if pre_tone is not None and pre_tone not in known_sounds:
                raise HTTPException(400, f"Row {index + 1}: choose a valid pre-tone")
            if zone not in cfg.zone_map:
                raise HTTPException(400, f"Row {index + 1}: choose a valid zone")
            try:
                event = BellEvent.model_validate(
                    {
                        "time": columns["event_time"][index],
                        "label": label,
                        "sound": sound,
                        "zone": zone,
                        "pre_tone": pre_tone,
                        "repeat_count": columns["event_repeat_count"][index],
                        "repeat_interval_seconds": columns["event_repeat_interval"][index],
                        "priority": columns["event_priority"][index],
                        "busy_policy": columns["event_busy_policy"][index],
                    }
                )
            except ValidationError as exc:
                message = exc.errors(include_url=False)[0]["msg"]
                raise HTTPException(400, f"Row {index + 1}: {message}") from exc
            if event.time.second or event.time.microsecond:
                raise HTTPException(400, f"Row {index + 1}: time must use whole minutes")
            if event.repeat_count > cfg.safety.max_repeats:
                raise HTTPException(
                    400,
                    f"Row {index + 1}: repeat count exceeds the safety maximum of {cfg.safety.max_repeats}",
                )
            events.append(event)
        events.sort(key=lambda item: item.time)
        try:
            validated = BellSchedule(name=schedule_name, events=events)
        except ValidationError as exc:
            message = exc.errors(include_url=False)[0]["msg"]
            raise HTTPException(400, f"Schedule validation failed: {message}") from exc

        # Quoted times avoid YAML 1.1 interpreting HH:MM as a base-60 integer.
        event_documents = [event_document(event) for event in validated.events]
        schedule_document = {"name": schedule_name, "events": event_documents}

        def mutate(document: Any, current: BellConfig) -> None:
            schedules = document.setdefault("schedules", [])
            names = [str(item.get("name", "")) for item in schedules]
            if original_name:
                if original_name not in current.schedule_map or original_name not in names:
                    raise HTTPException(404, "The schedule no longer exists")
                schedules[names.index(original_name)] = schedule_document
            else:
                if schedule_name in current.schedule_map or schedule_name in names:
                    raise HTTPException(409, "A schedule with that name already exists")
                schedules.append(schedule_document)

        write_config_document("schedules.yaml", "schedules", expected_hash, mutate)
        LOGGER.info(
            "ui_action",
            extra={
                "action": "schedule_save",
                "target": "schedule",
                "events": event_count,
                "result": "success",
            },
        )
        query = urlencode({"selected": schedule_name, "message": "Schedule saved and activated."})
        return RedirectResponse(f"/schedules?{query}", status_code=303)

    @app.post("/schedules/delete")
    def schedules_delete(
        request: Request,
        schedule_name: str = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        schedule_name = schedule_name.strip()

        def mutate(document: Any, current: BellConfig) -> None:
            if schedule_name not in current.schedule_map:
                raise HTTPException(404, "That schedule no longer exists")
            references = schedule_references(current, schedule_name)
            if references:
                raise HTTPException(
                    409,
                    "This schedule is still assigned to: "
                    + ", ".join(references)
                    + ". Update the Calendar first.",
                )
            schedules = document.get("schedules", [])
            document["schedules"] = [
                item for item in schedules if str(item.get("name", "")) != schedule_name
            ]

        write_config_document("schedules.yaml", "schedules", config_hash, mutate)
        LOGGER.info(
            "ui_action",
            extra={"action": "schedule_delete", "target": "schedule", "result": "success"},
        )
        query = urlencode({"message": f"{schedule_name} was deleted."})
        return RedirectResponse(f"/schedules?{query}", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(
        request: Request,
        new_standing: bool = False,
        new_zone: bool = False,
        new_destination: bool = False,
        message: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        return render(
            request,
            "setup.html",
            config=cfg,
            config_hash=cfg.hash,
            sounds=safe_sound_names(cfg),
            sound_references={name: sound_references(cfg, name) for name in safe_sound_names(cfg)},
            zone_references={zone.name: zone_references(cfg, zone.name) for zone in cfg.zones},
            destination_references={
                item.name: destination_references(cfg, item.name) for item in cfg.destinations
            },
            new_standing=new_standing,
            new_zone=new_zone,
            new_destination=new_destination,
            available_channels=sorted({23, 24, 25} - {zone.channel for zone in cfg.zones}),
            message=message,
            error=error,
        )

    @app.post("/setup/branding/save")
    async def branding_save(
        request: Request,
        school_name: str = Form(),
        console_subtitle: str = Form(),
        logo_file: UploadFile | None = File(default=None),  # noqa: B008
        remove_logo: bool = Form(default=False),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        current = config()
        has_upload = bool(logo_file and logo_file.filename)
        if remove_logo and has_upload:
            raise HTTPException(400, "Choose either a new logo or remove the current logo")
        try:
            validated = Settings.model_validate(
                {
                    **current.settings.model_dump(),
                    "school_name": school_name,
                    "console_subtitle": console_subtitle,
                    "logo_filename": (
                        "logo.png"
                        if has_upload
                        else None
                        if remove_logo
                        else current.settings.logo_filename
                    ),
                }
            )
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)[0]["msg"]) from exc

        staged: Path | None = None
        target = current.state_path / "branding" / "logo.png"
        previous: Path | None = None
        if has_upload:
            assert logo_file is not None
            staging = current.state_path / "branding-staging"
            staging.mkdir(parents=True, exist_ok=True)
            source: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "wb", dir=staging, prefix="logo-upload-", suffix=".bin", delete=False
                ) as handle:
                    source = Path(handle.name)
                    total = 0
                    while chunk := await logo_file.read(256 * 1024):
                        total += len(chunk)
                        if total > 2 * 1024 * 1024:
                            raise HTTPException(413, "Logo upload exceeds the 2 MiB limit")
                        handle.write(chunk)
                if not total:
                    raise HTTPException(400, "Uploaded logo is empty")
                staged = staging / f"normalized-{secrets.token_hex(8)}.png"
                normalize_logo(source, staged)
            except BrandingError as exc:
                raise HTTPException(400, str(exc)) from exc
            finally:
                if source:
                    source.unlink(missing_ok=True)

        try:
            if staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    previous = target.parent / f"previous-{secrets.token_hex(8)}.png"
                    shutil.copy2(target, previous)
                staged.replace(target)
                staged = None

            def mutate(document: Any, _cfg: BellConfig) -> None:
                settings = document.setdefault("settings", {})
                settings["school_name"] = validated.school_name
                settings["console_subtitle"] = validated.console_subtitle
                settings["logo_filename"] = validated.logo_filename

            write_config_document("settings.yaml", "settings", config_hash, mutate)
        except Exception:
            if previous:
                shutil.copy2(previous, target)
            elif has_upload:
                target.unlink(missing_ok=True)
            raise
        finally:
            if staged:
                staged.unlink(missing_ok=True)
            if previous:
                previous.unlink(missing_ok=True)
        if remove_logo:
            target.unlink(missing_ok=True)
        LOGGER.info("ui_action", extra={"action": "branding_change", "result": "success"})
        return RedirectResponse("/setup?message=School+branding+saved.#branding", status_code=303)

    def calibration_workspace(cfg: BellConfig) -> Path:
        return cfg.state_path / "poly-calibration"

    def capture_manifest(workspace: Path) -> dict[str, Any] | None:
        path = workspace / "manifest.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(500, "Stored calibration manifest is unreadable") from exc
        if not isinstance(payload, dict):
            raise HTTPException(500, "Stored calibration manifest is invalid")
        return payload

    def captured_channels(workspace: Path) -> dict[int, list[bytes]]:
        captures: dict[int, list[bytes]] = {}
        for path in sorted(workspace.glob("channel-*.bin")):
            match = re.fullmatch(r"channel-(\d{1,2})\.bin", path.name)
            if match:
                try:
                    captures[int(match.group(1))] = load_capture(path)
                except (OSError, ValueError) as exc:
                    raise HTTPException(500, f"Stored capture {path.name} is unreadable") from exc
        return captures

    @app.get("/setup/poly-calibration", response_class=HTMLResponse)
    def poly_calibration_page(
        request: Request,
        message: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        workspace = calibration_workspace(cfg)
        captures = captured_channels(workspace) if workspace.is_dir() else {}
        manifest = capture_manifest(workspace) if workspace.is_dir() else None
        candidate = None
        derivation_error = None
        if len(captures) >= 3:
            try:
                contract = manifest.get("contract", {}) if manifest else {}
                expected_payload_type = codec_spec(contract.get("codec", "pcmu")).payload_type
                candidate = derive_poly_calibration(captures, expected_payload_type)
            except (AudioProcessingError, CalibrationError) as exc:
                derivation_error = str(exc)
        destinations = [
            item
            for item in cfg.destinations
            if item.protocol == "multicast"
            and item.enabled
            and (item.wire_format or cfg.settings.wire_format) == "poly_group_page"
        ]
        return render(
            request,
            "poly_calibration.html",
            config=cfg,
            config_hash=cfg.hash,
            destinations=destinations,
            captures=captures,
            manifest=manifest,
            candidate=candidate,
            derivation_error=derivation_error,
            message=message,
            error=error,
        )

    @app.post("/setup/poly-calibration/capture")
    def poly_calibration_capture(
        request: Request,
        destination_name: str = Form(),
        known_channel: int = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        capture_identity = request.client.host if request.client else "authenticated-admin"
        if not capture_limiter.allow(capture_identity):
            raise HTTPException(429, "Calibration capture rate limit exceeded; wait five minutes")
        cfg = config()
        destination = cfg.destination_map.get(destination_name)
        if destination is None or destination.protocol != "multicast" or not destination.enabled:
            raise HTTPException(400, "Choose an enabled multicast destination")
        wire_format = destination.wire_format or cfg.settings.wire_format
        if wire_format != "poly_group_page":
            raise HTTPException(400, "Choose a multicast destination using Poly Group Page")
        if not 1 <= known_channel <= 25:
            raise HTTPException(400, "Known Poly channel must be between 1 and 25")
        if cfg.settings.interface_ip == "0.0.0.0":
            raise HTTPException(400, "Configure the Pi's wired phone-VLAN interface IP first")
        if not cfg.safety.kill_switch_enabled:
            raise HTTPException(
                409,
                "Enable the transmission kill switch before calibration capture",
            )
        codec = destination.codecs[0]
        payload_type = codec_spec(codec).payload_type
        contract = {
            "destination": destination.name,
            "group": destination.group,
            "port": destination.port,
            "interface_ip": cfg.settings.interface_ip,
            "codec": codec,
        }
        workspace = calibration_workspace(cfg)
        with calibration_lock:
            workspace.mkdir(parents=True, exist_ok=True)
            existing = capture_manifest(workspace)
            if existing is not None and existing.get("contract") != contract:
                raise HTTPException(
                    409,
                    "Capture contract changed. Clear staged captures before starting again.",
                )
            try:
                raw_packets, _arrivals = capture_rtp(
                    destination.group or "",
                    destination.port,
                    cfg.settings.interface_ip,
                    32,
                    transform=partial(
                        valid_header_or_none,
                        expected_payload_type=payload_type,
                    ),
                )
                headers = sanitize_capture(raw_packets, payload_type)
            except TimeoutError:
                query = urlencode(
                    {
                        "error": (
                            f"No compatible Poly {codec.upper()} Page packets arrived before the "
                            "10-second capture timeout."
                        )
                    }
                )
                return RedirectResponse(f"/setup/poly-calibration?{query}", status_code=303)
            except PermissionError:
                query = urlencode(
                    {
                        "error": (
                            f"The service cannot listen on UDP port {destination.port}. "
                            "Install release v0.6.1 or newer, restart bell-system, and try again."
                        )
                    }
                )
                return RedirectResponse(f"/setup/poly-calibration?{query}", status_code=303)
            except (OSError, CalibrationError) as exc:
                query = urlencode({"error": str(exc)})
                return RedirectResponse(f"/setup/poly-calibration?{query}", status_code=303)
            capture_path = workspace / f"channel-{known_channel}.bin"
            temporary_capture = workspace / f".{capture_path.name}.tmp"
            save_capture(temporary_capture, headers)
            temporary_capture.replace(capture_path)
            manifest_payload = {
                "schema": 2,
                "contract": contract,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            temporary_manifest = workspace / ".manifest.tmp"
            temporary_manifest.write_text(
                json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary_manifest.replace(workspace / "manifest.json")
        LOGGER.info(
            "ui_action",
            extra={
                "action": "poly_capture",
                "target": "multicast_destination",
                "packets": len(headers),
                "result": "captured_headers_only",
            },
        )
        query = urlencode({"message": f"Captured channel {known_channel}."})
        return RedirectResponse(f"/setup/poly-calibration?{query}", status_code=303)

    @app.post("/setup/poly-calibration/activate")
    def poly_calibration_activate(
        request: Request,
        confirm_evidence: bool = Form(default=False),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        if not confirm_evidence:
            raise HTTPException(400, "Confirm that each capture used the displayed known channel")
        cfg = config()
        if not config_hash or not secrets.compare_digest(config_hash, cfg.hash):
            raise HTTPException(
                409,
                "Configuration changed after this page loaded. Reload and try again.",
            )
        workspace = calibration_workspace(cfg)
        with calibration_lock:
            manifest = capture_manifest(workspace)
            if manifest is None or not isinstance(manifest.get("contract"), dict):
                raise HTTPException(400, "Capture contract evidence is missing")
            contract = manifest["contract"]
            destination = cfg.destination_map.get(str(contract.get("destination", "")))
            expected_contract = (
                {
                    "destination": destination.name,
                    "group": destination.group,
                    "port": destination.port,
                    "interface_ip": cfg.settings.interface_ip,
                    "codec": destination.codecs[0],
                }
                if destination is not None and destination.protocol == "multicast"
                else None
            )
            if contract != expected_contract:
                raise HTTPException(
                    409,
                    "The multicast contract changed after capture. Clear and recapture.",
                )
            captures = captured_channels(workspace)
            try:
                expected_payload_type = codec_spec(str(contract.get("codec", ""))).payload_type
                derived = derive_poly_calibration(captures, expected_payload_type)
            except (AudioProcessingError, CalibrationError) as exc:
                raise HTTPException(400, str(exc)) from exc
            captured_at = datetime.now(UTC)
            evidence_id = f"{captured_at:%Y%m%dT%H%M%S%fZ}-{derived.evidence[0].header_sha256[:12]}"
            calibration = PolyCalibration(
                channel_bias=derived.spec.channel_bias,
                control_header_bytes=derived.spec.control_header_bytes,
                audio_header_bytes=derived.spec.audio_header_bytes,
                codec=str(contract["codec"]),
                captured_channels=[item.channel for item in derived.evidence],
                capture_sha256=[item.header_sha256 for item in derived.evidence],
                captured_at=captured_at,
                evidence_id=evidence_id,
            )

            evidence_directory = workspace / "verified" / evidence_id
            evidence_directory.mkdir(parents=True, exist_ok=False)
            for item in derived.evidence:
                shutil.copy2(
                    workspace / f"channel-{item.channel}.bin",
                    evidence_directory / f"channel-{item.channel}.bin",
                )
            shutil.copy2(workspace / "manifest.json", evidence_directory / "manifest.json")
            (evidence_directory / "calibration.json").write_text(
                calibration.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )

        def mutate(document: Any, _current: BellConfig) -> None:
            document.setdefault("settings", {})["poly_group_page_calibration"] = (
                calibration.model_dump(mode="json")
            )

        write_config_document("settings.yaml", "poly-calibration", config_hash, mutate)
        LOGGER.warning(
            "ui_action",
            extra={
                "action": "poly_calibration_activate",
                "channels": calibration.captured_channels,
                "result": "success",
            },
        )
        return RedirectResponse(
            "/setup?message=Poly+Group+Page+calibration+activated.#destinations",
            status_code=303,
        )

    @app.post("/setup/poly-calibration/clear")
    def poly_calibration_clear(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        workspace = calibration_workspace(config())
        with calibration_lock:
            if workspace.is_dir():
                for path in workspace.iterdir():
                    if path.is_file() and (
                        re.fullmatch(r"channel-\d{1,2}\.bin", path.name)
                        or path.name == "manifest.json"
                    ):
                        path.unlink()
        return RedirectResponse(
            "/setup/poly-calibration?message=Staged+captures+cleared.",
            status_code=303,
        )

    @app.post("/setup/standing/save")
    def standing_save(
        request: Request,
        standing_index: int = Form(default=-1),
        event_time: str = Form(),
        event_label: str = Form(),
        event_sound: str = Form(),
        event_zone: str = Form(),
        event_pre_tone: str = Form(default=""),
        event_repeat_count: int = Form(default=1),
        event_repeat_interval: float = Form(default=0.0),
        event_priority: int = Form(default=50),
        event_busy_policy: str = Form(default="skip"),
        enabled: bool = Form(default=False),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        label = event_label.strip()
        if (
            not label
            or len(label) > 100
            or any(ord(character) < 32 or ord(character) == 127 for character in label)
        ):
            raise HTTPException(400, "Standing-item label must be 1 to 100 safe characters")
        if event_sound not in safe_sound_names(cfg) or (
            event_pre_tone and event_pre_tone not in safe_sound_names(cfg)
        ):
            raise HTTPException(400, "Choose sounds from the configured library")
        if event_zone not in cfg.zone_map:
            raise HTTPException(400, "Choose a configured zone")
        try:
            item = StandingItem.model_validate(
                {
                    "time": event_time,
                    "label": label,
                    "sound": event_sound,
                    "zone": event_zone,
                    "pre_tone": event_pre_tone or None,
                    "repeat_count": event_repeat_count,
                    "repeat_interval_seconds": event_repeat_interval,
                    "priority": event_priority,
                    "busy_policy": event_busy_policy,
                    "enabled": enabled,
                }
            )
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)[0]["msg"]) from exc
        if item.repeat_count > cfg.safety.max_repeats:
            raise HTTPException(400, "Repeat count exceeds the configured safety maximum")
        document_item = event_document(item)
        document_item["enabled"] = item.enabled

        def mutate(document: Any, current: BellConfig) -> None:
            items = document.setdefault("standing_items", [])
            if standing_index == -1:
                items.append(document_item)
            elif not 0 <= standing_index < len(current.standing_items) or standing_index >= len(
                items
            ):
                raise HTTPException(409, "That standing item changed or no longer exists")
            else:
                items[standing_index] = document_item

        write_config_document("schedules.yaml", "schedules", config_hash, mutate)
        action = "created" if standing_index == -1 else "updated"
        LOGGER.info(
            "ui_action",
            extra={
                "action": f"standing_item_{action}",
                "target": "standing_item",
                "result": "success",
            },
        )
        return RedirectResponse(
            f"/setup?{urlencode({'message': f'Standing item {action}.'})}#standing-items",
            status_code=303,
        )

    @app.post("/setup/standing/delete")
    def standing_delete(
        request: Request,
        standing_index: int = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)

        def mutate(document: Any, current: BellConfig) -> None:
            items = document.get("standing_items", [])
            if not 0 <= standing_index < len(current.standing_items) or standing_index >= len(
                items
            ):
                raise HTTPException(409, "That standing item changed or no longer exists")
            del items[standing_index]

        write_config_document("schedules.yaml", "schedules", config_hash, mutate)
        return RedirectResponse(
            "/setup?message=Standing+item+deleted.#standing-items", status_code=303
        )

    @app.post("/setup/zones/save")
    def zone_save(
        request: Request,
        original_name: str = Form(default=""),
        zone_name: str = Form(),
        channel: int = Form(),
        description: str = Form(),
        destinations: list[str] = Form(),  # noqa: B008
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        zone_name = zone_name.strip()
        description = description.strip()
        if (
            not zone_name
            or len(zone_name) > 100
            or any(ord(character) < 32 or ord(character) == 127 for character in zone_name)
        ):
            raise HTTPException(400, "Zone name must be 1 to 100 safe characters")
        if original_name and zone_name != original_name:
            raise HTTPException(400, "Zone names are immutable; create a new zone to rename safely")
        if channel not in {23, 24, 25}:
            raise HTTPException(400, "School paging zones must use Poly channels 23, 24, or 25")
        cfg = config()
        if not destinations or any(name not in cfg.destination_map for name in destinations):
            raise HTTPException(400, "Select at least one valid destination")
        try:
            zone = Zone.model_validate(
                {
                    "name": zone_name,
                    "channel": channel,
                    "description": description,
                    "destinations": destinations,
                }
            )
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)[0]["msg"]) from exc
        if not zone.description:
            raise HTTPException(400, "Zone name and description are required")
        zone_document = zone.model_dump(mode="json")

        def mutate(document: Any, current: BellConfig) -> None:
            zones = document.setdefault("zones", [])
            names = [str(item.get("name", "")) for item in zones]
            if original_name:
                if original_name not in current.zone_map or original_name not in names:
                    raise HTTPException(404, "That zone no longer exists")
                if any(
                    item.name != original_name and item.channel == zone.channel
                    for item in current.zones
                ):
                    raise HTTPException(
                        409, "That Poly channel is already assigned to another zone"
                    )
                zones[names.index(original_name)] = zone_document
            else:
                if zone.name in current.zone_map:
                    raise HTTPException(409, "A zone with that name already exists")
                if any(item.channel == zone.channel for item in current.zones):
                    raise HTTPException(
                        409, "That Poly channel is already assigned to another zone"
                    )
                zones.append(zone_document)

        write_config_document("zones.yaml", "zones", config_hash, mutate)
        return RedirectResponse("/setup?message=Zone+saved+and+activated.#zones", status_code=303)

    @app.post("/setup/zones/delete")
    def zone_delete(
        request: Request,
        zone_name: str = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)

        def mutate(document: Any, current: BellConfig) -> None:
            if zone_name not in current.zone_map:
                raise HTTPException(404, "That zone no longer exists")
            references = zone_references(current, zone_name)
            if references:
                raise HTTPException(409, "Zone is still used by: " + ", ".join(references))
            document["zones"] = [
                item for item in document.get("zones", []) if str(item.get("name", "")) != zone_name
            ]

        write_config_document("zones.yaml", "zones", config_hash, mutate)
        return RedirectResponse("/setup?message=Zone+deleted.#zones", status_code=303)

    @app.post("/setup/destinations/save")
    def destination_save(
        request: Request,
        original_name: str = Form(default=""),
        destination_name: str = Form(),
        protocol: str = Form(),
        port: int = Form(),
        group: str = Form(default=""),
        ttl: int = Form(default=1),
        wire_format: str = Form(default=""),
        codecs: list[str] = Form(),  # noqa: B008
        sip_uri: str = Form(default=""),
        sip_host: str = Form(default=""),
        sip_transport: str = Form(default="udp"),
        sip_username: str = Form(default=""),
        sip_password_env: str = Form(default=""),
        tls_server_name: str = Form(default=""),
        tls_ca_file: str = Form(default=""),
        webhook_url: str = Form(default=""),
        webhook_secret_env: str = Form(default=""),
        healthcheck_url: str = Form(default=""),
        allow_insecure_http: bool = Form(default=False),
        timeout_seconds: float = Form(default=5.0),
        retries: int = Form(default=2),
        required: bool = Form(default=False),
        enabled: bool = Form(default=False),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        destination_name = destination_name.strip()
        if (
            not destination_name
            or len(destination_name) > 100
            or any(ord(character) < 32 or ord(character) == 127 for character in destination_name)
        ):
            raise HTTPException(400, "Destination name must be 1 to 100 safe characters")
        if original_name and destination_name != original_name:
            raise HTTPException(
                400, "Destination names are immutable; create a new destination to rename safely"
            )
        payload: dict[str, Any] = {
            "name": destination_name,
            "protocol": protocol,
            "port": port,
            "ttl": ttl,
            "wire_format": (wire_format or None) if protocol == "multicast" else None,
            "codecs": codecs,
            "group": group.strip() if protocol == "multicast" else None,
            "sip_uri": (sip_uri or None) if protocol == "sip" else None,
            "sip_host": (sip_host or None) if protocol == "sip" else None,
            "sip_transport": sip_transport if protocol == "sip" else "udp",
            "sip_username": (sip_username or None) if protocol == "sip" else None,
            "sip_password_env": (sip_password_env or None) if protocol == "sip" else None,
            "tls_server_name": (tls_server_name or None) if protocol == "sip" else None,
            "tls_ca_file": (tls_ca_file or None) if protocol == "sip" else None,
            "webhook_url": (webhook_url or None) if protocol == "http" else None,
            "webhook_secret_env": ((webhook_secret_env or None) if protocol == "http" else None),
            "allow_insecure_http": allow_insecure_http if protocol == "http" else False,
            "healthcheck_url": (healthcheck_url or None) if protocol == "http" else None,
            "timeout_seconds": timeout_seconds,
            "retries": retries,
            "required": required,
            "enabled": enabled,
        }
        try:
            destination = Destination.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)[0]["msg"]) from exc
        destination_document = destination.model_dump(mode="json", exclude_none=True)

        def mutate(document: Any, current: BellConfig) -> None:
            destinations_doc = document.setdefault("destinations", [])
            names = [str(item.get("name", "")) for item in destinations_doc]
            if original_name:
                if original_name not in current.destination_map or original_name not in names:
                    raise HTTPException(404, "That destination no longer exists")
                destinations_doc[names.index(original_name)] = destination_document
            else:
                if destination.name in current.destination_map:
                    raise HTTPException(409, "A destination with that name already exists")
                destinations_doc.append(destination_document)

        write_config_document("destinations.yaml", "destinations", config_hash, mutate)
        return RedirectResponse(
            "/setup?message=Destination+saved+and+activated.#destinations", status_code=303
        )

    @app.post("/setup/destinations/delete")
    def destination_delete(
        request: Request,
        destination_name: str = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)

        def mutate(document: Any, current: BellConfig) -> None:
            if destination_name not in current.destination_map:
                raise HTTPException(404, "That destination no longer exists")
            references = destination_references(current, destination_name)
            if references:
                raise HTTPException(
                    409, "Destination is still used by zones: " + ", ".join(references)
                )
            document["destinations"] = [
                item
                for item in document.get("destinations", [])
                if str(item.get("name", "")) != destination_name
            ]

        write_config_document("destinations.yaml", "destinations", config_hash, mutate)
        return RedirectResponse("/setup?message=Destination+deleted.#destinations", status_code=303)

    @app.post("/setup/calendar/defaults/save")
    def calendar_defaults_save(
        request: Request,
        monday: str = Form(default=""),
        tuesday: str = Form(default=""),
        wednesday: str = Form(default=""),
        thursday: str = Form(default=""),
        friday: str = Form(default=""),
        saturday: str = Form(default=""),
        sunday: str = Form(default=""),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        defaults = {
            "monday": monday or None,
            "tuesday": tuesday or None,
            "wednesday": wednesday or None,
            "thursday": thursday or None,
            "friday": friday or None,
            "saturday": saturday or None,
            "sunday": sunday or None,
        }
        cfg = config()
        invalid = sorted(
            {name for name in defaults.values() if name and name not in cfg.schedule_map}
        )
        if invalid:
            raise HTTPException(400, "Unknown schedules: " + ", ".join(invalid))

        def mutate(document: Any, _current: BellConfig) -> None:
            document["calendar"]["weekday_defaults"] = defaults

        write_config_document("calendar.yaml", "calendar", config_hash, mutate)
        return RedirectResponse(
            "/setup?message=Weekday+defaults+saved+and+activated.#calendar-rules",
            status_code=303,
        )

    @app.post("/setup/calendar/ranges/save")
    def calendar_range_save(
        request: Request,
        range_index: int = Form(default=-1),
        start: date = Form(),  # noqa: B008
        end: date = Form(),  # noqa: B008
        schedule_name: str = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        if schedule_name not in cfg.schedule_map:
            raise HTTPException(400, "Choose a valid schedule")
        try:
            rule = DateRangeRule(start=start, end=end, schedule=schedule_name)
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)[0]["msg"]) from exc
        rule_document = rule.model_dump(mode="json")

        def mutate(document: Any, current: BellConfig) -> None:
            ranges = document["calendar"].setdefault("date_ranges", [])
            if range_index == -1:
                ranges.append(rule_document)
            elif not 0 <= range_index < len(current.calendar.date_ranges) or range_index >= len(
                ranges
            ):
                raise HTTPException(409, "That date range changed or no longer exists")
            else:
                ranges[range_index] = rule_document

        write_config_document("calendar.yaml", "calendar", config_hash, mutate)
        action = "created" if range_index == -1 else "updated"
        return RedirectResponse(
            f"/setup?{urlencode({'message': f'Date range {action}.'})}#calendar-rules",
            status_code=303,
        )

    @app.post("/setup/calendar/ranges/delete")
    def calendar_range_delete(
        request: Request,
        range_index: int = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)

        def mutate(document: Any, current: BellConfig) -> None:
            ranges = document["calendar"].setdefault("date_ranges", [])
            if not 0 <= range_index < len(current.calendar.date_ranges) or range_index >= len(
                ranges
            ):
                raise HTTPException(409, "That date range changed or no longer exists")
            del ranges[range_index]

        write_config_document("calendar.yaml", "calendar", config_hash, mutate)
        return RedirectResponse(
            "/setup?message=Date+range+deleted.#calendar-rules", status_code=303
        )

    @app.post("/setup/settings/save")
    def settings_save(
        request: Request,
        interface_ip: str = Form(),
        wire_format: str = Form(),
        poly_caller_id: str = Form(),
        alert_webhook_url: str = Form(default=""),
        alert_webhook_secret_env: str = Form(default=""),
        alert_allow_insecure_http: bool = Form(default=False),
        rtc_required: bool = Form(default=False),
        endpoint_check_interval_seconds: int = Form(),
        api_rate_limit_per_minute: int = Form(),
        clock_sync_required: bool = Form(default=False),
        max_audio_seconds: float = Form(),
        max_page_seconds: float = Form(),
        allowed_hours_start: str = Form(),
        allowed_hours_end: str = Form(),
        max_events_per_day: int = Form(),
        max_repeats: int = Form(),
        emergency_priority_threshold: int = Form(),
        kill_switch_enabled: bool = Form(default=False),
        kill_switch_until: str = Form(default=""),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        current = config()
        try:
            settings = Settings.model_validate(
                {
                    "timezone": "America/Denver",
                    "interface_ip": interface_ip,
                    "wire_format": wire_format,
                    "poly_caller_id": poly_caller_id,
                    "school_name": current.settings.school_name,
                    "console_subtitle": current.settings.console_subtitle,
                    "logo_filename": current.settings.logo_filename,
                    "alert_webhook_url": alert_webhook_url.strip() or None,
                    "alert_webhook_secret_env": alert_webhook_secret_env.strip() or None,
                    "alert_allow_insecure_http": alert_allow_insecure_http,
                    "rtc_required": rtc_required,
                    "endpoint_check_interval_seconds": endpoint_check_interval_seconds,
                    "api_rate_limit_per_minute": api_rate_limit_per_minute,
                    "clock_sync_required": clock_sync_required,
                    "max_audio_seconds": max_audio_seconds,
                    "max_page_seconds": max_page_seconds,
                    "poly_group_page_calibration": current.settings.poly_group_page_calibration,
                    "sounds_dir": current.settings.sounds_dir,
                    "state_dir": current.settings.state_dir,
                    "log_dir": current.settings.log_dir,
                }
            )
            safety = Safety.model_validate(
                {
                    "allowed_hours_start": allowed_hours_start,
                    "allowed_hours_end": allowed_hours_end,
                    "max_events_per_day": max_events_per_day,
                    "max_repeats": max_repeats,
                    "emergency_priority_threshold": emergency_priority_threshold,
                    "kill_switch_enabled": kill_switch_enabled,
                    "kill_switch_until": kill_switch_until or None,
                    "pause_until": current.safety.pause_until,
                    "pause_reason": current.safety.pause_reason,
                }
            )
        except ValidationError as exc:
            raise HTTPException(400, exc.errors(include_url=False)[0]["msg"]) from exc

        def mutate(document: Any, _current: BellConfig) -> None:
            settings_doc = document.setdefault("settings", {})
            settings_doc.update(settings.model_dump(mode="json"))
            safety_doc = document.setdefault("safety", {})
            safety_payload = safety.model_dump(mode="json")
            safety_payload["allowed_hours_start"] = DoubleQuotedScalarString(
                safety.allowed_hours_start.strftime("%H:%M")
            )
            safety_payload["allowed_hours_end"] = DoubleQuotedScalarString(
                safety.allowed_hours_end.strftime("%H:%M")
            )
            safety_doc.update(safety_payload)

        write_config_document("settings.yaml", "settings", config_hash, mutate)
        return RedirectResponse(
            "/setup?message=Settings+saved+and+activated.#settings", status_code=303
        )

    @app.post("/setup/alerts/test")
    def alert_test(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        outcome = AlertDispatcher(cfg.settings, outbox_path=cfg.state_path / "alerts.sqlite3").send(
            "operator_test",
            "School Bell test alert",
            severity="info",
            details={"source": "front-office setup"},
            force=True,
        )
        record_audit("alert_test", "operational webhook", outcome.detail)
        message = (
            "Test alert sent." if outcome.success else f"Test alert did not send: {outcome.detail}"
        )
        return RedirectResponse(
            f"/setup?{urlencode({'message': message})}#settings", status_code=303
        )

    def valid_sound_target(name: str) -> str:
        normalized = name.strip()
        if normalized.lower().endswith(".wav"):
            normalized = normalized[:-4]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,95}", normalized):
            raise HTTPException(
                400,
                "Sound name must start with a letter or number and use only letters, numbers, spaces, dots, underscores, or hyphens",
            )
        return f"{normalized}.wav"

    def sound_error(message: str) -> RedirectResponse:
        return RedirectResponse(
            f"/setup?{urlencode({'error': message})}#sounds",
            status_code=303,
        )

    @app.post("/setup/sounds/save")
    async def sound_save(
        request: Request,
        audio_file: UploadFile = File(),  # noqa: B008
        desired_name: str = Form(default=""),
        existing_name: str = Form(default=""),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        uploaded_name = Path(audio_file.filename or "").stem
        target_name = valid_sound_target(desired_name or uploaded_name)
        if existing_name and target_name != existing_name:
            raise HTTPException(
                400, "Replacement keeps the existing name; create a new sound to rename"
            )
        cfg = config()
        staging_dir = cfg.state_path / "sound-staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        source_suffix = Path(audio_file.filename or "upload").suffix[:10]
        source_path: Path | None = None
        prepared_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", dir=staging_dir, prefix="upload-", suffix=source_suffix, delete=False
            ) as handle:
                source_path = Path(handle.name)
                total = 0
                while chunk := await audio_file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 50 * 1024 * 1024:
                        return sound_error("The selected audio file exceeds the 50 MiB limit.")
                    handle.write(chunk)
            if not total:
                return sound_error("The selected audio file is empty.")
            info = probe_audio(source_path)
            if info.duration <= 0 or info.duration > cfg.settings.max_audio_seconds:
                return sound_error(
                    "Audio must contain sound and be no longer than "
                    f"{cfg.settings.max_audio_seconds:g} seconds."
                )
            prepared_path = staging_dir / f"prepared-{secrets.token_hex(8)}.wav"
            prep(source_path, prepared_path, max_seconds=cfg.settings.max_audio_seconds)
            prepared = probe_audio(prepared_path)
            if (
                prepared.duration <= 0
                or prepared.sample_rate != 8000
                or prepared.channels != 1
            ):
                return sound_error(
                    "The prepared file contains no audible audio. "
                    "Try exporting it again as PCM WAV, MP3, or FLAC."
                )
            target = cfg.sounds_path / target_name
            with config_lock:
                current = config()
                if not config_hash or not secrets.compare_digest(config_hash, current.hash):
                    raise HTTPException(409, "Configuration changed. Reload and try again.")
                if existing_name:
                    if existing_name not in safe_sound_names(current):
                        raise HTTPException(404, "That sound no longer exists")
                elif target.exists():
                    raise HTTPException(409, "A sound with that name already exists")
                backup_dir = current.state_path / "sound-backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / f"{target_name}-{secrets.token_hex(8)}.wav"
                had_target = target.exists()
                if had_target:
                    shutil.copy2(target, backup)
                target_temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        dir=target.parent,
                        prefix=".sound-",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        target_temporary = Path(handle.name)
                    shutil.copy2(prepared_path, target_temporary)
                    target_temporary.replace(target)
                finally:
                    if target_temporary:
                        target_temporary.unlink(missing_ok=True)
                prepared_path = None
                try:
                    load_config(directory)
                    if reload_callback:
                        reload_callback()
                except Exception as exc:
                    if had_target:
                        shutil.copy2(backup, target)
                    else:
                        target.unlink(missing_ok=True)
                    if reload_callback:
                        try:
                            reload_callback()
                        except Exception:
                            LOGGER.exception("sound_rollback_reload_failed")
                    raise HTTPException(
                        500, "The sound could not be activated; the previous library was restored"
                    ) from exc
        except (AudioProcessingError, AudioToolMissing):
            LOGGER.info("sound_upload_decode_failed")
            return sound_error(
                "The audio could not be decoded. Try exporting it as PCM WAV, MP3, "
                "M4A, FLAC, OGG, or Opus, then upload it again."
            )
        except OSError:
            LOGGER.exception("sound_upload_storage_failed")
            return sound_error(
                "The sound library could not be written. Install the latest update and "
                "try again; details were recorded in the service log."
            )
        finally:
            await audio_file.close()
            if source_path:
                source_path.unlink(missing_ok=True)
            if prepared_path:
                prepared_path.unlink(missing_ok=True)
        action = "replaced" if existing_name else "added"
        return RedirectResponse(
            f"/setup?{urlencode({'message': f'Sound {target_name} {action}.'})}#sounds",
            status_code=303,
        )

    @app.post("/setup/sounds/delete")
    def sound_delete(
        request: Request,
        sound_name: str = Form(),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        with config_lock:
            current = config()
            if not config_hash or not secrets.compare_digest(config_hash, current.hash):
                raise HTTPException(409, "Configuration changed. Reload and try again.")
            if sound_name not in safe_sound_names(current):
                raise HTTPException(404, "That sound no longer exists")
            references = sound_references(current, sound_name)
            if references:
                raise HTTPException(409, "Sound is still used by: " + ", ".join(references))
            path = current.sounds_path / sound_name
            backup_dir = current.state_path / "sound-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"deleted-{sound_name}-{secrets.token_hex(8)}.wav"
            shutil.copy2(path, backup)
            path.unlink()
            try:
                load_config(directory)
                if reload_callback:
                    reload_callback()
            except Exception as exc:
                shutil.copy2(backup, path)
                if reload_callback:
                    try:
                        reload_callback()
                    except Exception:
                        LOGGER.exception("sound_delete_rollback_reload_failed")
                raise HTTPException(
                    500, "The sound could not be deleted; the previous library was restored"
                ) from exc
        return RedirectResponse("/setup?message=Sound+deleted.#sounds", status_code=303)

    @app.get("/sounds/{sound_name}")
    def sound_preview(request: Request, sound_name: str) -> FileResponse:
        require_auth(request)
        cfg = config()
        if Path(sound_name).name != sound_name or sound_name not in safe_sound_names(cfg):
            raise HTTPException(404, "That sound does not exist")
        sound_path = cfg.sounds_path / sound_name
        media_type = mimetypes.guess_type(sound_name)[0] or "application/octet-stream"
        return FileResponse(
            sound_path,
            media_type=media_type,
            filename=sound_name,
            content_disposition_type="inline",
        )

    @app.get("/manual", response_class=HTMLResponse)
    def manual_page(
        request: Request,
        message: str | None = None,
        sound: str | None = None,
        zone: str | None = None,
    ) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        sounds = sorted(path.name for path in cfg.sounds_path.iterdir() if path.is_file())
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        within_hours = within_allowed_hours(
            now.timetz().replace(tzinfo=None),
            cfg.safety.allowed_hours_start,
            cfg.safety.allowed_hours_end,
        )
        return render(
            request,
            "manual.html",
            config=cfg,
            sounds=sounds,
            message=message,
            transmission_completed=message == "transmission completed",
            within_hours=within_hours,
            school_time=now,
            selected_sound=sound if sound in sounds else None,
            selected_zone=zone if zone in cfg.zone_map else None,
        )

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
        if zone not in cfg.zone_map or sound not in {
            path.name for path in cfg.sounds_path.iterdir()
        }:
            raise HTTPException(400, "Choose a valid sound and zone")
        try:
            digest = sound_digest(cfg.sounds_path / sound)
        except OSError as exc:
            raise HTTPException(400, "Sound is unavailable; choose another sound") from exc
        payload = {"sound": sound, "zone": zone, "override_hours": override_hours,
                   "action_id": secrets.token_hex(24), "config_hash": cfg.hash,
                   "sound_digest": digest}
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
            raise HTTPException(
                400, "Confirmation expired or is invalid. Please start again."
            ) from exc
        if not isinstance(payload, dict) or not all(
            key in payload for key in ("action_id", "config_hash", "sound_digest", "sound", "zone")
        ):
            raise HTTPException(400, "Confirmation predates this release; review the page again")
        now = datetime.now(ZoneInfo(config().settings.timezone))
        if not manual_actions.claim(payload["action_id"], now):
            return RedirectResponse(
                f"/manual?{urlencode({'message': manual_actions.result(payload['action_id'])})}",
                status_code=303,
            )
        with config_lock:
            cfg = config()
            try:
                unchanged = (cfg.hash == payload["config_hash"]
                             and payload["zone"] in cfg.zone_map
                             and sound_digest(cfg.sounds_path / payload["sound"]) == payload["sound_digest"])
            except OSError:
                unchanged = False
            if not unchanged:
                reason = "Configuration or audio changed. Review a new page before sending."
                manual_actions.finish(payload["action_id"], reason)
                return RedirectResponse(f"/manual?{urlencode({'message': reason})}", status_code=303)
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
                extra={
                    "action": "manual_fire",
                    "target": payload,
                    "result": "blocked",
                    "reason": decision.reason,
                },
            )
            manual_actions.finish(payload["action_id"], decision.reason)
            return RedirectResponse(f"/manual?{urlencode({'message': decision.reason})}", status_code=303)
        if scheduler is None:
            result_reason = "validated (test mode; no scheduler attached)"
        else:
            event_time = now.timetz().replace(second=0, microsecond=0, tzinfo=None)
            from bell.config import BellEvent

            event = BellEvent(
                time=event_time,
                sound=payload["sound"],
                zone=payload["zone"],
                label="Manual office trigger",
            )
            planned = PlannedEvent(event, "Manual", now, action_id=payload["action_id"])
            decision = scheduler.fire(
                planned,
                now=now,
                manual=True,
                override_hours=bool(payload.get("override_hours")),
                expected_config_hash=payload["config_hash"],
            )
            result_reason = decision.reason
        manual_actions.finish(payload["action_id"], result_reason)
        LOGGER.info(
            "ui_action",
            extra={"action": "manual_fire", "target": payload, "result": result_reason},
        )
        return RedirectResponse(f"/manual?message={result_reason}", status_code=303)

    @app.get("/commissioning", response_class=HTMLResponse)
    def commissioning_page(request: Request, message: str | None = None) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        health = health_provider() if health_provider else None
        confirmations = scheduler.state.zone_confirmations() if scheduler else {}
        default_sound = (
            "class-bell.wav"
            if (cfg.sounds_path / "class-bell.wav").is_file()
            else safe_sound_names(cfg)[0]
        )
        return render(
            request,
            "commissioning.html",
            config=cfg,
            health=health,
            confirmations=confirmations,
            receiver_records=acceptance.history(cfg, datetime.now(ZoneInfo(cfg.settings.timezone))),
            receiver_fingerprints={zone.name: zone_fingerprint(cfg, zone.name) for zone in cfg.zones},
            default_sound=default_sound,
            message=message,
        )

    @app.post("/commissioning/record")
    async def commissioning_record(request: Request) -> RedirectResponse:
        require_auth(request)
        form = await request.form()
        verify_csrf(request, str(form.get("csrf", "")))
        zone = str(form.get("zone", ""))
        try:
            evidence = ReceiverEvidence.model_validate({
                key: str(form[key]) for key in ReceiverEvidence.model_fields if key in form
            })
        except ValidationError as exc:
            raise HTTPException(400, "Complete receiver identity, ownership and applicable test results") from exc
        with config_lock:
            cfg = config()
            if zone not in cfg.zone_map:
                raise HTTPException(400, "Choose a configured zone")
            fingerprint = zone_fingerprint(cfg, zone)
            if fingerprint != form.get("receiver_fingerprint"):
                raise HTTPException(409, "Receiver configuration changed; repeat the checks before recording")
            acceptance.record(zone, fingerprint, evidence, datetime.now(ZoneInfo(cfg.settings.timezone)))
        return RedirectResponse("/commissioning?message=Receiver+evidence+recorded", status_code=303)

    @app.post("/commissioning/confirm")
    def commissioning_confirm(
        request: Request,
        zone: str = Form(),
        observer: str = Form(),
        note: str = Form(default=""),
        heard: bool = Form(default=False),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        if zone not in cfg.zone_map:
            raise HTTPException(400, "Choose a configured zone")
        normalized_observer = observer.strip()
        normalized_note = note.strip()
        if (
            not heard
            or not normalized_observer
            or len(normalized_observer) > 100
            or len(normalized_note) > 200
        ):
            raise HTTPException(400, "Confirm the audible result and enter a valid observer name")
        if scheduler is None:
            raise HTTPException(503, "Commissioning history is unavailable")
        now = datetime.now(ZoneInfo(cfg.settings.timezone))
        scheduler.state.confirm_zone(zone, normalized_observer, normalized_note, now)
        scheduler.state.record_audit(
            "zone_acceptance", zone, f"heard by {normalized_observer}: {normalized_note}", now
        )
        return RedirectResponse(
            f"/commissioning?{urlencode({'message': f'{zone.title()} acceptance recorded.'})}",
            status_code=303,
        )

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        recent = scheduler.state.recent() if scheduler else []
        health = health_provider() if health_provider else None
        disk = shutil.disk_usage(cfg.config_dir.parent)
        backup_dir = cfg.state_path / "operator-backups"
        latest_backup = max(backup_dir.glob("school-bell-backup-*.tar.gz"), default=None)
        return render(
            request,
            "status.html",
            config=cfg,
            recent=recent,
            health=health,
            disk=disk,
            latest_backup=latest_backup,
            version=__version__,
        )

    @app.get("/history", response_class=HTMLResponse)
    def history_page(
        request: Request,
        result: str | None = None,
        zone: str | None = None,
        source: str | None = None,
    ) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        rows = (
            scheduler.state.recent(250, result=result, zone=zone, source=source)
            if scheduler
            else []
        )
        audits = scheduler.state.recent_audit(100) if scheduler else []
        return render(
            request,
            "history.html",
            config=cfg,
            rows=rows,
            audits=audits,
            filters={"result": result or "", "zone": zone or "", "source": source or ""},
        )

    @app.get("/history/export.csv")
    def history_export(
        request: Request,
        result: str | None = None,
        zone: str | None = None,
        source: str | None = None,
    ) -> Response:
        require_auth(request)
        rows = (
            scheduler.state.recent(1000, result=result, zone=zone, source=source)
            if scheduler
            else []
        )
        output = io.StringIO(newline="")
        fields = [
            "attempted_at",
            "result",
            "source",
            "label",
            "zone",
            "sound",
            "scheduled_at",
            "detail",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=school-bell-history.csv"},
        )

    @app.get("/recovery", response_class=HTMLResponse)
    def recovery_page(request: Request, message: str | None = None) -> HTMLResponse:
        require_auth(request)
        cfg = config()
        backup_dir = cfg.state_path / "operator-backups"
        backups = (
            sorted(backup_dir.glob("school-bell-backup-*.tar.gz"), reverse=True)[:10]
            if backup_dir.is_dir()
            else []
        )
        return render(
            request,
            "recovery.html",
            config=cfg,
            config_hash=cfg.hash,
            backups=backups,
            continuity=continuity.snapshot(datetime.now(ZoneInfo(cfg.settings.timezone)).date()),
            message=message,
        )

    @app.post("/recovery/ownership")
    async def continuity_save(request: Request) -> RedirectResponse:
        require_auth(request)
        form = await request.form()
        verify_csrf(request, str(form.get("csrf", "")))
        try:
            values = {key: str(form[key]) for key in ContinuityPlan.model_fields if key in form}
            for key in ("last_restore", "last_offdevice_copy"):
                if not values.get(key):
                    values[key] = None
            plan = ContinuityPlan.model_validate(values)
            continuity.record(plan, datetime.now(ZoneInfo(config().settings.timezone)),
                              int(str(form.get("revision", "-1"))))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(400, "Complete valid ownership, dates and witnessed results; reload if edited elsewhere") from exc
        record_audit("continuity_record", plan.owner, plan.restore_result)
        return RedirectResponse("/recovery?message=Continuity+record+saved", status_code=303)

    @app.post("/recovery/export")
    def recovery_export(request: Request, csrf: str = Form()) -> FileResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        try:
            archive = create_portable_backup(cfg, cfg.state_path / "operator-backups")
        except (OSError, RecoveryError) as exc:
            raise HTTPException(500, f"Backup could not be created: {exc}") from exc
        LOGGER.info("ui_action", extra={"action": "backup_export", "result": "success"})
        return FileResponse(archive, media_type="application/gzip", filename=archive.name)

    @app.post("/recovery/restore")
    async def recovery_restore(
        request: Request,
        backup_file: UploadFile = File(),  # noqa: B008
        confirm_restore: bool = Form(default=False),
        config_hash: str = Form(default=""),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        if not confirm_restore:
            raise HTTPException(400, "Confirm that the current configuration will be replaced")
        cfg = config()
        if not config_hash or not secrets.compare_digest(config_hash, cfg.hash):
            raise HTTPException(409, "Configuration changed. Reload and try again.")
        staging = cfg.state_path / "restore-staging"
        staging.mkdir(parents=True, exist_ok=True)
        source: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", dir=staging, prefix="restore-", suffix=".tar.gz", delete=False
            ) as handle:
                source = Path(handle.name)
                total = 0
                while chunk := await backup_file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 50 * 1024 * 1024:
                        raise HTTPException(413, "Backup upload exceeds the 50 MiB limit")
                    handle.write(chunk)
            if not total:
                raise HTTPException(400, "Uploaded backup is empty")
            with config_lock:
                refreshed = config()
                if not secrets.compare_digest(config_hash, refreshed.hash):
                    raise HTTPException(409, "Configuration changed. Reload and try again.")
                restore_portable_backup(
                    source,
                    directory.parent,
                    refreshed.state_path / "operator-backups",
                    reload_callback=reload_callback,
                )
        except RecoveryError as exc:
            LOGGER.warning("backup_restore_rejected", extra={"detail": str(exc)})
            raise HTTPException(400, str(exc)) from exc
        finally:
            if source:
                source.unlink(missing_ok=True)
        LOGGER.warning("ui_action", extra={"action": "backup_restore", "result": "success"})
        record_audit("backup_restore", "configuration", "portable backup restored")
        return RedirectResponse(
            f"/recovery?{urlencode({'message': 'Backup restored and activated.'})}",
            status_code=303,
        )

    @app.post("/recovery/support")
    def recovery_support(request: Request, csrf: str = Form()) -> FileResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        cfg = config()
        health = health_provider() if health_provider else None
        recent = scheduler.state.recent(50) if scheduler else []
        try:
            archive = create_support_bundle(
                cfg,
                cfg.state_path / "support-bundles",
                health=health,
                recent=recent,
            )
        except OSError as exc:
            raise HTTPException(500, f"Support bundle could not be created: {exc}") from exc
        LOGGER.info("ui_action", extra={"action": "support_bundle", "result": "success"})
        return FileResponse(archive, media_type="application/zip", filename=archive.name)

    @app.get("/updates", response_class=HTMLResponse)
    def updates_page(request: Request, queued: bool = False) -> HTMLResponse:
        require_auth(request)
        if updates_enabled:
            cfg = config()
            try:
                status = load_update_status(cfg.state_path)
            except UpdateRequestError as exc:
                status = {"phase": "failed", "message": str(exc)}
        else:
            status = {
                "phase": "disabled",
                "installed_version": __version__,
                "message": (
                    "Production OTA is disabled in Docker. Rebuild the local image to test newer "
                    "code; use the signed updater only on the Raspberry Pi appliance."
                ),
            }
        return render(
            request,
            "updates.html",
            status=status,
            queued=queued,
            updates_enabled=updates_enabled,
        )

    @app.post("/updates/check")
    def updates_check(request: Request, csrf: str = Form()) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        require_updates_enabled()
        cfg = config()
        try:
            with update_lock:
                request_id = queue_update_request(cfg.state_path, "check")
        except UpdateRequestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        LOGGER.info(
            "ui_action",
            extra={"action": "update_check", "target": request_id, "result": "queued"},
        )
        return RedirectResponse("/updates?queued=true", status_code=303)

    @app.post("/updates/prepare", response_class=HTMLResponse)
    def updates_prepare(
        request: Request,
        tag: str = Form(),
        digest: str = Form(),
        csrf: str = Form(),
    ) -> HTMLResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        require_updates_enabled()
        cfg = config()
        try:
            status = load_update_status(cfg.state_path)
        except UpdateRequestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        release = status.get("release")
        if (
            status.get("phase") != "update_available"
            or not isinstance(release, dict)
            or release.get("tag") != tag
            or release.get("digest") != digest
        ):
            raise HTTPException(
                status_code=409,
                detail="Release information changed or expired. Check for updates again.",
            )
        token = update_signer.dumps({"tag": tag, "digest": digest})
        return render(request, "update_confirm.html", release=release, token=token)

    @app.post("/updates/install")
    def updates_install(
        request: Request,
        confirm_token: str = Form(),
        csrf: str = Form(),
    ) -> RedirectResponse:
        require_auth(request)
        verify_csrf(request, csrf)
        require_updates_enabled()
        try:
            payload = update_signer.loads(confirm_token, max_age=300)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(
                status_code=400,
                detail="Update confirmation expired or is invalid. Check again.",
            ) from exc
        tag = payload.get("tag") if isinstance(payload, dict) else None
        digest = payload.get("digest") if isinstance(payload, dict) else None
        if not isinstance(tag, str) or not isinstance(digest, str):
            raise HTTPException(status_code=400, detail="Update confirmation is invalid")
        cfg = config()
        try:
            status = load_update_status(cfg.state_path)
            release = status.get("release")
            if (
                status.get("phase") != "update_available"
                or not isinstance(release, dict)
                or release.get("tag") != tag
                or release.get("digest") != digest
            ):
                raise UpdateRequestError(
                    "Release information changed or expired. Check for updates again."
                )
            with update_lock:
                request_id = queue_update_request(
                    cfg.state_path,
                    "install",
                    tag=tag,
                    digest=digest,
                )
        except UpdateRequestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        LOGGER.warning(
            "ui_action",
            extra={
                "action": "production_update",
                "target": {"tag": tag, "digest": digest},
                "request_id": request_id,
                "result": "queued",
            },
        )
        return RedirectResponse("/updates?queued=true", status_code=303)

    @app.get("/api/v1/health")
    def api_health(request: Request) -> dict[str, Any]:
        monitor_key = os.environ.get("BELL_MONITOR_API_KEY", "")
        supplied = request.headers.get("X-Bell-API-Key", "")
        if not monitor_key or not secrets.compare_digest(supplied, monitor_key):
            api_scope(request)
        cfg = config()
        return (
            health_provider()
            if health_provider
            else {
                "status": "unknown",
                "ready": False,
                "readiness_reasons": ["Runtime health unavailable"],
                "config_valid": True,
                "config_hash": cfg.hash,
            }
        )

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
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in idempotency_key
            )
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
            scheduler.state.expire_stale_api_request(
                idempotency_key, datetime.now(ZoneInfo(config().settings.timezone))
            )
            existing = scheduler.state.api_result(idempotency_key) or existing
            status_code = 409 if existing["status"] == "indeterminate" else 200
            return JSONResponse({"idempotent_replay": True, **existing}, status_code=status_code)
        cfg = config()
        if trigger.zone not in cfg.zone_map:
            raise HTTPException(status_code=400, detail="Unknown zone")
        if not (cfg.sounds_path / trigger.sound).is_file():
            raise HTTPException(status_code=400, detail="Unknown sound")
        if trigger.repeat_count > cfg.safety.max_repeats:
            raise HTTPException(
                status_code=400, detail="repeat_count exceeds the configured safety limit"
            )
        emergency = (
            trigger.priority >= cfg.safety.emergency_priority_threshold or trigger.override_hours
        )
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
