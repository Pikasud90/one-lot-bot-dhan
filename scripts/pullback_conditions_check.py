"""US-101 check: an independent re-derivation of the pullback rule by market condition.

Written from the contract (`.agile/stories/US-101.md`) alone, before the builder's script and
outputs were opened. It re-derives: the three condition values of every trade; the trades,
RAW and EXCESS % a trade and the weekly-sum t of the 13 cells per period; each cell's status
by the rule; a placebo of 20 shuffles; and the reference book and BOOK-BROAD (end value, book
less B3 over the span). It reads US-100's `scripts/pullback_book.py` only for the book, the
cash rule, the stored session arrays and the benchmark definitions (as the contract allows).

Run from the worktree (inputs by absolute path, read-only):

    nice -n 10 .venv/bin/python scripts/pullback_conditions_check.py [--books]

Only pandas, numpy and sqlite3. Never an order: a check of a backtest, not advice.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
MAIN = Path("/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN")
OUT = MAIN / "data" / "research" / "pullback_conditions_2026-11-25"
US100 = MAIN / "data" / "research" / "pullback_book_2026-11-25"
TUNE_END, CHOOSE_END = pd.Timestamp("2024-11-07"), pd.Timestamp("2025-11-07")
PER = ("TUNE", "CHOOSE", "TEST")
SEED = 20261125


def load_book_module():
    spec = importlib.util.spec_from_file_location("pb", HERE / "pullback_book.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pb = load_book_module()


def period_of(d) -> np.ndarray:
    d = pd.DatetimeIndex(d)
    return np.select([d <= TUNE_END, d <= CHOOSE_END], ["TUNE", "CHOOSE"], "TEST")


def tw(x: np.ndarray, dates) -> float:
    """t of the mean of weekly sums: the trades of one ISO entry week are one draw."""
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    iso = pd.DatetimeIndex(dates).isocalendar()
    w = pd.Series(x).groupby([iso.year.to_numpy(), iso.week.to_numpy()]).sum()
    if len(w) < 2 or w.std(ddof=1) == 0:
        return float("nan")
    return float(w.mean() / (w.std(ddof=1) / np.sqrt(len(w))))


# ───────────────────────────── conditions ─────────────────────────────


def vix_series() -> pd.Series:
    con = sqlite3.connect(f"file:{pb.STORE}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ts, close FROM bars WHERE series_key = 'INDIAVIX' AND resolution = '1d' ORDER BY ts"
    ).fetchall()
    con.close()
    ts = pd.to_datetime([r[0] for r in rows], utc=True).tz_convert("Asia/Kolkata")
    s = pd.Series([float(r[1]) for r in rows], index=ts.tz_localize(None).normalize())
    return s[~s.index.duplicated(keep="last")]


def conditions(tr: pd.DataFrame) -> pd.DataFrame:
    """BREADTH, DEPTH, VIX labels of each trade, read at the close of its signal evening."""
    out = pd.DataFrame(index=tr.index)
    cnt = tr.groupby("signal_date").stock.transform("size")
    out["n_sig"] = cnt
    out["BREADTH"] = pd.cut(cnt, [0, 2, 9, 19, 10**6], labels=["1 to 2", "3 to 9", "10 to 19",
                                                               "20 or more"]).astype(object)  # fmt: skip
    nf = pb.nifty()
    hi = nf.rolling(20, min_periods=20).max()  # closes up to and including the evening
    dep = (1 - nf / hi) * 100
    out["depth_v"] = tr.signal_date.map(dep).to_numpy()
    d = out.depth_v
    out["DEPTH"] = np.select([d < 1, d < 2, d < 4, d >= 4], ["under 1%", "1 to 2%", "2 to 4%",
                                                             "4% or more"], None)  # fmt: skip
    out.loc[d.isna(), "DEPTH"] = None
    vx = vix_series()
    ratio = vx / vx.rolling(60, min_periods=60).median()
    out["vix_v"] = tr.signal_date.map(ratio).to_numpy()
    r = out.vix_v
    out["VIX"] = np.select([r < 0.9, r < 1.1, r < 1.3, r >= 1.3], ["under 0.9", "0.9 to 1.1",
                                                                   "1.1 to 1.3", "1.3 or more"], None)  # fmt: skip
    out.loc[r.isna(), "VIX"] = None
    out["ALL"] = "all"
    return out


# ───────────────────────────── the basket ─────────────────────────────


def basket_returns(tr: pd.DataFrame) -> np.ndarray:
    """Equal-weight basket over each trade's own days: each stock with a bar on both the entry
    day and the exit day, bought at the open of the entry day and sold at the open of the exit
    day (at the close of the last session for the one trade still open)."""
    fr = pb.load_frames()
    sess = pd.DatetimeIndex(sorted({d for f in fr.values() for d in f.index}))
    O = pd.DataFrame({s: f.open for s, f in fr.items()}).reindex(sess).to_numpy()
    C = pd.DataFrame({s: f.close for s, f in fr.items()}).reindex(sess).to_numpy()
    pos = {d: i for i, d in enumerate(sess)}
    res = np.full(len(tr), np.nan)
    cache: dict = {}
    for k, r in enumerate(tr.itertuples()):
        e, x = pos[r.entry_date], pos[r.exit_date]
        key = (e, x, bool(r.open_at_end))
        if key not in cache:
            a = O[e]
            b = C[x] if r.open_at_end else O[x]
            ok = np.isfinite(a) & np.isfinite(b)
            cache[key] = (np.mean(b[ok] / a[ok] - 1) * 100, int(ok.sum()))
        res[k] = cache[key][0]
    return res


# ───────────────────────────── cells ─────────────────────────────

CELLS = [("ALL", "all")]
for c, vals in (("BREADTH", ["1 to 2", "3 to 9", "10 to 19", "20 or more"]),
                ("DEPTH", ["under 1%", "1 to 2%", "2 to 4%", "4% or more"]),
                ("VIX", ["under 0.9", "0.9 to 1.1", "1.1 to 1.3", "1.3 or more"])):  # fmt: skip
    CELLS += [(c, v) for v in vals]


def cell_stats(d: pd.DataFrame, col: str) -> dict:
    """Per period and over the span: n, mean, weekly-sum t of the column `col`."""
    out = {}
    for p in (*PER, "ALL"):
        q = d if p == "ALL" else d[d.period == p]
        out[p] = {
            "n": int(len(q)), "days": int(q.entry_date.nunique()),
            "mean": float(q[col].mean()) if len(q) else float("nan"),
            "t": tw(q[col].to_numpy(), q.entry_date) if len(q) else float("nan"),
        }  # fmt: skip
    return out


def flags(s: dict) -> dict:
    """The selection on one set of cell statistics."""
    T, C, X = s["TUNE"], s["CHOOSE"], s["TEST"]
    ex = T["n"] >= 60 and T["days"] >= 30
    disc = ex and T["mean"] > 0 and T["t"] >= 2.5 and T["mean"] >= 0.5
    conf = disc and C["mean"] > 0 and C["t"] >= 1.0 and C["n"] >= 20
    pas = conf and X["mean"] > 0 and X["n"] >= 20
    avoid = ex and T["t"] <= -2.5 and C["mean"] < 0 and X["mean"] < 0
    return {"examined": bool(ex), "disc": bool(disc), "conf": bool(conf), "passed": bool(pas),
            "avoid": bool(avoid)}  # fmt: skip


def watch_second_route(s: dict) -> bool:
    """Positive in all three periods, span t of 2 or more, exactly one threshold missed."""
    T, C, X = s["TUNE"], s["CHOOSE"], s["TEST"]
    if not (T["mean"] > 0 and C["mean"] > 0 and X["mean"] > 0 and s["ALL"]["t"] >= 2):
        return False
    ok = [T["t"] >= 2.5 and T["mean"] >= 0.5 and T["n"] >= 60 and T["days"] >= 30,
          C["t"] >= 1.0 and C["n"] >= 20, X["n"] >= 20]  # fmt: skip
    return sum(not v for v in ok) == 1


def count_flags(df: pd.DataFrame, labels: dict[str, pd.Series], col: str) -> dict:
    n = {"examined": 0, "disc": 0, "conf": 0, "passed": 0, "avoid": 0}
    for c, v in CELLS:
        lab = df.ALL if c == "ALL" else labels[c]
        f = flags(cell_stats(df[lab == v], col))
        for k in n:
            n[k] += int(f[k])
    return n


def placebo(df: pd.DataFrame, col: str, runs: int = 20, seed: int = SEED) -> dict:
    """Shuffle each condition's labels over the EVENINGS inside each calendar year; an evening
    keeps all its trades. ALL stays real. Returns the mean and the maximum of the counts."""
    ev = df.groupby("signal_date").first()[["BREADTH", "DEPTH", "VIX"]]
    yr = ev.index.year
    rows = []
    for r in range(runs):
        rng = np.random.default_rng(seed + r)
        lab = {}
        for c in ("BREADTH", "DEPTH", "VIX"):
            s = ev[c].copy()
            for y in np.unique(yr):
                m = (yr == y) & s.notna().to_numpy()
                s.iloc[np.flatnonzero(m)] = rng.permutation(s.iloc[np.flatnonzero(m)].to_numpy())
            lab[c] = df.signal_date.map(s)
        # the real ALL cell is kept, so count it with the 12 shuffled ones
        rows.append(count_flags(df, lab, col))
    t = pd.DataFrame(rows)
    return {"mean": t.mean().round(3).to_dict(), "max": t.max().to_dict()}


# ───────────────────────────── books ─────────────────────────────


def books(df: pd.DataFrame) -> dict:
    """The reference book and BOOK-BROAD (and the other two named books), K = 10, Rs 1 lakh."""
    pb.OUT = US100  # US-100's cached prep, read only; nothing here calls a stage that writes
    tr, ss, P, X = pb.load_prep()
    key = tr[["stock", "signal_date"]].merge(
        df[["stock", "signal_date", "n_sig", "depth_v"]], on=["stock", "signal_date"], how="left"
    )
    assert len(key) == len(tr)
    m = len(ss)
    nsig = key.n_sig.to_numpy()
    deep = (key.depth_v >= 2).to_numpy()
    sets = {"REFERENCE": np.ones(len(tr), bool), "BROAD": nsig >= 10, "QUIET-OUT": nsig >= 3,
            "DEEP": deep}  # fmt: skip
    res = {}
    sess = ss.index
    for name, keep in sets.items():
        sub = tr[keep].reset_index(drop=True)
        rk = pb.ranks(sub)["PRODUCT"]
        st, used = pb.book(sub, rk, 10, pb.MONEY, m)
        h = pb.held(st)
        Ps, Xs = np.asarray(P)[keep], np.asarray(X)[keep]
        V = pb.MONEY + Ps[h].sum(0, dtype=np.float64)
        Xt = Xs[h].sum(0, dtype=np.float64)
        a, b = pb.bounds(sess, "ALL")
        years = (sess[b] - sess[a]).days / 365.25
        lg = pb.legs(V, Xt, ss, a, b)
        months = sess[a + 1 : b + 1].to_period("M").astype(str).to_numpy()
        bt = {k: pb.boot(lg, months, years, seed=k)["B3"] for k in range(40)}
        excl = np.mean([(v["pa_lo"] > 0) or (v["pa_hi"] < 0) for v in bt.values()]) * 100
        b0 = pb.boot(lg, months, years)
        res[name] = {
            "kept_trades": int(keep.sum()), "held": int(h.sum()),
            "end": float(lg["BOOK"][-1]), "growth": pb.growth(lg["BOOK"], years),
            "less_B1": b0["B1"]["pa"], "less_B2": b0["B2"]["pa"], "less_B3": b0["B3"]["pa"],
            "B3_ci": [b0["B3"]["pa_lo"], b0["B3"]["pa_hi"]], "seeds_excl_B3": float(excl),
            "B3_end": float(lg["B3"][-1]), "rupees_less_B3": b0["B3"]["rupees"],
        }  # fmt: skip
        print(name, res[name])
    return res


SRC_TRADES = pb.SWING / "trades_P1.pkl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tr = pd.read_pickle(SRC_TRADES).reset_index(drop=True)
    tr["period"] = period_of(tr.entry_date)
    cond = conditions(tr)
    df = pd.concat([tr, cond], axis=1)
    L = []
    for c in ("BREADTH", "DEPTH", "VIX"):
        L.append(f"{c} trades per value: {df[c].value_counts(dropna=False).to_dict()}")
    cache = OUT / "check_basket.npy"
    if cache.exists():
        bk = np.load(cache)
    else:
        bk = basket_returns(tr)
        np.save(cache, bk)
    df["basket"] = bk
    df["excess_b"] = df.net_pct - df.basket
    L.append(f"basket NaN: {int(np.isnan(bk).sum())}; mean basket {np.nanmean(bk):.3f}")
    ex_stat = {}
    for c, v in CELLS:
        d = df[df[c] == v]
        raw, exc = cell_stats(d, "net_pct"), cell_stats(d, "excess_b")
        spn = cell_stats(d, "excess_b")["ALL"]
        fe, fr = flags(exc), flags(raw)
        ex_stat[(c, v)] = (exc, raw, fe, fr)
        L.append(
            f"{c} | {v} | n {raw['TUNE']['n']}/{raw['CHOOSE']['n']}/{raw['TEST']['n']} | "
            f"RAW "
            + " / ".join(f"{raw[p]['mean']:+.2f} ({raw[p]['t']:+.1f})" for p in PER)
            + f" span {raw['ALL']['mean']:+.2f} ({raw['ALL']['t']:+.1f}) | EXC "
            + " / ".join(f"{exc[p]['mean']:+.2f} ({exc[p]['t']:+.1f})" for p in PER)
            + f" span {spn['mean']:+.2f} ({spn['t']:+.1f}) | "
            + " ".join(k for k, v2 in fe.items() if v2)
            + " | RAW: "
            + " ".join(k for k, v2 in fr.items() if v2)
            + (" watch2" if watch_second_route(raw) else "")
        )
    tot_e = {
        k: sum(int(s[2][k]) for s in ex_stat.values())
        for k in ("examined", "disc", "conf", "passed", "avoid")
    }
    tot_r = {
        k: sum(int(s[3][k]) for s in ex_stat.values())
        for k in ("examined", "disc", "conf", "passed", "avoid")
    }
    L.append(f"counts EXCESS {tot_e}; RAW {tot_r}")
    n = tot_e["examined"]
    L.append(
        f"luck arithmetic: disc {n * 0.006:.4f} conf {n * 0.006 * 0.16:.4f} "
        f"passed {n * 0.006 * 0.16 * 0.5:.4f}"
    )
    L.append(f"placebo EXCESS: {placebo(df, 'excess_b')}")
    L.append(f"placebo RAW: {placebo(df, 'net_pct')}")
    # NIFTY-based second excess (study's bench_pct)
    df["excess_n"] = df.net_pct - df.bench_pct
    for c, v in CELLS:
        s = cell_stats(df[df[c] == v], "excess_n")
        L.append(
            f"NIFTY-bench excess {c} {v}: "
            + " / ".join(f"{s[p]['mean']:+.2f} ({s[p]['t']:+.1f})" for p in (*PER, "ALL"))
        )
    if a.books:
        for k, v in books(df).items():
            L.append(f"BOOK {k}: {json.dumps(v)}")
    txt = "\n".join(L)
    (OUT / "check_numbers.txt").write_text(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
