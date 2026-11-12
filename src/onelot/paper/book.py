"""The paper book (ADR-001, US-001): every desk alert opens a paper trade.

`PaperBook.on_alert` records one PENDING_FILL trade per alert id (idempotent, thread
safe, persisted with its OPENED event in one transaction). `PaperBook.step` fills it on
the first closed bar after the alert and, from US-002, manages it to a close. The book
is evidence, not permission: it opens trades for risk-blocked and expired alerts too,
never touches the RiskGovernor, and can never send an order.

Money is integer paise (`onelot.paper.money`); times are tz-aware IST in memory and UTC
on disk like the rest of the schema; naive times are rejected, never guessed.
Dependencies are injected (no `current_settings()`), so the book is hermetic.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from functools import partial
from numbers import Real
from typing import Any, Literal, NamedTuple, cast

import numpy as np

# No pandas-stubs are pinned; every pandas module in the mypy baseline carries this one
# error, so a NEW module stays at zero only by naming it here (the flags are untouched).
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine, Text, and_, case, func, inspect, or_, select, type_coerce
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError
from sqlalchemy.types import NullType

from ..clock import IST, now_ist
from ..config import BacktestCfg, PaperCfg
from ..costs import CostModel
from ..data.universe import InstrumentSpec
from ..store.schema import METADATA, desk_signals, paper_events, paper_trades
from .alert import (
    Grade,
    PaperAlertError,
    alert_from_desk_signal,
    alert_id_of,
    grade_for,
    jsonable,
    trade_values,
)
from .charges import leg_sides, round_trip_paise
from .events import PaperEvent
from .fill import (
    Bars,
    FillDecision,
    decide_fill,
    infer_resolution_min,
    normalise_bars,
    option_entry_paise,
    option_exit_paise,
    spacing_gaps,
    stock_entry_paise,
    stock_exit_paise,
    stock_target_paise,
)
from .manage import (
    CLOSED,
    ManageRules,
    OhlcBars,
    PaperClose,
    PositionState,
    StrategyExit,
    TrailLevel,
    manage_view,
    normalise_ohlc,
    remember_exit,
)
from .money import stored_paise, to_paise
from .position import (
    initial_position,
    position_from_json,
    position_to_json,
    rules_from_alert,
    rules_from_json,
    rules_to_json,
    strategy_exit_of,
    trail_level_of,
)
from .pricing import frozen_iv, implied_iv
from .view import DayTotals, trade_view

# The public contract of this module (PaperAlertError and grade_for live in .alert).
__all__ = ["PaperAlertError", "PaperBook", "PaperEvent", "PaperTrade", "grade_for"]

log = logging.getLogger(__name__)

TradeStatus = Literal["PENDING_FILL", "OPEN", "NO_FILL", "CLOSED"]
TRADE_STATUSES: frozenset[str] = frozenset(("PENDING_FILL", "OPEN", "NO_FILL", "CLOSED"))
ACTIVE_STATUSES: tuple[TradeStatus, ...] = ("PENDING_FILL", "OPEN")
RETRY_CAP = 500  # failed on_alert calls kept in memory for a retry (oldest dropped)
STATUS_KEYS = ("opened", "filled", "no_fill", "closed", "hook_errors")
DEFAULT_TICK = 0.05  # rupees; only when the instrument spec is unknown
# A fill bar that gapped through a level: the row's close_reason (US-002 decision 3)
GAP_CLOSE_REASONS = {
    "gap_through_stop": "GAP_THROUGH_STOP",
    "gap_through_target": "GAP_THROUGH_TARGET",
}
RECENT_LIMIT = 200  # recent()'s default: the trades the desk's Paper panel is sent
READ_WARN_CAP = 1000  # corrupt-row reasons a book remembers having warned about (LRU)
REASON_KEY_CHARS = 200  # a reason is remembered by its first characters only
LAST_SESSION_SCAN = 50  # close times last_session() decodes, newest first, past bad ones

PremiumAt = Callable[[Mapping[str, Any], float, dt.datetime], float | None]
CostFor = Callable[[str], CostModel]
BarsFor = Callable[[str], pd.DataFrame | None]
# Answers a `manage.StrategyExit`, the desk's plain (reason, bar_close_ts) tuple, or None.
StrategyExitFor = Callable[["PaperTrade", dt.datetime], Any]
# Answers a `manage.TrailLevel`, the desk's plain (level in rupees, bar_close_ts) tuple, or None.
TrailLevelFor = Callable[["PaperTrade", dt.datetime], Any]


Booked = tuple[str, dt.datetime, dict[str, Any]]  # (event kind, data time, detail)
OpenedBy = Literal["on_alert", "reconcile"]  # OPENED detail.opened_by


class _Priced(NamedTuple):
    """A priced fill: entry and risk in paise, the inputs used, any extra events to book."""

    entry_paise: int
    risk_paise: int | None
    inputs: dict[str, Any]
    also: tuple[Booked, ...] = ()


@dataclass(frozen=True)
class PaperTrade:
    """One paper trade, mirroring its `paper_trades` row.

    Times are tz-aware IST; money is integer paise and stays None until a price exists.
    `plan` returns a fresh copy of the alert snapshot plus its levels in paise, so a
    caller can never change the book's state through a trade it was handed.
    """

    id: int
    alert_id: str
    status: TradeStatus
    close_reason: str | None
    grade: Grade
    data_source: str
    instrument_key: str
    series_key: str
    kind: str
    side: str
    direction: str
    strategy: str
    timeframe: str
    confidence: float | None
    product: str
    qty: int
    entry_ts: dt.datetime | None
    fill_bar_ts: dt.datetime | None
    exit_ts: dt.datetime | None
    last_bar_ts: dt.datetime | None
    resolution_min: int | None
    entry_paise: int | None
    exit_paise: int | None
    gross_paise: int | None
    costs_paise: int | None
    net_paise: int | None
    risk_paise: int | None
    created_at: dt.datetime
    expires_at: dt.datetime
    plan_json: str
    updated_at: dt.datetime

    @property
    def plan(self) -> dict[str, Any]:
        """{"alert": <alert snapshot>, "levels": {underlying_*_paise}} — a fresh copy."""
        return cast(dict[str, Any], json.loads(self.plan_json))

    @property
    def is_active(self) -> bool:
        """Still managed by the book: waiting for a fill, or open."""
        return self.status in ACTIVE_STATUSES

    @classmethod
    def from_row(cls, row: Mapping[Any, Any]) -> PaperTrade:
        """Build from a `paper_trades` row mapping (times come back UTC; made IST here)."""
        status = str(row["status"])
        if status not in TRADE_STATUSES:
            raise ValueError(f"paper_trades.id={row['id']}: unknown status {status!r}")
        grade = str(row["grade"])
        if grade not in ("live", "delayed"):
            raise ValueError(f"paper_trades.id={row['id']}: unknown grade {grade!r}")
        return cls(
            id=int(row["id"]),
            alert_id=str(row["alert_id"]),
            status=cast(TradeStatus, status),
            close_reason=row["close_reason"],
            grade=cast(Grade, grade),
            data_source=str(row["data_source"]),
            instrument_key=str(row["instrument_key"]),
            series_key=str(row["series_key"]),
            kind=str(row["kind"]),
            side=str(row["side"]),
            direction=str(row["direction"]),
            strategy=str(row["strategy"]),
            timeframe=str(row["timeframe"]),
            confidence=None if row["confidence"] is None else float(row["confidence"]),
            product=str(row["product"]),
            qty=int(row["qty"]),
            entry_ts=_ist_or_none(row["entry_ts"]),
            fill_bar_ts=_ist_or_none(row["fill_bar_ts"]),
            exit_ts=_ist_or_none(row["exit_ts"]),
            last_bar_ts=_ist_or_none(row["last_bar_ts"]),
            resolution_min=_int_or_none(row["resolution_min"]),
            entry_paise=_paise_or_none(row, "entry_paise"),
            exit_paise=_paise_or_none(row, "exit_paise"),
            gross_paise=_paise_or_none(row, "gross_paise"),
            costs_paise=_paise_or_none(row, "costs_paise"),
            net_paise=_paise_or_none(row, "net_paise"),
            risk_paise=_paise_or_none(row, "risk_paise"),
            created_at=_ist(row["created_at"]),
            expires_at=_ist(row["expires_at"]),
            plan_json=json.dumps(row["plan"], sort_keys=True, separators=(",", ":")),
            updated_at=_ist(row["updated_at"]),
        )


class PaperBook:
    """Opens a paper trade for every desk alert and manages it on closed bars.

    Thread safe: `on_alert` may be called from the desk thread while the API reads
    `open_trades()` / `status()`. Idempotent by alert id (UNIQUE in the table), across
    threads and across PaperBook instances on the same database.
    """

    def __init__(
        self,
        engine: Engine,
        cfg: PaperCfg,
        bt: BacktestCfg,
        specs: Mapping[str, InstrumentSpec],
        cost_for: CostFor,
        premium_at: PremiumAt,
        clock: Callable[[], dt.datetime] = now_ist,
        real_premium_at: PremiumAt | None = None,
    ) -> None:
        self.engine = engine
        self.cfg = cfg
        self.bt = bt
        self.specs = specs
        self.cost_for = cost_for
        self.premium_at = premium_at
        # The alerted contract's TRADED price (paper test, owner 2026-11-07): asked first at
        # every fill, exit and mark; None (no bar there yet, no feed) falls back to the model.
        self.real_premium_at = real_premium_at
        self.clock = clock
        self._lock = threading.RLock()
        self._active: dict[str, PaperTrade] = {}  # non-closed trades, by alert id
        self._counts: dict[str, int] = dict.fromkeys(STATUS_KEYS, 0)
        self._unpriced: set[tuple[int, dt.datetime]] = set()  # PRICING_UNAVAILABLE said once
        # on_alert calls that failed at runtime, retried by step (review 1, MINOR-3)
        self._retry: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._retry_logged: set[str] = set()  # retries whose traceback was already logged
        self._tracked: frozenset[str] | None = None  # the desk's series (retire_untracked)
        self._callback_failed: set[int] = set()  # trades whose strategy_exit traceback is logged
        # the strategy's own trail (its Supertrend line), asked for by `follow_trail`
        self._trail_level_for: TrailLevelFor | None = None
        self._trail_failed: set[int] = set()  # trades whose trail traceback is logged
        # options whose premium_at is failing now: warned once per streak (review 1 NIT)
        self._pricer_failing: set[tuple[str, str, str]] = set()
        # (trade id, reason) a read already warned about: the panel polls every few seconds,
        # so a repeat is DEBUG (US-030 review MINOR). Its own lock: reads never take _lock.
        self._read_warned: OrderedDict[tuple[object, str], None] = OrderedDict()
        self._read_warn_lock = threading.Lock()
        METADATA.create_all(engine, tables=[paper_trades, paper_events])  # no-op if present
        self._backfill_costs()  # also when disabled: it completes closes, it opens nothing
        self._load_active()
        if cfg.enabled:  # a disabled book opens nothing, not even at startup
            self._reconcile()

    # ── public API ────────────────────────────────────────────────────────
    def on_alert(self, alert: Mapping[str, Any], now: dt.datetime) -> PaperTrade | None:
        """Open (or return the already-open) paper trade for `alert`.

        Returns None and writes nothing when `paper.enabled` is false. Raises
        PaperAlertError for an alert it cannot record (the desk hook logs and counts it;
        the alert itself is never blocked by the paper book).

        A DATABASE failure (a locked or broken database, see `is_database_failure`)
        re-raises too, but first keeps a deep copy of the alert so the next step retries
        it: the trade is still booked exactly once, at the alert's own time (review 1,
        MINOR-3). Any other failure is a deterministic bug: it re-raises (the desk logs it
        once) and is never retried, since a retry would only repeat it (review 2).
        """
        _require_aware(now, "now")
        if not self.cfg.enabled:
            return None
        alert_id = alert_id_of(alert)
        try:
            trade, _ = self._open(alert_id, alert, now, opened_by="on_alert")
        except PaperAlertError:
            raise  # an unrecordable alert: a retry cannot fix it
        except Exception as exc:
            if is_database_failure(exc):
                self._keep_for_retry(alert_id, alert)
            raise
        self._forget_retry(alert_id)
        return trade

    def follow_trail(self, trail_level: TrailLevelFor | None) -> None:
        """Let OPEN trades whose alert froze `manage.line_trail` follow the strategy's own
        line. `trail_level(trade, now)` answers a `TrailLevel`, the desk's plain
        `(level in rupees, bar_close_ts)` tuple, or None; it is asked at every step, and only
        about such trades. The Desk registers it once, after it is built; a later call takes
        the earlier one's place and None switches it off. (It is registered here and not
        handed to `step`, whose signature is the US-001 public contract.)"""
        with self._lock:
            self._trail_level_for = trail_level

    def step(
        self,
        now: dt.datetime,
        bars: BarsFor,
        strategy_exit: StrategyExitFor | None = None,
    ) -> list[PaperEvent]:
        """Fill PENDING_FILL trades on closed bars (US-001) and manage OPEN ones to a close
        by the strategy's exit rules (US-002); a trade that fills is managed in the same
        call (decision A4).

        `bars(series_key)` is called ONCE per series with an active trade, and that frame is
        normalised once for every trade on it (AC8). `strategy_exit(trade, now)` is asked only
        about OPEN trades; it answers a `StrategyExit`, the desk's plain
        `(reason, bar_close_ts)` tuple, or None, and an answer that raises or makes no sense
        is logged and counted, never escapes. The callback given to `follow_trail` is asked
        in the same guarded way, only about OPEN trades whose alert froze
        `manage.line_trail`. A delayed-grade trade is managed on DATA time
        (`now` - paper.feed_delay_minutes), like its fill deadline.

        Each change commits with its paper_events rows in one transaction; a step with
        nothing new writes nothing. A failure on one trade is logged and counted in
        `hook_errors`; it never stops the other trades and never escapes. A database that
        fails as a whole (locked, read-only, a disk error) stops the step at the first
        failure, counted and logged once; the next poll retries (BUG-1, review 1; review 3).
        A naive `now` is rejected (ValueError), never guessed.

        Order: failed on_alert calls are retried, waiting trades on series the desk no
        longer tracks are retired (`retire_untracked`), then every series is stepped.
        """
        _require_aware(now, "now")
        now = now.astimezone(IST)
        events: list[PaperEvent] = []
        try:
            if not self._retry_failed_alerts(now):
                return []  # the database is locked: nothing else can be written this poll
            events.extend(self._retire_overdue_untracked(now))
            with self._lock:
                active = sorted(self._active.values(), key=lambda t: (t.created_at, t.id))
                trail = self._trail_level_for
            by_series: dict[str, list[PaperTrade]] = {}
            for trade in active:
                by_series.setdefault(trade.series_key, []).append(trade)
            for series_key, trades in by_series.items():
                # no bars at all: nothing fills, but an OPEN trade can still reach its
                # session-over close on the last close it saw (no overnight carry, AC5)
                views = _SeriesViews.read(bars, series_key) or _SeriesViews.empty(series_key)
                for trade in trades:
                    events.extend(
                        self._step_trade(trade.alert_id, views, now, strategy_exit, trail)
                    )
        except _StepStopped:
            pass  # logged and counted once; every untouched trade is retried next poll
        return events

    def _step_trade(
        self,
        alert_id: str,
        views: _SeriesViews,
        now: dt.datetime,
        strategy_exit: StrategyExitFor | None,
        trail_level: TrailLevelFor | None = None,
    ) -> list[PaperEvent]:
        """One trade: fill it if it waits, then manage it if it is (or just became) OPEN.
        The fill and the management are guarded apart, so a failed management never
        hides a fill that committed."""
        events: list[PaperEvent] = []
        trade = self._current(alert_id)
        if trade is not None and trade.status == "PENDING_FILL":
            waiting = views.fill
            if waiting is None:
                return events
            events += self._guarded(trade, partial(self._step_waiting, trade, waiting, now))
            trade = self._current(alert_id)
        if trade is not None and trade.status == "OPEN":
            work = partial(self._step_open, trade, views.ohlc, now, strategy_exit, trail_level)
            events += self._guarded(trade, work)
        return events

    def _current(self, alert_id: str) -> PaperTrade | None:
        with self._lock:
            return self._active.get(alert_id)

    def retire_untracked(self, series_keys: Iterable[str], now: dt.datetime) -> list[PaperEvent]:
        """Learn the series the desk tracks; retire waiting trades on any other series.

        An untracked series gets no bars, so it has no data time: a PENDING_FILL trade on it
        closes NO_FILL (detail reason "series_untracked") once the WALL clock is past its
        deadline (expires_at, + feed delay when delayed), here and at every later step.
        This is the one documented exception to decision 3 (review round 1, MINOR-4).
        Called by the Desk after it is built, and again after a watchlist reload.
        """
        _require_aware(now, "now")
        with self._lock:
            self._tracked = frozenset(str(key) for key in series_keys)
        try:
            return self._retire_overdue_untracked(now.astimezone(IST))
        except _StepStopped:
            return []

    def _keep_for_retry(self, alert_id: str, alert: Mapping[str, Any]) -> None:
        """Remember a failed alert (a deep JSON-safe copy); the oldest goes beyond RETRY_CAP."""
        with self._lock:
            self._retry[alert_id] = jsonable(alert)
            self._retry.move_to_end(alert_id)
            while len(self._retry) > RETRY_CAP:
                dropped, _ = self._retry.popitem(last=False)
                self._retry_logged.discard(dropped)
                log.warning(
                    "paper: retry set full (%d); alert %s dropped, a restart within "
                    "reconcile_days still books it",
                    RETRY_CAP,
                    dropped,
                )

    def _retry_failed_alerts(self, now: dt.datetime) -> bool:
        """Book the alerts whose on_alert hit a database failure, oldest first.

        A database failure is not alert-specific, so the phase stops at the first one
        (as BUG-1 does for fills): its traceback is logged once per alert, later failures of
        that alert are one-line warnings, and hook_errors counts at most once per step for
        this phase (review round 2). A non-database error at retry is a deterministic bug:
        logged once with its traceback and dropped, never retried. Returns False when the
        database is locked, so the step stops there too.
        """
        with self._lock:
            pending = list(self._retry.items())
        failed = False
        try:
            for alert_id, alert in pending:
                try:
                    self._open(alert_id, alert, now, opened_by="on_alert")
                except PaperAlertError as exc:  # not kept by on_alert; belt and braces
                    log.warning("paper: alert %s dropped from the retry set: %s", alert_id, exc)
                except Exception as exc:
                    failed = True
                    if not is_database_failure(exc):
                        log.exception(
                            "paper: retrying alert %s hit a non-database error; dropped from "
                            "the retry set, never retried",
                            alert_id,
                        )
                    else:
                        self._log_retry_failure(alert_id, exc)
                        locked = database_locked(exc)
                        if not locked:  # to the back: one bad alert never blocks the rest
                            with self._lock:
                                if alert_id in self._retry:
                                    self._retry.move_to_end(alert_id)
                        return not locked
                self._forget_retry(alert_id)
            return True
        finally:
            if failed:
                with self._lock:
                    self._counts["hook_errors"] += 1

    def _log_retry_failure(self, alert_id: str, exc: BaseException) -> None:
        """The traceback once per alert; a one-line warning on every later failure."""
        with self._lock:
            first = alert_id not in self._retry_logged
            self._retry_logged.add(alert_id)
        if first:
            log.error(
                "paper: retrying alert %s failed (database); kept, retried every step: %s",
                alert_id,
                _brief(exc),
                exc_info=exc,
            )
        else:
            log.warning("paper: retrying alert %s still failing: %s", alert_id, _brief(exc))

    def _forget_retry(self, alert_id: str) -> None:
        with self._lock:
            self._retry.pop(alert_id, None)
            self._retry_logged.discard(alert_id)

    def _retire_overdue_untracked(self, now: dt.datetime) -> list[PaperEvent]:
        with self._lock:
            tracked = self._tracked
            if tracked is None:  # the desk has not said: decision 3 alone applies
                return []
            overdue = sorted(
                (
                    t
                    for t in self._active.values()
                    if t.status == "PENDING_FILL"
                    and t.series_key not in tracked
                    and now > self._deadline(t)
                ),
                key=lambda t: (t.created_at, t.id),
            )
        events: list[PaperEvent] = []
        for trade in overdue:
            events.extend(self._guarded(trade, partial(self._close_untracked, trade, now)))
        return events

    def _close_untracked(self, trade: PaperTrade, now: dt.datetime) -> list[PaperEvent]:
        detail = {
            "reason": "series_untracked",
            "series_key": trade.series_key,
            "deadline": self._deadline(trade).isoformat(),
            "clock": "wall",  # no bars, no data time: the documented exception
        }
        values = _no_fill_values("NO_FILL", now)
        return self._commit(trade, values, [("NO_FILL", now, detail)], "no_fill")

    def _guarded(self, trade: PaperTrade, work: Callable[[], list[PaperEvent]]) -> list[PaperEvent]:
        """One trade's work. A failure is logged, counted and skipped. A database that
        fails as a whole stops the step: locked (BUG-1), read-only, a disk error (review
        round 3), i.e. any DBAPIError but the row-level IntegrityError and DataError. That
        is logged once without a traceback, counted once, then _StepStopped.

        Each write commits on its own, so a unit that commits one write and then fails (a
        REGRADED event, then a locked fill) leaves that committed event in the database
        but out of step()'s return value: paper_events, not the returned list, is the record.
        """
        try:
            return work()
        except (IntegrityError, DataError):  # one row's data: only this trade is skipped
            self._step_failed(trade)
            return []
        except DBAPIError as exc:  # the database as a whole: every other trade would fail too
            log.error(
                "paper: database %s at trade %s (alert %s); step stopped, the rest are "
                "retried on the next poll: %s",
                "is locked" if database_locked(exc) else "failed",
                trade.id,
                trade.alert_id,
                exc.orig,
            )
            with self._lock:
                self._counts["hook_errors"] += 1
            raise _StepStopped from None
        except Exception:
            self._step_failed(trade)
            return []

    def _step_failed(self, trade: PaperTrade) -> None:
        log.exception("paper: step failed for trade %s (alert %s)", trade.id, trade.alert_id)
        with self._lock:
            self._counts["hook_errors"] += 1

    def trade(self, trade_id: int) -> PaperTrade | None:
        """The stored trade with this id, read from the database (None if there is none).
        Never takes the book's lock."""
        return self._fetch_id(trade_id)

    def for_alert(self, alert_id: str) -> PaperTrade | None:
        """The paper trade of an alert: the managed copy while it is active, else the stored
        row (None when the alert never got one: paper off, or an alert from before the book).
        """
        with self._lock:
            active = self._active.get(alert_id)
        return active if active is not None else self._fetch(alert_id)

    def open_trades(self) -> list[PaperTrade]:
        """A copy of the trades still managed (PENDING_FILL or OPEN), oldest first."""
        with self._lock:
            trades = list(self._active.values())
        return sorted(trades, key=lambda t: (t.created_at, t.id))

    def status(self) -> dict[str, int]:
        """Counters since this book was built: opened, filled, no_fill, closed, hook_errors."""
        with self._lock:
            return dict(self._counts)

    @property
    def enabled(self) -> bool:
        """paper.enabled. False stops NEW paper trades only; open ones are still managed."""
        return bool(self.cfg.enabled)

    # ── read side (US-030): the desk's Paper panel ────────────────────────
    # Both read the database only. They never take `self._lock` (an API read must not
    # queue behind a step, BUG-2) and never touch the Desk.
    def recent(self, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
        """Up to `limit` trades, newest first (created_at, then id), as display dicts.

        The trades still managed (PENDING_FILL, OPEN) are picked first, whatever their age,
        then the newest final ones fill the rest: an old open trade never drops off the
        panel behind newer closed ones (Manager decision, 2026-11-01).

        Each is `onelot.paper.view.trade_view`: money as rupee strings from paise, times
        ISO-8601 IST, None for what is not known yet. A row that cannot be read (an unknown
        status, a corrupt plan or time, money that is not integer paise) is logged and left
        out, never fatal to the listing: the cells are fetched raw and decoded per row.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit={limit!r} must be a positive int")
        t = paper_trades.c
        newest_first = (t.created_at.desc(), t.id.desc())
        managed_first = case((t.status.in_(ACTIVE_STATUSES), 0), else_=1)
        shown = select(t.id).order_by(managed_first, *newest_first).limit(limit)
        raw = [type_coerce(c, NullType()).label(c.name) for c in paper_trades.c]
        stmt = select(*raw).where(t.id.in_(shown)).order_by(*newest_first)
        with self.engine.connect() as conn:
            decoders = {
                c.name: c.type.dialect_impl(conn.dialect).result_processor(conn.dialect, None)
                for c in paper_trades.c
            }
            rows = conn.execute(stmt).mappings().all()
        views: list[dict[str, Any]] = []
        for row in rows:
            try:
                stored = {k: _decoded(k, v, decoders[k]) for k, v in row.items()}
                views.append(trade_view(PaperTrade.from_row(stored)))
            except Exception as exc:  # noqa: BLE001 - one bad row never hides the others
                why = f"{type(exc).__name__}: {exc}"
                self._warn_once(
                    row["id"],
                    f"not shown: {why}",
                    "paper: trade %s (alert %s) not shown: %s",
                    row["id"],
                    row["alert_id"],
                    why,
                )
        return views

    def today_totals(self, now: dt.datetime) -> DayTotals:
        """`day_totals` of `now`'s IST calendar date. A naive `now` is rejected (ValueError)."""
        _require_aware(now, "now")
        return self.day_totals(now.astimezone(IST).date())

    def day_totals(self, day: dt.date) -> DayTotals:
        """The CLOSED trades closed on IST calendar date `day` (`closed_totals`).

        A trade is closed at its exit_ts, or at updated_at when it has none. NO_FILL trades
        never filled, so they are not counted. An amount that is not integer paise (a
        corrupt cell) is logged and read as unknown, so that sum is None, as for a missing
        one. `day` must be a plain date (a datetime carries a zone of its own: TypeError).
        """
        if not isinstance(day, dt.date) or isinstance(day, dt.datetime):
            raise TypeError(f"day={day!r} must be a datetime.date (an IST calendar date)")
        start = dt.datetime.combine(day, dt.time(0), tzinfo=IST)
        end = start + dt.timedelta(days=1)
        t = paper_trades.c
        closed_today = or_(
            and_(t.exit_ts >= start, t.exit_ts < end),
            and_(t.exit_ts.is_(None), t.updated_at >= start, t.updated_at < end),
        )
        stmt = select(t.id, t.gross_paise, t.net_paise).where(t.status == "CLOSED", closed_today)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return closed_totals(
            (
                self._paise_or_unknown(i, "gross_paise", gross),
                self._paise_or_unknown(i, "net_paise", net),
            )
            for i, gross, net in rows
        )

    def last_session(self, now: dt.datetime) -> dt.date | None:
        """The most recent IST date BEFORE `now`'s on which a CLOSED trade closed; only when no
        CLOSED trade closed before today, the most recent such date with a NO_FILL; else None.

        A trade closes at its exit_ts, or at updated_at when it has none (every NO_FILL), the
        rule `closed_at` and `day_totals` use. A no-fill alone never outranks a day with
        closes: a reconcile stamps late NO_FILLs with the day it ran, and that day must not
        hide the closes of the session before (US-032 review round 1 BLOCKER, the owner's
        Monday). Today is never the last session: the panel falls back to it when nothing
        has closed today. A close time that cannot be read is reported once and skipped; the
        newest LAST_SESSION_SCAN candidates of each kind are looked at. A naive `now` is
        rejected (ValueError).
        """
        _require_aware(now, "now")
        start = dt.datetime.combine(now.astimezone(IST).date(), dt.time(0), tzinfo=IST)
        return self._latest_close_day("CLOSED", start) or self._latest_close_day("NO_FILL", start)

    def _latest_close_day(self, status: TradeStatus, before: dt.datetime) -> dt.date | None:
        """The IST date of the newest readable close of a `status` trade before `before`."""
        t = paper_trades.c
        closed_at = func.coalesce(t.exit_ts, t.updated_at)  # typed as the columns: binds in UTC
        stmt = (
            select(t.id, type_coerce(closed_at, NullType()).label("closed_at"))  # decoded per row
            .where(t.status == status, closed_at < before)
            .order_by(closed_at.desc(), t.id.desc())
            .limit(LAST_SESSION_SCAN)
        )
        with self.engine.connect() as conn:
            decode = t.exit_ts.type.dialect_impl(conn.dialect).result_processor(conn.dialect, None)
            rows = conn.execute(stmt).all()
        for trade_id, raw in rows:
            try:
                when = decode(raw) if decode is not None else raw
                if not isinstance(when, dt.datetime):
                    raise ValueError(f"not a time: {when!r}")
                return _ist(when).date()
            except Exception as exc:  # noqa: BLE001 - one bad cell never hides the session
                self._warn_once(
                    trade_id,
                    f"close time {raw!r} unreadable",
                    "paper: trade %s close time %r is unreadable; last_session skips it: %s",
                    trade_id,
                    raw,
                    exc,
                )
        return None

    def _paise_or_unknown(self, trade_id: object, name: str, value: object) -> int | None:
        """today_totals(): a corrupt amount is reported (once) and read as unknown (None)."""
        if _is_paise(value):
            return cast(int | None, value)
        self._warn_once(
            trade_id,
            f"{name}={value!r} read as unknown",
            "paper: trade %s %s=%r is not integer paise; read as unknown",
            trade_id,
            name,
            value,
        )
        return None

    def _warn_once(self, trade_id: object, reason: str, msg: str, *args: object) -> None:
        """Log `msg` as a WARNING the first time this book reads `reason` on `trade_id`, and at
        DEBUG after that. The set is an LRU of READ_WARN_CAP entries: a reason it forgot is
        warned again, so memory stays bounded and nothing is ever silently dropped."""
        key = (trade_id, reason[:REASON_KEY_CHARS])
        with self._read_warn_lock:
            first = key not in self._read_warned
            self._read_warned[key] = None
            self._read_warned.move_to_end(key)
            while len(self._read_warned) > READ_WARN_CAP:
                self._read_warned.popitem(last=False)
        log.log(logging.WARNING if first else logging.DEBUG, msg, *args)

    # ── opening ───────────────────────────────────────────────────────────
    def _open(
        self, alert_id: str, alert: Mapping[str, Any], now: dt.datetime, opened_by: OpenedBy
    ) -> tuple[PaperTrade, bool]:
        """The trade for `alert_id`: the existing one, or a new PENDING_FILL one.

        Returns (trade, created). Shared by on_alert and the startup reconciliation, so both
        size, grade and stamp a trade the same way.

        The database is touched OUTSIDE `self._lock`, so open_trades() / status() never
        queue behind a write stuck on a busy database (BUG-2, review 1). Idempotency rests
        on the UNIQUE alert_id: a racing caller's insert fails and adopts the winner's row.
        Memory changes only after the commit.
        """
        with self._lock:
            known = self._active.get(alert_id)
        if known is not None:
            return known, False
        with _database_io():
            stored = self._fetch(alert_id)
        if stored is not None:
            with self._lock:
                return self._remember(stored), False
        values = trade_values(alert_id, alert, self.specs, now)  # pure: a bug here is a bug
        self._freeze_rules(values)
        if values["kind"] == "INDEX":
            values["plan"]["pricing"] = self._freeze_option_pricing(values["plan"])
        try:
            with _database_io():
                trade = self._insert_opened(values, opened_by)
        except IntegrityError:
            # Another thread or PaperBook on this database won the race for the alert id.
            with _database_io():
                raced = self._fetch(alert_id)
            if raced is None:
                raise
            with self._lock:
                return self._remember(raced), False
        with self._lock:
            self._counts["opened"] += 1
            self._remember(trade)
        log.info(
            "paper: opened trade %s for alert %s (%s %s %s, %s, qty %d)",
            trade.id,
            alert_id,
            trade.instrument_key,
            trade.side,
            trade.strategy,
            trade.grade,
            trade.qty,
        )
        return trade, True

    def _freeze_rules(self, values: dict[str, Any]) -> None:
        """Read the exit rules from the alert once, into the plan, and put a stock target on
        the tick grid against the trader (review round 1, MINOR-3 and NIT). Mutates `values`,
        the book's own new row."""
        plan = values["plan"]
        tick_paise = to_paise(self._tick_for(values["instrument_key"]))
        rules = rules_from_alert(
            plan["alert"], tick_paise=tick_paise, resolution=None, timeframe=values["timeframe"]
        )
        plan["rules"] = rules_to_json(rules)
        levels = plan["levels"]
        target = levels.get("underlying_target_paise")
        if values["kind"] == "STOCK" and target is not None:
            levels["underlying_target_paise"] = stock_target_paise(
                target, values["direction"], tick_paise
            )

    def _rules(self, trade: PaperTrade, plan: Mapping[str, Any]) -> ManageRules:
        """The rules frozen in the plan at the open; a row opened before that (US-001) has
        none, so its alert is read here instead (the step then stores them)."""
        stored = plan.get("rules")
        if isinstance(stored, Mapping):
            return rules_from_json(stored, resolution=trade.resolution_min)
        return rules_from_alert(
            plan["alert"],
            tick_paise=to_paise(self._tick(trade)),
            resolution=trade.resolution_min,
            timeframe=trade.timeframe,
        )

    def _freeze_option_pricing(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Freeze how the alerted option is priced, once, at the alert. Mutates `plan`, the
        book's own deep copy, and returns the plan's "pricing" record.

        IV: the alert's own, else implied from its chain premium (economics.entry_price) at
        the underlying entry and created_at (Manager decision h); if that fails the IV stays
        missing and every repricing is PRICING_UNAVAILABLE, never a constant IV.
        Basis: for a live-chain alert, chain premium minus the model premium at the alert,
        in paise (ADR-001 amendment 6). It is added to every premium the trade is priced at
        later, its fills and its marks alike (`_quote`, `_mark`; US-023 decision 1).
        """
        alert = plan["alert"]
        eco = alert["economics"]
        option = eco.get("option")
        if not isinstance(option, dict) or not option:
            return {"iv_source": "missing"}
        created = dt.datetime.fromisoformat(alert["created_at"])
        chain = eco.get("entry_price") or option.get("premium")
        underlying_paise = int(plan["levels"]["underlying_entry_paise"])
        pricing: dict[str, Any] = {"iv_source": "alert"}
        if frozen_iv(option) is None:
            rate = self.bt.risk_free_rate
            option["iv"] = implied_iv(option, chain, underlying_paise / 100, created, rate)
            pricing["iv_source"] = "implied" if option["iv"] is not None else "missing"
        if eco.get("synthetic") is False:  # a live-chain premium
            model, _ = self._premium(option, underlying_paise, created)
            try:
                pricing["basis_paise"] = (
                    None if model is None else to_paise(chain) - to_paise(model)
                )
            except ValueError:  # an unusable chain premium: no basis, never a guess
                pricing["basis_paise"] = None
        return pricing

    def _reconcile(self) -> int:
        """Open a trade for every desk_signals row that has none (ADR-001 amendment 10).

        A crash between the desk's insert and on_alert leaves such a row; so does an alert
        raised before any book existed. The trade is stamped with the ALERT's created_at
        (Manager decision 1), so it fills exactly as if the hook had run. Rows already
        booked are never touched. One unreadable row is logged and skipped, never fatal.
        Only rows raised since `reconcile_since(clock(), cfg.reconcile_days)` are looked at
        (Manager decision f): older alerts are not backfilled. Returns how many were opened.
        """
        if not inspect(self.engine).has_table(desk_signals.name):
            return 0  # a paper-only database: nothing to reconcile
        now = self.clock()
        _require_aware(now, "clock()")
        since = reconcile_since(now, self.cfg.reconcile_days)  # Manager decision (f)
        joined = desk_signals.outerjoin(paper_trades, paper_trades.c.alert_id == desk_signals.c.id)
        stmt = (
            select(desk_signals.c.id, type_coerce(desk_signals.c.payload, Text))  # raw JSON text
            .select_from(joined)
            .where(paper_trades.c.id.is_(None), desk_signals.c.created_at >= since)
            .order_by(desk_signals.c.created_at, desk_signals.c.id)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        if not rows:
            return 0
        opened = 0
        for signal_id, raw in rows:
            try:
                alert = alert_from_desk_signal(str(signal_id), raw)
                opened += self._open(str(signal_id), alert, now, opened_by="reconcile")[1]
            except Exception as exc:
                log.warning(
                    "paper: desk signal %s not reconciled (skipped): %s: %s",
                    signal_id,
                    type(exc).__name__,
                    exc,
                )
        log.info("paper: reconciled %d of %d unbooked desk signal(s)", opened, len(rows))
        return opened

    # ── the startup backfill of costs and net (US-023 NFR) ────────────────
    def _backfill_costs(self) -> int:
        """Book costs and net, once, for CLOSED rows a close left without them.

        US-002 closed trades with costs_paise/net_paise NULL; a close whose cost model failed
        (T1) does too. Each priced one (entry, exit and gross known, costs AND net NULL) gets
        exactly what a close books today: `charges.round_trip_paise` on its real sides from
        `cost_for(instrument_key)`, and net = gross - costs. It runs at every start, also with
        paper.enabled false (it opens nothing), and is idempotent: a costed row is never a
        candidate again, so a later start, even with other rates, changes nothing.

        Never a guess: a money cell that is not whole paise (`money.stored_paise`), a gross
        that disagrees with sign x (exit - entry) x qty, or a cost model that fails leaves the
        row NULL, warned and counted in hook_errors, with no event (Manager); a later start
        retries it. The costs are computed before any write, then every row is written in one
        transaction by an UPDATE guarded on the money it was computed from and on costs and net
        still being NULL, so a value another book wrote meanwhile is never overwritten. A
        database that fails defers the whole backfill to the next start (logged, counted);
        it never stops the book. Returns how many rows were written.
        """
        t = paper_trades.c
        stmt = (
            select(*(t[name] for name in _BACKFILL_COLUMNS))
            .where(
                t.status == "CLOSED",
                t.costs_paise.is_(None),
                t.net_paise.is_(None),
                t.entry_paise.is_not(None),
                t.exit_paise.is_not(None),
                t.gross_paise.is_not(None),
            )
            .order_by(t.id)
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(stmt).mappings().all()
        except DBAPIError as exc:
            self._backfill_deferred(exc)
            return 0
        if not rows:
            return 0
        planned: list[tuple[_Uncosted, int]] = []
        for row in rows:
            item = _uncosted(row)
            costs = None if item is None else self._backfill_leg_costs(item)
            if item is not None and costs is not None:
                planned.append((item, costs))
        failed = len(rows) - len(planned)
        written = self._write_backfill(planned)
        if failed:
            with self._lock:
                self._counts["hook_errors"] += failed
        log.info(
            "paper: backfilled costs and net for %d of %d closed trade(s); %d left NULL",
            written,
            len(rows),
            failed,
        )
        return written

    def _backfill_leg_costs(self, item: _Uncosted) -> int | None:
        """round_trip_paise for one candidate, or None (warned) when the cost model fails."""
        try:
            model = self.cost_for(item.instrument_key)
            return round_trip_paise(
                model, item.kind, item.direction, item.entry_paise, item.exit_paise, item.qty
            )
        except Exception as exc:
            log.warning(
                "paper: paper_trades.id=%s (alert %s) not backfilled, costs stay NULL until a "
                "start that can price them: %s: %s",
                item.trade_id,
                item.alert_id,
                type(exc).__name__,
                exc,
            )
            return None

    def _write_backfill(self, planned: list[tuple[_Uncosted, int]]) -> int:
        """Write the planned costs in one transaction, each UPDATE guarded; rows written."""
        if not planned:
            return 0
        t = paper_trades.c
        written = 0
        try:
            with self.engine.begin() as conn:
                for item, costs in planned:
                    guarded = paper_trades.update().where(
                        t.id == item.trade_id,
                        t.status == "CLOSED",
                        t.costs_paise.is_(None),
                        t.net_paise.is_(None),
                        t.entry_paise == item.entry_paise,
                        t.exit_paise == item.exit_paise,
                        t.gross_paise == item.gross_paise,
                    )
                    net = item.gross_paise - costs
                    changed = conn.execute(guarded.values(costs_paise=costs, net_paise=net))
                    if changed.rowcount != 1:  # another book costed (or changed) it first
                        log.info("paper: trade %s costed elsewhere; left as it is", item.trade_id)
                    written += max(changed.rowcount, 0)
        except DBAPIError as exc:
            self._backfill_deferred(exc)
            return 0
        return written

    def _backfill_deferred(self, exc: DBAPIError) -> None:
        log.error(
            "paper: the costs backfill could not use the database; deferred to the next start "
            "(nothing was written): %s",
            _brief(exc),
        )
        with self._lock:
            self._counts["hook_errors"] += 1

    # ── persistence ───────────────────────────────────────────────────────
    def _load_active(self) -> None:
        """Reload the PENDING_FILL and OPEN trades, one row at a time.

        One corrupt row (a JSON plan or a time that does not parse, an unknown grade) is
        logged, counted in hook_errors and skipped, so the book still starts and manages the
        others (US-023 carry-over); read together, one bad cell would fail every row. A
        database that cannot be read at all still raises: starting with no trades would
        leave them unmanaged without a word.
        """
        ids_stmt = (
            select(paper_trades.c.id)
            .where(paper_trades.c.status.in_(ACTIVE_STATUSES))
            .order_by(paper_trades.c.id)
        )
        with self.engine.connect() as conn:
            ids = [int(i) for i in conn.execute(ids_stmt).scalars().all()]
        loaded = skipped = 0
        for trade_id in ids:
            try:
                trade = self._fetch_id(trade_id)
            except (IntegrityError, DataError) as exc:  # this row's data, not the database
                skipped += self._skip_corrupt(trade_id, exc)
                continue
            except DBAPIError:
                raise
            except Exception as exc:
                skipped += self._skip_corrupt(trade_id, exc)
                continue
            if trade is None or not trade.is_active:  # closed or deleted meanwhile
                continue
            with self._lock:
                self._active[trade.alert_id] = trade
            loaded += 1
        if skipped:
            with self._lock:
                self._counts["hook_errors"] += skipped
        if loaded:
            log.info("paper: reloaded %d active paper trade(s)", loaded)

    @staticmethod
    def _skip_corrupt(trade_id: int, exc: Exception) -> int:
        log.warning(
            "paper: active paper_trades.id=%s is unreadable and was skipped (not managed until "
            "the row is repaired): %s: %s",
            trade_id,
            type(exc).__name__,
            exc,
        )
        return 1

    def _fetch_id(self, trade_id: int) -> PaperTrade | None:
        stmt = select(paper_trades).where(paper_trades.c.id == trade_id)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return PaperTrade.from_row(row) if row is not None else None

    def _fetch(self, alert_id: str) -> PaperTrade | None:
        stmt = select(paper_trades).where(paper_trades.c.alert_id == alert_id)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return PaperTrade.from_row(row) if row is not None else None

    def _remember(self, trade: PaperTrade) -> PaperTrade:
        if trade.is_active:
            self._active[trade.alert_id] = trade
        return trade

    def _insert_opened(self, values: Mapping[str, Any], opened_by: OpenedBy) -> PaperTrade:
        """Insert the trade and its OPENED event in ONE transaction; return the stored row."""
        detail = {
            "alert_status": values["plan"]["alert"].get("status"),
            "risk_allowed": values["plan"]["alert"].get("risk_allowed"),
            "grade": values["grade"],
            "data_source": values["data_source"],
            "resolution_min": values["resolution_min"],
            "qty": values["qty"],
            "product": values["product"],
            # who booked it, and the wall time it was booked at (the event ts stays the
            # alert's created_at): a reconciled trade is visibly late (review 1, MINOR-2)
            "opened_by": opened_by,
            "booked_at": values["updated_at"].astimezone(IST).isoformat(),
        }
        with self.engine.begin() as conn:
            pk = conn.execute(paper_trades.insert().values(**values)).inserted_primary_key
            if pk is None or pk[0] is None:  # pragma: no cover - SQLite always reports it
                raise RuntimeError(f"paper: no id returned for alert {values['alert_id']}")
            trade_id = int(pk[0])
            conn.execute(
                paper_events.insert().values(
                    trade_id=trade_id, ts=values["created_at"], kind="OPENED", detail=detail
                )
            )
            row = (
                conn.execute(select(paper_trades).where(paper_trades.c.id == trade_id))
                .mappings()
                .one()
            )
        return PaperTrade.from_row(row)

    # ── the fill (AC2/AC3) ────────────────────────────────────────────────
    def _step_waiting(self, trade: PaperTrade, view: Bars, now: dt.datetime) -> list[PaperEvent]:
        """Regrade if the feed mixes resolutions, then fill, close NO_FILL, or keep waiting."""
        resolution = trade.resolution_min or infer_resolution_min(view)
        events: list[PaperEvent] = []
        if trade.grade == "live":
            gaps = spacing_gaps(
                view, base_min=resolution, session_date=trade.created_at.date(), now=now
            )
            if gaps:  # before the fill decision: the delayed window then applies
                events = self._regrade(trade, gaps, resolution, now)
                with self._lock:
                    current = self._active.get(trade.alert_id)
                if current is None or current.status != "PENDING_FILL":
                    return events
                trade = current
        return events + self._fill_or_wait(trade, view, resolution, now)

    def _regrade(
        self, trade: PaperTrade, gaps: tuple[dt.datetime, ...], base_min: int, now: dt.datetime
    ) -> list[PaperEvent]:
        """live -> delayed, persisted with a REGRADED event (Manager decisions 6 and g).

        Never the other way: a feed that mixed resolutions once is not trusted as live again
        for this trade.
        """
        detail = {
            "from": trade.grade,
            "to": "delayed",
            "reason": "mixed_spacing",
            "base_min": base_min,
            "gap_bars": [ts.isoformat() for ts in gaps],
        }
        log.warning("paper: trade %s regraded delayed: mixed bar spacing %s", trade.id, gaps)
        values = {"grade": "delayed", "updated_at": now}
        return self._commit(trade, values, [("REGRADED", gaps[-1], detail)], counter=None)

    def _fill_or_wait(
        self, trade: PaperTrade, view: Bars, resolution: int, now: dt.datetime
    ) -> list[PaperEvent]:
        """Fill, close NO_FILL, or keep waiting one PENDING_FILL trade."""
        plan = trade.plan
        rules = self._rules(trade, plan)
        stop_paise = int(plan["levels"]["underlying_stop_paise"])
        target = plan["levels"].get("underlying_target_paise")
        decision = decide_fill(
            view,
            created_at=trade.created_at,
            deadline=self._deadline(trade),
            resolution_min=resolution,
            now=now,
            direction=trade.direction,
            stop_paise=stop_paise,
            target_paise=None if target is None else int(target),
            exit_time=rules.exit_time,
        )
        if decision.outcome == "wait":
            return []
        if decision.outcome == "no_fill":
            return self._close_no_fill(trade, decision, now)
        bar_ts, open_paise = decision.bar_ts, decision.bar_open_paise
        assert bar_ts is not None and open_paise is not None
        detail: dict[str, Any] = {
            "bar_ts": bar_ts.isoformat(),
            "bar_open_paise": open_paise,
            "resolution_min": resolution,
            "grade": trade.grade,
        }
        if trade.kind == "INDEX":
            priced = self._price_option(trade, plan, bar_ts, open_paise, stop_paise)
            if isinstance(priced, str):
                return self._entry_unpriced(trade, decision, detail | {"error": priced}, now)
        else:
            priced = self._price_stock(trade, open_paise, stop_paise)
            sign = 1 if trade.direction == "long" else -1
            if target is not None and (priced.entry_paise - int(target)) * sign >= 0:
                # the SLIPPED fill is at or past the target: a 'target' behind the entry
                # would book a loss as TARGET (review round 1, MAJOR-2)
                behind = replace(decision, outcome="no_fill", reason="gap_through_target")
                extra = {"entry_paise": priced.entry_paise, "target_paise": int(target)}
                return self._close_no_fill(trade, behind, now, extra)
        position = initial_position(
            trade_id=trade.id,
            direction=trade.direction,
            kind=trade.kind,
            qty=trade.qty,
            # the managed series' entry: a stock's slipped fill, an option's underlying open
            entry_paise=priced.entry_paise if trade.kind == "STOCK" else open_paise,
            levels=plan["levels"],
            opened_at=bar_ts,
            option=None,
        )
        values = {
            "status": "OPEN",
            "entry_paise": priced.entry_paise,
            "entry_ts": bar_ts,
            "fill_bar_ts": bar_ts,
            "last_bar_ts": bar_ts,
            "resolution_min": resolution,
            "risk_paise": priced.risk_paise,
            "plan": plan | {"position": position_to_json(position), "rules": rules_to_json(rules)},
            "updated_at": now,
        }
        detail |= priced.inputs | {
            "entry_paise": priced.entry_paise,
            "risk_paise": priced.risk_paise,
        }
        return self._commit(trade, values, [("FILLED", bar_ts, detail), *priced.also], "filled")

    # ── management (US-002) ───────────────────────────────────────────────
    def _step_open(
        self,
        trade: PaperTrade,
        view: OhlcBars,
        now: dt.datetime,
        strategy_exit: StrategyExitFor | None,
        trail_level: TrailLevelFor | None = None,
    ) -> list[PaperEvent]:
        """Manage one OPEN trade over the bars closed by its data time; book what changed."""
        plan = trade.plan
        state = self._position(trade, plan)
        rules = self._rules(trade, plan)
        data_now = self._data_now(trade, now)
        reported = self._ask_strategy(trade, now, strategy_exit)
        start = state
        if reported is not None and reported.bar_close_ts > data_now:
            # ahead of the trade's data time (a delayed feed fresher than feed_delay): kept
            # as pending, never dropped; it closes only at an open data time has reached
            start, reported = remember_exit(state, reported), None
        line = self._ask_trail(trade, now, trail_level) if rules.line_trail else None
        if line is not None and line.bar_close_ts > data_now:
            # ahead of the trade's data time: not due yet. Nothing is remembered: the desk
            # reports the strategy's latest level again at every poll.
            line = None
        new, managed = manage_view(start, view, rules, data_now, reported, line)
        if new == state and not managed and "rules" in plan:
            return []  # nothing new: no write at all
        before = plan.get("position")
        plan["position"] = position_to_json(new)
        plan["rules"] = rules_to_json(rules)  # already there, unless opened before US-002
        # the row's cursor never goes back to None before the fill bar is managed (NIT)
        last_bar_ts = new.last_bar_ts or trade.last_bar_ts
        values: dict[str, Any] = {"plan": plan, "last_bar_ts": last_bar_ts, "updated_at": now}
        booked: list[Booked] = [(e.kind, e.ts, dict(e.detail)) for e in managed]
        if new.closed is None:
            if trade.kind == "INDEX":
                booked += self._mark(trade, plan, before, state, new)
            return self._commit(trade, values, booked, counter=None)
        exit_paise, also, source = self._price_exit(trade, plan, new.closed)
        for kind, _, detail in booked:
            if kind == CLOSED:
                detail["exit_paise"] = exit_paise  # the rule level stays in price_paise (A13)
                if source is not None:
                    detail["exit_premium_source"] = source
        gross = _gross(trade, exit_paise)
        costs, uncosted = self._costs(trade, exit_paise, new.closed.at)
        net = None if gross is None or costs is None else gross - costs
        values |= {
            "status": "CLOSED",
            "close_reason": new.closed.reason,
            "exit_ts": new.closed.at,
            "exit_paise": exit_paise,
            "gross_paise": gross,
            # booked in the same transaction as the close (US-023 AC1); NULL without a price
            "costs_paise": costs,
            "net_paise": net,
        }
        log.info(
            "paper: trade %s (alert %s) closed %s at %s, exit %s, gross %s, costs %s, net %s paise",
            trade.id,
            trade.alert_id,
            new.closed.reason,
            new.closed.at.isoformat(),
            exit_paise,
            gross,
            costs,
            net,
        )
        return self._commit(trade, values, [*booked, *also, *uncosted], counter="closed")

    def _costs(
        self, trade: PaperTrade, exit_paise: int | None, at: dt.datetime
    ) -> tuple[int | None, list[Booked]]:
        """The trade's charges in paise (US-023 AC1), or None with why.

        Two legs on their real sides, each rounded once half-up (`charges.round_trip_paise`),
        from the injected `cost_for(instrument_key)`. No exit price: None, nothing asked (the
        exit's PRICING_UNAVAILABLE already says why). A cost model that fails: the close is
        still booked, costs and net stay NULL (never a fake 0), a COSTS_UNAVAILABLE event says
        why, and the failure is logged and counted in hook_errors.
        """
        if exit_paise is None or trade.entry_paise is None:
            return None, []
        try:
            model = self.cost_for(trade.instrument_key)
            costs = round_trip_paise(
                model, trade.kind, trade.direction, trade.entry_paise, exit_paise, trade.qty
            )
        except Exception as exc:
            log.exception(
                "paper: trade %s (alert %s) closed without costs: the cost model failed",
                trade.id,
                trade.alert_id,
            )
            with self._lock:
                self._counts["hook_errors"] += 1
            detail = {"error": f"{type(exc).__name__}: {exc}", "exit_paise": exit_paise}
            return None, [("COSTS_UNAVAILABLE", at, detail)]
        return costs, []

    def _mark(
        self,
        trade: PaperTrade,
        plan: dict[str, Any],
        before: object,
        state: PositionState,
        new: PositionState,
    ) -> list[Booked]:
        """Mark an OPEN option at the last close managed (US-023 AC3); mutates `plan`.

        A new close: mark_paise = max(to_paise(premium_at(option, close, close time)) + basis,
        one tick), the option frozen at the alert; rounded once (HALF_EVEN, `to_paise`). No
        new close (a step that only remembered an exit, or skipped unusable bars): the mark
        stays what it was. A mark that cannot be priced is None, never 0 and never the stale
        one, with ONE PRICING_UNAVAILABLE leg="mark" event per failure streak: a later
        failure books nothing, a recovery books nothing, a failure after it is a new event.
        """
        record = plan["position"]
        known = before if isinstance(before, Mapping) else {}
        close, at = new.last_close_paise, new.last_close_at
        if close is None or at is None or at == state.last_close_at:
            if "mark_paise" in known:
                record["mark_paise"] = known["mark_paise"]
            return []
        option = plan["alert"]["economics"].get("option") or {}
        tick = to_paise(self._tick(trade))
        real = self._real_premium(option, close, at)
        if real is not None:  # the contract's traded price: no basis
            record["mark_paise"] = max(to_paise(real), tick)
            return []
        fair, why = self._premium(option, close, at)
        if fair is not None:
            record["mark_paise"] = max(to_paise(fair) + _basis_paise(plan), tick)
            return []
        record["mark_paise"] = None
        if "mark_paise" in known and known["mark_paise"] is None:
            return []  # the streak was already said
        return [
            ("PRICING_UNAVAILABLE", at, {"leg": "mark", "underlying_paise": close, "error": why})
        ]

    def _quote(
        self, trade: PaperTrade, plan: Mapping[str, Any], underlying_paise: int, ts: dt.datetime
    ) -> tuple[float | None, str | None]:
        fair, why, _ = self._quote_ex(trade, plan, underlying_paise, ts)
        return fair, why

    def _quote_ex(
        self, trade: PaperTrade, plan: Mapping[str, Any], underlying_paise: int, ts: dt.datetime
    ) -> tuple[float | None, str | None, str]:
        """(premium, why not, source) a fill is priced from, never below one tick.

        "real": the alerted contract's traded price at that moment and underlying level
        (`real_premium_at`), taken as it is. Otherwise "model": the model plus the live-chain
        basis (Manager decision 1, option A: entry, exit and the risk leg alike, so a
        live-chain trade never shows P&L at birth); a synthetic alert has no basis: the
        model's premium, untouched (the Lab differential stays exact)."""
        option = plan["alert"]["economics"].get("option") or {}
        real = self._real_premium(option, underlying_paise, ts)
        if real is not None:
            return max(real, self._tick(trade)), None, "real"
        fair, why = self._premium(option, underlying_paise, ts)
        basis = _basis_paise(plan)
        if fair is None or basis == 0:
            return fair, why, "model"
        return max(fair + basis / 100, self._tick(trade)), None, "model"

    def _real_premium(
        self, option: Mapping[str, Any], underlying_paise: int, ts: dt.datetime
    ) -> float | None:
        """real_premium_at, or None: a failing feed is logged and the model prices it."""
        if self.real_premium_at is None or not option:
            return None
        try:
            value = self.real_premium_at(dict(option), underlying_paise / 100, ts)
            price = None if value is None else float(value)
        except Exception as exc:  # noqa: BLE001 - the model is the fallback, never a guess
            log.debug("paper: real premium unavailable for %s at %s: %s", option, ts, exc)
            return None
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        return price

    def _position(self, trade: PaperTrade, plan: Mapping[str, Any]) -> PositionState:
        """The managed state: the plan's record, or (a row filled before US-002) the fill."""
        option = plan["alert"]["economics"].get("option") or None
        record = plan.get("position")
        if isinstance(record, Mapping):
            return position_from_json(
                record,
                trade_id=trade.id,
                direction=trade.direction,
                kind=trade.kind,
                qty=trade.qty,
                option=option,
            )
        if trade.entry_paise is None or trade.fill_bar_ts is None:
            raise ValueError(f"paper: OPEN trade {trade.id} has no entry to manage from")
        entry = trade.entry_paise if trade.kind == "STOCK" else self._filled_open(trade)
        return initial_position(
            trade_id=trade.id,
            direction=trade.direction,
            kind=trade.kind,
            qty=trade.qty,
            entry_paise=entry,
            levels=plan["levels"],
            opened_at=trade.fill_bar_ts,
            option=option,
        )

    def _filled_open(self, trade: PaperTrade) -> int:
        """The underlying open an option trade filled at, from its FILLED event."""
        stmt = select(paper_events.c.detail).where(
            paper_events.c.trade_id == trade.id, paper_events.c.kind == "FILLED"
        )
        with _database_io(), self.engine.connect() as conn:
            detail = conn.execute(stmt).scalar_one()
        return int(detail["bar_open_paise"])

    def _data_now(self, trade: PaperTrade, now: dt.datetime) -> dt.datetime:
        """The data horizon of a trade: `now`, less the feed delay for a delayed grade."""
        if trade.grade == "delayed":
            return now - dt.timedelta(minutes=self.cfg.feed_delay_minutes)
        return now

    def _ask_strategy(
        self, trade: PaperTrade, now: dt.datetime, ask: StrategyExitFor | None
    ) -> StrategyExit | None:
        """The strategy's exit for an OPEN trade, or None. A callback that raises or answers
        nonsense is counted every time, logged with its traceback once per trade and quietly
        after that (review round 1 NIT), and the trade is still managed."""
        if ask is None:
            return None
        try:
            return strategy_exit_of(ask(trade, now))
        except Exception:
            with self._lock:
                self._counts["hook_errors"] += 1
                first = trade.id not in self._callback_failed
                self._callback_failed.add(trade.id)
            if first:
                log.exception(
                    "paper: strategy_exit failed for trade %s (alert %s); further failures "
                    "for it are logged at debug",
                    trade.id,
                    trade.alert_id,
                )
            else:
                log.debug("paper: strategy_exit failed again for trade %s", trade.id)
            return None

    def _ask_trail(
        self, trade: PaperTrade, now: dt.datetime, ask: TrailLevelFor | None
    ) -> TrailLevel | None:
        """The level the strategy's own line allows for an OPEN trade, or None. Guarded as
        `_ask_strategy` is: a callback that raises or answers nonsense is counted every time,
        logged with its traceback once per trade, and the trade is still managed (on the
        stop it has)."""
        if ask is None:
            return None
        try:
            return trail_level_of(ask(trade, now))
        except Exception:
            with self._lock:
                self._counts["hook_errors"] += 1
                first = trade.id not in self._trail_failed
                self._trail_failed.add(trade.id)
            if first:
                log.exception(
                    "paper: trail_level failed for trade %s (alert %s); further failures "
                    "for it are logged at debug",
                    trade.id,
                    trade.alert_id,
                )
            else:
                log.debug("paper: trail_level failed again for trade %s", trade.id)
            return None

    def _price_exit(
        self, trade: PaperTrade, plan: Mapping[str, Any], close: PaperClose
    ) -> tuple[int | None, list[Booked], str | None]:
        """The exit fill for the rule level the trade closed at (Manager decision 2).

        A stock mirrors the Lab: slipped by slippage_pct against the trader and snapped to
        the tick against the trader, except a TARGET (a limit fill, unslipped). An option is
        sold at `option_exit_paise` of the premium (`_quote`: model + live-chain basis) at the
        UNSLIPPED underlying level and the close time, the option frozen at the alert; if that
        premium is unavailable the close is still booked, with no exit price and a
        PRICING_UNAVAILABLE event (amendment 6).
        A close with no rule level (a session that ended before one usable bar, MINOR-1) is
        booked the same way.
        """
        if close.price_paise is None:
            unpriced: dict[str, Any] = {
                "leg": "exit",
                "underlying_paise": None,
                "error": "no usable bar was managed before the session ended: no price is invented",
            }
            return None, [("PRICING_UNAVAILABLE", close.at, unpriced)], None
        if trade.kind != "INDEX":
            if close.reason == "TARGET":
                return close.price_paise, [], None
            tick = to_paise(self._tick(trade))
            return (
                stock_exit_paise(close.price_paise, trade.direction, self.bt.slippage_pct, tick),
                [],
                None,
            )
        fair, why, source = self._quote_ex(trade, plan, close.price_paise, close.at)
        if fair is None:
            problem = {"leg": "exit", "underlying_paise": close.price_paise, "error": why}
            return None, [("PRICING_UNAVAILABLE", close.at, problem)], None
        bt = self.bt
        return (
            option_exit_paise(
                fair, bt.option_spread_pct, bt.option_slippage_ticks, self._tick(trade)
            ),
            [],
            source,
        )

    def _deadline(self, trade: PaperTrade) -> dt.datetime:
        """expires_at, plus the feed delay for a delayed-grade trade (ADR-001 amendment 3)."""
        if trade.grade == "delayed":
            return trade.expires_at + dt.timedelta(minutes=self.cfg.feed_delay_minutes)
        return trade.expires_at

    def _price_stock(self, trade: PaperTrade, open_paise: int, stop_paise: int) -> _Priced:
        """One share: the open slipped by slippage_pct (a PERCENT), snapped against the trader."""
        tick_paise = to_paise(self._tick(trade))
        entry = stock_entry_paise(open_paise, trade.direction, self.bt.slippage_pct, tick_paise)
        risk = abs(entry - stop_paise) * trade.qty
        # 0 only when an on-tick open AT the stop fills with zero slippage: R is undefined then.
        return _Priced(entry, risk or None, {"slippage_pct": self.bt.slippage_pct})

    def _price_option(
        self,
        trade: PaperTrade,
        plan: Mapping[str, Any],
        bar_ts: dt.datetime,
        open_paise: int,
        stop_paise: int,
    ) -> _Priced | str:
        """One lot, priced like `execute_trade`; or why the fill bar cannot be priced.

        The premium is the model's plus the live-chain basis (`_quote`; none for a synthetic
        alert). risk = (entry premium - the stop EXIT fill the Lab would book) x qty, the stop
        leg priced at the underlying stop with the option frozen at the alert (Manager decision
        (a): paper R and Lab R are comparable). If that repricing fails, or the stop exit is
        not below the entry, the trade still fills with risk_paise NULL and a
        PRICING_UNAVAILABLE event (ADR-001 amendment 6).
        """
        fair, why, source = self._quote_ex(trade, plan, open_paise, bar_ts)
        if fair is None:
            return why or "no premium"
        bt = self.bt
        tick = self._tick(trade)
        entry = option_entry_paise(fair, bt.option_spread_pct, bt.option_slippage_ticks, tick)
        inputs = {
            "fair_premium_paise": to_paise(fair),
            "option_spread_pct": bt.option_spread_pct,
            "option_slippage_ticks": bt.option_slippage_ticks,
            "premium_source": source,
        }
        basis = _basis_paise(plan)
        if basis and source == "model":
            inputs["basis_paise"] = basis  # included in fair_premium_paise
        stop_fair, why = self._quote(trade, plan, stop_paise, bar_ts)
        risk = None
        if stop_fair is not None:
            spread, ticks = bt.option_spread_pct, bt.option_slippage_ticks
            risk = (entry - option_exit_paise(stop_fair, spread, ticks, tick)) * trade.qty
        if risk is not None and risk > 0:
            return _Priced(entry, risk, inputs)
        problem = {
            "leg": "risk",
            "underlying_stop_paise": stop_paise,
            "error": why or f"premium at the stop {stop_fair!r} is not below the entry premium",
        }
        return _Priced(entry, None, inputs, (("PRICING_UNAVAILABLE", bar_ts, problem),))

    def _close_no_fill(
        self,
        trade: PaperTrade,
        decision: FillDecision,
        now: dt.datetime,
        extra: Mapping[str, Any] | None = None,
    ) -> list[PaperEvent]:
        """NO_FILL is final: no prices, zero money, the reason on the event."""
        reason = decision.reason or "expired"
        # a fill bar that decided it (a gap, the session end) stamps it with its open; a
        # window that ran out, with the data time that proves it
        decided_by_the_bar = reason in GAP_CLOSE_REASONS or reason == "session_end"
        ts = decision.bar_ts if decided_by_the_bar else decision.data_through
        detail: dict[str, Any] = {**(extra or {}), "reason": reason}
        if decision.bar_ts is not None:
            detail |= {
                "bar_ts": decision.bar_ts.isoformat(),
                "bar_open_paise": decision.bar_open_paise,
            }
        if decision.data_through is not None:
            detail["data_through"] = decision.data_through.isoformat()
        close_reason = GAP_CLOSE_REASONS.get(reason, "NO_FILL")
        values = _no_fill_values(close_reason, now)
        return self._commit(trade, values, [("NO_FILL", ts or now, detail)], "no_fill")

    def _entry_unpriced(
        self, trade: PaperTrade, decision: FillDecision, detail: dict[str, Any], now: dt.datetime
    ) -> list[PaperEvent]:
        """The fill bar cannot be priced: say so once per bar; NO_FILL once the window is over."""
        if decision.window_closed is not None:  # Manager decision 11: then NO_FILL as usual
            closed = replace(decision, outcome="no_fill", reason=decision.window_closed)
            unpriced = {"pricing": "unavailable", "error": detail.get("error")}
            return self._close_no_fill(trade, closed, now, extra=unpriced)
        assert decision.bar_ts is not None
        key = (trade.id, decision.bar_ts)
        with self._lock:
            if key in self._unpriced:
                return []
        # Guarded like every other write: if another book moved the trade first, the event
        # is not booked and this book adopts the stored row (review round 1 NIT).
        booked = [("PRICING_UNAVAILABLE", decision.bar_ts, detail | {"leg": "entry"})]
        events = self._commit(trade, {"updated_at": now}, booked, counter=None)
        if events:
            with self._lock:
                self._unpriced.add(key)
        return events

    def _premium(
        self, option: Mapping[str, Any], underlying_paise: int, ts: dt.datetime
    ) -> tuple[float | None, str | None]:
        """premium_at(option, underlying, ts), or (None, why): never a silent zero.

        A pricer that raises is logged at WARNING once per failure streak of that option
        (strike, CE/PE, expiry) and at DEBUG while the streak lasts; a call that returns
        ends the streak, so the next failure warns again (review round 1 NIT), like the
        PRICING_UNAVAILABLE event. The set is bounded by READ_WARN_CAP.
        """
        key = _option_key(option)
        try:
            value = self.premium_at(dict(option), underlying_paise / 100, ts)
        except Exception as exc:
            with self._lock:
                first = key not in self._pricer_failing
                if first and len(self._pricer_failing) >= READ_WARN_CAP:
                    self._pricer_failing.clear()  # bounded: a forgotten streak warns again
                self._pricer_failing.add(key)
            log.log(
                logging.WARNING if first else logging.DEBUG,
                "paper: premium_at failed for %s at %s: %s%s",
                option,
                ts,
                exc,
                "" if first else " (still failing)",
            )
            return None, f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._pricer_failing.discard(key)
        if value is None or isinstance(value, bool) or not isinstance(value, Real):
            return None, f"premium_at returned {value!r}"
        fair = float(value)
        if not math.isfinite(fair) or fair <= 0.0:
            return None, f"premium_at returned {value!r}"
        return fair, None

    def _tick(self, trade: PaperTrade) -> float:
        return self._tick_for(trade.instrument_key)

    def _tick_for(self, instrument_key: str) -> float:
        spec = self.specs.get(instrument_key)
        tick = float(spec.tick_size) if spec is not None and spec.tick_size else 0.0
        return tick if math.isfinite(tick) and tick > 0 else DEFAULT_TICK

    def _commit(
        self,
        trade: PaperTrade,
        values: Mapping[str, Any],
        booked: list[Booked],
        counter: str | None,
    ) -> list[PaperEvent]:
        """One transaction: the guarded change plus its events. Memory after commit.

        The guard is the status AND grade this book last saw, so a trade another book on
        the same database moved first is adopted, never booked twice. Managing an OPEN trade
        keeps its status, so for one the guard is also the management cursor (last_bar_ts):
        a book with a stale copy can neither re-book a STOP_MOVED nor move the cursor back
        (review round 1, Tester BUG-2).
        """
        events = [PaperEvent(trade.id, ts, kind, detail) for kind, ts, detail in booked]
        guard = [
            paper_trades.c.id == trade.id,
            paper_trades.c.status == trade.status,
            paper_trades.c.grade == trade.grade,
        ]
        if trade.status == "OPEN":
            cursor = paper_trades.c.last_bar_ts
            seen = trade.last_bar_ts
            guard.append(cursor.is_(None) if seen is None else cursor == seen)
        with self.engine.begin() as conn:
            changed = conn.execute(paper_trades.update().where(*guard).values(**values)).rowcount
            if changed == 1:
                for event in events:
                    conn.execute(paper_events.insert().values(**_event_row(event)))
                row = (
                    conn.execute(select(paper_trades).where(paper_trades.c.id == trade.id))
                    .mappings()
                    .one()
                )
        if changed != 1:  # another book on this database moved it first: adopt its row
            log.info("paper: trade %s changed elsewhere; reloading it", trade.id)
            self._refresh(trade.alert_id)
            return []
        stored = PaperTrade.from_row(row)
        with self._lock:
            if counter is not None:
                self._counts[counter] += 1
            self._set_active(stored)
        return events

    def _refresh(self, alert_id: str) -> None:
        current = self._fetch(alert_id)
        with self._lock:
            if current is None:
                self._active.pop(alert_id, None)
            else:
                self._set_active(current)

    def _set_active(self, trade: PaperTrade) -> None:
        """Hold `trade` if still managed, else forget it. Call with `self._lock` held."""
        if trade.is_active:
            self._active[trade.alert_id] = trade
        else:
            self._active.pop(trade.alert_id, None)
            self._unpriced = {key for key in self._unpriced if key[0] != trade.id}


# ── pure helpers ──────────────────────────────────────────────────────────────


class _SeriesViews:
    """One series' frame, read ONCE per step and normalised at most once per use: the fill
    view (opens) for waiting trades, the OHLC view for open ones (AC8). A frame that one use
    cannot read is logged once: no fill view, or an OHLC view with no bars."""

    def __init__(self, series_key: str, frame: pd.DataFrame) -> None:
        self.series_key = series_key
        self.frame = frame
        self._fill: Bars | None = None
        self._ohlc: OhlcBars | None = None
        self._tried: set[str] = set()

    @classmethod
    def empty(cls, series_key: str) -> _SeriesViews:
        """No frame this step: no fill view, and an OHLC view with no bars."""
        return cls(series_key, pd.DataFrame(columns=["open", "high", "low", "close"]))

    @classmethod
    def read(cls, bars: BarsFor, series_key: str) -> _SeriesViews | None:
        """bars(series_key), or None (logged) when the callable fails or gives no rows."""
        try:
            frame = bars(series_key)
        except Exception as exc:
            log.warning(
                "paper: no usable bars for %s this step: %s: %s",
                series_key,
                type(exc).__name__,
                exc,
            )
            return None
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        return cls(series_key, frame)

    @property
    def fill(self) -> Bars | None:
        if "fill" not in self._tried:
            self._tried.add("fill")
            try:
                view = normalise_bars(self.frame)
            except Exception as exc:
                self._unusable("fill", exc)
            else:
                self._fill = view if view is not None and len(view) else None
        return self._fill

    @property
    def ohlc(self) -> OhlcBars:
        """The OHLC view, possibly with no bars: an empty frame, or one that cannot be read
        (logged once), manages nothing this step, yet an OPEN trade on it can still reach its
        session-over close on the last close it saw (no overnight carry, AC5)."""
        if self._ohlc is None:
            self._ohlc = _NO_BARS
            if not self.frame.empty:
                try:
                    self._ohlc = normalise_ohlc(self.frame)
                except Exception as exc:
                    self._unusable("ohlc", exc)
        return self._ohlc

    def _unusable(self, use: str, exc: Exception) -> None:
        log.warning(
            "paper: bars for %s unusable for the %s this step: %s: %s",
            self.series_key,
            use,
            type(exc).__name__,
            exc,
        )


_NO_BARS = OhlcBars(
    ns=np.empty(0, dtype=np.int64),
    open=np.empty(0),
    high=np.empty(0),
    low=np.empty(0),
    close=np.empty(0),
    usable=np.empty(0, dtype=bool),
)


def _option_key(option: Mapping[str, Any]) -> tuple[str, str, str]:
    """The contract an option dict names (strike, CE/PE, expiry), as text: a log key only."""
    return str(option.get("strike")), str(option.get("opt_type")), str(option.get("expiry"))


def _basis_paise(plan: Mapping[str, Any]) -> int:
    """The live-chain basis frozen at the alert, in paise; 0 for a synthetic alert, for a row
    opened before it was recorded, and when the model could not price the alert (None): the
    trade is then priced on the model alone, as a synthetic one is."""
    pricing = plan.get("pricing")
    basis = pricing.get("basis_paise") if isinstance(pricing, Mapping) else None
    if isinstance(basis, bool) or not isinstance(basis, int):
        return 0
    return basis


def _gross(trade: PaperTrade, exit_paise: int | None) -> int | None:
    """sign x (exit - entry) x qty. An option is LONG PREMIUM whatever the direction on the
    underlying (a BUY_PE gains as the underlying falls); None without an exit price."""
    if exit_paise is None or trade.entry_paise is None:
        return None
    sign = 1 if trade.kind == "INDEX" or trade.direction == "long" else -1
    return sign * (exit_paise - trade.entry_paise) * trade.qty


# The cells the costs backfill reads: plain integers and strings, so no column decoder (JSON,
# times) runs and one corrupt plan or time elsewhere in a row cannot stop it.
_BACKFILL_COLUMNS = (
    "id",
    "alert_id",
    "instrument_key",
    "kind",
    "direction",
    "qty",
    "entry_paise",
    "exit_paise",
    "gross_paise",
)


class _Uncosted(NamedTuple):
    """A CLOSED row with a priced exit and no costs yet, its cells checked."""

    trade_id: int
    alert_id: str
    instrument_key: str
    kind: str
    direction: str
    qty: int
    entry_paise: int
    exit_paise: int
    gross_paise: int


def _uncosted(row: Mapping[Any, Any]) -> _Uncosted | None:
    """The checked candidate, or None (warned) when a cell would make its net a guess: money
    or a qty that is not a whole number, a kind or direction with no cost sides, a qty below
    one, or a gross that is not sign x (exit - entry) x qty."""
    trade_id = row["id"]
    whole: dict[str, int] = {}
    for column in ("qty", "entry_paise", "exit_paise", "gross_paise"):
        try:
            value = stored_paise(row[column])
        except ValueError:
            value = None
        if value is None:
            log.warning(
                "paper: paper_trades.id=%s not backfilled: %s=%r is not a whole number; "
                "left as it is, never truncated",
                trade_id,
                column,
                row[column],
            )
            return None
        whole[column] = value
    kind, direction = str(row["kind"]), str(row["direction"])
    try:
        entry_side, _ = leg_sides(kind, direction)
    except ValueError as exc:
        log.warning("paper: paper_trades.id=%s not backfilled: %s", trade_id, exc)
        return None
    qty, entry, exit_, gross = (
        whole[c] for c in ("qty", "entry_paise", "exit_paise", "gross_paise")
    )
    sign = 1 if entry_side == "buy" else -1
    if qty < 1 or gross != sign * (exit_ - entry) * qty:
        log.warning(
            "paper: paper_trades.id=%s not backfilled: gross_paise=%s is not %+d x (%s - %s) x "
            "%s; a net from it would be a guess",
            trade_id,
            gross,
            sign,
            exit_,
            entry,
            qty,
        )
        return None
    return _Uncosted(
        int(trade_id),
        str(row["alert_id"]),
        str(row["instrument_key"]),
        kind,
        direction,
        qty,
        entry,
        exit_,
        gross,
    )


def closed_totals(trades: Iterable[tuple[object, object]]) -> DayTotals:
    """Count and sum closed trades given as (gross_paise, net_paise) pairs.

    A trade is a win or a loss on its net when it has one, else on its gross (before
    US-023 there are no costs, so no net); zero is neither, and a trade with neither has
    no outcome. A sum is None as soon as one trade lacks that amount: a partial sum would
    look like a real total. With no trades the gross is 0 but the net stays None: no net
    has been computed, and a "0.00" net would read as costs already applied (US-030
    contract: net null until US-023).
    """
    closed = wins = losses = 0
    gross: int | None = 0
    net: int | None = 0
    for raw_gross, raw_net in trades:
        trade_gross, trade_net = _int_or_none(raw_gross), _int_or_none(raw_net)
        closed += 1
        gross = None if gross is None or trade_gross is None else gross + trade_gross
        net = None if net is None or trade_net is None else net + trade_net
        outcome = trade_net if trade_net is not None else trade_gross
        if outcome is not None and outcome > 0:
            wins += 1
        elif outcome is not None and outcome < 0:
            losses += 1
    return {
        "closed": closed,
        "wins": wins,
        "losses": losses,
        "gross_paise": gross,
        "net_paise": net if closed else None,
    }


def reconcile_since(now: dt.datetime, days: int) -> dt.datetime:
    """Midnight IST `days` calendar days before `now`'s IST date: the reconciliation horizon."""
    day = now.astimezone(IST).date() - dt.timedelta(days=max(int(days), 0))
    return dt.datetime.combine(day, dt.time(0), tzinfo=IST)


def _no_fill_values(close_reason: str, now: dt.datetime) -> dict[str, Any]:
    """A NO_FILL row: final, no prices, zero money."""
    return {
        "status": "NO_FILL",
        "close_reason": close_reason,
        "entry_paise": None,
        "exit_paise": None,
        "gross_paise": 0,
        "costs_paise": 0,
        "net_paise": 0,
        "updated_at": now,
    }


_DATABASE_FAILURE = "_paper_database_failure"  # set on exceptions raised by database I/O


@contextlib.contextmanager
def _database_io() -> Iterator[None]:
    """Mark whatever is raised while the book reads or writes the database as a database
    failure, whatever its type: the cursor, the driver or the disk failed, not the alert."""
    try:
        yield
    except Exception as exc:
        with contextlib.suppress(AttributeError, TypeError):
            setattr(exc, _DATABASE_FAILURE, True)
        raise


def is_database_failure(exc: BaseException) -> bool:
    """A failure a later retry can fix: a DBAPI error, an OS error (a full or vanished
    disk), or anything raised during the book's database I/O. Everything else is a bug."""
    return isinstance(exc, (DBAPIError, OSError)) or bool(getattr(exc, _DATABASE_FAILURE, False))


def _brief(exc: BaseException) -> str:
    """One line: the driver's message for a DBAPI error, else the exception's first line."""
    text = str(getattr(exc, "orig", None) or exc) or type(exc).__name__
    return text.splitlines()[0]


class _StepStopped(Exception):
    """The database is locked: the step ends here (logged and counted once)."""


def database_locked(exc: BaseException) -> bool:
    """SQLite's "database is locked" / "database table is locked": another writer holds it."""
    return isinstance(exc, OperationalError) and "is locked" in str(exc.orig).lower()


def _event_row(event: PaperEvent) -> dict[str, Any]:
    return {"trade_id": event.trade_id, "ts": event.ts, "kind": event.kind, "detail": event.detail}


def _require_aware(ts: dt.datetime, name: str) -> None:
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")


def _ist(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # TZDateTime re-attaches UTC; this is belt and braces
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(IST)


def _ist_or_none(value: dt.datetime | None) -> dt.datetime | None:
    return None if value is None else _ist(value)


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


_MONEY_WARNED: set[tuple[object, str]] = set()  # (trade id, column) already warned about
_MONEY_WARNED_LOCK = threading.Lock()


def _paise_or_none(row: Mapping[Any, Any], column: str) -> int | None:
    """A money column read back as integer paise, or None when it is not a whole number.

    A non-integer (a REAL 1030.5, text, NaN, a bool) is UNKNOWN, never truncated to a wrong
    amount (US-023 carry-over); it is warned about once per trade and column. A whole float
    within 2**53 is exact and accepted (`money.stored_paise`, shared with the ledger).
    """
    value = row[column]
    try:
        return stored_paise(value)
    except ValueError:
        pass
    key = (row["id"], column)
    with _MONEY_WARNED_LOCK:
        first = key not in _MONEY_WARNED
        _MONEY_WARNED.add(key)
    if first:
        log.warning(
            "paper_trades.id=%s: %s=%r is not a whole number of paise; read as unknown, "
            "never truncated",
            row["id"],
            column,
            value,
        )
    return None


def _is_paise(value: object) -> bool:
    """A stored amount: None or a plain int. A float or a string is corrupt, never truncated."""
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _decoded(name: str, value: object, decode: Callable[[Any], Any] | None) -> Any:
    """One raw cell of a `recent()` row, decoded as its column type would (ValueError if bad)."""
    value = decode(value) if decode is not None else value
    if name.endswith("_paise") and not _is_paise(value):
        raise ValueError(f"{name}={value!r} is not integer paise")
    return value
