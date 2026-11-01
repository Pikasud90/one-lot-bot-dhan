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

import datetime as dt
import json
import logging
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from numbers import Integral, Real
from typing import Any, Literal, cast

import numpy as np

# No pandas-stubs are pinned; every pandas module in the mypy baseline carries this one
# error, so a NEW module stays at zero only by naming it here (the flags are untouched).
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from ..clock import IST, now_ist
from ..config import PRODUCT_FOR_CATEGORY, BacktestCfg, PaperCfg
from ..costs import CostModel
from ..data.universe import InstrumentSpec
from ..store.schema import METADATA, paper_events, paper_trades
from .money import to_paise

log = logging.getLogger(__name__)

TradeStatus = Literal["PENDING_FILL", "OPEN", "NO_FILL", "CLOSED"]
Grade = Literal["live", "delayed"]
TRADE_STATUSES: frozenset[str] = frozenset(("PENDING_FILL", "OPEN", "NO_FILL", "CLOSED"))
ACTIVE_STATUSES: tuple[TradeStatus, ...] = ("PENDING_FILL", "OPEN")
STATUS_KEYS = ("opened", "filled", "no_fill", "closed", "hook_errors")
# Fallback cost table per alert kind when the instrument spec is unknown (Lab naming).
PRODUCT_FOR_KIND = {
    "STOCK": PRODUCT_FOR_CATEGORY["STOCK"],
    "INDEX": PRODUCT_FOR_CATEGORY["OPTIONS"],
}
DIRECTIONS = ("long", "short")

PremiumAt = Callable[[Mapping[str, Any], float, dt.datetime], float | None]
CostFor = Callable[[str], CostModel]
BarsFor = Callable[[str], pd.DataFrame | None]
# US-002 narrows the return type to `onelot.paper.manage.StrategyExit | None`.
StrategyExitFor = Callable[["PaperTrade", dt.datetime], Any]


class PaperAlertError(ValueError):
    """An alert the paper book cannot record: a missing field, a naive time, a bad level."""


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
        METADATA.create_all(engine, tables=[paper_trades, paper_events])  # no-op if present
        self._load_active()

    # ── public API ────────────────────────────────────────────────────────
    def on_alert(self, alert: Mapping[str, Any], now: dt.datetime) -> PaperTrade | None:
        """Open (or return the already-open) paper trade for `alert`.

        Returns None and writes nothing when `paper.enabled` is false. Raises
        PaperAlertError for an alert it cannot record (the desk hook logs and counts it;
        the alert itself is never blocked by the paper book).
        """
        _require_aware(now, "now")
        if not self.cfg.enabled:
            return None
        alert_id = _alert_id(alert)
        with self._lock:
            known = self._active.get(alert_id) or self._fetch(alert_id)
            if known is not None:
                return self._remember(known)
            values = self._new_trade_values(alert_id, alert, now)
            try:
                trade = self._insert_opened(values)
            except IntegrityError:
                # Another PaperBook on this database won the race for the same alert id.
                raced = self._fetch(alert_id)
                if raced is None:
                    raise
                return self._remember(raced)
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
        return trade

    def step(
        self,
        now: dt.datetime,
        bars: BarsFor,
        strategy_exit: StrategyExitFor | None = None,
    ) -> list[PaperEvent]:
        """Fill PENDING_FILL trades on closed bars (AC2/AC3); US-002 manages OPEN ones."""
        raise NotImplementedError("PaperBook.step: the fill rule is US-001 AC2/AC3 (turn T2)")

    def open_trades(self) -> list[PaperTrade]:
        """A copy of the trades still managed (PENDING_FILL or OPEN), oldest first."""
        with self._lock:
            trades = list(self._active.values())
        return sorted(trades, key=lambda t: (t.created_at, t.id))

    def status(self) -> dict[str, int]:
        """Counters since this book was built: opened, filled, no_fill, closed, hook_errors."""
        with self._lock:
            return dict(self._counts)

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

    def _insert_opened(self, values: Mapping[str, Any]) -> PaperTrade:
        """Insert the trade and its OPENED event in ONE transaction; return the stored row."""
        detail = {
            "alert_status": values["plan"]["alert"].get("status"),
            "risk_allowed": values["plan"]["alert"].get("risk_allowed"),
            "grade": values["grade"],
            "data_source": values["data_source"],
            "resolution_min": values["resolution_min"],
            "qty": values["qty"],
            "product": values["product"],
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

    # ── alert -> row ──────────────────────────────────────────────────────
    def _new_trade_values(
        self, alert_id: str, alert: Mapping[str, Any], now: dt.datetime
    ) -> dict[str, Any]:
        """Validate `alert` and build the PENDING_FILL row. Pure: no I/O."""
        snapshot = _jsonable(alert)  # a deep, JSON-safe copy: later desk mutations can't leak
        kind = _req_str(snapshot, "kind", alert_id)
        if kind not in PRODUCT_FOR_KIND:
            raise PaperAlertError(f"alert {alert_id}: kind={kind!r} is not STOCK or INDEX")
        direction = _req_str(snapshot, "direction", alert_id)
        if direction not in DIRECTIONS:
            raise PaperAlertError(f"alert {alert_id}: direction={direction!r} is not long/short")
        created_at = _aware_ts(snapshot.get("created_at"), "created_at", alert_id)
        expires_at = _aware_ts(snapshot.get("expires_at"), "expires_at", alert_id)
        if expires_at < created_at:
            raise PaperAlertError(f"alert {alert_id}: expires_at is before created_at")
        instrument_key = _req_str(snapshot, "instrument_key", alert_id)
        eco = snapshot.get("economics")
        if not isinstance(eco, dict):
            raise PaperAlertError(f"alert {alert_id}: economics missing")
        data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
        source = str(data.get("source") or "")
        spec = self.specs.get(instrument_key)
        # The cost table cost_for(instrument_key) charges from; by kind if the spec is unknown.
        product = spec.product if spec is not None and spec.product else PRODUCT_FOR_KIND[kind]
        return {
            "alert_id": alert_id,
            "status": "PENDING_FILL",
            "close_reason": None,
            "grade": grade_for(source),
            "data_source": source,
            "instrument_key": instrument_key,
            "series_key": _req_str(snapshot, "series_key", alert_id),
            "kind": kind,
            "side": _req_str(snapshot, "side", alert_id),
            "direction": direction,
            "strategy": _req_str(snapshot, "strategy", alert_id),
            "timeframe": _req_str(snapshot, "timeframe", alert_id),
            "confidence": _confidence_score(snapshot.get("confidence")),
            "product": product,
            "qty": _unit_qty(kind, eco, spec, alert_id),
            "resolution_min": _resolution_min(data.get("resolution")),
            "created_at": created_at,
            "expires_at": expires_at,
            "plan": {"alert": snapshot, "levels": _levels(eco, alert_id)},
            "updated_at": now,
        }


# ── pure helpers ──────────────────────────────────────────────────────────────


def grade_for(source: str | None) -> Grade:
    """`live` iff the bars came from Dhan (`dhan`, `dhan-ws`, ...); anything else is delayed."""
    return "live" if (source or "").strip().lower().startswith("dhan") else "delayed"


def _require_aware(ts: dt.datetime, name: str) -> None:
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")


def _alert_id(alert: Mapping[str, Any]) -> str:
    raw = alert.get("id") if isinstance(alert, Mapping) else None
    if raw is None or not str(raw).strip():
        raise PaperAlertError("alert has no id")
    return str(raw).strip()


def _req_str(alert: Mapping[str, Any], key: str, alert_id: str) -> str:
    value = alert.get(key)
    if value is None or not str(value).strip():
        raise PaperAlertError(f"alert {alert_id}: {key} missing")
    return str(value)


def _aware_ts(value: object, name: str, alert_id: str) -> dt.datetime:
    """An ISO-8601 string (or datetime) with an offset -> IST. Naive input is rejected."""
    if isinstance(value, dt.datetime):
        ts = value
    elif isinstance(value, str):
        try:
            ts = dt.datetime.fromisoformat(value)
        except ValueError as exc:
            raise PaperAlertError(f"alert {alert_id}: {name}={value!r} is not ISO-8601") from exc
    else:
        raise PaperAlertError(f"alert {alert_id}: {name} missing")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise PaperAlertError(
            f"alert {alert_id}: {name}={value!r} has no timezone; refusing to guess"
        )
    return ts.astimezone(IST)


def _confidence_score(conf: object) -> float | None:
    score = conf.get("score") if isinstance(conf, Mapping) else conf
    if isinstance(score, Real) and not isinstance(score, bool) and math.isfinite(float(score)):
        return float(score)
    return None


def _resolution_min(value: object) -> int | None:
    """'1m' -> 1, '2m' -> 2; anything unreadable -> None (step infers it from the bars)."""
    text = str(value or "").strip().lower()
    if text.endswith("m") and text[:-1].isdigit() and int(text[:-1]) > 0:
        return int(text[:-1])
    return None


def _unit_qty(kind: str, eco: Mapping[str, Any], spec: InstrumentSpec | None, alert_id: str) -> int:
    """One share for a stock; one lot of the alerted option for an index. Never sized."""
    if kind == "STOCK":
        return 1
    option = eco.get("option")
    lot = option.get("lot") if isinstance(option, Mapping) else None
    if lot is None and spec is not None:
        lot = spec.lot_size
    size = float(lot) if isinstance(lot, Real) and not isinstance(lot, bool) else math.nan
    if size.is_integer() and size > 0:
        return int(size)
    raise PaperAlertError(f"alert {alert_id}: no usable option lot size ({lot!r})")


def _levels(eco: Mapping[str, Any], alert_id: str) -> dict[str, int | None]:
    """The alert's underlying levels converted to paise once (ADR-001 amendment 5)."""
    out: dict[str, int | None] = {}
    for name, required in (
        ("underlying_entry", True),
        ("underlying_stop", True),
        ("underlying_target", False),
    ):
        value = eco.get(name)
        if value is None and not required:
            out[f"{name}_paise"] = None
            continue
        try:
            out[f"{name}_paise"] = to_paise(value)
        except ValueError as exc:
            raise PaperAlertError(f"alert {alert_id}: economics.{name}: {exc}") from exc
    return out


def _jsonable(value: object) -> Any:
    """A deep, JSON-safe copy: numpy scalars -> Python, NaN/inf -> None, times -> ISO."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Real):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    return str(value)


def _ist(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # TZDateTime re-attaches UTC; this is belt and braces
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(IST)


def _ist_or_none(value: dt.datetime | None) -> dt.datetime | None:
    return None if value is None else _ist(value)


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(cast(int, value))
