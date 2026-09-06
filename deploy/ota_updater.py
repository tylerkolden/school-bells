#!/usr/bin/env python3
"""Root-owned, fixed-source OTA updater for the school bell appliance.

This file deliberately has no imports from ``bell``. The web application may request one of
two fixed operations, but code writable by the service account is never imported as root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only; production is Raspberry Pi OS.
    fcntl = None  # type: ignore[assignment]

REPOSITORY = "tylerkolden/school-bells"
API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
EXPECTED_PUBLISHER = "github-actions[bot]"
TAG_PATTERN = re.compile(r"^v(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_REQUEST_BYTES = 4096
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
DEFAULT_MAINTENANCE_GUARD_SECONDS = 15 * 60
USER_AGENT = "school-bell-ota/1"
MANAGED_PATHS = (
    Path("/etc/systemd/system/bell-system.service"),
    Path("/etc/systemd/system/bell-update.service"),
    Path("/etc/systemd/system/bell-update.path"),
    Path("/usr/local/lib/bell-system/ota_updater.py"),
)


class UpdateError(RuntimeError):
    """A safe, operator-actionable update failure."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"timestamp": _now(), "event": event, **fields}, sort_keys=True), flush=True)


def parse_version(tag: str) -> tuple[int, int, int]:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise UpdateError(f"Release tag {tag!r} is not a stable semantic version (vMAJOR.MINOR.PATCH)")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _safe_message(value: object, limit: int = 8000) -> str:
    text = str(value or "")
    return "".join(character for character in text if character in "\n\t" or ord(character) >= 32)[:limit]


def _bell_service_uid() -> int:
    import pwd

    try:
        return pwd.getpwnam("bell").pw_uid
    except KeyError as exc:
        raise UpdateError("The bell service account does not exist") from exc


class GitHubReleases:
    def __init__(self, repository: str = REPOSITORY, *, timeout: float = 15.0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise UpdateError("Invalid fixed GitHub repository name")
        self.repository = repository
        self.timeout = timeout

    def _api(self, path: str) -> Any:
        url = f"{API_ROOT}/repos/{self.repository}/{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if urllib.parse.urlsplit(response.geturl()).hostname != "api.github.com":
                    raise UpdateError("GitHub API redirected to an unexpected host")
                if int(response.headers.get("Content-Length", "0")) > 2 * 1024 * 1024:
                    raise UpdateError("GitHub release metadata is unexpectedly large")
                payload = response.read(2 * 1024 * 1024 + 1)
                if len(payload) > 2 * 1024 * 1024:
                    raise UpdateError("GitHub release metadata is unexpectedly large")
                return json.loads(payload)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise UpdateError(f"Cannot read GitHub release metadata: {exc}") from exc

    def latest(self) -> dict[str, Any]:
        return self._validated_release(self._api("releases/latest"))

    def by_tag(self, tag: str) -> dict[str, Any]:
        parse_version(tag)
        encoded = urllib.parse.quote(tag, safe="")
        return self._validated_release(self._api(f"releases/tags/{encoded}"))

    def _validated_release(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise UpdateError("GitHub returned invalid release metadata")
        tag = value.get("tag_name")
        if not isinstance(tag, str):
            raise UpdateError("GitHub release metadata has no tag")
        parse_version(tag)
        if value.get("draft") or value.get("prerelease"):
            raise UpdateError("Only published stable releases may be installed")
        if value.get("immutable") is not True:
            raise UpdateError(
                "The release is not immutable. Enable GitHub immutable releases before OTA deployment."
            )
        author = value.get("author")
        if not isinstance(author, dict) or author.get("login") != EXPECTED_PUBLISHER:
            raise UpdateError("The release was not published by the approved GitHub Actions workflow")
        assets = value.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("GitHub release metadata has no assets")
        expected_name = f"bell-system-{tag}.tar.gz"
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == expected_name]
        if len(matches) != 1:
            raise UpdateError(f"Release must contain exactly one {expected_name} asset")
        asset = matches[0]
        digest = asset.get("digest")
        uploader = asset.get("uploader")
        if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
            raise UpdateError("The release asset has no valid GitHub SHA-256 digest")
        if not isinstance(uploader, dict) or uploader.get("login") != EXPECTED_PUBLISHER:
            raise UpdateError("The release asset was not uploaded by the approved workflow")
        size = asset.get("size")
        asset_id = asset.get("id")
        if not isinstance(size, int) or size <= 0 or size > MAX_ARCHIVE_BYTES:
            raise UpdateError("The release asset size is invalid or exceeds the 128 MiB limit")
        if not isinstance(asset_id, int) or asset_id <= 0:
            raise UpdateError("The release asset ID is invalid")
        return {
            "tag": tag,
            "version": tag.removeprefix("v"),
            "digest": digest,
            "asset_id": asset_id,
            "asset_name": expected_name,
            "size": size,
            "published_at": _safe_message(value.get("published_at"), 64),
            "release_url": _safe_message(value.get("html_url"), 500),
            "notes": _safe_message(value.get("body")),
        }

    def download(self, release: dict[str, Any], destination: Path) -> None:
        url = f"{API_ROOT}/repos/{self.repository}/releases/assets/{release['asset_id']}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/octet-stream",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        digest = hashlib.sha256()
        received = 0
        allowed_hosts = {
            "api.github.com",
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
        }
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 60.0)) as response, destination.open("xb") as output:
                final_host = urllib.parse.urlsplit(response.geturl()).hostname
                if final_host not in allowed_hosts:
                    raise UpdateError("Release download redirected to an unexpected host")
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > MAX_ARCHIVE_BYTES or received > release["size"]:
                        raise UpdateError("Release download exceeded its declared size")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise UpdateError(f"Cannot download release asset: {exc}") from exc
        if received != release["size"]:
            destination.unlink(missing_ok=True)
            raise UpdateError("Release download size does not match GitHub metadata")
        actual = f"sha256:{digest.hexdigest()}"
        if actual != release["digest"]:
            destination.unlink(missing_ok=True)
            raise UpdateError("Release SHA-256 digest does not match immutable GitHub metadata")


def _regular_request(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise UpdateError("No update request is queued") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UpdateError("Refusing a non-regular or hard-linked update request")
    if metadata.st_size <= 0 or metadata.st_size > MAX_REQUEST_BYTES:
        raise UpdateError("Update request size is invalid")
    if os.name == "posix":
        if metadata.st_uid != _bell_service_uid():
            raise UpdateError("Update request is not owned by the bell service account")
        if metadata.st_mode & 0o022:
            raise UpdateError("Update request must not be writable by group or other users")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Cannot read update request: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise UpdateError("Update request schema is invalid")
    if value.get("action") not in {"check", "install"}:
        raise UpdateError("Update request action is invalid")
    if not isinstance(value.get("id"), str) or not REQUEST_ID_PATTERN.fullmatch(value["id"]):
        raise UpdateError("Update request ID is invalid")
    if value["action"] == "install":
        if not isinstance(value.get("tag"), str):
            raise UpdateError("Install request tag is invalid")
        parse_version(value["tag"])
        if not isinstance(value.get("digest"), str) or not DIGEST_PATTERN.fullmatch(value["digest"]):
            raise UpdateError("Install request digest is invalid")
    return value


def installed_release(app_dir: Path) -> dict[str, Any]:
    candidates = [app_dir / "current" / "RELEASE.json", app_dir / "RELEASE.json"]
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        tag = value.get("tag") if isinstance(value, dict) else None
        if isinstance(tag, str) and TAG_PATTERN.fullmatch(tag):
            return value
    return {"tag": "v0.0.0", "version": "0.0.0", "commit": None}


def _write_json(path: Path, value: dict[str, Any], *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        try:
            import grp

            os.chown(temporary, 0, grp.getgrnam("bell").gr_gid)
        except (ImportError, KeyError, PermissionError):
            pass
        os.replace(temporary, path)
        if os.name == "posix":
            for directory in (path.parent, path.parent.parent):
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_status(status_path: Path, phase: str, message: str, **fields: object) -> None:
    _write_json(
        status_path,
        {"schema": 1, "phase": phase, "message": _safe_message(message, 1000), "updated_at": _now(), **fields},
    )


def _check_maintenance_window(guard_seconds: int) -> None:
    request = urllib.request.Request("http://127.0.0.1:8000/health", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            health = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise UpdateError(
            "The bell health service is unavailable; web OTA fails closed. Use local recovery procedures."
        ) from exc
    if not isinstance(health, dict):
        raise UpdateError("The bell health response is invalid")
    active = health.get("active_page")
    if isinstance(active, dict) and active:
        raise UpdateError("A page is active. Wait for it to finish before updating.")
    if health.get("ready") is not True:
        reasons = health.get("readiness_reasons")
        detail = ", ".join(str(reason) for reason in reasons) if isinstance(reasons, list) else "unknown reason"
        raise UpdateError(
            f"The bell service is not ready ({_safe_message(detail, 500)}). Web OTA requires a healthy baseline."
        )
    next_fire = health.get("next_scheduled_fire")
    if isinstance(next_fire, str) and next_fire:
        try:
            scheduled = datetime.fromisoformat(next_fire)
            if scheduled.tzinfo is None:
                raise ValueError("timezone missing")
        except ValueError as exc:
            raise UpdateError("The next scheduled bell time is invalid; refusing to update") from exc
        seconds = (scheduled.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        if 0 <= seconds < guard_seconds:
            minutes = max(1, int((seconds + 59) // 60))
            raise UpdateError(
                f"The next bell is in about {minutes} minute(s). Updates require a {guard_seconds // 60}-minute quiet window."
            )


def _safe_extract(archive: Path, destination: Path) -> dict[str, Any]:
    total = 0
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                raise UpdateError("Release archive member count is invalid")
            names: set[PurePosixPath] = set()
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise UpdateError(f"Release archive contains an unsafe path: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise UpdateError(f"Release archive contains a link or special file: {member.name}")
                if path in names:
                    raise UpdateError(f"Release archive contains a duplicate path: {member.name}")
                names.add(path)
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    raise UpdateError("Expanded release exceeds the 512 MiB safety limit")
            required_free = total + 256 * 1024 * 1024
            if shutil.disk_usage(destination).free < required_free:
                raise UpdateError("Not enough free disk space to stage the release safely")
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted: BinaryIO | None = source.extractfile(member)
                if extracted is None:
                    raise UpdateError(f"Cannot extract release member: {member.name}")
                with extracted, target.open("xb") as output:
                    shutil.copyfileobj(extracted, output, length=1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError(f"Cannot safely extract release archive: {exc}") from exc
    manifest_path = destination / "RELEASE.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Release manifest is missing or invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("Release manifest is invalid")
    return manifest


def _atomic_symlink(target: str, link: Path) -> None:
    temporary = link.with_name(f".{link.name}-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)
    if os.name == "posix":
        descriptor = os.open(link.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _run(command: list[str], *, timeout: float = 600.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"Command timed out: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = _safe_message((exc.stderr or exc.stdout or "").strip(), 3000)
        raise UpdateError(f"Command failed ({command[0]}): {detail}") from exc


def _wait_healthy(timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "service did not respond"
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request("http://127.0.0.1:8000/ready", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=3.0) as response:
                value = json.load(response)
            if response.status == 200 and isinstance(value, dict) and value.get("ready") is True:
                return
            last_detail = _safe_message(value, 500)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_detail = str(exc)
        time.sleep(2.0)
    raise UpdateError(f"Updated service did not become ready: {last_detail}")


def install_release(
    client: GitHubReleases,
    release: dict[str, Any],
    *,
    app_dir: Path,
    updater_dir: Path,
) -> None:
    if (app_dir / ".upgrade-incomplete").exists():
        raise UpdateError("An active or interrupted upgrade must be recovered first")
    cache_dir = updater_dir / "releases" / release["tag"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / release["asset_name"]
    if archive.exists():
        actual = f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"
        if actual != release["digest"]:
            archive.unlink()
    if not archive.exists():
        partial = archive.with_suffix(f"{archive.suffix}.partial")
        partial.unlink(missing_ok=True)
        client.download(release, partial)
        os.replace(partial, archive)
    with tempfile.TemporaryDirectory(prefix="install-", dir=updater_dir) as temporary:
        source = Path(temporary) / "release"
        source.mkdir()
        manifest = _safe_extract(archive, source)
        if manifest.get("tag") != release["tag"]:
            raise UpdateError("Release manifest tag does not match GitHub metadata")
        commit = manifest.get("commit")
        if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
            raise UpdateError("Release manifest commit is invalid")
        installer = source / "deploy" / "install.sh"
        if not installer.is_file():
            raise UpdateError("Release does not contain deploy/install.sh")
        current_link = app_dir / "current"
        previous_target = os.readlink(current_link) if current_link.is_symlink() else None
        previous_files = {
            path: path.read_bytes() if path.is_file() else None for path in MANAGED_PATHS
        }
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "BELL_RELEASE_VERSION": release["tag"],
            "BELL_RELEASE_COMMIT": commit,
            "BELL_RELEASE_DIGEST": release["digest"],
        }
        receipt = app_dir / ".upgrade-transaction"
        prior_receipt = receipt.read_text(encoding="utf-8") if receipt.exists() else None
        try:
            result = _run(["/usr/bin/bash", str(installer)], env=environment)
            _log("installer_complete", output=_safe_message(result.stdout, 2000))
            _wait_healthy()
        except UpdateError as update_error:
            new_receipt = receipt.read_text(encoding="utf-8") if receipt.exists() else None
            if new_receipt and new_receipt != prior_receipt:
                transaction = Path(new_receipt.strip()).resolve()
                if not transaction.is_relative_to(updater_dir.resolve() / "transactions"):
                    raise UpdateError("Untrusted upgrade recovery location; service must remain stopped") from update_error
                _run(["/usr/bin/python3", str(transaction / "upgrade_transaction.py"), "recover",
                      "--app-dir", str(app_dir), "--transaction", str(transaction), "--rollback-committed"], timeout=900)
                raise UpdateError(f"Update failed; data and code recovery completed: {update_error}") from update_error
            switched = current_link.is_symlink() and os.readlink(current_link) != previous_target
            for path, content in previous_files.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
                    path.chmod(0o755 if path.name.endswith(".py") else 0o644)
            _run(["/usr/bin/systemctl", "daemon-reload"], timeout=30)
            if previous_target and switched:
                _log("update_rollback_started", previous_target=previous_target)
                _atomic_symlink(previous_target, current_link)
                _run(["/usr/bin/systemctl", "restart", "bell-system.service"], timeout=60)
                try:
                    _wait_healthy(timeout=90)
                except UpdateError as rollback_error:
                    raise UpdateError(
                        f"Update failed and automatic rollback did not recover readiness: {rollback_error}"
                    ) from update_error
                raise UpdateError(f"Update failed; automatic rollback succeeded: {update_error}") from update_error
            raise


def process_request(
    *,
    app_dir: Path,
    updater_dir: Path,
    repository: str,
    guard_seconds: int,
) -> int:
    update_dir = app_dir / "state" / "update"
    request_path = update_dir / "request.json"
    status_path = update_dir / "status.json"
    updater_dir.mkdir(parents=True, exist_ok=True)
    lock_path = updater_dir / "update.lock"
    request_id: str | None = None
    with lock_path.open("a+b") as lock:
        if fcntl is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
        try:
            request = _regular_request(request_path)
            request_id = request["id"]
            current = installed_release(app_dir)
            client = GitHubReleases(repository)
            write_status(
                status_path,
                "checking" if request["action"] == "check" else "installing",
                "Checking the approved GitHub production release."
                if request["action"] == "check"
                else f"Installing {request['tag']}. The console may briefly reconnect.",
                request_id=request_id,
                installed_version=current.get("version", str(current.get("tag", "")).removeprefix("v")),
            )
            if request["action"] == "check":
                release = client.latest()
                newer = parse_version(release["tag"]) > parse_version(str(current.get("tag", "v0.0.0")))
                phase = "update_available" if newer else "up_to_date"
                message = (
                    f"Production release {release['tag']} is available."
                    if newer
                    else f"This appliance is current at {current.get('tag', 'v0.0.0')}."
                )
                write_status(
                    status_path,
                    phase,
                    message,
                    request_id=request_id,
                    installed_version=current.get("version", str(current.get("tag", "")).removeprefix("v")),
                    release=release,
                )
                return 0
            release = client.by_tag(request["tag"])
            if release["digest"] != request["digest"]:
                raise UpdateError("Release metadata changed after confirmation; check again")
            if parse_version(release["tag"]) <= parse_version(str(current.get("tag", "v0.0.0"))):
                raise UpdateError("Web OTA does not reinstall or downgrade releases")
            _check_maintenance_window(guard_seconds)
            install_release(client, release, app_dir=app_dir, updater_dir=updater_dir)
            updated = installed_release(app_dir)
            if updated.get("tag") != release["tag"]:
                raise UpdateError("Installer completed but the installed release marker is wrong")
            write_status(
                status_path,
                "success",
                f"Successfully updated to {release['tag']} and passed readiness checks.",
                request_id=request_id,
                installed_version=release["version"],
                installed_release=updated,
                release=release,
            )
            return 0
        except UpdateError as exc:
            _log("update_failed", request_id=request_id, detail=str(exc))
            write_status(
                status_path,
                "failed",
                str(exc),
                request_id=request_id,
                installed_version=installed_release(app_dir).get("version", "unknown"),
            )
            return 1
        except Exception as exc:  # Last-resort containment for a root systemd job.
            _log("update_failed_unexpected", request_id=request_id, error_type=type(exc).__name__)
            write_status(
                status_path,
                "failed",
                "The updater encountered an unexpected internal error. See the system journal.",
                request_id=request_id,
                installed_version=installed_release(app_dir).get("version", "unknown"),
            )
            return 1
        finally:
            request_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["process"])
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/bell"))
    parser.add_argument("--updater-dir", type=Path, default=Path("/var/lib/bell-updater"))
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument(
        "--maintenance-guard-seconds",
        type=int,
        default=DEFAULT_MAINTENANCE_GUARD_SECONDS,
    )
    args = parser.parse_args(argv)
    if not 60 <= args.maintenance_guard_seconds <= 24 * 60 * 60:
        parser.error("maintenance guard must be between 60 seconds and 24 hours")
    return process_request(
        app_dir=args.app_dir.resolve(),
        updater_dir=args.updater_dir.resolve(),
        repository=args.repository,
        guard_seconds=args.maintenance_guard_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
