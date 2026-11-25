"""US-092: new option setups (S1 to S7) and a long-volatility test on four index options.

Run from the MAIN checkout (the data is there), this file from the US-092 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/option_setups.py" <stage> [<stage> ...]

Stages
  days     per index and session: the day types known before 09:15 (size state, NR7, India
           VIX change, monthly expiry from the real option store), daily ATR and sd, and the
           desk stocks' first-half-hour turnover ratio. Also the expiry calendar.
  atm      per index: the near monthly at-the-money CALL of every minute from the real store
           (strike, open, close) and its implied volatility (the real-IV reading of S3, S4).
  s1 .. s6 the trades of each setup, one row per index, variant and session (both sides
           where the placebo flips the side), model and real readings.
  truncate the no-look-ahead proof: signals recomputed on data cut after the signal bar.
  select   the selection rule of RESEARCH-RULES section 5 over every cell -> cells.csv.
  placebo  20 runs with the signal dates shifted inside each calendar year (and the side
           flipped at random for S1, S5, S6) -> placebo.csv.
  report   the tables, to OUT/report.txt.

The stores are read only: `data/onelot.db` through the harness's readers and
`data/expired_options.db` through SQLite `mode=ro`. No network, no broker. The ratchet study
(and through it `src/onelot/manage/ratchet.py`) and `src/onelot/mllab/states.py` are loaded by
FILE PATH from this worktree; the worktree's `src` is never on the path. Outputs go to
data/research/option_setups_2026-11-24/ of the main checkout.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

WT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rs = _load("ratchet_study_us092", WT / "scripts" / "ratchet_study.py")
ST = _load("states_us092", WT / "src" / "onelot" / "mllab" / "states.py")
h, ms, R = rs.h, rs.ms, rs.R

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.special import ndtr  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

from onelot.data.expired_options import _unpack  # noqa: E402
from onelot.lab.real_options import atm_rel, interpolate  # noqa: E402

OUT = h.ROOT / "data" / "research" / "option_setups_2026-11-24"
STORE = h.ROOT / "data" / "expired_options.db"
INDICES = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
UND = {"NIFTY_OPT": "NIFTY", "BANKNIFTY_OPT": "BANKNIFTY", "FINNIFTY_OPT": "FINNIFTY",
       "SENSEX_OPT": "SENSEX"}  # fmt: skip
NAMES = ("TUNE", "CHOOSE", "TEST")
K_ATR, TARGET_R = 1.5, 3.0  # the desk's rule
PRM = R.RatchetParams(extend=True)
REAL_GAP = 1.0  # model less 1.0 point of premium where no real bar priced the trade
Q_DIV = 0.012  # dividend yield in the forward of the real-IV reading (stated in the note)
MAX_WAIT = 3  # minutes: the Lab's wait for the first option trade
SECONDS_PER_YEAR = h.SECONDS_PER_YEAR
DAY_TYPES = ("ALL", "ACTIVE-EXP", "CALM-EXP", "NR7", "VIX-UP", "VIX-DOWN", "EXPIRY", "PRE-EXPIRY")
MIN_EX_TRADES, MIN_EX_DAYS = 60, 30
WORKERS = 2


def mins(hhmm: str) -> int:
    a, b = hhmm.split(":")
    return int(a) * 60 + int(b)


# ───────────────────────────── the real option store, read only ─────────────


class Store:
    """Day blocks of `data/expired_options.db`, opened with SQLite `mode=ro`."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
        self.mem: dict[tuple, dict | None] = {}

    def get(self, und: str, code: int, opt: str, rel: int, day: dt.date) -> dict | None:
        key = (und, code, opt, rel, day)
        if key in self.mem:
            return self.mem[key]
        row = self.db.execute(
            "SELECT data FROM blocks WHERE underlying=? AND flag='MONTH' AND code=? AND opt=?"
            " AND rel=? AND day=?",
            (und, code, opt, rel, day.isoformat()),
        ).fetchone()
        blk = None
        if row is not None and row[0]:
            d = _unpack(row[0])
            d["pos"] = {int(t): i for i, t in enumerate(d["ts"])}
            blk = d
        if len(self.mem) > 50000:
            self.mem.clear()
        self.mem[key] = blk
        return blk

    def series(self, und: str, code: int, opt: str, rel: int) -> dict[dt.date, dict]:
        out = {}
        q = ("SELECT day, data FROM blocks WHERE underlying=? AND flag='MONTH' AND code=?"
             " AND opt=? AND rel=? AND rows > 0")  # fmt: skip
        for day, blob in self.db.execute(q, (und, code, opt, rel)):
            out[dt.date.fromisoformat(day)] = _unpack(blob)
        return out

    def coverage(self) -> pd.DataFrame:
        q = ("SELECT underlying, substr(day,1,4), code, opt, COUNT(DISTINCT day),"
             " SUM(rows > 0) FROM blocks WHERE rel = 0 GROUP BY 1, 2, 3, 4")  # fmt: skip
        return pd.DataFrame(self.db.execute(q).fetchall(),
                            columns=["und", "year", "code", "opt", "days", "with_bars"])  # fmt: skip


# ───────────────────────────── Black-76, vectorised ──────────────────────────


def b76(f, k, t, sig, r, call):  # noqa: ANN001, ANN201
    t = np.maximum(t, 60.0 / SECONDS_PER_YEAR)
    disc = np.exp(-r * t)
    v = sig * np.sqrt(t)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(f / k) + 0.5 * v * v) / v
    d2 = d1 - v
    c = disc * (f * ndtr(d1) - k * ndtr(d2))
    p = disc * (k * ndtr(-d2) - f * ndtr(-d1))
    return np.where(call, c, p)


def implied_vol(c, f, k, t, r):  # noqa: ANN001, ANN201
    """Black-76 call volatility by bisection on [0.005, 3]; NaN where the price is not a
    valid call price (below the discounted intrinsic value or above the forward)."""
    c, f, k, t = (np.asarray(x, float) for x in (c, f, k, t))
    t = np.maximum(t, 60.0 / SECONDS_PER_YEAR)
    disc = np.exp(-r * t)
    lo = np.full(c.shape, 0.005)
    hi = np.full(c.shape, 3.0)
    floor = disc * np.maximum(f - k, 0.0)
    ok = np.isfinite(c) & (c > floor + 1e-6) & (c < disc * f)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        up = b76(f, k, t, mid, r, True) < c
        lo = np.where(up, mid, lo)
        hi = np.where(up, hi, mid)
    out = 0.5 * (lo + hi)
    # a price at the discounted intrinsic value: no time value left, the lowest volatility
    out = np.where(np.isfinite(c) & (c <= floor + 1e-6) & (c > 0), 0.005, out)
    return np.where(ok | (np.isfinite(c) & (c > 0) & (c <= floor + 1e-6)), out, np.nan)


# ───────────────────────────── shared helpers ───────────────────────────────


def bt_cfg() -> dict:
    bt = h._settings.backtest
    return {"r": float(bt.risk_free_rate), "half": float(bt.option_spread_pct),
            "ticks": float(bt.option_slippage_ticks), "iv_mult": float(bt.option_iv_mult),
            "min_days": int(bt.option_min_days_to_expiry)}  # fmt: skip


def book(p: h.Prep, entry_px, exit_px) -> np.ndarray:  # noqa: ANN001
    """% of the money put in, after the model's friction (half-spread, ticks, charges): the
    same booking as `real_options.RealPricer._book` and the harness's `pnl`."""
    cfg = p.opt_cfg
    e = np.asarray(entry_px, float)
    x = np.asarray(exit_px, float)
    ef = h.tick_up(e * (1 + cfg["half"]) + cfg["slip"], p.tick)
    xf = np.maximum(h.tick_down(x * (1 - cfg["half"]) - cfg["slip"], p.tick), p.tick)
    oc = p.costs["options"]
    net = (xf - ef) * p.lot - oc.leg(ef * p.lot, sell=False) - oc.leg(xf * p.lot, sell=True)
    return net, ef * p.lot


def day_t(x, d) -> float:  # noqa: ANN001
    return rs.day_t(np.asarray(x, float), np.asarray(d))


def prep(inst: str) -> h.Prep:
    """The harness's arrays with the model's IV from the PREVIOUS session's India VIX (no
    look-ahead in the premium model)."""
    return h.prepare(inst, tfs=("60m",), iv_lag="prev")


def desk_exit(p: h.Prep, j: int, side: int, atr60: np.ndarray) -> tuple:
    """The desk's rule: ratchet, first stop 1.5 x ATR(14) of 60m bars, target 3 risks,
    extend on, and the harness's session exit (15:15). (bar, price, reason, how)."""
    s = int(p.sess[j])
    end, px = int(p.xi[s]), float(p.xp[s])
    how = "close" if p.xclose[s] else "open"
    stop_end = end + 1 if p.xclose[s] else end
    e = float(p.o[j])
    plan = R.plan_from_risk(side, e, K_ATR * float(atr60[j]), TARGET_R, rs.COST_FRAC * e)
    hit = rs.ratchet_numpy(plan, PRM, p.o[j:stop_end], p.h[j:stop_end], p.l[j:stop_end])
    if hit is None:
        return end, px, "session", how
    k, lvl, code, gap = hit
    return j + k, float(lvl), rs.REASONS[code], ("open" if gap else "touch")


def model_ret(p: h.Prep, side: int, E, X, PX) -> np.ndarray:  # noqa: ANN001, N803
    E, X, PX = (np.asarray(v) for v in (E, X, PX))  # noqa: N806
    if not len(E):
        return np.zeros(0)
    XT = p.ns[X] + np.where(p.xclose[p.sess[X]] & (X == p.xi[p.sess[X]]), h.NS_MIN, 0)  # noqa: N806
    return h.pnl(p, side, 0, E, X, PX, XT)["ret"]


def minute_at(p: h.Prep, s: int, minute: int) -> int:
    """The bar of session s stamped at `minute` (minutes after midnight), or -1."""
    a, b = int(p.sess_first[s]), int(p.sess_last[s])
    k = np.searchsorted(p.minute[a : b + 1], minute)
    if k <= b - a and p.minute[a + k] == minute:
        return a + int(k)
    return -1


# ───────────────────────────── stage: days ──────────────────────────────────


def expiry_calendar(st: Store, und: str, sessions: list[dt.date]) -> pd.DataFrame:
    """Monthly expiry days of `und` from the real store: the near monthly at-the-money call
    has almost no time value after 15:00 only on its last day (median time value from 15:00
    under 0.10% of the index; the lowest such day of the month), in the last ten days of a month. A month without such a day (no bars
    stored) takes the last session on or before the last weekday of the month that the
    nearest found expiry fell on. Returns one row per month: day, how (store | filled).
    Known (independent check, 2026-11-25): the filled NIFTY 2025-05 date (Wed 28) is a day early;
    the store puts that expiry on Thursday 29. Not re-run."""
    ser = st.series(und, 1, "CE", 0)
    found: dict[tuple[int, int], dt.date] = {}
    score: dict[tuple[int, int], float] = {}
    for d, b in sorted(ser.items()):
        last = (dt.date(d.year + d.month // 12, d.month % 12 + 1, 1) - dt.timedelta(days=1)).day
        if d.day < last - 9:
            continue
        minute = (b["ts"] + 19800) % 86400 // 60
        late = minute >= 15 * 60
        if not late.any():
            continue
        tv = (b["close"] - np.maximum(b["spot"] - b["strike"], 0.0)) / b["spot"]
        med = float(np.median(tv[late]))
        if med < 0.001 and med < score.get((d.year, d.month), 1.0):
            found[(d.year, d.month)] = d
            score[(d.year, d.month)] = med
    sess = np.array(sorted(sessions))
    months = sorted({(d.year, d.month) for d in sess})
    fd = sorted(found.values())
    rows = []
    for y, mth in months:
        if (y, mth) in found:
            rows.append({"month": f"{y}-{mth:02d}", "day": found[(y, mth)], "how": "store"})
            continue
        mid = dt.date(y, mth, 15)
        near = min(fd, key=lambda x: abs((x - mid).days))
        wd = near.weekday()
        d = dt.date(y + mth // 12, mth % 12 + 1, 1) - dt.timedelta(days=1)
        while d.weekday() != wd:
            d -= dt.timedelta(days=1)
        if d <= sess[-1]:  # a holiday moves it to the session before
            d = sess[np.searchsorted(sess, d, side="right") - 1]
        rows.append({"month": f"{y}-{mth:02d}", "day": d, "how": "filled"})
    return pd.DataFrame(rows)


def turnover_ratio(dates: list[dt.date]) -> pd.Series:
    """Rupee turnover (volume x close) of the 18 desk stocks from 09:15 to 09:44 over its
    median of the 20 sessions before; indexed by date."""
    tot: dict[dt.date, float] = {}
    for inst in h.STOCKS:
        key = h._cache.spec(inst).series_key
        m = h._cache.bars(key, "1m")
        mi = m.index.hour * 60 + m.index.minute
        sel = m[(mi >= mins("09:15")) & (mi <= mins("09:44"))]
        v = (sel["volume"].astype(float) * sel["close"].astype(float)).groupby(sel.index.date).sum()
        for d, x in v.items():
            tot[d] = tot.get(d, 0.0) + float(x)
    s = pd.Series(tot).sort_index()
    s = s[s > 0]
    med = s.shift(1).rolling(20, min_periods=20).median()
    r = (s / med).reindex(dates)
    return r


def stage_days() -> None:
    h._init_worker()
    st = Store()
    vix = h._cache.vix()
    vix = vix[vix > 0]
    frames, cal = [], []
    tr_ratio = None
    for inst in INDICES:
        und = UND[inst]
        key = h._cache.spec(inst).series_key
        m = h._cache.bars(key, "1m")
        m = m[m.index < pd.Timestamp(h.T_END + dt.timedelta(days=1), tz=m.index.tz)]
        dates = sorted(set(m.index.date))
        dates = [d for d in dates if d >= h.T0]
        ec = expiry_calendar(st, und, dates)
        ec["inst"] = inst
        cal.append(ec)
        exp = np.array(sorted(ec["day"]))
        daily = h._cache.bars(key, "1d")
        dd = np.array([x.date() for x in daily.index])
        states = ST.classify(daily)
        rng = (daily["high"] - daily["low"]).to_numpy(float)
        c = daily["close"].to_numpy(float)
        lr = np.r_[np.nan, np.log(c[1:] / c[:-1])]
        sd20 = pd.Series(lr).rolling(20, min_periods=20).std().to_numpy()
        nr7 = np.zeros(len(rng), dtype=bool)
        for i in range(6, len(rng)):
            nr7[i] = rng[i] <= rng[i - 6 : i].min()
        atr_d = rs.wilder_atr(daily["high"].to_numpy(float), daily["low"].to_numpy(float), c)
        vd = np.array([x.date() for x in vix.index])
        vv = vix.to_numpy(float)
        if tr_ratio is None:
            tr_ratio = turnover_ratio(dates)
        rows = []
        for d in dates:
            k = int(np.searchsorted(dd, d, side="left")) - 1  # the last daily bar BEFORE d
            kv = int(np.searchsorted(vd, d, side="left")) - 1
            ne = exp[np.searchsorted(exp, d)] if np.searchsorted(exp, d) < len(exp) else None
            nxt_sess = dates[dates.index(d) + 1] if dates.index(d) + 1 < len(dates) else None
            rows.append({
                "inst": inst, "date": d, "prev_close": c[k] if k >= 0 else np.nan,
                "size_prev": states["size"].iloc[k] if k >= 0 else None,
                "nr7": bool(nr7[k]) if k >= 0 else False,
                "vix_prev": vv[kv] if kv >= 0 else np.nan,
                "vix_chg": vv[kv] / vv[kv - 1] - 1 if kv >= 1 else np.nan,
                "atr_d": atr_d[k] if k >= 0 else np.nan, "sd20": sd20[k] if k >= 0 else np.nan,
                "expiry": ne, "is_exp": ne == d, "is_pre": ne is not None and ne == nxt_sess,
                "dte": (ne - d).days if ne is not None else np.nan,
                "turn_ratio": float(tr_ratio.get(d, np.nan)),
            })  # fmt: skip
        df = pd.DataFrame(rows)
        frames.append(df)
        print(f"{inst}: {len(df)} sessions, expiries from the store "
              f"{(ec.how == 'store').sum()} of {len(ec)}", flush=True)  # fmt: skip
    pd.concat(frames, ignore_index=True).to_csv(OUT / "days.csv", index=False)
    pd.concat(cal, ignore_index=True).to_csv(OUT / "expiries.csv", index=False)
    st.coverage().to_csv(OUT / "store_coverage.csv", index=False)


def load_days() -> pd.DataFrame:
    d = pd.read_csv(OUT / "days.csv", parse_dates=["date", "expiry"])
    d["date"] = d["date"].dt.date
    d["expiry"] = d["expiry"].dt.date
    vx = d["vix_chg"]
    flags = {
        "ALL": np.ones(len(d), dtype=bool),
        "ACTIVE-EXP": (d["size_prev"] == "active").to_numpy(),
        "CALM-EXP": (d["size_prev"] == "calm").to_numpy(),
        "NR7": d["nr7"].astype(bool).to_numpy(),
        "VIX-UP": (vx > 0.05).to_numpy(),
        "VIX-DOWN": (vx < -0.05).to_numpy(),
        "EXPIRY": d["is_exp"].astype(bool).to_numpy(),
        "PRE-EXPIRY": d["is_pre"].astype(bool).to_numpy(),
    }
    for k, v in flags.items():
        d["f_" + k] = v
    return d


# ───────────────────────────── stage: atm ───────────────────────────────────


def stage_atm() -> None:
    """The near monthly at-the-money CALL of every minute (rel 0, code 1) from the store."""
    st = Store()
    for inst in INDICES:
        ser = st.series(UND[inst], 1, "CE", 0)
        parts = [ser[d] for d in sorted(ser)]
        z = {c: np.concatenate([q[c] for q in parts]) for c in ("ts", "open", "close", "strike")}
        np.savez(OUT / f"atm_{inst}.npz", **z)
        print(f"{inst}: {len(ser)} days, {len(z['ts'])} minutes", flush=True)


# ───────────────────────────── real premiums of one contract ────────────────


class Exact:
    """Real premiums of the model's own contract (the harness's strike, the Lab's roll from
    the near to the next monthly inside 3 days) where the store holds its bars. The entry is
    the option's open in the entry minute, exits as `real_options.RealPricer` does them (open,
    close, or a touch interpolated inside the option's bar); the first trade within 3 minutes
    when the minute has none. None when the store cannot price the trade."""

    def __init__(self, p: h.Prep, inst: str, days: pd.DataFrame, store: Store) -> None:
        self.p, self.st = p, store
        self.und = UND[inst]
        self.step = float(h._cache.spec(inst).strike_step)
        self.ep = (p.ns // 1_000_000_000).astype(np.int64)
        dd = days[days.inst == inst]
        self.near = dict(zip(dd["date"], dd["expiry"], strict=True))
        self.min_days = bt_cfg()["min_days"]

    def code(self, day: dt.date) -> int:
        ne = self.near.get(day)
        return 2 if ne is not None and (ne - day).days < self.min_days else 1

    def _find(self, code: int, opt: str, k: float, j: int, day: dt.date):  # noqa: ANN202
        p = self.p
        last = int(p.sess_last[p.sess[j]])
        for w in range(MAX_WAIT + 1):
            jj = j + w
            if jj > last:
                break
            rel0 = atm_rel(k, float(p.c[jj]), self.step)
            for rel in (rel0, rel0 - 1, rel0 + 1):
                if abs(rel) > 10:
                    continue
                blk = self.st.get(self.und, code, opt, rel, day)
                if blk is None:
                    continue
                i = blk["pos"].get(int(self.ep[jj]))
                if i is not None and abs(float(blk["strike"][i]) - k) < 1e-6:
                    return w, blk, i
        return None

    def price(self, side: int, e: int, x: int, how: str, level: float, k: float) -> float:
        p = self.p
        day = p.sess_dates[p.sess[e]]
        code = self.code(day)
        opt = "CE" if side == 1 else "PE"
        a = self._find(code, opt, k, e, day)
        if a is None:
            return np.nan
        pe = float(a[1]["open"][a[2]])
        b = self._find(code, opt, k, x, p.sess_dates[p.sess[x]])
        if b is None:
            return np.nan
        w, blk, i = b
        bar = tuple(float(blk[c][i]) for c in ("open", "high", "low", "close"))
        if how == "touch" and w == 0:
            und = (float(p.o[x]), float(p.h[x]), float(p.l[x]), float(p.c[x]))
            px = interpolate(bar, und, level, opt)
        elif how == "close" and w == 0:
            px = bar[3]
        else:
            px = bar[0]
        net, base = book(p, pe, px)
        return float(net / base * 100.0)


def single(p: h.Prep, ex: Exact, side: int, rows: list[tuple]) -> tuple[np.ndarray, np.ndarray]:
    """Model and real % a trade for (entry, exit, exit price, how) rows of one side."""
    if not rows:
        return np.zeros(0), np.zeros(0)
    E, X, PX, HOW = (np.array(v) for v in zip(*rows, strict=True))  # noqa: N806
    E, X = E.astype(np.int64), X.astype(np.int64)  # noqa: N806
    m = model_ret(p, side, E, X, PX.astype(float))
    k = p.opt(side).strike[E]
    r = np.array([ex.price(side, int(e), int(x), str(hw), float(lv), float(kk))
                  for e, x, lv, hw, kk in zip(E, X, PX, HOW, k, strict=True)])  # fmt: skip
    return m, r


def session_exit(p: h.Prep, s: int) -> tuple[int, float, str]:
    return int(p.xi[s]), float(p.xp[s]), ("close" if p.xclose[s] else "open")


def sessions(p: h.Prep) -> list[int]:
    return [s for s in range(len(p.sess_first)) if p.year[p.sess_first[s]] >= 0]


def base_row(p: h.Prep, inst: str, s: int) -> dict:
    j = int(p.sess_first[s])
    return {"inst": inst, "date": p.sess_dates[s], "dayoff": int(p.dayoff[j]),
            "period": int(rs.mb.PERIOD[p.year[j]])}  # fmt: skip


def at_minute(minute: np.ndarray, a: int, b: int, mm: int) -> int:
    k = np.flatnonzero(minute[a:b] == mm)
    return a + int(k[0]) if len(k) else -1


# ───────────────────────────── signals (each provable on cut data) ─────────


def sig_s1(c: np.ndarray, minute: np.ndarray, a: int, b: int, prev_close: float):  # noqa: ANN201
    """S1 on the bars a..b-1 of a session: (first return: previous close to the 09:44 close,
    14:29 close to 14:59 close), or None. The signal bar is 14:59; entry 15:00."""
    j44, j29, j59 = (at_minute(minute, a, b, mins(x)) for x in ("09:44", "14:29", "14:59"))
    if min(j44, j29, j59) < 0 or not prev_close > 0:
        return None
    return float(c[j44] / prev_close - 1), float(c[j59] / c[j29] - 1)


def sig_s2(hi_: np.ndarray, lo_: np.ndarray, c: np.ndarray, minute: np.ndarray, a: int, b: int,
           length: int):  # noqa: ANN201  # fmt: skip
    """S2: (range high, range low, signal bar or -1, side). The range is 09:15 to the
    range's last minute; the signal is the first one-minute close outside it from the
    range's end, on a bar that closes by 13:00."""
    seg = minute[a:b]
    r0 = mins("09:15")
    rm = (seg >= r0) & (seg < r0 + length)
    if rm.sum() < length - 2 or not (seg == r0 + length - 1).any():
        return None
    rh, rl = float(hi_[a:b][rm].max()), float(lo_[a:b][rm].min())
    for k in np.flatnonzero((seg >= r0 + length) & (seg <= mins("12:59"))):
        if c[a + k] > rh:
            return rh, rl, a + int(k), 1
        if c[a + k] < rl:
            return rh, rl, a + int(k), -1
    return rh, rl, -1, 0


def sig_s4(c: np.ndarray, minute: np.ndarray, a: int, b: int, day_open: float, atr_d: float,
           at: str) -> float | None:  # fmt: skip
    """S4: distance of the last close before `at` from today's open, in daily ATRs."""
    j = at_minute(minute, a, b, mins(at) - 1)
    if j < 0 or not atr_d > 0:
        return None
    return float((c[j] - day_open) / atr_d)


def sig_s6(o: np.ndarray, c: np.ndarray, minute: np.ndarray, a: int, b: int) -> float | None:
    """S6: the index return from the 09:15 open to the 09:44 close."""
    j0, j44 = at_minute(minute, a, b, mins("09:15")), at_minute(minute, a, b, mins("09:44"))
    if j0 < 0 or j44 < 0:
        return None
    return float(c[j44] / o[j0] - 1)


# ───────────────────────────── stages: single-leg setups ───────────────────


def day_lookup(days: pd.DataFrame, inst: str) -> dict:
    d = days[days.inst == inst]
    return {r["date"]: r for r in d.to_dict("records")}


def both_sides(p: h.Prep, ex: Exact, atr60: np.ndarray | None, entries: list[tuple]) -> dict:
    """For (row, entry bar) pairs: model and real % for a call and for a put, exits by the
    desk's rule (atr60 given) or at the session exit."""
    out = {}
    for side, tag in ((1, "l"), (-1, "s")):
        rows = []
        for _r, j in entries:
            if atr60 is None:
                x, px, how = session_exit(p, int(p.sess[j]))
            else:
                x, px, _why, how = desk_exit(p, j, side, atr60)
            rows.append((j, x, px, how))
        m, r = single(p, ex, side, rows)
        out["m_" + tag], out["r_" + tag] = m, r
    return out


def stage_s1() -> None:
    h._init_worker()
    days, st = load_days(), Store()
    frames = []
    for inst in INDICES:
        p = prep(inst)
        ex = Exact(p, inst, days, st)
        dl = day_lookup(days, inst)
        rows, ent = [], []
        for s in sessions(p):
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            dr = dl.get(p.sess_dates[s])
            if dr is None:
                continue
            sg = sig_s1(p.c, p.minute, a, b, float(dr["prev_close"]))
            e = at_minute(p.minute, a, b, mins("15:00"))
            if sg is None or e < 0 or e >= p.xi[s] or sg[0] == 0:
                continue
            rows.append({**base_row(p, inst, s), "r1": sg[0], "rb": sg[1],
                         "side": int(np.sign(sg[0]))})  # fmt: skip
            ent.append((rows[-1], e))
        df = pd.DataFrame(rows)
        for k, v in both_sides(p, ex, None, ent).items():
            df[k] = v
        # the size filter: |first return| above its median of the 60 sessions before
        med = df["r1"].abs().shift(1).rolling(60, min_periods=60).median()
        df["f_BIG"] = df["r1"].abs() > med
        df["f_B"] = np.sign(df["rb"]) == np.sign(df["r1"])
        df["f_B+BIG"] = df["f_B"] & df["f_BIG"]
        df["f_ALL"] = True
        df["exists"] = True
        frames.append(df)
        print(f"{inst}: {len(df)} sessions", flush=True)
    pd.concat(frames, ignore_index=True).to_csv(OUT / "s1.csv", index=False)


def stage_s2() -> None:
    h._init_worker()
    days, st = load_days(), Store()
    frames = []
    for inst in INDICES:
        p = prep(inst)
        ex = Exact(p, inst, days, st)
        atr60 = ms.tf_age_atr(p, "60m", np.arange(p.n))[1]
        dl = day_lookup(days, inst)
        for length in (15, 30):
            rows, trades = [], {1: [], -1: []}
            for s in sessions(p):
                a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
                dr = dl.get(p.sess_dates[s])
                sg = sig_s2(p.h, p.l, p.c, p.minute, a, b, length)
                if dr is None or sg is None:
                    continue
                rh, rl, j, side = sg
                e = j + 1 if j >= 0 else -1
                ok = e > 0 and e < b and p.minute[e] <= mins("13:00") and atr60[e] > 0
                row = {**base_row(p, inst, s), "variant": f"{length}m", "width": (rh - rl) / p.o[a],
                       "gap": abs(p.o[a] / dr["prev_close"] - 1) / dr["sd20"], "side": side if ok else 0,
                       "exists": bool(ok)}  # fmt: skip
                if ok:
                    x, px, _why, how = desk_exit(p, e, side, atr60)
                    row["bp"] = side * (px / p.o[e] - 1) * 1e4
                    trades[side].append((len(rows), (e, x, px, how)))
                rows.append(row)
            df = pd.DataFrame(rows)
            df["m"], df["r"] = np.nan, np.nan
            for side in (1, -1):
                if trades[side]:
                    m, r = single(p, ex, side, [t for _i, t in trades[side]])
                    ix = [i for i, _t in trades[side]]
                    df.loc[ix, "m"], df.loc[ix, "r"] = m, r
            df["f_ALL"] = True
            df["f_TIGHT"] = df["width"] < df["width"].shift(1).rolling(20, min_periods=20).median()
            df["f_GAP"] = df["gap"] > 0.5
            frames.append(df)
            print(f"{inst} {length}m: {int(df.exists.sum())} trades", flush=True)
    pd.concat(frames, ignore_index=True).to_csv(OUT / "s2.csv", index=False)


def stage_s5() -> None:
    h._init_worker()
    days, st = load_days(), Store()
    frames = []
    for inst in INDICES:
        p = prep(inst)
        ex = Exact(p, inst, days, st)
        atr60 = ms.tf_age_atr(p, "60m", np.arange(p.n))[1]
        rows, ent = [], []
        for s in sessions(p):
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            e = at_minute(p.minute, a, b, mins("09:20"))
            if e < 0 or not atr60[e] > 0:
                continue
            day_c = float(p.c[int(p.sess_last[s])])
            rows.append({**base_row(p, inst, s), "oc": (day_c / p.o[a] - 1) * 1e4, "exists": True,
                         "side": 0})  # fmt: skip
            ent.append((rows[-1], e))
        df = pd.DataFrame(rows)
        for k, v in both_sides(p, ex, atr60, ent).items():
            df[k] = v
        frames.append(df)
        print(f"{inst}: {len(df)} sessions", flush=True)
    pd.concat(frames, ignore_index=True).to_csv(OUT / "s5.csv", index=False)


def stage_s6() -> None:
    h._init_worker()
    days, st = load_days(), Store()
    frames = []
    for inst in INDICES:
        p = prep(inst)
        ex = Exact(p, inst, days, st)
        atr60 = ms.tf_age_atr(p, "60m", np.arange(p.n))[1]
        rows, ent = [], []
        for s in sessions(p):
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            r30 = sig_s6(p.o, p.c, p.minute, a, b)
            e = at_minute(p.minute, a, b, mins("09:45"))
            if r30 is None or r30 == 0 or e < 0 or not atr60[e] > 0:
                continue
            side = int(np.sign(r30))
            rest_h, rest_l = float(p.h[e:b].max()), float(p.l[e:b].min())
            rows.append({**base_row(p, inst, s), "r30": r30, "side": side, "exists": True,
                         "rest_range": (rest_h - rest_l) / p.o[e] * 1e4,
                         "rest_ret": side * (p.c[b - 1] / p.o[e] - 1) * 1e4})  # fmt: skip
            ent.append((rows[-1], e))
        df = pd.DataFrame(rows)
        for k, v in both_sides(p, ex, atr60, ent).items():
            df[k] = v
        frames.append(df)
        print(f"{inst}: {len(df)} sessions", flush=True)
    pd.concat(frames, ignore_index=True).to_csv(OUT / "s6.csv", index=False)


# ───────────────────────────── the near contract at the money: S3, S4 ──────


class Near:
    """The near monthly contract (no roll: on its last day it expires today) at a strike K.

    Model reading: Black-76 on the index (the harness's convention: forward = index), the
    previous session's India VIX x the Lab's multiplier, time to the expiry found in the store.
    Real reading: the at-the-money CALL's real one-minute bar of the store gives the implied
    volatility of that minute (forward = index x exp((r - q) T)); a leg at strike K is priced
    at that volatility, and a PUT by put-call parity at the same volatility. Where the CALL at
    K is itself the at-the-money one (rel 0), this is its real price. Missing bar at an entry
    or exit: the first one within 3 minutes; none: unpriced."""

    def __init__(self, p: h.Prep, inst: str, days: pd.DataFrame) -> None:
        self.p = p
        cfg = bt_cfg()
        self.r = cfg["r"]
        self.step = float(h._cache.spec(inst).strike_step)
        z = np.load(OUT / f"atm_{inst}.npz")
        ep = (p.ns // 1_000_000_000).astype(np.int64)
        pos = pd.Series(np.arange(len(z["ts"])), index=z["ts"])
        pos = pos[~pos.index.duplicated()]
        idx = pos.reindex(ep).to_numpy()
        has = np.isfinite(idx)
        ii = np.where(has, idx, 0).astype(np.int64)
        self.has = has
        self.k = np.where(has, z["strike"][ii], np.nan)
        dd = days[days.inst == inst]
        near = dict(zip(dd["date"], dd["expiry"], strict=True))
        exp_ns = np.array([
            pd.Timestamp(dt.datetime.combine(near[d], dt.time(15, 30)), tz="Asia/Kolkata").value
            if d in near else -1 for d in p.sess_dates
        ], dtype=np.int64)  # fmt: skip
        self.exp_ns = exp_ns
        t_open = (exp_ns[p.sess] - p.ns) / 1e9 / SECONDS_PER_YEAR
        t_open = np.where(exp_ns[p.sess] > 0, t_open, np.nan)
        self.t_open = np.maximum(t_open, 60.0 / SECONDS_PER_YEAR)
        self.t_close = np.maximum(t_open - 60.0 / SECONDS_PER_YEAR, 60.0 / SECONDS_PER_YEAR)
        fo = p.o * np.exp((self.r - Q_DIV) * self.t_open)
        fc = p.c * np.exp((self.r - Q_DIV) * self.t_close)
        co = np.where(has, z["open"][ii], np.nan)
        cc = np.where(has, z["close"][ii], np.nan)
        self.sig_o = implied_vol(co, fo, self.k, self.t_open, self.r)
        self.sig_c = implied_vol(cc, fc, self.k, self.t_close, self.r)
        self.iv_model = p.iv[p.sess]
        # a bad or stale print: a volatility outside 0.25 to 4 times the model's is dropped
        for a in (self.sig_o, self.sig_c):
            a[(a < 0.25 * self.iv_model) | (a > 4.0 * self.iv_model)] = np.nan
        self.dropped = float(np.mean(has & ~np.isfinite(self.sig_o)))
        # closes carried forward inside a session, for watching a stop on the premium
        sc = pd.Series(self.sig_c).groupby(p.sess).ffill().to_numpy()
        self.sig_c_ff = sc

    def strike(self, j: int) -> tuple[float, bool]:
        """Dhan's at-the-money strike of minute j (or of the first traded minute within 3),
        else the strike nearest the index open (model only)."""
        p = self.p
        last = int(p.sess_last[p.sess[j]])
        for w in range(MAX_WAIT + 1):
            if j + w <= last and self.has[j + w]:
                return float(self.k[j + w]), True
        return float(np.round(p.o[j] / self.step) * self.step), False

    def sig_at(self, j: int) -> float:
        """Real volatility for a fill at the open of minute j (first traded minute within 3)."""
        p = self.p
        last = int(p.sess_last[p.sess[j]])
        for w in range(MAX_WAIT + 1):
            if j + w <= last and np.isfinite(self.sig_o[j + w]):
                return float(self.sig_o[j + w])
        return np.nan

    def value(self, js, k: float, call: bool, real: bool, at: str, sig=None):  # noqa: ANN001, ANN201
        p = self.p
        js = np.asarray(js)
        s_ = p.o[js] if at == "open" else p.c[js]
        t = self.t_open[js] if at == "open" else self.t_close[js]
        if real:
            sig = (self.sig_o[js] if at == "open" else self.sig_c_ff[js]) if sig is None else sig
            f = s_ * np.exp((self.r - Q_DIV) * t)
        else:
            sig = self.iv_model[js]
            f = s_
        return b76(f, k, t, sig, self.r, call)


def leg_book(p: h.Prep, e_px: np.ndarray, x_px: np.ndarray) -> tuple[float, float]:
    net, base = book(p, np.asarray(e_px, float), np.asarray(x_px, float))
    return float(np.sum(net)), float(np.sum(base))


def straddle(nr: Near, j0: int, x_end: int, k: float, real: bool, stop: bool) -> dict | None:
    """The pair (call + put at K) bought at the open of j0 and sold at the open of x_end, or
    earlier at the next open after a minute closed with the pair at -30% or +60%."""
    p = nr.p
    if real:
        s0 = nr.sig_at(j0)
        sx = nr.sig_at(x_end)
        if not np.isfinite(s0):
            return None
        e = [nr.value([j0], k, c, True, "open", np.array([s0]))[0] for c in (True, False)]
    else:
        e = [nr.value([j0], k, c, False, "open")[0] for c in (True, False)]
    x = x_end
    why = "time"
    if stop:
        js = np.arange(j0, x_end)
        v = sum(nr.value(js, k, c, real, "close") for c in (True, False))
        ratio = v / (e[0] + e[1]) - 1
        hit = np.flatnonzero((ratio <= -0.30) | (ratio >= 0.60))
        if len(hit) and js[hit[0]] + 1 < x_end:
            x = int(js[hit[0]] + 1)
            why = "stop" if ratio[hit[0]] <= -0.30 else "take"
            if real:
                sx = nr.sig_at(x)
    if real:
        if not np.isfinite(sx):
            return None
        xs = [nr.value([x], k, c, True, "open", np.array([sx]))[0] for c in (True, False)]
    else:
        xs = [nr.value([x], k, c, False, "open")[0] for c in (True, False)]
    net, base = leg_book(p, e, xs)
    return {"ret": net / base * 100.0, "why": why, "held": x - j0,
            "gross": (xs[0] + xs[1]) / (e[0] + e[1]) * 100 - 100}  # fmt: skip


def single_near(nr: Near, j0: int, x_end: int, k: float, call: bool, real: bool) -> dict | None:
    """One leg bought at the open of j0, sold at the open of x_end or at the next open after
    a minute closed with the premium at -40% of the entry."""
    p = nr.p
    if real:
        s0 = nr.sig_at(j0)
        if not np.isfinite(s0):
            return None
        e = nr.value([j0], k, call, True, "open", np.array([s0]))[0]
    else:
        e = nr.value([j0], k, call, False, "open")[0]
    js = np.arange(j0, x_end)
    v = nr.value(js, k, call, real, "close")
    hit = np.flatnonzero(v / e - 1 <= -0.40)
    x = int(js[hit[0]] + 1) if len(hit) and js[hit[0]] + 1 < x_end else x_end
    if real:
        sx = nr.sig_at(x)
        if not np.isfinite(sx):
            return None
        xv = nr.value([x], k, call, True, "open", np.array([sx]))[0]
    else:
        xv = nr.value([x], k, call, False, "open")[0]
    net, base = leg_book(p, [e], [xv])
    return {"ret": net / base * 100.0, "why": "stop" if x < x_end else "time"}


def vix_minutes() -> dict:
    v = h._cache.bars("INDIAVIX", "1m")
    mi = v.index.hour * 60 + v.index.minute
    out = {}
    for tag, mm in (("a", mins("09:20")), ("b", mins("15:20"))):
        sel = v[mi == mm]
        out[tag] = dict(zip(sel.index.date, sel["open"].astype(float), strict=True))
    return out


def stage_s3() -> None:
    h._init_worker()
    days = load_days()
    vm = vix_minutes()
    frames = []
    for inst in INDICES:
        p = prep(inst)
        nr = Near(p, inst, days)
        rows = []
        for s in sessions(p):
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            if nr.exp_ns[s] < 0:
                continue
            x1520 = at_minute(p.minute, a, b, mins("15:20"))
            x1200 = at_minute(p.minute, a, b, mins("12:00"))
            d = p.sess_dates[s]
            va, vb = vm["a"].get(d), vm["b"].get(d)
            for at in ("09:20", "09:45"):
                j0 = at_minute(p.minute, a, b, mins(at))
                if j0 < 0 or x1520 < 0 or x1200 < 0:
                    continue
                k, real_k = nr.strike(j0)
                for xn, x_end, stop in (("15:20", x1520, False), ("12:00", x1200, False),
                                        ("stop", x1520, True)):  # fmt: skip
                    mo = straddle(nr, j0, x_end, k, False, stop)
                    re = straddle(nr, j0, x_end, k, True, stop) if real_k else None
                    rows.append({**base_row(p, inst, s), "variant": f"{at} {xn}", "side": 0,
                                 "exists": True, "m": mo["ret"], "m_why": mo["why"],
                                 "r": re["ret"] if re else np.nan,
                                 "r_why": re["why"] if re else "", "k": k,
                                 "move": abs(p.o[x_end] / p.o[j0] - 1) * 1e4,
                                 "vix_d": (vb / va - 1) * 100 if va and vb else np.nan})  # fmt: skip
        df = pd.DataFrame(rows)
        df["f_ALL"] = True
        frames.append(df)
        print(f"{inst}: {len(df)} rows, real {df.r.notna().mean():.0%}, minutes with a bar "
              f"dropped as a bad print {nr.dropped:.2%}", flush=True)  # fmt: skip
    pd.concat(frames, ignore_index=True).to_csv(OUT / "s3.csv", index=False)


def stage_s4() -> None:
    h._init_worker()
    days = load_days()
    frames = []
    for inst in INDICES:
        p = prep(inst)
        nr = Near(p, inst, days)
        dl = day_lookup(days, inst)
        rows = []
        for s in sessions(p):
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            dr = dl.get(p.sess_dates[s])
            x_end = int(p.xi[s])
            if dr is None or nr.exp_ns[s] < 0 or p.xclose[s]:
                continue
            for at in ("13:30", "14:00", "14:30"):
                dist = sig_s4(p.c, p.minute, a, b, float(p.o[a]), float(dr["atr_d"]), at)
                j0 = at_minute(p.minute, a, b, mins(at))
                if dist is None or j0 < 0:
                    continue
                for thr in (0.25, 0.5):
                    side = int(np.sign(dist)) if abs(dist) > thr else 0
                    row = {**base_row(p, inst, s), "variant": f"{at} {thr:g}", "side": side,
                           "exists": side != 0, "dist": dist}  # fmt: skip
                    if side:
                        k, real_k = nr.strike(j0)
                        mo = single_near(nr, j0, x_end, k, side == 1, False)
                        re = single_near(nr, j0, x_end, k, side == 1, True) if real_k else None
                        row.update({"m": mo["ret"], "r": re["ret"] if re else np.nan,
                                    "m_why": mo["why"]})  # fmt: skip
                    rows.append(row)
        df = pd.DataFrame(rows)
        df["f_ALL"] = True
        frames.append(df)
        print(f"{inst}: {int(df.exists.sum())} trades on all days", flush=True)
    pd.concat(frames, ignore_index=True).to_csv(OUT / "s4.csv", index=False)


# ───────────────────────────── stage: truncate (no look-ahead) ──────────────


def stage_truncate() -> None:
    """Each signal recomputed on data cut right after its signal bar (60 random signals per
    setup and index group), and the day types recomputed on daily bars cut before the day."""
    h._init_worker()
    days = load_days()
    rng = np.random.default_rng(92)
    lines = []
    for inst in ("NIFTY_OPT", "SENSEX_OPT"):
        p = prep(inst)
        dl = day_lookup(days, inst)
        ss = sessions(p)
        same = {"S1": 0, "S2": 0, "S4": 0, "S6": 0}
        for s in rng.choice(ss, 60, replace=False):
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            dr = dl[p.sess_dates[s]]
            j59 = at_minute(p.minute, a, b, mins("14:59"))
            same["S1"] += sig_s1(p.c, p.minute, a, b, dr["prev_close"]) == sig_s1(
                p.c, p.minute, a, j59 + 1, dr["prev_close"])  # fmt: skip
            full = sig_s2(p.h, p.l, p.c, p.minute, a, b, 15)
            j = full[2] if full and full[2] >= 0 else at_minute(p.minute, a, b, mins("12:59"))
            same["S2"] += full == sig_s2(p.h, p.l, p.c, p.minute, a, j + 1, 15)
            j29 = at_minute(p.minute, a, b, mins("13:29"))
            same["S4"] += sig_s4(p.c, p.minute, a, b, p.o[a], dr["atr_d"], "13:30") == sig_s4(
                p.c, p.minute, a, j29 + 1, p.o[a], dr["atr_d"], "13:30")  # fmt: skip
            j44 = at_minute(p.minute, a, b, mins("09:44"))
            same["S6"] += sig_s6(p.o, p.c, p.minute, a, b) == sig_s6(p.o, p.c, p.minute, a, j44 + 1)
        lines.append(f"{inst}: signals equal on data cut after the signal bar, of 60: {same}")
        # day types: classify the daily bars cut before the day
        key = h._cache.spec(inst).series_key
        daily = h._cache.bars(key, "1d")
        dd = np.array([x.date() for x in daily.index])
        ok = 0
        picks = rng.choice(len(ss), 60, replace=False)
        for i in picks:
            d = p.sess_dates[ss[i]]
            cut = daily[dd < d]
            st_ = ST.classify(cut)
            rg = (cut["high"] - cut["low"]).to_numpy(float)
            nr7 = bool(rg[-1] <= rg[-7:-1].min())
            ok += (st_["size"].iloc[-1] == dl[d]["size_prev"]) and nr7 == bool(dl[d]["nr7"])
        lines.append(
            f"{inst}: size state and NR7 equal on daily bars cut before the day: {ok} of 60"
        )
    text = "\n".join(lines)
    (OUT / "truncate.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


# ───────────────────────────── cells, selection, placebo ────────────────────

FLIP = {"S1", "S5", "S6"}
TWO_SIDED_REAL = {"S3", "S4"}  # real reading on priced trades only (the near contract)


def cell_specs() -> list[dict]:
    out = []
    for c in ("B", "BIG", "B+BIG"):
        out.append({"setup": "S1", "variant": "15:00", "cond": c, "side": "sig"})
    for v in ("15m", "30m"):
        for c in ("ALL", "ACTIVE-EXP", "NR7", "TIGHT", "GAP"):
            out.append({"setup": "S2", "variant": v, "cond": c, "side": "sig"})
    for at in ("09:20", "09:45"):
        for x in ("15:20", "12:00", "stop"):
            for c in DAY_TYPES:
                out.append({"setup": "S3", "variant": f"{at} {x}", "cond": c, "side": "sig"})
    for at in ("13:30", "14:00", "14:30"):
        for thr in ("0.25", "0.5"):
            out.append({"setup": "S4", "variant": f"{at} {thr}", "cond": "EXPIRY", "side": "sig"})
    out.append({"setup": "S5", "variant": "09:20", "cond": "VIX-UP", "side": "+1"})
    out.append({"setup": "S5", "variant": "09:20", "cond": "VIX-DOWN", "side": "-1"})
    for c in ("HIGH", "LOW"):
        out.append({"setup": "S6", "variant": "09:45", "cond": c, "side": "sig"})
    return out


def load_tables() -> dict[str, pd.DataFrame]:
    days = load_days()
    fcols = [c for c in days.columns if c.startswith("f_")]
    dk = days[["inst", "date", *fcols, "vix_chg", "turn_ratio", "dte", "vix_prev"]]
    out = {}
    for name in ("s1", "s2", "s3", "s4", "s5", "s6"):
        f = OUT / f"{name}.csv"
        if not f.exists():
            continue
        t = pd.read_csv(f, parse_dates=["date"])
        t["date"] = t["date"].dt.date
        t = t.drop(columns=[c for c in fcols if c in t.columns and c != "f_ALL"])
        t = t.drop(columns=["f_ALL"], errors="ignore").merge(dk, on=["inst", "date"], how="left")
        for c in fcols:
            t[c] = t[c].fillna(False).astype(bool)
        if "variant" not in t.columns:
            t["variant"] = {"s1": "15:00", "s5": "09:20", "s6": "09:45"}[name]
        if name == "s6":
            t["f_HIGH"] = t["turn_ratio"] > 1.2
            t["f_LOW"] = t["turn_ratio"] < 0.8
        for c in [c for c in t.columns if c.startswith("f_")]:
            t[c] = t[c].fillna(False).astype(bool)
        t["setup"] = name.upper()
        t["wd"] = [d.weekday() for d in t["date"]]
        out[name.upper()] = t
    return out


def trades_of(t: pd.DataFrame, spec: dict, side_override: np.ndarray | None = None) -> pd.DataFrame:
    """The trades of one cell from a setup table: columns inst, dayoff, period, m, r, real."""
    q = t[(t["variant"] == spec["variant"]) & t["f_" + spec["cond"]] & t["exists"].astype(bool)]
    if spec["setup"] in FLIP:
        if side_override is not None:
            side = side_override[q.index.to_numpy()]
        elif spec["side"] == "sig":
            side = q["side"].to_numpy()
        else:
            side = np.full(len(q), int(spec["side"]))
        m = np.where(side == 1, q["m_l"], q["m_s"])
        r = np.where(side == 1, q["r_l"], q["r_s"])
    else:
        m, r = q["m"].to_numpy(float), q["r"].to_numpy(float)
    d = pd.DataFrame({"inst": q["inst"].to_numpy(), "date": q["date"].to_numpy(),
                      "dayoff": q["dayoff"].to_numpy(), "period": q["period"].to_numpy(),
                      "m": m, "r": r})  # fmt: skip
    d = d[np.isfinite(d["m"])]
    if spec["setup"] in TWO_SIDED_REAL:
        d["real"] = d["r"]
    else:
        d["real"] = d["r"].where(np.isfinite(d["r"]), d["m"] - REAL_GAP)
    return d


def stats_of(d: pd.DataFrame) -> dict:
    out = {"n": len(d), "days": d["dayoff"].nunique()}
    x = d["m"].to_numpy(float)
    out["mean"] = x.mean() if len(x) else np.nan
    out["t"] = day_t(x, d["dayoff"]) if len(x) >= 3 else np.nan
    win, loss = x[x > 0], x[x <= 0]
    out["win"] = (x > 0).mean() * 100 if len(x) else np.nan
    out["payoff"] = (
        win.mean() / abs(loss.mean()) if len(win) and len(loss) and loss.mean() else np.nan
    )
    for k, nm in enumerate(NAMES):
        q = d[d["period"] == k]
        out[f"{nm}_n"], out[f"{nm}_days"] = len(q), q["dayoff"].nunique()
        out[f"{nm}_m"] = q["m"].mean() if len(q) else np.nan
        out[f"{nm}_t"] = day_t(q["m"], q["dayoff"]) if len(q) >= 3 else np.nan
        rq = q["real"].dropna()
        out[f"{nm}_real"] = rq.mean() if len(rq) else np.nan
    rr = d["real"].dropna()
    out["real_n"] = len(rr)
    out["real"] = rr.mean() if len(rr) else np.nan
    out["real_t"] = day_t(rr, d.loc[rr.index, "dayoff"]) if len(rr) >= 3 else np.nan
    out["priced"] = np.isfinite(d["r"]).mean() * 100 if len(d) else np.nan
    return out


def judge(st_: dict) -> dict:
    ex = st_["TUNE_n"] >= MIN_EX_TRADES and st_["TUNE_days"] >= MIN_EX_DAYS
    disc = ex and st_["TUNE_m"] > 0 and st_["TUNE_t"] >= 2.5 and st_["TUNE_m"] >= 1.0
    conf = disc and st_["CHOOSE_m"] > 0 and st_["CHOOSE_t"] >= 1.0 and st_["CHOOSE_n"] >= 20
    pas = (conf and st_["TEST_m"] > 0 and st_["TEST_n"] >= 20
           and np.isfinite(st_["real"]) and st_["real"] > 0)  # fmt: skip
    avoid = ex and st_["TUNE_t"] <= -2.5 and st_["CHOOSE_m"] < 0 and st_["TEST_m"] < 0
    return {"examined": ex, "discovered": disc, "confirmed": conf, "passed": pas, "avoid": avoid}


def run_cells(tabs: dict, sides: dict | None = None) -> list[dict]:
    rows = []
    for spec in cell_specs():
        t = tabs.get(spec["setup"])
        if t is None:
            continue
        so = sides.get(spec["setup"]) if sides else None
        d = trades_of(t, spec, so)
        readings = [("model", d)]
        if spec["setup"] in TWO_SIDED_REAL:  # also selected on the real reading itself
            dr = d[np.isfinite(d["real"])].copy()
            dr["m"] = dr["real"]
            readings.append(("real", dr))
        for reading, dd in readings:
            for inst in [*INDICES, "POOL"]:
                q = dd if inst == "POOL" else dd[dd["inst"] == inst]
                st_ = stats_of(q)
                rows.append({**spec, "reading": reading, "inst": inst.replace("_OPT", ""),
                             **st_, **judge(st_)})  # fmt: skip
    return rows


def stage_select() -> None:
    tabs = load_tables()
    df = pd.DataFrame(run_cells(tabs))
    df.to_csv(OUT / "cells.csv", index=False)
    for reading, g in df.groupby("reading"):
        c = g[["examined", "discovered", "confirmed", "passed", "avoid"]].sum()
        print(f"{reading} cells {len(g)}: " + ", ".join(f"{k} {int(v)}" for k, v in c.items()))


def shifted(tabs: dict, rng: np.random.Generator) -> tuple[dict, dict]:
    """The placebo: every condition label moved by a random number of sessions inside its
    instrument and calendar year (whole days keep their trades); S1, S5, S6 a random side."""
    out, sides = {}, {}
    for name, t in tabs.items():
        t = t.copy()
        fcols = [c for c in t.columns if c.startswith("f_") and c != "f_ALL"]
        src = np.arange(len(t))
        for (_inst, _y), g in t.groupby([t["inst"], [d.year for d in t["date"]]]):
            dates = sorted(set(g["date"]))
            n = len(dates)
            if n < 2:
                continue
            k = int(rng.integers(1, n))
            back = {dates[i]: dates[(i - k) % n] for i in range(n)}
            pos = {(v, d): i for i, v, d in zip(g.index, g["variant"], g["date"], strict=True)}
            for i, v, d in zip(g.index, g["variant"], g["date"], strict=True):
                src[i] = pos.get((v, back[d]), -1)
        ok = src >= 0
        vals = t[fcols].to_numpy()
        new = np.zeros_like(vals, dtype=bool)
        new[ok] = vals[src[ok]]
        t[fcols] = new
        if name in FLIP:
            sides[name] = rng.choice([-1, 1], size=len(t))
        out[name] = t
    return out, sides


def stage_placebo() -> None:
    tabs = load_tables()
    rng = np.random.default_rng(2611)
    res = []
    for run in range(20):
        t0 = time.time()
        tb, sd = shifted(tabs, rng)
        df = pd.DataFrame(run_cells(tb, sd))
        c = df[df.reading == "model"][["examined", "discovered", "confirmed", "passed"]].sum()
        res.append({"run": run, **{k: int(v) for k, v in c.items()}})
        for setup, g in df.groupby(df.setup + np.where(df.reading == "real", "r", "")):
            for k in ("discovered", "confirmed", "passed"):
                res[-1][f"{setup}_{k}"] = int(g[k].sum())
        print(f"placebo {run}: {res[-1]} {time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(res).to_csv(OUT / "placebo.csv", index=False)


# ───────────────────────────── stage: report ────────────────────────────────


def f2(v: float) -> str:
    return "nan" if not np.isfinite(v) else f"{v:+.2f}"


def cell_line(r: pd.Series) -> str:
    per = " / ".join(f2(r[f"{n}_m"]) for n in NAMES)
    rper = " / ".join(f2(r[f"{n}_real"]) for n in NAMES)
    flags = "".join(k[0].upper() for k in ("discovered", "confirmed", "passed") if r[k])
    flags += " AVOID" if r["avoid"] else ""
    return (f"{r['inst']:<10}{r['variant']:<14}{r['cond']:<11}{r['n']:>5}{r['days']:>5} "
            f"{f2(r['mean']):>6} ({r['t']:+.1f}) {r['win']:4.0f}% {r['payoff']:4.2f} | {per:<22}"
            f"| real {f2(r['real']):>6} ({r['real_t']:+.1f}) pr {r['priced']:3.0f}% | {rper:<22}"
            f" TUNE n {r['TUNE_n']}, t {r['TUNE_t']:+.1f} {flags}")  # fmt: skip


HEAD = ("index     variant       cond          n days  model %/tr (t)  win  pay | "
        "TUNE / CHOOSE / TEST  | real %/tr (t), priced | real T / C / T")  # fmt: skip


def stage_report() -> None:
    L: list[str] = []  # noqa: N806
    w = L.append
    allc = pd.read_csv(OUT / "cells.csv")
    cells = allc[allc.reading == "model"]
    realc = allc[allc.reading == "real"]
    tabs = load_tables()
    w("COVERAGE of the real store (near monthly CALL at the money, days with bars)")
    cov = pd.read_csv(OUT / "store_coverage.csv")
    cv = cov[(cov.code == 1) & (cov.opt == "CE")].pivot(index="und", columns="year",
                                                         values="with_bars")  # fmt: skip
    w(cv.to_string())
    ex = pd.read_csv(OUT / "expiries.csv")
    w("expiry days found in the store / filled: " + ", ".join(
        f"{i} {int((g.how == 'store').sum())}/{int((g.how == 'filled').sum())}"
        for i, g in ex.groupby("inst")))  # fmt: skip
    w("")
    w("COUNTS against luck (cells = index or pool x setup variant x condition)")
    c = cells[["examined", "discovered", "confirmed", "passed", "avoid"]].sum()
    w("   real: " + ", ".join(f"{k} {int(v)}" for k, v in c.items()))
    w(f"   arithmetic luck: examined x 0.6% x 16% x 50% = "
      f"{c['examined'] * 0.006:.2f} discovered, {c['examined'] * 0.006 * 0.16:.2f} confirmed, "
      f"{c['examined'] * 0.006 * 0.16 * 0.5:.3f} passed")  # fmt: skip
    pf = OUT / "placebo.csv"
    if pf.exists():
        pl = pd.read_csv(pf)
        w("   placebo (20 shifts) mean: " + ", ".join(
            f"{k} {pl[k].mean():.2f} (max {pl[k].max()})"
            for k in ("examined", "discovered", "confirmed", "passed")))  # fmt: skip
        for st_ in ("S1", "S2", "S3", "S4", "S5", "S6"):
            if f"{st_}_discovered" in pl:
                real_d = cells[cells.setup == st_]
                w(f"   {st_}: real disc/conf/pass {int(real_d.discovered.sum())}/"
                  f"{int(real_d.confirmed.sum())}/{int(real_d.passed.sum())}; placebo mean "
                  + "/".join(f"{pl[f'{st_}_{k}'].mean():.2f}"
                             for k in ("discovered", "confirmed", "passed")))  # fmt: skip
    for st_ in ("S1", "S2", "S3", "S4", "S5", "S6"):
        sub = cells[cells.setup == st_]
        if sub.empty:
            continue
        w("")
        w(f"{st_}: {len(sub)} cells, {int(sub.examined.sum())} examined; sorted by TUNE mean")
        w(HEAD)
        for _i, r in sub.sort_values("TUNE_m", ascending=False).iterrows():
            w(cell_line(r))
    w("")
    w("S3 and S4 SELECTED ON THE REAL READING ITSELF (priced trades only)")
    for st_ in ("S3", "S4"):
        sub = realc[realc.setup == st_]
        c = sub[["examined", "discovered", "confirmed", "passed", "avoid"]].sum()
        line = f"   {st_}: {len(sub)} cells, " + ", ".join(f"{k} {int(v)}" for k, v in c.items())
        if pf.exists() and f"{st_}r_discovered" in pl:
            line += "; placebo mean " + "/".join(
                f"{pl[f'{st_}r_{k}'].mean():.2f}" for k in ("discovered", "confirmed", "passed")
            )
        w(line)
        w(HEAD)
        for _i, r in sub.sort_values("TUNE_m", ascending=False).head(25).iterrows():
            w(cell_line(r))
        for _i, r in sub[sub.avoid].iterrows():
            w("AVOID " + cell_line(r))
    w("")
    w("DISCOVERED cells: the real-premium reading first")
    for _i, r in cells[cells.discovered].iterrows():
        w(f"   {r.setup} {r.inst} {r.variant} {r.cond}: real {f2(r.real)} (t {r.real_t:+.1f}, "
          f"priced {r.priced:.0f}%), model {f2(r['mean'])}; "
          f"{' / '.join(f2(r[f'{n}_m']) for n in NAMES)}")  # fmt: skip
    # neighbours of passed cells
    for _i, r in cells[cells.passed].iterrows():
        w(f"   NEIGHBOURS of {r.setup} {r.inst} {r.variant} {r.cond}:")
        nb = cells[(cells.setup == r.setup) & (((cells.cond == r.cond) & (cells.inst != r.inst))
                   | ((cells.inst == r.inst) & (cells.cond == r.cond)))]  # fmt: skip
        for _j, q in nb.iterrows():
            w("      " + cell_line(q))
    facts(tabs, w)
    text = "\n".join(L) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)


def facts(tabs: dict, w) -> None:  # noqa: ANN001
    if "S3" in tabs:
        t = tabs["S3"]
        w("")
        w("S3 PLAIN FACT: the pair bought 09:20, sold 15:20; mean % a trade (model | real on "
          "priced days, n), POOL")  # fmt: skip
        q = t[t.variant == "09:20 15:20"]
        for c in DAY_TYPES:
            g = q[q["f_" + c]]
            rr = g.r.dropna()
            w(f"   {c:<11} model {g.m.mean():+.2f} (n {len(g)}) | real {rr.mean():+.2f} "
              f"(t {day_t(rr, g.loc[rr.index, 'dayoff']):+.1f}, n {len(rr)}) | "
              f"index move 09:20 to 15:20 {g.move.mean():.0f} bp")  # fmt: skip
        for inst, g in q.groupby("inst"):
            rr = g.r.dropna()
            w(f"   {inst:<14} ALL: model {g.m.mean():+.2f} | real {rr.mean():+.2f} (n {len(rr)}) "
              f"T/C/T real {' / '.join(f2(rr[g.loc[rr.index, 'period'] == k].mean()) for k in range(3))}")  # fmt: skip
        g = q.dropna(subset=["r", "vix_d"])
        w(
            f"   correlation of the real result with India VIX's change 09:20 to 15:20 "
            f"(same day, not tradable): {np.corrcoef(g.r, g.vix_d)[0, 1]:+.2f} (n {len(g)})"
        )
        w(f"   correlation of the real result with the index move: "
          f"{np.corrcoef(g.r, g.move)[0, 1]:+.2f}; with the previous day's VIX level: "
          f"{np.corrcoef(g.r, g.vix_prev)[0, 1]:+.2f}; with its change: "
          f"{np.corrcoef(g.r, g.vix_chg.fillna(0))[0, 1]:+.2f}")  # fmt: skip
        for lo, hi in ((-99, -5), (-5, 0), (0, 5), (5, 99)):
            gg = g[(g.vix_d > lo) & (g.vix_d <= hi)]
            w(f"   VIX change today {lo:+d}% to {hi:+d}%: real {gg.r.mean():+.2f} (n {len(gg)}),"
              f" model {gg.m.mean():+.2f}")  # fmt: skip
        g2 = q.dropna(subset=["r"])
        w("   by previous-day VIX level (quintile): " + ", ".join(
            f"{a:.1f}-{b:.1f}: real {x.r.mean():+.2f}, move {x.move.mean():.0f} bp"
            for (a, b), x in [((x.vix_prev.min(), x.vix_prev.max()), x) for _k, x in
                              g2.groupby(pd.qcut(g2.vix_prev, 5), observed=True)]))  # fmt: skip
    if "S5" in tabs:
        t = tabs["S5"].dropna(subset=["vix_chg"])
        w("")
        w("S5 PLAIN FACT: index open-to-close return by decile of the previous day's VIX change "
          "(four indices, bp)")  # fmt: skip
        t = t.assign(dec=pd.qcut(t.vix_chg, 10, labels=False))
        for k, g in t.groupby("dec"):
            w(f"   decile {k + 1:>2} VIX {g.vix_chg.min() * 100:+5.1f}% to "
              f"{g.vix_chg.max() * 100:+5.1f}%: mean {g.oc.mean():+6.1f} "
              f"(t {day_t(g.oc, g.dayoff):+.1f}), |move| {g.oc.abs().mean():5.1f}, n {len(g)}")  # fmt: skip
        w(f"   correlation of |open to close| with the previous day's VIX level "
          f"{np.corrcoef(t.oc.abs(), t.vix_prev)[0, 1]:+.2f}, with its change "
          f"{np.corrcoef(t.oc.abs(), t.vix_chg)[0, 1]:+.2f}; of the signed return with the "
          f"change {np.corrcoef(t.oc, t.vix_chg)[0, 1]:+.2f}")  # fmt: skip
    if "S6" in tabs:
        t = tabs["S6"].dropna(subset=["turn_ratio"])
        w("")
        w("S6 PLAIN FACT: rest of day after 09:45 by quintile of the first-half-hour turnover "
          "ratio (four indices, bp; return signed by the first half hour)")  # fmt: skip
        for k, g in t.groupby(pd.qcut(t.turn_ratio, 5), observed=True):
            w(f"   ratio {k.left:4.2f} to {k.right:4.2f}: range {g.rest_range.mean():5.1f}, "
              f"return {g.rest_ret.mean():+5.1f} (t {day_t(g.rest_ret, g.dayoff):+.1f}), n {len(g)}")  # fmt: skip
    w("")
    w("S7 DESCRIPTIVE: model % a trade (real like-for-like) by weekday and days to expiry, POOL")
    probes = [{"setup": "S1", "variant": "15:00", "cond": "ALL", "side": "sig"},
              {"setup": "S2", "variant": "15m", "cond": "ALL", "side": "sig"},
              {"setup": "S3", "variant": "09:20 15:20", "cond": "ALL", "side": "sig"},
              {"setup": "S4", "variant": "13:30 0.25", "cond": "ALL", "side": "sig"},
              {"setup": "S5", "variant": "09:20", "cond": "VIX-UP", "side": "+1"},
              {"setup": "S5", "variant": "09:20", "cond": "VIX-DOWN", "side": "-1"},
              {"setup": "S6", "variant": "09:45", "cond": "ALL", "side": "sig"}]  # fmt: skip
    for spec in probes:
        t = tabs.get(spec["setup"])
        if t is None:
            continue
        t = t.assign(f_ALL=True)
        d = trades_of(t, spec)
        meta = t[["inst", "date", "wd", "dte"]].drop_duplicates(["inst", "date"])
        d = d.merge(meta, on=["inst", "date"], how="left")
        wd = " ".join(f"{'MTWTFSS'[k]} {g.m.mean():+.2f} ({g.real.mean():+.2f})"
                      for k, g in d.groupby("wd"))  # fmt: skip
        b = pd.cut(d.dte, [-1, 0, 1, 5, 10, 40], labels=["0", "1", "2-5", "6-10", "11+"])
        de = " ".join(f"{k} {g.m.mean():+.2f} ({g.real.mean():+.2f})"
                      for k, g in d.groupby(b, observed=True))  # fmt: skip
        w(f"   {spec['setup']} {spec['variant']} {spec['cond']}: {wd} | days to expiry {de}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stages = sys.argv[1:] or ["report"]
    for name in stages:
        t0 = time.time()
        globals()["stage_" + name]()
        print(f"stage {name}: {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
