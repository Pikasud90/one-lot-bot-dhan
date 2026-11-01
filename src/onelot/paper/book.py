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

# No pandas-stubs are pinned; every pandas module in the mypy baseline carries this one
# error, so a NEW module stays at zero only by naming it here (the flags are untouched).
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine, Text, and_, inspect, or_, select, type_coerce
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

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
)
from .money import to_paise
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
RECENT_LIMIT = 200  # recent()'s default: the trades the desk's Paper panel is sent

PremiumAt = Callable[[Mapping[str, Any], float, dt.datetime], float | None]
CostFor = Callable[[str], CostModel]
BarsFor = Callable[[str], pd.DataFrame | None]
# US-002 narrows the return type to `onelot.paper.manage.StrategyExit | None`.
StrategyExitFor = Callable[["PaperTrade", dt.datetime], Any]


Booked = tuple[str, dt.datetime, dict[str, Any]]  # (event kind, data time, detail)
OpenedBy = Literal["on_alert", "reconcile"]  # OPENED detail.opened_by


class _Priced(NamedTuple):
    """A priced fill: entry and risk in paise, the inputs used, any extra events to book."""

    entry_paise: int
    risk_paise: int | None
    inputs: dict[str, Any]
    also: tuple[Booked, ...] = ()


@dataclass(frozen=True)
class PaperEvent:
    """One booked event of a paper trade (a `paper_events` row)."""

    trade_id: int
    ts: dt.datetime  # bar or decision time (data time), never wall time
    kind: str  # OPENED | FILLED | NO_FILL (US-002 adds CLOSED and friends)
    detail: Mapping[str, Any]


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
            entry_paise=_int_or_none(row["entry_paise"]),
            exit_paise=_int_or_none(row["exit_paise"]),
            gross_paise=_int_or_none(row["gross_paise"]),
            costs_paise=_int_or_none(row["costs_paise"]),
            net_paise=_int_or_none(row["net_paise"]),
            risk_paise=_int_or_none(row["risk_paise"]),
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
    ) -> None:
        self.engine = engine
        self.cfg = cfg
        self.bt = bt
        self.specs = specs
        self.cost_for = cost_for
        self.premium_at = premium_at
        self.clock = clock
        self._lock = threading.RLock()
        self._active: dict[str, PaperTrade] = {}  # non-closed trades, by alert id
        self._counts: dict[str, int] = dict.fromkeys(STATUS_KEYS, 0)
        self._unpriced: set[tuple[int, dt.datetime]] = set()  # PRICING_UNAVAILABLE said once
        # on_alert calls that failed at runtime, retried by step (review 1, MINOR-3)
        self._retry: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._retry_logged: set[str] = set()  # retries whose traceback was already logged
        self._tracked: frozenset[str] | None = None  # the desk's series (retire_untracked)
        METADATA.create_all(engine, tables=[paper_trades, paper_events])  # no-op if present
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

    def step(
        self,
        now: dt.datetime,
        bars: BarsFor,
        strategy_exit: StrategyExitFor | None = None,
    ) -> list[PaperEvent]:
        """Fill PENDING_FILL trades on closed bars (AC2/AC3); US-002 manages OPEN ones.

        `bars(series_key)` is called once per series that has a waiting trade. Each status
        change commits with its paper_events rows in one transaction. A failure on one
        trade is logged and counted in `hook_errors`; it never stops the other trades and
        never escapes. The exception is a locked database: every further write would wait
        a whole busy timeout (30 s) while the Desk lock is held, so the step stops at the
        first one, counts and logs it once, and the next poll retries (BUG-1, review 1).
        A naive `now` is rejected (ValueError), never guessed.

        Order: failed on_alert calls are retried, waiting trades on series the desk no
        longer tracks are retired (`retire_untracked`), then the rest are filled.
        """
        _require_aware(now, "now")
        now = now.astimezone(IST)
        events: list[PaperEvent] = []
        try:
            if not self._retry_failed_alerts(now):
                return []  # the database is locked: nothing else can be written this poll
            events.extend(self._retire_overdue_untracked(now))
            with self._lock:
                waiting = sorted(
                    (t for t in self._active.values() if t.status == "PENDING_FILL"),
                    key=lambda t: (t.created_at, t.id),
                )
            by_series: dict[str, list[PaperTrade]] = {}
            for trade in waiting:
                by_series.setdefault(trade.series_key, []).append(trade)
            for series_key, trades in by_series.items():
                view = _read_bars(bars, series_key)
                if view is None:
                    continue
                for trade in trades:
                    events.extend(
                        self._guarded(trade, partial(self._step_waiting, trade, view, now))
                    )
        except _StepStopped:
            pass  # logged and counted once; every untouched trade is retried next poll
        return events

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
                        return not database_locked(exc)
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
        """One trade's work. A failure is logged, counted and skipped; a LOCKED database
        stops the whole step (BUG-1): logged once without a traceback, then _StepStopped.

        Each write commits on its own, so a unit that commits one write and then fails (a
        REGRADED event, then a locked fill) leaves that committed event in the database
        but out of step()'s return value: paper_events, not the returned list, is the record.
        """
        try:
            return work()
        except OperationalError as exc:
            if not database_locked(exc):
                self._step_failed(trade)
                return []
            log.error(
                "paper: database is locked at trade %s (alert %s); step stopped, the rest "
                "are retried on the next poll: %s",
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
        """The latest `limit` trades, newest first (created_at, then id), as display dicts.

        Each is `onelot.paper.view.trade_view`: money as rupee strings from paise, times
        ISO-8601 IST, None for what is not known yet. A row that cannot be read (an unknown
        status, a corrupt plan) is logged and left out, never fatal to the listing.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit={limit!r} must be a positive int")
        plan_text = type_coerce(paper_trades.c.plan, Text).label("plan")  # parsed per row
        columns = [c for c in paper_trades.c if c.name != "plan"] + [plan_text]
        stmt = (
            select(*columns)
            .order_by(paper_trades.c.created_at.desc(), paper_trades.c.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        views: list[dict[str, Any]] = []
        for row in rows:
            try:
                stored = {**row, "plan": json.loads(row["plan"])}
                views.append(trade_view(PaperTrade.from_row(stored)))
            except Exception as exc:  # noqa: BLE001 - one bad row never hides the others
                log.warning(
                    "paper: trade %s (alert %s) not shown: %s: %s",
                    row["id"],
                    row["alert_id"],
                    type(exc).__name__,
                    exc,
                )
        return views

    def today_totals(self, now: dt.datetime) -> DayTotals:
        """The CLOSED trades closed on `now`'s IST calendar date (`closed_totals`).

        A trade is closed at its exit_ts, or at updated_at when it has none. NO_FILL trades
        never filled, so they are not counted. A naive `now` is rejected (ValueError).
        """
        _require_aware(now, "now")
        start = dt.datetime.combine(now.astimezone(IST).date(), dt.time(0), tzinfo=IST)
        end = start + dt.timedelta(days=1)
        t = paper_trades.c
        closed_today = or_(
            and_(t.exit_ts >= start, t.exit_ts < end),
            and_(t.exit_ts.is_(None), t.updated_at >= start, t.updated_at < end),
        )
        stmt = select(t.gross_paise, t.net_paise).where(t.status == "CLOSED", closed_today)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return closed_totals((gross, net) for gross, net in rows)

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

    def _freeze_option_pricing(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Freeze how the alerted option is priced, once, at the alert. Mutates `plan`, the
        book's own deep copy, and returns the plan's "pricing" record.

        IV: the alert's own, else implied from its chain premium (economics.entry_price) at
        the underlying entry and created_at (Manager decision h); if that fails the IV stays
        missing and every repricing is PRICING_UNAVAILABLE, never a constant IV.
        Basis: for a live-chain alert, chain premium minus the model premium at the alert,
        in paise. Only RECORDED here (ADR-001 amendment 6); US-023 applies it.
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

    # ── persistence ───────────────────────────────────────────────────────
    def _load_active(self) -> None:
        stmt = select(paper_trades).where(paper_trades.c.status.in_(ACTIVE_STATUSES))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        with self._lock:
            for row in rows:
                trade = PaperTrade.from_row(row)
                self._active[trade.alert_id] = trade
        if rows:
            log.info("paper: reloaded %d active paper trade(s)", len(rows))

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
        stop_paise = int(plan["levels"]["underlying_stop_paise"])
        decision = decide_fill(
            view,
            created_at=trade.created_at,
            deadline=self._deadline(trade),
            resolution_min=resolution,
            now=now,
            direction=trade.direction,
            stop_paise=stop_paise,
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
        values = {
            "status": "OPEN",
            "entry_paise": priced.entry_paise,
            "entry_ts": bar_ts,
            "fill_bar_ts": bar_ts,
            "last_bar_ts": bar_ts,
            "resolution_min": resolution,
            "risk_paise": priced.risk_paise,
            "updated_at": now,
        }
        detail |= priced.inputs | {
            "entry_paise": priced.entry_paise,
            "risk_paise": priced.risk_paise,
        }
        return self._commit(trade, values, [("FILLED", bar_ts, detail), *priced.also], "filled")

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

        risk = (entry premium - the stop EXIT fill the Lab would book) x qty, the stop leg
        priced at the underlying stop with the option frozen at the alert (Manager decision
        (a): paper R and Lab R are comparable). If that repricing fails, or the stop exit is
        not below the entry, the trade still fills with risk_paise NULL and a
        PRICING_UNAVAILABLE event (ADR-001 amendment 6).
        """
        option = plan["alert"]["economics"].get("option") or {}
        fair, why = self._premium(option, open_paise, bar_ts)
        if fair is None:
            return why or "no premium"
        bt = self.bt
        tick = self._tick(trade)
        entry = option_entry_paise(fair, bt.option_spread_pct, bt.option_slippage_ticks, tick)
        inputs = {
            "fair_premium_paise": to_paise(fair),
            "option_spread_pct": bt.option_spread_pct,
            "option_slippage_ticks": bt.option_slippage_ticks,
        }
        stop_fair, why = self._premium(option, stop_paise, bar_ts)
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
        ts = decision.bar_ts if reason == "gap_through_stop" else decision.data_through
        detail: dict[str, Any] = {**(extra or {}), "reason": reason}
        if decision.bar_ts is not None:
            detail |= {
                "bar_ts": decision.bar_ts.isoformat(),
                "bar_open_paise": decision.bar_open_paise,
            }
        if decision.data_through is not None:
            detail["data_through"] = decision.data_through.isoformat()
        close_reason = "GAP_THROUGH_STOP" if reason == "gap_through_stop" else "NO_FILL"
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
        """premium_at(option, underlying, ts), or (None, why): never a silent zero."""
        try:
            value = self.premium_at(dict(option), underlying_paise / 100, ts)
        except Exception as exc:
            log.warning("paper: premium_at failed for %s at %s: %s", option, ts, exc)
            return None, f"{type(exc).__name__}: {exc}"
        if value is None or isinstance(value, bool) or not isinstance(value, Real):
            return None, f"premium_at returned {value!r}"
        fair = float(value)
        if not math.isfinite(fair) or fair <= 0.0:
            return None, f"premium_at returned {value!r}"
        return fair, None

    def _tick(self, trade: PaperTrade) -> float:
        spec = self.specs.get(trade.instrument_key)
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
        the same database moved first is adopted, never booked twice.
        """
        events = [PaperEvent(trade.id, ts, kind, detail) for kind, ts, detail in booked]
        with self.engine.begin() as conn:
            changed = conn.execute(
                paper_trades.update()
                .where(
                    paper_trades.c.id == trade.id,
                    paper_trades.c.status == trade.status,
                    paper_trades.c.grade == trade.grade,
                )
                .values(**values)
            ).rowcount
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


def _read_bars(bars: BarsFor, series_key: str) -> Bars | None:
    """bars(series_key), normalised; None (logged) when the callable or the frame is unusable."""
    try:
        view = normalise_bars(bars(series_key))
    except Exception as exc:
        log.warning(
            "paper: no usable bars for %s this step: %s: %s", series_key, type(exc).__name__, exc
        )
        return None
    return view if view is not None and len(view) else None


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
