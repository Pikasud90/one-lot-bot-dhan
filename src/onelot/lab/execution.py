"""Execution models: one simulated trade on the underlying -> rupees in the instrument.

The simulator works entirely on the underlying series (index, stock, spot FX)
and returns `SimTrade`s with entry/exit LEVELS on that series. This module is
where the one-lot rule meets the instrument:

    STOCK      1 share at the underlying price.
    FUTURES    1 lot at the underlying price (basis is ignored; documented).
    CURRENCY   1 lot of the currency future at the spot level.
    OPTIONS    1 lot of a delta-selected CE/PE, PRICED WITH BLACK-76 off the
               underlying path. IV comes from India VIX (scaled) or a constant.

The option model is synthetic on purpose: no keyless source serves Indian
option history. Once Dhan's expired-options endpoint is wired, real premiums
replace the model through the same interface (`OptionPricer`). Every result
produced with the synthetic pricer is labelled as such.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..analytics.blackscholes import greeks as b76_greeks
from ..analytics.blackscholes import price as b76_price
from ..clock import IST
from ..config import BacktestCfg
from ..costs import CostModel, round_to_tick
from ..data.universe import InstrumentSpec
from .simulator import SimTrade

SECONDS_PER_YEAR = 365.0 * 24 * 3600


# ───────────────────────────── option helpers ────────────────────────────

def monthly_expiry(day: dt.date, weekday: int, min_days: int) -> dt.date:
    """The last `weekday` of the month on or after `day` + `min_days`."""
    target = day + dt.timedelta(days=min_days)
    y, m = target.year, target.month
    for _ in range(3):
        last = calendar.monthrange(y, m)[1]
        d = dt.date(y, m, last)
        while d.weekday() != weekday:
            d -= dt.timedelta(days=1)
        if d >= target:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return d  # pragma: no cover


def expiry_datetime(d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(15, 30), tzinfo=IST)


def year_frac(now: dt.datetime, expiry: dt.datetime) -> float:
    return max((expiry - now).total_seconds(), 60.0) / SECONDS_PER_YEAR


def strike_for_delta(spot: float, t: float, iv: float, opt: str, target: float, step: float,
                     r: float = 0.0) -> float:
    """Strike on the exchange grid whose |delta| is closest to `target`."""
    step = step or max(round(spot * 0.002, -1), 1.0)
    atm = round(spot / step) * step
    best, best_err = atm, 9.0
    # ITM calls are below spot, ITM puts above: scan 25 strikes each side of ATM.
    for i in range(-25, 26):
        k = atm + i * step
        if k <= 0:
            continue
        d = abs(b76_greeks(spot, k, t, iv, r, opt).delta)
        err = abs(d - target)
        if err < best_err:
            best, best_err = k, err
    return float(best)


@dataclass
class OptionPricer:
    """Black-76 synthetic premiums off the underlying path."""

    cfg: BacktestCfg
    vix: pd.Series | None = None          # daily INDIA VIX closes (percent), IST-indexed

    def iv_for(self, ts: pd.Timestamp) -> float:
        if self.cfg.option_iv_source == "constant" or self.vix is None or self.vix.empty:
            return float(self.cfg.option_iv_constant)
        day = ts.normalize()
        s = self.vix
        pos = s.index.searchsorted(day, side="right") - 1
        if pos < 0:
            return float(self.cfg.option_iv_constant)
        return float(s.iloc[pos]) / 100.0 * float(self.cfg.option_iv_mult)

    def premium(self, spot: float, k: float, ts: pd.Timestamp, expiry: dt.datetime, opt: str) -> float:
        t = year_frac(ts.to_pydatetime(), expiry)
        return b76_price(spot, k, t, self.iv_for(ts), self.cfg.risk_free_rate, opt)

    def delta(self, spot: float, k: float, ts: pd.Timestamp, expiry: dt.datetime, opt: str) -> float:
        t = year_frac(ts.to_pydatetime(), expiry)
        return b76_greeks(spot, k, t, self.iv_for(ts), self.cfg.risk_free_rate, opt).delta


# ───────────────────────────── execution ─────────────────────────────────

@dataclass
class Fill:
    price: float
    qty: float


def _fill_linear(level: float, side_sign: int, slippage_pct: float, tick: float) -> float:
    """Underlying-level fill with % slippage against you; side_sign +1 buys, -1 sells."""
    px = level * (1 + side_sign * slippage_pct / 100.0)
    return round_to_tick(px, tick, "up" if side_sign > 0 else "down") if tick > 0 else px


def execute_trade(t: SimTrade, spec: InstrumentSpec, costs: CostModel, bt: BacktestCfg,
                  slippage_pct: float, pricer: OptionPricer | None = None,
                  option_slippage_ticks: int | None = None) -> dict:
    """Translate one simulated underlying trade into a rupee P&L row.

    The simulator already applied `slippage_pct` to the underlying fills for
    linear instruments; here it is only re-applied for options (on the premium,
    in ticks) because their fills are modelled from scratch.
    """
    side = t.side
    lot = spec.lot_size
    per = spec.price_per or 1.0
    tick = spec.tick_size
    detail: dict = {"category": spec.category}

    if spec.category == "OPTIONS":
        assert pricer is not None, "options need an OptionPricer"
        opt = "CE" if side == 1 else "PE"
        entry_ts = pd.Timestamp(t.entry_ts)
        exp_date = monthly_expiry(entry_ts.date(), bt.option_expiry_weekday, bt.option_min_days_to_expiry)
        expiry = expiry_datetime(exp_date)
        t0 = year_frac(entry_ts.to_pydatetime(), expiry)
        iv0 = pricer.iv_for(entry_ts)
        k = strike_for_delta(t.entry_price, t0, iv0, opt, bt.option_target_delta, spec.strike_step,
                             bt.risk_free_rate)
        slip = (bt.option_slippage_ticks if option_slippage_ticks is None else option_slippage_ticks) * tick
        half = bt.option_spread_pct
        fair0 = pricer.premium(t.entry_price, k, entry_ts, expiry, opt)
        entry_fill = round_to_tick(fair0 * (1 + half) + slip, tick, "up")
        delta0 = abs(pricer.delta(t.entry_price, k, entry_ts, expiry, opt))
        # premium if the underlying stop is hit immediately: the rupee risk
        stop_fair = pricer.premium(t.stop_initial, k, entry_ts, expiry, opt)
        stop_fill = max(round_to_tick(stop_fair * (1 - half) - slip, tick, "down"), tick)
        risk_per_unit = max(entry_fill - stop_fill, tick)

        gross = 0.0
        cost = costs.leg(entry_fill, lot, "buy").total
        exits = []
        for leg in t.legs:
            ts = pd.Timestamp(leg.ts)
            fair = pricer.premium(leg.price, k, ts, expiry, opt)
            fill = max(round_to_tick(fair * (1 - half) - slip, tick, "down"), tick)
            q = lot * leg.frac
            gross += (fill - entry_fill) * q
            cost += costs.leg(fill, int(round(q)) or 1, "sell").total * (q / max(int(round(q)), 1))
            exits.append(fill)
        exit_fill = float(np.average(exits, weights=[leg.frac for leg in t.legs])) if exits else entry_fill
        net = gross - cost
        risk_inr = risk_per_unit * lot
        detail.update({"opt_type": opt, "strike": k, "expiry": exp_date.isoformat(),
                       "iv_entry": round(iv0, 4), "delta_entry": round(delta0, 3),
                       "premium_entry": entry_fill, "premium_exit": round(exit_fill, 2),
                       "underlying_entry": t.entry_price, "underlying_exit": round(t.avg_exit, 2),
                       "pricing": "black76_synthetic"})
        return _row(t, spec, entry_fill, exit_fill, lot, gross, cost, net, risk_inr,
                    mfe_inr=t.mfe * delta0 * lot, mae_inr=t.mae * delta0 * lot, detail=detail)

    # ── linear instruments: stock (1 share), futures / currency (1 lot) ──
    qty = 1.0 if spec.category == "STOCK" else float(lot)
    mult = qty / per
    entry_fill = t.entry_price
    gross = 0.0
    cost = costs.leg(entry_fill / per, int(qty), "buy" if side == 1 else "sell").total
    exit_vals = []
    for leg in t.legs:
        q = qty * leg.frac
        gross += (leg.price - entry_fill) * side * q / per
        cost += costs.leg(leg.price / per, max(int(round(q)), 1), "sell" if side == 1 else "buy").total
        exit_vals.append(leg.price)
    exit_fill = float(np.average(exit_vals, weights=[leg.frac for leg in t.legs])) if exit_vals else entry_fill
    net = gross - cost
    risk_inr = t.risk_ps * mult
    detail.update({"qty": qty, "underlying_entry": entry_fill, "underlying_exit": round(exit_fill, 4)})
    return _row(t, spec, entry_fill, exit_fill, qty, gross, cost, net, risk_inr,
                mfe_inr=t.mfe * mult, mae_inr=t.mae * mult, detail=detail)


def _row(t: SimTrade, spec: InstrumentSpec, entry: float, exit_: float, qty: float, gross: float,
         cost: float, net: float, risk_inr: float, mfe_inr: float, mae_inr: float, detail: dict) -> dict:
    risk_inr = max(risk_inr, 1e-9)
    notional = entry * qty
    return {
        "strategy": t.strategy, "instrument_key": spec.key, "series_key": spec.series_key,
        "side": "LONG" if t.side == 1 else "SHORT", "signal_ts": t.signal_ts, "entry_ts": t.entry_ts,
        "entry_price": round(entry, 4), "exit_ts": t.exit_ts, "exit_price": round(exit_, 4), "qty": qty,
        "stop": t.stop_initial, "target": t.target, "risk_ps": t.risk_ps, "risk_inr": round(risk_inr, 2),
        "gross_pnl": round(gross, 2), "costs": round(cost, 2), "net_pnl": round(net, 2),
        "ret_pct": round(net / notional * 100, 4) if notional else 0.0,
        "r_multiple": round(net / risk_inr, 4), "r_gross": round(gross / risk_inr, 4),
        "exit_reason": t.exit_reason, "strength": t.strength,
        "mae_r": round(mae_inr / risk_inr, 3), "mfe_r": round(mfe_inr / risk_inr, 3),
        "bars_held": t.bars_held, "legs": len(t.legs), "detail": detail,
    }


def equity_from_trades(trades: pd.DataFrame, reference_capital: float) -> pd.Series:
    """Daily equity from realised rupee P&L (no compounding: one lot is one lot)."""
    if trades is None or trades.empty:
        return pd.Series(dtype=float)
    t = trades.sort_values("exit_ts")
    eq = reference_capital + t["net_pnl"].cumsum()
    eq.index = pd.DatetimeIndex(t["exit_ts"])
    return eq.groupby(eq.index.normalize()).last()


def daily_equity_on_calendar(daily: pd.Series, days: pd.DatetimeIndex, capital: float) -> pd.Series:
    """Reindex realised equity onto every trading day in the window (flat days included)."""
    days = pd.DatetimeIndex(sorted(set(days.normalize())))
    if daily.empty:
        return pd.Series(capital, index=days)
    s = daily.reindex(days.union(daily.index)).sort_index().ffill().fillna(capital)
    return s.reindex(days).astype(float)


def thirty_percent_rule(trades: pd.DataFrame) -> dict:
    """The operator's thesis: a 30% hit rate pays if winners are ~3x losers.

    Reports the achieved payoff ratio, the break-even win rate it implies
    (1 / (1 + payoff)), and the profit factor the SAME exits would produce at
    exactly a 30% win rate. If the last number is below 1.0, the stop/target
    geometry does not yet support the thesis regardless of signal quality.
    """
    if trades is None or trades.empty:
        return {}
    pnl = trades["net_pnl"].to_numpy(float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    if not len(wins) or not len(losses):
        return {"note": "needs both winners and losers"}
    avg_w, avg_l = float(wins.mean()), float(-losses.mean())
    payoff = avg_w / avg_l if avg_l > 0 else float("inf")
    be_wr = 1.0 / (1.0 + payoff) if math.isfinite(payoff) else 0.0
    wr = len(wins) / len(pnl)
    pf_at_30 = (0.30 * avg_w) / (0.70 * avg_l) if avg_l > 0 else float("inf")
    return {
        "win_rate": round(wr * 100, 1), "avg_win_inr": round(avg_w, 0), "avg_loss_inr": round(avg_l, 0),
        "payoff": round(payoff, 2), "breakeven_win_rate": round(be_wr * 100, 1),
        "edge_vs_breakeven_pct": round((wr - be_wr) * 100, 1),
        "pf_at_30pct_win_rate": round(pf_at_30, 2),
        "payoff_needed_for_30pct": round(0.70 / 0.30, 2),
        "verdict": ("exits support the 30% thesis" if pf_at_30 >= 1.1 else
                    "winners are not large enough relative to losers"),
    }
