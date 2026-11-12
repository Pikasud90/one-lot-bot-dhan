"""The desk side of the pullback delivery trades (US-067 D1 to D15). Alerts and paper only.

`Desk.poll` calls `load_history` outside its lock (a database read, once a day and after a
top-up) and `on_poll` under it. Each poll, in this order:

1. Fills (D3, D5): a pending entry or exit fills on the desk's fine bars (`fills.decide`).
2. Warnings (D15): a first bar more than `gap_warn_pct` from the previous close flags any
   pending or open trade in that stock.
3. Evaluation (D1): every completed session not yet evaluated, in date order, at most
   `CATCH_UP` of them (only the newest one on a first start). Exits (D5) are read on each of
   them; an entry (D2) is raised on the newest only, an older entry signal is recorded MISSED.
   A session whose history is short or has a hole is not evaluated: the card says why, a
   warning is logged once, and the next poll tries again.
4. Alerts: every entry and exit alert written and not yet sent is sent, then marked sent.

Every write is a compare-and-set in `DeliveryBook`, so a restart at any moment resumes with no
duplicate alert and no duplicate trade (D12). Nothing here can place an order.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
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
#: the top-up never runs between these times (IST): the market and its close (ruling 3)
NO_TOP_UP = (dt.time(9, 0), dt.time(15, 35))
REASON_WORDS = {
    "SMA20": "the close is above its 20-session average",
    "TIME": "30 sessions held",
    "STOP": "the close is 15% or more below the entry fill",
}

BarsFor = Callable[[str], pd.DataFrame | None]
#: the top-up: given the series keys, fetch and store their recent one-minute bars
TopUp = Callable[[list[str]], Any]


@dataclass(frozen=True)
class Stock:
    series_key: str
    instrument_key: str
    label: str
    symbol: str  # what to type into Dhan
    params: PullbackParams
    evidence: str


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
        self._top_up_day: dt.date | None = None
        self._top_up_thread: threading.Thread | None = None
        self.top_up_note: str | None = None
        self.notes: dict[str, str] = {}  # series key -> why the rule cannot read it now
        self._warned: set[tuple[str, str]] = set()
        self._cards: dict[str, dict[str, Any]] = {}
        self._view: dict[str, Any] = {"enabled": True, "pending": [], "open": []}
        self.errors = 0

    @property
    def series(self) -> list[str]:
        return [*self.stocks, self.nifty_key]

    # ── the stored days (outside the desk lock) ──────────────────────────────
    def load_history(self, now: dt.datetime) -> None:
        """Read every series' stored days once a calendar day, and again after a top-up."""
        today = now.astimezone(IST).date()
        if self._history_day == today and not self._reload.is_set():
            return
        self._reload.clear()
        stored: dict[str, tuple[pd.DataFrame, dict[dt.date, dt.time]]] = {}
        for key in self.series:
            try:
                stored[key] = rule_daily(self.engine, key, now)
            except Exception:  # noqa: BLE001 - a missing history is a note, never a crash
                log.exception("delivery: the stored daily history of %s could not be read", key)
                stored[key] = (pd.DataFrame(), {})
        self._stored = stored
        self._history_day = today

    def maybe_top_up(self, now: dt.datetime) -> None:
        """Top the store's one-minute bars up from the broker (D8, ruling 3): only with the
        switch on, the Data API on and a top-up given, outside 09:00 to 15:35, once a day,
        and only when a stored history ends before the last completed session."""
        if self._top_up is None or not self.cfg.top_up or not self._data_api_active():
            return
        now = now.astimezone(IST)
        if NO_TOP_UP[0] <= now.time() <= NO_TOP_UP[1] or self._top_up_day == now.date():
            return
        if self._top_up_thread is not None and self._top_up_thread.is_alive():
            return
        last = self._last_trading_day(now)
        behind = [
            k
            for k in self.series
            if (to := self._stored_to(k)) is None or (last is not None and to < last)
        ]
        if not behind:
            return
        self._top_up_day = now.date()
        keys = list(self.series)

        def run() -> None:
            try:
                self._top_up(keys)  # type: ignore[misc]
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
        hist = {k: self._history(k, bars_for(k)) for k in self.series}
        self._fills(now, hist, bars_for)  # a fill before the evaluation of its session's exit
        self._evaluate(now, hist)
        self._tell(now)  # told before anything else can change the alert
        self._fills(now, hist, bars_for)  # an alert raised late fills in the same poll
        self._tell(now)
        self._rebuild(now, hist, last_price)

    def _history(self, key: str, live: pd.DataFrame | None) -> RuleHistory:
        stored, last = self._stored.get(key, (pd.DataFrame(), {}))
        return join_live(key, stored, last, live)

    def _complete(self, h: RuleHistory, now: dt.datetime) -> Callable[[dt.date], bool]:
        def f(day: dt.date) -> bool:
            return session_complete(
                day, now, h.last_bar.get(day), self.clock, self.calendar, self.last_bar_at,
                self.settle, day in h.daily_only,
            )  # fmt: skip

        return f

    def _fills(self, now: dt.datetime, hist: Mapping[str, RuleHistory], bars_for: BarsFor) -> None:
        for t in self.book.active():
            stock = self.stocks.get(t.series_key)
            bars = bars_for(t.series_key)
            h = hist.get(t.series_key)
            if stock is None or h is None:
                continue
            done = self._complete(h, now)
            if t.status == PENDING_ENTRY:
                got = decide(bars, t.signal_date, self.started_at, done, may_miss=True)
                if isinstance(got, Fill):
                    self.book.fill_entry(
                        t.id,
                        got.ts,
                        got.session,
                        entry_fill(got.open_, stock.params),
                        got.late,
                        now,
                    )
                elif isinstance(got, Missed):
                    self.book.miss_entry(t.id, got.why, now)
            elif t.status == EXIT_PENDING and t.exit_signal_date is not None:
                got = decide(bars, t.exit_signal_date, self.started_at, done, may_miss=False)
                if isinstance(got, Fill):
                    self.book.fill_exit(
                        t.id, got.ts, exit_fill(got.open_, stock.params), got.late, now
                    )
            limit = self.cfg.gap_warn_pct / 100.0
            warning = gap_warning(bars, t.signal_date, limit)
            if warning is not None and t.warning is None:
                self.book.warn(t.id, warning, now)

    def _evaluate(self, now: dt.datetime, hist: Mapping[str, RuleHistory]) -> None:
        nh = hist[self.nifty_key]
        n_done = self._complete(nh, now)
        n_days = set(nh.days.index)
        for key, stock in self.stocks.items():
            h = hist[key]
            done = self._complete(h, now)
            last = self.book.last_evaluated(key)
            todo: list[dt.date] = []
            for d in h.days.index:
                if last is not None and d <= last:
                    continue
                if not done(d):
                    if self._past_close(d, now):
                        bar = h.last_bar.get(d)
                        at = bar.strftime("%H:%M") if bar else "none"
                        self._note(key, f"the session of {d} is incomplete (last bar {at})", d)
                        todo.append(d)  # marks the stop: nothing after it is read
                    break
                todo.append(d)
            if todo and not done(todo[-1]):
                todo = todo[:-1]
                stalled = True
            else:
                stalled = False
            if last is None:
                todo = todo[-1:]  # a first start reads the newest completed session only
            todo = todo[-CATCH_UP:]
            if not todo:
                if not stalled:
                    self.notes.pop(key, None)
                continue
            newest = todo[-1]
            for day in todo:
                why = problem(h, day, self.calendar, self.cfg.min_bars)
                if why is None and (self.calendar.is_trading_day(day) or day in n_days):
                    if not n_done(day):
                        why = f"NIFTY's session of {day.isoformat()} is not complete yet"
                    elif (nwhy := problem(nh, day, self.calendar, self.cfg.min_bars)) is not None:
                        why = f"NIFTY: {nwhy}"
                if why is not None:
                    self._note(key, why, day)
                    break
                if not stalled:
                    self.notes.pop(key, None)
                self._evaluate_session(now, stock, h, nh, day, day == newest and not stalled)

    def _past_close(self, day: dt.date, now: dt.datetime) -> bool:
        if day < now.date():
            return True
        return self.calendar.is_trading_day(day) and now >= (
            self.clock.window_for(day).close_at + self.settle
        )

    def _evaluate_session(
        self,
        now: dt.datetime,
        stock: Stock,
        h: RuleHistory,
        nh: RuleHistory,
        day: dt.date,
        newest: bool,
    ) -> None:
        p = stock.params
        frame = h.upto(day)
        dates = list(frame.index)
        c = frame["close"].to_numpy(float)
        nf = nh.upto(day)
        ok = nifty_ok(dates, list(nf.index), nf["close"].to_numpy(float), p)
        reading = entry_reading(c, ok, len(c) - 1, p)
        exits: list[dict[str, Any]] = []
        for t in self.book.active():
            if t.series_key != stock.series_key or t.status != OPEN or t.entry_session is None:
                continue
            if t.entry_session > day or t.entry_paise is None:
                continue
            if t.entry_session not in dates:
                self._note(
                    stock.series_key,
                    f"the entry session {t.entry_session} is not in the daily history",
                    day,
                )
                continue
            r = exit_reading(c, dates.index(t.entry_session), t.entry_paise / 100, len(c) - 1, p)
            exits.append({"trade": t.id, "sessions": r.sessions, "reasons": list(r.reasons)})
            if r.hit:
                self.book.signal_exit(t.id, day, r.reasons, r.sessions, now)
        busy = any(t.series_key == stock.series_key for t in self.book.active())
        entry = None
        if reading.signal and not busy:
            entry = NewEntry(
                stock.instrument_key,
                float(c[-1]),
                quantity(float(c[-1]), p.unit_inr),
                {"rule": p.__dict__, "reading": reading.as_dict(), "evidence": stock.evidence},
                PENDING_ENTRY if newest else MISSED,
                None if newest else "missed: the desk was not running after that close",
            )
        record = {**reading.as_dict(), "session": day.isoformat(), "exits": exits, "busy": busy}
        self.book.record_evaluation(stock.series_key, day, record, now, entry)

    def _note(self, key: str, why: str, day: dt.date | None = None) -> None:
        self.notes[key] = why
        mark = (key, f"{day}:{why}")
        if mark not in self._warned:
            self._warned.add(mark)
            log.warning("delivery: %s not evaluated%s: %s", key, f" for {day}" if day else "", why)

    # ── alerts (D2, D5, D14) ─────────────────────────────────────────────────
    def alert_text(self, t: DeliveryTrade, which: str) -> tuple[str, str]:
        stock = self.stocks.get(t.series_key)
        label = stock.label if stock else t.series_key
        symbol = stock.symbol if stock else t.series_key
        evidence = (stock.evidence if stock else "") or str(t.plan.get("evidence") or "")
        if which == "entry":
            title = f"BUY {t.qty} shares of {label} at the next open · pullback 1d (delivery)"
            body = (
                f"BUY {t.qty} shares of {symbol} at the next open (last close Rs {t.signal_close:,.2f}).\n"
                f"Pullback: the close of {t.signal_date:%d %b %Y} is 8% or more below its 20-session "
                f"closing high, above its SMA200, with NIFTY above its SMA200. Delivery, carried "
                f"for days: the exit alert says when to sell. Paper test: no order is placed."
            )
        else:
            reasons = [r for r in (t.exit_reason or "").split(",") if r]
            why = "; ".join(REASON_WORDS.get(r, r) for r in reasons)
            title = f"SELL {t.qty} shares of {label} at the next open · pullback exit ({', '.join(reasons)})"
            body = (
                f"SELL {t.qty} shares of {symbol} at the next open: {why} "
                f"(close of {t.exit_signal_date:%d %b %Y}, {t.sessions_held} of {stock.params.max_sessions if stock else 30} sessions). "
                f"Paper test: no order is placed."
            )
        if evidence:
            body += f"\n{evidence}"
        return title, body

    def _tell(self, now: dt.datetime) -> None:
        for t, which in self.book.untold():
            title, body = self.alert_text(t, which)
            sid = f"pb-{t.series_key}-{t.signal_date.isoformat()}-{which}"
            payload = {**t.as_dict(), "alert": which, "text": body}
            try:
                self.notifier.send(
                    Notification(
                        title=title,
                        body=body,
                        level=NotificationLevel.SIGNAL,
                        signal_id=sid,
                        ttl_seconds=self._ttl(now),
                        payload=payload,
                    ),
                    dedup_key=f"delivery:{sid}",
                )
            except Exception:  # noqa: BLE001 - retried at the next poll
                self.errors += 1
                log.exception("delivery: the %s alert of %s could not be sent", which, sid)
                continue
            self.book.mark_told(t.id, which, now)

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
        nh = hist[self.nifty_key]
        n_done = self._complete(nh, now)
        n_to = max((d for d in nh.days.index if n_done(d)), default=None)
        active = self.book.active()
        cards: dict[str, dict[str, Any]] = {}
        opened: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for key, stock in self.stocks.items():
            h = hist[key]
            done = self._complete(h, now)
            complete = [d for d in h.days.index if done(d)]
            to = complete[-1] if complete else None
            card: dict[str, Any] = {
                "evidence": stock.evidence,
                "history_to": to.isoformat() if to else None,
                "stored_to": stored.isoformat() if (stored := self._stored_to(key)) else None,
                "note": self.notes.get(key),
                "session": None,
                "reading": None,
            }
            if to is not None and problem(h, to, self.calendar, self.cfg.min_bars) is None:
                frame = h.upto(to)
                nf = nh.upto(to)
                ok = nifty_ok(
                    list(frame.index), list(nf.index), nf["close"].to_numpy(float), stock.params
                )
                r = entry_reading(frame["close"].to_numpy(float), ok, len(frame) - 1, stock.params)
                card["session"], card["reading"] = to.isoformat(), r.as_dict()
            mine = [t for t in active if t.series_key == key]
            card["trade"] = mine[0].as_dict() if mine else None
            cards[key] = card
            for t in mine:
                row = self._trade_row(t, stock, h, last_price(key))
                (pending if t.status in (PENDING_ENTRY, EXIT_PENDING) else opened).append(row)
                if t.status == EXIT_PENDING:
                    opened.append(row)
        self._cards = cards
        tos = [c["history_to"] for c in cards.values() if c["history_to"]]
        self._view = {
            "enabled": True,
            "strategy": self.book.strategy,
            "timeframe": self.book.timeframe,
            "pending": pending,
            "open": opened,
            "history_to": min(tos) if tos and len(tos) == len(cards) else None,
            "nifty_history_to": n_to.isoformat() if n_to else None,
            "stale": sorted(k for k, c in cards.items() if not c["history_to"] or c["note"]),
            "top_up": self.top_up_note,
            "errors": self.errors,
            "evidence": next((s.evidence for s in self.stocks.values() if s.evidence), ""),
        }

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
