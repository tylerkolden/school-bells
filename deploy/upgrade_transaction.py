#!/usr/bin/env python3
"""Root-owned upgrade transaction; saved alongside each checkpoint for crash recovery."""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from pathlib import Path

from ota_updater import (
    MANAGED_PATHS,
    UpdateError,
    _atomic_symlink,
    _check_maintenance_window,
    _run,
    _wait_healthy,
    _write_json,
)

GUARD = Path("/etc/systemd/system/bell-system.service.d/10-preservation.conf")
PROBE = Path("/run/systemd/system/bell-system.service.d/90-upgrade-probe.conf")
SERVICE = "bell-system.service"


def durable_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "posix":
        for directory in (path.parent, path.parent.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def marker(app: Path) -> Path:
    return app / ".upgrade-incomplete"


def save(path: Path, record: dict) -> None:
    _write_json(path / "transaction.json", record, mode=0o600)


def begin(app: Path, transaction: Path, python: Path, helper: Path) -> None:
    if marker(app).exists():
        raise UpdateError("An interrupted upgrade needs recovery before another install")
    current = app / "current"
    if not current.is_symlink() or not current.resolve().is_dir():
        raise UpdateError("Existing installation lacks a rollback release; migrate it explicitly first")
    _check_maintenance_window(900)
    transaction.mkdir(parents=True, mode=0o700, exist_ok=False)
    # Retain trusted recovery tooling independently of staging cleanup or the selected release.
    for source in (Path(__file__), Path(__file__).with_name("ota_updater.py"), helper):
        shutil.copy2(source, transaction / source.name)
    record = {"schema": 1, "phase": "preparing", "app": str(app), "previous": os.readlink(current),
              "files": {str(path): {"content": base64.b64encode(path.read_bytes()).decode(),
                                    "mode": path.stat().st_mode & 0o777} if path.is_file() else None
                        for path in MANAGED_PATHS}}
    save(transaction, record)
    durable_text(app / ".upgrade-transaction", str(transaction))
    GUARD.parent.mkdir(parents=True, exist_ok=True)
    durable_text(GUARD, f"[Unit]\nConditionPathExists=!{marker(app)}\n")
    durable_text(marker(app), str(transaction) + "\n")
    marker(app).chmod(0o644)
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
    _run(["/usr/bin/systemctl", "stop", SERVICE], timeout=60)
    _run([str(python), str(helper), "checkpoint", "--app-dir", str(app),
          "--checkpoint", str(transaction / "data")], timeout=600)
    record["phase"] = "prepared"
    save(transaction, record)


def allow_probe() -> None:
    # Runtime override disappears at reboot; the persistent guard remains fail-closed.
    PROBE.parent.mkdir(parents=True, exist_ok=True)
    PROBE.write_text("[Unit]\nConditionPathExists=\n", encoding="utf-8")
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)


def finish(app: Path, transaction: Path) -> None:
    record = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
    if record["phase"] != "prepared" or record["app"] != str(app):
        raise UpdateError("Upgrade checkpoint is not prepared")
    allow_probe()
    _run(["/usr/bin/systemctl", "restart", SERVICE], timeout=60)
    _wait_healthy()
    _run([sys.executable, str(transaction / "upgrade.py"), "verify", "--app-dir", str(app),
          "--checkpoint", str(transaction / "data")], timeout=120)
    record["phase"] = "committed"
    save(transaction, record)
    marker(app).unlink(missing_ok=True)
    PROBE.unlink(missing_ok=True)
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)


def recover(app: Path, transaction: Path, *, rollback_committed: bool = False) -> None:
    record = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
    if record.get("schema") != 1 or record.get("app") != str(app):
        raise UpdateError("Recovery record does not match this appliance")
    if rollback_committed and record["phase"] == "committed":
        durable_text(marker(app), str(transaction))
        record["phase"] = "prepared"
        save(transaction, record)
    if record["phase"] in {"committed", "rolled_back"}:
        marker(app).unlink(missing_ok=True)
        PROBE.unlink(missing_ok=True)
        _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
        _run(["/usr/bin/systemctl", "start", SERVICE], timeout=60)
        return
    _run(["/usr/bin/systemctl", "stop", SERVICE], timeout=60)
    if record["phase"] == "prepared":
        _run([sys.executable, str(transaction / "upgrade.py"), "rollback", "--app-dir", str(app),
              "--checkpoint", str(transaction / "data")], timeout=600)
    elif record["phase"] != "preparing":
        raise UpdateError("Unknown transaction phase; manual recovery required")
    for raw, previous in record["files"].items():
        path = Path(raw)
        if path not in MANAGED_PATHS:
            raise UpdateError("Unrecognized managed recovery path")
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(base64.b64decode(previous["content"]))
            path.chmod(previous["mode"])
    _atomic_symlink(record["previous"], app / "current")
    allow_probe()
    _run(["/usr/bin/systemctl", "restart", SERVICE], timeout=60)
    _wait_healthy()
    record["phase"] = "rolled_back"
    save(transaction, record)
    marker(app).unlink(missing_ok=True)
    PROBE.unlink(missing_ok=True)
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["begin", "finish", "recover"])
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/bell"))
    parser.add_argument("--transaction", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--rollback-committed", action="store_true")
    args = parser.parse_args()
    if args.action == "begin":
        if not args.python or not args.helper:
            parser.error("begin requires --python and --helper")
        begin(args.app_dir, args.transaction, args.python, args.helper)
    elif args.action == "recover":
        recover(args.app_dir, args.transaction, rollback_committed=args.rollback_committed)
    else:
        finish(args.app_dir, args.transaction)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
