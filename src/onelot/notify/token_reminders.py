"""Reminders before the Dhan token expires (US-087).

The owner renews the Dhan access token by hand or the desk renews it by itself (TOTP). Either
way a missed renewal switches the live data off, so the desk says so ahead of time, on the
page, the computer and the phone:

    60, 30, 15 and 5 minutes before the expiry, then "expired", then (while still expired) one
    reminder at 08:30 IST on each trading day, and one message when the token is renewed.
    When the desk first looks later that trading day (it was started after 09:15, or the laptop
    slept), the reminder is sent at that first look, once that day, until the market closes.

Two parts, kept apart so the ladder can be tested without a clock or a database:

* `decide` is pure: (what was remembered, the expiry now read, the time, whether today is a
  trading day) -> (what to remember now, the reminders due now);
* `TokenReminderStep` remembers in the database (`token_reminder_state`, so a restart repeats
  nothing), turns each reminder into plain words and sends it through the Notifier.

Rules: each stage fires once per token (the expiry instant and the stage name the message);
a start or a first reading inside a stage's window sends only the nearest stage not yet sent,
with the true time left, never a burst of older ones; an unreadable expiry is reported once.
Nothing here ever holds or prints the token itself.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from sqlalchemy import Engine, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..clock import IST, to_ist
from ..store.schema import token_reminder_state as STATE
from .base import Notification, NotificationLevel

log = logging.getLogger(__name__)

STAGE_MINUTES = (60, 30, 15, 5)
MORNING_FROM = dt.time(8, 30)  # IST: the reminder on a trading day while still expired
MARKET_OPEN = dt.time(9, 15)  # IST: the morning reminder says "before 09:15"
MORNING_UNTIL = dt.time(15, 30)  # IST: a first look until the close still sends it (US-097)
MIN_STEP_SECONDS = 55.0  # the step runs from a loop that ticks faster than once a minute
KIND = "token"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EXPIRED = 0  # the stage number "expired" is remembered under

ReminderKind = Literal["stage", "expired", "morning", "renewed", "unreadable"]


@dataclass(frozen=True)
class ReminderState:
    """What the ladder remembers between two looks."""

    expiry: dt.datetime | None = None  # the expiry instant the stages below belong to
    sent: frozenset[int] = field(default_factory=frozenset)  # stage minutes (0: expired)
    unreadable_told: bool = False
    morning_day: str | None = None  # the IST date of the last "expired" message


@dataclass(frozen=True)
class Reminder:
    kind: ReminderKind
    stage: int | None  # 60, 30, 15 or 5 for a stage
    minutes_left: int | None  # the true time left, rounded up to a whole minute
    key: str  # the durable dedup key of the message
    late: bool = False  # a morning reminder sent after 09:15: the market is already open


def _iso(moment: dt.datetime) -> str:
    return to_ist(moment).isoformat(timespec="minutes")


def decide(
    state: ReminderState,
    expiry: dt.datetime | None,
    now: dt.datetime,
    trading_day: bool,
) -> tuple[ReminderState, list[Reminder]]:
    """The reminders due at `now`, and the state to remember. Pure.

    `expiry` is None when the desk cannot read it. `trading_day` is whether today (IST) is an
    NSE trading day; it only matters for the 08:30 reminder."""
    now = to_ist(now)
    if expiry is None:
        if state.unreadable_told:
            return state, []
        key = f"token:unreadable:{_iso(now)}"
        return replace(state, unreadable_told=True), [Reminder("unreadable", None, None, key)]

    expiry = to_ist(expiry)
    out: list[Reminder] = []
    state = replace(state, unreadable_told=False)
    if state.expiry is None or expiry != state.expiry:
        if state.expiry is not None and expiry > state.expiry:  # a new token: re-arm and tell
            out.append(Reminder("renewed", None, None, f"token:{_iso(expiry)}:renewed"))
        state = replace(state, expiry=expiry, sent=frozenset(), morning_day=None)

    left = (expiry - now).total_seconds()
    today = now.date().isoformat()
    sent = set(state.sent)
    if left <= 0:
        if _EXPIRED not in sent:
            out.append(Reminder("expired", None, None, f"token:{_iso(expiry)}:expired"))
            sent |= {_EXPIRED, *STAGE_MINUTES}
            state = replace(state, morning_day=today)  # that message is today's reminder
        elif (
            trading_day
            and MORNING_FROM <= now.time().replace(tzinfo=None) < MORNING_UNTIL
            and state.morning_day != today
        ):
            late = now.time().replace(tzinfo=None) >= MARKET_OPEN
            key = f"token:{_iso(expiry)}:morning:{today}"
            out.append(Reminder("morning", None, None, key, late=late))
            state = replace(state, morning_day=today)
    else:
        due = [m for m in STAGE_MINUTES if left <= m * 60]
        if due and min(due) not in sent:
            nearest = min(due)
            left_min = max(1, math.ceil(left / 60))
            out.append(Reminder("stage", nearest, left_min, f"token:{_iso(expiry)}:{nearest}"))
            sent |= set(due)  # the older stages of the same window are skipped, not sent late
    return replace(state, sent=frozenset(sent)), out


# ── plain words ──────────────────────────────────────────────────────────────
def when_text(moment: dt.datetime) -> str:
    """'21:31 on 25 Nov' (IST)."""
    t = to_ist(moment)
    return f"{t:%H:%M} on {t.day} {_MONTHS[t.month - 1]}"


def _ist_text(moment: dt.datetime) -> str:
    """'21:31 IST on 25 Nov'."""
    t = to_ist(moment)
    return f"{t:%H:%M} IST on {t.day} {_MONTHS[t.month - 1]}"


def _minutes(n: int) -> str:
    return f"{n} minute{'' if n == 1 else 's'}"


def reminder_text(
    reminder: Reminder, expiry: dt.datetime | None, can_self_renew: bool
) -> tuple[str, str]:
    """(title, body) in plain words. The expiry is shown in IST. No token, ever."""
    do = "Settings, Dhan card: Renew, or paste a new token."
    tries = " The desk will also try to renew it by itself." if can_self_renew else ""
    at = _ist_text(expiry) if expiry is not None else "an unknown time"
    until = when_text(expiry) if expiry is not None else "an unknown time"
    if reminder.kind == "stage":
        left = _minutes(reminder.minutes_left or 1)
        return (
            f"Dhan token: {left} left",
            f"It expires at {at}. Renew it now: {do}{tries}",
        )
    if reminder.kind == "expired":
        return (
            "Dhan token expired",
            f"It ran out at {at}. The live data stops until it is renewed: {do}{tries}",
        )
    if reminder.kind == "morning" and reminder.late:
        return (
            "Dhan token is still expired",
            f"The market is open and the live data is off. Renew it now: {do}{tries}",
        )
    if reminder.kind == "morning":
        return (
            "Dhan token is still expired",
            f"Renew it before 09:15, when the market opens: {do}{tries}",
        )
    if reminder.kind == "renewed":
        return (
            f"Dhan token renewed: valid until {until}",
            "The reminders start again 60, 30, 15 and 5 minutes before it runs out.",
        )
    return (
        "Cannot read when the Dhan token expires",
        "The desk cannot see the token's end time, so it cannot count down. Open Settings, "
        "Dhan card, and refresh the account. It still tells you when the token has expired.",
    )


def _payload(reminder: Reminder, expiry: dt.datetime | None) -> dict[str, object]:
    """What rides with the message. A stage reminder is worthless once the token has run out
    ("5 minutes left" an hour late would mislead), so it carries `valid_until`: the channel
    queues it with that deadline."""
    out: dict[str, object] = {"stage": reminder.stage, "reminder": reminder.kind}
    if reminder.kind == "stage" and expiry is not None:
        out["valid_until"] = expiry.isoformat()
    return out


# ── memory ───────────────────────────────────────────────────────────────────
class ReminderMemory:
    """`ReminderState` in the table `token_reminder_state` (one row per fact)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self) -> ReminderState:
        with self._engine.connect() as conn:
            rows = {r[0]: r[1] for r in conn.execute(STATE.select())}
        expiry: dt.datetime | None = None
        if rows.get("expiry"):
            try:
                expiry = dt.datetime.fromisoformat(rows["expiry"]).astimezone(IST)
            except ValueError:
                expiry = None
        try:
            sent = frozenset(int(x) for x in json.loads(rows.get("sent", "[]")))
        except (ValueError, TypeError):
            sent = frozenset()
        return ReminderState(
            expiry=expiry,
            sent=sent,
            unreadable_told=rows.get("unreadable_told") == "1",
            morning_day=rows.get("morning_day") or None,
        )

    def save(self, state: ReminderState) -> None:
        values = {
            "expiry": state.expiry.isoformat() if state.expiry else "",
            "sent": json.dumps(sorted(state.sent)),
            "unreadable_told": "1" if state.unreadable_told else "0",
            "morning_day": state.morning_day or "",
        }
        with self._engine.begin() as conn:
            conn.execute(delete(STATE))
            conn.execute(sqlite_insert(STATE), [{"name": k, "value": v} for k, v in values.items()])


class TokenReminderStep:
    """One look at the token: read the expiry, decide, remember, and send what is due.

    `look` is called from a loop that already runs about once a minute (the Engine's
    housekeeping loop); it does nothing when called again within MIN_STEP_SECONDS."""

    def __init__(
        self,
        memory: ReminderMemory,
        send: Callable[[Notification], object],
        read_expiry: Callable[[], dt.datetime | None],
        is_trading_day: Callable[[dt.date], bool],
        can_self_renew: Callable[[], bool],
    ) -> None:
        self._memory = memory
        self._send = send
        self._read_expiry = read_expiry
        self._is_trading_day = is_trading_day
        self._can_self_renew = can_self_renew
        self._last_look: dt.datetime | None = None

    def look(self, now: dt.datetime, *, force: bool = False) -> list[Reminder]:
        if (
            not force
            and self._last_look is not None
            and 0 <= (now - self._last_look).total_seconds() < MIN_STEP_SECONDS
        ):
            return []
        self._last_look = now
        expiry = self._read_expiry()
        state = self._memory.load()
        if expiry is None and state.expiry is not None and state.expiry <= now:
            expiry = state.expiry  # the profile went unreadable because the token ran out
        new_state, due = decide(state, expiry, now, self._is_trading_day(to_ist(now).date()))
        if new_state != state:
            self._memory.save(new_state)  # first: a restart must never repeat a message
        for reminder in due:
            title, body = reminder_text(reminder, expiry, self._can_self_renew())
            try:
                self._send(
                    Notification(
                        title=title,
                        body=body,
                        level=NotificationLevel.WARNING,
                        kind=KIND,
                        dedup_key=reminder.key,
                        payload=_payload(reminder, expiry),
                    )
                )
            except Exception as exc:  # the class only; a reminder never stops the loop
                log.warning("token reminder not sent (%s)", type(exc).__name__)
        return due
