"""US-092 independent check of the option-setups study (own code, no import of its functions).

Run from the MAIN checkout, this file from the US-092 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/option_setups_check.py" <stage> [<stage> ...]

Stages: parity (real put against the parity put), s3 (the long-volatility pair, implied
volatility fall, bad-print filter), s1 (last half hour, cell b), vix (S5 day lists), expiry
(inferred expiry dates), counts (cells.csv recount), doc (note format rules).
The stores are opened read only (SQLite `mode=ro`); scratch output goes to the folder given by
the environment variable CHECK_OUT, else a temporary folder; no network.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

# the harness is the MAIN checkout's: drop this file's own folder from the import path
sys.path = [q for q in sys.path if Path(q or ".").resolve() != Path(__file__).resolve().parent]

import mtf_bruteforce as h  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.special import ndtr  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

ROOT = h.ROOT
STORE = ROOT / "data" / "expired_options.db"
MAIN_DB = ROOT / "data" / "onelot.db"
STUDY = ROOT / "data" / "research" / "option_setups_2026-11-24"
WT = Path(__file__).resolve().parents[1]
NOTE = WT / "docs" / "research" / "OPTION_SETUPS_2026-11-24.md"
SCRATCH = Path(os.environ.get("CHECK_OUT", tempfile.gettempdir()))
INDICES = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
UND = {"NIFTY_OPT": "NIFTY", "BANKNIFTY_OPT": "BANKNIFTY", "FINNIFTY_OPT": "FINNIFTY",
       "SENSEX_OPT": "SENSEX"}  # fmt: skip
SPY = h.SECONDS_PER_YEAR
IST = 19800
MIN = {"09:20": 560, "15:20": 920}


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def unpack(blob: bytes) -> dict:
    with np.load(io.BytesIO(blob)) as z:
        return {k: (z[k].astype("int64") if k == "ts" else z[k].astype(float)) for k in z.files}


def mod(ts: np.ndarray) -> np.ndarray:
    return (ts + IST) % 86400 // 60


def period(d: dt.date) -> int:
    return 0 if d <= dt.date(2024, 11, 7) else 1 if d <= dt.date(2025, 11, 7) else 2


# ───────────────────────────── Black-76 and implied volatility ──────────────


def b76(f, k, t, sig, r, call):  # noqa: ANN001, ANN201
    t = np.maximum(t, 60.0 / SPY)
    v = sig * np.sqrt(t)
    d1 = (np.log(f / k) + 0.5 * v * v) / v
    d2 = d1 - v
    disc = np.exp(-r * t)
    c = disc * (f * ndtr(d1) - k * ndtr(d2))
    p = disc * (k * ndtr(-d2) - f * ndtr(-d1))
    return np.where(call, c, p)


def iv_of_call(c: float, f: float, k: float, t: float, r: float) -> float:
    """Bisection on [0.005, 3]; NaN when the price is outside no-arbitrage bounds."""
    t = max(t, 60.0 / SPY)
    disc = np.exp(-r * t)
    if not (c > disc * max(f - k, 0.0) + 1e-6 and c < disc * f):
        return float("nan")
    lo, hi = 0.005, 3.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if float(b76(f, k, t, mid, r, True)) < c:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ───────────────────────────── the real store ───────────────────────────────


def atm_calls(und: str) -> dict[dt.date, dict]:
    """Near monthly at-the-money CALL blocks (code 1, rel 0) per day."""
    db = ro(STORE)
    q = ("SELECT day, data FROM blocks WHERE underlying=? AND flag='MONTH' AND code=1 AND "
         "opt='CE' AND rel=0 AND rows>0")  # fmt: skip
    return {dt.date.fromisoformat(d): unpack(b) for d, b in db.execute(q, (und,))}


def my_expiries(blocks: dict[dt.date, dict]) -> dict[tuple[int, int], dt.date]:
    """Monthly expiry per month from the store: in the last 10 calendar days of a month, the
    day on which the at-the-money call keeps under 0.15% of the index as time value in its
    bars from 15:00 (the lowest such day)."""
    best: dict[tuple[int, int], tuple[float, dt.date]] = {}
    for d, b in sorted(blocks.items()):
        nxt = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
        if (nxt - d).days > 10:
            continue
        late = mod(b["ts"]) >= 900
        if not late.any():
            continue
        tv = (b["close"] - np.maximum(b["spot"] - b["strike"], 0.0)) / b["spot"]
        med = float(np.median(tv[late]))
        key = (d.year, d.month)
        if med < 0.0015 and (key not in best or med < best[key][0]):
            best[key] = (med, d)
    return {k: v[1] for k, v in best.items()}


def expiry_of(day: dt.date, exps: list[dt.date]) -> dt.date | None:
    """The first found expiry on or after `day`, only when no month lies between the last
    found expiry before `day` and it (a month without a found expiry is skipped, never
    given the next month's date)."""
    prev = [e for e in exps if e < day]
    for e in exps:
        if e >= day:
            if prev and (e.year * 12 + e.month) - (prev[-1].year * 12 + prev[-1].month) != 1:
                return None
            return e if (e - day).days <= 35 else None
    return None


def exp_epoch(d: dt.date) -> int:
    return int(dt.datetime.combine(d, dt.time(15, 30), tzinfo=dt.timezone.utc).timestamp()) - IST


# ───────────────────────────── stage: parity (1a) ───────────────────────────


def stage_parity() -> None:
    db = ro(STORE)
    rows_all = []
    for und in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        exps = sorted(my_expiries(atm_calls(und)).values())
        q = ("SELECT p.day, p.rel, p.data, c.data FROM blocks p JOIN blocks c ON "
             "c.underlying=p.underlying AND c.flag=p.flag AND c.code=p.code AND c.rel=p.rel "
             "AND c.day=p.day AND c.opt='CE' WHERE p.underlying=? AND p.flag='MONTH' AND "
             "p.code=1 AND p.opt='PE' AND p.rows>0 AND c.rows>0")  # fmt: skip
        for day, rel, pb, cb in db.execute(q, (und,)):
            d = dt.date.fromisoformat(day)
            ex = expiry_of(d, exps)
            if ex is None:
                continue
            pe, ce = unpack(pb), unpack(cb)
            i = pd.Series(np.arange(len(ce["ts"])), index=ce["ts"])
            j = i.reindex(pe["ts"]).to_numpy()
            ok = np.isfinite(j)
            jj = j[ok].astype(int)
            same = pe["strike"][ok] == ce["strike"][jj]
            idx = np.flatnonzero(ok)[same]
            jj = jj[same]
            for a, b in zip(idx, jj, strict=True):
                rows_all.append((und, d, ex, int(pe["ts"][a]), float(pe["strike"][a]),
                                 float(pe["spot"][a]), float(pe["close"][a]),
                                 float(pe["volume"][a]), float(ce["close"][b]),
                                 float(ce["volume"][b]), rel))  # fmt: skip
    df = pd.DataFrame(rows_all, columns=["und", "day", "exp", "ts", "k", "spot", "put",
                                         "pvol", "call", "cvol", "rel"])  # fmt: skip
    df["t"] = (df["exp"].map(exp_epoch) - df["ts"]) / SPY
    df["m"] = mod(df["ts"].to_numpy())
    df.to_pickle(SCRATCH / "parity_rows.pkl")
    print("same-strike minutes:", len(df), "days", df.groupby("und")["day"].nunique().to_dict())
    r = 0.065
    g = df[(df["put"] >= 2) & (df["call"] >= 2) & (df["pvol"] > 0) & (df["cvol"] > 0)].copy()
    print(f"rows with both volumes positive and premium >= 2 rupees: {len(g)}")
    qs = (0.0, 0.012, 0.02)
    for q_div in qs:
        pp = g["call"] - g["spot"] * np.exp(-q_div * g["t"]) + g["k"] * np.exp(-r * g["t"])
        g[f"e{q_div}"] = (pp - g["put"]) / g["put"] * 100
    g["mono"] = (g["k"] / g["spot"] - 1).abs()
    for q_div in qs:
        e = g[f"e{q_div}"]
        print(f"q {q_div:.3f}: n {len(e)} median {e.median():+.2f}% mean {e.mean():+.2f}% "
              f"IQR {e.quantile(.25):+.2f} to {e.quantile(.75):+.2f}  p5 {e.quantile(.05):+.2f}"
              f" p95 {e.quantile(.95):+.2f}")  # fmt: skip
    for lo, hi in ((0, 0.003), (0.003, 0.01), (0.01, 1)):
        s = g[(g["mono"] >= lo) & (g["mono"] < hi)]["e0.012"]
        print(f"|K/S-1| {lo:.3f} to {hi:.3f}: n {len(s)} median {s.median():+.2f}% "
              f"IQR {s.quantile(.25):+.2f} to {s.quantile(.75):+.2f}")  # fmt: skip
    for und, s in g.groupby("und"):
        e = s["e0.012"]
        print(f"{und}: n {len(e)} days {s['day'].nunique()} median {e.median():+.2f}% "
              f"IQR {e.quantile(.25):+.2f} to {e.quantile(.75):+.2f}")  # fmt: skip
    for mm, name in ((560, "09:20"), (920, "15:20")):
        s = g[g["m"] == mm]["e0.012"]
        print(f"minute {name}: n {len(s)} median {s.median():+.2f}% mean {s.mean():+.2f}%")
    parity_pairs(g)
    errs = {k: float(g["e0.012"].quantile(v)) for k, v in
            (("p25", .25), ("p50", .5), ("p75", .75))}  # fmt: skip
    pd.Series(errs).to_json(SCRATCH / "parity_err.json")


def parity_pairs(g: pd.DataFrame, r: float = 0.065) -> None:
    """Per day, the at-the-money pair (call + put of the strike nearest the index) bought at
    the first minute from 09:20 and sold at the first from 15:20 (the strike may differ
    between the two minutes), no costs, with the real put and with the parity put (q 1.2%).
    Also the error of the parity put at the two minutes."""
    g = g.copy()
    g["mono"] = (g["k"] / g["spot"] - 1).abs()
    g["pp"] = g["call"] - g["spot"] * np.exp(-0.012 * g["t"]) + g["k"] * np.exp(-r * g["t"])
    out = []
    for (und, d), s in g[g["mono"] < 0.015].groupby(["und", "day"]):
        a = s[(s["m"] >= 560) & (s["m"] <= 563)].sort_values(["m", "mono"])
        b = s[(s["m"] >= 920) & (s["m"] <= 923)].sort_values(["m", "mono"])
        if len(a) and len(b):
            a, b = a.iloc[0], b.iloc[0]
            real = (b["call"] + b["put"]) / (a["call"] + a["put"]) * 100 - 100
            par = (b["call"] + b["pp"]) / (a["call"] + a["pp"]) * 100 - 100
            out.append((und, d, a["k"] == b["k"], (a["pp"] / a["put"] - 1) * 100,
                        (b["pp"] / b["put"] - 1) * 100, real, par))  # fmt: skip
    o = pd.DataFrame(out, columns=["und", "day", "same_k", "err0920", "err1520", "real_pair",
                                   "parity_pair"])  # fmt: skip
    o["gap"] = o["parity_pair"] - o["real_pair"]
    print(o.round(2).to_string(index=False))
    print(f"days {len(o)}: real pair {o.real_pair.mean():+.2f}% parity pair "
          f"{o.parity_pair.mean():+.2f}% gap (parity less real) mean {o.gap.mean():+.2f} "
          f"median {o.gap.median():+.2f} sd {o.gap.std():.2f}; put error at 09:20 median "
          f"{o.err0920.median():+.1f}% at 15:20 {o.err1520.median():+.1f}%")  # fmt: skip


def stage_pairs() -> None:
    parity_pairs(pd.read_pickle(SCRATCH / "parity_rows.pkl"))


# ───────────────────────────── stage: s3 ────────────────────────────────────


def own_book(p: h.Prep, e: list[float], x: list[float]) -> float:
    """% of the money put in after the model's friction, for a set of legs (call, put)."""
    cfg = p.opt_cfg
    oc = p.costs["options"]
    net = base = 0.0
    for en, ex in zip(e, x, strict=True):
        ef = float(h.tick_up(np.array([en * (1 + cfg["half"]) + cfg["slip"]]), p.tick)[0])
        xf = float(np.maximum(h.tick_down(np.array([ex * (1 - cfg["half"]) - cfg["slip"]]),
                                          p.tick), p.tick)[0])  # fmt: skip
        net += (xf - ef) * p.lot - oc.leg(ef * p.lot, sell=False) - oc.leg(xf * p.lot, sell=True)
        base += ef * p.lot
    return net / base * 100.0


def pick(b: dict, minute: int) -> int:
    """First bar of the day block at `minute` or within 3 minutes after it, else -1."""
    m = mod(b["ts"])
    for w in range(4):
        k = np.flatnonzero(m == minute + w)
        if len(k):
            return int(k[0])
    return -1


def stage_s3() -> None:
    h._init_worker()
    err = pd.read_json(SCRATCH / "parity_err.json", typ="series")
    spread = float(err["p75"] - err["p25"])
    med_abs = float(abs(err["p50"]))
    scen = {
        "q1.2": (0.012, 1.0, 1.0), "q0": (0.0, 1.0, 1.0), "q2": (0.02, 1.0, 1.0),
        "against 5%": (0.012, 1.05, 0.95), "for 5%": (0.012, 0.95, 1.05),
        "against buyer": (0.012, 1 + med_abs / 100, 1 - med_abs / 100),
        "for buyer": (0.012, 1 - med_abs / 100, 1 + med_abs / 100),
        "against, IQR half": (0.012, 1 + spread / 200, 1 - spread / 200),
        "for, IQR half": (0.012, 1 - spread / 200, 1 + spread / 200),
    }  # fmt: skip
    print(f"parity error used: median {err['p50']:+.2f}%, IQR {spread:.2f} points")
    allrows = []
    drop_rep = []
    for inst in INDICES:
        und = UND[inst]
        p = h.prepare(inst, tfs=("60m",), iv_lag="prev")
        r = p.opt_cfg["r"]
        blocks = atm_calls(und)
        exps = sorted(my_expiries(blocks).values())
        idx = pd.Series(np.arange(len(p.ns)), index=p.ns // 1_000_000_000)
        idx = idx[~idx.index.duplicated()]
        sess_of_date = {d: s for s, d in enumerate(p.sess_dates)}
        n_bar = n_bad = n_nan = 0
        for d, b in sorted(blocks.items()):
            if not (h.T0 <= d <= h.T_END):
                continue
            ex = expiry_of(d, exps)
            s = sess_of_date.get(d)
            if ex is None or s is None:
                continue
            ivm = float(p.iv[s])
            legs = {}
            for tag in ("09:20", "15:20"):
                i = pick(b, MIN[tag])
                if i < 0:
                    break
                ts = int(b["ts"][i])
                jx = idx.get(ts)
                if jx is None:
                    break
                t = max((exp_epoch(ex) - ts) / SPY, 60.0 / SPY)
                legs[tag] = (i, float(b["open"][i]), float(b["strike"][i]), float(p.o[jx]), t)
            if len(legs) < 2:
                continue
            for tag in legs:
                i, c, k, so, t = legs[tag]
                n_bar += 1
                v = iv_of_call(c, so * np.exp((r - 0.012) * t), k, t, r)
                if not np.isfinite(v):
                    n_nan += 1
                elif v < 0.25 * ivm or v > 4 * ivm:
                    n_bad += 1
            row = {"inst": inst, "date": d, "period": period(d), "ivm": ivm,
                   "same_k": legs["09:20"][2] == legs["15:20"][2]}  # fmt: skip
            for name, (qd, m0, m1) in scen.items():
                res = {}
                for tag in ("09:20", "15:20"):
                    i, c, k, so, t = legs[tag]
                    f = so * np.exp((r - qd) * t)
                    res[tag] = (c, k, f, t, iv_of_call(c, f, k, t, r))
                c0, kk0, f0, t0, v0 = res["09:20"]
                c1, kk1, f1, t1, v1 = res["15:20"]
                row[f"v0_{name}"], row[f"v1_{name}"] = v0, v1
                p0 = c0 - np.exp(-r * t0) * (f0 - kk0)
                if kk1 == kk0:
                    cx, px = c1, c1 - np.exp(-r * t1) * (f1 - kk0)
                elif np.isfinite(v1):
                    cx = float(b76(f1, kk0, t1, v1, r, True))
                    px = float(b76(f1, kk0, t1, v1, r, False))
                else:
                    cx = px = float("nan")
                if not (np.isfinite(cx) and np.isfinite(p0)):
                    row[f"r_{name}"] = np.nan
                    continue
                row[f"r_{name}"] = own_book(p, [c0, max(p0 * m0, 0.05)], [cx, max(px * m1, 0.05)])
            v0, v1 = row["v0_q1.2"], row["v1_q1.2"]
            row["bad"] = not (
                np.isfinite(v0) and np.isfinite(v1)
                and 0.25 * ivm <= v0 <= 4 * ivm and 0.25 * ivm <= v1 <= 4 * ivm
            )  # fmt: skip
            row["dhan_iv0"] = float(b["iv"][legs["09:20"][0]])
            row["dhan_iv1"] = float(b["iv"][legs["15:20"][0]])
            allrows.append(row)
        drop_rep.append((inst, n_bar, n_bad, n_nan))
    df = pd.DataFrame(allrows)
    df.to_pickle(SCRATCH / "s3_rows.pkl")
    for inst, nb, bad, nn in drop_rep:
        print(f"{inst}: entry and exit minutes with a call bar {nb}, outside 0.25 to 4 x model "
              f"{bad} ({bad / nb:.2%}), no solvable volatility {nn} ({nn / nb:.2%})")  # fmt: skip
    report_s3(df)


def tstat(x: pd.Series, day: pd.Series) -> float:
    v = x.to_numpy(float)
    _, inv = np.unique(day.to_numpy(), return_inverse=True)
    dev = np.bincount(inv, v) - v.mean() * np.bincount(inv)
    se = np.sqrt((dev**2).sum()) / len(v)
    return float(v.mean() / se) if se > 0 else float("nan")


def report_s3(df: pd.DataFrame) -> None:
    day = df["date"].map(lambda d: d.toordinal())
    pools = {"NIFTY+BANKNIFTY": df["inst"].isin(["NIFTY_OPT", "BANKNIFTY_OPT"]),
             "four indices": df["inst"].notna()}  # fmt: skip
    cols = [c for c in df.columns if c.startswith("r_")]
    for pn, msk in pools.items():
        print(f"--- pair 09:20 to 15:20, {pn}")
        for filt in ("filter on", "filter off"):
            q = df[msk & (~df["bad"] if filt == "filter on" else True)]
            for c in cols:
                x = q[c].dropna()
                if len(x) < 3:
                    continue
                tr = [x[q.loc[x.index, "period"] == k].mean() for k in range(3)]
                print(f"{filt:10s} {c[2:]:18s} n {len(x):5d} mean {x.mean():+.2f}% "
                      f"t {tstat(x, day[x.index]):+.1f}  TUNE/CHOOSE/TEST "
                      f"{tr[0]:+.2f}/{tr[1]:+.2f}/{tr[2]:+.2f}")  # fmt: skip
    vf = SCRATCH / "vix_flags.pkl"
    if vf.exists():
        fl = pd.read_pickle(vf).drop_duplicates(["inst", "date"])
        q = df[~df["bad"] & df["inst"].isin(["NIFTY_OPT", "BANKNIFTY_OPT"])].merge(
            fl, on=["inst", "date"], how="left")  # fmt: skip
        dq = q["date"].map(lambda d: d.toordinal())
        for name, col in (("study list", "vix_chg"), ("stored-bar list", "chg_stored")):
            for lab, m_ in (("VIX-UP", q[col] > 0.05), ("VIX-DOWN", q[col] < -0.05)):
                x = q.loc[m_, "r_q1.2"].dropna()
                print(f"{name:16s} {lab:9s} NIFTY+BANKNIFTY n {len(x)} mean {x.mean():+.2f}% "
                      f"t {tstat(x, dq[x.index]):+.1f}")  # fmt: skip
    print("--- by index, q 1.2, filter on")
    for inst, q in df[~df["bad"]].groupby("inst"):
        x = q["r_q1.2"].dropna()
        print(f"{inst}: n {len(x)} mean {x.mean():+.2f}% t {tstat(x, day[x.index]):+.1f}")
    print("--- same strike at entry and exit only (no volatility re-pricing), q 1.2")
    q = df[~df["bad"] & df["same_k"] & df["inst"].isin(["NIFTY_OPT", "BANKNIFTY_OPT"])]
    x = q["r_q1.2"].dropna()
    print(f"n {len(x)} mean {x.mean():+.2f}% t {tstat(x, day[x.index]):+.1f}")
    print("--- implied volatility 09:20 to 15:20 (call bars; q 1.2)")
    for inst, q in df.groupby("inst"):
        for lab, qq in (("all valid", q), ("same strike", q[q["same_k"]]),
                        ("filter on", q[~q["bad"]])):  # fmt: skip
            v0, v1 = qq["v0_q1.2"], qq["v1_q1.2"]
            ok = np.isfinite(v0) & np.isfinite(v1)
            rel = (v1[ok] / v0[ok] - 1) * 100
            print(f"{inst:14s} {lab:12s} n {ok.sum():5d} mean IV 09:20 {v0[ok].mean() * 100:.1f}% "
                  f"15:20 {v1[ok].mean() * 100:.1f}%  change {rel.mean():+.2f}% relative, "
                  f"{(v1[ok] - v0[ok]).mean() * 100:+.2f} points; model IV "
                  f"{qq['ivm'][ok].mean() * 100:.1f}%")  # fmt: skip
        d0, d1 = q["dhan_iv0"], q["dhan_iv1"]
        ok = (d0 > 0) & (d1 > 0)
        print(f"{inst:14s} Dhan's own iv field n {ok.sum()} change "
              f"{((d1[ok] / d0[ok] - 1) * 100).mean():+.2f}% relative")  # fmt: skip


# ───────────────────────────── stage: s1 (3) ────────────────────────────────


def stage_s1() -> None:
    h._init_worker()
    for inst in ("NIFTY_OPT", "BANKNIFTY_OPT"):
        p = h.prepare(inst, tfs=("60m",), iv_lag="prev")
        daily = h._cache.bars(h._cache.spec(inst).series_key, "1d")
        dd = np.array([x.date() for x in daily.index])
        dc = daily["close"].to_numpy(float)
        rows = []
        for s, d in enumerate(p.sess_dates):
            if not (h.T0 <= d <= h.T_END):
                continue
            a, b = int(p.sess_first[s]), int(p.sess_last[s]) + 1
            m = p.minute[a:b]

            def at(mm: int, a: int = a, m: np.ndarray = m) -> int:
                k = np.flatnonzero(m == mm)
                return a + int(k[0]) if len(k) else -1

            j44, j29, j59, e = at(584), at(869), at(899), at(900)
            kd = int(np.searchsorted(dd, d, side="left")) - 1
            if min(j44, j29, j59, e) < 0 or kd < 0 or e >= p.xi[s]:
                continue
            r1 = p.c[j44] / dc[kd] - 1
            rb = p.c[j59] / p.c[j29] - 1
            if r1 == 0 or np.sign(r1) != np.sign(rb):
                continue
            side = int(np.sign(r1))
            x = int(p.xi[s])
            xt = p.ns[x] + (h.NS_MIN if p.xclose[s] else 0)
            ret = h.pnl(p, side, 0, np.array([e]), np.array([x]), np.array([p.xp[s]]),
                        np.array([xt]))["ret"][0]  # fmt: skip
            rows.append((d, ret, period(d)))
        t = pd.DataFrame(rows, columns=["date", "ret", "period"])
        day = t["date"].map(lambda d: d.toordinal())
        pm = [t[t.period == k].ret.mean() for k in range(3)]
        pt = [tstat(t[t.period == k].ret, day[t.period == k]) for k in range(3)]
        print(f"{inst} cell b: n {len(t)} mean {t.ret.mean():+.2f}% t {tstat(t.ret, day):+.1f}; "
              f"TUNE/CHOOSE/TEST {pm[0]:+.2f}/{pm[1]:+.2f}/{pm[2]:+.2f} "
              f"(t {pt[0]:+.1f}/{pt[1]:+.1f}/{pt[2]:+.1f}, n "
              f"{[int((t.period == k).sum()) for k in range(3)]})")  # fmt: skip


# ───────────────────────────── stage: vix (4) ───────────────────────────────


def stored_vix() -> tuple[np.ndarray, np.ndarray]:
    """India VIX daily closes as stored (`bars`, resolution 1d), one per date."""
    db = ro(MAIN_DB)
    v = pd.DataFrame(
        db.execute(
            "SELECT ts, close FROM bars WHERE series_key='INDIAVIX' AND resolution='1d' ORDER BY ts"
        ).fetchall(),
        columns=["ts", "c"],
    )
    v["d"] = pd.to_datetime(v["ts"]).dt.date
    v = v[v["c"] > 0].drop_duplicates("d").reset_index(drop=True)
    return v["d"].to_numpy(), v["c"].to_numpy(float)


def vix_change(vd: np.ndarray, vc: np.ndarray, d: dt.date) -> float:
    """Change of the last VIX close before day d over the one before it (D-1 over D-2)."""
    k = int(np.searchsorted(vd, d, side="left"))  # first VIX bar on or after d
    return float(vc[k - 1] / vc[k - 2] - 1) if k >= 2 else float("nan")


def stage_vix() -> None:
    h._init_worker()
    days = pd.read_csv(STUDY / "days.csv", parse_dates=["date"])
    days["date"] = days["date"].dt.date
    hv = h._cache.vix()  # the harness's series: daily bars rebuilt from the one-minute bars
    hv = hv[hv > 0]
    series = {"harness (one-minute rebuilt)": (np.array([x.date() for x in hv.index]),
                                               hv.to_numpy(float)),
              "stored 1d bars": stored_vix()}  # fmt: skip
    up_s, dn_s = days["vix_chg"] > 0.05, days["vix_chg"] < -0.05
    print(f"index-days {len(days)}; study lists: UP {int(up_s.sum())}, DOWN {int(dn_s.sum())} "
          f"({int(up_s.sum()) // 4} and {int(dn_s.sum()) // 4} per index)")  # fmt: skip
    rng = np.random.default_rng(1)
    pick_ = days.iloc[rng.choice(len(days), 80, replace=False)]
    for name, (vd, vc) in series.items():
        mine = days["date"].map(lambda d, vd=vd, vc=vc: vix_change(vd, vc, d))
        up_m, dn_m = mine > 0.05, mine < -0.05
        print(f"{name}: UP {int(up_m.sum())} DOWN {int(dn_m.sum())}; differ from the study "
              f"UP {int((up_m != up_s).sum())} DOWN {int((dn_m != dn_s).sum())}; max |change "
              f"diff| {np.nanmax(np.abs(mine - days['vix_chg'])):.2e}; VIX dates {len(vd)}")  # fmt: skip
        same = 0
        for d in pick_["date"]:
            keep = vd < d  # every VIX bar from day D on removed
            new = vc[keep][-1] / vc[keep][-2] - 1
            same += bool(abs(new - vix_change(vd, vc, d)) < 1e-12)
        print(f"   flag of day D with only VIX bars before D: {same} of 80 equal to the "
              f"full-series value")  # fmt: skip
    vd, vc = series["harness (one-minute rebuilt)"]
    dts = sorted(set(days["date"]))
    pos = {d: i for i, d in enumerate(dts)}
    gap = sum(int(vd[int(np.searchsorted(vd, d, side="left")) - 1] != dts[pos[d] - 1])
              for d in dts[2:])  # fmt: skip
    print(f"days where the last VIX bar before D is not the previous index session: {gap}")
    # the same flags from the stored bars, as a list for the s3 stage
    vs, cs = series["stored 1d bars"]
    days["chg_stored"] = days["date"].map(lambda d: vix_change(vs, cs, d))
    days[["inst", "date", "vix_chg", "chg_stored"]].to_pickle(SCRATCH / "vix_flags.pkl")


# ───────────────────────────── stage: expiry (5) ────────────────────────────


def stage_expiry() -> None:
    h._init_worker()
    ec = pd.read_csv(STUDY / "expiries.csv", parse_dates=["day"])
    ec["day"] = ec["day"].dt.date
    for inst in INDICES:
        blocks = atm_calls(UND[inst])
        mine = my_expiries(blocks)
        g = ec[ec["inst"] == inst]
        filled, st = g[g["how"] == "filled"], g[g["how"] == "store"]
        agree = sum(mine.get((d.year, d.month)) == d for d in st["day"])
        print(f"{inst}: months {len(g)}, from the store {len(st)} (mine agree {agree}), filled "
              f"{len(filled)}: {', '.join(filled['month'])}")  # fmt: skip
        p = h.prepare(inst, tfs=("60m",), iv_lag="prev")
        hset = {pd.Timestamp(int(e), tz="UTC").tz_convert("Asia/Kolkata").date()
                for e in set(p.exp_ns.tolist())}  # fmt: skip
        print(f"  harness expiry dates (one weekday rule) that are a store expiry: "
              f"{sum(r.day in hset for r in st.itertuples())} of {len(st)}; that equal the "
              f"filled day: {sum(r.day in hset for r in filled.itertuples())} of {len(filled)}")  # fmt: skip
        for r in filled.itertuples():
            lo = r.day - dt.timedelta(days=4)
            lines = []
            for d in sorted(blocks):
                if lo <= d <= r.day + dt.timedelta(days=4):
                    b = blocks[d]
                    late = mod(b["ts"]) >= 900
                    if late.any():
                        tv = (b["close"] - np.maximum(b["spot"] - b["strike"], 0)) / b["spot"]
                        lines.append(f"{d:%d}:{np.median(tv[late]) * 100:.2f}")
            if lines:
                print(f"  filled {r.month} {r.day} ({r.day:%a}) "
                      f" store time value % from 15:00 by day: {' '.join(lines)}")  # fmt: skip


def stage_s4days() -> None:
    """Does any S4 trade sit on a day that the store says is not the monthly expiry? The
    test: the at-the-money call's time value from 15:00 under 0.15% of the index."""
    s4 = pd.read_csv(STUDY / "s4.csv", parse_dates=["date"])
    s4["date"] = s4["date"].dt.date
    dy = pd.read_csv(STUDY / "days.csv", parse_dates=["date"])
    dy["date"] = dy["date"].dt.date
    s4 = s4.merge(dy[["inst", "date", "is_exp"]], on=["inst", "date"])
    s4 = s4[s4["exists"] & s4["r"].notna() & s4["is_exp"]]  # priced trades on EXPIRY days
    for inst in INDICES:
        blocks = atm_calls(UND[inst])
        q = s4[s4["inst"] == inst]
        days = sorted(set(q["date"]))
        ok, bad, none = [], [], []
        for d in days:
            b = blocks.get(d)
            if b is None:
                none.append(d)
                continue
            late = mod(b["ts"]) >= 900
            tv = (b["close"] - np.maximum(b["spot"] - b["strike"], 0.0)) / b["spot"]
            (ok if np.median(tv[late]) < 0.0015 else bad).append(d)
        nb = int(q["date"].isin(bad).sum())
        print(f"{inst}: priced S4 rows {len(q)} on {len(days)} days; expiry confirmed by the "
              f"store {len(ok)}, NOT an expiry by the store {len(bad)} ({nb} rows): "
              f"{[f'{d} {d:%a}' for d in bad]}; no bars {len(none)}")  # fmt: skip


def iv_vec(c, f, k, t, r):  # noqa: ANN001, ANN201
    """Vectorised bisection of the call volatility (NaN outside no-arbitrage bounds)."""
    t = np.maximum(t, 60.0 / SPY)
    disc = np.exp(-r * t)
    ok = np.isfinite(c) & (c > disc * np.maximum(f - k, 0.0) + 1e-6) & (c < disc * f)
    lo, hi = np.full(c.shape, 0.005), np.full(c.shape, 3.0)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        up = b76(f, k, t, mid, r, True) < c
        lo, hi = np.where(up, mid, lo), np.where(up, hi, mid)
    return np.where(ok, 0.5 * (lo + hi), np.nan)


def stage_filter() -> None:
    """Minutes dropped by the bad-print filter, over every minute with an at-the-money call
    bar (open price, index open, q 1.2%, the study's own model volatility rule)."""
    h._init_worker()
    for inst in INDICES:
        p = h.prepare(inst, tfs=("60m",), iv_lag="prev")
        r = p.opt_cfg["r"]
        blocks = atm_calls(UND[inst])
        exps = sorted(my_expiries(blocks).values())
        idx = pd.Series(np.arange(len(p.ns)), index=p.ns // 1_000_000_000)
        idx = idx[~idx.index.duplicated()]
        sess_of = {d: s for s, d in enumerate(p.sess_dates)}
        n = bad = unsolv = 0
        for d, b in blocks.items():
            ex, s = expiry_of(d, exps), sess_of.get(d)
            if ex is None or s is None or not (h.T0 <= d <= h.T_END):
                continue
            jx = idx.reindex(b["ts"]).to_numpy()
            ok = np.isfinite(jx)
            jj = jx[ok].astype(int)
            t = np.maximum((exp_epoch(ex) - b["ts"][ok]) / SPY, 60.0 / SPY)
            f = p.o[jj] * np.exp((r - 0.012) * t)
            v = iv_vec(b["open"][ok], f, b["strike"][ok], t, r)
            m = float(p.iv[s])
            n += len(v)
            unsolv += int(np.isnan(v).sum())
            bad += int(((v < 0.25 * m) | (v > 4 * m)).sum())
        print(f"{inst}: minutes with a call bar {n}; volatility outside 0.25 to 4 x the model's "
              f"{bad} ({bad / n:.2%}); no solvable volatility {unsolv} ({unsolv / n:.2%})")  # fmt: skip


def stage_smile() -> None:
    """The flat-smile assumption of the pair's exit: on days when the index rose so that the
    exit at-the-money strike K1 is above the entry strike K0, the store holds the in-the-money
    call at K0 itself (rel below 0). Compare its real open price at the exit minute with the
    price of K0 at the volatility implied by the at-the-money call (q 1.2%)."""
    db = ro(STORE)
    r = 0.065
    rows = []
    for und in ("NIFTY", "BANKNIFTY"):
        blocks = atm_calls(und)
        exps = sorted(my_expiries(blocks).values())
        itm: dict[dt.date, list[dict]] = {}
        q = ("SELECT day, data FROM blocks WHERE underlying=? AND flag='MONTH' AND code=1 AND "
             "opt='CE' AND rel<0 AND rows>0")  # fmt: skip
        for day, blob in db.execute(q, (und,)):
            itm.setdefault(dt.date.fromisoformat(day), []).append(unpack(blob))
        for d, b in blocks.items():
            ex = expiry_of(d, exps)
            if ex is None or d not in itm or not (h.T0 <= d <= h.T_END):
                continue
            i0, i1 = pick(b, 560), pick(b, 920)
            if i0 < 0 or i1 < 0:
                continue
            k0, k1 = b["strike"][i0], b["strike"][i1]
            if k1 <= k0:
                continue
            ts = int(b["ts"][i1])
            hit = [x["open"][np.flatnonzero((x["ts"] == ts) & (x["strike"] == k0))[0]]
                   for x in itm[d] if ((x["ts"] == ts) & (x["strike"] == k0)).any()]  # fmt: skip
            if not hit:
                continue
            t = max((exp_epoch(ex) - ts) / SPY, 60.0 / SPY)
            f = float(b["spot"][i1]) * np.exp((r - 0.012) * t)
            v = iv_of_call(float(b["open"][i1]), f, float(k1), t, r)
            if not np.isfinite(v):
                continue
            model = float(b76(f, k0, t, v, r, True))
            rows.append((und, d, k0, k1, float(hit[0]), model))
    o = pd.DataFrame(rows, columns=["und", "day", "k0", "k1", "real", "flat_iv"])
    o["err"] = (o["flat_iv"] / o["real"] - 1) * 100
    print(f"days {len(o)}: flat-smile price of the entry strike at exit less its real price, "
          f"% of the real price: median {o.err.median():+.2f} mean {o.err.mean():+.2f} "
          f"IQR {o.err.quantile(.25):+.2f} to {o.err.quantile(.75):+.2f}")  # fmt: skip
    for und, g in o.groupby("und"):
        print(f"  {und}: n {len(g)} median {g.err.median():+.2f}% mean {g.err.mean():+.2f}%")


# ───────────────────────────── stage: counts (6) ────────────────────────────


def stage_counts() -> None:
    d = pd.read_csv(STUDY / "cells.csv")
    for reading, g in d.groupby("reading"):
        n = g[["examined", "discovered", "confirmed", "passed", "avoid"]].sum()
        print(f"{reading}: cells {len(g)}; " + ", ".join(f"{k} {int(v)}" for k, v in n.items()))
        ex = (g.TUNE_n >= 60) & (g.TUNE_days >= 30)
        di = ex & (g.TUNE_m > 0) & (g.TUNE_t >= 2.5) & (g.TUNE_m >= 1.0)
        co = di & (g.CHOOSE_m > 0) & (g.CHOOSE_t >= 1.0) & (g.CHOOSE_n >= 20)
        pa = co & (g.TEST_m > 0) & (g.TEST_n >= 20) & (g.real > 0)
        av = ex & (g.TUNE_t <= -2.5) & (g.CHOOSE_m < 0) & (g.TEST_m < 0)
        print(f"{reading} from the numbers: examined {int(ex.sum())} discovered {int(di.sum())} "
              f"confirmed {int(co.sum())} passed {int(pa.sum())} avoid {int(av.sum())}")  # fmt: skip
    m = d[(d.reading == "model") & d.examined]
    print("model examined by setup:", m.groupby("setup").size().to_dict())
    print("discovered by setup:", d[d.discovered & (d.reading == "model")].groupby("setup")
          .size().to_dict())  # fmt: skip
    print(f"arithmetic: {len(m) * 0.006:.2f} / {len(m) * 0.006 * 0.16:.2f} / "
          f"{len(m) * 0.006 * 0.16 * 0.5:.2f}")  # fmt: skip
    pl = pd.read_csv(STUDY / "placebo.csv")
    print(
        "placebo mean (max):",
        {
            c: f"{pl[c].mean():.2f} ({pl[c].max():.0f})"
            for c in ("examined", "discovered", "confirmed", "passed")
        },
    )


# ───────────────────────────── stage: doc (8) ───────────────────────────────


def stage_doc() -> None:
    lines = NOTE.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    long_ = [i + 1 for i, s in enumerate(lines) if len(s) > 100 and not s.startswith("|")]
    dashes = [i + 1 for i, s in enumerate(lines) if "—" in s or "–" in s]
    print(f"lines {len(lines)} (max 180); prose lines over 100 characters: {long_}; "
          f"lines with an em or en dash: {dashes}")  # fmt: skip
    bad = [i + 1 for i, s in enumerate(lines)
           if re.search(r"\d\s*-\s*\d", s) and not s.startswith("|")]  # fmt: skip
    print(f"prose lines with a hyphen between digits (check by eye): {bad[:10]}")


STAGES = {"parity": stage_parity, "pairs": stage_pairs, "s3": stage_s3, "s1": stage_s1, "vix": stage_vix,
          "expiry": stage_expiry, "s4days": stage_s4days, "filter": stage_filter, "smile": stage_smile, "counts": stage_counts, "doc": stage_doc}  # fmt: skip

if __name__ == "__main__":
    for a in sys.argv[1:]:
        STAGES[a]()
