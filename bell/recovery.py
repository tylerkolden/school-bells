"""Portable operator backups, validated restore, and redacted support bundles."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ruamel.yaml import YAML

from bell import __version__
from bell.config import BellConfig, ConfigLoadError, load_config

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
    files = []
    for item in sorted(directory.rglob("*")):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise RecoveryError(f"Refusing link or special file in backup source: {item}")
        if item.is_file():
            files.append(item)
    return files


def _prune(directory: Path, pattern: str, keep: int = 10) -> None:
    items = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime)
    for item in items[:-keep]:
        item.unlink(missing_ok=True)


def _hashes(directory: Path) -> dict[str, str]:
    result = {}
    for path in _regular_files(directory):
        if path.relative_to(directory).as_posix() == "manifest.json":
            continue
        with path.open("rb") as handle:
            result[path.relative_to(directory).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def create_portable_backup(config: BellConfig, output_dir: Path, *, now: datetime | None = None) -> Path:
    current = now or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"school-bell-backup-{current:%Y%m%dT%H%M%S%fZ}.tar.gz"
    partial = archive.with_suffix(".partial")
    try:
        with tempfile.TemporaryDirectory(prefix="bell-portable-") as temporary:
            stage = Path(temporary) / "contents"
            (stage / "config").mkdir(parents=True)
            (stage / "sounds").mkdir()
            for name in sorted(CONFIG_FILES):
                source = config.config_dir / name
                if source.is_symlink():
                    raise RecoveryError("Configuration links cannot be exported")
                shutil.copy2(source, stage / "config" / name)
            ca_paths = {}
            for destination in config.destinations:
                if destination.tls_ca_file:
                    try:
                        relative = destination.tls_ca_file.relative_to(config.config_dir)
                    except ValueError as exc:
                        raise RecoveryError("External TLS CA files require a deployment checkpoint, not portable recovery") from exc
                    if relative.suffix.lower() not in {".pem", ".crt", ".cer"} or destination.tls_ca_file.is_symlink():
                        raise RecoveryError("Portable TLS CA must be a regular PEM/CRT/CER file inside config")
                    target = stage / "config" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination.tls_ca_file, target)
                    ca_paths[destination.name] = relative.as_posix()
            if ca_paths:
                yaml = YAML()
                yaml.preserve_quotes = True
                path = stage / "config/destinations.yaml"
                document = yaml.load(path.read_text(encoding="utf-8"))
                for entry in document.get("destinations", []):
                    if entry["name"] in ca_paths:
                        entry["tls_ca_file"] = ca_paths[entry["name"]]
                with path.open("w", encoding="utf-8") as handle:
                    yaml.dump(document, handle)
            for sound in _regular_files(config.sounds_path):
                target = stage / "sounds" / sound.relative_to(config.sounds_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sound, target)
            if config.logo_path and config.logo_path.is_file():
                if config.logo_path.is_symlink():
                    raise RecoveryError("Branding links cannot be exported")
                logo = stage / "state/branding/logo.png"
                logo.parent.mkdir(parents=True)
                shutil.copy2(config.logo_path, logo)
            manifest = {"schema": 2, "product": "bell-system", "version": __version__,
                        "created_at": current.isoformat(), "school_name": config.settings.school_name,
                        "config_hash": config.hash, "excludes_authentication_store": True,
                        "sound_scope": "all_regular_files", "files": _hashes(stage)}
            (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            with tarfile.open(partial, "w:gz") as output:
                for name in ("manifest.json", "config", "sounds", "state"):
                    if (stage / name).exists():
                        output.add(stage / name, arcname=name)
            # Do not publish or prune an archive that this version cannot restore.
            extract_and_validate_backup(partial, Path(temporary) / "verified")
            partial.chmod(0o600)
            partial.replace(archive)
    finally:
        partial.unlink(missing_ok=True)
    _prune(output_dir, "school-bell-backup-*.tar.gz")
    return archive


def _allowed_member(name: str, *, directory: bool = False) -> bool:
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or PureWindowsPath(name).drive
            or any(PureWindowsPath(part).is_reserved() or part.endswith((".", " "))
                   or any(char in part for char in '<>"|?*') for part in path.parts)):

        return False
    if name == "manifest.json" or name in {"config", "sounds", "state", "state/branding"}:
        return True
    if path.parts[0] == "config":
        if "bell.env" in path.parts:
            return False
        return directory or name in {f"config/{item}" for item in CONFIG_FILES} or path.suffix.lower() in {".pem", ".crt", ".cer"}
    return path.parts[0] == "sounds" or name == "state/branding/logo.png"


def extract_and_validate_backup(archive: Path, destination: Path) -> BellConfig:
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RecoveryError("Backup is missing or exceeds the 50 MiB archive limit")
    if destination.exists() and (destination.is_symlink() or any(destination.iterdir())):
        raise RecoveryError("Backup extraction requires an empty, non-symlink directory")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if len(members) > MAX_MEMBERS:
                raise RecoveryError("Backup contains too many files")
            expanded = 0
            seen: set[str] = set()
            for member in members:
                if not _allowed_member(member.name, directory=member.isdir()):
                    raise RecoveryError(f"Backup contains an unexpected path: {member.name}")
                canonical = member.name.casefold()
                if canonical in seen:
                    raise RecoveryError(f"Backup contains a duplicate or case-colliding path: {member.name}")
                seen.add(canonical)
                if not (member.isfile() or member.isdir()):
                    raise RecoveryError("Backup links and special files are not allowed")
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES:
                    raise RecoveryError("Expanded backup exceeds the 150 MiB safety limit")
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise RecoveryError("Backup member is unreadable")
                    with extracted, target.open("xb") as output:
                        shutil.copyfileobj(extracted, output, length=1024 * 1024)
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryError(f"Backup archive is unreadable: {exc}") from exc
    try:
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") not in {1, 2} or manifest.get("product") != "bell-system":
            raise RecoveryError("Backup manifest is not compatible with this appliance")
        if manifest["schema"] == 2 and (manifest.get("sound_scope") != "all_regular_files" or manifest.get("files") != _hashes(destination)):
            raise RecoveryError("Backup file inventory or checksum does not match")
        if manifest["schema"] == 1 and any(path.suffix.lower() != ".wav" for path in _regular_files(destination / "sounds")):
            raise RecoveryError("Legacy backup may contain only WAV audio")
        return load_config(destination / "config", portable=True)
    except (OSError, UnicodeDecodeError, ValueError, ConfigLoadError) as exc:
        raise RecoveryError(f"Backup configuration or manifest did not validate: {exc}") from exc


def _replace_library(source: Path, target: Path, *, legacy: bool = False) -> None:
    target.mkdir(parents=True, exist_ok=True)
    incoming = {item.relative_to(source) for item in _regular_files(source)}
    for existing in _regular_files(target):
        if existing.relative_to(target) not in incoming and (not legacy or existing.suffix.lower() == ".wav"):
            existing.unlink()
    for relative in incoming:
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".restore-", dir=output.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copy2(source / relative, temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


def restore_portable_backup(archive: Path, app_dir: Path, backup_dir: Path, *,
                            reload_callback: Callable[[], None] | None = None) -> Path:
    current = load_config(app_dir / "config")
    pending = current.state_path / ".restore-incomplete"
    if pending.exists():
        raise RecoveryError("An interrupted restore requires recovery from its recorded pre-restore backup")
    with tempfile.TemporaryDirectory(prefix="bell-restore-") as extracted_temp, tempfile.TemporaryDirectory(prefix="bell-rollback-") as rollback_temp:
        extracted, rollback = Path(extracted_temp), Path(rollback_temp)
        extract_and_validate_backup(archive, extracted)
        legacy = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))["schema"] == 1
        # A restore changes site content, never this appliance's storage locations.
        yaml = YAML()
        yaml.preserve_quotes = True
        settings_path = extracted / "config/settings.yaml"
        document = yaml.load(settings_path.read_text(encoding="utf-8"))
        for name in ("sounds_dir", "state_dir", "log_dir"):
            document["settings"][name] = str(getattr(current.settings, name))
        with settings_path.open("w", encoding="utf-8") as handle:
            yaml.dump(document, handle)
        pre_restore = create_portable_backup(current, backup_dir)
        _regular_files(current.config_dir)
        shutil.copytree(current.config_dir, rollback / "config")
        shutil.copytree(current.sounds_path, rollback / "sounds")
        branding = current.state_path / "branding"
        if branding.is_dir():
            _regular_files(branding)
            shutil.copytree(branding, rollback / "branding")
        pending.parent.mkdir(parents=True, exist_ok=True)
        with pending.open("x", encoding="utf-8") as handle:
            json.dump({"backup": str(pre_restore), "config": str(current.config_dir),
                       "sounds": str(current.sounds_path), "state": str(current.state_path)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            for item in _regular_files(extracted / "config"):
                relative = item.relative_to(extracted / "config")
                target = current.config_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
            _replace_library(extracted / "sounds", current.sounds_path, legacy=legacy)
            branding.mkdir(parents=True, exist_ok=True)
            logo = extracted / "state/branding/logo.png"
            if logo.exists():
                shutil.copy2(logo, branding / "logo.png")
            else:
                (branding / "logo.png").unlink(missing_ok=True)
            load_config(current.config_dir)
            if reload_callback:
                reload_callback()
            pending.unlink()
        except Exception as exc:
            _replace_library(rollback / "config", current.config_dir)
            _replace_library(rollback / "sounds", current.sounds_path)
            _replace_library(rollback / "branding", branding)
            if reload_callback:
                reload_callback()
            pending.unlink()
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
