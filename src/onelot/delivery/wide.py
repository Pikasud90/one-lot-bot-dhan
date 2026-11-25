"""The pullback rule as a PAPER test on the wide list (US-099). Never an order.

The 204 stocks of `config/delivery_universe.yaml` that are not desk instruments are read from
daily bars in an EVENING ROUND, once per finished session, after the close (never between 09:00
and 15:35), on the delivery desk's background thread:

1. Fetch (when the Data API is on): the last 30 or so daily bars of each stock through the
   desk's own adapter, at most two requests a second, one stock at a time; an error is noted
   and the others go on; a stock fetched for the session is not fetched again (resumable).
   ADJUSTMENTS: when the fetched closes differ from the stored ones on the overlap by more
   than 0.5% (a split, a bonus or a demerger re-based the history), the whole history is
   fetched again and replaces the stored one, and the round says so. When that refetch
   fails, the stock is STALE: it is not evaluated until a later round agrees or refetches.
2. Evaluate, with the desk 18's own pure code (`rule.entry_reading`, `rule.exit_reading`,
   `rule.nifty_ok`, `rule.quantity`, `data.problem`, `money.trade_result`): every session of a
   stock after the last one evaluated, day by day across the stocks. On each session a pending
   entry fills at that session's open (from its daily bar), an exit signalled before fills at
   that open, an open trade reads the rule's exits on the close, and a stock with no active
   trade reads the entry. A first round reads the newest session only.
3. Two tiers. Every signal becomes a paper trade (tier "all"). At most `cap_open` trades are
   open "within the cap" at a time, the desk 18's active trades counted; a signal of the
   round's own session takes a free place, the desk group first, then the deepest pullback
   first; a trade keeps its tier for life. Only a within-the-cap trade is ALERTED (entry and
   exit, "wide list"); the others are recorded, "not alerted: the cap of 10 is full". A
   signal read late (a catch-up of an earlier session) is recorded and never alerted.

State lives in three tables of its own (`wide_trades`, `wide_state`, `wide_rounds`): the desk
18's `delivery_trades` is never written here. Every trade change is a compare-and-set on its
status and a trade is unique per (stock, signal date), so a round run twice changes nothing
and a restart resumes where it stopped.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine, and_, delete, func, select, update
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError

from ..clock import IST, SessionClock, TradingCalendar
from ..costs import CostModel
from ..notify.base import Notification, NotificationLevel
from ..store.schema import METADATA, bars, delivery_trades, wide_rounds, wide_state, wide_trades
from .data import RuleHistory, join_live, problem
from .money import rupees_to_paise, trade_result
from .rule import (
    EXIT_ORDER,
    PullbackParams,
    entry_fill,
    entry_reading,
    exit_fill,
    exit_reading,
    nifty_ok,
    quantity,
)
from .universe import UniverseStock

log = logging.getLogger(__name__)

PENDING_ENTRY, OPEN, EXIT_PENDING, CLOSED = "PENDING_ENTRY", "OPEN", "EXIT_PENDING", "CLOSED"
ACTIVE = (PENDING_ENTRY, OPEN, EXIT_PENDING)
CAP, TEST = "cap", "test"
#: the round stays off between these times (IST), as the desk's top-up does
NO_ROUND = (dt.time(9, 0), dt.time(15, 35))
#: tries of one session's round, this far apart, before it is closed with what it has
ROUND_TRIES = 4
ROUND_GAP = dt.timedelta(minutes=30)
#: requests to the broker at most this often (two a second)
REQUEST_GAP_S = 0.5
#: calendar days of daily bars a round fetches (about 30 sessions)
FETCH_DAYS = 45
#: a fetched close this far from the stored one on the overlap means a re-based history
ADJUST_LIMIT = 0.005
#: where a whole-history refetch starts (the study's first day)
FULL_FROM = dt.date(2010, 1, 1)
#: an alert no channel accepted is tried again after this long
RETELL = dt.timedelta(minutes=5)

#: (stock, start, end) -> that stock's daily bars, tz-aware index, open/high/low/close/volume
FetchDaily = Callable[[UniverseStock, dt.date, dt.date], pd.DataFrame]


def _day(v: object) -> dt.date | None:
    return dt.date.fromisoformat(str(v)) if v else None


def _ist(v: dt.datetime | None) -> dt.datetime | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=dt.timezone.utc)
    return v.astimezone(IST)


@dataclass(frozen=True)
class WideTrade:
    id: int
    series_key: str
    group: str
    signal_date: dt.date
    signal_close: float
    depth_pct: float | None
    qty: int
    tier: str
    late: bool
    status: str
    note: str | None
    entry_session: dt.date | None
    entry_paise: int | None
    exit_signal_date: dt.date | None
    exit_reason: str | None
    sessions_held: int | None
    exit_session: dt.date | None
    exit_paise: int | None
    gross_paise: int | None
    charges_paise: int | None
    dp_paise: int | None
    net_paise: int | None
    net_pct: float | None
    entry_told_at: dt.datetime | None
    exit_told_at: dt.datetime | None

    @classmethod
    def from_row(cls, r: Mapping[Any, Any]) -> WideTrade:
        def i(v: object) -> int | None:
            return None if v is None else int(v)  # type: ignore[call-overload]

        return cls(
            int(r["id"]), str(r["series_key"]), str(r["grp"]),
            dt.date.fromisoformat(str(r["signal_date"])), float(r["signal_close"]),
            None if r["depth_pct"] is None else float(r["depth_pct"]), int(r["qty"]),
            str(r["tier"]), bool(r["late"]), str(r["status"]), r["note"],
            _day(r["entry_session"]), i(r["entry_paise"]), _day(r["exit_signal_date"]),
            r["exit_reason"], i(r["sessions_held"]), _day(r["exit_session"]), i(r["exit_paise"]),
            i(r["gross_paise"]), i(r["charges_paise"]), i(r["dp_paise"]), i(r["net_paise"]),
            None if r["net_pct"] is None else float(r["net_pct"]),
            _ist(r["entry_told_at"]), _ist(r["exit_told_at"]),
        )  # fmt: skip

    def as_dict(self) -> dict[str, Any]:
        def iso(v: dt.date | None) -> str | None:
            return None if v is None else v.isoformat()

        def rupees(p: int | None) -> float | None:
            return None if p is None else p / 100

        return {
            "id": self.id,
            "series_key": self.series_key,
            "group": self.group,
            "signal_date": iso(self.signal_date),
            "signal_close": self.signal_close,
            "depth_pct": None if self.depth_pct is None else round(self.depth_pct, 2),
            "qty": self.qty,
            "tier": self.tier,
            "late": self.late,
            "status": self.status,
            "note": self.note,
            "entry_session": iso(self.entry_session),
            "entry": rupees(self.entry_paise),
            "exit_signal_date": iso(self.exit_signal_date),
            "exit_reason": self.exit_reason,
            "sessions_held": self.sessions_held,
            "exit_session": iso(self.exit_session),
            "exit": rupees(self.exit_paise),
            "net": rupees(self.net_paise),
            "net_pct": None if self.net_pct is None else round(self.net_pct, 4),
        }


class WideBook:
    """The wide list's paper book and round state. One writer (the round's thread)."""

    def __init__(self, engine: Engine, model: CostModel, dp_paise: int) -> None:
        self.engine = engine
        self.model = model
        self.dp_paise = int(dp_paise)
        METADATA.create_all(engine, tables=[wide_trades, wide_state, wide_rounds])
        self.version = 0
        self._active: dict[str, WideTrade] | None = None

    # ── trades ──
    def _select(
        self, *where: Any, limit: int | None = None, newest: bool = False
    ) -> list[WideTrade]:
        order = wide_trades.c.id.desc() if newest else wide_trades.c.id
        stmt = select(wide_trades).where(*where).order_by(order)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.engine.connect() as conn:
            return [WideTrade.from_row(r) for r in conn.execute(stmt).mappings()]

    def all(self) -> list[WideTrade]:
        return self._select()

    def active(self) -> dict[str, WideTrade]:
        """The active trade of each stock (at most one: the rule's own "no open trade")."""
        if self._active is None:
            self._active = {t.series_key: t for t in self._select(wide_trades.c.status.in_(ACTIVE))}
        return dict(self._active)

    def cap_open(self) -> int:
        return sum(1 for t in self.active().values() if t.tier == CAP)

    def closed(self, limit: int | None = None) -> list[WideTrade]:
        return self._select(wide_trades.c.status == CLOSED, limit=limit, newest=True)

    def _changed(self) -> None:
        self.version += 1
        self._active = None

    def insert(
        self,
        stock: UniverseStock,
        signal_date: dt.date,
        close: float,
        depth: float | None,
        qty: int,
        tier: str,
        late: bool,
        note: str | None,
        plan: Mapping[str, Any],
        now: dt.datetime,
    ) -> WideTrade | None:
        """A new pending entry; None when that (stock, signal date) is recorded already."""
        try:
            with self.engine.begin() as conn:
                key = conn.execute(
                    wide_trades.insert().values(
                        series_key=stock.key,
                        grp=stock.group,
                        signal_date=signal_date.isoformat(),
                        signal_close=float(close),
                        depth_pct=depth,
                        qty=int(qty),
                        tier=tier,
                        late=bool(late),
                        status=PENDING_ENTRY,
                        note=note,
                        created_at=now,
                        plan=dict(plan),
                        updated_at=now,
                    )  # fmt: skip
                ).inserted_primary_key
        except IntegrityError:
            self._changed()
            return None
        self._changed()
        rows = self._select(wide_trades.c.id == int(key[0])) if key is not None else []
        return rows[0] if rows else None

    def _cas(self, trade_id: int, expected: Iterable[str], now: dt.datetime, **values: Any) -> bool:
        with self.engine.begin() as conn:
            res = conn.execute(
                update(wide_trades)
                .where(and_(wide_trades.c.id == trade_id, wide_trades.c.status.in_(expected)))
                .values(updated_at=now, **values)
            )
        self._changed()
        return bool(res.rowcount == 1)

    def fill_entry(self, t: WideTrade, session: dt.date, price: float, now: dt.datetime) -> bool:
        return self._cas(
            t.id, (PENDING_ENTRY,), now, status=OPEN, entry_session=session.isoformat(),
            entry_paise=rupees_to_paise(price),
        )  # fmt: skip

    def signal_exit(
        self, t: WideTrade, session: dt.date, reasons: Sequence[str], held: int, now: dt.datetime
    ) -> bool:
        return self._cas(
            t.id, (OPEN,), now, status=EXIT_PENDING, exit_signal_date=session.isoformat(),
            exit_reason=",".join(reasons), sessions_held=int(held),
        )  # fmt: skip

    def fill_exit(self, t: WideTrade, session: dt.date, price: float, now: dt.datetime) -> bool:
        if t.entry_paise is None:
            return False
        exit_paise = rupees_to_paise(price)
        r = trade_result(self.model, t.entry_paise, exit_paise, t.qty, self.dp_paise)
        return self._cas(
            t.id, (EXIT_PENDING,), now, status=CLOSED, exit_session=session.isoformat(),
            exit_paise=exit_paise, gross_paise=r.gross_paise, charges_paise=r.charges_paise,
            dp_paise=r.dp_paise, net_paise=r.net_paise, net_pct=r.net_pct,
        )  # fmt: skip

    def mark_told(self, trade_id: int, which: str, now: dt.datetime) -> None:
        col = {"entry": "entry_told_at", "exit": "exit_told_at"}[which]
        with self.engine.begin() as conn:
            conn.execute(update(wide_trades).where(wide_trades.c.id == trade_id).values({col: now}))
        self._changed()

    def untold(self) -> list[tuple[WideTrade, str]]:
        """Alerts of within-the-cap trades not yet accepted by a channel: (trade, which)."""
        out: list[tuple[WideTrade, str]] = []
        for t in self._select(wide_trades.c.tier == CAP, wide_trades.c.status.in_(ACTIVE)):
            if t.late:
                continue
            if t.status == PENDING_ENTRY and t.entry_told_at is None:
                out.append((t, "entry"))
            if t.status == EXIT_PENDING and t.exit_told_at is None:
                out.append((t, "exit"))
        return out

    # ── per-stock state ──
    def states(self) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(wide_state)).mappings().all()
        return {str(r["series_key"]): dict(r) for r in rows}

    def save_states(self, rows: Sequence[Mapping[str, Any]], now: dt.datetime) -> None:
        """Upsert per-stock state rows (only the columns given), in one transaction."""
        if not rows:
            return
        with self.engine.begin() as conn:
            for r in rows:
                values = {"stale": False, **r, "updated_at": now}
                stmt = sqlite.insert(wide_state).values(**values)
                cols = {k: getattr(stmt.excluded, k) for k in values if k != "series_key"}
                conn.execute(stmt.on_conflict_do_update(index_elements=["series_key"], set_=cols))
        self.version += 1

    # ── rounds ──
    def round(self, session: dt.date) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = (
                conn.execute(
                    select(wide_rounds).where(wide_rounds.c.session_date == session.isoformat())
                )
                .mappings()
                .first()
            )
        return None if r is None else dict(r)

    def last_round(self) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = (
                conn.execute(select(wide_rounds).order_by(wide_rounds.c.session_date.desc()))
                .mappings()
                .first()
            )
        return None if r is None else dict(r)

    def save_round(
        self,
        session: dt.date,
        started: dt.datetime,
        tries: int,
        summary: Mapping[str, Any],
        finished: dt.datetime | None,
    ) -> None:
        values = {
            "session_date": session.isoformat(),
            "started_at": started,
            "tries": int(tries),
            "summary": dict(summary),
            "finished_at": finished,
        }
        stmt = sqlite.insert(wide_rounds).values(**values)
        cols = {k: getattr(stmt.excluded, k) for k in values if k != "session_date"}
        with self.engine.begin() as conn:
            conn.execute(stmt.on_conflict_do_update(index_elements=["session_date"], set_=cols))
        self.version += 1

    # ── the score ──
    def score(self) -> dict[str, dict[str, Any]]:
        """Closed trades of both tiers: "all" (every signal) and "cap" (within the cap)."""
        out: dict[str, dict[str, Any]] = {}
        for tier, where in (("all", ()), ("cap", (wide_trades.c.tier == CAP,))):
            stmt = select(
                func.count(),
                func.sum(wide_trades.c.net_paise > 0),
                func.avg(wide_trades.c.net_pct),
                func.sum(wide_trades.c.net_paise),
            ).where(wide_trades.c.status == CLOSED, *where)
            with self.engine.connect() as conn:
                n, wins, avg, net = conn.execute(stmt).one()
            out[tier] = {
                "trades": int(n or 0),
                "winners": int(wins or 0),
                "net_pct": None if avg is None else round(float(avg), 3),
                "net": round((net or 0) / 100, 2),
            }
        return out


@dataclass
class StockDays:
    """One stock's daily bars as the rule reads them (`data.join_live` of the stored days)."""

    key: str
    hist: RuleHistory
    dates: list[dt.date]
    ords: np.ndarray
    open: np.ndarray
    close: np.ndarray

    @classmethod
    def of(cls, key: str, frame: pd.DataFrame) -> StockDays:
        h = join_live(key, frame, {}, None)
        dates = list(h.days.index)
        return cls(
            key,
            h,
            dates,
            np.array([d.toordinal() for d in dates], dtype=np.int64),
            h.days["open"].to_numpy(float),
            h.days["close"].to_numpy(float),
        )

    def last_at_or_before(self, day: dt.date) -> int:
        """Index of the last bar on or before `day`, or -1."""
        return int(np.searchsorted(self.ords, day.toordinal(), side="right")) - 1

    def index_of(self, day: dt.date) -> int | None:
        i = self.last_at_or_before(day)
        return i if i >= 0 and self.dates[i] == day else None


@dataclass
class _Candidate:
    stock: UniverseStock
    day: dt.date
    close: float
    depth: float
    reading: dict[str, Any]


@dataclass
class RoundResult:
    session: dt.date
    finished: bool
    summary: dict[str, Any] = field(default_factory=dict)


class WideDesk:
    """The evening round, its alerts and its view (see the module docstring)."""

    def __init__(
        self,
        engine: Engine,
        stocks: Sequence[UniverseStock],
        params: PullbackParams,
        model: CostModel,
        dp_paise: int,
        calendar: TradingCalendar,
        clock: SessionClock,
        *,
        cap_open: int = 10,
        evening_round: bool = True,
        min_bars: int = 201,
        notifier: Any = None,
        fetch: FetchDaily | None = None,
        data_api_active: Callable[[], bool] | None = None,
        nifty: Callable[[dt.datetime], tuple[list[dt.date], np.ndarray] | None] | None = None,
        desk_active: Callable[[], int] | None = None,
        source: Callable[[Sequence[str]], Mapping[str, StockDays]] | None = None,
        protected: Iterable[str] = (),
        backtest: Mapping[str, Any] | None = None,
        evidence: str = "",
        background: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.protected = {k.upper() for k in protected}
        self.stocks = {s.key: s for s in stocks if s.key.upper() not in self.protected}
        self.params = params
        self.calendar = calendar
        self.clock = clock
        self.cap = int(cap_open)
        self.evening_round = bool(evening_round)
        self.floor = max(int(min_bars), params.min_bars)
        self.notifier = notifier
        self._fetch = fetch
        self._data_api_active = data_api_active or (lambda: False)
        self._nifty = nifty or (lambda now: None)
        self._desk_active = desk_active or (lambda: 0)
        self._source = source
        self.backtest = dict(backtest or {})
        self.evidence = evidence
        self.book = WideBook(engine, model, dp_paise)
        self._background = background
        self._sleep, self._monotonic = sleep, monotonic
        self._last_request: float | None = None
        self._thread: threading.Thread | None = None
        self._last_try: dt.datetime | None = None
        self._retell_at: dict[tuple[int, str], dt.datetime] = {}
        self._told_version = -1
        self.note: str | None = None
        self.errors = 0
        self.requests = 0  # broker requests made: for the tests and the round's note
        self._summary: tuple[tuple[int, int | None], dict[str, Any]] | None = None

    # ── when a round runs ────────────────────────────────────────────────────
    def session_for(self, now: dt.datetime) -> dt.date | None:
        """The last finished session at `now`: today after 15:35 on a trading day, else the
        trading day before."""
        now = now.astimezone(IST)
        d = now.date() if now.time() > NO_ROUND[1] else now.date() - dt.timedelta(days=1)
        for _ in range(15):
            if self.calendar.is_trading_day(d):
                return d
            d -= dt.timedelta(days=1)
        return None

    def maybe_round(self, now: dt.datetime) -> None:
        """Start the evening round on its own thread when one is due: the switch on, outside
        09:00 to 15:35, the session's round not finished, at most `ROUND_TRIES` tries
        `ROUND_GAP` apart. Without a broker (no adapter or the Data API off) a round runs only
        when the store has daily bars of a wide-list stock to read."""
        if not self.evening_round or not self.stocks:
            return
        now = now.astimezone(IST)
        if NO_ROUND[0] <= now.time() <= NO_ROUND[1]:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        session = self.session_for(now)
        if session is None:
            return
        row = self.book.round(session)
        if row is not None and (row["finished_at"] is not None or int(row["tries"]) >= ROUND_TRIES):
            return
        if self._last_try is not None and now - self._last_try < ROUND_GAP:
            return
        if not self._can_fetch() and self._source is None and not self._store_has_bars():
            self.note = "no daily bars of the wide list in the store and no Dhan data"
            return
        self._last_try = now
        stop_at = dt.datetime.combine(
            now.date() + (dt.timedelta(days=1) if now.time() > NO_ROUND[0] else dt.timedelta()),
            NO_ROUND[0],
            tzinfo=IST,
        )

        def stop() -> bool:
            return dt.datetime.now(IST) >= stop_at

        def run() -> None:
            try:
                self.run_round(now, session, stop)
            except Exception:  # noqa: BLE001 - the round never stops the desk
                self.errors += 1
                self.note = f"the round of {session} failed: see the log"
                log.exception("wide list: the round of %s failed", session)

        if self._background:
            self._thread = threading.Thread(target=run, name="delivery-wide-round", daemon=True)
            self._thread.start()
        else:
            run()

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _can_fetch(self) -> bool:
        try:
            return self._fetch is not None and bool(self._data_api_active())
        except Exception:  # noqa: BLE001
            return False

    def _store_has_bars(self) -> bool:
        keys = list(self.stocks)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(bars.c.series_key)
                .where(bars.c.resolution == "1d", bars.c.series_key.in_(keys))
                .limit(1)
            ).first()
        return row is not None

    # ── one round ────────────────────────────────────────────────────────────
    def run_round(
        self,
        now: dt.datetime,
        session: dt.date | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> RoundResult:
        """The round of `session` (default: the last finished one). Idempotent: a finished
        round answers its summary; an unfinished one resumes stock by stock."""
        now = now.astimezone(IST)
        s = session or self.session_for(now)
        if s is None:
            return RoundResult(now.date(), False, {"note": "no trading day found"})
        row = self.book.round(s)
        if row is not None and row["finished_at"] is not None:
            return RoundResult(s, True, dict(row["summary"]))
        tries = (int(row["tries"]) if row is not None else 0) + 1
        started = _ist(row["started_at"]) if row is not None else None
        started = started or now
        summary: dict[str, Any] = {
            "session": s.isoformat(), "stocks": len(self.stocks), "fetched": 0, "refetched": [],
            "stale": [], "errors": [], "waiting": [], "scanned": 0, "signals": 0, "alerted": 0,
            "recorded": 0, "late": 0, "exits": 0, "note": None,
        }  # fmt: skip
        fetching = self._can_fetch()
        if fetching:
            self._fetch_all(s, now, summary, stop)
        elif self._source is None:
            summary["note"] = "no Dhan data: the round read the stored daily bars only"
        days = self._load(list(self.stocks))
        states = self.book.states()
        summary["stale"] = sorted(k for k, st in states.items() if st.get("stale"))
        nifty = self._nifty(now)
        if nifty is None or not len(nifty[0]) or nifty[0][-1] < s:
            summary["note"] = f"waiting for NIFTY's session of {s}"
            self.book.save_round(s, started, tries, summary, None)
            self.note = summary["note"]
            return RoundResult(s, False, summary)
        self._evaluate(s, days, nifty, states, now, summary)
        waiting = [
            k for k, d in days.items() if d.index_of(s) is None and k not in summary["stale"]
        ]
        summary["waiting"] = sorted(set(summary["waiting"]) | set(waiting))[:50]
        finished = (not fetching) or not summary["waiting"] or tries >= ROUND_TRIES
        self.book.save_round(s, started, tries, summary, now if finished else None)
        self.note = None
        return RoundResult(s, finished, summary)

    # ── fetching (step 1) ────────────────────────────────────────────────────
    def _pace(self) -> None:
        """At most two requests a second."""
        if self._last_request is not None:
            wait = REQUEST_GAP_S - (self._monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._monotonic()
        self.requests += 1

    def _fetch_all(
        self, s: dt.date, now: dt.datetime, summary: dict[str, Any], stop: Callable[[], bool] | None
    ) -> None:
        states = self.book.states()
        for key, stock in self.stocks.items():
            if stop is not None and stop():
                summary["errors"].append("stopped at 09:00: the next round goes on")
                break
            st = states.get(key) or {}
            if st.get("fetched_for") and str(st["fetched_for"]) >= s.isoformat():
                continue  # fetched for this session already (a resumed round)
            try:
                outcome = self._fetch_one(stock, s)
            except Exception as exc:  # noqa: BLE001 - one stock never stops the others
                self.errors += 1
                summary["errors"].append(f"{key}: {type(exc).__name__}: {str(exc)[:80]}")
                log.warning("wide list: the daily bars of %s could not be fetched: %s", key, exc)
                continue
            state: dict[str, Any] = {"series_key": key, "stale": outcome in ("stale",)}
            if outcome == "stale":
                summary["stale"].append(key)
                state["note"] = f"{s}: the history disagrees and its refetch failed: not evaluated"
            elif outcome == "waiting":
                summary["waiting"].append(key)
            else:
                summary["fetched"] += 1
                state["fetched_for"] = s.isoformat()
                state["note"] = None
                if outcome == "refetched":
                    summary["refetched"].append(key)
                    state["note"] = f"{s}: history re-based by the broker: fetched again whole"
            self.book.save_states([state], now)

    def _stored(self, key: str) -> pd.DataFrame:
        from ..data.store import _read_bars

        return _read_bars(self.engine, key, "1d")

    def _fetch_one(self, stock: UniverseStock, s: dt.date) -> str:
        """Fetch, check the overlap, store. "ok", "refetched", "stale" or "waiting"."""
        if stock.key.upper() in self.protected:
            raise ValueError(f"{stock.key} is a desk instrument: the round never writes it")
        stored = self._stored(stock.key)
        full = len(stored) < self.floor
        got: pd.DataFrame | None = None
        if not full:
            self._pace()
            got = self._fetch(stock, s - dt.timedelta(days=FETCH_DAYS), s)  # type: ignore[misc]
            if _disagrees(stored, got):
                full = True
        if full:
            self._pace()
            try:
                whole = self._fetch(stock, FULL_FROM, s)  # type: ignore[misc]
            except Exception:
                if len(stored) >= self.floor:
                    log.warning("wide list: %s disagrees and its refetch failed", stock.key)
                    return "stale"
                raise
            self._replace(stock.key, whole)
            outcome = "refetched" if len(stored) else "ok"
            got = whole
        else:
            self._save(stock.key, got)
            outcome = "ok"
        if got is None or not len(got) or _dates(got)[-1] < s:
            return "waiting"
        return outcome

    def _save(self, key: str, frame: pd.DataFrame | None) -> None:
        from ..data.store import save_bars

        if frame is None or not len(frame):
            return
        if key.upper() in self.protected:
            raise ValueError(f"{key} is a desk instrument: the round never writes it")
        save_bars(self.engine, key, "1d", frame, "dhan")

    def _replace(self, key: str, frame: pd.DataFrame) -> None:
        """The whole history replaced: the new bars written first, then every stored daily bar
        of the stock the new ones do not have deleted (never a moment with no history)."""
        if frame is None or not len(frame):
            raise ValueError(f"{key}: the refetch gave no bars")
        self._save(key, frame)
        keep = {_utc(t) for t in pd.DatetimeIndex(frame.index)}
        gone = [t for t in map(_utc, pd.DatetimeIndex(self._stored(key).index)) if t not in keep]
        for i in range(0, len(gone), 500):
            with self.engine.begin() as conn:
                conn.execute(
                    delete(bars).where(
                        bars.c.series_key == key,
                        bars.c.resolution == "1d",
                        bars.c.ts.in_(gone[i : i + 500]),
                    )
                )

    # ── evaluation (steps 2 and 3) ───────────────────────────────────────────
    def _load(self, keys: Sequence[str]) -> dict[str, StockDays]:
        if self._source is not None:
            return dict(self._source(keys))
        out: dict[str, StockDays] = {}
        for k in keys:
            try:
                frame = self._stored(k)
                if len(frame):
                    out[k] = StockDays.of(k, frame)
            except Exception:  # noqa: BLE001 - one stock never stops the others
                self.errors += 1
                log.exception("wide list: the daily bars of %s could not be read", k)
        return out

    def _evaluate(
        self,
        s: dt.date,
        days: Mapping[str, StockDays],
        nifty: tuple[list[dt.date], np.ndarray],
        states: Mapping[str, Mapping[str, Any]],
        now: dt.datetime,
        summary: dict[str, Any],
    ) -> None:
        p = self.params
        n_dates = [d for d in nifty[0] if d <= s]
        n_close = np.asarray(nifty[1], dtype=float)[: len(n_dates)]
        okmap = dict(zip(n_dates, nifty_ok(n_dates, n_dates, n_close, p).tolist(), strict=True))
        todo: dict[dt.date, list[tuple[str, int]]] = {}
        for key, sd in days.items():
            st = states.get(key) or {}
            if key not in self.stocks or st.get("stale"):
                continue
            i_s = sd.last_at_or_before(s)
            if i_s < 0:
                continue
            last = _day(st.get("last_session"))
            if last is None:
                idxs = [i_s]
            else:
                lo = sd.last_at_or_before(last) + 1
                idxs = list(range(lo, i_s + 1))
            for i in idxs:
                todo.setdefault(sd.dates[i], []).append((key, i))
        for d in sorted(todo):
            cands: list[_Candidate] = []
            rows: list[dict[str, Any]] = []
            for key, i in todo[d]:
                try:
                    cand, reading = self._session(key, days[key], i, d, okmap, now, summary)
                except Exception:  # noqa: BLE001 - one stock never stops the others
                    self.errors += 1
                    summary["errors"].append(f"{key} {d}: evaluation failed")
                    log.exception("wide list: the evaluation of %s on %s failed", key, d)
                    continue
                if cand is not None:
                    cands.append(cand)
                rows.append({"series_key": key, "last_session": d.isoformat(), "reading": reading})
                summary["scanned"] += d == s
            self._admit(d, s, cands, now, summary)
            self.book.save_states(rows, now)

    def _session(
        self,
        key: str,
        sd: StockDays,
        i: int,
        d: dt.date,
        okmap: Mapping[dt.date, bool],
        now: dt.datetime,
        summary: dict[str, Any],
    ) -> tuple[_Candidate | None, dict[str, Any]]:
        """One stock, one session: fills at its open, exits and entry on its close."""
        p = self.params
        c = sd.close[: i + 1]
        t = self.book.active().get(key)
        reading: dict[str, Any] = {"session": d.isoformat()}
        if t is not None and t.status == PENDING_ENTRY and t.signal_date < d:
            self.book.fill_entry(t, d, entry_fill(float(sd.open[i]), p), now)
            t = self.book.active().get(key)
        if t is not None and t.status == EXIT_PENDING and t.exit_signal_date is not None:
            if t.exit_signal_date < d:
                self.book.fill_exit(t, d, exit_fill(float(sd.open[i]), p), now)
                t = self.book.active().get(key)
        if t is not None and t.status == OPEN and t.entry_session is not None:
            e = sd.index_of(t.entry_session)
            if e is not None and e <= i and t.entry_paise is not None:
                r = exit_reading(c, e, t.entry_paise / 100, i, p)
                reading["trade"] = {"sessions": r.sessions, "reasons": list(r.reasons)}
                if r.hit and self.book.signal_exit(t, d, r.reasons, r.sessions, now):
                    summary["exits"] += 1
        if t is not None:
            reading["busy"] = True
            return None, reading
        lo = max(0, i + 1 - self.floor)
        window = RuleHistory(sd.hist.days.iloc[lo : i + 1], bad=sd.hist.bad)
        why = problem(window, d, self.calendar, self.floor)
        if why is not None:
            reading["entry_blocked"] = why
            return None, reading
        ok = np.zeros(i + 1, dtype=bool)
        ok[i] = bool(okmap.get(d, False))
        er = entry_reading(c, ok, i, p)
        reading.update(er.as_dict())
        if not er.signal:
            return None, reading
        depth = float(er.below_high_pct or 0.0)
        return _Candidate(self.stocks[key], d, float(c[-1]), depth, reading), reading

    def _admit(
        self,
        d: dt.date,
        s: dt.date,
        cands: list[_Candidate],
        now: dt.datetime,
        summary: dict[str, Any],
    ) -> None:
        """Every signal of session `d` becomes a pending entry; those of the round's own session
        take the free places within the cap: the desk group first, then the deepest."""
        if not cands:
            return
        late = d < s
        free = self.cap - self.book.cap_open() - (0 if late else self._desk_count())
        cands.sort(key=lambda c: (c.stock.group != "desk", -c.depth, c.stock.key))
        for c in cands:
            if late:
                tier, note = TEST, "read late (an earlier session's round did not run): recorded"
                note += " for the test, not alerted"
            elif free > 0:
                tier, note = CAP, None
                free -= 1
            else:
                tier = TEST
                note = f"recorded for the test, not alerted: the cap of {self.cap} is full"
            plan = {"rule": self.params.__dict__, "reading": c.reading}
            qty = quantity(c.close, self.params.unit_inr)
            got = self.book.insert(
                c.stock, d, c.close, c.depth, qty, tier, late, note, plan, now
            )  # fmt: skip
            if got is None:
                continue
            if late:
                summary["late"] += 1
            elif tier == CAP:
                summary["signals"] += 1
                summary["alerted"] += 1
            else:
                summary["signals"] += 1
                summary["recorded"] += 1

    def _desk_count(self) -> int:
        try:
            return int(self._desk_active())
        except Exception:  # noqa: BLE001 - unknown: the cap is treated as full (conservative)
            log.warning("wide list: the desk 18's open trades could not be counted")
            return self.cap

    # ── alerts ───────────────────────────────────────────────────────────────
    def alert_text(self, t: WideTrade, which: str) -> tuple[str, str]:
        stock = self.stocks.get(t.series_key)
        name = stock.name if stock else t.series_key
        symbol = stock.symbol if stock else t.series_key
        p = self.params
        if which == "entry":
            title = (
                f"BUY {t.qty} shares of {name} at the next open · pullback 1d (delivery, wide list)"
            )
            body = (
                f"BUY {t.qty} shares of {symbol} at the next open "
                f"(last close Rs {t.signal_close:,.2f}). Wide list ({t.group}): the close of "
                f"{t.signal_date:%d %b %Y} is {t.depth_pct or 0:.1f}% below its "
                f"{p.high_lookback}-session closing high, above its SMA{p.trend_sma}, with NIFTY "
                f"above its SMA{p.trend_sma}. Within the cap of {self.cap} open trades. Delivery, "
                f"carried for days: the exit alert says when to sell. Paper test: no order is placed."
            )
        else:
            reasons = [r for r in (t.exit_reason or "").split(",") if r]
            words = {
                "SMA20": f"the close is above its {p.exit_sma}-session average",
                "TIME": f"{p.max_sessions} sessions held",
                "STOP": f"the close is {p.stop * 100:g}% or more below the entry fill",
            }
            why = "; ".join(words.get(r, r) for r in reasons)
            title = (
                f"SELL {t.qty} shares of {name} at the next open · pullback exit, wide list "
                f"({', '.join(reasons)})"
            )
            body = (
                f"SELL {t.qty} shares of {symbol} at the next open: {why} (close of "
                f"{t.exit_signal_date:%d %b %Y}, {t.sessions_held} of {p.max_sessions} sessions). "
                f"Paper test: no order is placed."
            )
        if self.evidence:
            body += f"\n{self.evidence}"
        return title, body

    def notification(self, t: WideTrade, which: str) -> Notification:
        title, body = self.alert_text(t, which)
        sid = f"pbw-{t.series_key}-{t.signal_date.isoformat()}-{which}"
        return Notification(
            title=title,
            body=body,
            level=NotificationLevel.SIGNAL,
            signal_id=sid,
            ttl_seconds=24 * 3600,
            payload={
                "id": sid,
                "delivery": which,
                "wide": True,
                "signal_date": t.signal_date.isoformat(),
                "qty": t.qty,
                "status": t.status,
                "title": title,
                "text": body,
            },
        )

    def tell(self, now: dt.datetime) -> None:
        """Send the within-the-cap alerts not yet accepted; one refused is tried again after
        `RETELL`. Never while a round is writing (it is told at the next poll)."""
        if self.notifier is None or self.busy():
            return
        due = any(at <= now for at in self._retell_at.values())
        if self.book.version == self._told_version and not due:
            return
        for t, which in self.book.untold():
            mark = (t.id, which)
            if mark in self._retell_at and self._retell_at[mark] > now:
                continue
            n = self.notification(t, which)
            try:
                result = self.notifier.send(n, dedup_key=f"delivery:{n.signal_id}")
            except Exception:  # noqa: BLE001 - tried again later
                self.errors += 1
                self._retell_at[mark] = now + RETELL
                log.exception("wide list: the %s alert of %s could not be sent", which, n.signal_id)
                continue
            took = isinstance(result, Mapping) and (not result or any(result.values()))
            if not took:
                self.errors += 1
                self._retell_at[mark] = now + RETELL
                log.warning("wide list: no channel took the %s alert of %s", which, n.signal_id)
                continue
            self._retell_at.pop(mark, None)
            self.book.mark_told(t.id, which, now)
        self._told_version = self.book.version

    # ── what the page shows ──────────────────────────────────────────────────
    def summary(self) -> dict[str, Any]:
        """The compact block of the desk's snapshot, rebuilt only after a write."""
        desk = self._desk_count_quiet()
        hit = self._summary
        if hit is not None and hit[0] == (self.book.version, desk):
            return dict(hit[1])
        active = list(self.book.active().values())
        last = self.book.last_round()
        out = {
            "enabled": True,
            "stocks": len(self.stocks),
            "cap_open": self.cap,
            "cap_used": sum(1 for t in active if t.tier == CAP) + (desk or 0),
            "desk_active": desk,
            "open_cap": sum(1 for t in active if t.tier == CAP),
            "open_test": sum(1 for t in active if t.tier == TEST),
            "pending": [
                {**t.as_dict(), "name": self._name(t.series_key)}
                for t in active
                if t.tier == CAP and t.status in (PENDING_ENTRY, EXIT_PENDING) and not t.late
            ],
            "last_round": _round_view(last),
            "score": self.book.score(),
            "backtest": self.backtest,
            "note": self.note,
            "errors": self.errors,
        }
        self._summary = ((self.book.version, desk), out)
        return dict(out)

    def _desk_count_quiet(self) -> int | None:
        try:
            return int(self._desk_active())
        except Exception:  # noqa: BLE001
            return None

    def _name(self, key: str) -> str:
        s = self.stocks.get(key)
        return s.name if s else key

    def view(self, closed: int = 100, closest: int = 12) -> dict[str, Any]:
        """Everything `GET /api/delivery/wide` answers: the summary, the open trades of both
        tiers, the latest closed trades, the stale stocks and the stocks closest to a signal."""
        out = self.summary()
        active = sorted(self.book.active().values(), key=lambda t: (t.tier, t.signal_date))
        out["open"] = [{**t.as_dict(), "name": self._name(t.series_key)} for t in active]
        out["closed"] = [
            {**t.as_dict(), "name": self._name(t.series_key)} for t in self.book.closed(closed)
        ]
        states = self.book.states()
        out["stale"] = sorted(k for k, st in states.items() if st.get("stale"))
        near = []
        for k, st in states.items():
            r = st.get("reading") or {}
            if k not in self.stocks or r.get("busy") or r.get("below_high_pct") is None:
                continue
            if not (r.get("above_sma200") and r.get("nifty_above_sma200")):
                continue
            near.append(
                {
                    "series_key": k,
                    "name": self._name(k),
                    "group": self.stocks[k].group,
                    "session": r.get("session"),
                    "below_high_pct": r.get("below_high_pct"),
                    "needed_pct": r.get("needed_pct"),
                    "signal": bool(r.get("signal")),
                }
            )
        near.sort(key=lambda x: -float(x["below_high_pct"] or 0))
        out["closest"] = near[:closest]
        return out


def _round_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    s = dict(row.get("summary") or {})
    started, finished = _ist(row.get("started_at")), _ist(row.get("finished_at"))
    return {
        "session": row.get("session_date"),
        "started_at": None if started is None else started.isoformat(),
        "finished_at": None if finished is None else finished.isoformat(),
        "tries": row.get("tries"),
        "stocks": s.get("stocks"),
        "scanned": s.get("scanned"),
        "fetched": s.get("fetched"),
        "stale": list(s.get("stale") or []),
        "refetched": list(s.get("refetched") or []),
        "errors": list(s.get("errors") or [])[:10],
        "error_count": len(s.get("errors") or []),
        "waiting": list(s.get("waiting") or [])[:10],
        "signals": s.get("signals"),
        "alerted": s.get("alerted"),
        "recorded": s.get("recorded"),
        "late": s.get("late"),
        "exits": s.get("exits"),
        "note": s.get("note"),
    }


def _utc(t: Any) -> dt.datetime:
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    return ts.tz_convert("UTC").to_pydatetime()


def _dates(frame: pd.DataFrame) -> list[dt.date]:
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST)
    return sorted(set(idx.date))


def _closes(frame: pd.DataFrame) -> dict[dt.date, float]:
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST)
    return dict(zip(idx.date, frame["close"].to_numpy(float), strict=True))


def _disagrees(stored: pd.DataFrame, got: pd.DataFrame | None) -> bool:
    """A fetched close more than `ADJUST_LIMIT` from the stored one on a shared date, the
    newest shared date excepted when it is the stored history's last (a session stored before
    its final close may be revised once)."""
    if got is None or not len(got) or not len(stored):
        return False
    a, b = _closes(stored), _closes(got)
    shared = sorted(set(a) & set(b))
    if shared and shared[-1] == max(a):
        shared = shared[:-1]
    return any(a[d] > 0 and abs(b[d] / a[d] - 1.0) > ADJUST_LIMIT for d in shared)


def desk_active_count(engine: Engine) -> int:
    """The desk 18's active delivery trades (pending entry, open, exit pending): a fresh read,
    never the desk book's own cache (the round runs on another thread)."""
    stmt = select(func.count()).where(delivery_trades.c.status.in_(ACTIVE))
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar() or 0)


__all__ = [
    "ACTIVE",
    "CAP",
    "EXIT_ORDER",
    "TEST",
    "StockDays",
    "WideBook",
    "WideDesk",
    "WideTrade",
    "desk_active_count",
]
