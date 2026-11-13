"""The desk side of the pullback delivery trades (US-067 D1 to D15). Alerts and paper only.

`Desk.poll` calls `load_history` outside its lock (a database read, once a day and after a
top-up; between 09:00 and 15:35 it runs in the background and is swapped in when done) and
`on_poll` under it. Each poll, in this order:

1. Fills (D3, D5): a pending entry or exit fills on the desk's fine bars (`fills.decide`).
2. Warnings (D15): a first bar more than `gap_warn_pct` from the previous close flags any
   pending or open trade in that stock.
3. Evaluation (D1): every completed session not yet evaluated, in date order, at most
   `CATCH_UP` of them (only the newest one on a first start). Exits (D5) read the stock's own
   bars on each of them; an entry (D2) is raised on the newest only, an older entry signal is
   recorded MISSED. A hole or a short history stops the ENTRY of that day only (the card says
   why); NIFTY missing that day means no entry that day. A stock's day still incomplete when a
   later session of it is complete is read as it is for the exit tests only, with a warning.
4. Alerts: every entry and exit alert written and not yet sent is sent, and marked sent once a
   channel accepted it; otherwise it is tried again, at most every `RETELL` .

An error in one stock or one trade is logged (once a day per stock and kind), counted in
`errors` and skipped: the other stocks are still filled and evaluated. A poll with no new bar
and no change in the book reads nothing from the database and rebuilds nothing.

Every write is a compare-and-set in `DeliveryBook`, so a restart at any moment resumes with no
duplicate alert and no duplicate trade (D12). Nothing here can place an order.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine

from ..clock import IST, SessionClock, TradingCalendar, parse_hhmm
from ..config import CostConfig, SessionCfg
from ..desk.config import DeskConfig
from ..desk.history import rule_daily
from ..notify.base import Notification, NotificationLevel
from .book import (
    EXIT_PENDING,
    MISSED,
    OPEN,
    PENDING_ENTRY,
    DeliveryBook,
    DeliveryTrade,
    NewEntry,
)
from .data import RuleHistory, join_live, problem, session_complete
from .fills import Fill, Missed, decide, gap_warning
from .money import delivery_model, depository_paise
from .rule import (
    PullbackParams,
    entry_fill,
    entry_reading,
    exit_fill,
    exit_reading,
    nifty_ok,
    quantity,
    sma,
)

log = logging.getLogger(__name__)

#: completed sessions evaluated at most in one catch-up (D1, design Q2)
CATCH_UP = 10
#: the top-up and the history load stay off the poll thread between these times (IST)
NO_TOP_UP = (dt.time(9, 0), dt.time(15, 35))
#: the top-up: at most this many tries in a window, this far apart (lead's ruling)
TOP_UP_TRIES = 3
TOP_UP_GAP = dt.timedelta(minutes=20)
#: an alert no channel accepted is tried again after this long
RETELL = dt.timedelta(minutes=5)
REASON_WORDS = {
    "SMA20": "the close is above its 20-session average",
    "TIME": "the holding limit of sessions is reached",
    "STOP": "the close is at or below the stop",
}

BarsFor = Callable[[str], pd.DataFrame | None]
#: the top-up: given the series keys and a stop flag, fetch and store recent one-minute bars;
#: it may answer an object with `errors` (a `BackfillResult`)
TopUp = Callable[..., Any]


@dataclass(frozen=True)
class Stock:
    series_key: str
    instrument_key: str
    label: str
    symbol: str  # what to type into Dhan
    params: PullbackParams
    evidence: str


def _frame_key(frame: pd.DataFrame | None) -> tuple[Any, ...]:
    """What identifies a frame's content cheaply: its length, first and last stamp and last
    open and close (a bar revised in place changes its close)."""
    if frame is None or len(frame) == 0:
        return (0, None, None, None, None)
    last = frame.iloc[-1]
    return (len(frame), frame.index[0], frame.index[-1], last.get("open"), last.get("close"))


class DeliveryDesk:
    def __init__(
        self,
        engine: Engine,
        desk_cfg: DeskConfig,
        costs: CostConfig,
        session: SessionCfg,
        calendar: TradingCalendar,
        notifier: Any,
        started_at: dt.datetime,
        names: Mapping[str, tuple[str, str]] | None = None,
        nifty_key: str = "NIFTY",
        top_up: TopUp | None = None,
        data_api_active: Callable[[], bool] | None = None,
        background: bool = True,
    ) -> None:
        self.engine = engine
        self.cfg = desk_cfg.delivery
        self.calendar = calendar
        self.session = session
        self.clock = SessionClock(session, calendar)
        self.notifier = notifier
        self.started_at = started_at
        self.nifty_key = nifty_key
        self.last_bar_at = parse_hhmm(self.cfg.last_bar_at)
        self.settle = dt.timedelta(minutes=self.cfg.settle_minutes)
        names = names or {}
        self.stocks: dict[str, Stock] = {}
        for inst, slot in desk_cfg.delivery_slots():
            label, symbol = names.get(inst.key, (inst.key, inst.key))
            self.stocks[inst.key] = Stock(
                inst.key, inst.tracked, label, symbol,
                PullbackParams.from_mapping(slot.params), str(slot.params.get("evidence") or ""),
            )  # fmt: skip
        first = next(iter(desk_cfg.delivery_slots()), None)
        strategy, timeframe = (first[1].name, first[1].timeframe) if first else ("pullback", "1d")
        self.book = DeliveryBook(
            engine, delivery_model(costs), depository_paise(costs), strategy, timeframe
        )
        self._top_up = top_up
        self._data_api_active = data_api_active or (lambda: False)
        self._background = background
        self._stored: dict[str, tuple[pd.DataFrame, dict[dt.date, dt.time]]] = {}
        self._history_day: dt.date | None = None
        self._reload = threading.Event()
        self._loader: threading.Thread | None = None
        self._top_up_tries: list[dt.datetime] = []
        self._top_up_thread: threading.Thread | None = None
        self.top_up_note: str | None = None
        self.notes: dict[str, str] = {}  # series key -> why an entry cannot be read now
        self._warned: set[tuple[str, str]] = set()
        self._errors_logged: set[tuple[str, str, dt.date]] = set()
        self._cards: dict[str, dict[str, Any]] = {}
        self._view: dict[str, Any] = {"enabled": True, "pending": [], "open": []}
        self.errors = 0
        #: other series the desk tracks, topped up with these (set by the desk)
        self.also_top_up: list[str] = []
        # in memory between polls (cost of a poll, reviewer 4)
        self._hist: dict[str, tuple[tuple[Any, ...], RuleHistory]] = {}
        self._last_eval: dict[str, dt.date | None] = {}
        self._gap_seen: dict[tuple[int, dt.date | None], None] = {}
        self._told_version = -1
        self._retell_at: dict[tuple[int, str], dt.datetime] = {}
        self._accepted: set[tuple[int, str]] = set()  # alerts a channel took in this process
        # the exits step's result per (stock, session), the latest only: run once each
        self._exits_done: dict[tuple[str, dt.date], list[dict[str, Any]]] = {}
        self._record_warned: set[tuple[str, dt.date]] = set()
        self._rebuilt_on: tuple[Any, ...] | None = None
        self.calls: Counter[str] = Counter()  # join_live, rebuild, untold: for the tests

    @property
    def series(self) -> list[str]:
        return [*self.stocks, self.nifty_key]

    @property
    def top_up_series(self) -> list[str]:
        """What the top-up keeps fresh: these series and every other one the desk tracks (the
        micro-trend ladders of the indices read the stored one-minute bars, US-068)."""
        return list(dict.fromkeys([*self.series, *self.also_top_up]))

    # ── the stored days (outside the desk lock) ──────────────────────────────
    def load_history(self, now: dt.datetime) -> None:
        """Read every series' stored days once a calendar day, and again after a top-up. In
        market hours (09:00 to 15:35) the read runs in the background and is swapped in when
        done (about 6 s for 19 series on the owner's store), never on the poll thread."""
        now = now.astimezone(IST)
        today = now.date()
        if self._history_day == today and not self._reload.is_set():
            return
        if self._loader is not None and self._loader.is_alive():
            return
        self._reload.clear()

        def load() -> None:
            stored: dict[str, tuple[pd.DataFrame, dict[dt.date, dt.time]]] = {}
            for key in self.series:
                try:
                    stored[key] = rule_daily(self.engine, key, now)
                except Exception:  # noqa: BLE001 - a missing history is a note, never a crash
                    log.exception("delivery: the stored daily history of %s could not be read", key)
                    stored[key] = (pd.DataFrame(), {})
            self._stored = stored  # one assignment: the poll sees the old or the new, whole
            self._history_day = today

        if self._background and NO_TOP_UP[0] <= now.time() <= NO_TOP_UP[1]:
            self._loader = threading.Thread(target=load, name="delivery-history", daemon=True)
            self._loader.start()
        else:
            load()

    def maybe_top_up(self, now: dt.datetime) -> None:
        """Top the store's one-minute bars up from the broker (D8, lead's rulings): only with
        the switch on, the Data API on and a top-up given, outside 09:00 to 15:35, only when a
        stored history ends before the last completed session, at most `TOP_UP_TRIES` tries
        in a window, `TOP_UP_GAP` apart. The top-up gets a stop flag that turns true at 09:00."""
        if self._top_up is None or not self.cfg.top_up or not self._data_api_active():
            return
        now = now.astimezone(IST)
        if NO_TOP_UP[0] <= now.time() <= NO_TOP_UP[1]:
            return
        if self._top_up_thread is not None and self._top_up_thread.is_alive():
            return
        window = self._window_start(now)
        tries = [t for t in self._top_up_tries if t >= window]
        if len(tries) >= TOP_UP_TRIES or (tries and now - tries[-1] < TOP_UP_GAP):
            return
        last = self._last_trading_day(now)
        behind = [
            k
            for k in self.series
            if (to := self._stored_to(k)) is None or (last is not None and to < last)
        ]
        if not behind:
            return
        self._top_up_tries = [*tries, now]
        keys = self.top_up_series
        stop_at = dt.datetime.combine(
            now.date() + (dt.timedelta(days=1) if now.time() > NO_TOP_UP[0] else dt.timedelta()),
            NO_TOP_UP[0],
            tzinfo=IST,
        )

        def stop() -> bool:
            return type(self)._wall() >= stop_at

        def run() -> None:
            try:
                result = self._top_up(keys, stop)  # type: ignore[misc]
                errors = list(getattr(result, "errors", None) or [])
                if errors:
                    shown = "; ".join(str(e) for e in errors[:3])
                    more = f" and {len(errors) - 3} more" if len(errors) > 3 else ""
                    self.top_up_note = (
                        f"top-up {now:%d %b %H:%M}: {len(errors)} error(s): {shown}{more}"
                    )
                    log.warning("delivery: the top-up reported errors: %s", errors)
                else:
                    self.top_up_note = f"topped up {now:%d %b %H:%M}"
            except Exception as exc:  # noqa: BLE001 - the freshness test is the safety net
                self.top_up_note = f"top-up failed {now:%d %b %H:%M}: {type(exc).__name__}"
                log.warning("delivery: the top-up of the daily history failed: %s", exc)
            self._reload.set()

        if self._background:
            self._top_up_thread = threading.Thread(target=run, name="delivery-top-up", daemon=True)
            self._top_up_thread.start()
        else:
            run()

    @staticmethod
    def _wall() -> dt.datetime:
        return dt.datetime.now(IST)

    @staticmethod
    def _window_start(now: dt.datetime) -> dt.datetime:
        """The start of the top-up window `now` is in: 15:35 today, or yesterday's."""
        today = dt.datetime.combine(now.date(), NO_TOP_UP[1], tzinfo=IST)
        return today if now >= today else today - dt.timedelta(days=1)

    def _stored_to(self, key: str) -> dt.date | None:
        frame, _ = self._stored.get(key, (pd.DataFrame(), {}))
        if frame is None or frame.empty:
            return None
        idx = pd.DatetimeIndex(frame.index)
        return max((idx.tz_convert(IST) if idx.tz is not None else idx).date)

    def _last_trading_day(self, now: dt.datetime) -> dt.date | None:
        d = (
            now.date()
            if now >= self.clock.window_for(now.date()).close_at
            else now.date() - dt.timedelta(days=1)
        )
        for _ in range(15):
            if self.calendar.is_trading_day(d):
                return d
            d -= dt.timedelta(days=1)
        return None

    # ── one poll (under the desk lock) ───────────────────────────────────────
    def on_poll(
        self, now: dt.datetime, bars_for: BarsFor, last_price: Callable[[str], float | None]
    ) -> None:
        now = now.astimezone(IST)
        bars = {k: bars_for(k) for k in self.series}
        hist = {k: self._history(k, bars[k]) for k in self.series}
        self._fills(now, hist, bars)  # a fill before the evaluation of its session's exit
        loading = self._loader is not None and self._loader.is_alive() and not self._stored
        if not loading:  # no stored history yet: nothing is evaluated on a half history
            self._evaluate(now, hist)
        self._tell(now)  # told before anything else can change the alert
        self._fills(now, hist, bars)  # an alert raised late fills in the same poll
        self._tell(now)
        self._rebuild(now, hist, last_price)

    def _history(self, key: str, live: pd.DataFrame | None) -> RuleHistory:
        stored, last = self._stored.get(key, (pd.DataFrame(), {}))
        sig = (*_frame_key(stored), *_frame_key(live))
        hit = self._hist.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        self.calls["join_live"] += 1
        h = join_live(
            key, stored, last, live, self.last_bar_at, self.calendar.is_trading_day,
            self.cfg.special_min_bars,
        )  # fmt: skip
        self._hist[key] = (sig, h)
        return h

    def _complete(self, h: RuleHistory, now: dt.datetime) -> Callable[[dt.date], bool]:
        def f(day: dt.date) -> bool:
            return session_complete(
                day, now, h.last_bar.get(day), self.clock, self.calendar, self.last_bar_at,
                self.settle, day in h.daily_only,
            )  # fmt: skip

        return f

    def _opening(self, day: dt.date) -> dt.datetime | None:
        """The session's opening minute, for the late flag; None for a special session."""
        if not self.calendar.is_trading_day(day):
            return None
        return self.clock.window_for(day).open_at

    def _failed(self, key: str, kind: str, now: dt.datetime) -> None:
        """One stock's or one trade's error: counted, logged once a day per (stock, kind)."""
        self.errors += 1
        mark = (key, kind, now.date())
        if mark not in self._errors_logged:
            self._errors_logged.add(mark)
            log.exception("delivery: %s failed for %s (skipped; the others go on)", kind, key)

    def _fills(
        self,
        now: dt.datetime,
        hist: Mapping[str, RuleHistory],
        bars: Mapping[str, pd.DataFrame | None],
    ) -> None:
        for t in self.book.active():
            try:
                self._fill_one(now, t, hist, bars)
            except Exception:  # noqa: BLE001 - one trade never stops the others
                self._failed(t.series_key, "fill", now)

    def _fill_one(
        self,
        now: dt.datetime,
        t: DeliveryTrade,
        hist: Mapping[str, RuleHistory],
        bars_of: Mapping[str, pd.DataFrame | None],
    ) -> None:
        stock = self.stocks.get(t.series_key)
        bars = bars_of.get(t.series_key)
        h = hist.get(t.series_key)
        if stock is None or h is None:
            return
        done = self._complete(h, now)
        if t.status == PENDING_ENTRY:
            got = decide(
                bars, t.signal_date, self.started_at, done, may_miss=True,
                opening=self._opening, given_at=t.created_at,
            )  # fmt: skip
            if isinstance(got, Fill):
                self.book.fill_entry(
                    t.id, got.ts, got.session, entry_fill(got.open_, stock.params), got.late, now
                )
            elif isinstance(got, Missed):
                self.book.miss_entry(t.id, got.why, now)
        elif t.status == EXIT_PENDING and t.exit_signal_date is not None:
            got = decide(
                bars, t.exit_signal_date, self.started_at, done, may_miss=False,
                opening=self._opening, given_at=t.exit_signalled_at,
            )  # fmt: skip
            if isinstance(got, Fill):
                self.book.fill_exit(t.id, got.ts, exit_fill(got.open_, stock.params), got.late, now)
        last_day = (
            None
            if bars is None or not len(bars)
            else pd.Timestamp(bars.index[-1]).tz_convert(IST).date()
        )
        seen = (t.id, last_day)  # one look per trade and session: never grows with each bar
        if seen not in self._gap_seen:
            self._gap_seen = {k: v for k, v in self._gap_seen.items() if k[0] != t.id}
            self._gap_seen[seen] = None
            warning = gap_warning(bars, t.signal_date, self.cfg.gap_warn_pct / 100.0)
            if warning is not None:
                self.book.warn(t.id, warning, now)

    def _evaluate(self, now: dt.datetime, hist: Mapping[str, RuleHistory]) -> None:
        nh = hist[self.nifty_key]
        for key, stock in self.stocks.items():
            try:
                self._evaluate_stock(now, stock, hist[key], nh)
            except Exception:  # noqa: BLE001 - one stock never stops the others
                self._failed(key, "evaluation", now)

    def _evaluated(self, key: str) -> dt.date | None:
        if key not in self._last_eval:
            self._last_eval[key] = self.book.last_evaluated(key)
        return self._last_eval[key]

    def _evaluate_stock(
        self, now: dt.datetime, stock: Stock, h: RuleHistory, nh: RuleHistory
    ) -> None:
        """The sessions of one stock not yet read (see the module docstring, step 3)."""
        key = stock.series_key
        done = self._complete(h, now)
        last = self._evaluated(key)
        days = [d for d in h.days.index if last is None or d > last]
        complete = [done(d) for d in days]
        todo: list[tuple[dt.date, bool]] = []  # (day, complete)
        stall: tuple[str, dt.date] | None = None
        for i, d in enumerate(days):
            if complete[i]:
                todo.append((d, True))
            elif any(complete[i + 1 :]):  # a later session is complete: read it as it is
                todo.append((d, False))
            else:
                if self._past_close(d, now):
                    bar = h.last_bar.get(d)
                    at = bar.strftime("%H:%M") if bar else "none"
                    stall = (f"the session of {d} is incomplete (last bar {at})", d)
                break
        if last is None:
            todo = todo[-1:]  # a first start reads the newest completed session only
        todo = todo[-CATCH_UP:]
        if todo:
            # a note stays until a reading replaces it (never cleared by today's partial day)
            self.notes.pop(key, None)
        if stall is not None:
            self._note(key, *stall)
        if not todo:
            return
        newest = todo[-1][0]
        for day, whole in todo:
            # exits read only the stock's own bars: never held back by NIFTY (round 3, item 1)
            exits = self._exits_step(now, stock, h, day, whole)
            if whole and self._nifty_pending(nh, day, now):
                self._note(key, f"waiting for NIFTY's session of {day}", day)
                return  # the entry step waits; not recorded: read again at the next poll
            self._entry_step(now, stock, h, nh, day, whole, day == newest, exits)

    def _nifty_pending(self, nh: RuleHistory, day: dt.date, now: dt.datetime) -> bool:
        """NIFTY's bar of a calendar session is missing or ends before `last_bar_at` while
        the clock is within an hour of that session's close: wait for it rather than record
        "no entry" (round 2, item 7). After that hour the day is read as it is."""
        if not self.calendar.is_trading_day(day) or day in nh.daily_only:
            return False
        bar = nh.last_bar.get(day)
        if day in set(nh.days.index) and bar is not None and bar >= self.last_bar_at:
            return False
        close = self.clock.window_for(day).close_at
        return now < close + dt.timedelta(minutes=60)

    def _past_close(self, day: dt.date, now: dt.datetime) -> bool:
        if day < now.date():
            return True
        return self.calendar.is_trading_day(day) and now >= (
            self.clock.window_for(day).close_at + self.settle
        )

    def _entry_problem(
        self, stock: Stock, h: RuleHistory, nh: RuleHistory, day: dt.date, now: dt.datetime
    ) -> str | None:
        """Why no entry can be read on `day` (a hole, a short history, a bad bar), or None.
        NIFTY missing that day is not a problem: R1(b) then gives no signal."""
        floor = max(self.cfg.min_bars, stock.params.min_bars)
        why = problem(h, day, self.calendar, floor)
        if day in h.bad:
            why = f"{day}: {h.bad[day]}"
        if why is None:
            bad = [d for d in h.bad if d <= day]
            if bad:
                self._note(stock.series_key, f"bar of {bad[-1]} left out: {h.bad[bad[-1]]}", day)
        return why

    def _exits_step(
        self, now: dt.datetime, stock: Stock, h: RuleHistory, day: dt.date, whole: bool
    ) -> list[dict[str, Any]]:
        """R3 on the close of `day` for the stock's open trades, once per (stock, session):
        a repeat (the entry step waiting for NIFTY) answers what the first run found."""
        mark = (stock.series_key, day)
        if mark in self._exits_done:
            return self._exits_done[mark]
        p = stock.params
        frame = h.upto(day)
        dates = list(frame.index)
        c = frame["close"].to_numpy(float)
        exits: list[dict[str, Any]] = []
        partial = None if whole else f"{day.isoformat()} read from incomplete data"
        if partial:
            self._note(stock.series_key, partial, day)
        for t in self.book.active():
            if t.series_key != stock.series_key or t.status != OPEN or t.entry_session is None:
                continue
            if t.entry_session > day or t.entry_paise is None:
                continue
            if partial:
                self.book.warn(t.id, partial, now)
            if t.entry_session not in dates:
                self._note(
                    stock.series_key,
                    f"the entry session {t.entry_session} is not in the daily history",
                    day,
                )
                continue
            short = self._short_sessions(dates, t.entry_session, day, h)
            if short:
                self.book.warn(t.id, f"session count short by {short}", now)
            r = exit_reading(c, dates.index(t.entry_session), t.entry_paise / 100, len(c) - 1, p)
            exits.append({"trade": t.id, "sessions": r.sessions, "reasons": list(r.reasons)})
            if r.hit:
                self.book.signal_exit(t.id, day, r.reasons, r.sessions, now)
        self._exits_done = {k: v for k, v in self._exits_done.items() if k[0] != mark[0]}
        self._exits_done[mark] = exits
        return exits

    def _entry_step(
        self,
        now: dt.datetime,
        stock: Stock,
        h: RuleHistory,
        nh: RuleHistory,
        day: dt.date,
        whole: bool,
        newest: bool,
        exits: list[dict[str, Any]],
    ) -> None:
        """R1 on the close of `day`, then the session's record (it holds the exits too)."""
        p = stock.params
        frame = h.upto(day)
        dates = list(frame.index)
        c = frame["close"].to_numpy(float)
        partial = None if whole else f"{day.isoformat()} read from incomplete data"
        entry = None
        record: dict[str, Any] = {"session": day.isoformat(), "exits": exits, "whole": whole}
        why = partial or self._entry_problem(stock, h, nh, day, now)
        if why is not None:
            self._note(stock.series_key, why, day)
            record["entry_blocked"] = why
        else:
            nf = nh.upto(day)
            ok = nifty_ok(dates, list(nf.index), nf["close"].to_numpy(float), p)
            reading = entry_reading(c, ok, len(c) - 1, p)
            record.update(reading.as_dict())
            busy = any(t.series_key == stock.series_key for t in self.book.active())
            record["busy"] = busy
            if reading.signal and not busy:
                entry = NewEntry(
                    stock.instrument_key,
                    float(c[-1]),
                    quantity(float(c[-1]), p.unit_inr),
                    {"rule": p.__dict__, "reading": reading.as_dict(), "evidence": stock.evidence},
                    PENDING_ENTRY if newest else MISSED,
                    None if newest else "missed: the desk was not running after that close",
                )
        self.book.record_evaluation(stock.series_key, day, record, now, entry)
        if self.book.evaluation(stock.series_key, day) is not None:  # really written
            self._last_eval[stock.series_key] = day
        elif (stock.series_key, day) not in self._record_warned:  # once per (stock, session)
            self._record_warned.add((stock.series_key, day))
            log.warning("delivery: %s %s could not be recorded", stock.series_key, day)

    def _short_sessions(
        self, dates: list[dt.date], entry: dt.date, day: dt.date, h: RuleHistory
    ) -> int:
        """Calendar sessions from the entry to `day` missing from the stock's bars (in the
        years the holiday calendar covers): the trade's session count is short by that many."""
        have = set(dates)
        n, d = 0, entry
        while d <= day:
            if self.calendar.has_year(d.year) and self.calendar.is_trading_day(d) and d not in have:
                n += 1
            d += dt.timedelta(days=1)
        return n

    def _note(self, key: str, why: str, day: dt.date | None = None) -> None:
        self.notes[key] = why
        mark = (key, f"{day}:{why}")
        if mark not in self._warned:
            self._warned.add(mark)
            log.warning("delivery: %s%s: %s", key, f" {day}" if day else "", why)

    # ── alerts (D2, D5, D14) ─────────────────────────────────────────────────
    def alert_text(self, t: DeliveryTrade, which: str) -> tuple[str, str]:
        stock = self.stocks.get(t.series_key)
        label = stock.label if stock else t.series_key
        symbol = stock.symbol if stock else t.series_key
        evidence = (stock.evidence if stock else "") or str(t.plan.get("evidence") or "")
        p = stock.params if stock else PullbackParams()
        if which == "entry":
            title = f"BUY {t.qty} shares of {label} at the next open · pullback 1d (delivery)"
            body = (
                f"BUY {t.qty} shares of {symbol} at the next open "
                f"(last close Rs {t.signal_close:,.2f}).\n"
                f"Pullback: the close of {t.signal_date:%d %b %Y} is {p.depth * 100:g}% or more "
                f"below its {p.high_lookback}-session closing high, above its SMA{p.trend_sma}, "
                f"with NIFTY above its SMA{p.trend_sma}. Delivery, carried for days: the exit "
                f"alert says when to sell. Paper test: no order is placed."
            )
        else:
            reasons = [r for r in (t.exit_reason or "").split(",") if r]
            words = {
                "SMA20": f"the close is above its {p.exit_sma}-session average",
                "TIME": f"{p.max_sessions} sessions held",
                "STOP": f"the close is {p.stop * 100:g}% or more below the entry fill",
            }
            why = "; ".join(words.get(r, REASON_WORDS.get(r, r)) for r in reasons)
            title = (
                f"SELL {t.qty} shares of {label} at the next open · pullback exit "
                f"({', '.join(reasons)})"
            )
            body = (
                f"SELL {t.qty} shares of {symbol} at the next open: {why} "
                f"(close of {t.exit_signal_date:%d %b %Y}, "
                f"{t.sessions_held} of {p.max_sessions} sessions). Paper test: no order is placed."
            )
        if evidence:
            body += f"\n{evidence}"
        return title, body

    def notification(self, t: DeliveryTrade, which: str, now: dt.datetime) -> Notification:
        """The alert as every channel gets it: its own title and text. The payload names no
        symbol or side, so a channel never formats it as an intraday desk alert."""
        title, body = self.alert_text(t, which)
        sid = f"pb-{t.series_key}-{t.signal_date.isoformat()}-{which}"
        return Notification(
            title=title,
            body=body,
            level=NotificationLevel.SIGNAL,
            signal_id=sid,
            ttl_seconds=self._ttl(now),
            payload={
                "id": sid,
                "delivery": which,
                "signal_date": t.signal_date.isoformat(),
                "qty": t.qty,
                "status": t.status,
                "title": title,
                "text": body,
            },
        )

    def _tell(self, now: dt.datetime) -> None:
        due = any(at <= now for at in self._retell_at.values())
        if self.book.version == self._told_version and not due:
            return
        self.calls["untold"] += 1
        broke = False
        for t, which in self.book.untold():
            mark = (t.id, which)
            if mark in self._retell_at and self._retell_at[mark] > now:
                continue
            n = self.notification(t, which, now)
            key = f"delivery:{n.signal_id}"
            asks = getattr(self.notifier, "suppresses", None)
            repeat = bool(asks(key)) if callable(asks) else None
            try:
                result = self.notifier.send(n, dedup_key=key)
            except Exception:  # noqa: BLE001 - the notifier itself broke: tried at the next poll
                self.errors += 1
                broke = True
                log.exception("delivery: the %s alert of %s could not be sent", which, n.signal_id)
                continue
            # told when a channel accepted it; an empty result is told only when it is the
            # repeat window answering for a first send that was accepted (round 2, item 3).
            # A notifier that cannot say (`suppresses` absent) answers {} for "sent" (fakes).
            # Every channel refusing it is tried again after RETELL, not at every poll.
            took = isinstance(result, Mapping) and any(result.values())
            if isinstance(result, Mapping) and not result:
                took = mark in self._accepted if repeat else repeat is None
            accepted = took
            if not accepted:
                self.errors += 1
                self._retell_at[mark] = now + RETELL
                log.warning("delivery: no channel took the %s alert of %s", which, n.signal_id)
                continue
            self._retell_at.pop(mark, None)
            self._accepted.add(mark)
            self.book.mark_told(t.id, which, now)
        self._told_version = -1 if broke else self.book.version

    def _ttl(self, now: dt.datetime) -> int:
        """Pending until the next session's close: seconds to the next trading day's close."""
        d = now.date() + dt.timedelta(days=1)
        for _ in range(15):
            if self.calendar.is_trading_day(d):
                break
            d += dt.timedelta(days=1)
        return max(60, int((self.clock.window_for(d).close_at - now).total_seconds()))

    # ── what the page shows (D7, D11, D14, ruling 5) ─────────────────────────
    def _rebuild(
        self,
        now: dt.datetime,
        hist: Mapping[str, RuleHistory],
        last_price: Callable[[str], float | None],
    ) -> None:
        sig = (
            tuple(self._hist[k][0] for k in self.series if k in self._hist),
            self.book.version,
            tuple(sorted(self.notes.items())),
            self.top_up_note,
            self.errors,
            now.date(),
            self._past_close(now.date(), now),
        )
        if sig == self._rebuilt_on:
            self._refresh_prices(last_price)
            return
        self.calls["rebuild"] += 1
        nh = hist[self.nifty_key]
        n_done = self._complete(nh, now)
        n_to = max((d for d in nh.days.index if n_done(d)), default=None)
        active = self.book.active()
        cards: dict[str, dict[str, Any]] = {}
        opened: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for key, stock in self.stocks.items():
            try:
                card, rows = self._rebuild_one(now, stock, hist[key], nh, active, last_price)
            except Exception:  # noqa: BLE001 - one card never stops the others
                self._failed(key, "view", now)
                card, rows = (
                    {"evidence": stock.evidence, "note": "this card failed: see the log"},
                    [],
                )
            cards[key] = card
            for t, row in rows:
                (pending if t.status in (PENDING_ENTRY, EXIT_PENDING) else opened).append(row)
                if t.status == EXIT_PENDING:
                    opened.append(row)
        self._cards = cards
        tos = [str(c["history_to"]) for c in cards.values() if c.get("history_to")]
        self._view = {
            "enabled": True,
            "strategy": self.book.strategy,
            "timeframe": self.book.timeframe,
            "pending": pending,
            "open": opened,
            "history_to": min(tos) if tos and len(tos) == len(cards) else None,
            "nifty_history_to": n_to.isoformat() if n_to else None,
            "stale": sorted(
                k for k, c in cards.items() if not c.get("history_to") or c.get("note")
            ),
            "top_up": self.top_up_note,
            "errors": self.errors,
            "evidence": next((s.evidence for s in self.stocks.values() if s.evidence), ""),
            "unit_inr": next((s.params.unit_inr for s in self.stocks.values()), None),
        }
        self._rebuilt_on = sig

    def _rebuild_one(
        self,
        now: dt.datetime,
        stock: Stock,
        h: RuleHistory,
        nh: RuleHistory,
        active: list[DeliveryTrade],
        last_price: Callable[[str], float | None],
    ) -> tuple[dict[str, Any], list[tuple[DeliveryTrade, dict[str, Any]]]]:
        key = stock.series_key
        done = self._complete(h, now)
        complete = [d for d in h.days.index if done(d)]
        to = complete[-1] if complete else None
        stored = self._stored_to(key)
        card: dict[str, Any] = {
            "evidence": stock.evidence,
            "history_to": to.isoformat() if to else None,
            "stored_to": stored.isoformat() if stored else None,
            "note": self.notes.get(key),
            "session": None,
            "reading": None,
        }
        floor = max(self.cfg.min_bars, stock.params.min_bars)
        if to is not None and problem(h, to, self.calendar, floor) is None:
            frame = h.upto(to)
            nf = nh.upto(to)
            ok = nifty_ok(
                list(frame.index), list(nf.index), nf["close"].to_numpy(float), stock.params
            )
            r = entry_reading(frame["close"].to_numpy(float), ok, len(frame) - 1, stock.params)
            card["session"], card["reading"] = to.isoformat(), r.as_dict()
        mine = [t for t in active if t.series_key == key]
        rows = [(t, self._trade_row(t, stock, h, last_price(key))) for t in mine]
        card["trade"] = rows[0][1] if rows else None  # with "sessions" for the row's chip
        card["unit_inr"] = stock.params.unit_inr
        card["max_sessions"] = stock.params.max_sessions
        return card, rows

    def _refresh_prices(self, last_price: Callable[[str], float | None]) -> None:
        """Between rebuilds only the last price moves: the open rows' marks follow it."""
        for row in self._view.get("open", []):
            try:
                last = last_price(str(row.get("series_key")))
            except Exception:  # noqa: BLE001
                continue
            entry = row.get("entry")
            if last is None or not entry:
                continue
            row["last"] = last
            row["unrealised"] = round((last - entry) * row["qty"], 2)
            row["unrealised_pct"] = round((last / entry - 1) * 100, 3)

    def _trade_row(
        self, t: DeliveryTrade, stock: Stock, h: RuleHistory, last: float | None
    ) -> dict[str, Any]:
        row = t.as_dict()
        row["label"] = stock.label
        row["symbol"] = stock.symbol
        p = stock.params
        if t.status in (PENDING_ENTRY, EXIT_PENDING):
            row["alert"] = self.alert_text(t, "entry" if t.status == PENDING_ENTRY else "exit")[1]
        if t.entry_paise is None:
            return row
        entry = t.entry_paise / 100
        dates = list(h.days.index)
        held = None
        if t.entry_session in dates:
            held = len(dates) - dates.index(t.entry_session)
        closes = h.days["close"].to_numpy(float)
        level = sma(closes, p.exit_sma)[-1] if len(closes) >= p.exit_sma else np.nan
        row.update(
            {
                "last": last,
                "unrealised": None if last is None else round((last - entry) * t.qty, 2),
                "unrealised_pct": None if last is None else round((last / entry - 1) * 100, 3),
                "sessions": held,
                "max_sessions": p.max_sessions,
                "sma_exit": None if not np.isfinite(level) else round(float(level), 2),
                "stop": round(entry * (1 - p.stop), 2),
            }
        )
        return row

    def card(self, series_key: str) -> dict[str, Any] | None:
        return self._cards.get(series_key)

    def view(self) -> dict[str, Any]:
        return dict(self._view)

    def alerts(self, since: dt.datetime) -> list[dict[str, Any]]:
        """Delivery alerts for the archive (D2): every trade created or changed since `since`."""
        out = []
        for t in self.book.recent(since):
            row = t.as_dict()
            stock = self.stocks.get(t.series_key)
            row["label"] = stock.label if stock else t.series_key
            out.append(row)
        return out
