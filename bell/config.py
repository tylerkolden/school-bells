"""Validated YAML configuration model and validation CLI."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigLoadError(RuntimeError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("Configuration is invalid:\n- " + "\n- ".join(self.errors))


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    protocol: Literal["multicast", "sip", "http"] = "multicast"
    group: str | None = None
    port: int = Field(ge=1, le=65535)
    ttl: int = Field(default=1, ge=0, le=255)
    wire_format: Literal["plain_rtp", "poly_group_page"] | None = None
    codecs: list[Literal["pcmu", "pcma", "g722"]] = Field(
        default_factory=lambda: ["pcmu"], min_length=1, max_length=3
    )
    sip_uri: str | None = None
    sip_host: str | None = None
    sip_transport: Literal["udp", "tcp", "tls"] = "udp"
    sip_username: str | None = None
    sip_password_env: str | None = None
    tls_server_name: str | None = None
    tls_ca_file: Path | None = None
    webhook_url: str | None = None
    webhook_secret_env: str | None = None
    allow_insecure_http: bool = False
    healthcheck_url: str | None = None
    timeout_seconds: float = Field(default=5.0, ge=0.25, le=30.0)
    retries: int = Field(default=2, ge=0, le=5)
    required: bool = True
    enabled: bool = True

    @field_validator(
        "name",
        "sip_uri",
        "sip_host",
        "sip_username",
        "sip_password_env",
        "tls_server_name",
        "webhook_url",
        "webhook_secret_env",
        "healthcheck_url",
    )
    @classmethod
    def no_header_injection(cls, value: str | None) -> str | None:
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("must not contain carriage returns or line feeds")
        return value

    @field_validator("group")
    @classmethod
    def multicast_ipv4(cls, value: str | None) -> str | None:
        if value is None:
            return value
        address = ipaddress.ip_address(value)
        if address.version != 4 or not address.is_multicast:
            raise ValueError("must be a multicast IPv4 address in 224.0.0.0/4")
        return value

    @field_validator("codecs")
    @classmethod
    def codecs_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("codecs must not contain duplicates")
        return value

    @model_validator(mode="after")
    def protocol_fields(self) -> Destination:
        if self.protocol == "multicast" and self.group is None:
            raise ValueError("multicast destinations require group")
        if self.protocol == "multicast" and len(self.codecs) != 1:
            raise ValueError("multicast destinations require exactly one codec")
        if self.wire_format == "poly_group_page" and self.codecs != ["pcmu"]:
            raise ValueError("Poly Group Page requires PCMU codec")
        if self.protocol == "sip":
            if not self.sip_uri or not self.sip_uri.lower().startswith(("sip:", "sips:")):
                raise ValueError("SIP destinations require a sip: or sips: sip_uri")
            if not self.sip_host:
                raise ValueError("SIP destinations require sip_host")
            if self.sip_transport == "tls" and not self.tls_server_name:
                raise ValueError("TLS SIP destinations require tls_server_name")
            if self.sip_uri and self.sip_uri.lower().startswith("sips:") and self.sip_transport != "tls":
                raise ValueError("sips: destinations require sip_transport: tls")
            if self.sip_uri:
                userinfo = self.sip_uri.split(":", 1)[1].split("@", 1)[0]
                if ":" in userinfo:
                    raise ValueError("SIP passwords must not be embedded in sip_uri")
            if bool(self.sip_username) != bool(self.sip_password_env):
                raise ValueError("sip_username and sip_password_env must be configured together")
        if self.protocol == "http" and (
            not self.webhook_url
            or not self.webhook_url.lower().startswith(("http://", "https://"))
        ):
            raise ValueError("HTTP destinations require an http:// or https:// webhook_url")
        if self.protocol == "http" and self.webhook_url:
            parsed = urlsplit(self.webhook_url)
            if parsed.username or parsed.password:
                raise ValueError("HTTP credentials must not be embedded in webhook_url")
            if parsed.scheme == "http" and not self.allow_insecure_http:
                raise ValueError("plain HTTP requires allow_insecure_http: true")
        for environment_name in (self.sip_password_env, self.webhook_secret_env):
            if environment_name and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", environment_name):
                raise ValueError(
                    f"secret environment variable name {environment_name!r} is not a safe uppercase name"
                )
        return self


class Zone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    channel: int = Field(ge=0, le=25)
    destinations: list[str]
    description: str


class BellEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: time
    sound: str
    zone: str
    label: str
    pre_tone: str | None = None
    repeat_count: int = Field(default=1, ge=1, le=10)
    repeat_interval_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    priority: int = Field(default=50, ge=0, le=100)
    busy_policy: Literal["skip", "queue", "preempt"] = "skip"

    @field_validator("sound", "pre_tone")
    @classmethod
    def sound_is_library_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("must be a filename in the configured sound library")
        return value


class BellSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    events: list[BellEvent]

    @model_validator(mode="after")
    def unique_times(self) -> BellSchedule:
        seen: set[time] = set()
        duplicates: list[str] = []
        for event in self.events:
            if event.time in seen:
                duplicates.append(event.time.strftime("%H:%M"))
            seen.add(event.time)
        if duplicates:
            raise ValueError(f"duplicate event times: {', '.join(sorted(set(duplicates)))}")
        return self


class StandingItem(BellEvent):
    enabled: bool = True


class DateRangeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    schedule: str

    @model_validator(mode="after")
    def ordered(self) -> DateRangeRule:
        if self.end < self.start:
            raise ValueError("range end precedes range start")
        return self


class CalendarRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday_defaults: dict[str, str | None]
    overrides: dict[date, str] = Field(default_factory=dict)
    date_ranges: list[DateRangeRule] = Field(default_factory=list)
    no_bell_dates: dict[date, str] = Field(default_factory=dict)

    @field_validator("weekday_defaults")
    @classmethod
    def weekdays_valid(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        valid = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
        unknown = set(value) - valid
        if unknown:
            raise ValueError(f"unknown weekdays: {', '.join(sorted(unknown))}")
        return value


class Safety(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_hours_start: time
    allowed_hours_end: time
    max_events_per_day: int = Field(ge=1, le=100)
    max_repeats: int = Field(default=4, ge=1, le=10)
    emergency_priority_threshold: int = Field(default=90, ge=1, le=100)
    kill_switch_enabled: bool = False
    kill_switch_until: date | None = None


class PolyMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: int = Field(ge=0, le=262139)
    source: Literal["channel"] | int

    @field_validator("source")
    @classmethod
    def source_fits_byte(cls, value: str | int) -> str | int:
        if isinstance(value, int) and not 0 <= value <= 0xFF:
            raise ValueError("constant source must fit in one byte")
        return value


class PolyCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extension_profile_id: int = Field(ge=0, le=0xFFFF)
    extension_word_count: int = Field(ge=1, le=0xFFFF)
    mappings: list[PolyMapping]
    captured_channels: list[int] = Field(min_length=3)
    capture_sha256: list[str] = Field(min_length=3)
    captured_at: datetime
    evidence_id: str = Field(pattern=r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}$")

    @model_validator(mode="after")
    def complete_proven_layout(self) -> PolyCalibration:
        extension_size = self.extension_word_count * 4
        offsets = [item.offset for item in self.mappings]
        if sorted(offsets) != list(range(extension_size)):
            raise ValueError("mappings must cover every extension byte exactly once")
        if sum(item.source == "channel" for item in self.mappings) != 1:
            raise ValueError("mappings must contain exactly one channel byte")
        if len(self.captured_channels) != len(set(self.captured_channels)):
            raise ValueError("captured_channels must be unique")
        if any(not 1 <= channel <= 25 for channel in self.captured_channels):
            raise ValueError("captured_channels must be between 1 and 25")
        if len(self.capture_sha256) != len(self.captured_channels) or any(
            not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in self.capture_sha256
        ):
            raise ValueError("capture_sha256 must contain one lowercase SHA-256 per channel")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = "America/Denver"
    interface_ip: str
    wire_format: str = "poly_group_page"
    rtc_required: bool = False
    endpoint_check_interval_seconds: int = Field(default=60, ge=15, le=3600)
    api_rate_limit_per_minute: int = Field(default=10, ge=1, le=120)
    clock_sync_required: bool = True
    max_audio_seconds: float = Field(default=30.0, ge=0.25, le=600.0)
    max_page_seconds: float = Field(default=120.0, ge=1.0, le=1800.0)
    poly_group_page_calibration: PolyCalibration | None = None
    sounds_dir: Path = Path("sounds")
    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @field_validator("interface_ip")
    @classmethod
    def interface_is_ipv4(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if address.version != 4:
            raise ValueError("must be an IPv4 address")
        return value

    @field_validator("wire_format")
    @classmethod
    def wire_format_known(cls, value: str) -> str:
        if value not in {"plain_rtp", "poly_group_page"}:
            raise ValueError("must be plain_rtp or poly_group_page")
        return value


class BellConfig(BaseModel):
    settings: Settings
    safety: Safety
    destinations: list[Destination]
    zones: list[Zone]
    schedules: list[BellSchedule]
    standing_items: list[StandingItem]
    calendar: CalendarRule
    config_dir: Path = Field(exclude=True)

    @property
    def destination_map(self) -> dict[str, Destination]:
        return {item.name: item for item in self.destinations}

    @property
    def zone_map(self) -> dict[str, Zone]:
        return {item.name: item for item in self.zones}

    @property
    def schedule_map(self) -> dict[str, BellSchedule]:
        return {item.name: item for item in self.schedules}

    @property
    def sounds_path(self) -> Path:
        return _resolve_path(self.settings.sounds_dir, self.config_dir.parent)

    @property
    def poly_spec(self):
        """Return the runtime wire spec proven by persisted capture evidence, if any."""
        calibration = self.settings.poly_group_page_calibration
        if calibration is None:
            return None
        from bell.wire.poly_group_page import PolySpec

        return PolySpec(
            calibration.extension_profile_id,
            calibration.extension_word_count,
            tuple((item.offset, item.source) for item in calibration.mappings),
        )

    @property
    def state_path(self) -> Path:
        return _resolve_path(self.settings.state_dir, self.config_dir.parent)

    @property
    def log_path(self) -> Path:
        return _resolve_path(self.settings.log_dir, self.config_dir.parent)

    @property
    def hash(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.config_dir.glob("*.yaml")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        if self.sounds_path.is_dir():
            for path in sorted(item for item in self.sounds_path.iterdir() if item.is_file()):
                digest.update(f"sounds/{path.name}".encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()


def _resolve_path(path: Path, base: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()


def _read_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigLoadError([f"{path.name}: {exc}"]) from exc


def _format_validation(prefix: str, exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        errors.append(f"{prefix}{'.' + location if location else ''}: {item['msg']}")
    return errors


def load_config(config_dir: Path | str = Path("config")) -> BellConfig:
    directory = Path(config_dir).expanduser().resolve()
    errors: list[str] = []
    required = ("settings.yaml", "destinations.yaml", "zones.yaml", "schedules.yaml", "calendar.yaml")
    raw: dict[str, Any] = {}
    for filename in required:
        path = directory / filename
        if not path.is_file():
            errors.append(f"missing required file: {filename}")
            continue
        try:
            raw[filename] = _read_yaml(path)
        except ConfigLoadError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigLoadError(errors)
    settings_doc = raw["settings.yaml"]
    payload = {
        "settings": settings_doc.get("settings", {}),
        "safety": settings_doc.get("safety", {}),
        "destinations": raw["destinations.yaml"].get("destinations", []),
        "zones": raw["zones.yaml"].get("zones", []),
        "schedules": raw["schedules.yaml"].get("schedules", []),
        "standing_items": raw["schedules.yaml"].get("standing_items", []),
        "calendar": raw["calendar.yaml"].get("calendar", {}),
        "config_dir": directory,
    }
    try:
        config = BellConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigLoadError(_format_validation("config", exc)) from exc

    for destination in config.destinations:
        if destination.tls_ca_file:
            destination.tls_ca_file = _resolve_path(destination.tls_ca_file, directory)
            if not destination.tls_ca_file.is_file():
                errors.append(
                    f"destination {destination.name!r}: TLS CA file does not exist: {destination.tls_ca_file}"
                )
        effective_wire = destination.wire_format or config.settings.wire_format
        if (
            destination.protocol == "multicast"
            and effective_wire == "poly_group_page"
            and destination.codecs != ["pcmu"]
        ):
            errors.append(
                f"destination {destination.name!r}: Poly Group Page requires PCMU codec"
            )
    destinations = config.destination_map
    zones = config.zone_map
    schedules = config.schedule_map
    duplicate_groups = (
        ("destination", [item.name for item in config.destinations]),
        ("zone", [item.name for item in config.zones]),
        ("schedule", [item.name for item in config.schedules]),
    )
    for label, names in duplicate_groups:
        for name in sorted({name for name in names if names.count(name) > 1}):
            errors.append(f"duplicate {label} name: {name}")
    for zone in config.zones:
        if not zone.destinations:
            errors.append(f"zone {zone.name!r} has no destinations")
        for destination in zone.destinations:
            if destination not in destinations:
                errors.append(f"zone {zone.name!r} references unknown destination {destination!r}")
        known = [destinations[name] for name in zone.destinations if name in destinations]
        if known and not any(item.enabled for item in known):
            errors.append(f"zone {zone.name!r} has no enabled destinations")
    all_events: list[tuple[str, BellEvent]] = []
    for schedule in config.schedules:
        all_events.extend((f"schedule {schedule.name!r}", event) for event in schedule.events)
    all_events.extend(("standing item", event) for event in config.standing_items if event.enabled)
    for context, event in all_events:
        if event.zone not in zones:
            errors.append(f"{context}, event {event.label!r}: unknown zone {event.zone!r}")
        sound = config.sounds_path / event.sound
        if not sound.is_file() or not os.access(sound, os.R_OK):
            errors.append(f"{context}, event {event.label!r}: sound is missing or unreadable: {sound}")
        if event.pre_tone:
            pre_tone = config.sounds_path / event.pre_tone
            if not pre_tone.is_file() or not os.access(pre_tone, os.R_OK):
                errors.append(
                    f"{context}, event {event.label!r}: pre-tone is missing or unreadable: {pre_tone}"
                )
        if event.repeat_count > config.safety.max_repeats:
            errors.append(
                f"{context}, event {event.label!r}: repeat_count {event.repeat_count} exceeds "
                f"safety max_repeats {config.safety.max_repeats}"
            )
        if not _within_window(event.time, config.safety.allowed_hours_start, config.safety.allowed_hours_end):
            errors.append(
                f"{context}, event {event.label!r} at {event.time:%H:%M} is outside safety window "
                f"{config.safety.allowed_hours_start:%H:%M}-{config.safety.allowed_hours_end:%H:%M}"
            )
    referenced = [name for name in config.calendar.weekday_defaults.values() if name]
    referenced.extend(config.calendar.overrides.values())
    referenced.extend(item.schedule for item in config.calendar.date_ranges)
    for name in sorted(set(referenced)):
        if name not in schedules:
            errors.append(f"calendar references unknown schedule {name!r}")
    if errors:
        raise ConfigLoadError(errors)
    return config


def _within_window(value: time, start: time, end: time) -> bool:
    return start <= value <= end if start <= end else value >= start or value <= end


def validation_table(config: BellConfig) -> str:
    lines = [
        f"Configuration: {config.config_dir}",
        f"Timezone:      {config.settings.timezone}",
        f"Interface:     {config.settings.interface_ip}",
        f"Wire format:   {config.settings.wire_format}",
        "",
        "Zones:",
    ]
    for zone in config.zones:
        lines.append(f"  {zone.name:<12} channel {zone.channel:<2}  {zone.description}")
    lines.append("")
    for schedule in config.schedules:
        lines.append(f"{schedule.name}: {len(schedule.events)} scheduled events")
    lines.append(f"Standing items: {sum(item.enabled for item in config.standing_items)} enabled")
    lines.append("VALID")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--config-dir", type=Path, default=Path("config"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_dir)
    except ConfigLoadError as exc:
        print(exc)
        return 1
    print(validation_table(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
