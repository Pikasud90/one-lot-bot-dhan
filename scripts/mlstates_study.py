"""Do market states carry memory? (research, 2026-11-23, US-077)

Every desk instrument (5 indices, 18 stocks; Adani Green is not in the desk): each session
is given a state by `onelot.mllab.states.classify` (rules fixed in advance, not tuned), and
plain counts of what followed what are tested against the instrument's own base rate with a
t that treats all instruments' sessions of one calendar day as one draw. The product's
`states.py` is loaded BY FILE PATH from the worktree this file sits in; the data and the
harness come from the checkout it runs from. Read-only on the store.

Run from the main checkout:
  PYTHONPATH=src:scripts:. .venv/bin/python -W ignore <this file>
Outputs go to data/research/mlstates_2026-11-23/ in that checkout.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path[:] = [x for x in sys.path if Path(x or ".").resolve() != Path(__file__).resolve().parent]
import mtf_bruteforce as h  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), h.__file__

_spec = importlib.util.spec_from_file_location(
    "mlstates_under_test", Path(__file__).resolve().parents[1] / "src/onelot/mllab/states.py"
)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)  # type: ignore[union-attr]

OUT = Path.cwd() / "data" / "research" / "mlstates_2026-11-23"
GROUPS = {"indices": list(h.INDEX_OPTIONS), "stocks": list(h.STOCKS)}
DIRS, SIZES = S.DIRS, S.SIZES


def fm(x: float, d: int = 1) -> str:
    return "nan" if x is None or not np.isfinite(x) else f"{x:.{d}f}".replace("-", "−")


def clustered_t(x: np.ndarray, day: np.ndarray) -> float:
    """mean / cluster-robust SE of x; all observations of one day are one draw."""
    x = np.asarray(x, float)
    if len(x) < 3:
        return float("nan")
    mu = x.mean()
    sums = pd.Series(x - mu).groupby(day).sum()
    se = np.sqrt((sums**2).sum()) / len(x)
    return float(mu / se) if se > 0 else float("nan")


def diff_t(x1, d1, x2, d2) -> float:
    """t of mean(x2) - mean(x1) with day clusters (the two samples share days)."""
    x1, x2 = np.asarray(x1, float), np.asarray(x2, float)
    a = pd.Series(x1 - x1.mean()).groupby(d1).sum() / len(x1)
    b = pd.Series(x2 - x2.mean()).groupby(d2).sum() / len(x2)
    z = b.sub(a, fill_value=0.0)
    se = np.sqrt((z**2).sum())
    return float((x2.mean() - x1.mean()) / se) if se > 0 else float("nan")


def load() -> dict[str, pd.DataFrame]:
    h._init_worker()
    out = {}
    for inst in GROUPS["indices"] + GROUPS["stocks"]:
        key = h._cache.spec(inst).series_key
        b = h._cache.bars(key, "1d")
        b = b[np.isfinite(b["close"]) & (b["close"] > 0)]
        st = S.classify(b)
        st.index = [pd.Timestamp(t).date() for t in st.index]
        out[inst] = st
    return out


def pairs(st: pd.DataFrame, inst: str) -> pd.DataFrame:
    """One row per stated session with a stated previous (and previous-but-one) session."""
    s = st[st.direction.notna()]
    base = s.direction.value_counts(normalize=True)
    sbase = (s["size"] == "active").mean()
    df = pd.DataFrame(
        {
            "inst": inst,
            "day": s.index,
            "d": s.direction.to_numpy(),
            "p1": s.direction.shift(1).to_numpy(),
            "p2": s.direction.shift(2).to_numpy(),
            "sz": s["size"].to_numpy(),
            "psz": s["size"].shift(1).to_numpy(),
            "r": s["r"].to_numpy(),
        }
    )
    for k in DIRS:
        df[f"b_{k}"] = base.get(k, 0.0)
    df["b_active"] = sbase
    return df


def cell(
    sel: pd.DataFrame, hit: np.ndarray, basecol: np.ndarray
) -> tuple[int, float, float, float]:
    n = len(sel)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan")
    p = hit.mean()
    return n, p, basecol.mean(), clustered_t(hit - basecol, sel["day"].to_numpy())


def tables(P: pd.DataFrame, name: str, L: list[str]) -> None:
    A = L.append
    A(f"\n=== {name}")
    s = P[P.d.notna()]
    A(
        "1. Base rates (share of stated sessions, pooled): "
        + " ".join(f"{k} {fm(100 * (s.d == k).mean())}%" for k in DIRS)
        + f" | active {fm(100 * (s.sz == 'active').mean())}% | stated sessions {len(s)}"
    )
    A("2. First order: P(next | yesterday) [n, P, base, edge pts, t]")
    for a in DIRS:
        sel = P[P.p1 == a]
        row = []
        for b in DIRS:
            n, p, bs, t = cell(sel, (sel.d == b).to_numpy(float), sel[f"b_{b}"].to_numpy())
            row.append(
                f"{b} {fm(100 * p)}% (base {fm(100 * bs)}, edge {fm(100 * (p - bs))}, t {fm(t, 2)})"
            )
        A(f"  after {a:4} n={len(sel):6}: " + " | ".join(row))
    A("   Second order: P(next | last two) [n, P, base, edge pts, t]")
    for a in DIRS:
        for c in DIRS:
            sel = P[(P.p2 == a) & (P.p1 == c)]
            row = []
            for b in DIRS:
                n, p, bs, t = cell(sel, (sel.d == b).to_numpy(float), sel[f"b_{b}"].to_numpy())
                row.append(
                    f"{b} {fm(100 * p)}% (b {fm(100 * bs)}, e {fm(100 * (p - bs))}, t {fm(t, 2)})"
                )
            A(f"  after {a:4},{c:4} n={len(sel):5}: " + " | ".join(row))
    A(
        "3. Owner's example and runs: P(same direction again | run of exactly 1, 2, 3+) [n, P, base, t]"
    )
    rows = []
    for _inst, G in P.groupby("inst", sort=False):
        d = G.d.to_numpy()
        # run length ending at i (the session before the one predicted at i+1)
        ln = np.zeros(len(d), int)
        for i in range(len(d)):
            ln[i] = ln[i - 1] + 1 if i and d[i] == d[i - 1] else 1
        nx = G.iloc[1:].copy()
        nx["prev_dir"] = d[:-1]
        nx["prev_run"] = np.minimum(ln[:-1], 3)
        rows.append(nx)
    R = pd.concat(rows)
    for k in ("bull", "bear"):
        cells, xs = [], {}
        for ln in (1, 2, 3):
            sel = R[(R.prev_dir == k) & (R.prev_run == ln)]
            hit = (sel.d == k).to_numpy(float)
            bs = sel[f"b_{k}"].to_numpy()
            n, p, b0, t = cell(sel, hit, bs)
            xs[ln] = (hit - bs, sel["day"].to_numpy())
            cells.append(
                f"run {ln}{'+' if ln == 3 else ''}: n={n} P={fm(100 * p)}% base {fm(100 * b0)}% t {fm(t, 2)}"
            )
        A(f"  {k}: " + " | ".join(cells))
        A(
            f"     run 2 minus run 1: {fm(100 * (xs[2][0].mean() - xs[1][0].mean()), 1)} pts, "
            f"t {fm(diff_t(xs[1][0], xs[1][1], xs[2][0], xs[2][1]), 2)}; "
            f"run 3+ minus run 1: {fm(100 * (xs[3][0].mean() - xs[1][0].mean()), 1)} pts, "
            f"t {fm(diff_t(xs[1][0], xs[1][1], xs[3][0], xs[3][1]), 2)}"
        )
    A("4. Size: P(active | yesterday) [n, P, base, edge pts, t]")
    for a in SIZES:
        sel = P[P.psz == a]
        n, p, bs, t = cell(sel, (sel.sz == "active").to_numpy(float), sel["b_active"].to_numpy())
        A(
            f"  after {a:6} n={n:6}: active {fm(100 * p)}% base {fm(100 * bs)}% edge {fm(100 * (p - bs))} t {fm(t, 2)}"
        )


def walk(all_w: pd.DataFrame, name: str, L: list[str]) -> None:
    A = L.append
    A(f"\n5. Walk-forward, {name}: n={len(all_w)} sessions, {all_w.inst.nunique()} instruments")
    day = all_w["day"].to_numpy()
    for lab, a, b, c in (
        ("direction", "lean", "naive", "actual"),
        ("size", "size_lean", "size_naive", "size_actual"),
    ):
        hit = (all_w[a] == all_w[c]).to_numpy(float)
        nv = (all_w[b] == all_w[c]).to_numpy(float)
        per = all_w.assign(h=hit, nv=nv).groupby("inst")[["h", "nv"]].mean()
        A(
            f"  {lab}: model {fm(100 * hit.mean())}% | most common so far {fm(100 * nv.mean())}% | "
            f"diff {fm(100 * (hit - nv).mean(), 2)} pts, t {fm(clustered_t(hit - nv, day), 2)} | "
            f"model better in {(per.h > per.nv).sum()} of {len(per)} instruments"
        )
    A("  by conviction (direction): n, hit, naive")
    for cv in ("none", "weak", "moderate", "strong"):
        s = all_w[all_w.conviction == cv]
        if len(s):
            A(
                f"    {cv:8} n={len(s):6} hit {fm(100 * (s.lean == s.actual).mean())}% "
                f"naive {fm(100 * (s.naive == s.actual).mean())}%"
            )
    A("  by conviction (size): n, hit, naive")
    for cv in ("none", "weak", "moderate", "strong"):
        s = all_w[all_w.size_conviction == cv]
        if len(s):
            A(
                f"    {cv:8} n={len(s):6} hit {fm(100 * (s.size_lean == s.size_actual).mean())}% "
                f"naive {fm(100 * (s.size_naive == s.size_actual).mean())}%"
            )


def pays(all_w: pd.DataFrame, name: str, L: list[str]) -> None:
    A = L.append
    A(f"\n6. Does a lean pay, {name}: next-session return, close to close, basis points")
    day = all_w["day"].to_numpy()
    rb = all_w["rbp"].to_numpy()
    A(f"  all sessions: n={len(rb)} mean {fm(rb.mean(), 2)} bp t {fm(clustered_t(rb, day), 2)}")
    for tag, mask in (
        ("any conviction above none", all_w.conviction != "none"),
        ("moderate or strong", all_w.conviction.isin(["moderate", "strong"])),
    ):
        A(f" {tag}:")
        for nm, m in (
            ("bull lean", mask & (all_w.lean == 0)),
            ("bear lean", mask & (all_w.lean == 2)),
            ("flat lean", mask & (all_w.lean == 1)),
            ("no lean", ~mask),
        ):
            x = rb[m.to_numpy()]
            A(
                f"    {nm:9} n={len(x):6} mean {fm(x.mean() if len(x) else np.nan, 2)} bp "
                f"t {fm(clustered_t(x, day[m.to_numpy()]), 2)}"
            )
        lm = mask & (all_w.lean != 1)
        sg = np.where(all_w.lean == 0, 1.0, -1.0)[lm.to_numpy()] * rb[lm.to_numpy()]
        A(
            f"    follow the lean (long bull, short bear): n={len(sg)} mean {fm(sg.mean() if len(sg) else np.nan, 2)} bp "
            f"t {fm(clustered_t(sg, day[lm.to_numpy()]), 2)}"
        )


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    states = load()
    L: list[str] = []
    walks = []
    pairs_all = []
    for inst, st in states.items():
        P = pairs(st, inst)
        pairs_all.append(P)
        w = S.walk_steps(st)
        if len(w):
            w["inst"] = inst
            w["day"] = w["date"]
            rmap = st["r"]
            w["rbp"] = [1e4 * (np.exp(rmap.loc[d]) - 1) for d in w["date"]]
            walks.append(w)
    PA = pd.concat(pairs_all, ignore_index=True)
    W = pd.concat(walks, ignore_index=True)
    L.append(
        f"instruments {len(states)}; stated sessions {PA.d.notna().sum()}; "
        f"sessions per instrument {PA.groupby('inst').size().min()} to {PA.groupby('inst').size().max()}"
    )
    for gname, keys in GROUPS.items():
        tables(PA[PA.inst.isin(keys)], gname, L)
        walk(W[W.inst.isin(keys)], gname, L)
        pays(W[W.inst.isin(keys)], gname, L)
    walk(W, "all 23", L)
    pays(W, "all 23", L)
    txt = "\n".join(L)
    (OUT / "report.txt").write_text(txt)
    W.to_pickle(OUT / "walk.pkl")
    print(txt)
    print(f"total {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
