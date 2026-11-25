"""When the desk ran, and the messages that tell the owner it is up (US-097).

The desk was not running on 5 of the 7 sessions after the micro-trend rule went live (16 to
20 Nov 2026) and nothing recorded it. So:

* `UptimeRecorder.beat` writes a heartbeat, at most one small write every `BEAT_EVERY`: one row
  per calendar day in `desk_uptime` (first and last beat, beats, the minutes of market hours
  the beats covered, started late or not). A restart continues the day's row.
* `coverage` gives, per trading day of the app's calendar, from when to when the desk ran
  inside market hours and the share of the session it covered.
* `DeskMessages` sends, through the Notifier (kind `desk`, which the Telegram channel
  forwards), "Desk started 09:02. Token valid until 21:31. Feed: live." at a start, with
  "Started late: alerts before 10:57 were missed." when the start is after 09:15 on a trading
  day, and the same three facts at 09:05 IST on a trading day when the desk is up; each once
  a day by its own dedup key. Nothing can be sent while the laptop sleeps or the desk is
  stopped: a missing 09:05 message means the desk is down.

Every step here is guarded by its caller: a failure never stops the desk or an alert.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..clock import SessionClock, to_ist
from ..notify.base import Notification, NotificationLevel
from ..store.schema import desk_uptime

log = logging.getLogger(__name__)

BEAT_EVERY = dt.timedelta(minutes=5)
#: two beats further apart than this are a gap (the laptop slept, the desk was stopped): the
#: minutes between them are not counted as covered
GAP = dt.timedelta(minutes=10)
MORNING_AT = dt.time(9, 5)
MORNING_UNTIL = dt.time(9, 15)
FULL_SHARE = 0.9  # a session covered less than this is named on Home
KIND = "desk"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _overlap_minutes(a: dt.datetime, b: dt.datetime, lo: dt.datetime, hi: dt.datetime) -> float:
    start, end = max(a, lo), min(b, hi)
    return max((end - start).total_seconds() / 60.0, 0.0)


class UptimeRecorder:
    """The heartbeat. `beat(now)` is cheap to call often: it writes at most every BEAT_EVERY."""

    def __init__(
        self,
        engine: Engine,
        session_clock: SessionClock,
        is_trading_day: Callable[[dt.date], bool],
    ) -> None:
        self.engine = engine
        self.session_clock = session_clock
        self.is_trading_day = is_trading_day
        self._last: dt.datetime | None = None  # the last beat this process wrote
        self.writes = 0
        desk_uptime.create(engine, checkfirst=True)

    def beat(self, now: dt.datetime) -> bool:
        """Record that the desk is running at `now`; True when a row was written."""
        now = to_ist(now)
        last = self._last
        if last is not None and to_ist(last).date() == now.date() and now - last < BEAT_EVERY:
            return False
        day = now.date()
        window = self.session_clock.window_for(day)
        trading = bool(self.is_trading_day(day))
        covered = 0.0
        if trading and last is not None and to_ist(last).date() == day and now - last <= GAP:
            covered = _overlap_minutes(last, now, window.open_at, window.close_at)
        late = trading and window.open_at < now < window.close_at
        with self.engine.begin() as conn:
            row = conn.execute(
                select(desk_uptime).where(desk_uptime.c.day == day.isoformat())
            ).first()
            if row is None:
                conn.execute(
                    sqlite_insert(desk_uptime).values(
                        day=day.isoformat(),
                        first_seen=now,
                        last_seen=now,
                        beats=1,
                        session_minutes=covered,
                        started_late=late,
                    )  # fmt: skip
                )
            else:
                conn.execute(
                    desk_uptime.update()
                    .where(desk_uptime.c.day == day.isoformat())
                    .values(
                        last_seen=now,
                        beats=int(row.beats or 0) + 1,
                        session_minutes=float(row.session_minutes or 0.0) + covered,
                    )
                )
        self._last = now
        self.writes += 1
        return True


@dataclass(frozen=True)
class DayCover:
    day: dt.date
    known: bool  # False before the first day the desk ever recorded: nothing can be said
    ran_from: dt.datetime | None  # inside market hours
    ran_to: dt.datetime | None
    share: float  # of the session (of the session so far, for today)
    started_late: bool
    finished: bool  # the session is over

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "known": self.known,
            "ran_from": f"{self.ran_from:%H:%M}" if self.ran_from else None,
            "ran_to": f"{self.ran_to:%H:%M}" if self.ran_to else None,
            "share": round(self.share, 3),
            "missed": self.known and self.finished and self.share <= 0.0,
            "started_late": self.started_late,
            "finished": self.finished,
        }


def coverage(
    engine: Engine,
    session_clock: SessionClock,
    is_trading_day: Callable[[dt.date], bool],
    now: dt.datetime,
    days: int = 30,
) -> list[DayCover]:
    """Per trading day of the last `days` calendar days (today included), oldest first."""
    now = to_ist(now)
    first_day = now.date() - dt.timedelta(days=max(int(days), 1) - 1)
    with engine.connect() as conn:
        rows = {r.day: r for r in conn.execute(select(desk_uptime))}
    earliest = min((dt.date.fromisoformat(d) for d in rows), default=None)
    out: list[DayCover] = []
    d = first_day
    while d <= now.date():
        if is_trading_day(d):
            w = session_clock.window_for(d)
            finished = now >= w.close_at
            length = (w.close_at - w.open_at).total_seconds() / 60.0
            if not finished:
                length = max((min(now, w.close_at) - w.open_at).total_seconds() / 60.0, 0.0)
            r = rows.get(d.isoformat())
            ran_from = ran_to = None
            share = 0.0
            late = False
            if r is not None:
                a, b = to_ist(r.first_seen), to_ist(r.last_seen)
                if b >= w.open_at and a <= w.close_at:
                    ran_from, ran_to = max(a, w.open_at), min(b, w.close_at)
                share = min(float(r.session_minutes or 0.0) / length, 1.0) if length > 0 else 0.0
                late = bool(r.started_late)
            known = earliest is not None and d >= earliest
            out.append(DayCover(d, known, ran_from, ran_to, share, late, finished))
        d += dt.timedelta(days=1)
    return out


# ── the messages ─────────────────────────────────────────────────────────────
def _until(expiry: dt.datetime | None, now: dt.datetime) -> str:
    if expiry is None:
        return "Token: expiry not known."
    e, n = to_ist(expiry), to_ist(now)
    when = f"{e:%H:%M}" if e.date() == n.date() else f"{e:%H:%M} on {e.day} {_MONTHS[e.month - 1]}"
    if e <= n:
        return f"Token expired at {when}."
    return f"Token valid until {when}."


def facts_text(head: str, at: dt.datetime, expiry: dt.datetime | None, feed: str) -> str:
    """'Desk started 09:02. Token valid until 21:31. Feed: live.'"""
    return f"{head} {to_ist(at):%H:%M}. {_until(expiry, at)} Feed: {feed}."


def start_text(
    at: dt.datetime, expiry: dt.datetime | None, feed: str, late: bool
) -> tuple[str, str]:
    body = facts_text("Desk started", at, expiry, feed)
    if late:
        body += f" Started late: alerts before {to_ist(at):%H:%M} were missed."
    return ("Desk started late" if late else "Desk started"), body


class DeskMessages:
    """The start message and the 09:05 check, each once a day (dedup key per day)."""

    def __init__(
        self,
        send: Callable[[Notification], object],
        session_clock: SessionClock,
        is_trading_day: Callable[[dt.date], bool],
        read_expiry: Callable[[], dt.datetime | None],
        read_feed: Callable[[], str],
    ) -> None:
        self._send = send
        self.session_clock = session_clock
        self.is_trading_day = is_trading_day
        self._read_expiry = read_expiry
        self._read_feed = read_feed
        self._morning_day: dt.date | None = None
        self.sent: list[str] = []  # the dedup keys sent by this process

    def _notify(self, title: str, body: str, key: str) -> None:
        self._send(
            Notification(
                title=title,
                body=body,
                level=NotificationLevel.WARNING,
                kind=KIND,
                dedup_key=key,
                payload={"desk": key.split(":")[1]},
            )
        )
        self.sent.append(key)

    def started(self, at: dt.datetime, now: dt.datetime | None = None) -> str:
        """The message of a start at `at` (sent at `now`, a little later: the token reading
        is then current); returns its dedup key (`desk:start:<day>`)."""
        at = to_ist(at)
        day = at.date()
        w = self.session_clock.window_for(day)
        late = bool(self.is_trading_day(day)) and w.open_at < at < w.close_at
        title, body = start_text(at, self._read_expiry(), self._read_feed(), late)
        key = f"desk:start:{day.isoformat()}"
        self._notify(title, body, key)
        return key

    def look(self, now: dt.datetime) -> str | None:
        """The 09:05 check while the desk is up: once a trading day, between 09:05 and 09:15.
        Returns its dedup key when it was sent now."""
        now = to_ist(now)
        day = now.date()
        if self._morning_day == day or not (MORNING_AT <= now.time().replace(tzinfo=None)
                                            < MORNING_UNTIL):  # fmt: skip
            return None
        if not self.is_trading_day(day):
            return None
        self._morning_day = day
        key = f"desk:morning:{day.isoformat()}"
        body = facts_text("Desk up at", now, self._read_expiry(), self._read_feed())
        self._notify("Desk is up", body, key)
        return key


def feed_word(source_name: str | None, delayed: bool) -> str:
    """'live' or 'delayed (Yahoo, about 15 minutes behind)'; 'not started' without a desk."""
    if not source_name:
        return "not started"
    return "delayed (Yahoo, about 15 minutes behind)" if delayed else "live"
