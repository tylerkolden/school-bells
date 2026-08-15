"""Bell scheduler daemon, startup validation, and localhost health service."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bell.audio import transcode
from bell.config import BellConfig, BellEvent, ConfigLoadError, load_config
from bell.delivery import DeliveryManager, DeliveryReport, PageDeliveryError
from bell.logging_setup import configure_logging
from bell.monitor import EndpointMonitor, EndpointRegistry
from bell.paging import PageCoordinator
from bell.scheduler import BellScheduler

LOGGER = logging.getLogger(__name__)


def clock_sync_status() -> tuple[bool | None, str]:
    timedatectl = shutil.which("timedatectl")
    if not timedatectl:
        return None, "timedatectl unavailable on this platform"
    result = subprocess.run(
        [timedatectl, "show", "--property=NTPSynchronized", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or "unable to query NTP status"
    synced = result.stdout.strip().lower() == "yes"
    return synced, "NTP synchronized" if synced else "NTP is not synchronized"


def interface_present(interface_ip: str) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((interface_ip, 0))
    except OSError as exc:
        return False, f"interface IP {interface_ip} is not present: {exc}"
    finally:
        sock.close()
    return True, f"interface IP {interface_ip} is present"


def validate_startup(config: BellConfig) -> list[str]:
    errors: list[str] = []
    secret_lengths = {
        "BELL_UI_PASSWORD": 12,
        "BELL_UI_SESSION_SECRET": 32,
        "BELL_API_KEY": 24,
        "BELL_EMERGENCY_API_KEY": 24,
    }
    for name, minimum in secret_lengths.items():
        value = os.environ.get(name)
        if value and len(value) < minimum:
            errors.append(f"{name} must contain at least {minimum} characters")
    normal_api_key = os.environ.get("BELL_API_KEY")
    emergency_api_key = os.environ.get("BELL_EMERGENCY_API_KEY")
    if normal_api_key and emergency_api_key and secrets.compare_digest(
        normal_api_key, emergency_api_key
    ):
        errors.append("BELL_API_KEY and BELL_EMERGENCY_API_KEY must be different")
    tls_certificate = os.environ.get("BELL_TLS_CERTFILE")
    tls_key = os.environ.get("BELL_TLS_KEYFILE")
    if bool(tls_certificate) != bool(tls_key):
        errors.append("BELL_TLS_CERTFILE and BELL_TLS_KEYFILE must be configured together")
    for label, value in (("TLS certificate", tls_certificate), ("TLS private key", tls_key)):
        if value and not Path(value).is_file():
            errors.append(f"{label} file does not exist: {value}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        errors.append("ffmpeg/ffprobe missing; install with: sudo apt install ffmpeg")
    else:
        sounds = {event.sound for schedule in config.schedules for event in schedule.events}
        sounds.update(
            event.pre_tone
            for schedule in config.schedules
            for event in schedule.events
            if event.pre_tone
        )
        sounds.update(item.sound for item in config.standing_items if item.enabled)
        sounds.update(item.pre_tone for item in config.standing_items if item.enabled and item.pre_tone)
        for sound_name in sorted(sounds):
            try:
                raw = transcode(config.sounds_path / sound_name)
                duration = raw.stat().st_size / 8000.0
                if duration > config.settings.max_audio_seconds:
                    errors.append(
                        f"sound {sound_name} is {duration:.2f}s; maximum is "
                        f"{config.settings.max_audio_seconds:.2f}s"
                    )
            except Exception as exc:
                errors.append(f"cannot transcode {sound_name}: {exc}")
    interface_ok, interface_detail = interface_present(config.settings.interface_ip)
    if not interface_ok:
        errors.append(interface_detail)
    else:
        for endpoint in config.destinations:
            if not endpoint.enabled or endpoint.protocol != "multicast":
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                try:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(config.settings.interface_ip),
                    )
                finally:
                    sock.close()
            except OSError as exc:
                errors.append(f"cannot create multicast socket for {endpoint.name}: {exc}")
    for endpoint in config.destinations:
        if not endpoint.enabled:
            continue
        secret_env = (
            endpoint.sip_password_env
            if endpoint.protocol == "sip"
            else endpoint.webhook_secret_env if endpoint.protocol == "http" else None
        )
        if secret_env and not os.environ.get(secret_env) and endpoint.required:
            errors.append(f"required destination {endpoint.name}: environment variable {secret_env} is not set")
    synced, detail = clock_sync_status()
    if synced is not True and config.settings.clock_sync_required:
        errors.append(f"system clock is not synchronized: {detail}")
    elif synced is not True:
        LOGGER.warning("clock_sync_warning", extra={"status": synced, "detail": detail})
    return errors


class ServiceRuntime:
    def __init__(self, config: BellConfig) -> None:
        self.config = config
        self.started_monotonic = time.monotonic()
        self.last_fire: dict[str, Any] | None = None
        self.coordinator = PageCoordinator()
        self.endpoint_registry = EndpointRegistry()
        self.delivery = DeliveryManager(config, self.endpoint_registry)
        self.monitor = EndpointMonitor(config, self.endpoint_registry)
        self.scheduler = BellScheduler(config, self.transmit_event)
        self.health_app = self._health_app()

    def reload_config(self) -> None:
        refreshed = load_config(self.config.config_dir)
        self.config = refreshed
        self.scheduler.config = refreshed
        self.delivery.update_config(refreshed)
        self.monitor.update_config(refreshed)
        self.scheduler.register_day(
            datetime.now(self.scheduler.timezone).date(), include_recent_misfires=False
        )
        LOGGER.info("configuration_reloaded", extra={"config_hash": refreshed.hash})

    def health_data(self) -> dict[str, Any]:
        sync, sync_detail = clock_sync_status()
        jobs = [job for job in self.scheduler.scheduler.get_jobs() if job.next_run_time]
        next_fire = min((job.next_run_time for job in jobs), default=None)
        endpoints = self.endpoint_registry.snapshot()
        required = {item.name for item in self.config.destinations if item.enabled and item.required}
        unhealthy = [
            item["name"]
            for item in endpoints
            if item["name"] in required and item["state"] in {"unhealthy", "circuit_open"}
        ]
        known = {str(item["name"]) for item in endpoints}
        unknown_required = sorted(required - known)
        scheduler_running = self.scheduler.scheduler.running
        monitor_running = self.monitor.is_alive
        reasons: list[str] = []
        if unhealthy:
            reasons.append("required endpoint unhealthy")
        if unknown_required:
            reasons.append("required endpoint not checked")
        if sync is not True and self.config.settings.clock_sync_required:
            reasons.append("clock not synchronized")
        if not scheduler_running:
            reasons.append("scheduler not running")
        if not monitor_running:
            reasons.append("endpoint monitor not running")
        return {
            "status": "degraded" if reasons else "ok",
            "ready": not reasons,
            "readiness_reasons": reasons,
            "last_fire": self.last_fire,
            "next_scheduled_fire": next_fire.isoformat() if next_fire else None,
            "config_hash": self.config.hash,
            "kill_switch": {
                "enabled": self.config.safety.kill_switch_enabled,
                "until": str(self.config.safety.kill_switch_until) if self.config.safety.kill_switch_until else None,
            },
            "uptime_seconds": time.monotonic() - self.started_monotonic,
            "clock": {"synchronized": sync, "detail": sync_detail},
            "active_page": self.coordinator.snapshot(),
            "endpoints": endpoints,
            "unhealthy_required_endpoints": unhealthy,
            "unknown_required_endpoints": unknown_required,
            "scheduler_running": scheduler_running,
            "monitor_running": monitor_running,
            "config_valid": True,
        }

    def transmit_event(self, event: BellEvent, config: BellConfig, schedule_name: str) -> object:
        zone = config.zone_map[event.zone]
        destinations = [
            config.destination_map[name].name
            for name in zone.destinations
            if config.destination_map[name].enabled
        ]
        started = datetime.now(ZoneInfo(config.settings.timezone))
        reports: list[DeliveryReport] = []
        try:
            raw = transcode(config.sounds_path / event.sound)
            main_duration = raw.stat().st_size / 8000.0
            pre_raw = transcode(config.sounds_path / event.pre_tone) if event.pre_tone else None
            pre_duration = pre_raw.stat().st_size / 8000.0 if pre_raw else 0.0
            if main_duration > config.settings.max_audio_seconds:
                raise PageDeliveryError(
                    f"sound duration {main_duration:.2f}s exceeds max_audio_seconds "
                    f"{config.settings.max_audio_seconds:.2f}s"
                )
            if pre_duration > config.settings.max_audio_seconds:
                raise PageDeliveryError(
                    f"pre-tone duration {pre_duration:.2f}s exceeds max_audio_seconds "
                    f"{config.settings.max_audio_seconds:.2f}s"
                )
            total_duration = (
                pre_duration
                + main_duration * event.repeat_count
                + event.repeat_interval_seconds * max(0, event.repeat_count - 1)
            )
            if total_duration > config.settings.max_page_seconds:
                raise PageDeliveryError(
                    f"page duration {total_duration:.2f}s exceeds max_page_seconds "
                    f"{config.settings.max_page_seconds:.2f}s"
                )
            busy_policy = (
                "preempt"
                if event.priority >= config.safety.emergency_priority_threshold
                else event.busy_policy
            )
            with self.coordinator.lease(event.label, event.priority, busy_policy) as lease:
                if not lease.acquired:
                    raise PageDeliveryError(lease.reason)
                base_key = hashlib.sha256(
                    f"{started.isoformat()}|{schedule_name}|{event.label}|{event.zone}".encode()
                ).hexdigest()
                if event.pre_tone:
                    assert pre_raw is not None
                    reports.append(
                        self.delivery.deliver(
                            pre_raw,
                            event,
                            zone,
                            lease.cancel_event,
                            sound_name=event.pre_tone,
                            idempotency_key=f"{base_key}-pre",
                        )
                    )
                    if lease.cancel_event.wait(0.10):
                        raise PageDeliveryError("page was preempted after pre-tone")
                for repeat in range(event.repeat_count):
                    reports.append(
                        self.delivery.deliver(
                            raw,
                            event,
                            zone,
                            lease.cancel_event,
                            idempotency_key=f"{base_key}-{repeat + 1}",
                        )
                    )
                    if any(report.cancelled for report in reports):
                        raise PageDeliveryError("page was preempted")
                    if repeat + 1 < event.repeat_count and lease.cancel_event.wait(
                        event.repeat_interval_seconds
                    ):
                        raise PageDeliveryError("page was preempted between repeats")
            outcomes = [asdict(outcome) for report in reports for outcome in report.outcomes]
            self.last_fire = {
                "timestamp": started.isoformat(),
                "schedule_name": schedule_name,
                "event_label": event.label,
                "zone": zone.name,
                "channel": zone.channel,
                "destinations": destinations,
                "sound_file": event.sound,
                "priority": event.priority,
                "repeat_count": event.repeat_count,
                "delivery_outcomes": outcomes,
                "duration": sum(report.duration_seconds for report in reports),
                "result": "success",
            }
            LOGGER.info("bell_transmission", extra=self.last_fire)
            return reports
        except Exception as exc:
            self.last_fire = {
                "timestamp": started.isoformat(),
                "schedule_name": schedule_name,
                "event_label": event.label,
                "zone": zone.name,
                "channel": zone.channel,
                "destinations": destinations,
                "sound_file": event.sound,
                "priority": event.priority,
                "delivery_outcomes": [
                    asdict(outcome) for report in reports for outcome in report.outcomes
                ],
                "result": "failed",
                "detail": str(exc),
            }
            LOGGER.exception("bell_transmission", extra=self.last_fire)
            raise

    def _health_app(self) -> FastAPI:
        app = FastAPI(title="Bell System Health", docs_url=None, redoc_url=None)

        @app.get("/health")
        def health() -> dict[str, Any]:
            return self.health_data()

        @app.get("/ready")
        def ready() -> JSONResponse:
            health = self.health_data()
            payload = {
                "ready": health["ready"],
                "readiness_reasons": health["readiness_reasons"],
                "unhealthy_required_endpoints": health["unhealthy_required_endpoints"],
                "unknown_required_endpoints": health["unknown_required_endpoints"],
            }
            return JSONResponse(payload, status_code=200 if health["ready"] else 503)

        return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--health-port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_dir)
    except ConfigLoadError as exc:
        print(exc, file=sys.stderr)
        return 1
    configure_logging(config.log_path)
    errors = validate_startup(config)
    if errors:
        for error in errors:
            LOGGER.error("startup_validation_failed", extra={"detail": error})
        return 1
    LOGGER.info("startup_validation_passed", extra={"config_hash": config.hash})
    if args.check_only:
        return 0
    if not os.environ.get("BELL_UI_PASSWORD"):
        LOGGER.error(
            "startup_validation_failed",
            extra={"detail": "BELL_UI_PASSWORD is required for the LAN front-office UI"},
        )
        return 1
    if not os.environ.get("BELL_TLS_CERTFILE"):
        LOGGER.warning(
            "office_ui_plain_http",
            extra={
                "detail": "UI/API credentials are protected only by the trusted LAN; configure BELL_TLS_CERTFILE and BELL_TLS_KEYFILE for HTTPS"
            },
        )
    runtime = ServiceRuntime(config)
    runtime.scheduler.start()
    runtime.monitor.check_once()
    runtime.monitor.start()
    server = uvicorn.Server(
        uvicorn.Config(runtime.health_app, host="127.0.0.1", port=args.health_port, log_config=None)
    )
    server_thread = threading.Thread(target=server.run, name="health-server", daemon=True)
    server_thread.start()
    from bell.web import create_app

    office_app = create_app(
        config.config_dir,
        scheduler=runtime.scheduler,
        reload_callback=runtime.reload_config,
        health_provider=runtime.health_data,
    )
    office_server = uvicorn.Server(
        uvicorn.Config(
            office_app,
            host="0.0.0.0",
            port=8080,
            log_config=None,
            ssl_certfile=os.environ.get("BELL_TLS_CERTFILE"),
            ssl_keyfile=os.environ.get("BELL_TLS_KEYFILE"),
        )
    )
    office_thread = threading.Thread(target=office_server.run, name="office-server", daemon=True)
    office_thread.start()

    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOGGER.info("bell_service_started")
    while not stopping.wait(1.0):
        pass
    LOGGER.info("bell_service_stopping")
    runtime.scheduler.shutdown(wait=True)
    runtime.monitor.stop()
    server.should_exit = True
    office_server.should_exit = True
    server_thread.join(timeout=10)
    office_thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
