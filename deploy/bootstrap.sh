#!/usr/bin/env bash
set -Eeuo pipefail

# First-install bootstrap for a fresh 64-bit Raspberry Pi OS host. Future updates
# are installed from the authenticated web console by the root-owned OTA helper.

REPOSITORY=tylerkolden/school-bells
EXPECTED_PUBLISHER='github-actions[bot]'
MAX_ARCHIVE_BYTES=$((128 * 1024 * 1024))

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this bootstrap as root: sudo bash install-school-bells.sh" >&2
  exit 1
fi

machine="$(uname -m)"
if [[ "$machine" != "aarch64" && "$machine" != "arm64" ]]; then
  echo "School Bell requires 64-bit Raspberry Pi OS (aarch64); found: $machine" >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
  echo "This installer requires Raspberry Pi OS or another systemd-based Debian derivative." >&2
  exit 1
fi

echo "Installing Raspberry Pi prerequisites..."
apt-get update -o Acquire::Retries=3
DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
  ca-certificates ffmpeg iproute2 python3 python3-venv

runtime_abi="$(python3 -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
case "$runtime_abi" in
  cp311|cp312|cp313) ;;
  *)
    echo "School Bell supports Python 3.11-3.13; found: $(python3 --version)" >&2
    exit 1
    ;;
esac

interface_ip="${BELL_INTERFACE_IP:-}"
if [[ -z "$interface_ip" ]]; then
  interface_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' || true)"
fi
if [[ -z "$interface_ip" ]]; then
  interface_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if ! python3 - "$interface_ip" <<'PY_INTERFACE'
import ipaddress
import sys

try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if address.version != 4 or address.is_loopback or address.is_multicast or address.is_unspecified:
    raise SystemExit(1)
PY_INTERFACE
then
  echo "Cannot select the phone-VLAN IPv4 address automatically." >&2
  echo "Retry with: sudo BELL_INTERFACE_IP=192.168.x.x bash install-school-bells.sh" >&2
  exit 1
fi
echo "Using $interface_ip as the initial phone-VLAN address."

umask 077
work_dir="$(mktemp -d /tmp/bell-bootstrap.XXXXXXXX)"
cleanup() {
  if [[ -n "${work_dir:-}" && -d "$work_dir" && "$work_dir" == /tmp/bell-bootstrap.* ]]; then
    rm -rf -- "$work_dir"
  fi
}
trap cleanup EXIT INT TERM

echo "Downloading and verifying the latest immutable production release..."
python3 - "$work_dir" "$REPOSITORY" "$EXPECTED_PUBLISHER" "$MAX_ARCHIVE_BYTES" <<'PY_BOOTSTRAP'
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "school-bell-bootstrap/1"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
TAG_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class BootstrapError(RuntimeError):
    """Raised when first-install release verification fails."""


def api_json(repository: str) -> object:
    request = urllib.request.Request(
        f"{API_ROOT}/repos/{repository}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=20.0) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname != "api.github.com":
            raise BootstrapError("GitHub metadata redirected to an unexpected host")
        declared = int(response.headers.get("Content-Length", "0"))
        if declared > MAX_METADATA_BYTES:
            raise BootstrapError("GitHub release metadata is unexpectedly large")
        payload = response.read(MAX_METADATA_BYTES + 1)
    if len(payload) > MAX_METADATA_BYTES:
        raise BootstrapError("GitHub release metadata is unexpectedly large")
    return json.loads(payload)


def validate_release(
    value: object,
    repository: str,
    expected_publisher: str,
    max_archive_bytes: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapError("GitHub returned invalid release metadata")
    tag = value.get("tag_name")
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise BootstrapError("Latest release does not have a stable semantic-version tag")
    if value.get("draft") or value.get("prerelease"):
        raise BootstrapError("Only published stable releases may be installed")
    if value.get("immutable") is not True:
        raise BootstrapError("Latest release is not immutable")
    author = value.get("author")
    if not isinstance(author, dict) or author.get("login") != expected_publisher:
        raise BootstrapError("Release was not published by the approved workflow")
    expected_release_url = f"https://github.com/{repository}/releases/tag/{tag}"
    if value.get("html_url") != expected_release_url:
        raise BootstrapError("Release URL does not match the fixed repository")
    assets = value.get("assets")
    expected_name = f"bell-system-{tag}.tar.gz"
    if not isinstance(assets, list):
        raise BootstrapError("Release metadata has no assets")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == expected_name]
    if len(matches) != 1:
        raise BootstrapError(f"Release must contain exactly one {expected_name} asset")
    asset = matches[0]
    uploader = asset.get("uploader")
    digest = asset.get("digest")
    size = asset.get("size")
    asset_id = asset.get("id")
    if not isinstance(uploader, dict) or uploader.get("login") != expected_publisher:
        raise BootstrapError("Release asset was not uploaded by the approved workflow")
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise BootstrapError("Release asset has no valid GitHub SHA-256 digest")
    if not isinstance(size, int) or size <= 0 or size > max_archive_bytes:
        raise BootstrapError("Release asset size is invalid or too large")
    if not isinstance(asset_id, int) or asset_id <= 0:
        raise BootstrapError("Release asset ID is invalid")
    return {
        "tag": tag,
        "version": tag.removeprefix("v"),
        "asset_id": asset_id,
        "asset_name": expected_name,
        "digest": digest,
        "size": size,
    }


def download_release(repository: str, release: dict[str, Any], destination: Path) -> None:
    request = urllib.request.Request(
        f"{API_ROOT}/repos/{repository}/releases/assets/{release['asset_id']}",
        headers={
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    digest = hashlib.sha256()
    received = 0
    with urllib.request.urlopen(request, timeout=90.0) as response, destination.open("xb") as output:
        final_host = urllib.parse.urlsplit(response.geturl()).hostname
        if final_host not in ALLOWED_DOWNLOAD_HOSTS:
            raise BootstrapError("Release download redirected to an unexpected host")
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > release["size"]:
                raise BootstrapError("Release download exceeded its declared size")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if received != release["size"]:
        raise BootstrapError("Release download size does not match GitHub metadata")
    if f"sha256:{digest.hexdigest()}" != release["digest"]:
        raise BootstrapError("Release digest does not match immutable GitHub metadata")


def safe_extract(archive: Path, destination: Path) -> dict[str, Any]:
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise BootstrapError("Release archive member count is invalid")
        names: set[PurePosixPath] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise BootstrapError(f"Release archive contains an unsafe path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise BootstrapError(f"Release archive contains a link or special file: {member.name}")
            if path in names:
                raise BootstrapError(f"Release archive contains a duplicate path: {member.name}")
            names.add(path)
            total += member.size
            if total > MAX_EXPANDED_BYTES:
                raise BootstrapError("Expanded release exceeds the safety limit")
        if shutil.disk_usage(destination).free < total + 256 * 1024 * 1024:
            raise BootstrapError("Not enough free disk space to stage the release")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted: BinaryIO | None = source.extractfile(member)
            if extracted is None:
                raise BootstrapError(f"Cannot extract release member: {member.name}")
            with extracted, target.open("xb") as output:
                shutil.copyfileobj(extracted, output, length=1024 * 1024)
            target.chmod(0o755 if member.mode & stat.S_IXUSR else 0o644)
    manifest_path = destination / "RELEASE.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Release manifest is missing or invalid: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise BootstrapError("Release manifest schema is invalid")
    return manifest


def main(arguments: list[str]) -> None:
    if len(arguments) != 5:
        raise BootstrapError("Invalid bootstrap invocation")
    work_dir = Path(arguments[1])
    repository = arguments[2]
    expected_publisher = arguments[3]
    max_archive_bytes = int(arguments[4])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BootstrapError("Invalid fixed repository name")
    release = validate_release(
        api_json(repository), repository, expected_publisher, max_archive_bytes
    )
    archive = work_dir / release["asset_name"]
    source = work_dir / "release"
    source.mkdir(mode=0o700)
    download_release(repository, release, archive)
    manifest = safe_extract(archive, source)
    if manifest.get("tag") != release["tag"] or manifest.get("version") != release["version"]:
        raise BootstrapError("Release manifest version does not match GitHub metadata")
    commit = manifest.get("commit")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise BootstrapError("Release manifest commit is invalid")
    installer = source / "deploy" / "install.sh"
    if not installer.is_file():
        raise BootstrapError("Release does not contain deploy/install.sh")
    result = {**release, "commit": commit, "source": str(source)}
    (work_dir / "result.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        main(sys.argv)
    except (BootstrapError, OSError, ValueError, tarfile.TarError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Secure bootstrap failed: {exc}") from exc
PY_BOOTSTRAP

read_result() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' \
    "$work_dir/result.json" "$1"
}

release_tag="$(read_result tag)"
release_commit="$(read_result commit)"
release_digest="$(read_result digest)"
release_source="$(read_result source)"

echo "Installing verified School Bell $release_tag..."
BELL_RELEASE_VERSION="$release_tag" \
BELL_RELEASE_COMMIT="$release_commit" \
BELL_RELEASE_DIGEST="$release_digest" \
BELL_INTERFACE_IP="$interface_ip" \
  bash "$release_source/deploy/install.sh"

echo "First installation is complete. Future production updates are available in the web console."
