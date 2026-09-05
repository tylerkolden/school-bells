"""Versioned ownership and witnessed recovery records; never infer off-device success."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bell.config import BellConfig
from bell.recovery import RecoveryError, create_portable_backup, extract_and_validate_backup


class ContinuityPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    owner: str = Field(min_length=1, max_length=100)
    monitoring_host: str = Field(min_length=1, max_length=200)
    monitoring_owner: str = Field(min_length=1, max_length=100)
    escalation: str = Field(min_length=1, max_length=300)
    backup_destination: str = Field(min_length=1, max_length=300)
    backup_owner: str = Field(min_length=1, max_length=100)
    restore_interval_days: int = Field(default=90, ge=7, le=365)
    last_offdevice_copy: date | None = None
    last_restore: date | None = None
    restore_observer: str = Field(default="", max_length=100)
    restore_result: str = Field(default="not_tested", pattern=r"^(pass|fail|not_tested)$")
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def witnessed_restore(self) -> ContinuityPlan:
        if self.restore_result != "not_tested" and (not self.last_restore or not self.restore_observer):
            raise ValueError("A restore result requires a date and witness")
        return self


class ContinuityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS continuity (id INTEGER PRIMARY KEY, recorded TEXT, document TEXT)")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def record(self, plan: ContinuityPlan, now: datetime, expected_revision: int) -> None:
        if any(day and day > now.date() for day in (plan.last_offdevice_copy, plan.last_restore)):
            raise ValueError("Completed checks cannot have a future date")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT coalesce(max(id),0) FROM continuity").fetchone()[0]
            if revision != expected_revision:
                raise ValueError("Ownership record changed; reload before saving")
            db.execute("INSERT INTO continuity(recorded,document) VALUES(?,?)", (now.isoformat(), plan.model_dump_json()))

    def snapshot(self, today: date) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT id,recorded,document FROM continuity ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            return {"revision": 0, "plan": None, "issues": ["Assign continuity owners and record the first restore drill"], "history": []}
        plan = ContinuityPlan.model_validate_json(rows[0][2])
        issues = []
        if not plan.last_offdevice_copy or today - plan.last_offdevice_copy > timedelta(days=7):
            issues.append("Off-device backup receipt is missing or more than 7 days old")
        if plan.restore_result != "pass" or not plan.last_restore or today - plan.last_restore >= timedelta(days=plan.restore_interval_days):
            issues.append("Witnessed restore drill is due or has not passed")
        due = plan.last_restore + timedelta(days=plan.restore_interval_days) if plan.last_restore else today
        return {"revision": rows[0][0], "plan": plan, "issues": issues, "next_drill": due,
                "history": [{"recorded": row[1], "plan": ContinuityPlan.model_validate_json(row[2])} for row in rows]}


def _verified_copy(source: Path, target: Path) -> None:
    partial = target.with_name(target.name + ".partial")
    try:
        with source.open("rb") as original, partial.open("xb") as output:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        with source.open("rb") as original, partial.open("rb") as copied:
            if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(copied, "sha256").digest():
                raise RecoveryError("Off-device copy checksum mismatch")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def _record_archive(config: BellConfig, stage: Path) -> Path:
    archive = stage / "records.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name in ("receiver-acceptance.sqlite3", "continuity.sqlite3"):
            source = config.state_path / name
            if source.is_symlink():
                raise RecoveryError("Refusing symbolic link in recovery record source")
            if not source.is_file():
                continue
            snapshot = stage / name
            # SQLite backup API produces a consistent snapshot even during an office edit.
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(snapshot)) as copied:
                original.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RecoveryError("Recovery record database did not validate")
            output.write(snapshot, arcname=name)
    return archive


def copy_to_backup_volume(config: BellConfig, destination: Path) -> Path:
    """Verified copy to an explicitly mounted volume. Never create a missing mount point."""
    destination = destination.resolve(strict=True)
    if not destination.is_dir() or not os.path.ismount(destination):
        raise RecoveryError("Backup destination must be an existing mounted volume")
    if destination == Path(destination.anchor) or destination == config.config_dir.parent.resolve():
        raise RecoveryError("Refusing root or appliance directory as off-device backup destination")
    # Staging/extraction validates the archive before replacing any destination file.
    with tempfile.TemporaryDirectory(prefix="bell-offdevice-") as temporary:
        stage = Path(temporary)
        archive = create_portable_backup(config, stage / "archives")
        extract_and_validate_backup(archive, stage / "verified")
        target = destination / archive.name
        records = _record_archive(config, stage)
        records_target = destination / (archive.name + ".records.zip")
        _verified_copy(records, records_target)
        try:
            _verified_copy(archive, target)
        except (OSError, RecoveryError):
            records_target.unlink(missing_ok=True)
            raise
    # Only our well-formed archive names; no recursion, links, or arbitrary directory cleanup.
    copies = sorted(item for item in destination.glob("school-bell-backup-*.tar.gz")
                    if item.is_file() and not item.is_symlink())
    for old in copies[:-10]:
        old.unlink()
        sidecar = old.with_name(old.name + ".records.zip")
        if sidecar.is_file() and not sidecar.is_symlink():
            sidecar.unlink()
    return target


def main() -> int:
    import argparse

    from bell.config import load_config
    parser = argparse.ArgumentParser(description="Create and verify a backup on an existing off-device mount")
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    target = copy_to_backup_volume(load_config(args.config_dir), args.destination)
    parser.exit(0, json.dumps({"archive": str(target), "verified": True}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
