from __future__ import annotations

import threading

from bell.paging import PageCoordinator


def test_busy_page_skips_lower_priority() -> None:
    coordinator = PageCoordinator(queue_timeout_seconds=0.2)
    with coordinator.lease("class bell", 50, "skip") as first:
        assert first.acquired
        with coordinator.lease("routine page", 40, "skip") as second:
            assert not second.acquired and "busy" in second.reason


def test_emergency_preempts_cooperatively() -> None:
    coordinator = PageCoordinator(queue_timeout_seconds=2)
    released = threading.Event()

    def routine() -> None:
        with coordinator.lease("routine", 40, "skip") as lease:
            assert lease.acquired
            lease.cancel_event.wait(2)
        released.set()

    thread = threading.Thread(target=routine)
    thread.start()
    while coordinator.snapshot() is None:
        released.wait(0.01)
    with coordinator.lease("emergency", 100, "preempt") as emergency:
        assert emergency.acquired
        assert released.wait(1)
    thread.join(timeout=2)
