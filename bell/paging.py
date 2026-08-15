"""Single-stream paging arbitration with cooperative emergency preemption."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

LOGGER = logging.getLogger(__name__)
BusyPolicy = Literal["skip", "queue", "preempt"]


@dataclass(frozen=True, slots=True)
class PageLease:
    acquired: bool
    reason: str
    cancel_event: threading.Event
    token: str | None = None


@dataclass(slots=True)
class _ActivePage:
    token: str
    label: str
    priority: int
    cancel_event: threading.Event


class PageCoordinator:
    """Guarantee one active page and let a higher-priority page stop a lower one safely."""

    def __init__(self, queue_timeout_seconds: float = 30.0) -> None:
        self.queue_timeout_seconds = queue_timeout_seconds
        self._condition = threading.Condition()
        self._active: _ActivePage | None = None

    def snapshot(self) -> dict[str, object] | None:
        with self._condition:
            if self._active is None:
                return None
            return {"label": self._active.label, "priority": self._active.priority}

    @contextmanager
    def lease(self, label: str, priority: int, policy: BusyPolicy):
        lease = self._acquire(label, priority, policy)
        try:
            yield lease
        finally:
            if lease.acquired and lease.token:
                self._release(lease.token)

    def _acquire(self, label: str, priority: int, policy: BusyPolicy) -> PageLease:
        deadline = time.monotonic() + self.queue_timeout_seconds
        with self._condition:
            if self._active is not None and policy == "preempt" and priority > self._active.priority:
                LOGGER.warning(
                    "page_preemption_requested",
                    extra={
                        "new_label": label,
                        "new_priority": priority,
                        "active_label": self._active.label,
                        "active_priority": self._active.priority,
                    },
                )
                self._active.cancel_event.set()
            while self._active is not None:
                if policy == "skip" or (policy == "preempt" and priority <= self._active.priority):
                    return PageLease(
                        False,
                        f"busy with {self._active.label!r} at priority {self._active.priority}",
                        threading.Event(),
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return PageLease(False, "timed out waiting for active page", threading.Event())
                self._condition.wait(remaining)
            token = secrets.token_hex(12)
            cancel_event = threading.Event()
            self._active = _ActivePage(token, label, priority, cancel_event)
            return PageLease(True, "page slot acquired", cancel_event, token)

    def _release(self, token: str) -> None:
        with self._condition:
            if self._active is not None and self._active.token == token:
                self._active = None
                self._condition.notify_all()
