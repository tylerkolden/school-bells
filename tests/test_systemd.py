from __future__ import annotations

from pathlib import Path

from bell import systemd

ROOT = Path(__file__).resolve().parents[1]


def test_watchdog_interval_uses_half_systemd_timeout(monkeypatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")
    assert systemd.watchdog_interval() == 30.0
    monkeypatch.setenv("WATCHDOG_USEC", "invalid")
    assert systemd.watchdog_interval(12.0) == 12.0


def test_notify_is_optional_and_supports_abstract_socket(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert systemd.notify("READY=1") is False
    sent: list[tuple[bytes, str]] = []

    class FakeSocket:
        def __init__(self, *_args) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def sendto(self, message: bytes, address: str) -> None:
            sent.append((message, address))

    monkeypatch.setenv("NOTIFY_SOCKET", "@bell-notify")
    monkeypatch.setattr(systemd.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(systemd.socket, "socket", FakeSocket)
    assert systemd.notify("READY=1") is True
    assert sent == [(b"READY=1", "\0bell-notify")]


def test_update_unit_has_fixed_privilege_boundary() -> None:
    service = (ROOT / "deploy" / "bell-update.service").read_text(encoding="utf-8")
    path = (ROOT / "deploy" / "bell-update.path").read_text(encoding="utf-8")
    assert "User=root" in service
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/bell-system/ota_updater.py process" in service
    assert "ProtectSystem=strict" in service
    assert "NoNewPrivileges=yes" in service
    assert "--repository" not in service
    assert "PathExists=/opt/bell/state/update/request.json" in path


def test_installer_stages_before_atomic_activation_and_supports_offline_wheels() -> None:
    installer = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    embedded_interface = installer.split("<<'PY_INTERFACE'\n", maxsplit=1)[1].split(
        "\nPY_INTERFACE", maxsplit=1
    )[0]
    compile(embedded_interface, "deploy/install.sh:PY_INTERFACE", "exec")
    preflight = installer.index("--check-only --config-dir")
    activation = installer.index('mv -Tf -- "$new_link" "$APP_DIR/current"')
    restart = installer.index("systemctl restart bell-system.service")
    assert preflight < activation < restart
    assert "--no-index --find-links" in installer
    assert "cp311|cp312|cp313" in installer
    assert 'wheelhouse/$runtime_abi' in installer
    assert "requires 64-bit ARM" in installer
    assert '[[ "$fresh_config" -eq 1 && -n "$interface_override" ]]' in installer
    assert "settings.yaml must contain exactly one interface_ip setting" in installer
    unit = (ROOT / "deploy" / "bell-system.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/bell/current" in unit
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in unit
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in unit
    assert "CAP_NET_RAW" not in unit
    assert "CAP_NET_ADMIN" not in unit
    assert "CAP_SYS_ADMIN" not in unit
