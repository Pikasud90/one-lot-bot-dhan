"""What the Machine Learning Lab's bands are worth (the up probability is worth nothing:
`mllab_confidence.py`).

For every instrument, timeframe and model the product's own `walk_forward` is run over the
same bars as the confidence study; each step keeps the forecast band and what the next
candle did. The report answers four questions:

1. Coverage: does the next close land inside the 68% band 68% of the time, and inside the
   95% band 95% of the time? Also for the session's first candle alone (it holds the
   overnight gap) and for the others.
2. Sharpness: when a model draws a wide band, is the next move really larger? Bands are
   ranked wide, middle, narrow within each instrument; the mean absolute move of each third
   is shown, with the rank correlation.
3. Five candles ahead: does the 68% band widened by the square root of 5 hold 68% of the
   closes five candles later?
4. A close outside the 95% band: does the next candle continue or reverse?

With the argument `regimes` it tests the fix for what section 1 shows (the session's first
candle holds the overnight gap, so one band cannot fit it and the ordinary candles): each
model is run on two return series of its own, the opening candles only (previous close to
the first candle's close, one a session) and the ordinary candles only.

Run from the main checkout (read-only on the store, about two minutes each):

    PYTHONPATH=src:scripts:. .venv/bin/python -W ignore scripts/mllab_bands.py
    PYTHONPATH=src:scripts:. .venv/bin/python -W ignore scripts/mllab_bands.py regimes
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

# a worker process starts without this folder on its path: add it for the import below
# (appended, so the harness still comes from the checkout this runs from)
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import mllab_confidence as mc  # noqa: E402

h = mc.h
OUT = Path.cwd() / "data" / "research" / "mllab_2026-11-13"
MODELS = ("arima", "sarima", "garch", "ewma", "empirical")


def run_one(task: tuple[str, str]) -> pd.DataFrame:
    from onelot.mllab.models import SEASON, walk_forward

    if h._cache is None:
        h._init_worker()
    inst, tf = task
    s = mc.load_closes(inst, tf)
    c = s.to_numpy()
    n = len(c)
    if n <= mc.WARM + 10:
        return pd.DataFrame()
    last = n - mc.WARM
    lc = np.log(c)
    t = np.arange(mc.WARM, n)
    r1 = (lc[t] - lc[t - 1]) * 1e4
    r5 = np.full(len(t), np.nan)
    ok = t + 4 < n
    r5[ok] = (lc[t[ok] + 4] - lc[t[ok] - 1]) * 1e4
    nxt = np.full(len(t), np.nan)  # the candle after the forecast one
    ok = t + 1 < n
    nxt[ok] = (lc[t[ok] + 1] - lc[t[ok]]) * 1e4
    ts = s.index[mc.WARM :]
    first = np.r_[True, ts[1:].date != ts[:-1].date] if tf != "1d" else np.zeros(len(t), bool)
    parts = []
    for m in MODELS:
        wf = walk_forward(c, m, last=last, season=SEASON[tf])
        good = np.array([f.note is None and f.lo68 is not None for f, _, _ in wf])
        lo68, hi68, lo95, hi95 = (
            np.array(
                [getattr(f, k) if g else np.nan for (f, _, _), g in zip(wf, good, strict=True)],
                dtype=float,
            )
            for k in ("lo68", "hi68", "lo95", "hi95")
        )
        x = c[t]
        parts.append(
            pd.DataFrame(
                {
                    "inst": inst,
                    "tf": tf,
                    "model": m,
                    "day": ts.date,
                    "first": first,
                    "w": np.log(hi68 / lo68) / 2 * 1e4,  # the band's half width, bp
                    "in68": (x >= lo68) & (x <= hi68),
                    "in95": (x >= lo95) & (x <= hi95),
                    "above95": x > hi95,
                    "below95": x < lo95,
                    "r1": r1,
                    "r5": r5,
                    "nxt": nxt,
                }
            )[good]
        )
    return pd.concat(parts, ignore_index=True)


def day_t(x: pd.Series, day: pd.Series) -> float:
    v = x.to_numpy(float)
    if len(v) < 3:
        return float("nan")
    _, inv = np.unique(day.to_numpy(), return_inverse=True)
    dev = np.bincount(inv, v) - v.mean() * np.bincount(inv)
    se = float(np.sqrt((dev**2).sum())) / len(v)
    return float(v.mean() / se) if se > 0 else float("nan")


def report(df: pd.DataFrame) -> str:
    out: list[str] = []
    w = out.append
    w("1. COVERAGE: share of next closes inside the band (68% and 95% are the targets)")
    w(
        f"{'tf':4s} {'model':10s} {'steps':>7s}  {'in 68%':>7s} {'in 95%':>7s}   first candle 68/95     other candles 68/95"
    )
    for tf in mc.TFS:
        for m in MODELS:
            d = df[(df.tf == tf) & (df.model == m)]
            a, b = d[d["first"]], d[~d["first"]]
            fa = (
                f"{100 * a.in68.mean():5.1f} / {100 * a.in95.mean():5.1f}"
                if len(a)
                else "      -      "
            )
            w(
                f"{tf:4s} {m:10s} {len(d):7d}  {100 * d.in68.mean():6.1f}% {100 * d.in95.mean():6.1f}%   "
                f"{fa}          {100 * b.in68.mean():5.1f} / {100 * b.in95.mean():5.1f}"
            )
    w("")
    w("2. SHARPNESS: mean absolute move (bp) after a narrow, middle and wide band")
    w("   (thirds within each instrument; the session's first candle left out intraday)")
    w(
        f"{'tf':4s} {'model':10s} {'narrow':>7s} {'middle':>7s} {'wide':>7s} {'wide/narrow':>12s} {'rank corr':>10s}"
    )
    for tf in mc.TFS:
        for m in MODELS:
            d = df[(df.tf == tf) & (df.model == m) & ~df["first"]].copy()
            d["a"] = d.r1.abs()
            d["q"] = d.groupby("inst")["w"].transform(
                lambda s: pd.qcut(s.rank(method="first"), 3, labels=False)
            )
            g = d.groupby("q")["a"].mean()
            rho = d.groupby("inst").apply(lambda z: z["w"].corr(z["a"], method="spearman")).mean()
            w(
                f"{tf:4s} {m:10s} {g.get(0, np.nan):7.1f} {g.get(1, np.nan):7.1f} {g.get(2, np.nan):7.1f} {g.get(2, np.nan) / g.get(0, np.nan):12.2f} {rho:10.2f}"
            )
    w("")
    w("3. FIVE CANDLES AHEAD: share of closes five candles later inside the 68% band x sqrt(5)")
    for tf in mc.TFS:
        cells = []
        for m in MODELS:
            d = df[(df.tf == tf) & (df.model == m) & df.r5.notna()]
            cells.append(f"{m} {100 * (d.r5.abs() <= d.w * np.sqrt(5)).mean():.1f}%")
        w(f"{tf:4s} " + "   ".join(cells))
    w("")
    w("4. A CLOSE OUTSIDE THE 95% BAND (garch): the next candle, bp (t by day)")
    w(
        f"{'tf':4s} {'above: share':>13s} {'next bp (t)':>16s}   {'below: share':>13s} {'next bp (t)':>16s}   any candle bp"
    )
    for tf in mc.TFS:
        d = df[(df.tf == tf) & (df.model == "garch") & df.nxt.notna()]
        a, b = d[d.above95], d[d.below95]
        w(
            f"{tf:4s} {100 * len(a) / len(d):12.1f}% {a.nxt.mean():+8.1f} ({day_t(a.nxt, a.day):+4.1f})   "
            f"{100 * len(b) / len(d):12.1f}% {b.nxt.mean():+8.1f} ({day_t(b.nxt, b.day):+4.1f})   {d.nxt.mean():+6.2f}"
        )
    w("")
    w("5. HOW WIDE THE BANDS ARE: median 68% half width, bp of price (garch)")
    for tf in mc.TFS:
        d = df[(df.tf == tf) & (df.model == "garch")]
        gi = d[d.inst.isin(h.INDEX_OPTIONS)]
        gs = d[~d.inst.isin(h.INDEX_OPTIONS)]
        w(f"{tf:4s} indices {gi.w.median():6.1f}   stocks {gs.w.median():6.1f}")
    return "\n".join(out) + "\n"


#: bars read for the opening-candle series (about 360 sessions), by timeframe
LONG = {"15m": 9000, "30m": 4700, "60m": 2500, "4h": 1200}
OPEN_WARM = 120  # opening candles a model sees before its first step (the product: 180 days)


def run_regimes(task: tuple[str, str]) -> pd.DataFrame:
    from onelot.mllab.models import SEASON, walk_forward

    if h._cache is None:
        h._init_worker()
    inst, tf = task
    mc.TFS[tf] = LONG[tf]
    s = mc.load_closes(inst, tf)
    days = np.asarray(s.index.date)
    r = np.diff(np.log(s.to_numpy()))
    into_first = days[1:] != days[:-1]
    day_of = days[1:]
    parts = []
    for regime, mask, season, warm, keep in (
        ("open", into_first, 5, OPEN_WARM, None),
        ("ordinary", ~into_first, max(SEASON[tf] - 1, 1), mc.WARM, 3000),
    ):
        sel, dd = r[mask], day_of[mask]
        if keep is not None:
            sel, dd = sel[-keep:], dd[-keep:]
        c = np.exp(np.r_[0.0, np.cumsum(sel)])  # closes whose returns are this regime's
        if len(c) <= warm + 10:
            continue
        t = np.arange(warm, len(c))
        for m in MODELS:
            wf = walk_forward(c, m, last=len(c) - warm, season=season)
            good = np.array([f.note is None and f.lo68 is not None for f, _, _ in wf])
            lo68, hi68, lo95, hi95 = (
                np.array(
                    [getattr(f, k) if g else np.nan for (f, _, _), g in zip(wf, good, strict=True)],
                    dtype=float,
                )
                for k in ("lo68", "hi68", "lo95", "hi95")
            )
            x = c[t]
            parts.append(
                pd.DataFrame(
                    {
                        "inst": inst,
                        "tf": tf,
                        "model": m,
                        "regime": regime,
                        "day": dd[t - 1],
                        "w": np.log(hi68 / lo68) / 2 * 1e4,
                        "in68": (x >= lo68) & (x <= hi68),
                        "in95": (x >= lo95) & (x <= hi95),
                        "a": np.abs(np.log(c[t] / c[t - 1])) * 1e4,
                    }
                )[good]
            )
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def report_regimes(df: pd.DataFrame) -> str:
    out: list[str] = []
    w = out.append
    w("EACH MODEL ON ITS OWN SERIES: opening candles only, ordinary candles only")
    w("coverage of the 68% and 95% bands; mean absolute move after a narrow / wide band (bp)")
    for regime in ("ordinary", "open"):
        w("")
        w(f"-- {regime} candles")
        w(
            f"{'tf':4s} {'model':10s} {'steps':>7s} {'in 68%':>7s} {'in 95%':>7s} {'narrow':>8s} {'wide':>7s} {'ratio':>6s} {'rank corr':>10s}"
        )
        for tf in LONG:
            for m in MODELS:
                d = df[(df.tf == tf) & (df.model == m) & (df.regime == regime)].copy()
                if d.empty:
                    continue
                d["q"] = d.groupby("inst")["w"].transform(
                    lambda s: pd.qcut(s.rank(method="first"), 3, labels=False)
                )
                g = d.groupby("q")["a"].mean()
                rho = (
                    d.groupby("inst").apply(lambda z: z["w"].corr(z["a"], method="spearman")).mean()
                )
                w(
                    f"{tf:4s} {m:10s} {len(d):7d} {100 * d.in68.mean():6.1f}% {100 * d.in95.mean():6.1f}% "
                    f"{g.get(0, np.nan):8.1f} {g.get(2, np.nan):7.1f} {g.get(2, np.nan) / g.get(0, np.nan):6.2f} {rho:10.2f}"
                )
    return "\n".join(out) + "\n"


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:] == ["regimes"]:
        insts = list(h.INDEX_OPTIONS) + list(h.STOCKS)
        tasks = [(i, tf) for tf in LONG for i in insts]
        with ProcessPoolExecutor(max_workers=7, initializer=h._init_worker) as pool:
            parts = list(pool.map(run_regimes, tasks))
        txt = report_regimes(pd.concat([p for p in parts if len(p)], ignore_index=True))
        (OUT / "bands_regimes_report.txt").write_text(txt)
        print(txt)
        print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)
        return
    raw = OUT / "bands.pkl"
    if raw.exists():
        df = pd.read_pickle(raw)
    else:
        insts = list(h.INDEX_OPTIONS) + list(h.STOCKS)
        tasks = [(i, tf) for tf in mc.TFS for i in insts]
        with ProcessPoolExecutor(max_workers=7, initializer=h._init_worker) as pool:
            parts = list(pool.map(run_one, tasks))
        df = pd.concat([p for p in parts if len(p)], ignore_index=True)
        df.to_pickle(raw)
        print(f"walk-forward done in {time.time() - t0:.0f}s", file=sys.stderr)
    txt = report(df)
    (OUT / "bands_report.txt").write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
