from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from bell.update import UpdateRequestError, load_update_status, queue_update_request

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ota_updater", ROOT / "deploy" / "ota_updater.py")
assert SPEC and SPEC.loader
ota = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ota)


def release(tag: str = "v0.2.0", *, immutable: bool = True) -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "immutable": immutable,
        "published_at": "2026-08-15T12:00:00Z",
        "html_url": f"https://github.com/tylerkolden/school-bells/releases/tag/{tag}",
        "body": "Security and reliability improvements",
        "author": {"login": "github-actions[bot]"},
        "assets": [
            {
                "id": 42,
                "name": f"bell-system-{tag}.tar.gz",
                "size": 1234,
                "digest": "sha256:" + "a" * 64,
                "uploader": {"login": "github-actions[bot]"},
            }
        ],
    }


def test_web_queue_is_fixed_schema_and_does_not_overwrite(tmp_path: Path) -> None:
    state = tmp_path / "state"
    request_id = queue_update_request(state, "check")
    request = json.loads((state / "update" / "request.json").read_text(encoding="utf-8"))
    assert request == {
        "schema": 1,
        "id": request_id,
        "action": "check",
        "requested_at": request["requested_at"],
    }
    with pytest.raises(UpdateRequestError, match="already queued"):
        queue_update_request(state, "check")


def test_status_reader_fails_closed_on_invalid_or_oversized_data(tmp_path: Path) -> None:
    state = tmp_path / "state"
    update = state / "update"
    update.mkdir(parents=True)
    assert load_update_status(state)["phase"] == "idle"
    (update / "status.json").write_text("[]", encoding="utf-8")
    with pytest.raises(UpdateRequestError, match="invalid"):
        load_update_status(state)
    (update / "status.json").write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(UpdateRequestError, match="large"):
        load_update_status(state)


def test_release_policy_requires_stable_immutable_workflow_asset() -> None:
    client = ota.GitHubReleases()
    checked = client._validated_release(release())
    assert checked["tag"] == "v0.2.0"
    assert checked["digest"] == "sha256:" + "a" * 64
    with pytest.raises(ota.UpdateError, match="not immutable"):
        client._validated_release(release(immutable=False))
    untrusted = release()
    untrusted["author"] = {"login": "repository-owner"}
    with pytest.raises(ota.UpdateError, match="approved GitHub Actions"):
        client._validated_release(untrusted)
    with pytest.raises(ota.UpdateError, match="stable semantic version"):
        ota.parse_version("main")


def test_safe_extract_rejects_links_and_path_escape(tmp_path: Path) -> None:
    for name, member in (
        ("link", tarfile.TarInfo("deploy/link")),
        ("escape", tarfile.TarInfo("../escape")),
    ):
        archive = tmp_path / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            if name == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
            else:
                member.size = 1
            output.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
        destination = tmp_path / name
        destination.mkdir()
        with pytest.raises(ota.UpdateError, match=r"unsafe path|link or special"):
            ota._safe_extract(archive, destination)


def test_safe_extract_accepts_regular_release_manifest(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    manifest = json.dumps({"tag": "v0.2.0", "commit": "2" * 40}).encode()
    installer = b"#!/usr/bin/env bash\n"
    with tarfile.open(archive, "w:gz") as output:
        for name, content in (("RELEASE.json", manifest), ("deploy/install.sh", installer)):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    destination = tmp_path / "expanded"
    destination.mkdir()
    assert ota._safe_extract(archive, destination)["tag"] == "v0.2.0"
    assert (destination / "deploy" / "install.sh").read_bytes() == installer


def test_root_check_request_publishes_only_newer_release(tmp_path: Path, monkeypatch) -> None:
    app = tmp_path / "app"
    (app / "state").mkdir(parents=True)
    (app / "RELEASE.json").write_text(
        json.dumps({"tag": "v0.1.0", "version": "0.1.0", "commit": "1" * 40}),
        encoding="utf-8",
    )
    queue_update_request(app / "state", "check")
    request_path = app / "state" / "update" / "request.json"
    monkeypatch.setattr(ota, "_bell_service_uid", lambda: request_path.stat().st_uid)
    validated = ota.GitHubReleases()._validated_release(release())

    class FakeClient:
        def __init__(self, _repository: str) -> None:
            pass

        def latest(self):
            return validated

    monkeypatch.setattr(ota, "GitHubReleases", FakeClient)
    assert ota.process_request(
        app_dir=app,
        updater_dir=tmp_path / "updater",
        repository="tylerkolden/school-bells",
        guard_seconds=900,
    ) == 0
    status = load_update_status(app / "state")
    assert status["phase"] == "update_available"
    assert status["release"]["tag"] == "v0.2.0"
    assert not (app / "state" / "update" / "request.json").exists()


def test_install_request_rechecks_digest_and_refuses_changed_release(
    tmp_path: Path, monkeypatch
) -> None:
    app = tmp_path / "app"
    (app / "state").mkdir(parents=True)
    (app / "RELEASE.json").write_text(
        json.dumps({"tag": "v0.1.0", "version": "0.1.0", "commit": "1" * 40}),
        encoding="utf-8",
    )
    queue_update_request(
        app / "state", "install", tag="v0.2.0", digest="sha256:" + "a" * 64
    )
    request_path = app / "state" / "update" / "request.json"
    monkeypatch.setattr(ota, "_bell_service_uid", lambda: request_path.stat().st_uid)
    changed = ota.GitHubReleases()._validated_release(release())
    changed["digest"] = "sha256:" + "b" * 64

    class FakeClient:
        def __init__(self, _repository: str) -> None:
            pass

        def by_tag(self, _tag: str):
            return changed

    monkeypatch.setattr(ota, "GitHubReleases", FakeClient)
    assert ota.process_request(
        app_dir=app,
        updater_dir=tmp_path / "updater",
        repository="tylerkolden/school-bells",
        guard_seconds=900,
    ) == 1
    status = load_update_status(app / "state")
    assert status["phase"] == "failed"
    assert "changed after confirmation" in status["message"]


def test_failed_installer_restores_every_root_managed_file(tmp_path: Path, monkeypatch) -> None:
    payload = b"verified archive"
    digest = "sha256:" + __import__("hashlib").sha256(payload).hexdigest()
    release_data = {
        "tag": "v0.2.0",
        "version": "0.2.0",
        "digest": digest,
        "asset_name": "bell-system-v0.2.0.tar.gz",
        "size": len(payload),
    }

    class FakeClient:
        def download(self, _release, destination: Path) -> None:
            destination.write_bytes(payload)

    managed = tuple(tmp_path / "managed" / name for name in ("bell.service", "update.service"))
    for path in managed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old")
    monkeypatch.setattr(ota, "MANAGED_PATHS", managed)

    def fake_extract(_archive: Path, destination: Path):
        installer = destination / "deploy" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")
        return {"tag": "v0.2.0", "commit": "2" * 40}

    def fake_run(command, **_kwargs):
        if command[0] == "/usr/bin/bash":
            for path in managed:
                path.write_bytes(b"new")
            raise ota.UpdateError("staged validation failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ota, "_safe_extract", fake_extract)
    monkeypatch.setattr(ota, "_run", fake_run)
    with pytest.raises(ota.UpdateError, match="staged validation failed"):
        ota.install_release(
            FakeClient(),
            release_data,
            app_dir=tmp_path / "app",
            updater_dir=tmp_path / "updater",
        )
    assert [path.read_bytes() for path in managed] == [b"old", b"old"]


def test_maintenance_window_blocks_an_active_page(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def read(self, _size: int = -1) -> bytes:
            return json.dumps(
                {
                    "ready": True,
                    "active_page": {"label": "Emergency message", "priority": 100},
                }
            ).encode()

    monkeypatch.setattr(ota.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ota.UpdateError, match="page is active"):
        ota._check_maintenance_window(900)
