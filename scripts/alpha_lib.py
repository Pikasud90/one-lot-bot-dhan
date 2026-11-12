"""Alpha search of 2026-11-12: shared helpers on top of the day-by-minute cache.

The cache (scripts/alpha_cache.py) holds, per instrument, matrices (sessions x 375) on the grid
09:15..15:29 IST. This module adds: valid sessions, previous close, research year and period,
the modelled option result of a same-day trade (the same Black-76 model, fills and charges
as scripts/mtf_bruteforce.py), and a day-clustered t-statistic.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402

OUT = h.ROOT / "data" / "research" / "alpha_2026-11-12"
CACHE = OUT / "cache"
GRID0 = 9 * 60 + 15
NMIN = 375
T0_ORD = h.T0.toordinal()
YEAR_ORDS = np.array([d.toordinal() for d in h.YEAR_STARTS])
PERIOD_OF_YEAR = np.array([0, 0, 0, 1, 2])  # TUNE, TUNE, TUNE, CHOOSE, TEST
PERIODS = ("TUNE", "CHOOSE", "TEST")
INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def M(hhmm: str) -> int:  # noqa: N802
    """Grid column of a clock time: M("09:45") == 30."""
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm) - GRID0


class Inst:
    """One instrument's cache with the derived per-session arrays."""

    def __init__(self, series: str) -> None:
        z = np.load(CACHE / f"{series}.npz")
        self.series = series
        self.day, self.o, self.nbars = z["day"], z["o"], z["nbars"]
        self.C, self.H, self.L, self.V = z["C"], z["H"], z["L"], z["V"]
        n = len(self.day)
        last = self.C[:, -1]
        self.pc = np.r_[np.nan, last[:-1]]  # previous session's last close
        self.full = (self.nbars >= 360) & np.isfinite(self.C[:, 0])
        self.valid = self.full & np.r_[False, self.full[:-1]] & np.isfinite(self.pc)
        y = np.searchsorted(YEAR_ORDS, self.day, side="right") - 1
        self.year = np.where((y >= 0) & (y < 5), y, -1)
        self.period = np.where(self.year >= 0, PERIOD_OF_YEAR[np.clip(self.year, 0, 4)], -1)
        self.n = n

    def date(self, i: int) -> dt.date:
        return dt.date.fromordinal(int(self.day[i]))


_OPT: dict[str, dict] = {}


def opt_params(series: str, inst: Inst) -> dict:
    """Per-session IV and monthly expiry, and the option settings, as the harness uses them."""
    if series in _OPT:
        return _OPT[series]
    path = CACHE / f"optparams_{series}.npz"
    if path.exists():
        z = np.load(path)
        P = {k: z[k] for k in z.files}  # noqa: N806
        P = {k: (v.item() if v.shape == () else v) for k, v in P.items()}  # noqa: N806
        _OPT[series] = P
        return P
    from onelot.lab.execution import OptionPricer, expiry_datetime, monthly_expiry

    if h._cache is None:
        h._init_worker()
    spec = h._cache.spec(f"{series}_OPT")
    bt = h._settings.backtest
    pricer = OptionPricer(bt, h._cache.vix())
    iv = np.empty(inst.n)
    exp_ns = np.empty(inst.n, dtype=np.int64)
    open_ns = np.empty(inst.n, dtype=np.int64)
    for i in range(inst.n):
        d = inst.date(i)
        ts = pd.Timestamp(dt.datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST))
        iv[i] = pricer.iv_for(ts)
        exp = monthly_expiry(d, bt.option_expiry_weekday, bt.option_min_days_to_expiry)
        exp_ns[i] = pd.Timestamp(expiry_datetime(exp)).tz_convert("UTC").as_unit("ns").value
        open_ns[i] = ts.tz_convert("UTC").as_unit("ns").value
    c = h.LegCost.from_config(h._cost_cfg.product("index_options"))
    P = {  # noqa: N806
        "iv": iv, "exp_ns": exp_ns, "open_ns": open_ns,
        "lot": float(spec.lot_size), "tick": float(spec.tick_size), "step": float(spec.strike_step),
        "r": float(bt.risk_free_rate), "delta": float(bt.option_target_delta),
        "half": float(bt.option_spread_pct),
        "slip": float(bt.option_slippage_ticks) * float(spec.tick_size),
        "c_flat": float(c.flat), "c_per_order": c.per_order, "c_pct": c.pct, "c_cap": c.cap,
        "c_stt_buy": c.stt_buy, "c_stt_sell": c.stt_sell, "c_fees": c.fees, "c_stamp": c.stamp,
        "c_gst": c.gst,
    }  # fmt: skip
    np.savez(path, **P)
    _OPT[series] = P
    return P


def _leg(P: dict, turnover: np.ndarray, sell: bool) -> np.ndarray:  # noqa: N803
    c = h.LegCost(
        bool(P["c_flat"]), P["c_per_order"], P["c_pct"], P["c_cap"], P["c_stt_buy"],
        P["c_stt_sell"], P["c_fees"], P["c_stamp"], P["c_gst"],
    )  # fmt: skip
    return c.leg(turnover, sell)


def opt_ret(
    inst: Inst, P: dict, side: int, d: np.ndarray, e: np.ndarray, x: np.ndarray,  # noqa: N803
    cost_mult: float = 1.0, spot_e: np.ndarray | None = None, spot_x: np.ndarray | None = None,
) -> dict:  # fmt: skip
    """Modelled result of buying one lot (call for side=+1, put for -1) on session rows `d`
    at grid minute `e` and selling at minute `x` the same day. Returns net % of the premium
    paid, rupees, and the frictionless premium change in %."""
    call = side == 1
    se = inst.C[d, e] if spot_e is None else spot_e
    sx = inst.C[d, x] if spot_x is None else spot_x
    ns_e = P["open_ns"][d] + e.astype(np.int64) * h.NS_MIN
    ns_x = P["open_ns"][d] + x.astype(np.int64) * h.NS_MIN
    t0 = np.maximum(P["exp_ns"][d] - ns_e, h.NS_MIN) / 1e9 / h.SECONDS_PER_YEAR
    t1 = np.maximum(P["exp_ns"][d] - ns_x, h.NS_MIN) / 1e9 / h.SECONDS_PER_YEAR
    iv, r, step = P["iv"][d], P["r"], P["step"]
    atm = np.round(se / step) * step
    disc = np.exp(-r * t0)
    v = iv * np.sqrt(t0)
    best, best_err = atm.copy(), np.full(len(d), 9.0)
    for i in range(-25, 26):
        k = atm + i * step
        with np.errstate(invalid="ignore", divide="ignore"):
            d1 = (np.log(se / k) + 0.5 * iv * iv * t0) / v
        delta = disc * h.ndtr(d1) if call else disc * h.ndtr(-d1)
        err = np.abs(delta - P["delta"])
        better = (err < best_err) & (k > 0)
        best = np.where(better, k, best)
        best_err = np.where(better, err, best_err)
    fair0 = h.b76(se, best, t0, iv, r, call)
    ef = h.tick_up(fair0 * (1 + P["half"]) + P["slip"], P["tick"])
    fair1 = h.b76(sx, best, t1, iv, r, call)
    xf = np.maximum(h.tick_down(fair1 * (1 - P["half"]) - P["slip"], P["tick"]), P["tick"])
    lot = P["lot"]
    gross = (xf - ef) * lot
    cost = _leg(P, ef * lot, False) + _leg(P, xf * lot, True)
    net = gross - cost_mult * cost
    return {
        "ret": net / (ef * lot) * 100.0, "inr": net, "raw": (fair1 - fair0) / fair0 * 100.0,
        "strike": best, "prem": ef, "cost_pct": cost / (ef * lot) * 100.0,
    }  # fmt: skip


def day_t(x: np.ndarray, day: np.ndarray) -> tuple[float, float, int]:
    """Mean of x per trade, and the t of the daily SUMS (all trades of one day are one draw,
    weighted by how many there were; this is the test of the total result being above zero)."""
    x, day = np.asarray(x, float), np.asarray(day)
    if len(x) == 0:
        return float("nan"), float("nan"), 0
    u, inv = np.unique(day, return_inverse=True)
    m = np.bincount(inv, x)
    n = len(m)
    sd = m.std(ddof=1) if n > 1 else float("nan")
    t = m.mean() / (sd / np.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    return float(x.mean()), float(t), n


def fmt(x: np.ndarray, day: np.ndarray, scale: float = 1.0, nd: int = 2) -> str:
    m, t, _ = day_t(x, day)
    return "      n/a      " if not np.isfinite(m) else f"{m * scale:+7.{nd}f} (t{t:+5.1f})"
