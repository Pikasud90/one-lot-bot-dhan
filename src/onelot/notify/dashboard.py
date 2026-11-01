"""The dashboard channel: an in-process queue the UI polls.

This channel is deliberately the one that cannot fail. It performs no I/O, has
no external dependency and no permission model, so even with every OS
integration broken the alert still reaches you if the dashboard is open.
"""

from __future__ import annotations

import threading
from collections import deque

from .base import Notification


class DashboardChannel:
    name = "dashboard"
    available = True

    def __init__(self, maxlen: int = 200):
        self._items: deque[Notification] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._delivered_seq: dict[str, int] = {}

    def send(self, n: Notification) -> bool:
        with self._lock:
            self._seq += 1
            self._delivered_seq[n.notification_id] = self._seq
            self._items.appendleft(n)
        return True

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [n.as_dict() for n in list(self._items)[:limit]]

    def since(self, seq: int, limit: int = 50) -> tuple[list[dict], int]:
        """Everything newer than `seq`, plus the current sequence number.
        Lets the UI long-poll without replaying alerts it has already shown."""
        with self._lock:
            out = [
                n.as_dict()
                for n in self._items
                if self._delivered_seq.get(n.notification_id, 0) > seq
            ]
            return out[:limit], self._seq

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._seq

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
