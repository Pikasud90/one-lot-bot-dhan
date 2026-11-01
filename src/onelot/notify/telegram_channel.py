"""The Notifier's Telegram channel (US-004; ADR-002 amendments 2, 3, 6 and 9).

What reaches the phone and when: the levels `notify.telegram_levels` allows, silent or not,
and the EXIT reminder budget. Delivery itself is the outbox's job (.telegram_outbox); the
channel only ever does one INSERT through `TelegramOutbox.enqueue`.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..clock import now_ist
from ..config import NotifyCfg
from .base import Notification, NotificationLevel
from .telegram import TelegramClient, _plain
from .telegram_outbox import TelegramOutbox, _time_of

log = logging.getLogger(__name__)

EXIT_REMINDER_EVERY = dt.timedelta(minutes=5)
EXIT_MAX_REMINDERS = 3  # after the first EXIT message
_PHONE_LEVELS = frozenset(
    (NotificationLevel.SIGNAL, NotificationLevel.EXIT, NotificationLevel.CRITICAL)
)


@dataclass
class _ExitEpisode:
    """One EXIT advice for one position: the messages sent for it and when."""

    advice: str
    messages: int  # the first EXIT plus reminders so far
    last_message_at: dt.datetime


class TelegramChannel:
    """The Notifier's Telegram channel: decides what goes to the phone and queues it.

    `send` is ONE short INSERT into the outbox (the Desk holds its lock while it runs):
    no HTTP, no Notifier call. SIGNAL, EXIT and CRITICAL only, as `notify.telegram_levels`
    allows. Silent (no sound, never dropped): a risk-blocked SIGNAL always; SIGNAL and EXIT
    outside the session when `notify.telegram_quiet_outside_session`; CRITICAL never.

    EXIT advice is raised on every Desk poll. The phone gets the first message at once,
    then at most EXIT_MAX_REMINDERS reminders, each EXIT_REMINDER_EVERY after the previous
    message. A different advice for the position starts a fresh budget; a restart starts
    one too (the budget is in memory, accepted). Each message is its own row with alert_id
    '{position_id}#n'; n continues across restarts, so a new message never collides with an
    old row.
    """

    name = "telegram"
    deferred = True  # True from send() means "queued": the outbox reports what was sent

    def __init__(
        self,
        client: TelegramClient,
        outbox: TelegramOutbox,
        cfg: NotifyCfg,
        in_session: Callable[[dt.datetime], bool],
        clock: Callable[[], dt.datetime] = now_ist,
    ) -> None:
        self._client = client
        self._outbox = outbox
        self._cfg = cfg
        self._levels = frozenset(cfg.telegram_levels)
        self._in_session = in_session
        self._clock = clock
        self._lock = threading.Lock()  # EXIT episodes; never held by the worker
        self._episodes: dict[str, _ExitEpisode] = {}
        self._exit_numbers = self._load_exit_numbers()

    def _load_exit_numbers(self) -> dict[str, int]:
        try:
            return self._outbox.exit_sequences()
        except Exception as exc:  # an unreadable table: numbering restarts, logged
            log.warning("telegram channel: EXIT numbering not restored (%s)", type(exc).__name__)
            return {}

    @property
    def available(self) -> bool:
        return self._client.available

    def accepts(self, n: Notification) -> bool:
        return n.level in _PHONE_LEVELS and n.level.value in self._levels

    def status(self) -> dict[str, Any]:
        return self._outbox.status()

    def send(self, n: Notification) -> bool:
        if not self.accepts(n):
            return False
        if n.level is NotificationLevel.SIGNAL:
            return self._send_signal(n)
        if n.level is NotificationLevel.EXIT:
            return self._send_exit(n)
        return self._send_critical(n)

    # ── per level ──────────────────────────────────────────────────────────
    def _silent(self, n: Notification, now: dt.datetime, blocked: bool = False) -> bool:
        if n.level is NotificationLevel.CRITICAL:
            return False
        if blocked:
            return True
        if not self._cfg.telegram_quiet_outside_session:
            return False
        try:
            open_now = bool(self._in_session(now))
        except Exception as exc:  # an unknown session never mutes an alert
            log.warning("telegram channel: session check failed (%s)", type(exc).__name__)
            open_now = True
        return not open_now

    def _send_signal(self, n: Notification) -> bool:
        now = self._clock()
        alert = n.payload if isinstance(n.payload, Mapping) else {}
        alert_id = n.signal_id or _plain(alert.get("id")) or n.notification_id
        created = _time_of(alert.get("created_at")) or n.created_at or now
        expires = _time_of(alert.get("expires_at"))
        if expires is None and n.ttl_seconds:
            expires = created + dt.timedelta(seconds=n.ttl_seconds)
        return self._outbox.enqueue(
            dedup_key=n.dedup_key or f"signal:{alert_id}",
            kind="alert",
            alert_id=alert_id,
            payload=alert,
            alert_created_at=created,
            expires_at=expires,
            silent=self._silent(n, now, blocked=alert.get("risk_allowed") is False),
        )

    def _send_exit(self, n: Notification) -> bool:
        now = self._clock()
        event = n.payload if isinstance(n.payload, Mapping) else {}
        position = event.get("position")
        pid = (
            n.position_id
            or (_plain(position.get("id")) if isinstance(position, Mapping) else "")
            or n.notification_id
        )
        advice = _plain(event.get("advice"))
        with self._lock:
            episode = self._episodes.get(pid)
            if episode is not None and episode.advice != advice:
                episode = None  # a different advice: a fresh budget, sent at once
            if episode is not None:
                if episode.messages > EXIT_MAX_REMINDERS:
                    return True  # first + every reminder already queued: nothing to do
                if now - episode.last_message_at < EXIT_REMINDER_EVERY:
                    return True  # not yet: the previous message is recent
            number = self._exit_numbers.get(pid, 0) + 1
            queued = self._outbox.enqueue(
                dedup_key=f"{n.dedup_key or 'exit:' + pid}#{number}",
                kind="exit",
                alert_id=f"{pid}#{number}",
                payload=event,
                alert_created_at=n.created_at or now,
                expires_at=None,
                silent=self._silent(n, now),
            )
            if queued:
                self._exit_numbers[pid] = number
                sent_before = 0 if episode is None else episode.messages
                self._episodes[pid] = _ExitEpisode(advice, sent_before + 1, now)
            return queued

    def _send_critical(self, n: Notification) -> bool:
        now = self._clock()
        return self._outbox.enqueue(
            dedup_key=n.dedup_key or f"critical:{n.notification_id}",
            kind="critical",
            alert_id=n.notification_id,
            payload={"title": n.title, "body": n.body},
            alert_created_at=n.created_at or now,
            expires_at=None,
            silent=False,
        )
