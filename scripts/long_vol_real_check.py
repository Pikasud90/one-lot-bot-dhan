"""US-098 independent check of the long straddle on real puts. Own code, nothing of the study's
functions: it reads the fetched file and the app's store through SQLite `mode=ro`, the index
bars of `data/onelot.db` through SQLite `mode=ro`, and `config/costs.yaml`. No network.

    PYTHONPATH=src .venv/bin/python scripts/long_vol_real_check.py [MAIN_CHECKOUT [OUTFILE]]

Prints: (1) the fetched file, (2) the pooled 09:20 to 15:20 pair on both real legs with own
day types, (3) the parity put against the real put, (4) a truncation test of the day types.
"""

from __future__ import annotations

import datetime as dt
import io
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

MAIN = Path(
    sys.argv[1] if len(sys.argv) > 1 else "/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN"
)
MINE = MAIN / "data/research/real_puts_2026-11-25/options.sqlite"
APP = MAIN / "data/expired_options.db"
LIVE = MAIN / "data/onelot.db"
DAYS092 = MAIN / "data/research/option_setups_2026-11-24/days.csv"
FIRST, LAST = dt.date(2021, 11, 8), dt.date(2026, 11, 7)
TUNE_END, CHOOSE_END = dt.date(2024, 11, 7), dt.date(2025, 11, 7)
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
STEP = {"NIFTY": 50, "BANKNIFTY": 100}
HALF, SLIP, TICK = 0.006, 2 * 0.05, 0.05
R, Q = 0.065, 0.012
SPY = 365.0 * 86400.0
E_MIN, X_MIN, WAIT = 9 * 60 + 20, 15 * 60 + 20, 3
OUT: list[str] = []


def say(s: str = "") -> None:
    print(s, flush=True)
    OUT.append(s)


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)


# ───────────────────────────── costs (own formula from config/costs.yaml) ──────────────
_c = yaml.safe_load((MAIN / "config/costs.yaml").read_text())["products"]["index_options"]
_t = _c["taxes"]
FEES = _t["exchange_txn_pct"] + _t["sebi_turnover_pct"] + _t["ipft_pct"]


def leg_cost(turnover: float, sell: bool) -> float:
    b = _c["brokerage"]["per_order_inr"]  # mode flat
    assert _c["brokerage"]["mode"] == "flat"
    f = FEES * turnover
    tot = b + f + _t["gst_pct"] * (b + f)
    tot += (_t["stt_sell_pct"] if sell else _t["stt_buy_pct"]) * turnover
    if not sell:
        tot += _t["stamp_duty_pct"] * turnover
    return tot


def pair_return(und: str, e: tuple[float, float], x: tuple[float, float]) -> float:
    lot = LOT[und]
    net = base = 0.0
    for ep, xp in zip(e, x, strict=True):
        ef = round(np.ceil((ep * (1 + HALF) + SLIP) / TICK - 1e-9) * TICK, 2)
        xf = max(round(np.floor((xp * (1 - HALF) - SLIP) / TICK + 1e-9) * TICK, 2), TICK)
        net += (xf - ef) * lot - leg_cost(ef * lot, False) - leg_cost(xf * lot, True)
        base += ef * lot
    return net / base * 100.0


# ───────────────────────────── bars ────────────────────────────────────────────────────
def blobs(und: str) -> dict[tuple[str, str], list[tuple[int, bytes]]]:
    """(day, opt) -> [(rel, blob)] over both stores, code 1 MONTH, rows > 0."""
    out: dict[tuple[str, str], list[tuple[int, bytes]]] = {}
    for p in (MINE, APP):
        q = ("SELECT day, opt, rel, data FROM blocks WHERE underlying=? AND flag='MONTH' "
             "AND code=1 AND rows>0")  # fmt: skip
        for d, o, rel, b in ro(p).execute(q, (und,)):
            out.setdefault((d, o), []).append((int(rel), b))
    return out


def day_bars(parts: list[tuple[int, bytes]]) -> dict[str, np.ndarray]:
    cols = {k: [] for k in ("minute", "open", "close", "strike", "spot", "rel")}
    for rel, b in parts:
        with np.load(io.BytesIO(b)) as z:
            ts = z["ts"].astype("int64")
            cols["minute"].append(((ts + 19800) % 86400) // 60)
            for k in ("open", "close", "strike", "spot"):
                cols[k].append(z[k].astype(float))
            cols["rel"].append(np.full(len(ts), rel))
    z = {k: np.concatenate(v) for k, v in cols.items()}
    med = np.median(z["spot"])
    ok = np.abs(z["spot"] / med - 1) <= 0.05
    z = {k: v[ok] for k, v in z.items()}
    z["dropped_spot"] = np.array([int((~ok).sum())])
    key = z["minute"] * 10_000_000 + np.round(z["strike"]).astype(np.int64)
    _, first = np.unique(key, return_index=True)
    first = np.sort(first)
    return {k: (v[first] if k != "dropped_spot" else v) for k, v in z.items()}


def first_at(z: dict, k: float, m0: int, rel0: bool = False) -> tuple[float, int, float]:
    """(open, minute, spot) of strike k, earliest minute in [m0, m0 + WAIT]."""
    sel = (z["minute"] >= m0) & (z["minute"] <= m0 + WAIT)
    sel &= (z["rel"] == 0) if rel0 else (np.abs(z["strike"] - k) < 0.5)
    idx = np.flatnonzero(sel)
    if not len(idx):
        return np.nan, -1, np.nan
    i = idx[np.argmin(z["minute"][idx])]
    return float(z["open"][i]), int(z["minute"][i]), float(z["spot"][i])


def spiky(z: dict, k: float, m: int, px: float) -> bool:
    """A print more than 40% away from the median open of the same strike's neighbours
    (two minutes either side), or under its intrinsic value by 0.2% of the index."""
    sel = (np.abs(z["strike"] - k) < 0.5) & (np.abs(z["minute"] - m) <= 2) & (z["minute"] != m)
    nb = z["open"][sel]
    return bool(len(nb) >= 2 and abs(px / np.median(nb) - 1) > 0.40)


# ───────────────────────────── day types (own, from the stored daily bars) ──────────────
def daily_frames() -> tuple[dict[str, pd.DataFrame], pd.Series]:
    c = ro(LIVE)
    out = {}
    for key in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close FROM bars WHERE series_key=? AND resolution='1d'"
            " ORDER BY ts", c, params=(key,))  # fmt: skip
        df["d"] = pd.to_datetime(df.ts).dt.date
        out[key] = df.drop_duplicates("d").set_index("d")
    return out, out["INDIAVIX"]["close"]


def day_flags(dfd: pd.DataFrame, vix: pd.Series, d: dt.date) -> dict[str, bool]:
    """Types of session d from rows dated before d only."""
    h = dfd[dfd.index < d]
    v = vix[(vix.index < d) & (vix > 0)]
    if len(h) < 22 or len(v) < 2:
        return {"ACTIVE": False, "CALM": False, "NR7": False, "VIX-UP": False, "VIX-DOWN": False}
    rng = ((h.high - h.low) / h.close.shift(1)).to_numpy()
    med20 = np.median(rng[-21:-1])  # the 20 sessions before the last one
    nr7 = bool((h.high.iloc[-1] - h.low.iloc[-1]) <= (h.high - h.low).iloc[-7:-1].min())
    ch = v.iloc[-1] / v.iloc[-2] - 1
    act = bool(rng[-1] > med20)
    return {"ACTIVE": act, "CALM": not act, "NR7": nr7, "VIX-UP": bool(ch > 0.05),
            "VIX-DOWN": bool(ch < -0.05)}  # fmt: skip


# ───────────────────────────── statistics ───────────────────────────────────────────────
def day_t(x: np.ndarray, day: np.ndarray) -> float:
    """t of the mean, all trades of one date being one draw (cluster by date)."""
    x = np.asarray(x, float)
    if len(x) < 3:
        return float("nan")
    _, inv = np.unique(day, return_inverse=True)
    if inv.max() < 1:
        return float("nan")
    dev = np.bincount(inv, x) - x.mean() * np.bincount(inv)
    se = float(np.sqrt((dev**2).sum())) / len(x)
    return float(x.mean() / se) if se > 0 else float("nan")


def line(name: str, df: pd.DataFrame) -> str:
    if not len(df):
        return f"{name:<22} no trades"
    x = df.ret.to_numpy()
    per = [df[df.per == k].ret for k in (0, 1, 2)]
    pm = " / ".join(f"{p.mean():+.2f}" if len(p) else "n/a" for p in per)
    return (
        f"{name:<22} n {len(df):>5} days {df.date.nunique():>5} {x.mean():+6.2f}% "
        f"(t {day_t(x, df.date.to_numpy()):+5.1f}) win {100 * (x > 0).mean():4.1f}%  {pm}"
    )


def main() -> None:
    daily, vix = daily_frames()
    idx1m: dict[str, pd.DataFrame] = {}
    c = ro(LIVE)
    for und in ("NIFTY", "BANKNIFTY"):
        df = pd.read_sql_query(
            "SELECT ts, open FROM bars WHERE series_key=? AND resolution='1m' AND ts>=? AND ts<?",
            c, params=(und, "2021-11-07", "2026-11-09"))  # fmt: skip
        t = pd.to_datetime(df.ts) + pd.Timedelta(hours=5, minutes=30)
        df["d"], df["m"] = t.dt.date, t.dt.hour * 60 + t.dt.minute
        idx1m[und] = df

    cal = pd.read_csv(DAYS092, parse_dates=["date", "expiry"])
    cal["date"] = cal["date"].dt.date
    cal["expiry"] = cal["expiry"].dt.date
    cal["und"] = cal.inst.str.replace("_OPT", "")
    expiry = {(r.und, r.date): r.expiry for r in cal.itertuples()}

    rows, par_rows, miss = [], [], {}
    say("(1) THE FETCHED FILE (own reading, both stores merged by minute and strike)")
    for und in ("NIFTY", "BANKNIFTY"):
        bl = blobs(und)
        sess = sorted(d for d in set(idx1m[und].d) if FIRST <= d <= LAST)
        say(f"{und}: sessions in the index 1m bars {len(sess)}")
        n_leg = {"CE": 0, "PE": 0}
        mins, near, near_idx, near_n, spikes, bars_n, dropped = [], 0, 0, 0, 0, 0, 0
        intr_bad = 0
        for d in sess:
            ds = d.isoformat()
            ce = day_bars(bl[(ds, "CE")]) if (ds, "CE") in bl else None
            pe = day_bars(bl[(ds, "PE")]) if (ds, "PE") in bl else None
            for o, z in (("CE", ce), ("PE", pe)):
                if z is not None and (z["rel"] == 0).any():
                    n_leg[o] += 1
            if pe is not None and (pe["rel"] == 0).any():
                r0 = pe["rel"] == 0
                mins.append(int(r0.sum()))
                dropped += int(pe["dropped_spot"][0])
                # the put strike against the index at 09:20: Dhan's own spot field and our index bar
                px, m, sp = first_at(pe, np.nan, E_MIN, rel0=True)
                if m >= 0:
                    k = float(pe["strike"][(pe["rel"] == 0) & (pe["minute"] == m)][0])
                    near_n += 1
                    near += abs(k - sp) <= STEP[und] / 2
                    ix = idx1m[und]
                    ix = ix[(ix.d == d) & (ix.m == m)]
                    if len(ix):
                        near_idx += abs(k - float(ix.open.iloc[0])) <= STEP[und] / 2
                # bad prints: an open 40% away from its neighbours' median, or under intrinsic
                o_, mi, st, spt = pe["open"][r0], pe["minute"][r0], pe["strike"][r0], pe["spot"][r0]
                order = np.argsort(mi)
                o_, st, spt = o_[order], st[order], spt[order]
                bars_n += len(o_)
                if len(o_) >= 5:
                    med = pd.Series(o_).rolling(5, center=True).median().to_numpy()
                    spikes += int(np.nansum(np.abs(o_ / med - 1) > 0.40))
                intr_bad += int(np.sum(o_ < np.maximum(st - spt, 0) - 0.002 * spt))
            if ce is None or pe is None:
                miss[(und, d)] = "no day"
                continue
            k0, m0, _ = first_at(ce, np.nan, E_MIN, rel0=True)
            if m0 < 0:
                _, m0, _ = first_at(pe, np.nan, E_MIN, rel0=True)
                if m0 < 0:
                    miss[(und, d)] = "no ATM bar at entry"
                    continue
                k = float(pe["strike"][(pe["rel"] == 0) & (pe["minute"] == m0)][0])
            else:
                k = float(ce["strike"][(ce["rel"] == 0) & (ce["minute"] == m0)][0])
            e = [first_at(z, k, E_MIN) for z in (ce, pe)]
            x = [first_at(z, k, X_MIN) for z in (ce, pe)]
            if any(np.isnan(v[0]) for v in e):
                miss[(und, d)] = "entry print missing"
                continue
            if any(np.isnan(v[0]) for v in x):
                miss[(und, d)] = "exit print missing"
                continue
            rec = {"und": und, "date": d, "k": k,
                   "ret": pair_return(und, (e[0][0], e[1][0]), (x[0][0], x[1][0])),
                   "ret_call": pair_return(und, (e[0][0], e[0][0]), (x[0][0], x[0][0])),
                   "ret_put": pair_return(und, (e[1][0], e[1][0]), (x[1][0], x[1][0])),
                   "gross": (x[0][0] + x[1][0]) / (e[0][0] + e[1][0]) * 100 - 100,
                   "spiky": any(spiky(z, k, v[1], v[0]) for z, v in zip((ce, pe) * 2, e + x,
                                                                         strict=False)),
                   "e": (e[0][0], e[1][0]), "x": (x[0][0], x[1][0]), "m": (e[0][1], x[0][1]),
                   "sp": (e[0][2], x[0][2])}  # fmt: skip
            d_ = d
            rec["per"] = 0 if d_ <= TUNE_END else (1 if d_ <= CHOOSE_END else 2)
            rows.append(rec)
            # the parity put at the same strike on every minute both ATM legs are real
            e0 = expiry.get((und, d))
            if e0 is not None:
                a, b = ce["rel"] == 0, pe["rel"] == 0
                kc = ce["minute"][a] * 10_000_000 + np.round(ce["strike"][a]).astype(np.int64)
                kp = pe["minute"][b] * 10_000_000 + np.round(pe["strike"][b]).astype(np.int64)
                common, ia, ib = np.intersect1d(kc, kp, return_indices=True)
                if len(common):
                    par_rows.append(pd.DataFrame({
                        "und": und, "date": d, "minute": ce["minute"][a][ia],
                        "k": ce["strike"][a][ia], "call": ce["close"][a][ia],
                        "put": pe["close"][b][ib], "spot": ce["spot"][a][ia], "exp": e0}))  # fmt: skip
        ms = np.array(mins)
        say(
            f"  days with a real ATM put {n_leg['PE']}, call {n_leg['CE']} of {len(sess)}; "
            f"put minutes a day median {np.median(ms):.0f}, under 300 on {(ms < 300).sum()} days"
        )
        say(
            f"  09:20 put strike within half a step of Dhan's spot {near} of {near_n}; "
            f"of our index open {near_idx} of {near_n}"
        )
        say(
            f"  bad prints (own: open 40% off the rolling 5-minute median, or under intrinsic by "
            f"0.2%): spikes {spikes} ({100 * spikes / bars_n:.2f}% of {bars_n} put minutes), "
            f"under intrinsic {intr_bad}; bars with another index's spot {dropped}"
        )

    tr = pd.DataFrame(rows)
    say("\n(2) THE PAIR, 09:20 -> 15:20, both legs their own real prints, one lot each, 2 legs")
    say(f"trades {len(tr)}; left out: {pd.Series(miss).value_counts().to_dict()}")
    say(
        f"spiky prints in the sample: {int(tr.spiky.sum())} trades; "
        f"cost model reproduced from config (half {HALF}, slip {SLIP}, tick {TICK})"
    )
    clean = tr[~tr.spiky]
    for nm, df in (
        ("POOL (all)", tr),
        ("POOL (no spike)", clean),
        ("NIFTY", tr[tr.und == "NIFTY"]),
        ("BANKNIFTY", tr[tr.und == "BANKNIFTY"]),
    ):
        say(line(nm, df))
    x = tr.ret.to_numpy()
    rng = np.random.default_rng(1)
    dates = tr.date.to_numpy()
    ud = np.unique(dates)
    boots = []
    grp = {d: x[dates == d] for d in ud}
    for _ in range(2000):
        pick = rng.choice(len(ud), len(ud))
        v = np.concatenate([grp[ud[i]] for i in pick])
        boots.append(v.mean())
    say(
        f"  95% interval (resampling dates) {np.percentile(boots, 2.5):+.2f} to "
        f"{np.percentile(boots, 97.5):+.2f}; median {np.median(x):+.2f}; gross pair move "
        f"{tr.gross.mean():+.2f}%"
    )
    say(
        "  by-day t over the pool as one draw per date: "
        f"{day_t(x, dates):+.2f}; "
        f"t over (index, date) as one draw: "
        f"{day_t(x, (tr.und + tr.date.astype(str)).to_numpy()):+.2f}"
    )

    # day types
    fl = {}
    for und in ("NIFTY", "BANKNIFTY"):
        for d in tr[tr.und == und].date:
            fl[(und, d)] = day_flags(daily[und], vix, d)
    for ty in ("ACTIVE", "NR7", "VIX-UP", "VIX-DOWN"):
        m = np.array([fl[(u, d)][ty] for u, d in zip(tr.und, tr.date, strict=True)])
        say(line(f"POOL {ty}", tr[m]))
        # no ACTIVE twin for CALM here: it is the complement
    m = np.array([fl[(u, d)]["CALM"] for u, d in zip(tr.und, tr.date, strict=True)])
    say(line("POOL CALM", tr[m]))
    m = (
        np.array([fl[(u, d)]["NR7"] for u, d in zip(tr.und, tr.date, strict=True)])
        & (tr.und == "NIFTY").to_numpy()
    )
    say(line("NIFTY NR7", tr[m]))
    say(line("call alone (pooled)", tr.assign(ret=tr.ret_call)))
    say(line("put alone (pooled)", tr.assign(ret=tr.ret_put)))
    # the study's own flags against mine
    st = cal.set_index(["und", "date"])
    agree = {"ACTIVE": 0, "NR7": 0, "VIX-UP": 0}
    tot = 0
    for u, d in zip(tr.und, tr.date, strict=True):
        if (u, d) in st.index:
            r = st.loc[(u, d)]
            tot += 1
            agree["ACTIVE"] += (r.size_prev == "active") == fl[(u, d)]["ACTIVE"]
            agree["NR7"] += bool(r.nr7) == fl[(u, d)]["NR7"]
            agree["VIX-UP"] += bool(r.vix_chg > 0.05) == fl[(u, d)]["VIX-UP"]
    say(f"  my day types against US-092's days.csv on {tot} trade days: {agree}")

    # (3) parity put
    say("\n(3) PARITY PUT AGAINST THE REAL PUT (same strike, same minute, both ATM legs real)")
    pm = pd.concat(par_rows, ignore_index=True)
    day0 = pd.to_datetime(pm.date).dt.tz_localize(None).astype("int64") // 10**9
    exp_s = (pd.to_datetime(pm.exp).astype("int64") // 10**9) + 15.5 * 3600
    t = np.maximum((exp_s - (day0 + pm.minute * 60.0)) / SPY, 60.0 / SPY)
    pm["t"] = t
    ok = (pm.put > 0) & (pm.call > 0)
    for q in (0.0, Q, 0.02):
        f = pm.spot * np.exp((R - q) * pm.t)
        par = pm.call - np.exp(-R * pm.t) * (f - pm.k)
        err = (par / pm.put - 1)[ok]
        say(
            f"  q {q:.3f}: {ok.sum()} minutes on {pm.date.nunique()} days per index pair; median "
            f"{100 * err.median():+.1f}%, IQR {100 * err.quantile(0.25):+.1f} to "
            f"{100 * err.quantile(0.75):+.1f}%, mean |err| {100 * err.abs().mean():.1f}%"
        )
        if q == Q:
            pm["err"] = par / pm.put - 1
            for nm, m_ in (("09:20", 9 * 60 + 20), ("15:20", 15 * 60 + 20)):
                e_ = pm[(pm.minute == m_) & ok]
                say(
                    f"    at {nm}: n {len(e_)}, median {100 * e_.err.median():+.1f}%, "
                    f"IQR {100 * e_.err.quantile(0.25):+.1f} to {100 * e_.err.quantile(0.75):+.1f}%"
                )
            for u in ("NIFTY", "BANKNIFTY"):
                e_ = pm[(pm.und == u) & ok].err
                say(f"    {u}: median {100 * e_.median():+.1f}%")
    # implied time to expiry from the pair: is the calendar right?
    s0 = pm[(pm.minute == E_MIN) & ok]
    fwd = (s0.call - s0.put) * np.exp(R * s0.t) + s0.k
    imp = np.log(fwd / s0.spot) / (R - Q)
    say(
        f"  forward from the real pair at 09:20: median {1e4 * np.log(fwd / s0.spot).median():+.0f}"
        f" bp over the index; implied T / calendar T median {(imp / s0.t).median():.2f}"
        f" (IQR {(imp / s0.t).quantile(0.25):.2f} to {(imp / s0.t).quantile(0.75):.2f})"
    )
    # the trade with a parity put in place of the real one, same trades
    pr = []
    for r in tr.itertuples():
        e0 = expiry.get((r.und, r.date))
        if e0 is None:
            continue
        legs = []
        for (cpx, _), mn, sp in zip(((r.e[0], 0), (r.x[0], 0)), r.m, r.sp, strict=True):
            tt = max((dt.datetime.combine(e0, dt.time(15, 30)).timestamp()
                      - dt.datetime.combine(r.date, dt.time(0, 0)).timestamp()
                      - mn * 60.0 - 0.0) / SPY, 60.0 / SPY)  # fmt: skip
            f = sp * np.exp((R - Q) * tt)
            legs.append(cpx - np.exp(-R * tt) * (f - r.k))
        if min(legs) > 0:
            pr.append(
                (r.ret, pair_return(r.und, (r.e[0], legs[0]), (r.x[0], legs[1])), r.date, r.und)
            )
    p = pd.DataFrame(pr, columns=["real", "par", "date", "und"])
    p["diff"] = p.par - p.real
    s3 = pd.read_csv(DAYS092.parent / "s3.csv", parse_dates=["date"])
    s3 = s3[(s3.variant == "09:20 15:20") & s3.r.notna()]
    s3["und"] = s3.inst.str.replace("_OPT", "")
    old_set = set(zip(s3.und, s3.date.dt.date, strict=True))
    q = p[[(u, d) in old_set for u, d in zip(p.und, p.date, strict=True)]]
    say(
        f"  the {len(q)} of those trades the earlier store priced: parity {q.par.mean():+.2f}, "
        f"real {q.real.mean():+.2f}, parity less real {q['diff'].mean():+.2f} "
        f"(t {day_t(q['diff'].to_numpy(), q.date.to_numpy()):+.1f}); the other "
        f"{len(p) - len(q)} trades: real {p[~p.index.isin(q.index)].real.mean():+.2f}; "
        f"earlier file's own parity read on its {len(s3)} days {s3.r.mean():+.2f}"
    )
    say(
        f"  on {len(p)} trades (the call real, the put its parity value at q {Q}): "
        f"pair {p.par.mean():+.2f}% against real {p.real.mean():+.2f}%; "
        f"parity less real {p['diff'].mean():+.2f} (t {day_t(p['diff'].to_numpy(), p.date.to_numpy()):+.1f})"
    )

    # (4) no look-ahead in the day types
    say("\n(4) NO LOOK-AHEAD IN THE DAY TYPES")
    rnd = np.random.default_rng(7)
    bad = tested = 0
    for und in ("NIFTY", "BANKNIFTY"):
        dfd = daily[und]
        hl = dfd.high - dfd.low
        rngs = hl / dfd.close.shift(1)
        act = rngs > rngs.shift(1).rolling(20).median()
        nr7 = hl <= hl.shift(1).rolling(6).min()
        v = vix[vix > 0]
        vch = v / v.shift(1) - 1
        for d in rnd.choice(np.array(sorted(tr[tr.und == und].date)), 150, replace=False):
            k = dfd.index.searchsorted(d, side="left") - 1  # last daily row before d
            kv = v.index.searchsorted(d, side="left") - 1
            cut = day_flags(
                dfd.iloc[: k + 1], v.iloc[: kv + 1], dt.date(2100, 1, 1)
            )  # nothing later
            full = {
                "ACTIVE": bool(act.iloc[k]),
                "NR7": bool(nr7.iloc[k]),
                "VIX-UP": bool(vch.iloc[kv] > 0.05),
                "VIX-DOWN": bool(vch.iloc[kv] < -0.05),
            }
            tested += 1
            bad += any(cut[a] != full[a] for a in full)
            assert dfd.index[k] < d and v.index[kv] < d
    say(
        f"  types from rows cut before the day against the whole-series reading: {tested} days, "
        f"{bad} differ; the last row used is always earlier than the day"
    )
    dd = daily["NIFTY"]
    n_bad = int(
        (
            ~np.isfinite(dd[["high", "low", "close"]]).all(axis=1)
            | (dd.close <= 0)
            | (dd.high < dd.low)
        ).sum()
    )
    say(
        f"  stored NIFTY daily rows with a bad price (classify() would drop them, shifting an "
        f"iloc index): {n_bad} of {len(dd)}"
    )
    dis = []
    for u, d in zip(tr.und, tr.date, strict=True):
        if (u, d) in st.index and (st.loc[(u, d)].size_prev == "active") != fl[(u, d)]["ACTIVE"]:
            dfd = daily[u]
            k = dfd.index.searchsorted(d, side="left") - 1
            same_day = day_flags(dfd, vix, dfd.index[min(k + 2, len(dfd) - 1)])["ACTIVE"]
            dis.append((u, d, st.loc[(u, d)].size_prev, fl[(u, d)]["ACTIVE"], same_day))
    ahead = sum((x[2] == "active") == x[4] for x in dis)
    say(
        f"  the study's size state differs from mine on {len(dis)} trade days; it equals the "
        f"state of the day itself on {ahead} of them (look-ahead would show here)"
    )
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text("\n".join(OUT))


if __name__ == "__main__":
    main()
