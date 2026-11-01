"""Broker-agnostic data contracts and the read-only adapter protocol."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class BrokerError(RuntimeError):
    """Any broker-side failure."""


class RateLimitError(BrokerError):
    """RL001 / HTTP 429. The caller must back off via the shared RateLimiter."""


class TokenExpiredError(BrokerError):
    """Access token rejected (v1 defect E3: tokens expire and v1 never noticed).

    Distinguished from a generic error so the engine can raise a specific,
    actionable alert instead of a wall of 401s.
    """


class DataEntitlementError(BrokerError):
    """The account lacks the Data API subscription needed for this endpoint."""


@dataclass(frozen=True)
class Contract:
    security_id: str
    exchange_segment: str
    symbol: str
    underlying: str
    instrument_type: str  # OPTIDX | FUTIDX | INDEX
    expiry: str | None = None  # ISO date
    strike: float | None = None
    opt_type: str | None = None  # CE | PE
    lot_size: int | None = None
    tick_size: float = 0.05

    @property
    def key(self) -> str:
        if self.opt_type:
            return f"{self.underlying}_{self.expiry}_{self.strike:g}_{self.opt_type}"
        return f"{self.underlying}_{self.instrument_type}_{self.expiry or 'SPOT'}"


@dataclass
class ChainRow:
    """One strike/side of the option chain.

    Greeks are OPTIONAL. A broker that omits or staleness-corrupts them must
    not silently poison sizing (v1 defect A9) — the engine recomputes locally
    and cross-checks.
    """

    strike: float
    opt_type: str
    security_id: str | None = None
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: float | None = None
    oi: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    greek_source: str = "broker"

    def as_dict(self) -> dict:
        return {
            "strike": self.strike,
            "opt_type": self.opt_type,
            "security_id": self.security_id,
            "ltp": self.ltp,
            "bid": self.bid,
            "ask": self.ask,
            "bid_qty": self.bid_qty,
            "ask_qty": self.ask_qty,
            "volume": self.volume,
            "oi": self.oi,
            "iv": self.iv,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "greek_source": self.greek_source,
        }


@dataclass
class ChainSnapshot:
    ts: dt.datetime
    underlying: str
    expiry: str
    underlying_price: float
    rows: list[ChainRow] = field(default_factory=list)

    def age_seconds(self, now: dt.datetime) -> float:
        return (now - self.ts).total_seconds()

    def for_side(self, opt_type: str) -> list[ChainRow]:
        return [r for r in self.rows if r.opt_type == opt_type]

    def as_dicts(self) -> list[dict]:
        return [r.as_dict() for r in self.rows]


@dataclass
class QuoteSnapshot:
    security_id: str
    ts: dt.datetime
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: float | None = None
    oi: float | None = None
    source: str = "ws"  # ws | rest

    def age_seconds(self, now: dt.datetime) -> float:
        return (now - self.ts).total_seconds()


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    # None (not 0) when the series has no traded volume — i.e. an index.
    volume: float | None = None


TickCallback = Callable[[QuoteSnapshot], None]


@runtime_checkable
class BrokerAdapter(Protocol):
    """Read-only market data access.

    Deliberately absent, and never to be added: place_order, modify_order,
    cancel_order, place_slice_order, or any positions/funds write. If a future
    change needs one, it is not a change to this system.
    """

    name: str

    def preflight(self) -> dict: ...
    def instruments(self, underlying: str) -> list[Contract]: ...
    def expiry_list(self, underlying: str) -> list[str]: ...
    def option_chain(self, underlying: str, expiry: str) -> ChainSnapshot: ...
    def quote(self, contracts: list[Contract]) -> dict[str, QuoteSnapshot]: ...
    def historical_daily(self, contract: Contract, start: dt.date, end: dt.date) -> list[Bar]: ...
    def historical_intraday(
        self, contract: Contract, interval_min: int, start: dt.datetime, end: dt.datetime
    ) -> list[Bar]: ...
    def start_stream(self, contracts: list[Contract], on_tick: TickCallback) -> None: ...
    def update_subscription(self, contracts: list[Contract]) -> None: ...
    def stop_stream(self) -> None: ...
    def stream_alive(self) -> bool: ...


def nearest_monthly_expiry(expiries: list[str], today: dt.date) -> str | None:
    """Pick the nearest monthly expiry that has not passed.

    Bank Nifty weeklies were discontinued, and the exchange has moved the
    expiry weekday more than once. We therefore never compute expiry dates —
    we take the broker's own list and pick the last expiry within each calendar
    month (v1 defect D6: hardcoding either the weekday or the lot size is a
    live landmine).
    """
    if not expiries:
        return None
    parsed: list[dt.date] = []
    for e in expiries:
        try:
            parsed.append(dt.date.fromisoformat(e[:10]))
        except ValueError:
            continue
    future = sorted(d for d in parsed if d >= today)
    if not future:
        return None
    by_month: dict[tuple[int, int], dt.date] = {}
    for d in sorted(parsed):
        by_month[(d.year, d.month)] = max(by_month.get((d.year, d.month), d), d)
    monthlies = sorted(d for d in by_month.values() if d >= today)
    return (monthlies[0] if monthlies else future[0]).isoformat()
