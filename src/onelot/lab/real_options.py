"""Real option premiums for Lab trades (Dhan expired-options data), beside the Black-76 model.

The simulator decides every trade on the UNDERLYING (entry at a bar open, exits at stop,
target, trail, time or session levels), so its trades do not depend on how the option is
priced. This module re-prices the SAME trades with the premiums that actually traded:

* the contract: the model's strike K (`execution.strike_for_delta` at the model's IV) in the
  NEAR monthly contract, or the NEXT one when the near contract expires within
  `option_min_days_to_expiry` days (the model's roll rule). The days left are read from the
  market itself (`days_from_ratio`): at-the-money time value grows with the square root of
  the time left, so the near/next time-value ratio R on the same minute gives
  T_near = R^2 G / (1 - R^2), with G ~ 30 days between monthly expiries. (Dhan's own `iv`
  field cannot be used for this: it runs far below the IV that prices its bars - 8.0 % vs
  13.1 % for NIFTY on 2024-07-26 - so inverting it overstates the time left 2-3 fold.) The
  model's own expiry rule (last Tuesday) is wrong before Sep 2025, when NSE's monthly
  contracts expired on Thursdays: a few days a month the model priced a contract days from
  expiry while the market's near contract had a month left;
* the entry: the option's one-minute OPEN at the entry minute (the bar the underlying fills
  at). No trade in that minute: the first trade within `max_wait_min` minutes, at its open;
* an exit at a bar open (strategy signal, time stop, session end, a gap through a level):
  the option's open in that minute;
* an exit at a level inside a strategy bar (stop, trail, target, partial): the first minute
  whose underlying range reaches the level, and inside that minute the option price
  interpolated between its open and its extreme as far as the underlying went between its
  open and its extreme (`interpolate`): the open when the level is the open, the option's
  low (CE) or high (PE) when the level is the underlying's low;
* `eod` (the close of a session's last bar): the option's close in that minute;
* the fills keep the model's premium friction (half-spread `option_spread_pct` and
  `option_slippage_ticks` against the trader, the cost table's charges). The simulator's
  UNDERLYING slippage is taken back out of the exit levels (`raw_level`): a real premium is
  already what traded at that moment, while the model priced its premium at a level made
  worse by `slippage_pct` AND then took the spread and ticks again on the premium.

Nothing is invented: a trade whose strike is beyond ATM+-10, or whose option did not trade
near a needed minute, is UNPRICED with the reason, and reports count them.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..analytics.blackscholes import price as b76_price
from ..clock import IST
from ..costs import round_to_tick
from ..data.expired_options import (
    MAX_REL,
    Block,
    OptionBarStore,
    RollingKey,
    RollingSource,
    Underlying,
    fetch_days,
)

DAY_SECONDS = 86400.0
YEAR_DAYS = 365.0
OPEN_REASONS = {"signal", "time", "square_off"}
UNSLIPPED_REASONS = {"target", "partial"}  # the simulator fills these at the level itself


def raw_level(price: float, reason: str, side: int, slip_pct: float) -> float:
    """The underlying level behind a simulator exit price: stops, trails, opens and the eod
    close carry `slippage_pct` against the trader; targets and partials do not."""
    if reason in UNSLIPPED_REASONS:
        return float(price)
    return float(price) / (1 - side * slip_pct / 100.0)


def implied_days(
    premium: float, spot: float, strike: float, iv_pct: float, opt: str, r: float = 0.065
) -> float | None:
    """Calendar days to expiry that price `premium` at Dhan's IV (Black-76 on the spot, the
    Lab's convention). None when the bar cannot say: no IV, or no time value left (a
    premium at or under intrinsic). Bisection on [1 minute, 120 days]."""
    if not (premium > 0 and spot > 0 and strike > 0 and iv_pct and iv_pct > 0):
        return None
    sigma = iv_pct / 100.0
    intrinsic = max(spot - strike, 0.0) if opt == "CE" else max(strike - spot, 0.0)
    if premium <= intrinsic + 1e-6:
        return 0.0
    lo, hi = 60.0 / (DAY_SECONDS * YEAR_DAYS), 120.0 / YEAR_DAYS
    if b76_price(spot, strike, hi, sigma, r, opt) < premium:  # type: ignore[arg-type]
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if b76_price(spot, strike, mid, sigma, r, opt) < premium:  # type: ignore[arg-type]
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi) * YEAR_DAYS


def days_from_ratio(ratio: float, gap_days: float = 30.0) -> float | None:
    """Days left on the near contract from R = near / next at-the-money time value."""
    if not (ratio > 0) or ratio >= 1:
        return None
    r2 = ratio * ratio
    return r2 * gap_days / (1 - r2)


def atm_rel(strike: float, spot: float, step: float) -> int:
    """How many strike steps `strike` sits above the strike nearest `spot` (Dhan's ATM)."""
    atm = round(spot / step) * step
    return int(round((strike - atm) / step))


def interpolate(
    opt_bar: tuple[float, float, float, float],
    und_bar: tuple[float, float, float, float],
    level: float,
    opt: str,
) -> float:
    """The option's price when the underlying stood at `level` inside one minute.

    The option moves with the underlying (a CE up, a PE down), so the share of the way the
    underlying went from its open towards its extreme on that side is the share of the way
    the option went from its open towards ITS extreme on the matching side. Clamped to the
    option's own range."""
    o_o, o_h, o_l, _ = opt_bar
    u_o, u_h, u_l, _ = und_bar
    up = level >= u_o
    span = (u_h - u_o) if up else (u_o - u_l)
    frac = 0.0 if span <= 0 else min(max(abs(level - u_o) / span, 0.0), 1.0)
    rises = up == (opt == "CE")  # a CE rises with the underlying, a PE falls
    target = o_h if rises else o_l
    px = o_o + frac * (target - o_o)
    return float(min(max(px, o_l), o_h))


def premium_in_minute(
    opt_bars: pd.DataFrame,
    und_bars: pd.DataFrame,
    level: float,
    ts: Any,
    opt: str,
    tol: float = 0.006,
) -> float | None:
    """The option's REAL price when its underlying stood at `level` at `ts`, from both
    one-minute bar series (stamped at the minute's start), or None.

    An exact match comes first: the level is the underlying's open of the minute starting at
    `ts` (a fill or exit at an open: the option's open), or its close of the minute ending at
    `ts` or starting at it (a mark at a close: the option's close). Otherwise the level must
    lie inside the underlying's range in the minute starting at `ts` (else the minute ending
    there), and the price is `interpolate`d inside the option's own bar. A missing bar, or a
    level the underlying never reached in those minutes, is None: the caller's model prices it.
    """
    t = pd.Timestamp(ts)
    t = t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)
    start = t.floor("min")
    minutes = [start] if t != start else [start, start - pd.Timedelta(minutes=1)]
    rows = []
    for m in minutes:
        if m in und_bars.index and m in opt_bars.index:
            u, o = und_bars.loc[m], opt_bars.loc[m]
            u4 = (float(u["open"]), float(u["high"]), float(u["low"]), float(u["close"]))
            o4 = (float(o["open"]), float(o["high"]), float(o["low"]), float(o["close"]))
            rows.append((m, u4, o4))
    for m, u, o in rows:  # exact: an open at the minute's start, a close at its end
        if m == start and abs(level - u[0]) <= tol:
            return o[0]
        if abs(level - u[3]) <= tol:
            return o[3]
    for _m, u, o in rows:
        if u[2] - tol <= level <= u[1] + tol:
            return interpolate(o, u, level, opt)
    return None


@dataclass
class RealTrade:
    """One trade priced both ways. `real_*` are None when the trade is unpriced."""

    entry_ts: pd.Timestamp
    side: int
    strike: float
    opt: str
    model_ret_pct: float
    model_net: float
    code: int | None = None
    days_to_expiry: float | None = None
    real_entry: float | None = None
    real_exit: float | None = None
    real_net: float | None = None
    real_ret_pct: float | None = None
    real_gross: float | None = None
    entry_wait_min: int = 0
    exit_wait_min: int = 0
    rels: list[int] = field(default_factory=list)
    unpriced: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["entry_ts"] = self.entry_ts.isoformat()
        return d


class NeedBlock(Exception):
    """A day block is missing and fetching is off: the caller collects and fetches it."""

    def __init__(self, key: RollingKey, day: dt.date) -> None:
        super().__init__(f"{key} {day}")
        self.key, self.day = key, day


class RealPricer:
    """Prices one underlying's option trades from the expired-options store."""

    def __init__(
        self,
        und: Underlying,
        spec: Any,  # InstrumentSpec of the options (lot, tick)
        bt: Any,  # BacktestCfg (spread, slippage ticks, min days, rate)
        costs: Any,  # CostModel for the option product
        store: OptionBarStore,
        und_1m: pd.DataFrame,
        tf_minutes: int,
        source: RollingSource | None = None,
        max_wait_min: int = 3,
        slip_pct: float = 0.0,
    ) -> None:
        self.und, self.spec, self.bt, self.costs = und, spec, bt, costs
        self.slip_pct = float(slip_pct)
        self.store, self.source = store, source
        self.tf = int(tf_minutes)
        self.max_wait = int(max_wait_min)
        u = und_1m[["open", "high", "low", "close"]]
        # epoch seconds whatever the index's resolution (s, ms, us or ns in pandas 2)
        self._u_ts = (u.index.as_unit("ns").asi8 // 1_000_000_000).astype("int64")
        self._u = u.to_numpy(float)
        self._u_pos = {int(t): i for i, t in enumerate(self._u_ts)}
        self._idx: dict[tuple, dict[int, int]] = {}
        self._left: dict[tuple, float | None] = {}
        self.requests = 0

    # ── data ──────────────────────────────────────────────────────────────
    def _block(self, key: RollingKey, day: dt.date) -> Block:
        blk = self.store.get(key, day)
        if blk is not None and blk.complete:
            return blk
        if self.source is None:
            raise NeedBlock(key, day)
        for b in fetch_days(self.source, self.und, key, day):
            self.store.put(b)
        self.requests += 1
        blk = self.store.get(key, day)
        assert blk is not None
        return blk

    def _bar_at(
        self, key: RollingKey, day: dt.date, epoch: int, strike: float
    ) -> np.ndarray | None:
        """The row (open, high, low, close, iv, spot) of `strike`'s bar at exactly `epoch` in
        one rolling series, or None."""
        blk = self._block(key, day)
        pk = (key, day)
        pos = self._idx.get(pk)
        if pos is None:
            pos = {int(t): i for i, t in enumerate(blk.data["ts"])}
            self._idx[pk] = pos
        i = pos.get(epoch)
        d = blk.data
        if i is None or abs(float(d["strike"][i]) - strike) >= 1e-6:
            return None
        return np.array([d[c][i] for c in ("open", "high", "low", "close", "iv", "spot")], float)

    def _find(
        self, code: int, opt: str, strike: float, epoch: int, day: dt.date, wait: int
    ) -> tuple[int, np.ndarray, int] | str:
        """(minutes waited, row, rel) of the first bar of `strike` at `epoch` or within `wait`
        minutes after it, else why not ("beyond", "no index bar", "no trade").

        Minute by minute, earliest first: each minute is looked up in the rolling series its
        OWN spot points to (the at-the-money strike moves while one waits), then in that
        series' two neighbours (Dhan's spot can round a half step the other way: our close is
        not Dhan's spot to the paisa, and Python rounds a tie to even)."""
        beyond = seen = False
        for w in range(wait + 1):
            minute = epoch + 60 * w
            upos = self._u_pos.get(minute)
            if upos is None:
                continue
            seen = True
            rel0 = atm_rel(strike, float(self._u[upos, 3]), self.und.strike_step)
            beyond = beyond or abs(rel0) > MAX_REL
            for rel in (rel0, rel0 - 1, rel0 + 1):
                if abs(rel) > MAX_REL:
                    continue
                row = self._bar_at(RollingKey(self.und.key, code, opt, rel), day, minute, strike)
                if row is not None:
                    return w, row, rel
        if beyond:
            return "beyond"
        return "no trade" if seen else "no index bar"

    def _why(self, miss: str, opt: str, strike: float, where: str) -> str:
        if miss == "beyond":
            return f"{opt} {strike:g} was beyond ATM+-{MAX_REL} {where}"
        if miss == "no index bar":
            return f"no index bar {where}"
        return f"no {opt} {strike:g} trade within {self.max_wait} min {where}"

    def _near_days_left(self, day: dt.date, epoch: int) -> float | None:
        """Days left on the near contract at `epoch`: the median near/next ratio of the
        at-the-money CALL's time value over the minutes within 30 of it (`days_from_ratio`).
        None when either contract has no bars there."""
        key = (day, epoch // 1800)
        if key in self._left:
            return self._left[key]
        near = self._block(RollingKey(self.und.key, 1, "CE", 0), day).data
        nxt = self._block(RollingKey(self.und.key, 2, "CE", 0), day).data
        lo, hi = epoch - 1800, epoch + 1800
        a = {int(t): i for i, t in enumerate(near["ts"]) if lo <= t <= hi}
        ratios = []
        for j, t in enumerate(nxt["ts"]):
            i = a.get(int(t))
            if i is None or abs(near["strike"][i] - nxt["strike"][j]) > 1e-6:
                continue
            k, s1 = float(near["strike"][i]), float(near["spot"][i])
            tv1 = float(near["close"][i]) - max(s1 - k, 0.0)
            tv2 = float(nxt["close"][j]) - max(float(nxt["spot"][j]) - k, 0.0)
            if tv2 > 0:
                ratios.append(max(tv1, 0.0) / tv2)
        left = days_from_ratio(float(np.median(ratios))) if ratios else None
        if ratios and float(np.median(ratios)) <= 0:
            left = 0.0
        self._left[key] = left
        return left

    # ── the minute an exit happened ─────────────────────────────────────────
    def _exit_minute(self, leg_ts: pd.Timestamp, level: float, reason: str) -> tuple[int, str]:
        """(epoch of the minute, how): `open`, `close` or `touch`."""
        start = int(leg_ts.value // 1_000_000_000)
        first = self._u_pos.get(start)
        if reason in OPEN_REASONS or first is None:
            return start, "open"
        end = start + 60 * self.tf
        i = first
        if reason == "eod":
            while i + 1 < len(self._u_ts) and self._u_ts[i + 1] < end:
                i += 1
            return int(self._u_ts[i]), "close"
        u_open = self._u[first, 0]
        if abs(level - u_open) < 1e-6:
            return start, "open"
        down = level < u_open
        while i < len(self._u_ts) and self._u_ts[i] < end:
            lo, hi = self._u[i, 2], self._u[i, 1]
            if (down and lo <= level + 1e-6) or ((not down) and hi >= level - 1e-6):
                return int(self._u_ts[i]), "touch"
            i += 1
        return start, "open"  # the level was a fill at the bar open after a gap

    # ── pricing ─────────────────────────────────────────────────────────────
    def price(self, t: Any, model_row: dict) -> RealTrade:
        det = model_row.get("detail") or {}
        opt = "CE" if t.side == 1 else "PE"
        strike = float(det["strike"])
        rt = RealTrade(
            pd.Timestamp(t.entry_ts),
            int(t.side),
            strike,
            opt,
            float(model_row.get("ret_pct") or 0.0),
            float(model_row.get("net_pnl") or 0.0),
        )
        entry = pd.Timestamp(t.entry_ts).tz_convert(IST)
        day = entry.date()
        e0 = int(entry.value // 1_000_000_000)
        min_days = float(self.bt.option_min_days_to_expiry)
        left = self._near_days_left(day, e0)
        rt.days_to_expiry = left
        code = 1
        if left is not None:
            expiry_day = (entry + pd.Timedelta(days=left)).date()
            if (expiry_day - day).days < min_days:
                code = 2  # the model's roll: never the contract in its last days
        rt.code = code
        hit = self._find(code, opt, strike, e0, day, self.max_wait)
        if isinstance(hit, str):
            rt.unpriced = self._why(hit, opt, strike, "of entry")
            return rt
        rt.entry_wait_min, rt.rels = hit[0], [hit[2]]
        code = int(rt.code or 1)
        p_entry = float(hit[1][0])
        legs_px: list[tuple[float, float]] = []
        worst_wait = 0
        for leg in t.legs:
            ts = pd.Timestamp(leg.ts).tz_convert(IST)
            level = raw_level(float(leg.price), str(leg.reason), int(t.side), self.slip_pct)
            epoch, how = self._exit_minute(ts, level, str(leg.reason))
            found = self._find(code, opt, strike, epoch, ts.date(), self.max_wait)
            if isinstance(found, str):
                rt.unpriced = self._why(
                    found, opt, strike, f"of the {leg.reason} exit at {ts:%H:%M}"
                )
                return rt
            wait, row, rel = found
            worst_wait = max(worst_wait, wait)
            rt.rels.append(rel)
            if how == "touch" and wait == 0:
                upos = self._u_pos[epoch]
                px = interpolate(
                    (row[0], row[1], row[2], row[3]),
                    tuple(self._u[upos]),  # type: ignore[arg-type]
                    level,
                    opt,
                )
            elif how == "close" and wait == 0:
                px = float(row[3])
            else:
                px = float(row[0])  # the open of the exit minute (or of the first trade after)
            legs_px.append((px, float(leg.frac)))
        rt.exit_wait_min = worst_wait
        self._book(rt, p_entry, legs_px)
        return rt

    def _book(self, rt: RealTrade, p_entry: float, legs_px: list[tuple[float, float]]) -> None:
        """The model's friction on real premiums: identical to `execution.execute_trade`."""
        lot = self.spec.lot_size
        tick = self.spec.tick_size
        half = self.bt.option_spread_pct
        slip = self.bt.option_slippage_ticks * tick
        entry_fill = round_to_tick(p_entry * (1 + half) + slip, tick, "up")
        gross = 0.0
        cost = self.costs.leg(entry_fill, lot, "buy").total
        exits = []
        for px, frac in legs_px:
            fill = max(round_to_tick(px * (1 - half) - slip, tick, "down"), tick)
            q = lot * frac
            gross += (fill - entry_fill) * q
            cost += self.costs.leg(fill, int(round(q)) or 1, "sell").total * (
                q / max(int(round(q)), 1)
            )
            exits.append((fill, frac))
        tot = sum(f for _, f in exits) or 1.0
        rt.real_entry = round(p_entry, 2)
        rt.real_exit = round(sum(px * f for px, f in legs_px) / tot, 2)
        rt.real_gross = round(gross, 2)
        rt.real_net = round(gross - cost, 2)
        notional = entry_fill * lot
        rt.real_ret_pct = round(rt.real_net / notional * 100, 4) if notional else None


def finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(float(v))


__all__ = [
    "NeedBlock",
    "RealPricer",
    "RealTrade",
    "atm_rel",
    "days_from_ratio",
    "implied_days",
    "interpolate",
    "premium_in_minute",
    "raw_level",
]
