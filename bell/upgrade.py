"""Code-only upgrade checkpoint. Invoked from the trusted, root-owned release installer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


class PreservationError(RuntimeError):
    pass


def inventory(root: Path) -> dict[str, str]:
    result = {}
    if not root.is_dir():
        raise PreservationError(f"Missing site-data directory: {root}")
    for item in sorted(root.rglob("*")):
        if item.is_symlink() or not (item.is_dir() or item.is_file()):
            raise PreservationError(f"Unsupported link or special file: {item}")
        if item.is_file():
            with item.open("rb") as handle:
                result[item.relative_to(root).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def roots(app: Path) -> dict[str, Path]:
    from bell.config import load_config
    app = app.resolve()
    cfg = load_config(app / "config")
    selected = {"config": cfg.config_dir, "sounds": cfg.sounds_path, "state": cfg.state_path}
    for name, path in selected.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(app) or resolved == app or any(part in {"releases", "current"} for part in resolved.relative_to(app).parts):
            raise PreservationError(f"{name} storage requires a reviewed migration: {resolved}; no data changed")
        selected[name] = resolved
    for name, path in selected.items():
        if any(path == other or path.is_relative_to(other) for key, other in selected.items() if key != name):
            raise PreservationError("Overlapping site storage roots are not supported")
    return selected


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".partial")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    if os.name == "posix":
        for directory in (path.parent, path.parent.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def checkpoint(app: Path, destination: Path) -> None:
    selected = roots(app)
    if destination.exists():
        raise PreservationError("Checkpoint already exists; refusing overwrite")
    for path in selected.values():
        if destination.resolve().is_relative_to(path):
            raise PreservationError("Checkpoint must be outside site-data roots")
    before = {name: inventory(path) for name, path in selected.items()}
    required = sum(item.stat().st_size for root in selected.values() for item in root.rglob("*") if item.is_file())
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < required * 2 + 256 * 1024 * 1024:
        raise PreservationError("Insufficient space for verified checkpoint and rollback")
    destination.mkdir(mode=0o700)
    for name, source in selected.items():
        target = destination / name
        target.mkdir()
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            output = target / relative
            if item.is_symlink():
                raise PreservationError("Site data changed to a symlink during checkpoint")
            if item.is_dir():
                output.mkdir(exist_ok=True)
            elif item.is_file():
                with item.open("rb") as header:
                    sqlite_file = name == "state" and header.read(16) == b"SQLite format 3\x00"
                sqlite_sidecar = False
                if name == "state":
                    for suffix in ("-wal", "-shm", "-journal"):
                        base = item.with_name(item.name.removesuffix(suffix))
                        if item.name.endswith(suffix) and base.is_file():
                            with base.open("rb") as header:
                                sqlite_sidecar = header.read(16) == b"SQLite format 3\x00"
                            break
                if sqlite_sidecar:
                    # SQLite backup folds committed WAL content into the database.
                    continue
                if sqlite_file:
                    with closing(sqlite3.connect(item.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(output)) as copied:
                        original.backup(copied)
                        if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise PreservationError("SQLite checkpoint failed integrity check")
                    shutil.copystat(item, output)
                else:
                    shutil.copy2(item, output)
            else:
                raise PreservationError("Site data changed during checkpoint")
    after = {name: inventory(path) for name, path in selected.items()}
    if before != after:
        raise PreservationError("Site data changed during checkpoint; service must remain stopped")
    copied = {name: inventory(destination / name) for name in selected}
    metadata = {name: {item.relative_to(root).as_posix(): {"mode": item.stat().st_mode & 0o777,
                 "uid": item.stat().st_uid, "gid": item.stat().st_gid}
                 for item in [root, *root.rglob("*")]
                 if item.is_dir() or item.relative_to(root).as_posix() in copied[name]}
                for name, root in selected.items()}
    for name in ("config", "sounds"):
        if before[name] != copied[name]:
            raise PreservationError("Copied configuration or uploads failed checksum verification")
    for item in destination.rglob("*"):
        if item.is_file():
            with item.open("rb" if os.name == "posix" else "ab") as handle:
                os.fsync(handle.fileno())
        elif os.name == "posix":
            descriptor = os.open(item, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    write_json(destination / "checkpoint.json", {"schema": 1, "roots": {k: str(v) for k, v in selected.items()}, "files": copied, "metadata": metadata})


def verified_checkpoint(app: Path, destination: Path) -> dict:
    record = json.loads((destination / "checkpoint.json").read_text(encoding="utf-8"))
    if record.get("schema") != 1 or set(record.get("roots", {})) != {"config", "sounds", "state"}:
        raise PreservationError("Checkpoint schema is invalid")
    base = app.resolve()
    for name, raw in record["roots"].items():
        root = Path(raw)
        if root.resolve() != root or not root.is_relative_to(base) or root == base:
            raise PreservationError("Checkpoint target no longer matches its trusted location")
        if record["files"][name] != inventory(destination / name):
            raise PreservationError("Checkpoint checksum mismatch; refusing rollback")
    return record


def _sql_name(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def verify_database(before: Path, after: Path) -> None:
    try:
        with closing(sqlite3.connect(before.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(after.as_uri() + "?mode=ro", uri=True)) as candidate:
            for (table,) in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                name = _sql_name(table)
                columns = [row[1] for row in original.execute(f"PRAGMA table_info({name})")]
                selected = ",".join(_sql_name(column) for column in columns)
                old_rows = Counter(original.execute(f"SELECT {selected} FROM {name}"))
                new_rows = Counter(candidate.execute(f"SELECT {selected} FROM {name}"))
                if old_rows - new_rows:
                    raise PreservationError("Candidate removed or changed existing SQLite data")
    except sqlite3.DatabaseError as exc:
        raise PreservationError("Candidate SQLite schema is not backward compatible") from exc


def verify_site(app: Path, destination: Path) -> None:
    record = verified_checkpoint(app, destination)
    for name in ("config", "sounds"):
        if inventory(Path(record["roots"][name])) != record["files"][name]:
            raise PreservationError("Upgrade changed site configuration or custom uploads")
    for name, entries in record.get("metadata", {}).items():
        root = Path(record["roots"][name])
        for relative, attributes in entries.items():
            path = root / relative
            if not path.exists():
                raise PreservationError("Candidate removed an existing site-data path")
            stat = path.stat()
            if stat.st_mode & 0o777 != attributes["mode"] or (os.name == "posix" and
                    (stat.st_uid != attributes["uid"] or stat.st_gid != attributes["gid"])):
                raise PreservationError("Candidate changed site-data permissions or ownership")
    state = Path(record["roots"]["state"])
    current_files = inventory(state)
    for relative, digest in record["files"]["state"].items():
        if relative not in current_files:
            raise PreservationError("Candidate removed existing site state")
        original, candidate = destination / "state" / relative, state / relative
        with original.open("rb") as handle:
            sqlite_file = handle.read(16) == b"SQLite format 3\x00"
        if sqlite_file:
            verify_database(original, candidate)
        elif current_files[relative] != digest:
            raise PreservationError("Candidate changed existing site state")


def rollback(app: Path, destination: Path) -> None:
    record = verified_checkpoint(app, destination)
    # Service stays stopped and the durable maintenance marker stays present throughout.
    for name, raw in record["roots"].items():
        target, source = Path(raw), destination / name
        inventory(target)  # Reject unexpected symlinks before deleting/replacing anything.
        for item in sorted(target.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if item.is_file():
                item.unlink()
            else:
                item.rmdir()
        for item in sorted(source.rglob("*")):
            output = target / item.relative_to(source)
            if item.is_dir():
                output.mkdir()
            else:
                shutil.copy2(item, output)
        for relative, attributes in record.get("metadata", {}).get(name, {}).items():
            path = target / relative
            path.chmod(attributes["mode"])
            if os.name == "posix":
                os.chown(path, attributes["uid"], attributes["gid"])
        if inventory(target) != record["files"][name]:
            raise PreservationError("Restored site data failed checksum verification")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["checkpoint", "verify", "rollback"])
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    {"checkpoint": checkpoint, "verify": verify_site, "rollback": rollback}[args.action](args.app_dir, args.checkpoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
