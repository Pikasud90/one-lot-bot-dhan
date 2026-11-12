"""Brute force of the owner's multi-timeframe Supertrend rule (2026-11-11).

The rule: Supertrend with period 14 and multiplier 1.7 on every timeframe. When the chosen
timeframes all point the same way, take one unit in that direction; the Supertrend line of one
of them is the stop and the trail, and it follows the market until the market comes back to it.

That sentence leaves choices open, so stage 1 tries every reading of it on one-minute bars:

* which timeframes must agree: every non-empty subset of 1m 5m 15m 30m 60m 1D (63);
* whose line is the stop and trail: each timeframe of the subset;
* how the line is acted on: `touch` (a resting stop at the line), `alert` (the desk alerts when
  the line was touched and the trade is closed at the next one-minute open - what a human with
  the alert on screen can do), `close` (exit only when that Supertrend flips);
* when to enter: `fresh` (the minute the alignment appears), `once` (first allowed minute of an
  alignment), `daily` (first allowed minute of an alignment in each session);
* how long to hold: `intraday` (flat before the close) or `carry` (hold until the line ends it;
  stocks long only, index options to the monthly expiry at the latest);
* long and short (index options: buy the call / buy the put; never a sold option).

5,760 structures per instrument, one position at a time per structure, costs from
`config/costs.yaml`, option premiums from the Lab's synthetic Black-76 model. The state of every
timeframe at a given minute comes from `onelot.indicators.mtf` (closed bars only).

    .venv/bin/python scripts/mtf_bruteforce.py --validate   # checks against the Lab's own code
    .venv/bin/python scripts/mtf_bruteforce.py --run        # stage 1, about 5 minutes
    .venv/bin/python scripts/mtf_bruteforce.py --report     # tables from the saved runs

Evidence about a rule on past data, after modelled costs; not advice, not a forecast.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "mtf_2026-11-11"

STOCKS = [
    "RELIANCE", "MM", "SBIN", "HDFCBANK", "ITC", "TMPV", "MARUTI", "ICICIBANK", "HEROMOTOCO",
    "INFY", "TCS", "AXISBANK", "KOTAKBANK", "LT", "BAJFINANCE", "BHARTIARTL", "TATASTEEL", "SUNPHARMA",
]  # fmt: skip
INDEX_OPTIONS = ["BANKNIFTY_OPT", "NIFTY_OPT", "FINNIFTY_OPT", "MIDCPNIFTY_OPT", "SENSEX_OPT"]
BASKETS = {"STOCKS": STOCKS, "INDEX_OPTIONS": INDEX_OPTIONS}

T0 = dt.date(2021, 11, 8)
T_END = dt.date(2026, 11, 7)
YEAR_STARTS = [dt.date(2021 + i, 11, 8) for i in range(6)]
YEAR_NAMES = ["Y1 21-22", "Y2 22-23", "Y3 23-24", "Y4 24-25", "Y5 25-26"]
PERIODS = {"TUNE": (0, 3), "CHOOSE": (3, 4), "TEST": (4, 5), "ALL": (0, 5)}
N_DAYS = (T_END - T0).days + 1

TFS = ("1m", "5m", "15m", "30m", "60m", "1d")
TOUCH, ALERT, CLOSE = 0, 1, 2
EXIT_MODES = ("touch", "alert", "close")
INTRADAY, CARRY = 0, 1
ENTRY_HORIZONS = (
    ("fresh", INTRADAY), ("once", INTRADAY), ("daily", INTRADAY), ("fresh", CARRY), ("once", CARRY),
)  # fmt: skip
EH_NAMES = ("intraday/fresh", "intraday/once", "intraday/daily", "carry/fresh", "carry/once")
BENCH_MINUTES = (9 * 60 + 30, 9 * 60 + 45, 10 * 60 + 15)

ENTRY_START_MIN = 9 * 60 + 30
ENTRY_END_MIN = 14 * 60 + 45
STOCK_EXIT_MIN = 15 * 60 + 10  # Dhan squares intraday equity off at 15:10
OPTION_EXIT_MIN = 15 * 60 + 15
STOCK_SLIPPAGE = 0.0003  # 0.03% a side, the Lab's value
DP_CHARGE_INR = 14.75  # per scrip per sell day out of the demat (Rs 12.50 + GST)
SECONDS_PER_YEAR = 365.0 * 24 * 3600
NS_MIN = 60 * 1_000_000_000

# aggregate fields per (structure, year)
F_N, F_SUM, F_SQ, F_WIN, F_RAW, F_HOLD, F_POS, F_NEG, F_CARRIED, F_SUM_DP, F_SQ_DP, F_INR, F_SESS, F_EVENT = range(14)  # fmt: skip
N_FIELDS = 14


# ───────────────────────────── structures ─────────────────────────────────


def structures(n_tfs: int = len(TFS)) -> np.ndarray:
    """[side, mask, trail timeframe, exit mode, entry/horizon] for every structure."""
    rows = []
    for side in (1, -1):
        for mask in range(1, 1 << n_tfs):
            for ti in range(n_tfs):
                if not mask >> ti & 1:
                    continue
                for xm in range(3):
                    for eh in range(len(ENTRY_HORIZONS)):
                        rows.append((side, mask, ti, xm, eh))
    return np.asarray(rows, dtype=np.int16)


def mask_name(mask: int, tfs: tuple[str, ...] = TFS) -> str:
    return "+".join(tf for i, tf in enumerate(tfs) if mask >> i & 1)


def structure_name(code: np.ndarray, tfs: tuple[str, ...] = TFS) -> str:
    side, mask, ti, xm, eh = (int(v) for v in code)
    return (
        f"{'LONG ' if side == 1 else 'SHORT'} [{mask_name(mask, tfs)}] trail {tfs[ti]} "
        f"{EXIT_MODES[xm]} {EH_NAMES[eh]}"
    )


# ───────────────────────────── costs (vectorised) ─────────────────────────


@dataclass(frozen=True)
class LegCost:
    """`onelot.costs.CostModel.leg(...).total` for arrays of turnover."""

    flat: bool
    per_order: float
    pct: float
    cap: float
    stt_buy: float
    stt_sell: float
    fees: float  # exchange + SEBI + IPFT, as a fraction of turnover
    stamp: float
    gst: float

    @classmethod
    def from_config(cls, cfg) -> LegCost:
        b, t = cfg.brokerage, cfg.taxes
        return cls(
            b.mode == "flat", float(b.per_order_inr), float(b.pct_of_turnover), float(b.cap_inr),
            float(t.stt_buy_pct), float(t.stt_sell_pct),
            float(t.exchange_txn_pct) + float(t.sebi_turnover_pct) + float(t.ipft_pct),
            float(t.stamp_duty_pct), float(t.gst_pct),
        )  # fmt: skip

    def leg(self, turnover: np.ndarray, sell: bool) -> np.ndarray:
        if self.flat:
            brokerage = np.full_like(turnover, self.per_order, dtype=float)
        else:
            brokerage = np.minimum(np.minimum(self.per_order, self.pct * turnover), self.cap)
        fees = self.fees * turnover
        total = brokerage + fees + self.gst * (brokerage + fees)
        total = total + (self.stt_sell if sell else self.stt_buy) * turnover
        if not sell:
            total = total + self.stamp * turnover
        return total


def tick_up(price: np.ndarray, tick: float) -> np.ndarray:
    return np.round(np.ceil(price / tick - 1e-9) * tick, 2)


def tick_down(price: np.ndarray, tick: float) -> np.ndarray:
    return np.round(np.floor(price / tick + 1e-9) * tick, 2)


def b76(f: np.ndarray, k: np.ndarray, t: np.ndarray, sigma: np.ndarray, r: float, call: bool):
    """Black-76 price, the array form of `onelot.analytics.blackscholes.price`."""
    disc = np.exp(-r * t)
    v = sigma * np.sqrt(t)
    d1 = (np.log(f / k) + 0.5 * sigma * sigma * t) / v
    d2 = d1 - v
    if call:
        return disc * (f * ndtr(d1) - k * ndtr(d2))
    return disc * (k * ndtr(-d2) - f * ndtr(-d1))


# ───────────────────────────── prepared instrument ────────────────────────


@dataclass
class Events:
    nxt: tuple[np.ndarray, np.ndarray, np.ndarray]  # next event index >= j, per exit mode
    px_touch: np.ndarray  # fill of a resting stop at bar j


@dataclass
class OptSide:
    strike: np.ndarray
    fair: np.ndarray
    entry_fill: np.ndarray


@dataclass
class Prep:
    inst: str
    is_option: bool
    lot: int
    tick: float
    tfs: tuple[str, ...]
    index: pd.DatetimeIndex
    ns: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    minute: np.ndarray
    sess: np.ndarray
    sess_dates: list[dt.date]
    sess_first: np.ndarray
    sess_last: np.ndarray
    xi: np.ndarray  # per session: bar of the forced exit
    lim: np.ndarray  # per session: a line event counts when its bar index < lim
    xp: np.ndarray  # per session: price of the forced exit
    xclose: np.ndarray  # per session: the forced exit is that bar's close (no 15:10 bar)
    year: np.ndarray
    dayoff: np.ndarray
    D: np.ndarray
    L: np.ndarray  # noqa: E741
    inwin: np.ndarray
    costs: dict[str, LegCost]
    # options
    iv: np.ndarray | None = None
    exp_ns: np.ndarray | None = None
    exp_sess: np.ndarray | None = None
    opt_cfg: dict | None = None
    vol: np.ndarray | None = None  # one-minute volume (stage 2 builds the entry-timeframe bars)
    ladder: dict | None = None  # timeframe -> onelot.indicators.mtf.TimeframeTrend
    _opt: dict[int, OptSide] = field(default_factory=dict)
    _events: dict[tuple[int, int], Events] = field(default_factory=dict)
    _dside: dict[int, np.ndarray] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.o)

    def dside(self, side: int) -> np.ndarray:
        if side not in self._dside:
            self._dside[side] = self.D == side
        return self._dside[side]

    def events(self, ti: int, side: int) -> Events:
        key = (ti, side)
        if key not in self._events:
            n = self.n
            line = self.L[ti]
            flip = self.D[ti] != side
            with np.errstate(invalid="ignore"):
                touch = (self.l <= line) if side == 1 else (self.h >= line)
            prev_touch = np.r_[False, touch[:-1]]
            stop_px = np.minimum(self.o, line) if side == 1 else np.maximum(self.o, line)
            self._events[key] = Events(
                (_next_true(flip | touch, n), _next_true(flip | prev_touch, n), _next_true(flip, n)),
                np.where(flip, self.o, stop_px),
            )  # fmt: skip
        return self._events[key]

    def opt(self, side: int) -> OptSide:
        if side not in self._opt:
            self._opt[side] = _option_side(self, side)
        return self._opt[side]

    def drop_caches(self) -> None:
        self._events.clear()


def _next_true(ev: np.ndarray, n: int) -> np.ndarray:
    idx = np.where(ev, np.arange(n, dtype=np.int32), np.int32(n))
    out = np.empty(n + 1, dtype=np.int32)
    out[:n] = np.minimum.accumulate(idx[::-1])[::-1]
    out[n] = n
    return out


def _option_side(p: Prep, side: int) -> OptSide:
    """Strike nearest the target delta, fair premium and entry fill for an entry at each bar open."""
    cfg = p.opt_cfg
    call = side == 1
    r, target, step = cfg["r"], cfg["delta"], cfg["step"]
    spot = p.o
    t0 = np.maximum(p.exp_ns[p.sess] - p.ns, NS_MIN) / 1e9 / SECONDS_PER_YEAR
    iv = p.iv[p.sess]
    atm = np.round(spot / step) * step
    disc = np.exp(-r * t0)
    v = iv * np.sqrt(t0)
    half_var = 0.5 * iv * iv * t0
    best = atm.copy()
    best_err = np.full(p.n, 9.0)
    for i in range(-25, 26):
        k = atm + i * step
        with np.errstate(invalid="ignore", divide="ignore"):
            d1 = (np.log(spot / k) + half_var) / v
        delta = disc * ndtr(d1) if call else disc * ndtr(-d1)
        err = np.abs(delta - target)
        better = (err < best_err) & (k > 0)
        best = np.where(better, k, best)
        best_err = np.where(better, err, best_err)
    fair = b76(spot, best, t0, iv, r, call)
    fill = tick_up(fair * (1 + cfg["half"]) + cfg["slip"], p.tick)
    return OptSide(best, fair, fill)


_cache = None
_settings = None
_cost_cfg = None


def _init_worker() -> None:
    global _cache, _settings, _cost_cfg
    logging.disable(logging.WARNING)
    sys.path.insert(0, str(ROOT / "src"))
    from onelot.config import CostConfig
    from onelot.lab.runner import DataCache
    from onelot.settings import current_settings

    _cache = DataCache()
    _settings = current_settings()
    _cost_cfg = CostConfig.load(_settings.project_root / "config" / "costs.yaml")


def prepare(
    inst: str,
    tfs: tuple[str, ...] = TFS,
    period: int | None = None,
    mult: float | None = None,
    iv_lag: str = "same",
) -> Prep:
    """Everything a structure needs about one instrument, as arrays over its one-minute bars."""
    from onelot.indicators import mtf
    from onelot.lab.execution import OptionPricer, expiry_datetime, monthly_expiry

    if _cache is None:
        _init_worker()
    spec = _cache.spec(inst)
    series = spec.series_key
    bars = _cache.bars(series, "1m")
    end = pd.Timestamp(T_END + dt.timedelta(days=1), tz=bars.index.tz)
    bars = bars[bars.index < end]
    daily = _cache.bars(series, "1d")
    daily = daily[daily.index < end]
    ladder = mtf.build_ladder(
        bars, daily, tfs, mtf.ST_PERIOD if period is None else period,
        mtf.ST_MULT if mult is None else mult,
    )  # fmt: skip
    ns = mtf.index_ns(bars.index)
    n = len(bars)
    D = np.zeros((len(tfs), n), dtype=np.int8)
    L = np.full((len(tfs), n), np.nan)  # noqa: N806
    for i, tf in enumerate(tfs):
        D[i], L[i] = ladder[tf].at(ns)

    idx = bars.index
    minute = (idx.hour * 60 + idx.minute).to_numpy()
    dates = np.asarray(idx.date, dtype=object)
    change = np.r_[True, dates[1:] != dates[:-1]]
    sess = np.cumsum(change) - 1
    sess_first = np.flatnonzero(change)
    sess_last = np.r_[sess_first[1:] - 1, n - 1]
    sess_dates = [dates[i] for i in sess_first]
    n_sess = len(sess_first)

    exit_min = OPTION_EXIT_MIN if spec.is_option else STOCK_EXIT_MIN
    o, h, low, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    xi = np.empty(n_sess, dtype=np.int64)
    lim = np.empty(n_sess, dtype=np.int64)
    xp = np.empty(n_sess)
    xclose = np.zeros(n_sess, dtype=bool)
    for s in range(n_sess):
        a, b = sess_first[s], sess_last[s]
        late = np.flatnonzero(minute[a : b + 1] >= exit_min)
        if len(late) and minute[a] < exit_min:
            xi[s] = lim[s] = a + late[0]
            xp[s] = o[xi[s]]
        else:  # a short or evening session: out at its last close
            xi[s], lim[s], xp[s], xclose[s] = b, b + 1, c[b], True

    day_ord = np.array([d.toordinal() for d in sess_dates])[sess]
    dayoff = day_ord - T0.toordinal()
    year = np.searchsorted([d.toordinal() for d in YEAR_STARTS], day_ord, side="right") - 1
    year = np.where((year >= 0) & (year < 5), year, -1)
    inwin = (minute >= ENTRY_START_MIN) & (minute <= ENTRY_END_MIN) & (year >= 0)

    costs = {
        "intraday": LegCost.from_config(_cost_cfg.product("equity_intraday")),
        "delivery": LegCost.from_config(_cost_cfg.product("equity_delivery")),
        "options": LegCost.from_config(_cost_cfg.product("index_options")),
    }
    p = Prep(
        inst, spec.is_option, int(spec.lot_size), float(spec.tick_size), tuple(tfs), idx, ns,
        o, h, low, c, minute, sess, sess_dates, sess_first, sess_last, xi, lim, xp, xclose,
        year, dayoff, D, L, inwin, costs,
    )  # fmt: skip
    p.vol = bars["volume"].to_numpy(float) if "volume" in bars.columns else np.zeros(n)
    p.ladder = ladder
    if spec.is_option:
        bt = _settings.backtest
        pricer = OptionPricer(bt, _cache.vix())
        iv = np.empty(n_sess)
        exp_ns = np.empty(n_sess, dtype=np.int64)
        exp_sess = np.empty(n_sess, dtype=np.int64)
        ords = np.array([d.toordinal() for d in sess_dates])
        for s in range(n_sess):
            ts = idx[sess_first[s]]
            iv[s] = pricer.iv_for(ts if iv_lag == "same" else ts.normalize() - pd.Timedelta(days=1))
            exp = monthly_expiry(
                sess_dates[s], bt.option_expiry_weekday, bt.option_min_days_to_expiry
            )
            exp_ns[s] = pd.Timestamp(expiry_datetime(exp)).tz_convert("UTC").as_unit("ns").value
            exp_sess[s] = max(int(np.searchsorted(ords, exp.toordinal(), side="right")) - 1, s)
        p.iv, p.exp_ns, p.exp_sess = iv, exp_ns, exp_sess
        p.opt_cfg = {
            "r": float(bt.risk_free_rate), "delta": float(bt.option_target_delta),
            "step": float(spec.strike_step), "half": float(bt.option_spread_pct),
            "slip": float(bt.option_slippage_ticks) * float(spec.tick_size),
        }  # fmt: skip
    return p


# ───────────────────────────── one structure ──────────────────────────────


def aligned(p: Prep, side: int, mask: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(all listed timeframes agree, first bar of each such episode, episode number)."""
    ds = p.dside(side)
    al = None
    for i in range(len(p.tfs)):
        if mask >> i & 1:
            al = ds[i].copy() if al is None else al & ds[i]
    start = al & ~np.r_[False, al[:-1]]
    return al, start, np.cumsum(start)


def candidates(
    p: Prep,
    side: int,
    al: np.ndarray,
    start: np.ndarray,
    ep: np.ndarray,
    ti: int,
    extra: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Bars at whose open an entry may be taken, per entry mode."""
    with np.errstate(invalid="ignore"):
        ok = al & p.inwin & (side * (p.o - p.L[ti]) > 0)
    if extra is not None:
        ok &= extra
    idx = np.flatnonzero(ok)
    if not len(idx):
        return {"fresh": idx, "once": idx, "daily": idx}
    epi, si = ep[idx], p.sess[idx]
    new_ep = np.r_[True, epi[1:] != epi[:-1]]
    new_day = np.r_[True, si[1:] != si[:-1]]
    return {"fresh": idx[start[idx]], "once": idx[new_ep], "daily": idx[new_ep | new_day]}


def exits(p: Prep, side: int, ti: int, xm: int, horizon: int, E: np.ndarray):  # noqa: N803
    """Exit bar, exit price on the underlying, exit time and 'ended by the line' per entry."""
    ev = p.events(ti, side)
    n = p.n
    x = ev.nxt[xm][np.minimum(E + (0 if xm == TOUCH else 1), n)].astype(np.int64)
    s = p.sess[E]
    ls = s if horizon == INTRADAY else (p.exp_sess[s] if p.is_option else None)
    xs = np.minimum(x, n - 1)
    px_event = ev.px_touch[xs] if xm == TOUCH else p.o[xs]
    if ls is None:
        hit = x < n
        X = np.where(hit, x, n - 1)  # noqa: N806
        PX = np.where(hit, px_event, p.c[n - 1])  # noqa: N806
        XT = p.ns[X] + np.where(hit, 0, NS_MIN)  # noqa: N806
    else:
        hit = x < p.lim[ls]
        X = np.where(hit, x, p.xi[ls])  # noqa: N806
        PX = np.where(hit, px_event, p.xp[ls])  # noqa: N806
        XT = p.ns[X] + np.where(hit | ~p.xclose[ls], 0, NS_MIN)  # noqa: N806
    return X, PX, XT, hit


def greedy(E: np.ndarray, X: np.ndarray) -> np.ndarray:  # noqa: N803
    """One position at a time: keep an entry only when the previous trade has ended."""
    n = len(E)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    jump = np.searchsorted(E, X, side="right").tolist()
    keep = []
    i = 0
    while i < n:
        keep.append(i)
        i = jump[i]
    return np.asarray(keep, dtype=np.int64)


def pnl(p: Prep, side: int, horizon: int, E, X, PX, XT, cost_mult: float = 1.0) -> dict:  # noqa: N803
    """Net result of each trade: % of the money put in, rupees, and the frictionless move."""
    same = p.sess[E] == p.sess[X]
    if p.is_option:
        os_ = p.opt(side)
        k, ef, fair0 = os_.strike[E], os_.entry_fill[E], os_.fair[E]
        cfg = p.opt_cfg
        t = np.maximum(p.exp_ns[p.sess[E]] - XT, NS_MIN) / 1e9 / SECONDS_PER_YEAR
        fair = b76(PX, k, t, p.iv[p.sess[X]], cfg["r"], side == 1)
        fill = np.maximum(tick_down(fair * (1 - cfg["half"]) - cfg["slip"], p.tick), p.tick)
        gross = (fill - ef) * p.lot
        oc = p.costs["options"]
        cost = oc.leg(ef * p.lot, sell=False) + oc.leg(fill * p.lot, sell=True)
        net = gross - cost_mult * cost
        base = ef * p.lot
        raw = (fair - fair0) / fair0 * 100.0
        dp = np.zeros(len(E))
    else:
        ef = p.o[E] * (1 + side * STOCK_SLIPPAGE)
        xf = PX * (1 - side * STOCK_SLIPPAGE)
        gross = side * (xf - ef)
        ic = p.costs["intraday"]
        cost = ic.leg(ef, sell=side == -1) + ic.leg(xf, sell=side == 1)
        dp = np.zeros(len(E))
        if horizon == CARRY:
            dc = p.costs["delivery"]
            cost = np.where(same, cost, dc.leg(ef, sell=False) + dc.leg(xf, sell=True))
            dp = np.where(same, 0.0, DP_CHARGE_INR / ef * 100.0)
        net = gross - cost_mult * cost
        base = ef
        raw = side * (PX - p.o[E]) / p.o[E] * 100.0
    return {"ret": net / base * 100.0, "inr": net, "raw": raw, "dp": dp, "carried": ~same}


def run_structure(
    p: Prep, code, extra: np.ndarray | None = None, delay: int = 0, cost_mult: float = 1.0
) -> dict:
    """The trades of one structure on one instrument."""
    side, mask, ti, xm, eh = (int(v) for v in code)
    mode, horizon = ENTRY_HORIZONS[eh]
    if horizon == CARRY and side == -1 and not p.is_option:
        return _no_trades()
    al, start, ep = aligned(p, side, mask)
    E = candidates(p, side, al, start, ep, ti, extra)[mode]  # noqa: N806
    return trades_from(p, side, ti, xm, horizon, E, delay, cost_mult)


def _no_trades() -> dict:
    z = np.zeros(0)
    zi = np.zeros(0, dtype=np.int64)
    return {"E": zi, "X": zi, "PX": z, "XT": zi, "hit": z.astype(bool), "ret": z, "inr": z,
            "raw": z, "dp": z, "carried": z.astype(bool)}  # fmt: skip


def trades_from(p: Prep, side, ti, xm, horizon, E, delay: int = 0, cost_mult: float = 1.0) -> dict:  # noqa: N803
    if delay and len(E):
        late = E + delay
        ok = late < p.n
        late, E = late[ok], E[ok]  # noqa: N806
        with np.errstate(invalid="ignore"):
            ok = (p.sess[late] == p.sess[E]) & (side * (p.o[late] - p.L[ti][late]) > 0)
            ok &= p.D[ti][late] == side
        E = late[ok]  # noqa: N806
    if not len(E):
        return _no_trades()
    X, PX, XT, hit = exits(p, side, ti, xm, horizon, E)  # noqa: N806
    keep = greedy(E, X)
    E, X, PX, XT, hit = E[keep], X[keep], PX[keep], XT[keep], hit[keep]  # noqa: N806
    out = pnl(p, side, horizon, E, X, PX, XT, cost_mult)
    out.update({"E": E, "X": X, "PX": PX, "XT": XT, "hit": hit})
    return out


def accumulate(p: Prep, tr: dict, agg: np.ndarray, daysum: np.ndarray, daycnt: np.ndarray) -> None:
    E = tr["E"]  # noqa: N806
    if not len(E):
        return
    y = p.year[E]
    ret = tr["ret"]
    ret_dp = ret - tr["dp"]

    def by_year(w: np.ndarray | None = None) -> np.ndarray:
        return np.bincount(y, weights=w, minlength=5)[:5]

    agg[:, F_N] = by_year()
    agg[:, F_SUM] = by_year(ret)
    agg[:, F_SQ] = by_year(ret * ret)
    agg[:, F_WIN] = by_year((ret > 0).astype(float))
    agg[:, F_RAW] = by_year(tr["raw"])
    agg[:, F_HOLD] = by_year((tr["XT"] - p.ns[E]) / NS_MIN)
    agg[:, F_POS] = by_year(np.where(ret > 0, ret, 0.0))
    agg[:, F_NEG] = by_year(np.where(ret < 0, ret, 0.0))
    agg[:, F_CARRIED] = by_year(tr["carried"].astype(float))
    agg[:, F_SUM_DP] = by_year(ret_dp)
    agg[:, F_SQ_DP] = by_year(ret_dp * ret_dp)
    agg[:, F_INR] = by_year(tr["inr"])
    agg[:, F_SESS] = by_year((p.sess[tr["X"]] - p.sess[E]).astype(float))
    agg[:, F_EVENT] = by_year(tr["hit"].astype(float))
    d = p.dayoff[E]
    daysum[:] = np.bincount(d, weights=ret, minlength=N_DAYS)[:N_DAYS]
    daycnt[:] = np.bincount(d, minlength=N_DAYS)[:N_DAYS]


def benchmark_entries(p: Prep, minute: int) -> np.ndarray:
    """One entry a session: the first bar at or after `minute` (inside the entry window)."""
    ok = p.inwin & (p.minute >= minute)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return idx
    si = p.sess[idx]
    return idx[np.r_[True, si[1:] != si[:-1]]]


def benchmark_trades(p: Prep, side: int, minute: int) -> dict:
    """Enter every session at `minute` and hold to the session exit: no signal at all."""
    E = benchmark_entries(p, minute)  # noqa: N806
    s = p.sess[E]
    X, PX = p.xi[s], p.xp[s]  # noqa: N806
    XT = p.ns[X] + np.where(p.xclose[s], NS_MIN, 0)  # noqa: N806
    out = pnl(p, side, INTRADAY, E, X, PX, XT)
    out.update({"E": E, "X": X, "PX": PX, "XT": XT, "hit": np.zeros(len(E), dtype=bool)})
    return out


def grid(p: Prep, codes: np.ndarray) -> dict[str, np.ndarray]:
    """Every structure on one instrument."""
    n_s = len(codes)
    n_b = 2 * len(BENCH_MINUTES)
    agg = np.zeros((n_s + n_b, 5, N_FIELDS))
    daysum = np.zeros((n_s + n_b, N_DAYS), dtype=np.float32)
    daycnt = np.zeros((n_s + n_b, N_DAYS), dtype=np.uint16)
    row = {tuple(int(v) for v in c): i for i, c in enumerate(codes)}
    for side in (1, -1):
        for mask in range(1, 1 << len(p.tfs)):
            al, start, ep = aligned(p, side, mask)
            for ti in range(len(p.tfs)):
                if not mask >> ti & 1:
                    continue
                cands = candidates(p, side, al, start, ep, ti)
                for xm in range(3):
                    for eh, (mode, horizon) in enumerate(ENTRY_HORIZONS):
                        if horizon == CARRY and side == -1 and not p.is_option:
                            continue
                        tr = trades_from(p, side, ti, xm, horizon, cands[mode])
                        i = row[(side, mask, ti, xm, eh)]
                        accumulate(p, tr, agg[i], daysum[i], daycnt[i])
    b = n_s
    for side in (1, -1):
        for minute in BENCH_MINUTES:
            accumulate(p, benchmark_trades(p, side, minute), agg[b], daysum[b], daycnt[b])
            b += 1
    # the underlying itself, per research year: first open, last close, sessions
    hold = np.zeros((5, 3))
    for yr in range(5):
        j = np.flatnonzero(p.year == yr)
        if len(j):
            hold[yr] = p.o[j[0]], p.c[j[-1]], len(np.unique(p.sess[j]))
    return {"agg": agg, "daysum": daysum, "daycnt": daycnt, "hold": hold}


def _run_instrument(task: tuple) -> tuple:
    inst, out_dir = task
    t0 = time.time()
    p = prepare(inst)
    codes = structures(len(p.tfs))
    res = grid(p, codes)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / f"{inst}.npz", codes=codes, **res)
    n_tr = int(res["agg"][: len(codes), :, F_N].sum())
    return inst, round(time.time() - t0, 1), n_tr


def run(workers: int, only: list[str] | None, out_dir: Path) -> None:
    insts = [i for b in BASKETS.values() for i in b if not only or i in only]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
        futs = [pool.submit(_run_instrument, (i, str(out_dir))) for i in insts]
        for k, fut in enumerate(as_completed(futs), 1):
            inst, secs, n_tr = fut.result()
            print(f"[{k}/{len(insts)}] {inst:<16} {secs:>6.1f}s  {n_tr:>10,} trades "
                  f"(elapsed {time.time() - t0:.0f}s)", flush=True)  # fmt: skip
    print(f"total {time.time() - t0:.0f}s -> {out_dir}", flush=True)


# ───────────────────────────── validation ─────────────────────────────────


def reference(p: Prep, code, extra: np.ndarray | None = None) -> list[tuple]:
    """The same structure as a plain bar-by-bar loop: the slow, obvious implementation."""
    side, mask, ti, xm, eh = (int(v) for v in code)
    mode, horizon = ENTRY_HORIZONS[eh]
    if horizon == CARRY and side == -1 and not p.is_option:
        return []
    members = [i for i in range(len(p.tfs)) if mask >> i & 1]
    n = p.n
    o, h, low, c = p.o.tolist(), p.h.tolist(), p.l.tolist(), p.c.tolist()
    line_all = p.L[ti].tolist()
    d_t = p.D[ti].tolist()
    d_m = [p.D[i].tolist() for i in members]
    sess = p.sess.tolist()
    inwin = p.inwin.tolist()
    ex = extra.tolist() if extra is not None else None
    xi, xclose = p.xi.tolist(), p.xclose.tolist()
    exp_sess = p.exp_sess.tolist() if p.is_option else None
    out: list[tuple] = []
    pos = -1
    prev_al = False
    episode = 0
    last_key = None
    prev_touched = False
    for j in range(n):
        al = all(d[j] == side for d in d_m)
        if al and not prev_al:
            episode += 1
        start = al and not prev_al
        prev_al = al
        line = line_all[j]
        flip = d_t[j] != side
        touched = (low[j] <= line) if side == 1 else (h[j] >= line)
        was_open = pos >= 0
        if not was_open:
            elig = al and inwin[j] and side * (o[j] - line) > 0 and (ex is None or ex[j])
            if mode == "fresh":
                cand = elig and start
            else:
                key = episode if mode == "once" else (episode, sess[j])
                cand = elig and key != last_key
                if elig:
                    last_key = key
            if cand:
                pos = j
        elif mode != "fresh":
            # an allowed minute passes while a trade is open: this episode has had its entry
            elig = al and inwin[j] and side * (o[j] - line) > 0 and (ex is None or ex[j])
            if elig:
                last_key = episode if mode == "once" else (episode, sess[j])
        if pos >= 0:
            e = pos
            ls = sess[e] if horizon == INTRADAY else (exp_sess[sess[e]] if p.is_option else None)
            px = None
            if ls is not None and not xclose[ls] and j == xi[ls]:
                px = o[j]
            else:
                if xm == TOUCH:
                    if flip:
                        px = o[j]
                    elif touched:
                        px = min(o[j], line) if side == 1 else max(o[j], line)
                elif xm == ALERT:
                    if j > e and (flip or prev_touched):
                        px = o[j]
                elif j > e and flip:
                    px = o[j]
                if px is None and ((ls is not None and xclose[ls] and j == xi[ls]) or j == n - 1):
                    px = c[j]
            if px is not None:
                out.append((e, j, px))
                pos = -1
        prev_touched = touched
    return out


def validate() -> int:
    """Check every vectorised shortcut against the Lab's own code or a plain loop."""
    from onelot.costs import CostModel
    from onelot.lab.execution import OptionPricer, execute_trade
    from onelot.lab.simulator import Leg, SimTrade

    _init_worker()
    rng = np.random.default_rng(11)
    bad = 0

    # 1. charges
    for name in ("equity_intraday", "equity_delivery", "index_options"):
        cfg = _cost_cfg.product(name)
        model, fast = CostModel(cfg), LegCost.from_config(cfg)
        turn = np.r_[rng.uniform(50, 30000, 200), rng.uniform(30000, 3e6, 200)]
        for sell in (False, True):
            want = np.array([model.leg(float(t), 1, "sell" if sell else "buy").total for t in turn])
            worst = float(np.abs(want - fast.leg(turn, sell)).max())
            bad += worst > 1e-9
            print(f"charges {name:<16} {'sell' if sell else 'buy ':<4} max diff Rs {worst:.2e}")

    codes = structures()
    for inst, n_ref in (("SBIN", 60), ("BANKNIFTY_OPT", 24)):
        p = prepare(inst)
        spec = _cache.spec(inst)
        print(f"{inst}: {p.n:,} one-minute bars, {len(p.sess_dates)} sessions")
        pick = rng.choice(len(codes), size=n_ref, replace=False)
        # make sure the owner's exact rule and both horizons are among the checked structures
        full = (1 << len(TFS)) - 1
        forced = [i for i, cd in enumerate(codes) if cd[1] == full and cd[2] in (0, 3)][:30]
        t0 = time.time()
        n_tr = 0
        for i in list(pick) + forced:
            tr = run_structure(p, codes[i])
            ref = reference(p, codes[i])
            got = list(zip(tr["E"].tolist(), tr["X"].tolist(), tr["PX"].tolist(), strict=True))
            n_tr += len(ref)
            if len(got) != len(ref) or any(
                a[0] != b[0] or a[1] != b[1] or abs(a[2] - b[2]) > 1e-9
                for a, b in zip(got, ref, strict=True)
            ):
                bad += 1
                print(f"  MISMATCH {structure_name(codes[i])}: fast {len(got)} vs loop {len(ref)}")
        print(f"  {len(pick) + len(forced)} structures, {n_tr:,} trades: fast == loop "
              f"({time.time() - t0:.0f}s)")  # fmt: skip

        # 2. rupees of sampled trades against the Lab's execute_trade
        bt = _settings.backtest
        pricer = OptionPricer(bt, _cache.vix()) if spec.is_option else None
        worst_inr = worst_ret = 0.0
        checked = 0
        for i in rng.choice(len(codes), size=40, replace=False):
            code = codes[i]
            side, horizon = int(code[0]), ENTRY_HORIZONS[int(code[4])][1]
            tr = run_structure(p, code)
            if not len(tr["E"]):
                continue
            for j in rng.choice(len(tr["E"]), size=min(15, len(tr["E"])), replace=False):
                e = int(tr["E"][j])
                carried = bool(tr["carried"][j])
                product = spec.product if spec.is_option else (
                    "equity_delivery" if carried and horizon == CARRY else "equity_intraday"
                )  # fmt: skip
                model = CostModel(_cost_cfg.product(product))
                if spec.is_option:
                    entry_px, exit_px = float(p.o[e]), float(tr["PX"][j])
                else:
                    entry_px = float(p.o[e] * (1 + side * STOCK_SLIPPAGE))
                    exit_px = float(tr["PX"][j] * (1 - side * STOCK_SLIPPAGE))
                st = SimTrade(inst, "mtf", side, p.index[e], p.index[e], entry_px,
                              entry_px * (1 - side * 0.01), entry_px * 0.01, None, 0.0, 1.0)  # fmt: skip
                st.legs.append(Leg(pd.Timestamp(int(tr["XT"][j]), tz="UTC").tz_convert(p.index.tz),
                                   exit_px, 1.0, "x"))  # fmt: skip
                rowd = execute_trade(st, spec, model, bt, bt.slippage_pct, pricer)
                worst_inr = max(worst_inr, abs(rowd["net_pnl"] - float(tr["inr"][j])))
                worst_ret = max(worst_ret, abs(rowd["ret_pct"] - float(tr["ret"][j])))
                checked += 1
        ok = worst_inr <= 0.0051 and worst_ret <= 0.00051
        bad += not ok
        print(f"  {checked} trades against execute_trade: max diff Rs {worst_inr:.4f}, "
              f"{worst_ret:.5f}% {'ok' if ok else 'MISMATCH'}")  # fmt: skip
    print("validation:", "PASS" if not bad else f"FAIL ({bad})")
    return int(bad > 0)


# ───────────────────────────── report ─────────────────────────────────────

YEAR_OFF = [(d - T0).days for d in YEAR_STARTS]
MONTH_ID = np.array(
    [(T0 + dt.timedelta(days=i)).year * 12 + (T0 + dt.timedelta(days=i)).month for i in range(N_DAYS)]
)  # fmt: skip
#: pre-declared minimum trades in (TUNE, CHOOSE) for a structure to be eligible
MIN_TRADES = {"STOCKS": (150, 40), "INDEX_OPTIONS": (60, 20)}
BENCH_NAMES = [
    f"{'LONG ' if s == 1 else 'SHORT'} every session at {m // 60:02d}:{m % 60:02d}, out at the close"
    for s in (1, -1)
    for m in BENCH_MINUTES
]


@dataclass
class Basket:
    name: str
    insts: list[str]
    codes: np.ndarray
    inst_agg: np.ndarray  # [instrument, row, year, field]
    daysum: np.ndarray  # [row, day], summed over the instruments
    daycnt: np.ndarray
    hold: np.ndarray  # [instrument, year, (first open, last close, sessions)]

    @property
    def agg(self) -> np.ndarray:
        return self.inst_agg.sum(axis=0)

    @property
    def n_struct(self) -> int:
        return len(self.codes)


def load_basket(name: str, out_dir: Path) -> Basket:
    insts, aggs, holds = [], [], []
    daysum = daycnt = codes = None
    for inst in BASKETS[name]:
        f = out_dir / f"{inst}.npz"
        if not f.exists():
            continue
        z = np.load(f)
        codes = z["codes"]
        insts.append(inst)
        aggs.append(z["agg"])
        holds.append(z["hold"])
        ds, dc = z["daysum"].astype(np.float64), z["daycnt"].astype(np.float64)
        daysum = ds if daysum is None else daysum + ds
        daycnt = dc if daycnt is None else daycnt + dc
    if not insts:
        raise SystemExit(f"no saved runs for {name} in {out_dir}")
    return Basket(name, insts, codes, np.stack(aggs), daysum, daycnt, np.stack(holds))


def clustered_t(S: np.ndarray, C: np.ndarray, lo: int, hi: int, by_month: bool = False):  # noqa: N803
    """t of the mean return per trade, trades entered on one day (or month) as one cluster."""
    s, c = S[:, lo:hi], C[:, lo:hi]
    if by_month:
        mid = MONTH_ID[lo:hi]
        starts = np.flatnonzero(np.r_[True, mid[1:] != mid[:-1]])
        s, c = np.add.reduceat(s, starts, axis=1), np.add.reduceat(c, starts, axis=1)
    n = c.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        m = s.sum(axis=1) / n
        resid = s - m[:, None] * c
        g = (c > 0).sum(axis=1)
        var = (resid * resid).sum(axis=1) / (n * n) * g / np.maximum(g - 1, 1)
        t = m / np.sqrt(var)
    return np.where(np.isfinite(t), t, 0.0)


def period_stats(b: Basket) -> dict[str, dict[str, np.ndarray]]:
    """Per period and row: trades, mean net % per trade, clustered t and the rest."""
    agg = b.agg
    out: dict[str, dict[str, np.ndarray]] = {}
    for pname, (y0, y1) in PERIODS.items():
        a = agg[:, y0:y1].sum(axis=1)
        n = a[:, F_N]
        lo, hi = YEAR_OFF[y0], YEAR_OFF[y1]
        with np.errstate(divide="ignore", invalid="ignore"):
            mean_dp = a[:, F_SUM_DP] / n
            sd_dp = np.sqrt(np.maximum(a[:, F_SQ_DP] / n - mean_dp**2, 0.0))
            out[pname] = {
                "n": n,
                "mean": a[:, F_SUM] / n,
                "t": clustered_t(b.daysum, b.daycnt, lo, hi),
                "tm": clustered_t(b.daysum, b.daycnt, lo, hi, by_month=True),
                "win": a[:, F_WIN] / n * 100.0,
                "raw": a[:, F_RAW] / n,
                "hold": a[:, F_HOLD] / n,
                "sess": a[:, F_SESS] / n,
                "pf": a[:, F_POS] / -a[:, F_NEG],
                "inr": a[:, F_INR],
                "event": a[:, F_EVENT] / n * 100.0,
                "carried": a[:, F_CARRIED] / n * 100.0,
                "mean_dp": mean_dp,
                "t_dp": mean_dp / (sd_dp / np.sqrt(n)),
            }
    return out


def eligibility(b: Basket, st: dict) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    """The pre-declared rule (NOTES.md): eligible rows and their score, without the TEST year."""
    n_tune, n_choose = MIN_TRADES[b.name]
    agg = b.agg
    with np.errstate(divide="ignore", invalid="ignore"):
        year_mean = agg[:, :3, F_SUM] / agg[:, :3, F_N]
    years_pos = (np.nan_to_num(year_mean, nan=-1.0) > 0).sum(axis=1)
    inst_tc = b.inst_agg[:, :, :4, F_SUM].sum(axis=2)  # [instrument, row] over TUNE + CHOOSE
    inst_pos = (inst_tc > 0).sum(axis=0)
    need = (len(b.insts) + 1) // 2
    checks = {
        "enough trades": (st["TUNE"]["n"] >= n_tune) & (st["CHOOSE"]["n"] >= n_choose),
        "TUNE mean > 0": np.nan_to_num(st["TUNE"]["mean"], nan=-1.0) > 0,
        "CHOOSE mean > 0": np.nan_to_num(st["CHOOSE"]["mean"], nan=-1.0) > 0,
        "2 of 3 TUNE years > 0": years_pos >= 2,
        f"{need} of {len(b.insts)} instruments > 0": inst_pos >= need,
    }
    ok = np.ones(len(agg), dtype=bool)
    for v in checks.values():
        ok &= v
    ok[b.n_struct :] = False  # benchmarks are not structures
    score = np.minimum(st["TUNE"]["t"], st["CHOOSE"]["t"])
    return ok, score, checks


def _hold_text(st: dict, i: int) -> str:
    minutes, sessions = st["ALL"]["hold"][i], st["ALL"]["sess"][i]
    if not np.isfinite(minutes):
        return "      -"
    if sessions >= 0.5:
        return f"{sessions:>5.1f} d"
    return f"{minutes:>5.0f} m"


HEAD = (
    f"{'structure':<58} | {'TUNE n':>7} {'mean%':>7} {'t':>5} | {'CHOOSE':>6} {'mean%':>7} {'t':>5} | "
    f"{'TEST n':>6} {'mean%':>7} {'t':>5} | {'ALL n':>7} {'mean%':>7} {'t':>5} {'win%':>5} "
    f"{'gross%':>7} {'hold':>7} {'net Rs':>11}"
)  # fmt: skip


def row_text(name: str, st: dict, i: int) -> str:
    parts = [f"{name:<58}"]
    for pn in ("TUNE", "CHOOSE", "TEST", "ALL"):
        s = st[pn]
        n = int(s["n"][i])
        w = 7 if pn in ("TUNE", "ALL") else 6
        if n == 0:
            parts.append(f"{0:>{w}} {'-':>7} {'-':>5}")
        else:
            parts.append(f"{n:>{w}} {s['mean'][i]:>+7.3f} {s['t'][i]:>+5.1f}")
    a = st["ALL"]
    if int(a["n"][i]):
        tail = (f" {a['win'][i]:>5.1f} {a['raw'][i]:>+7.3f} {_hold_text(st, i)} {a['inr'][i]:>+11,.0f}")  # fmt: skip
    else:
        tail = ""
    return " | ".join(parts) + tail


def _attrs(codes: np.ndarray) -> dict[str, np.ndarray]:
    mask = codes[:, 1].astype(int)
    bits = np.array([[m >> i & 1 for i in range(len(TFS))] for m in mask])
    return {
        "side": codes[:, 0].astype(int),
        "mask": mask,
        "bits": bits,
        "n_tf": bits.sum(axis=1),
        "slowest": np.array([max(i for i in range(len(TFS)) if m >> i & 1) for m in mask]),
        "fastest": np.array([min(i for i in range(len(TFS)) if m >> i & 1) for m in mask]),
        "trail": codes[:, 2].astype(int),
        "exit": codes[:, 3].astype(int),
        "eh": codes[:, 4].astype(int),
        "horizon": np.array([ENTRY_HORIZONS[e][1] for e in codes[:, 4]]),
    }


def _marginal(w, title: str, groups: list[tuple[str, np.ndarray]], st: dict, base: np.ndarray):
    w(f"\n  {title}")
    w(f"    {'group':<26} {'structures':>10} {'TUNE>0':>7} {'CHOOSE>0':>9} {'TEST>0':>7} {'all 3':>6} "
      f"{'median mean% ALL':>17} {'median gross%':>14} {'median trades':>14}")  # fmt: skip
    for label, sel in groups:
        m = sel & base
        k = int(m.sum())
        if not k:
            w(f"    {label:<26} {0:>10}")
            continue
        pos = {pn: st[pn]["mean"][m] > 0 for pn in ("TUNE", "CHOOSE", "TEST")}
        all3 = pos["TUNE"] & pos["CHOOSE"] & pos["TEST"]
        w(f"    {label:<26} {k:>10} {pos['TUNE'].mean() * 100:>6.0f}% {pos['CHOOSE'].mean() * 100:>8.0f}% "
          f"{pos['TEST'].mean() * 100:>6.0f}% {all3.mean() * 100:>5.0f}% "
          f"{np.median(st['ALL']['mean'][m]):>+17.3f} {np.median(st['ALL']['raw'][m]):>+14.3f} "
          f"{np.median(st['ALL']['n'][m]):>14,.0f}")  # fmt: skip


def report_basket(b: Basket, w, csv_rows: list[dict]) -> None:
    st = period_stats(b)
    at = _attrs(b.codes)
    ns = b.n_struct
    n_rows = len(st["ALL"]["n"])
    n_tune, n_choose = MIN_TRADES[b.name]
    unit = "% of the premium paid" if b.name == "INDEX_OPTIONS" else "% of the share price"

    def pad(x: np.ndarray, fill: int = -9) -> np.ndarray:
        return np.r_[x, np.full(n_rows - ns, fill)]

    w("=" * 150)
    w(f"{b.name}: {len(b.insts)} instruments, {ns:,} structures each. Returns are net of modelled "
      f"costs, {unit}, per trade.")  # fmt: skip
    w("t = mean / standard error with all trades entered on one day as one cluster. gross% = the move "
      "itself before any cost.")  # fmt: skip
    w("=" * 150)

    # ---- benchmarks ---------------------------------------------------------------
    w("\n[1] No signal at all: one trade every session, every instrument")
    w(HEAD)
    for k, name in enumerate(BENCH_NAMES):
        w(row_text(name, st, ns + k))
    w(
        "  the underlying itself, first open to last close of each research year (mean over the basket):"
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        drift = (b.hold[:, :, 1] / b.hold[:, :, 0] - 1.0) * 100.0
    w("    " + "  ".join(f"{YEAR_NAMES[y]} {np.nanmean(drift[:, y]):>+6.1f}%" for y in range(5)))

    # ---- the owner's rule exactly ---------------------------------------------------
    full = (1 << len(TFS)) - 1
    w(f"\n[2] The rule as stated: ALL of {', '.join(TFS)} agree (Supertrend 14, 1.7 on each)")
    for side in (1, -1):
        for eh in range(len(ENTRY_HORIZONS)):
            rows = np.flatnonzero((at["mask"] == full) & (at["side"] == side) & (at["eh"] == eh))
            if not len(rows) or st["ALL"]["n"][rows].sum() == 0:
                continue
            w(f"\n  {'LONG' if side == 1 else 'SHORT'}, {EH_NAMES[eh]}")
            w("  " + HEAD)
            for i in rows:
                w(
                    "  "
                    + row_text(f"trail {TFS[at['trail'][i]]:<3} {EXIT_MODES[at['exit'][i]]}", st, i)
                )

    # ---- what matters ---------------------------------------------------------------
    enough = (st["TUNE"]["n"] >= n_tune) & (st["CHOOSE"]["n"] >= n_choose) & (st["TEST"]["n"] >= n_choose)  # fmt: skip
    enough[ns:] = False
    w(f"\n[3] What matters: share of structures with a positive mean, by one property at a time "
      f"(structures with at least {n_tune} trades in TUNE and {n_choose} in CHOOSE and TEST)")  # fmt: skip
    for hz, hz_name in ((INTRADAY, "INTRADAY"), (CARRY, "CARRY")):
        base = enough & (pad(at["horizon"]) == hz)
        if not base.any():
            continue
        w(f"\n  ── {hz_name}: {int(base.sum()):,} structures ──")
        _marginal(w, "side", [("long", pad(at["side"]) == 1), ("short", pad(at["side"]) == -1)], st, base)  # fmt: skip
        _marginal(w, "how many timeframes must agree",
                  [(str(k), pad(at["n_tf"]) == k) for k in range(1, len(TFS) + 1)], st, base)  # fmt: skip
        _marginal(w, "slowest timeframe in the set",
                  [(TFS[k], pad(at["slowest"]) == k) for k in range(len(TFS))], st, base)  # fmt: skip
        _marginal(w, "fastest timeframe in the set",
                  [(TFS[k], pad(at["fastest"]) == k) for k in range(len(TFS))], st, base)  # fmt: skip
        _marginal(w, "set includes",
                  [(TFS[k], pad(at["bits"][:, k], 0) == 1) for k in range(len(TFS))], st, base)  # fmt: skip
        _marginal(w, "line used as stop and trail",
                  [(TFS[k], pad(at["trail"]) == k) for k in range(len(TFS))], st, base)  # fmt: skip
        _marginal(w, "how the line is acted on",
                  [(EXIT_MODES[k], pad(at["exit"]) == k) for k in range(3)], st, base)  # fmt: skip
        _marginal(w, "entry",
                  [(EH_NAMES[k], pad(at["eh"]) == k) for k in range(len(ENTRY_HORIZONS))], st, base)  # fmt: skip
        for side in (1, -1):
            _marginal(w, f"{'long' if side == 1 else 'short'} only: line used as stop and trail",
                      [(TFS[k], (pad(at["trail"]) == k) & (pad(at["side"]) == side))
                       for k in range(len(TFS))], st, base)  # fmt: skip

    # ---- selection ------------------------------------------------------------------
    ok, score, checks = eligibility(b, st)
    w(
        f"\n[4] Selection by the rule written down before the run (TEST not used). {ns:,} structures tried."
    )
    running = np.ones(len(ok), dtype=bool)
    running[ns:] = False
    for label, v in checks.items():
        running &= v
        w(f"    after '{label}': {int(running.sum()):,} structures left")
    elig = np.flatnonzero(ok)
    if len(elig):
        order = elig[np.argsort(-score[elig])]
        test_pos = st["TEST"]["mean"][elig] > 0
        others = np.flatnonzero(~ok[:ns] & (st["TEST"]["n"][:ns] >= n_choose))
        w(f"    eligible: {len(elig):,}. Positive in TEST: {int(test_pos.sum()):,} "
          f"({test_pos.mean() * 100:.0f}%). For comparison, {np.mean(st['TEST']['mean'][others] > 0) * 100:.0f}% "
          f"of the {len(others):,} NOT eligible structures with enough TEST trades were positive in TEST.")  # fmt: skip
        w(f"    median TEST mean of the eligible: {np.median(st['TEST']['mean'][elig]):+.3f}%; "
          f"of the top 20: {np.median(st['TEST']['mean'][order[:20]]):+.3f}%")  # fmt: skip
        for hz, hz_name in ((INTRADAY, "intraday"), (CARRY, "carry")):
            sub = elig[at["horizon"][elig] == hz]
            if len(sub):
                w(f"    eligible {hz_name}: {len(sub):,}, positive in TEST {np.mean(st['TEST']['mean'][sub] > 0) * 100:.0f}%, "
                  f"median TEST mean {np.median(st['TEST']['mean'][sub]):+.3f}%")  # fmt: skip
        w("\n  Top 25 by min(t TUNE, t CHOOSE):")
        w("  " + HEAD)
        for i in order[:25]:
            w("  " + row_text(structure_name(b.codes[i]), st, i))
        for hz, hz_name in ((INTRADAY, "intraday"), (CARRY, "carry")):
            sub = order[at["horizon"][order] == hz][:12]
            if len(sub):
                w(f"\n  Top 12 {hz_name} structures:")
                w("  " + HEAD)
                for i in sub:
                    w("  " + row_text(structure_name(b.codes[i]), st, i))
        best = order[0]
        _detail(b, st, int(best), w, "The top-ranked structure")
        for hz in (INTRADAY, CARRY):
            sub = order[at["horizon"][order] == hz]
            if len(sub) and int(sub[0]) != int(best):
                _detail(b, st, int(sub[0]), w, f"The top {'intraday' if hz == INTRADAY else 'carry'} structure")  # fmt: skip
    else:
        w("    eligible: 0 - no structure passes the rule.")

    # ---- the whole grid, for the record ----------------------------------------------
    w("\n[5] The whole grid at a glance (structures with enough trades in every period)")
    pos = {pn: np.nan_to_num(st[pn]["mean"], nan=-1.0) > 0 for pn in ("TUNE", "CHOOSE", "TEST")}
    a3 = pos["TUNE"] & pos["CHOOSE"] & pos["TEST"]
    worst = np.minimum(np.minimum(st["TUNE"]["t"], st["CHOOSE"]["t"]), st["TEST"]["t"])
    for hz, hz_name in ((INTRADAY, "intraday"), (CARRY, "carry")):
        m = enough & (pad(at["horizon"]) == hz)
        if not m.any():
            continue
        w(f"    {hz_name:<9} {int(m.sum()):>5,} structures: positive TUNE {pos['TUNE'][m].mean() * 100:.0f}%, "
          f"CHOOSE {pos['CHOOSE'][m].mean() * 100:.0f}%, TEST {pos['TEST'][m].mean() * 100:.0f}%, "
          f"all three {a3[m].mean() * 100:.1f}% ({int((a3 & m).sum()):,}); median mean ALL "
          f"{np.median(st['ALL']['mean'][m]):+.3f}%, median gross {np.median(st['ALL']['raw'][m]):+.3f}%")  # fmt: skip
        both = np.flatnonzero(a3 & m)
        if len(both):
            w("      positive in all three periods, best by the weakest period's t (found WITH the test year - "
              "a description, not a selection):")  # fmt: skip
            w("      " + HEAD)
            for i in both[np.argsort(-worst[both])][:12]:
                w("      " + row_text(structure_name(b.codes[i]), st, i))

    for i in range(ns):
        code = b.codes[i]
        rec = {
            "basket": b.name, "side": "long" if code[0] == 1 else "short",
            "timeframes": mask_name(int(code[1])), "trail": TFS[int(code[2])],
            "exit": EXIT_MODES[int(code[3])], "entry": EH_NAMES[int(code[4])],
            "eligible": bool(ok[i]), "score": round(float(score[i]), 3),
        }  # fmt: skip
        for pn in ("TUNE", "CHOOSE", "TEST", "ALL"):
            n = int(st[pn]["n"][i])
            rec[f"n_{pn}"] = n
            rec[f"mean_{pn}"] = round(float(st[pn]["mean"][i]), 4) if n else None
            rec[f"t_{pn}"] = round(float(st[pn]["t"][i]), 2) if n else None
        a = st["ALL"]
        if rec["n_ALL"]:
            rec.update({
                "win_pct": round(float(a["win"][i]), 1), "gross_pct": round(float(a["raw"][i]), 4),
                "hold_minutes": round(float(a["hold"][i]), 0), "sessions_held": round(float(a["sess"][i]), 2),
                "net_inr": round(float(a["inr"][i]), 0), "mean_with_dp": round(float(a["mean_dp"][i]), 4),
                "t_month": round(float(a["tm"][i]), 2), "ended_by_line_pct": round(float(a["event"][i]), 1),
            })  # fmt: skip
        csv_rows.append(rec)


def _detail(b: Basket, st: dict, i: int, w, title: str) -> None:
    """One structure: by year, by instrument, with and without the depository charge."""
    agg = b.agg[i]
    name = structure_name(b.codes[i]) if i < b.n_struct else BENCH_NAMES[i - b.n_struct]
    w(f"\n  {title}: {name}")
    a = st["ALL"]
    w(f"    ALL: {int(a['n'][i]):,} trades, mean {a['mean'][i]:+.3f}%, t by day {a['t'][i]:+.2f}, t by month "
      f"{a['tm'][i]:+.2f}, win {a['win'][i]:.1f}%, profit factor {a['pf'][i]:.2f}, gross move {a['raw'][i]:+.3f}%, "
      f"net Rs {a['inr'][i]:+,.0f}; ended by the line {a['event'][i]:.0f}%, held overnight {a['carried'][i]:.0f}%, "
      f"mean hold {a['hold'][i]:,.0f} min ({a['sess'][i]:.1f} sessions)")  # fmt: skip
    if b.name == "STOCKS" and a["carried"][i] > 0:
        w(f"    with the depository charge of Rs {DP_CHARGE_INR} per delivery sell on ONE share: mean "
          f"{a['mean_dp'][i]:+.3f}% (t {a['t_dp'][i]:+.2f}, trades treated as independent)")  # fmt: skip
    w("    by year:  " + "  ".join(
        f"{YEAR_NAMES[y]} n {int(agg[y, F_N]):>5} {agg[y, F_SUM] / max(agg[y, F_N], 1):>+7.3f}%"
        for y in range(5)))  # fmt: skip
    w(
        f"    {'instrument':<16} {'n':>6} {'TUNE':>8} {'CHOOSE':>8} {'TEST':>8} {'ALL':>8} {'win%':>6} {'net Rs':>11}"
    )
    for k, inst in enumerate(b.insts):
        ia = b.inst_agg[k, i]
        cells = []
        for y0, y1 in (PERIODS["TUNE"], PERIODS["CHOOSE"], PERIODS["TEST"], PERIODS["ALL"]):
            n = ia[y0:y1, F_N].sum()
            cells.append(f"{ia[y0:y1, F_SUM].sum() / n:>+8.3f}" if n else f"{'-':>8}")
        n = ia[:, F_N].sum()
        win = ia[:, F_WIN].sum() / n * 100 if n else 0.0
        w(f"    {inst:<16} {int(n):>6} {' '.join(cells)} {win:>6.1f} {ia[:, F_INR].sum():>+11,.0f}")


def report(out_dir: Path, dest: Path) -> None:
    lines: list[str] = []

    def w(text: str = "") -> None:
        lines.append(text)

    w("MULTI-TIMEFRAME SUPERTREND(14, 1.7) - STAGE 1 BRUTE FORCE")
    w(f"Bars: cleaned Dhan one-minute history {T0} .. {T_END}. TUNE = years 1-3, CHOOSE = year 4, "
      "TEST = year 5 (8 Nov to 7 Nov).")  # fmt: skip
    w("Every reading of the rule: which timeframes must agree x whose line is the stop and trail x how "
      "the line is acted on x when to enter x intraday or carried x long or short.")  # fmt: skip
    w("Evidence about a rule on past data after modelled costs. Not advice and not a forecast.")
    csv_rows: list[dict] = []
    for name in BASKETS:
        w("")
        report_basket(load_basket(name, out_dir), w, csv_rows)
    dest.write_text("\n".join(lines) + "\n")
    pd.DataFrame(csv_rows).to_csv(dest.with_suffix(".csv"), index=False)
    print(f"{len(lines)} lines -> {dest}")


# ───────────────────────────── cli ─────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default=str(OUT / "stage1"))
    ap.add_argument("--dest", default=str(OUT / "mtf_stage1.txt"))
    args = ap.parse_args()
    if args.validate:
        return validate()
    if args.run:
        run(args.workers, args.only, Path(args.out))
    if args.report:
        report(Path(args.out), Path(args.dest))
    if not (args.run or args.report):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
