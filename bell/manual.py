"""Durable, single-use manual actions, independent of scheduler minute keys."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path


def sound_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class ManualActions:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS manual_actions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL)"""
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def claim(self, action_id: str, now: datetime) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO manual_actions VALUES (?, ?, 'processing', ?)",
                (action_id, now.isoformat(),
                 "This action was already claimed. It may be running or interrupted; "
                 "check History and receivers before preparing another page."),
            )
        return cursor.rowcount == 1

    def result(self, action_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT detail FROM manual_actions WHERE id=?", (action_id,)
            ).fetchone()
        return str(row[0]) if row else "Action result unavailable; inspect History."

    def finish(self, action_id: str, detail: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE manual_actions SET status='finished', detail=? WHERE id=?",
                (detail, action_id),
            )
