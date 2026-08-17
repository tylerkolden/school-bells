"""Portable operator backups, validated restore, and redacted support bundles."""

from __future__ import annotations

import json
import platform
import re
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bell import __version__
from bell.config import BellConfig, load_config

CONFIG_FILES = {
    "calendar.yaml",
    "destinations.yaml",
    "schedules.yaml",
    "settings.yaml",
    "zones.yaml",
}
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 150 * 1024 * 1024
MAX_MEMBERS = 500


class RecoveryError(RuntimeError):
    pass


def _regular_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for item in sorted(directory.iterdir()):
        if item.is_symlink():
            raise RecoveryError(f"Refusing symbolic link in backup source: {item}")
        if item.is_file():
            files.append(item)
        elif item.is_dir():
            raise RecoveryError(f"Unexpected nested directory in backup source: {item}")
    return files


def _prune(directory: Path, pattern: str, keep: int = 10) -> None:
    items = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime)
    for item in items[:-keep]:
        item.unlink(missing_ok=True)


def create_portable_backup(
    config: BellConfig,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> Path:
    current = now or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"school-bell-backup-{current:%Y%m%dT%H%M%S%fZ}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="bell-portable-") as temporary:
        stage = Path(temporary)
        config_stage = stage / "config"
        sounds_stage = stage / "sounds"
        config_stage.mkdir()
        sounds_stage.mkdir()
        for name in sorted(CONFIG_FILES):
            shutil.copy2(config.config_dir / name, config_stage / name)
        for sound in _regular_files(config.sounds_path):
            if sound.suffix.lower() == ".wav":
                shutil.copy2(sound, sounds_stage / sound.name)
        if config.logo_path and config.logo_path.is_file():
            logo = stage / "state" / "branding" / "logo.png"
            logo.parent.mkdir(parents=True)
            shutil.copy2(config.logo_path, logo)
        manifest = {
            "schema": 1,
            "product": "bell-system",
            "version": __version__,
            "created_at": current.isoformat(),
            "school_name": config.settings.school_name,
            "config_hash": config.hash,
            "includes_credentials": False,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        with tarfile.open(archive, "w:gz") as output:
            for name in ("manifest.json", "config", "sounds", "state"):
                path = stage / name
                if path.exists():
                    output.add(path, arcname=name, recursive=True)
    archive.chmod(0o600)
    _prune(output_dir, "school-bell-backup-*.tar.gz")
    return archive


def _allowed_member(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    if name == "manifest.json" or name in {"config", "sounds", "state", "state/branding"}:
        return True
    if len(path.parts) == 2 and path.parts[0] == "config":
        return path.parts[1] in CONFIG_FILES
    if len(path.parts) == 2 and path.parts[0] == "sounds":
        return path.suffix.lower() == ".wav"
    return name == "state/branding/logo.png"


def extract_and_validate_backup(archive: Path, destination: Path) -> BellConfig:
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RecoveryError("Backup is missing or exceeds the 50 MiB archive limit")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if len(members) > MAX_MEMBERS:
                raise RecoveryError("Backup contains too many files")
            expanded = 0
            seen: set[str] = set()
            for member in members:
                if not _allowed_member(member.name):
                    raise RecoveryError(f"Backup contains an unexpected path: {member.name}")
                if member.name in seen:
                    raise RecoveryError(f"Backup contains a duplicate path: {member.name}")
                seen.add(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise RecoveryError("Backup links and device files are not allowed")
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES:
                    raise RecoveryError("Expanded backup exceeds the 150 MiB safety limit")
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RecoveryError(f"Unsupported backup member: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = source.extractfile(member)
                if extracted is None:
                    raise RecoveryError(f"Backup member is unreadable: {member.name}")
                with target.open("wb") as output:
                    shutil.copyfileobj(extracted, output, length=1024 * 1024)
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryError(f"Backup archive is unreadable: {exc}") from exc
    manifest_path = destination / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("Backup manifest is missing or invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 1
        or manifest.get("product") != "bell-system"
    ):
        raise RecoveryError("Backup manifest is not compatible with this appliance")
    missing = sorted(CONFIG_FILES - {path.name for path in (destination / "config").glob("*.yaml")})
    if missing:
        raise RecoveryError("Backup is missing configuration files: " + ", ".join(missing))
    try:
        return load_config(destination / "config")
    except Exception as exc:
        raise RecoveryError(f"Backup configuration did not validate: {exc}") from exc


def _replace_library(source: Path, target: Path, suffix: str) -> None:
    target.mkdir(parents=True, exist_ok=True)
    desired = {item.name for item in source.iterdir() if item.is_file() and item.suffix == suffix}
    for existing in target.iterdir():
        if existing.is_file() and existing.suffix == suffix and existing.name not in desired:
            existing.unlink()
    for item in source.iterdir():
        if item.is_file() and item.suffix == suffix:
            temporary = target / f".{item.name}.restore"
            shutil.copy2(item, temporary)
            temporary.replace(target / item.name)


def restore_portable_backup(
    archive: Path,
    app_dir: Path,
    backup_dir: Path,
    *,
    reload_callback: Callable[[], None] | None = None,
) -> Path:
    current = load_config(app_dir / "config")
    pre_restore = create_portable_backup(current, backup_dir)
    with (
        tempfile.TemporaryDirectory(prefix="bell-restore-") as extracted_temp,
        tempfile.TemporaryDirectory(prefix="bell-rollback-") as rollback_temp,
    ):
        extracted = Path(extracted_temp)
        rollback = Path(rollback_temp)
        extract_and_validate_backup(archive, extracted)
        shutil.copytree(current.config_dir, rollback / "config")
        shutil.copytree(current.sounds_path, rollback / "sounds")
        if current.logo_path and current.logo_path.is_file():
            old_logo = rollback / "state" / "branding" / "logo.png"
            old_logo.parent.mkdir(parents=True)
            shutil.copy2(current.logo_path, old_logo)
        try:
            for name in CONFIG_FILES:
                temporary = current.config_dir / f".{name}.restore"
                shutil.copy2(extracted / "config" / name, temporary)
                temporary.replace(current.config_dir / name)
            _replace_library(extracted / "sounds", current.sounds_path, ".wav")
            target_branding = current.state_path / "branding"
            restored_logo = extracted / "state" / "branding" / "logo.png"
            if restored_logo.is_file():
                target_branding.mkdir(parents=True, exist_ok=True)
                shutil.copy2(restored_logo, target_branding / "logo.png")
            elif target_branding.is_dir():
                (target_branding / "logo.png").unlink(missing_ok=True)
            load_config(current.config_dir)
            if reload_callback:
                reload_callback()
        except Exception as exc:
            for name in CONFIG_FILES:
                shutil.copy2(rollback / "config" / name, current.config_dir / name)
            _replace_library(rollback / "sounds", current.sounds_path, ".wav")
            old_logo = rollback / "state" / "branding" / "logo.png"
            branding = current.state_path / "branding"
            if old_logo.is_file():
                branding.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_logo, branding / "logo.png")
            elif branding.is_dir():
                (branding / "logo.png").unlink(missing_ok=True)
            if reload_callback:
                reload_callback()
            raise RecoveryError("Restore failed; the previous configuration was restored") from exc
    return pre_restore


_SECRET_PATTERN = re.compile(
    r"(?i)(password|secret|token|api[_-]?key)([\"']?\s*[:=]\s*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,\s}\r\n]+)"
)


def redact_text(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)


def _redact_object(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(r"(?i)(password|secret|token|api[_-]?key)", str(key))
                else _redact_object(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_object(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def create_support_bundle(
    config: BellConfig,
    output_dir: Path,
    *,
    health: dict[str, Any] | None = None,
    recent: list[dict[str, str]] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    archive = output_dir / f"school-bell-support-{now:%Y%m%dT%H%M%S%fZ}.zip"
    disk = shutil.disk_usage(config.config_dir.parent)
    summary = {
        "schema": 1,
        "created_at": now.isoformat(),
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config_hash": config.hash,
        "school_name": config.settings.school_name,
        "interface_ip": config.settings.interface_ip,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "health": health,
        "recent_fire_attempts": recent or [],
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "support.json",
            json.dumps(_redact_object(summary), sort_keys=True, indent=2, default=str),
        )
        for name in sorted(CONFIG_FILES):
            output.writestr(
                f"config/{name}",
                redact_text((config.config_dir / name).read_text(encoding="utf-8")),
            )
        if config.log_path.is_dir():
            logs = sorted(
                (item for item in config.log_path.iterdir() if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )[-5:]
            for log in logs:
                with log.open("rb") as handle:
                    size = log.stat().st_size
                    if size > 1024 * 1024:
                        handle.seek(size - 1024 * 1024)
                    text = handle.read().decode("utf-8", errors="replace")
                output.writestr(f"logs/{log.name}.tail.txt", redact_text(text))
    archive.chmod(0o600)
    _prune(output_dir, "school-bell-support-*.zip")
    return archive
