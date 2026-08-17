"""Local appliance accounts using standard-library scrypt password hashes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    username: str
    role: str


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


class AuthStore:
    def __init__(self, path: Path, bootstrap_password: str) -> None:
        self.path = path
        self.bootstrap_password = bootstrap_password
        self._lock = threading.Lock()

    def _load(self) -> dict[str, object] | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("Stored account database is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise AuthError("Stored account database has an unsupported schema")
        users = payload.get("users")
        if not isinstance(users, list):
            raise AuthError("Stored account database is invalid")
        return payload

    @property
    def revision(self) -> int:
        payload = self._load()
        return int(payload.get("revision", 0)) if payload else 0

    def verify(self, username: str, password: str) -> AuthenticatedUser | None:
        normalized = username.strip().lower()
        payload = self._load()
        if payload is None:
            if normalized == "admin" and secrets.compare_digest(password, self.bootstrap_password):
                return AuthenticatedUser("admin", "admin")
            return None
        for item in payload["users"]:
            if (
                not isinstance(item, dict)
                or item.get("username") != normalized
                or not item.get("enabled", True)
            ):
                continue
            try:
                salt = bytes.fromhex(str(item["salt"]))
                expected = str(item["password_hash"])
            except (KeyError, ValueError) as exc:
                raise AuthError("Stored account record is invalid") from exc
            if secrets.compare_digest(_password_hash(password, salt), expected):
                return AuthenticatedUser(normalized, str(item.get("role", "operator")))
        return None

    def _record(self, username: str, role: str, password: str) -> dict[str, object]:
        if len(password) < 12:
            raise AuthError("Passwords must contain at least 12 characters")
        salt = secrets.token_bytes(16)
        return {
            "username": username,
            "role": role,
            "enabled": True,
            "salt": salt.hex(),
            "password_hash": _password_hash(password, salt),
        }

    def _save(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, prefix=".auth-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(self.path)
            temporary = None
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def set_password(self, username: str, role: str, password: str) -> None:
        with self._lock:
            payload = self._load() or {"schema": 1, "revision": 0, "users": []}
            users = [
                item
                for item in payload["users"]
                if isinstance(item, dict) and item.get("username") != username
            ]
            users.append(self._record(username, role, password))
            payload["users"] = users
            payload["revision"] = int(payload.get("revision", 0)) + 1
            self._save(payload)

    def delete_user(self, username: str) -> bool:
        if username == "admin":
            raise AuthError("The administrator account cannot be deleted")
        with self._lock:
            payload = self._load()
            if payload is None:
                return False
            previous = len(payload["users"])
            payload["users"] = [
                item
                for item in payload["users"]
                if isinstance(item, dict) and item.get("username") != username
            ]
            if len(payload["users"]) == previous:
                return False
            payload["revision"] = int(payload.get("revision", 0)) + 1
            self._save(payload)
            return True

    def users(self) -> list[dict[str, str]]:
        payload = self._load()
        if payload is None:
            return [{"username": "admin", "role": "admin", "source": "bootstrap password"}]
        return [
            {
                "username": str(item.get("username")),
                "role": str(item.get("role")),
                "source": "local account database",
            }
            for item in payload["users"]
            if isinstance(item, dict) and item.get("enabled", True)
        ]
