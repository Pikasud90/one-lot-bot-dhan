"""Notification value types, the channel protocol and shared display rules."""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol


def shown_confidence(score: object) -> int | None:
    """The confidence number every surface shows (the desk SIGNAL title, the phone).

    The score floored to a whole number and clamped to 0..100, so the number shown never
    crosses the label thresholds (high >= 65, low < 45: 64.9 shows 64, never a 'medium'
    65). None when the score is missing or not a finite number (bools included). The
    comparisons are exact, never through float, so an int or a Decimal beyond float range
    (10**400) is clamped like any other, not an OverflowError.
    """
    if isinstance(score, bool) or not isinstance(score, (int, float, Decimal)):
        return None
    if isinstance(score, float) and not math.isfinite(score):
        return None
    if isinstance(score, Decimal) and not score.is_finite():
        return None
    if score >= 100:
        return 100
    if score <= 0:
        return 0
    return math.floor(score)


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
