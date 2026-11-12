"""Per-symbol trade simulator (bar-by-bar, closed-bar signals, realistic fills).

Rules (shared with the live Guardian so research matches what you will be told live):
* A signal on bar t (known at its close) is filled at bar t+1 OPEN +/- slippage, same session only.
* If the open gaps through the stop, the trade is skipped (entry) or exited at the open (exit).
* Initial risk is widened to `stop_atr_min` x ATR and setups riskier than `stop_atr_max` x ATR skipped.
* Inside a bar, if both stop and target/partial are touched, the finer 1-minute path decides when
  available; otherwise the stop is assumed first (conservative).
* Trailing stop: after the trade is `trail_activate_r` R in profit, stop = max(stop, close - k*ATR(n))
  updated at each bar CLOSE (effective from the next bar - no look-ahead).
* Line trail: a strategy may give `long_trail` / `short_trail` columns, the level its own line
  (a Supertrend line, say) allows the stop to stand at, known at each bar's CLOSE. The stop follows
  it from the next bar, only ever in the trade's favour (a long's stop never comes down), from the
  entry bar on and with no profit needed first. NaN means "no level on this bar". A stop the line
  has carried past the price is a gap through the stop at the next open.
* Strategy exit signals and time stops exit at the next bar open; everything is flat by square-off.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..indicators import core as ind
from ..strategies.base import StrategyParams


@dataclass
class Leg:
    ts: pd.Timestamp
    price: float
    frac: float
    reason: str


@dataclass
class SimTrade:
    symbol: str
    strategy: str
    side: int  # +1 long / -1 short
    signal_ts: pd.Timestamp
    entry_ts: pd.Timestamp
    entry_price: float
    stop_initial: float
    risk_ps: float
    target: float | None
    strength: float
    atr: float
    legs: list[Leg] = field(default_factory=list)
    mfe: float = 0.0  # best excursion per share
    mae: float = 0.0  # worst excursion per share (positive number)
    bars_held: int = 0

    @property
    def exit_ts(self) -> pd.Timestamp:
        return self.legs[-1].ts if self.legs else self.entry_ts

    @property
    def exit_reason(self) -> str:
        return self.legs[-1].reason if self.legs else "open"

    @property
    def avg_exit(self) -> float:
        tot = sum(l.frac for l in self.legs)
        return sum(l.price * l.frac for l in self.legs) / tot if tot else self.entry_price

    @property
    def pnl_ps(self) -> float:
        return sum((l.price - self.entry_price) * self.side * l.frac for l in self.legs)

    @property
    def r_multiple(self) -> float:
        return self.pnl_ps / self.risk_ps if self.risk_ps > 0 else 0.0


def _minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _hhmm_min(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


class PathResolver:
    """Uses finer (1-minute) bars to decide which level a coarse bar touched first."""

    def __init__(self, fine: pd.DataFrame | None, tf_minutes: int) -> None:
        self.fine = fine if fine is not None and not fine.empty else None
        self.tf = pd.Timedelta(minutes=tf_minutes)

    def first_hit(self, start: pd.Timestamp, side: int, stop: float, other: float) -> str | None:
        """'stop' | 'other' | None (no fine data). `other` is the favourable level (target/partial)."""
        if self.fine is None:
            return None
        win = self.fine.loc[start : start + self.tf - pd.Timedelta(seconds=1)]
        if win.empty:
            return None
        for h, l in zip(win["high"].to_numpy(), win["low"].to_numpy(), strict=True):
            s_hit = (l <= stop) if side == 1 else (h >= stop)
            o_hit = (h >= other) if side == 1 else (l <= other)
            if s_hit and o_hit:
                return "stop"  # same minute: stay conservative
            if s_hit:
                return "stop"
            if o_hit:
                return "other"
        return None


def simulate(
    f: pd.DataFrame,
    p: StrategyParams,
    symbol: str,
    strategy: str,
    slippage_pct: float = 0.03,
    fine: pd.DataFrame | None = None,
    start: pd.Timestamp | None = None,
) -> list[SimTrade]:
    n = len(f)
    if n < 3:
        return []
    idx = f.index
    o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr = f["atr"].to_numpy(float)
    atr_tr = f["atr_trail"].to_numpy(float)
    d_atr = f["d_atr"].to_numpy(float) if "d_atr" in f.columns else np.full(n, np.nan)
    le, se = f["long_entry"].to_numpy(bool), f["short_entry"].to_numpy(bool)
    ls, ss = f["long_stop"].to_numpy(float), f["short_stop"].to_numpy(float)
    lsc, ssc = f["long_score"].to_numpy(float), f["short_score"].to_numpy(float)
    lx, sx = f["long_exit"].to_numpy(bool), f["short_exit"].to_numpy(bool)
    # the strategy's own line trail (optional): the level the stop may stand at after each close
    ltr = f["long_trail"].to_numpy(float) if "long_trail" in f.columns else None
    strl = f["short_trail"].to_numpy(float) if "short_trail" in f.columns else None
    dkey = ind.session_key(idx)
    tmin = np.array([_minutes(t) for t in idx.time])
    last_of_day = np.r_[dkey[1:] != dkey[:-1], True]
    sq_min = _hhmm_min(p.exit_time)
    slip = slippage_pct / 100.0
    tf_minutes = max(1, int(round((idx[1] - idx[0]).total_seconds() / 60))) if n > 1 else 5
    resolver = PathResolver(fine, tf_minutes)
    start_i = int(idx.searchsorted(start)) if start is not None else 0

    trades: list[SimTrade] = []
    pos: SimTrade | None = None
    remaining = 0.0
    stop = target = partial_px = None
    partial_done = False
    pending = None  # (side, signal_i, stop, score)
    pending_exit: str | None = None
    last_exit_i = -(10**9)
    per_day: dict[int, int] = {}

    def close_leg(i: int, price: float, frac: float, reason: str) -> None:
        nonlocal remaining, pos, last_exit_i, pending_exit
        pos.legs.append(Leg(idx[i], float(price), float(frac), reason))
        remaining -= frac
        if remaining <= 1e-9:
            pos.bars_held = i - entry_i + 1
            trades.append(pos)
            pos = None
            last_exit_i = i
            pending_exit = None

    entry_i = 0
    for i in range(n):
        # ---------------------------------------------------------------- 1) fill a pending entry
        if pending is not None and pos is None:
            side, si, sig_stop, score = pending
            pending = None
            if dkey[i] == dkey[si] and tmin[i] < sq_min:
                fill = o[i] * (1 + slip * side)
                a = atr[si]
                risk = (fill - sig_stop) * side
                if not np.isnan(a) and risk > 0:
                    st = sig_stop
                    max_risk = min(p.stop_atr_max * a, p.max_risk_pct / 100 * fill)
                    floor = max(
                        p.stop_atr_min * a,
                        p.min_risk_pct / 100 * fill,
                        p.stop_datr_min * d_atr[si] if not np.isnan(d_atr[si]) else 0.0,
                    )
                    if risk < floor:
                        risk = floor
                        st = fill - side * risk
                    if risk <= max(max_risk, floor if p.min_risk_pct > 0 else 0):
                        pos = SimTrade(
                            symbol,
                            strategy,
                            side,
                            idx[si],
                            idx[i],
                            float(fill),
                            float(st),
                            float(risk),
                            float(fill + side * p.target_r * risk) if p.target_r > 0 else None,
                            float(score) if not np.isnan(score) else 0.0,
                            float(a),
                        )
                        remaining, stop, target = 1.0, st, pos.target
                        partial_px = fill + side * p.partial_r * risk if p.partial_r > 0 else None
                        partial_done = False
                        entry_i = i
                        per_day[dkey[i]] = per_day.get(dkey[i], 0) + 1

        # ---------------------------------------------------------------- 2) manage open position
        if pos is not None:
            side = pos.side
            if pending_exit is None and tmin[i] < sq_min:
                # excursions inside this bar (MAE capped at the stop if it is hit)
                fav = (h[i] - pos.entry_price) if side == 1 else (pos.entry_price - l[i])
                adv = (pos.entry_price - l[i]) if side == 1 else (h[i] - pos.entry_price)
                stop_dist = (pos.entry_price - stop) * side
                pos.mae = max(
                    pos.mae, min(adv, max(stop_dist, 0.0)) if adv >= stop_dist > 0 else adv
                )
                pos.mfe = max(
                    pos.mfe, fav if target is None else min(fav, abs(target - pos.entry_price))
                )
            if pending_exit is not None or tmin[i] >= sq_min:
                reason = pending_exit or "square_off"
                close_leg(i, o[i] * (1 - slip * side), remaining, reason)
            else:
                # gap through the stop at the open
                if (o[i] - stop) * side <= 0:
                    close_leg(i, o[i] * (1 - slip * side), remaining, _stop_reason(pos, stop))
                else:
                    hit_stop = (l[i] <= stop) if side == 1 else (h[i] >= stop)
                    fav_levels = []
                    if partial_px is not None and not partial_done:
                        fav_levels.append(("partial", partial_px))
                    if target is not None:
                        fav_levels.append(("target", target))
                    fav_hit = [
                        (k, v) for k, v in fav_levels if ((h[i] >= v) if side == 1 else (l[i] <= v))
                    ]
                    stop_first = hit_stop
                    if hit_stop and fav_hit:
                        first = resolver.first_hit(idx[i], side, stop, fav_hit[0][1])
                        stop_first = first != "other"
                    if hit_stop and stop_first:
                        close_leg(i, stop * (1 - slip * side), remaining, _stop_reason(pos, stop))
                    else:
                        for kind, lvl in fav_hit:
                            if pos is None:
                                break
                            px = (
                                lvl if (o[i] - lvl) * side < 0 else o[i]
                            )  # gap beyond the level fills at open
                            if kind == "partial":
                                close_leg(i, px, min(p.partial_frac, remaining), "partial")
                                partial_done = True
                                if pos is not None and p.breakeven_after_partial:
                                    stop = (
                                        max(stop, pos.entry_price)
                                        if side == 1
                                        else min(stop, pos.entry_price)
                                    )
                            else:
                                close_leg(i, px, remaining, "target")
                        # after a partial, the stop may still be hit later in the same bar
                        if (
                            pos is not None
                            and partial_done
                            and fav_hit
                            and fav_hit[0][0] == "partial"
                        ):
                            if (
                                (l[i] <= stop) if side == 1 else (h[i] >= stop)
                            ) and stop != pos.stop_initial:
                                close_leg(
                                    i, stop * (1 - slip * side), remaining, _stop_reason(pos, stop)
                                )
            if pos is not None:
                side = pos.side
                # trailing stop (effective from next bar)
                if (
                    p.trail_atr_mult > 0
                    and not np.isnan(atr_tr[i])
                    and pos.mfe >= p.trail_activate_r * pos.risk_ps
                ):
                    cand = c[i] - side * p.trail_atr_mult * atr_tr[i]
                    stop = max(stop, cand) if side == 1 else min(stop, cand)
                # the strategy's line trail: the stop follows the line, never back (next bar on)
                line = ltr if side == 1 else strl
                if line is not None and not np.isnan(line[i]):
                    stop = max(stop, line[i]) if side == 1 else min(stop, line[i])
                if (side == 1 and lx[i]) or (side == -1 and sx[i]):
                    pending_exit = "signal"
                elif p.max_bars and (i - entry_i + 1) >= p.max_bars:
                    pending_exit = "time"
                if last_of_day[i]:
                    close_leg(i, c[i] * (1 - slip * side), remaining, "eod")

        # ---------------------------------------------------------------- 3) new signal -> pending entry
        if (
            pos is None
            and pending is None
            and i >= start_i
            and i + 1 < n
            and dkey[i + 1] == dkey[i]
        ):
            if (
                (le[i] or se[i])
                and (i - last_exit_i) > p.cooldown_bars
                and per_day.get(dkey[i], 0) < p.max_trades_per_day
            ):
                if le[i] and se[i]:
                    side = 1 if (np.nan_to_num(lsc[i]) >= np.nan_to_num(ssc[i])) else -1
                else:
                    side = 1 if le[i] else -1
                sig_stop = ls[i] if side == 1 else ss[i]
                if not np.isnan(sig_stop):
                    pending = (side, i, sig_stop, lsc[i] if side == 1 else ssc[i])
    return trades


def _stop_reason(pos: SimTrade, stop: float) -> str:
    if abs(stop - pos.entry_price) < 1e-9:
        return "breakeven"
    moved = (stop - pos.stop_initial) * pos.side > 1e-9
    return "trail" if moved else "stop"
