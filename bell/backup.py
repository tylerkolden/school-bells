"""Consistent configuration and SQLite state backups for deployment upgrades."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_symlink():
            raise RuntimeError(f"refusing to back up symbolic link: {item}")
        destination = target / item.name
        if item.is_dir():
            _copy_tree(item, destination)
        elif item.is_file() and item.suffix in {".sqlite", ".sqlite3", ".db"}:
            with closing(sqlite3.connect(item)) as source_db, closing(
                sqlite3.connect(destination)
            ) as target_db:
                source_db.backup(target_db)
        elif item.is_file():
            shutil.copy2(item, destination)
        else:
            raise RuntimeError(f"refusing to back up non-regular file: {item}")


def create_backup(
    app_dir: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
    retention_days: int = 90,
) -> Path | None:
    config_dir = app_dir / "config"
    if not config_dir.is_dir():
        return None
    current = now or datetime.now(UTC)
    backup_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bell-backup-") as temporary:
        stage = Path(temporary)
        _copy_tree(config_dir, stage / "config")
        state_dir = app_dir / "state"
        if state_dir.is_dir():
            _copy_tree(state_dir, stage / "state")
        archive = backup_dir / f"deployment-{current:%Y%m%dT%H%M%S%fZ}.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(stage / "config", arcname="config")
            if (stage / "state").exists():
                output.add(stage / "state", arcname="state")
    archive.chmod(0o600)
    cutoff = current - timedelta(days=retention_days)
    for existing in backup_dir.glob("deployment-*.tar.gz"):
        modified = datetime.fromtimestamp(existing.stat().st_mtime, UTC)
        if existing != archive and modified < cutoff:
            existing.unlink()
    return archive


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/bell"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/var/backups/bell-system"))
    args = parser.parse_args(argv)
    archive = create_backup(args.app_dir, args.backup_dir)
    if archive:
        print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
