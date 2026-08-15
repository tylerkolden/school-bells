from __future__ import annotations

from bell import systemd


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
