"""Notification value types and the channel protocol."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class NotificationLevel(str, Enum):
    INFO = "INFO"
    SIGNAL = "SIGNAL"  # a tradeable signal — the highest-value alert
    WARNING = "WARNING"
    EXIT = "EXIT"  # close a position NOW
    CRITICAL = "CRITICAL"  # halt, token expired, feed dead

    @property
    def is_urgent(self) -> bool:
        return self in (
            NotificationLevel.SIGNAL,
            NotificationLevel.EXIT,
            NotificationLevel.CRITICAL,
        )


@dataclass
class Notification:
    title: str
    body: str
    level: NotificationLevel = NotificationLevel.INFO
    created_at: dt.datetime | None = None
    notification_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    signal_id: str | None = None
    position_id: str | None = None
    ttl_seconds: int | None = None
    sound: bool = True
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "notification_id": self.notification_id,
            "title": self.title,
            "body": self.body,
            "level": self.level.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "signal_id": self.signal_id,
            "position_id": self.position_id,
            "ttl_seconds": self.ttl_seconds,
            "sound": self.sound,
            "payload": dict(self.payload),
        }


class Channel(Protocol):
    name: str
    available: bool

    def send(self, n: Notification) -> bool: ...
