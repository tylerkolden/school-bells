"""Unprivileged side of the root-owned OTA update request queue."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_STATUS_BYTES = 64 * 1024
ALLOWED_ACTIONS = {"check", "install"}


class UpdateRequestError(RuntimeError):
    """Raised when a safe update request cannot be queued."""


def update_directory(state_dir: Path) -> Path:
    return state_dir / "update"


def load_update_status(state_dir: Path) -> dict[str, Any]:
    path = update_directory(state_dir) / "status.json"
    try:
        if path.stat().st_size > MAX_STATUS_BYTES:
            raise UpdateRequestError("The updater status file is unexpectedly large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"phase": "idle", "message": "No update check has run yet."}
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateRequestError(f"Cannot read updater status: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("phase"), str):
        raise UpdateRequestError("The updater status file is invalid")
    return value


def queue_update_request(
    state_dir: Path,
    action: str,
    *,
    tag: str | None = None,
    digest: str | None = None,
) -> str:
    if action not in ALLOWED_ACTIONS:
        raise UpdateRequestError("Unknown update action")
    if action == "install" and (not tag or not digest):
        raise UpdateRequestError("An install request requires a release tag and digest")
    directory = update_directory(state_dir)
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    request_path = directory / "request.json"
    if request_path.exists():
        raise UpdateRequestError("An update request is already queued")
    request_id = secrets.token_hex(16)
    request = {
        "schema": 1,
        "id": request_id,
        "action": action,
        "requested_at": datetime.now(UTC).isoformat(),
    }
    if action == "install":
        request.update({"tag": tag, "digest": digest})
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix="request-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(request, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        # The path unit watches this exact name. Do not overwrite a queued request.
        try:
            os.link(temporary_name, request_path)
        except FileExistsError as exc:
            raise UpdateRequestError("An update request is already queued") from exc
        request_path.chmod(0o600)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    return request_id
