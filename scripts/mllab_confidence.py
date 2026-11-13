"""Does the Machine Learning Lab's `p_up` carry information? (research, 2026-11-13)

For 5 indices + 18 stocks and the timeframes 15m, 30m, 60m, 4h, 1d, the product's own
`onelot.mllab.models.walk_forward` is run over the most recent bars (first 300 are history
only): the forecast for bar t sees closes up to t-1 only and re-fits every 20 steps.
Per step it records each directional model's p_up and the realised log returns from close
t-1 to closes t, t+2 and t+4 (next 1, 3, 5 bars). `ewma` has a zero mean, so p_up is 0.5
by construction: it is left out.

Read-only on the store; data after 2026-11-12 is cut so every bar is complete. Outputs go
to data/research/mllab_2026-11-13/ in the checkout it runs from.

Run from the main checkout:
  PYTHONPATH=src:scripts:. .venv/bin/python -W ignore <this file>
"""

from __future__ import annotations

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

# import the harness of the checkout we run from (cwd), never this file's own folder: its
# ROOT decides which store is opened
sys.path[:] = [x for x in sys.path if Path(x or ".").resolve() != Path(__file__).resolve().parent]
import mtf_bruteforce as h  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), h.__file__

OUT = Path.cwd() / "data" / "research" / "mllab_2026-11-13"
DIR_MODELS = ("arima", "sarima", "garch", "empirical")
TFS = {"15m": 3000, "30m": 3000, "60m": 2500, "4h": 1200, "1d": 1200}
WARM = 300
CUTOFF = pd.Timestamp("2026-11-13", tz="Asia/Kolkata")  # bars before today only: all complete
HORIZONS = (1, 3, 5)


def load_closes(inst: str, tf: str) -> pd.Series:
    from onelot.data.resample import resample_bars

    key = h._cache.spec(inst).series_key
    if tf == "1d":
        b = h._cache.bars(key, "1d")
    else:
        b = h._cache.bars(key, "1m")
        b = b[b.index < CUTOFF.tz_convert(b.index.tz)]
        b = resample_bars(b, "240m" if tf == "4h" else tf)
    b = b[b.index < CUTOFF.tz_convert(b.index.tz)]
    c = b["close"].astype(float)
    c = c[np.isfinite(c) & (c > 0)]
    return c.iloc[-TFS[tf] :]


def run_one(task: tuple[str, str]) -> pd.DataFrame:
    from onelot.mllab.models import SEASON, walk_forward

    if h._cache is None:
        h._init_worker()
    inst, tf = task
    s = load_closes(inst, tf)
    c = s.to_numpy()
    n = len(c)
    if n <= WARM + 10:
        return pd.DataFrame()
    last = n - WARM
    df = pd.DataFrame({"ts": s.index[WARM:]})
    for m in DIR_MODELS:
        wf = walk_forward(c, m, last=last, season=SEASON[tf])
        assert len(wf) == last
        df[m] = [f.p_up if f.note is None else np.nan for f, _, _ in wf]
        # sanity: the step's "previous close" is c[t-1] (no look-ahead in the inputs)
        assert wf[0][1] == c[WARM - 1] and wf[0][2] == c[WARM]
    lc = np.log(c)
    t = np.arange(WARM, n)
    for k in HORIZONS:
        r = np.full(len(t), np.nan)
        ok = t + k - 1 < n
        r[ok] = lc[t[ok] + k - 1] - lc[t[ok] - 1]
        df[f"r{k}"] = r * 1e4  # basis points
    df["inst"], df["tf"] = inst, tf
    df["grp"] = "index" if inst in h.INDEX_OPTIONS else "stock"
    df["half"] = np.where(np.arange(len(df)) < len(df) // 2, 1, 2)
    return df


# ── statistics ───────────────────────────────────────────────────────────
def ctstat(x: pd.Series, day: pd.Series) -> float:
    """Mean / cluster-robust SE, all observations of one calendar day forming one draw."""
    x = x.dropna()
    if len(x) < 3:
        return float("nan")
    d = day.loc[x.index]
    mu = x.mean()
    s = (x - mu).groupby(d).sum()
    se = np.sqrt((s**2).sum()) / len(x)
    return float(mu / se) if se > 0 else float("nan")


def label(df: pd.DataFrame, hi: float = 0.5, lo: float = 0.5) -> pd.Series:
    p = df[list(DIR_MODELS)]
    full = p.notna().all(axis=1)
    up = full & (p > hi).all(axis=1)
    dn = full & (p < lo).all(axis=1)
    out = pd.Series("mixed", index=df.index)
    out[up], out[dn] = "up", "down"
    out[~full] = "na"
    return out


def fmt(x: float, d: int = 1) -> str:
    return "nan" if not np.isfinite(x) else f"{x:.{d}f}".replace("-", "−")


def row(sel: pd.DataFrame, total: int, day: pd.Series, col: str = "r1") -> str:
    r = sel[col].dropna()
    return (
        f"{len(sel):>6} {fmt(100 * len(sel) / max(total, 1)):>5}% "
        f"{fmt(100 * (r > 0).mean()):>5}% {fmt(r.mean(), 2):>7} {fmt(ctstat(r, day), 2):>6}"
    )


def report(all_df: pd.DataFrame) -> str:
    L: list[str] = []
    P = L.append
    day = all_df["ts"].map(lambda x: x.date())
    for tf in TFS:
        T = all_df[all_df.tf == tf]
        P(f"\n######## {tf}: {T.inst.nunique()} instruments, {len(T)} steps")
        for g in ("all", "index", "stock"):
            G = T if g == "all" else T[T.grp == g]
            n = len(G)
            if n == 0:
                continue
            P(
                f"--- {tf} {g}: base up share {fmt(100 * (G.r1 > 0).mean())}% "
                f"mean r1 {fmt(G.r1.mean(), 2)} bp"
            )
            P("single model: p>0.5 [n up% bp t] | p<0.5 [n up% bp t]")
            for m in DIR_MODELS:
                a, b = G[G[m] > 0.5], G[G[m] < 0.5]
                P(f"  {m:9} {row(a, n, day)} | {row(b, n, day)}")
            P("calibration (n, mean p, up%, bp):")
            edges = [0, 0.45, 0.48, 0.52, 0.55, 1.0001]
            for m in DIR_MODELS:
                cells = []
                for lo, hi in zip(edges[:-1], edges[1:], strict=False):
                    s = G[(G[m] >= lo) & (G[m] < hi)]
                    cells.append(
                        f"{len(s)}/{fmt(s[m].mean(), 3)}/{fmt(100 * (s.r1 > 0).mean())}"
                        f"/{fmt(s.r1.mean(), 2)}"
                    )
                P(f"  {m:9} " + " | ".join(cells))
            for nm, hi, lo in (("agree>0.5", 0.5, 0.5), ("strong 0.52/0.48", 0.52, 0.48)):
                lab = label(G, hi, lo)
                P(f"{nm}: [n share% up% bp t]")
                for k in ("up", "down", "mixed"):
                    P(f"  {k:6} {row(G[lab == k], n, day)}")
            lab = label(G)
            P("after agree [n up% bp t] by horizon 1/3/5:")
            P("  all   " + " | ".join(row(G, n, day, f"r{h_}") for h_ in HORIZONS))
            for k in ("up", "down"):
                sel = G[lab == k]
                P(f"  {k:5} " + " | ".join(row(sel, n, day, f"r{h_}") for h_ in HORIZONS))
            # first bar of a run, persistence: within one instrument's series
            per = []
            for _, S in G.groupby("inst", sort=False):
                ls = label(S).reset_index(drop=True)
                d = {"lab": ls.to_numpy()}
                for k in (1, 3, 5):
                    d[f"f{k}"] = ls.shift(-k).to_numpy()
                d["prev"] = ls.shift(1).to_numpy()
                per.append(pd.DataFrame(d, index=S.index))
            Q = pd.concat(per)
            for k in ("up", "down"):
                base = (Q.lab == k).mean()
                on = Q[Q.lab == k]
                P(
                    f"  persist {k}: base {fmt(100 * base)}% | still at t+1 "
                    f"{fmt(100 * (on.f1 == k).mean())}% t+3 {fmt(100 * (on.f3 == k).mean())}% "
                    f"t+5 {fmt(100 * (on.f5 == k).mean())}% | mean run "
                    f"{fmt(len(on) / max(((Q.lab == k) & (Q.prev != k)).sum(), 1), 2)} bars"
                )
                first = G.loc[Q[(Q.lab == k) & (Q.prev != k)].index]
                P(f"  first-{k:4} " + " | ".join(row(first, n, day, f"r{h_}") for h_ in HORIZONS))
            if tf in ("15m", "60m"):
                for half in (1, 2):
                    H = G[G.half == half]
                    lab = label(H)
                    P(
                        f"  half {half} ({H.ts.min().date()} to {H.ts.max().date()}): "
                        f"up {row(H[lab == 'up'], len(H), day)} | down "
                        f"{row(H[lab == 'down'], len(H), day)}"
                    )
    return "\n".join(L)


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "steps.pkl"
    insts = list(h.INDEX_OPTIONS) + list(h.STOCKS)
    if os.environ.get("QUICK"):
        insts = insts[:1]
    if raw.exists() and not os.environ.get("QUICK"):
        all_df = pd.read_pickle(raw)
    else:
        tasks = [(i, tf) for tf in TFS for i in insts]
        with ProcessPoolExecutor(max_workers=7, initializer=h._init_worker) as pool:
            parts = list(pool.map(run_one, tasks))
        all_df = pd.concat([p for p in parts if len(p)], ignore_index=True)
        if not os.environ.get("QUICK"):
            all_df.to_pickle(raw)
        print(f"walk-forward done in {time.time() - t0:.0f}s", file=sys.stderr)
    txt = report(all_df)
    (OUT / "report.txt").write_text(txt)
    print(txt)
    print(f"total {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
