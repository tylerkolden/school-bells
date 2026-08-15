from __future__ import annotations

import os
import sqlite3
import tarfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bell.backup import create_backup


def test_deployment_backup_contains_consistent_sqlite_and_config(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    (app / "state").mkdir()
    (app / "config" / "settings.yaml").write_text("settings: {}\n", encoding="utf-8")
    database = app / "state" / "fires.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE events (name TEXT)")
        connection.execute("INSERT INTO events VALUES ('bell')")
        connection.commit()
    archive = create_backup(
        app, tmp_path / "backups", now=datetime(2026, 8, 15, tzinfo=UTC)
    )
    assert archive
    if os.name != "nt":
        assert archive.stat().st_mode & 0o777 == 0o600
    restored = tmp_path / "restored"
    with tarfile.open(archive) as source:
        source.extractall(restored, filter="data")
    assert (restored / "config" / "settings.yaml").is_file()
    with closing(sqlite3.connect(restored / "state" / "fires.sqlite3")) as connection:
        assert connection.execute("SELECT name FROM events").fetchone() == ("bell",)


def test_deployment_backup_rejects_symlinks(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    target = tmp_path / "secret"
    target.write_text("secret", encoding="utf-8")
    try:
        (app / "config" / "unsafe").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(RuntimeError, match="symbolic link"):
        create_backup(app, tmp_path / "backups")
