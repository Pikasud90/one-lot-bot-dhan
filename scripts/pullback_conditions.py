"""US-101: the pullback rule by market condition (study; fixed before any result).

The study's 8,085 pullback trades (`trades_P1.pkl`, unchanged) are split by three conditions,
each read at the close of the trade's signal evening, one condition at a time:
BREADTH (signals that evening among the 222 stocks), DEPTH (NIFTY below its highest close of
20 sessions) and VIX (India VIX over its median close of 60 sessions); with ALL that is 13
cells. Each cell is measured on RAW (`net_pct`) and on EXCESS: RAW less the equal-weight
basket of the 222 stocks over the trade's own holding days. The selection of
RESEARCH-RULES section 5 runs on the EXCESS; on RAW it is reported beside ("with drift").
Then three named books of US-100's kind (K = 10, the product's order, Rs 1 lakh, the cash
rule) against B1, B2, B3, and the power for a paper test.

US-100's module `scripts/pullback_book.py` is IMPORTED BY FILE PATH, not copied: its prep
(trades with session numbers, the marks P and exposures X, NIFTY and the basket by session),
`run_book`, `ranks`, `figures`, `legs`, `boot` and `tweek` are used unchanged; its `OUT` is
pointed at this study's folder before its prep runs.

Run from the worktree (inputs by absolute path, read-only; outputs to --out):

    nice -n 10 .venv/bin/python scripts/pullback_conditions.py --stage prep
    nice -n 10 .venv/bin/python scripts/pullback_conditions.py --stage cells
    nice -n 10 .venv/bin/python scripts/pullback_conditions.py --stage books
    nice -n 10 .venv/bin/python scripts/pullback_conditions.py --stage report

Only pandas, numpy and sqlite3. Never an order: this is a backtest of a rule, not advice.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

_spec = importlib.util.spec_from_file_location(
    "pullback_book", Path(__file__).resolve().parent / "pullback_book.py"
)
pb = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(pb)  # type: ignore[union-attr]

OUT = pb.MAIN / "data" / "research" / "pullback_conditions_2026-11-25"
PERIODS = ("TUNE", "CHOOSE", "TEST", "ALL")
K = 10
MONEY = 100_000.0
BOOT_SEED = 20261125
BOOT_DRAWS = 2000
SEEDS = range(40)
PLACEBO_RUNS = 20
PLACEBO_SEED = 101_000
TRUNC_N = 50
MIN_WEEKS = 5  # display only: no t printed for a period of fewer entry weeks (review)
CONDITIONS: dict[str, tuple[tuple[float, ...], tuple[str, ...]]] = {
    # a value x falls in the first name whose upper edge is above x (edges are "x < edge")
    "BREADTH": ((3, 10, 20), ("1 to 2", "3 to 9", "10 to 19", "20 or more")),
    "DEPTH": ((1.0, 2.0, 4.0), ("under 1%", "1 to 2%", "2 to 4%", "4% or more")),
    "VIX": ((0.9, 1.1, 1.3), ("under 0.9", "0.9 to 1.1", "1.1 to 1.3", "1.3 or more")),
}
COND_COL = {"BREADTH": "breadth", "DEPTH": "depth_nifty", "VIX": "vix_ratio"}
BOOKS = ("US-100 (all evenings)", "BOOK-QUIET-OUT", "BOOK-BROAD", "BOOK-DEEP")


# ───────────────────────────── pure parts (tested on hand-made evenings) ─────────────────────


def bin_label(x, edges, names) -> np.ndarray:
    """The condition's value for each x; a missing x gets None (left out of the condition)."""
    x = np.asarray(x, dtype=float)
    idx = np.searchsorted(np.asarray(edges, dtype=float), x, side="right")
    out = np.array([names[i] for i in np.minimum(idx, len(names) - 1)], dtype=object)
    out[np.isnan(x)] = None
    return out


def depth_pct(close: pd.Series, n: int = 20) -> pd.Series:
    """How far each close stands below the highest close of the last n sessions (this one
    included), in %. NaN until n closes exist. Uses only closes up to that session."""
    hi = close.rolling(n, min_periods=n).max()
    return (1 - close / hi) * 100


def vix_ratio(close: pd.Series, n: int = 60) -> pd.Series:
    """Each close over the median close of the last n sessions (this one included); NaN until
    n closes exist."""
    return close / close.rolling(n, min_periods=n).median()


def breadth(signal_dates: pd.Series) -> np.ndarray:
    """For each trade, the number of trades with the same signal evening."""
    s = pd.Series(pd.DatetimeIndex(signal_dates))
    return s.map(s.value_counts()).to_numpy()


def shuffle_within_year(dates, labels, rng: np.random.Generator) -> np.ndarray:
    """The placebo: the labels of the EVENINGS (one row per evening) permuted among the
    evenings of the same calendar year. Evenings without a label (None) stay out and keep
    None. A trade follows its evening, so an evening keeps all its trades."""
    years = pd.DatetimeIndex(dates).year.to_numpy()
    lab = np.asarray(labels, dtype=object).copy()
    have = np.array([v is not None for v in lab])
    for y in np.unique(years):
        idx = np.flatnonzero((years == y) & have)
        lab[idx] = lab[idx][rng.permutation(len(idx))]
    return lab


def select(c: dict) -> dict:
    """RESEARCH-RULES section 5 for delivery, on one measure. `c[p]` holds n, days, mean, t for
    TUNE, CHOOSE, TEST and ALL. Returns the step reached, AVOID, the WATCH route and the
    number that decided it."""
    tu, ch, te, al = c["TUNE"], c["CHOOSE"], c["TEST"], c["ALL"]

    def ge(a, b):
        return bool(np.isfinite(a) and a >= b)

    examined = tu["n"] >= 60 and tu["days"] >= 30
    t_ok, size_ok = tu["mean"] > 0 and ge(tu["t"], 2.5), ge(tu["mean"], 0.50)
    ch_n, ch_ok = ch["n"] >= 20, ch["n"] > 0 and ch["mean"] > 0 and ge(ch["t"], 1.0)
    te_ok = te["n"] >= 20 and te["mean"] > 0
    disc = examined and t_ok and size_ok
    conf = disc and ch_n and ch_ok
    passed = conf and te_ok
    avoid = bool(examined and ge(-tu["t"], 2.5) and ch["n"] > 0 and ch["mean"] < 0
                 and te["n"] > 0 and te["mean"] < 0)  # fmt: skip
    missed = [not t_ok, not size_ok, not ch_n, not ch_ok, not te_ok]
    pos3 = all(c[p]["n"] > 0 and c[p]["mean"] > 0 for p in ("TUNE", "CHOOSE", "TEST"))
    watch = bool(examined and not passed and pos3 and ge(al["t"], 2.0) and sum(missed) == 1)
    if not examined:
        step, why = "not examined", f"TUNE {tu['n']} trades on {tu['days']} entry days"
    elif not disc:
        step = "REJECTED"
        why = (f"TUNE t {fs(tu['t'], 1)} under 2.5" if not t_ok
               else f"TUNE {fs(tu['mean'])}% under +0.50")  # fmt: skip
    elif not conf:
        step = "DISCOVERED only"
        why = (f"CHOOSE {ch['n']} trades" if not ch_n
               else f"CHOOSE {fs(ch['mean'])}% t {fs(ch['t'], 1)}")  # fmt: skip
    elif not passed:
        step, why = "CONFIRMED only", f"TEST {te['n']} trades, {fs(te['mean'])}%"
    else:
        step, why = "PASSED", f"TEST {fs(te['mean'])}% on {te['n']} trades"
    if avoid:
        why = f"TUNE t {fs(tu['t'], 1)}, CHOOSE {fs(ch['mean'])}, TEST {fs(te['mean'])}"
    return {"examined": bool(examined), "discovered": bool(disc), "confirmed": bool(conf),
            "passed": bool(passed), "avoid": avoid, "watch_route": watch, "step": step,
            "why": why}  # fmt: skip


def fs(x, d: int = 2) -> str:
    return pb.fs(x, d)


# ───────────────────────────── inputs (read-only) ─────────────────────────────


def store_daily(key: str, col: str = "close") -> pd.Series:
    """A daily series of the store (bars, 1d, read-only), indexed by the Indian date."""
    if key not in ("NIFTY", "INDIAVIX") or col not in ("open", "close"):
        raise ValueError(key)
    con = sqlite3.connect(f"file:{pb.STORE}?mode=ro", uri=True)
    rows = con.execute(
        f"SELECT ts, {col} FROM bars WHERE series_key = ? AND resolution = '1d' ORDER BY ts",
        (key,),
    ).fetchall()
    con.close()
    # UTC to India's clock time (+05:30), with no zone object (tests/test_ist_zone.py)
    ts = pd.to_datetime([r[0] for r in rows], utc=True) + np.timedelta64(330, "m")
    s = pd.Series([float(r[1]) for r in rows], index=ts.tz_localize(None).normalize())
    s = s[~s.index.duplicated(keep="last")]
    return s[s.index <= pb.LAST]


def nifty_open() -> pd.Series:
    """NIFTY's daily opens joined as `pb.nifty` joins the closes (the CSV before the store)."""
    st = store_daily("NIFTY", "open")
    y = pd.read_csv(pb.NIFTY_CSV, index_col=0, parse_dates=True)["open"].dropna()
    y.index = pd.DatetimeIndex(y.index).normalize()
    return pd.concat([y[y.index < st.index[0]], st]).sort_index()


# ───────────────────────────── stage prep ─────────────────────────────


def stage_prep() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pb.stage_prep()  # US-100's prep, written to this study's folder (pb.OUT = OUT)
    tr, ss, _, _ = pb.load_prep()
    fr = pb.load_frames()
    sess = ss.index
    stocks = sorted(fr)
    O = pd.DataFrame({s: fr[s].open for s in stocks}).reindex(sess).to_numpy()
    C = pd.DataFrame({s: fr[s].close for s in stocks}).reindex(sess).to_numpy()
    L: list[str] = []
    # conditions at the signal evening's close
    tr["breadth"] = breadth(tr.signal_date)
    nf = pb.nifty()
    dep = depth_pct(nf)
    tr["depth_nifty"] = tr.signal_date.map(dep).astype(float)  # NaN: no NIFTY close that day
    vx = store_daily("INDIAVIX")
    vr = vix_ratio(vx)
    tr["vix_ratio"] = tr.signal_date.map(vr).astype(float)
    ev = tr.drop_duplicates("signal_date")
    L.append(f"trades {len(tr)}, evenings {len(ev)}")
    L.append(f"DEPTH left out (no NIFTY close of 20 sessions on the evening): "
             f"{int(ev.depth_nifty.isna().sum())} evenings, "
             f"{int(tr.depth_nifty.isna().sum())} trades")  # fmt: skip
    no_bar = ~ev.signal_date.isin(vx.index)
    L.append(
        f"VIX left out: {int(ev.vix_ratio.isna().sum())} evenings, "
        f"{int(tr.vix_ratio.isna().sum())} trades (no INDIAVIX bar that evening "
        f"{int(no_bar.sum())} evenings, of them before the store's first bar "
        f"{int((ev.signal_date < vx.index[0]).sum())}; a bar but fewer than 60 closes "
        f"{int((~no_bar & ev.vix_ratio.isna()).sum())}); INDIAVIX closes {len(vx)} from "
        f"{vx.index[0].date()}, first ratio {vr.dropna().index[0].date()}"
    )
    # EXCESS: the equal-weight basket bought at the entry open, sold at the exit open
    e = tr.ent_i.to_numpy()
    x = tr.ext_i.to_numpy()
    m = len(sess)
    bk, nb = np.empty(len(tr)), np.empty(len(tr), dtype=int)
    for i in range(len(tr)):
        p0 = O[e[i]]
        p1 = C[m - 1] if x[i] >= m else O[x[i]]  # still open at the end: the last close
        ok = np.isfinite(p0) & np.isfinite(p1)
        bk[i] = float(np.mean(p1[ok] / p0[ok] - 1) * 100)
        nb[i] = int(ok.sum())
    tr["basket_pct"], tr["basket_n"] = bk, nb
    tr["excess_basket"] = tr.net_pct - tr.basket_pct
    no = nifty_open()
    nc = nf
    n0 = tr.entry_date.map(no).astype(float)
    n1 = np.where(tr.open_at_end, tr.exit_date.map(nc), tr.exit_date.map(no)).astype(float)
    tr["nifty_pct"] = (n1 / n0.to_numpy() - 1) * 100
    tr["excess_nifty"] = tr.net_pct - tr.nifty_pct
    tr["excess_study"] = tr.net_pct - tr.bench_pct  # the study's own column, unchanged
    # A sensitivity added after the first results (review of 2026-11-25), never selecting:
    # the trade before its costs, fill to fill, less the same basket (which has no costs).
    tr["gross_pct"] = (tr.exit_raw / tr.entry_open - 1) * 100
    tr["gross_excess"] = tr.gross_pct - tr.basket_pct
    L.append(f"costs a trade (net_pct less the fill-to-fill move): mean "
             f"{fs((tr.gross_pct - tr.net_pct).mean())}%, median "
             f"{fs((tr.gross_pct - tr.net_pct).median())}%")  # fmt: skip
    L.append(f"basket: stocks in it per trade min {nb.min()}, median {int(np.median(nb))}, "
             f"max {nb.max()}; NIFTY open missing on entry or exit for "
             f"{int(tr.nifty_pct.isna().sum())} trades")  # fmt: skip
    # labels
    for name, (edges, names) in CONDITIONS.items():
        tr[f"lab_{name}"] = bin_label(tr[COND_COL[name]].to_numpy(), edges, names)
    # no look-ahead: cut every input after the signal evening and recompute (50 signals).
    # For BREADTH this holds by construction (the count is taken from the fixed trade list);
    # the count is known at the close because the study's loop (swing_universe.py `loop`)
    # lets a stock signal again from its sale day's close and the NIFTY filter is a close.
    rng = np.random.default_rng(PLACEBO_SEED)
    pick = rng.choice(len(tr), size=TRUNC_N, replace=False)
    bad = 0
    for i in pick:
        d = tr.signal_date.iloc[i]
        dd = depth_pct(nf[nf.index <= d]).get(d, np.nan)
        vv = vix_ratio(vx[vx.index <= d]).get(d, np.nan)
        bb = int((tr.signal_date[tr.signal_date <= d] == d).sum())
        same = (np.isclose(dd, tr.depth_nifty.iloc[i], equal_nan=True)
                and np.isclose(vv, tr.vix_ratio.iloc[i], equal_nan=True)
                and bb == tr.breadth.iloc[i])  # fmt: skip
        bad += 0 if same else 1
    L.append(f"truncation test: {TRUNC_N} random signals, inputs cut after the signal evening; "
             f"values that changed: {bad}")  # fmt: skip
    tr.to_pickle(OUT / "cond.pkl")
    txt = "\n".join(L)
    (OUT / "prep.txt").write_text(txt + "\n")
    print(txt)


# ───────────────────────────── stage cells ─────────────────────────────


def period_stats(x: np.ndarray, per: np.ndarray, entry: pd.Series, sig: pd.Series,
                 week: np.ndarray) -> dict:  # fmt: skip
    out = {}
    for p in PERIODS:
        msk = np.ones(len(x), bool) if p == "ALL" else per == p
        msk = msk & np.isfinite(x)  # only the excess over NIFTY has gaps (no NIFTY bar)
        xs = x[msk]
        out[p] = {
            "n": int(msk.sum()), "days": int(entry[msk].nunique()),
            "evenings": int(sig[msk].nunique()), "weeks": int(len(np.unique(week[msk]))),
            "mean": float(xs.mean()) if len(xs) else float("nan"),
            "t": pb.tweek(xs, entry[msk]) if len(xs) else float("nan"),
        }  # fmt: skip
    return out


def cell_table(tr: pd.DataFrame, labels: dict[str, np.ndarray], measures: list[str]) -> dict:
    """Every cell's figures. `labels[cond]`: one label per trade (None: left out)."""
    per = tr.period.to_numpy()
    iso = pd.DatetimeIndex(tr.entry_date).isocalendar()
    week = (iso.year.to_numpy() * 100 + iso.week.to_numpy()).astype(int)
    out: dict = {}
    cells = [("ALL", "all", np.ones(len(tr), bool))]
    for cond, (_, names) in CONDITIONS.items():
        lab = labels[cond]
        for v in names:
            cells.append((cond, v, lab == v))
    for cond, v, msk in cells:
        g = tr[msk]
        row = {}
        for mcol in measures:
            st = period_stats(g[mcol].to_numpy(float), per[msk], g.entry_date.reset_index(
                drop=True), g.signal_date.reset_index(drop=True), week[msk])  # fmt: skip
            row[mcol] = {"stats": st, "sel": select(st)}
        out[f"{cond}|{v}"] = row
    return out


def counts(cells: dict, measure: str) -> dict:
    keys = ("examined", "discovered", "confirmed", "passed", "avoid")
    return {k: int(sum(c[measure]["sel"][k] for c in cells.values())) for k in keys}


def stage_cells() -> None:
    tr = pd.read_pickle(OUT / "cond.pkl")
    measures = ["excess_basket", "net_pct", "excess_nifty", "excess_study", "basket_pct",
                "gross_excess"]  # fmt: skip
    labels = {c: tr[f"lab_{c}"].to_numpy(object) for c in CONDITIONS}
    real = cell_table(tr, labels, measures)
    # placebo: each condition's labels shuffled over the evenings inside each calendar year
    ev_first = tr.drop_duplicates("signal_date")
    ev_dates = ev_first.signal_date.to_numpy()
    ev_of = pd.Index(ev_dates).get_indexer(tr.signal_date)
    plac: dict = {"runs": []}
    sel_measures = ["excess_basket", "net_pct"]
    for r in range(PLACEBO_RUNS):
        run = {}
        for ci, cond in enumerate(CONDITIONS):
            rng = np.random.default_rng(PLACEBO_SEED + 100 * ci + r)
            ev_lab = ev_first[f"lab_{cond}"].to_numpy(object)
            sh = shuffle_within_year(ev_dates, ev_lab, rng)
            lab = {c: np.full(len(tr), None, dtype=object) for c in CONDITIONS}
            lab[cond] = sh[ev_of]
            ct = cell_table(tr, lab, sel_measures)
            sub = {k: v for k, v in ct.items() if k.startswith(cond + "|")}
            run[cond] = {mm: counts(sub, mm) for mm in sel_measures}
        plac["runs"].append(run)
        print("placebo", r, {c: run[c]["excess_basket"]["passed"] for c in CONDITIONS})
    (OUT / "cells.json").write_text(json.dumps(real, indent=1, default=float))
    (OUT / "placebo.json").write_text(json.dumps(plac, indent=1, default=float))
    for k, v in real.items():
        s = v["excess_basket"]
        print(k, s["stats"]["TUNE"]["n"], fs(s["stats"]["TUNE"]["mean"]),
              fs(s["stats"]["TUNE"]["t"], 1), s["sel"]["step"])  # fmt: skip


def luck(real: dict, plac: dict, measure: str) -> dict:
    """Real counts over the 13 cells against the arithmetic and the placebo. In a placebo
    round the ALL cell keeps its real result; the 12 conditioned cells are shuffled."""
    rc = counts(real, measure)
    all_cell = {k: int(real["ALL|all"][measure]["sel"][k]) for k in rc}
    tot = []
    for run in plac["runs"]:
        tot.append({k: all_cell[k] + sum(run[c][measure][k] for c in CONDITIONS) for k in rc})
    tdf = pd.DataFrame(tot)
    e = rc["examined"]
    arith = {"discovered": e * 0.006, "confirmed": e * 0.006 * 0.16,
             "passed": e * 0.006 * 0.16 * 0.5}  # fmt: skip
    clear = bool(rc["passed"] >= 1 and rc["passed"] > arith["passed"]
                 and rc["passed"] > tdf.passed.max())  # fmt: skip
    return {"real": rc, "arith": arith, "placebo_mean": tdf.mean().to_dict(),
            "placebo_max": tdf.max().to_dict(), "clearly_above": clear}  # fmt: skip


def status(cell: dict, lk: dict) -> str:
    s = cell["sel"]
    if s["avoid"]:
        return "AVOID"
    if s["passed"]:
        return "CANDIDATE" if lk["clearly_above"] else "WATCH"
    if s["watch_route"]:
        return "WATCH"
    return s["step"]


# ───────────────────────────── stage books ─────────────────────────────


def book_masks(tr: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "US-100 (all evenings)": np.ones(len(tr), bool),
        "BOOK-QUIET-OUT": (tr.breadth >= 3).to_numpy(),
        "BOOK-BROAD": (tr.breadth >= 10).to_numpy(),
        "BOOK-DEEP": (tr.depth_nifty >= 2.0).to_numpy(),  # NaN depth: not taken
    }


def stage_books() -> None:
    tr = pd.read_pickle(OUT / "cond.pkl")
    _, ss, P, X = pb.load_prep()
    sess = ss.index
    m = len(sess)
    rank_all = pb.ranks(tr)["PRODUCT"]
    res: dict = {}
    states: dict = {}
    for name, msk in book_masks(tr).items():
        idx = np.flatnonzero(msk)
        sub = tr.iloc[idx].reset_index(drop=True)
        st, used = pb.book(sub, rank_all[idx], K, MONEY, m)
        Ps, Xs = np.asarray(P[idx]), np.asarray(X[idx])
        f = pb.figures(sub, ss, Ps, Xs, st, used, K, MONEY)
        h = pb.held(st)
        V = MONEY + Ps[h].sum(0, dtype=np.float64)
        Xt = Xs[h].sum(0, dtype=np.float64)
        for p in PERIODS:
            a, b = pb.bounds(sess, p)
            years = (sess[b] - sess[a]).days / 365.25
            lg = pb.legs(V, Xt, ss, a, b)
            months = sess[a + 1 : b + 1].to_period("M").astype(str).to_numpy()
            seeds: dict = {}
            for blk in (1, 3):
                ex = dict.fromkeys(("B1", "B2", "B3"), 0)
                for sd in SEEDS:
                    v = pb.boot(lg, months, years, seed=sd, block=blk)
                    for bn in ex:
                        ex[bn] += int(v[bn]["pa_lo"] > 0 or v[bn]["pa_hi"] < 0)
                seeds[f"block{blk}"] = {bn: ex[bn] / len(SEEDS) for bn in ex}
            f[p]["seeds"] = seeds
            f[p]["vs_block3"] = pb.boot(lg, months, years, block=3)
            q = sub[h & ((sub.period == p) | (p == "ALL"))]
            f[p]["excess_basket"] = float(q.excess_basket.mean()) if len(q) else float("nan")
            f[p]["t_excess"] = (pb.tweek(q.excess_basket.to_numpy(), q.entry_date)
                                if len(q) > 1 else float("nan"))  # fmt: skip
        f["eligible"] = int(len(idx))
        f["evenings"] = int(sub.signal_date.nunique())
        res[name] = f
        full = np.zeros(len(tr), np.int8)
        full[idx] = st + 1  # 0: not eligible
        states[name] = full
        print(name, len(idx), int(h.sum()), pb.lakh(f["ALL"]["end"]), fs(f["ALL"]["growth"]))
    pd.DataFrame(states).to_pickle(OUT / "book_states.pkl")
    (OUT / "books.json").write_text(json.dumps(res, indent=1, default=float))


# ───────────────────────────── stage report ─────────────────────────────


def power_rows(tr: pd.DataFrame, msk: np.ndarray, span_m: float) -> list[dict]:
    rows = []
    for p in ("ALL", "TEST"):
        g = tr[msk & ((tr.period == p).to_numpy() | (p == "ALL"))]
        months = span_m if p == "ALL" else 12.0
        row = {"period": p, "n": len(g), "rate": len(g) / months,
               "evenings_m": g.signal_date.nunique() / months}  # fmt: skip
        for mcol in ("excess_basket", "net_pct"):
            tw = pb.tweek(g[mcol].to_numpy(), g.entry_date) if len(g) > 2 else float("nan")
            row[mcol] = {"mean": float(g[mcol].mean()) if len(g) else float("nan"), "t": tw,
                         "months": months * (2 / tw) ** 2 if tw > 0 else float("inf")}  # fmt: skip
        rows.append(row)
    return rows


def stage_report() -> None:  # noqa: C901 - a report, read top to bottom
    tr = pd.read_pickle(OUT / "cond.pkl")
    real = json.loads((OUT / "cells.json").read_text())
    plac = json.loads((OUT / "placebo.json").read_text())
    books = json.loads((OUT / "books.json").read_text())
    ss = pd.read_pickle(OUT / "sessions.pkl")
    L: list[str] = [(OUT / "prep.txt").read_text()]
    lk = {mm: luck(real, plac, mm) for mm in ("excess_basket", "net_pct")}
    # the 13 cells
    L.append("\n## The 13 cells (EXCESS over the basket selects; RAW is 'with drift')\n")
    L.append("| Condition | Value | Trades T / C / T | Evenings T / C / T | Weeks T / C / T | "
             "RAW % (t) TUNE / CHOOSE / TEST | RAW span (t) | EXCESS % (t) TUNE / CHOOSE / TEST "
             "| EXCESS span (t) | Status (excess) | Decided by | With drift (raw) |")  # fmt: skip
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    stat_of = {}
    for key, c in real.items():
        cond, v = key.split("|")
        e, r = c["excess_basket"], c["net_pct"]
        se, sr = e["stats"], r["stats"]

        def tri(st, f):
            return " / ".join(f(st[p]) for p in ("TUNE", "CHOOSE", "TEST"))

        def mt(s):
            # a t on fewer than MIN_WEEKS entry weeks is not printed (it selects unchanged)
            if s["weeks"] < MIN_WEEKS:
                return f"{fs(s['mean'])} (n/a)" if s["n"] else "none"
            return f"{fs(s['mean'])} ({fs(s['t'], 1)})"

        stx = status(e, lk["excess_basket"])
        str_ = status(r, lk["net_pct"])
        stat_of[key] = (stx, str_)
        L.append(
            f"| {cond} | {v} | {tri(se, lambda s: str(s['n']))} | "
            f"{tri(se, lambda s: str(s['evenings']))} | {tri(se, lambda s: str(s['weeks']))} | "
            f"{tri(sr, mt)} | "
            f"{fs(sr['ALL']['mean'])} ({fs(sr['ALL']['t'], 1)}) | "
            f"{tri(se, mt)} | "
            f"{fs(se['ALL']['mean'])} ({fs(se['ALL']['t'], 1)}) | {stx} | {e['sel']['why']} | "
            f"{str_} ({r['sel']['why']}) |"
        )
    L.append(
        "\n### The other excesses and the basket itself (% a trade, TUNE / CHOOSE / TEST, span)\n"
    )
    L.append("| Cell | basket over the holding days | over NIFTY, same days | over the study's "
             "bench_pct (same stock, same length) | before costs, over the basket "
             "(sensitivity added after the first results) | entry days TUNE |")  # fmt: skip
    L.append("|---|---|---|---|---|---|")
    for key, c in real.items():
        cells = []
        for mm in ("basket_pct", "excess_nifty", "excess_study", "gross_excess"):
            s = c[mm]["stats"]
            cells.append(" / ".join(fs(s[p]["mean"]) for p in PERIODS))
        L.append(
            f"| {key} | " + " | ".join(cells) + f" | {c['net_pct']['stats']['TUNE']['days']} |"
        )
    L.append("\nThe selection run on these other measures (reported only, none selects): step "
             "reached (TUNE t, span t)\n")  # fmt: skip
    L.append("| Cell | over NIFTY | over bench_pct | before costs, over the basket |")
    L.append("|---|---|---|---|")
    for key, c in real.items():
        L.append(f"| {key} | " + " | ".join(
            f"{status(c[mm], {'clearly_above': False})} ({fs(c[mm]['stats']['TUNE']['t'], 1)}, "
            f"{fs(c[mm]['stats']['ALL']['t'], 1)})"
            for mm in ("excess_nifty", "excess_study", "gross_excess")) + " |")  # fmt: skip
    # luck
    head = (
        "\n## Against luck (13 cells; placebo: 20 shuffles of each condition's labels "
        "over the evenings inside each calendar year, ALL keeps its real result)\n"
    )
    L.append(head)
    L.append("| Measure | | Examined | Discovered | Confirmed | Passed | AVOID |")
    L.append("|---|---|---|---|---|---|---|")
    for mm, lab in (("excess_basket", "EXCESS"), ("net_pct", "RAW (with drift)")):
        x = lk[mm]
        L.append(f"| {lab} | real | " + " | ".join(str(x["real"][k]) for k in
                 ("examined", "discovered", "confirmed", "passed", "avoid")) + " |")  # fmt: skip
        L.append(f"| {lab} | arithmetic | {x['real']['examined']} | {x['arith']['discovered']:.3f} "
                 f"| {x['arith']['confirmed']:.4f} | {x['arith']['passed']:.4f} | |")  # fmt: skip
        L.append(f"| {lab} | placebo mean (max) | " + " | ".join(
            f"{x['placebo_mean'][k]:.2f} ({x['placebo_max'][k]:.0f})" for k in
            ("examined", "discovered", "confirmed", "passed", "avoid")) + " |")  # fmt: skip
        L.append(f"| {lab} | passes clearly above luck | {x['clearly_above']} | | | | |")
    for cond in CONDITIONS:
        pm = pd.DataFrame([r[cond]["excess_basket"] for r in plac["runs"]]).mean()
        L.append(f"- placebo {cond} (EXCESS), mean counts: " + ", ".join(
            f"{k} {v:.2f}" for k, v in pm.items()))  # fmt: skip
    # neighbours
    L.append("\n## Neighbours of PASSED or AVOID cells (EXCESS % a trade (t), n)\n")
    any_n = False
    for key, c in real.items():
        if key == "ALL|all":
            continue
        cond, v = key.split("|")
        for mm in ("excess_basket", "net_pct"):
            sel = c[mm]["sel"]
            if not (sel["passed"] or sel["avoid"]):
                continue
            any_n = True
            names = CONDITIONS[cond][1]
            i = names.index(v)
            L.append(f"- {key} ({'PASSED' if sel['passed'] else 'AVOID'} on {mm}):")
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(names):
                    s = real[f"{cond}|{names[j]}"][mm]["stats"]
                    L.append(f"    {names[j]}: " + " / ".join(
                        f"{fs(s[p]['mean'])} ({fs(s[p]['t'], 1)}) n {s[p]['n']}"
                        for p in ("TUNE", "CHOOSE", "TEST")))  # fmt: skip
    if not any_n:
        L.append("No cell is PASSED or AVOID on either measure.")
    # books
    L.append("\n## Books (K = 10, PRODUCT order, Rs 1 lakh, cash rule of US-100)\n")
    L.append("| Book | Eligible trades (evenings) | Period | Held | Capped | Refused (cash) | "
             "% a trade (t) | EXCESS % (t) | End (lakh) | Growth % a year | Worst fall % | "
             "Places used | B1 / B2 / B3 end |")  # fmt: skip
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for bn in BOOKS:
        b = books[bn]
        for p in PERIODS:
            f = b[p]
            L.append(f"| {bn} | {b['eligible']} ({b['evenings']}) | {p} | {f['held']} | "
                     f"{f['capped']} | {f['refused']} | {fs(f['pct'])} ({fs(f['t'], 1)}) | "
                     f"{fs(f['excess_basket'])} ({fs(f['t_excess'], 1)}) | {pb.lakh(f['end'])} | "
                     f"{fs(f['growth'])} | {fs(f['maxdd'], 1)} | {f['places']:.1f} | "
                     f"{pb.lakh(f['B1_end'])} / {pb.lakh(f['B2_end'])} / "
                     f"{pb.lakh(f['B3_end'])} |")  # fmt: skip
    L.append("\n| Book | Period | less B1 % a year [95%] | less B2 | less B3 | seeds 0 to 39 "
             "excluding zero B1 / B2 / B3 | blocks of 3 months: less B1 / B2 / B3 | seeds, blocks "
             "of 3, excluding zero B1 / B2 / B3 |")  # fmt: skip
    L.append("|---|---|---|---|---|---|---|---|")
    for bn in BOOKS:
        for p in PERIODS:
            f = books[bn][p]
            v, v3 = f["vs"], f["vs_block3"]

            def iv(d):
                return f"{fs(d['pa'])} [{fs(d['pa_lo'])} to {fs(d['pa_hi'])}]"

            s1 = " / ".join(f"{f['seeds']['block1'][x] * 100:.0f}%" for x in ("B1", "B2", "B3"))
            s3 = " / ".join(f"{f['seeds']['block3'][x] * 100:.0f}%" for x in ("B1", "B2", "B3"))
            L.append(f"| {bn} | {p} | {iv(v['B1'])} | {iv(v['B2'])} | {iv(v['B3'])} | {s1} | "
                     + " / ".join(f"[{fs(v3[x]['pa_lo'])} to {fs(v3[x]['pa_hi'])}]"
                                  for x in ("B1", "B2", "B3")) + f" | {s3} |")  # fmt: skip
    ref = books[BOOKS[0]]
    L.append("\nBooks against US-100's book, growth % a year TUNE / CHOOSE / TEST (the fixed "
             "reading of answer 6: not behind in any period):")  # fmt: skip
    for bn in BOOKS[1:]:
        g = [books[bn][p]["growth"] - ref[p]["growth"] for p in ("TUNE", "CHOOSE", "TEST")]
        L.append(f"- {bn}: " + " / ".join(fs(x) for x in g)
                 + f"; not behind in all three: {all(x >= 0 for x in g)}")  # fmt: skip
    # power and how often
    sess = ss.index
    span_m = (sess[-1] - sess[0]).days / 30.4375
    L.append("\n## Power (week-clustered: months x (2 / t)^2) and how often evenings come\n")
    L.append("| Trades | Period | n | Trades a month | Evenings a month | EXCESS % (t) | Months "
             "for t = 2, EXCESS | RAW % (t) | Months for t = 2, RAW |")  # fmt: skip
    L.append("|---|---|---|---|---|---|---|---|---|")
    groups = [(k, None) for k in real]
    for key, _ in groups:
        cond, v = key.split("|")
        msk = np.ones(len(tr), bool) if cond == "ALL" else (tr[f"lab_{cond}"] == v).to_numpy()
        for row in power_rows(tr, msk, span_m):
            e, r = row["excess_basket"], row["net_pct"]
            L.append(f"| {key} | {row['period']} | {row['n']} | {row['rate']:.1f} | "
                     f"{row['evenings_m']:.2f} | {fs(e['mean'])} ({fs(e['t'], 1)}) | "
                     f"{e['months']:,.0f} | {fs(r['mean'])} ({fs(r['t'], 1)}) | "
                     f"{r['months']:,.0f} |")  # fmt: skip
    bst = pd.read_pickle(OUT / "book_states.pkl")
    for bn in BOOKS:
        s = bst[bn].to_numpy() - 1
        msk = (s == pb.OPEN) | (s == pb.DONE)
        for row in power_rows(tr, msk, span_m):
            e, r = row["excess_basket"], row["net_pct"]
            L.append(f"| {bn}, held | {row['period']} | {row['n']} | {row['rate']:.1f} | "
                     f"{row['evenings_m']:.2f} | {fs(e['mean'])} ({fs(e['t'], 1)}) | "
                     f"{e['months']:,.0f} | {fs(r['mean'])} ({fs(r['t'], 1)}) | "
                     f"{r['months']:,.0f} |")  # fmt: skip
    mon = pd.DatetimeIndex(tr.signal_date).to_period("M")
    all_m = pd.period_range(sess[0].to_period("M"), sess[-1].to_period("M"), freq="M")
    for lab, msk in (("evenings with 10 or more signals", tr.breadth >= 10),
                     ("evenings with 3 or more signals", tr.breadth >= 3),
                     ("evenings with NIFTY 2% or more below", tr.depth_nifty >= 2)):  # fmt: skip
        got = set(mon[msk.to_numpy()])
        in_test = msk.to_numpy() & (tr.period == "TEST").to_numpy()
        got_test = sorted({str(p) for p in mon[in_test]})  # months of the TEST evenings only
        test_m = [p for p in all_m if p >= pd.Period("2025-11", "M")]
        n_test = tr[in_test].signal_date.nunique()
        L.append(f"- {lab}: in {len(got)} of {len(all_m)} calendar months; TEST: {n_test} "
                 f"evenings, in {len(got_test)} of the {len(test_m)} calendar months it "
                 f"touches ({', '.join(got_test)})")  # fmt: skip
    txt = "\n".join(L)
    (OUT / "report.md").write_text(txt + "\n")
    print(txt)


STAGES = {"prep": stage_prep, "cells": stage_cells, "books": stage_books,
          "report": stage_report}  # fmt: skip


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=list(STAGES), required=True)
    ap.add_argument("--out", type=Path, default=OUT, help="the output folder (default: ours)")
    a = ap.parse_args()
    OUT = a.out
    pb.OUT = a.out  # US-100's prep and readers use the same folder
    STAGES[a.stage]()


if __name__ == "__main__":
    main()
