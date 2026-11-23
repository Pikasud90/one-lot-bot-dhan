"""The traded price and the charges of a simulated trade ("market accurate").

Stock, one share: the one-minute bar's open with the Lab's `slippage_pct` against the trader
(as `lab/simulator.py` fills), a stop or a gap exit slipped the same way, a target filled at
its level; charges from the intraday equity cost table (`config/costs.yaml`), both legs, as
`lab/execution.py: execute_trade` applies them.

Index option, one lot: the contract the Lab chooses (`lab/execution.py`: `monthly_expiry`,
`strike_for_delta` at the model's IV from India VIX). The premium at a minute is the REAL
traded one when the expired-options store has that strike's bar (`lab/real_options.py`:
`RealPricer._find`, opened read-only, never fetched), or today's through the desk in mode
`live`; otherwise the Black-76 model premium. The Lab's friction is applied to either: the
half-spread `option_spread_pct` and `option_slippage_ticks` against the trader, rounded to
the tick (up on a buy, down on a sale); charges from the index options table.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..config import BacktestCfg
from ..costs import CostModel, Side, round_to_tick
from ..data.expired_options import OptionBarStore, Underlying
from ..data.universe import InstrumentSpec
from ..lab.execution import (
    OptionPricer,
    expiry_datetime,
    monthly_expiry,
    strike_for_delta,
    year_frac,
)
from ..lab.real_options import NeedBlock, RealPricer, interpolate

#: (option dict as the desk's alerts carry it, the index level, the instant) -> premium | None
LivePremium = Callable[[dict[str, Any], float, dt.datetime], float | None]

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ReadOnlyOptionStore(OptionBarStore):
    """The expired-options store opened read-only: the same `get`, no table creation, no
    write ahead log switch, and `put` refuses. The Simulator never fetches."""

    def __init__(self, path: str | Path) -> None:  # noqa: D107 - see the class text
        self.path = Path(path)
        self._lock = threading.Lock()
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._mem: dict[tuple, Any] = {}

    def put(self, block: Any) -> None:
        raise RuntimeError("the Simulator's option store is read-only")


def open_option_store(path: str | Path | None) -> ReadOnlyOptionStore | None:
    """The store at `path` read-only, or None when there is none (the model prices all)."""
    if path is None or not Path(path).is_file():
        return None
    try:
        return ReadOnlyOptionStore(path)
    except sqlite3.Error:
        return None


@dataclass(frozen=True)
class Contract:
    """The option a session's alert names, fixed from the alert to the exit."""

    strike: float
    opt: str  # CE | PE
    expiry: dt.date
    what: str  # "NIFTY 25000 CE 28 Oct"
    security_id: str | None = None  # mode live: the desk's chain row, for today's premiums

    def as_dict(self) -> dict[str, Any]:
        return {
            "strike": self.strike,
            "opt_type": self.opt,
            "expiry": self.expiry.isoformat(),
            "what": self.what,
            "security_id": self.security_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Contract:
        return cls(
            float(d["strike"]),
            str(d["opt_type"]),
            dt.date.fromisoformat(str(d["expiry"])),
            str(d["what"]),
            d.get("security_id"),
        )


class StoredPremiums:
    """Real one-minute option bars of a fixed strike from the expired-options store, through
    the Lab's own lookup (`RealPricer._find`, no waiting: the bar of that minute or none)."""

    def __init__(
        self,
        store: OptionBarStore,
        und: Underlying,
        spec: InstrumentSpec,
        bt: BacktestCfg,
        costs: CostModel,
        und_1m: pd.DataFrame,
    ) -> None:
        self.bt = bt
        self.rp = RealPricer(und, spec, bt, costs, store, und_1m, 1, source=None, max_wait_min=0)

    def _code(self, day: dt.date, epoch: int) -> int:
        """The near (1) or next (2) monthly contract, by the Lab's roll rule."""
        try:
            left = self.rp._near_days_left(day, epoch)
        except NeedBlock:
            return 1
        if left is None:
            return 1
        return 2 if left < float(self.bt.option_min_days_to_expiry) else 1

    def bar(self, c: Contract, minute: pd.Timestamp) -> np.ndarray | None:
        """(open, high, low, close, iv, spot) of the contract's bar in the minute starting at
        `minute`, or None."""
        epoch = int(minute.value // 1_000_000_000)
        day = minute.date()
        try:
            hit = self.rp._find(self._code(day, epoch), c.opt, c.strike, epoch, day, 0)
        except NeedBlock:
            return None
        if isinstance(hit, str):
            return None
        return hit[1]


@dataclass(frozen=True)
class Quote:
    price: float  # a raw premium (before the spread) or a share price
    source: str  # real | model


class Pricer:
    """Fills, marks and charges of one session's instrument."""

    def __init__(
        self,
        spec: InstrumentSpec,
        kind: str,  # STOCK | INDEX
        bt: BacktestCfg,
        costs: CostModel,
        vix: pd.Series | None = None,
        stored: StoredPremiums | None = None,
        live: LivePremium | None = None,
        underlying_key: str = "",
        segment: str = "NSE_FNO",
    ) -> None:
        self.spec, self.kind, self.bt, self.costs = spec, kind, bt, costs
        self.model = OptionPricer(bt, vix)
        self.stored = stored
        self.live = live
        self.underlying_key = underlying_key or spec.series_key
        self.segment = segment
        self.qty = 1 if kind == "STOCK" else int(spec.lot_size)
        self.slip_pct = float(bt.slippage_pct)
        self.tick = float(spec.tick_size)

    @property
    def is_option(self) -> bool:
        return self.kind != "STOCK"

    # ── stock ─────────────────────────────────────────────────────────────
    def stock_entry(self, side: int, open_: float) -> float:
        return float(open_) * (1 + side * self.slip_pct / 100.0)

    def stock_exit(self, side: int, level: float, slipped: bool) -> float:
        return float(level) * (1 - side * self.slip_pct / 100.0) if slipped else float(level)

    # ── option ────────────────────────────────────────────────────────────
    def contract_for(
        self, opt: str, spot: float, ts: pd.Timestamp, security_id: str | None = None
    ) -> Contract:
        bt = self.bt
        exp = monthly_expiry(ts.date(), bt.option_expiry_weekday, bt.option_min_days_to_expiry)
        t0 = year_frac(ts.to_pydatetime(), expiry_datetime(exp))
        k = strike_for_delta(
            spot,
            t0,
            self.model.iv_for(ts),
            opt,
            bt.option_target_delta,
            self.spec.strike_step,
            bt.risk_free_rate,
        )
        what = f"{self.underlying_key} {k:g} {opt} {exp.day} {MONTHS[exp.month - 1]}"
        return Contract(float(k), opt, exp, what, security_id)

    def model_premium(self, c: Contract, spot: float, ts: pd.Timestamp) -> float:
        return float(self.model.premium(spot, c.strike, ts, expiry_datetime(c.expiry), c.opt))

    def delta(self, c: Contract, spot: float, ts: pd.Timestamp) -> float:
        return abs(float(self.model.delta(spot, c.strike, ts, expiry_datetime(c.expiry), c.opt)))

    def premium(
        self,
        c: Contract,
        minute: pd.Timestamp,
        bar: tuple[float, float, float, float],
        level: float,
        how: str,
    ) -> Quote:
        """The option's price when the index stood at `level` in the minute starting at
        `minute` whose index bar is `bar` (open, high, low, close). `how`: `open` (a fill or
        exit at the open), `touch` (a level inside the minute) or `close` (a mark)."""
        if self.live is not None and c.security_id:
            when = minute + pd.Timedelta(minutes=1) if how == "close" else minute
            option = {
                "security_id": c.security_id,
                "segment": self.segment,
                "underlying": self.underlying_key,
                "opt_type": c.opt,
            }
            try:
                got = self.live(option, float(level), when.to_pydatetime())
            except Exception:  # noqa: BLE001 - the model prices it instead
                got = None
            if got is not None and got > 0:
                return Quote(float(got), "real")
        if self.stored is not None:
            row = self.stored.bar(c, minute)
            if row is not None:
                o, h, lo, cl = float(row[0]), float(row[1]), float(row[2]), float(row[3])
                if how == "open":
                    return Quote(o, "real")
                if how == "close":
                    return Quote(cl, "real")
                return Quote(interpolate((o, h, lo, cl), bar, float(level), c.opt), "real")
        when = minute + pd.Timedelta(minutes=1) if how == "close" else minute
        return Quote(self.model_premium(c, float(level), when), "model")

    def option_entry(self, raw: float) -> float:
        slip = self.bt.option_slippage_ticks * self.tick
        return round_to_tick(raw * (1 + self.bt.option_spread_pct) + slip, self.tick, "up")

    def option_exit(self, raw: float) -> float:
        slip = self.bt.option_slippage_ticks * self.tick
        fill = round_to_tick(raw * (1 - self.bt.option_spread_pct) - slip, self.tick, "down")
        return max(fill, self.tick)

    # ── money ─────────────────────────────────────────────────────────────
    def charges(self, side: int, entry: float, exit_: float) -> float:
        """Both legs' charges in rupees for one share or one lot."""
        if self.is_option:
            return (
                self.costs.leg(entry, self.qty, "buy").total
                + self.costs.leg(exit_, self.qty, "sell").total
            )
        first: Side = "buy" if side == 1 else "sell"
        second: Side = "sell" if side == 1 else "buy"
        return (
            self.costs.leg(entry, self.qty, first).total
            + self.costs.leg(exit_, self.qty, second).total
        )

    def gross(self, side: int, entry: float, exit_: float) -> float:
        """Rupees before charges: a share's move in the trade's direction, or the premium's."""
        sign = 1 if self.is_option else side
        return (exit_ - entry) * sign * self.qty
