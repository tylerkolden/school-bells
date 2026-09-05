from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from bell.alerts import AlertDispatcher
from bell.auth import AuthError, AuthStore
from bell.branding import BrandingError, normalize_logo
from bell.config import load_config
from bell.recovery import (
    RecoveryError,
    create_portable_backup,
    create_support_bundle,
    extract_and_validate_backup,
    restore_portable_backup,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_auth_store_migrates_bootstrap_and_invalidates_old_password(tmp_path: Path) -> None:
    path = tmp_path / "state" / "auth" / "users.json"
    store = AuthStore(path, "bootstrap-only")

    assert store.verify("admin", "bootstrap-only") is not None
    assert store.verify("operator", "bootstrap-only") is None
    store.set_password("admin", "admin", "new-admin-password")

    assert store.verify("admin", "bootstrap-only") is None
    assert store.verify("admin", "new-admin-password").role == "admin"  # type: ignore[union-attr]
    payload = path.read_text(encoding="utf-8")
    assert "new-admin-password" not in payload
    assert json.loads(payload)["revision"] == 1


def test_auth_store_operator_lifecycle_and_password_policy(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "users.json", "bootstrap-only")

    with pytest.raises(AuthError, match="at least 12"):
        store.set_password("operator", "operator", "short")
    store.set_password("operator", "operator", "operator-password")
    assert store.verify("operator", "operator-password").role == "operator"  # type: ignore[union-attr]
    assert store.delete_user("operator") is True
    assert store.delete_user("operator") is False
    with pytest.raises(AuthError, match="cannot be deleted"):
        store.delete_user("admin")


def test_alert_dispatcher_signs_and_deduplicates(config_tree: Path, monkeypatch) -> None:
    settings = load_config(config_tree).settings.model_copy(
        update={
            "alert_webhook_url": "https://alerts.example.test/bell",
            "alert_webhook_secret_env": "BELL_TEST_ALERT_SECRET",
        }
    )
    monkeypatch.setenv("BELL_TEST_ALERT_SECRET", "shared-secret")
    # A freshly booted Linux host can have a monotonic clock below the five-minute
    # deduplication window; the first alert must still be sent.
    monkeypatch.setattr("bell.alerts.time.monotonic", lambda: 10.0)
    captured: list[object] = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        captured.extend((request, timeout))
        return Response()

    monkeypatch.setattr("bell.alerts.open_webhook", fake_urlopen)
    dispatcher = AlertDispatcher(settings)
    outcome = dispatcher.send(
        "delivery_failed", "Everywhere page failed", details={"zone": "everywhere"}
    )

    assert outcome.success is True
    request = captured[0]
    assert captured[1] == 5
    expected = (
        "sha256="
        + hmac.new(
            b"shared-secret",
            request.data,
            hashlib.sha256,  # type: ignore[attr-defined]
        ).hexdigest()
    )
    assert request.get_header("X-bell-signature") == expected  # type: ignore[attr-defined]
    assert (
        dispatcher.send("delivery_failed", "Everywhere page failed").detail
        == "duplicate alert suppressed"
    )
    assert len(captured) == 2


def test_branding_rejects_unknown_content_and_rewrites_allowlisted_image(
    tmp_path: Path, monkeypatch
) -> None:
    invalid = tmp_path / "logo.png"
    invalid.write_text("not an image", encoding="utf-8")
    with pytest.raises(BrandingError, match="valid PNG"):
        normalize_logo(invalid, tmp_path / "output.png")

    source = tmp_path / "source.png"
    source.write_bytes(PNG)
    monkeypatch.setattr("bell.branding.shutil.which", lambda _name: "/usr/bin/ffmpeg")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(PNG)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("bell.branding.subprocess.run", fake_run)
    destination = tmp_path / "state" / "branding" / "logo.png"
    normalize_logo(source, destination)
    assert destination.read_bytes() == PNG


def test_portable_backup_round_trip_and_excludes_credentials(config_tree: Path) -> None:
    config = load_config(config_tree)
    (config.state_path / "branding").mkdir(parents=True)
    (config.state_path / "branding" / "logo.png").write_bytes(PNG)
    settings = (config_tree / "settings.yaml").read_text(encoding="utf-8")
    (config_tree / "settings.yaml").write_text(
        settings.replace("logo_filename:\n", "logo_filename: logo.png\n"), encoding="utf-8"
    )
    (config_tree / "bell.env").write_text("BELL_UI_PASSWORD=do-not-export\n", encoding="utf-8")
    config = load_config(config_tree)
    archive = create_portable_backup(config, config.state_path / "operator-backups")

    with tarfile.open(archive, "r:gz") as source:
        names = source.getnames()
        assert "config/bell.env" not in names
        assert "state/branding/logo.png" in names
        assert "sounds/class-bell.wav" in names
    extracted = config_tree.parent / "extracted"
    restored = extract_and_validate_backup(archive, extracted)
    assert restored.settings.logo_filename == "logo.png"


def test_restore_rolls_back_all_libraries_when_reload_fails(config_tree: Path) -> None:
    original = load_config(config_tree)
    archive = create_portable_backup(original, original.state_path / "operator-backups")
    settings_path = config_tree / "settings.yaml"
    settings_path.write_text(
        settings_path.read_text(encoding="utf-8").replace(
            "school_name: School Bell", "school_name: Current School"
        ),
        encoding="utf-8",
    )
    (original.sounds_path / "site-only.wav").write_bytes(b"site audio")

    calls = 0

    def failed_reload() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("reload rejected")

    with pytest.raises(RecoveryError, match="previous configuration was restored"):
        restore_portable_backup(
            archive,
            config_tree.parent,
            original.state_path / "operator-backups",
            reload_callback=failed_reload,
        )

    assert load_config(config_tree).settings.school_name == "Current School"
    assert (original.sounds_path / "site-only.wav").read_bytes() == b"site audio"
    assert calls == 2


def test_backup_extractor_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        payload = b"owned"
        member = tarfile.TarInfo("../outside.txt")
        member.size = len(payload)
        output.addfile(member, io.BytesIO(payload))

    with pytest.raises(RecoveryError, match="unexpected path"):
        extract_and_validate_backup(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


def test_support_bundle_redacts_configuration_and_log_secrets(config_tree: Path) -> None:
    config = load_config(config_tree)
    config.log_path.mkdir(parents=True)
    (config.log_path / "bell.log").write_text(
        "password=private-value token: token-value harmless=yes\n", encoding="utf-8"
    )
    archive = create_support_bundle(
        config,
        config.state_path / "support-bundles",
        health={"detail": 'token: "health secret value"'},
    )

    with zipfile.ZipFile(archive) as source:
        log = source.read("logs/bell.log.tail.txt").decode()
        summary = json.loads(source.read("support.json"))
    assert "private-value" not in log
    assert "token-value" not in log
    assert log.count("[REDACTED]") == 2
    assert summary["version"]
    assert summary["created_at"].endswith("+00:00")
    assert summary["health"]["detail"] == "token: [REDACTED]"
