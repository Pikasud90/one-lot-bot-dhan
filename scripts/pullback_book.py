"""US-100: the capped pullback book against the index (study; fixed before any result).

The study's 8,085 pullback trades (`trades_P1.pkl`, unchanged: entry, exit, shares, costs) are
held in a BOOK of Rs 1,00,000 with at most K places. A signal on the evening of day t asks
for a place; places are counted as the product counts them (`onelot.delivery.wide`): a trade
is active on an evening from its signal evening until the morning its exit fills, so a trade
that exits at an evening's open no longer holds a place that evening. Same-evening signals
take free places in a fixed ORDER. A trade the book cannot pay for in cash at its fill is
refused and counted (its place was taken on the evening and is lost). Idle cash earns
nothing; costs are booked at the sale (the trade's net rupees), the cash paid at the fill is
the shares at the open with slippage. Marked to market on the stocks' daily closes.

Run from the worktree (inputs by absolute path, read-only; outputs to OUT):

    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage prep
    PYTHONPATH=src nice -n 10 .venv/bin/python scripts/pullback_book_product.py ... --out \
        OUT/product_trades.csv          (the product's own tiers; about six minutes)
    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage cross
    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage books
    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage random
    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage sens   (added after the reviews)
    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage report

Only pandas, numpy and sqlite3. Never an order: this is a backtest of a rule, not advice.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

MAIN = Path("/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN")
SWING = MAIN / "data" / "research" / "swing_2026-11-24"
NIFTY_CSV = MAIN / "data" / "research" / "alpha_2026-11-12" / "daily_universe" / "NIFTY.csv"
STORE = MAIN / "data" / "onelot.db"
OUT = MAIN / "data" / "research" / "pullback_book_2026-11-25"
LAST = pd.Timestamp("2026-11-07")
TUNE_END, CHOOSE_END = pd.Timestamp("2024-11-07"), pd.Timestamp("2025-11-07")
PERIODS = ("TUNE", "CHOOSE", "TEST", "ALL")
UNIT = 10_000.0
MONEY = 100_000.0
ORDERS = ("PRODUCT", "DEEP", "SHALLOW", "LIQUID", "CALM")
CAPS = (5, 10, 20, None)
RANDOM_RUNS = 500
RANDOM_SEED = 1000
BOOT_DRAWS = 2000
BOOT_SEED = 20261125
LOOKBACK = 20  # the depth's closing high, the turnover median and the spread of returns
# the study's corporate actions (scripts/swing_universe.py, CORP_ACTIONS), unchanged
CORP_ACTIONS = {"ADANIENT": ["2015-06-03"], "JSL": ["2015-11-19"], "TATACHEM": ["2020-03-04"],
                "BRITANNIA": ["2010-03-08"], "TMPV": ["2011-01-03"]}  # fmt: skip

# trade states inside the book
WAIT, PENDING, OPEN, DONE, CAPPED, REFUSED = 0, 1, 2, 3, 4, 5


# ───────────────────────────── the book (pure; tested on hand-made trades) ─────────────────────


def run_book(
    sig: np.ndarray,
    ent: np.ndarray,
    ext: np.ndarray,
    amount: np.ndarray,
    net: np.ndarray,
    rank: np.ndarray,
    k: int,
    money: float,
    n_sess: int,
    use_cash: bool = True,
    older_first: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Which trades the book holds. `sig`, `ent`, `ext` are session numbers (signal evening,
    entry fill at the open, exit fill at the open; `ext` >= n_sess: still open at the end).
    `rank`: lower takes a free place first among one evening's signals. Returns the state of
    each trade (OPEN or DONE = held, CAPPED = no free place, REFUSED = no cash at the fill)
    and the places in use on each evening after the signals were placed. The entries of one
    morning are paid older signal first, then by rank (`older_first`); False pays them by rank
    alone (a sensitivity added after the first results; it matters only where cash binds)."""
    n = len(sig)
    state = np.full(n, WAIT, dtype=np.int8)
    by_sig: dict[int, list[int]] = {}
    by_ent: dict[int, list[int]] = {}
    by_ext: dict[int, list[int]] = {}
    for i in range(n):
        by_sig.setdefault(int(sig[i]), []).append(i)
        by_ent.setdefault(int(ent[i]), []).append(i)
        by_ext.setdefault(int(ext[i]), []).append(i)
    cash, active = float(money), 0
    used = np.zeros(n_sess, dtype=np.int32)
    for s in range(n_sess):
        for i in by_ext.get(s, ()):  # exits fill at the open: the place is free this evening
            if state[i] == OPEN:
                state[i] = DONE
                cash += amount[i] + net[i]
                active -= 1
        fill_key = (lambda j: (sig[j], rank[j])) if older_first else (lambda j: rank[j])
        for i in sorted(by_ent.get(s, ()), key=fill_key):
            if state[i] != PENDING:
                continue
            if use_cash and cash < amount[i]:
                state[i] = REFUSED
                active -= 1
            else:
                state[i] = OPEN
                cash -= amount[i]
        cands = by_sig.get(s)
        if cands:
            free = k - active
            for i in sorted(cands, key=lambda j: rank[j]):
                if free > 0:
                    state[i] = PENDING
                    active += 1
                    free -= 1
                else:
                    state[i] = CAPPED
        used[s] = active
    return state, used


def held(state: np.ndarray) -> np.ndarray:
    return (state == OPEN) | (state == DONE)


# ───────────────────────────── inputs (read-only) ─────────────────────────────


def load_frames() -> dict[str, pd.DataFrame]:
    """The study's daily bars with its repairs (scripts/swing_universe.py, load_raw)."""
    con = sqlite3.connect(f"file:{SWING / 'daily.sqlite'}?mode=ro", uri=True)
    d = pd.read_sql("SELECT * FROM daily ORDER BY symbol, date", con, parse_dates=["date"])
    con.close()
    out = {}
    for sym, g in d.groupby("symbol"):
        f = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        f = f[(f.open > 0) & (f.high > 0) & (f.low > 0) & (f.close > 0)]
        for day in CORP_ACTIONS.get(str(sym), ()):
            i = f.index.get_loc(pd.Timestamp(day))
            k = f.close.iloc[i] / f.close.iloc[i - 1]
            f.iloc[:i, :4] = f.iloc[:i, :4] * k
            f.iloc[:i, 4] = f.iloc[:i, 4] / k
        out[str(sym)] = f[f.index <= LAST]
    return out


def nifty() -> pd.Series:
    """NIFTY's daily closes: the CSV before the store's first bar, the store (bars, NIFTY, 1d,
    read-only) from it, as scripts/pullback_wide_replay.py joins them."""
    con = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ts, close FROM bars WHERE series_key = 'NIFTY' AND resolution = '1d' ORDER BY ts"
    ).fetchall()
    con.close()
    ts = pd.to_datetime([r[0] for r in rows], utc=True).tz_convert("Asia/Kolkata")
    st = pd.Series([float(r[1]) for r in rows], index=ts.tz_localize(None).normalize())
    st = st[~st.index.duplicated(keep="last")]
    y = pd.read_csv(NIFTY_CSV, index_col=0, parse_dates=True)["close"].dropna()
    y.index = pd.DatetimeIndex(y.index).normalize()
    both = pd.concat([y[y.index < st.index[0]], st]).sort_index()
    return both[both.index <= LAST]


def tweek(x: np.ndarray, dates: pd.Series) -> float:
    """t of the mean with the trades of one ISO entry week summed into one draw (the study's)."""
    if len(x) < 2:
        return float("nan")
    iso = pd.DatetimeIndex(dates).isocalendar()
    w = pd.Series(np.asarray(x, float)).groupby([iso.year.values, iso.week.values]).sum()
    if len(w) < 2 or w.std(ddof=1) == 0:
        return float("nan")
    return float(w.mean() / (w.std(ddof=1) / np.sqrt(len(w))))


def period_of(d: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    d = pd.DatetimeIndex(d)
    return np.select([d <= TUNE_END, d <= CHOOSE_END], ["TUNE", "CHOOSE"], "TEST")


# ───────────────────────────── stage prep ─────────────────────────────


def stage_prep() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tr = pd.read_pickle(SWING / "trades_P1.pkl").reset_index(drop=True)
    fr = load_frames()
    start = tr.signal_date.min()
    sess = sorted({d for f in fr.values() for d in f.index if start <= d <= LAST})
    sess = pd.DatetimeIndex(sess)
    pos = {d: i for i, d in enumerate(sess)}
    stocks = sorted(fr)
    close = pd.DataFrame({s: fr[s].close for s in stocks}).reindex(sess)
    has_bar = close.notna().to_numpy()
    close_ff = close.ffill()
    # the equal-weight basket's return each session: stocks with a bar that day and before
    prev = close.ffill().shift(1)
    rb = (close / prev - 1).where(close.notna() & prev.notna())
    basket = rb.mean(axis=1).fillna(0.0).to_numpy()
    nf = nifty().reindex(sess).ffill()
    if nf.isna().any():
        raise SystemExit("NIFTY is missing on the first sessions")
    # what is known at each signal's close: depth, turnover, spread of returns
    depth, liquid, calm = [], [], []
    for r in tr.itertuples():
        f = fr[r.stock]
        i = f.index.get_loc(r.signal_date)
        c = f.close.to_numpy()
        hi = c[max(0, i - LOOKBACK + 1) : i + 1].max()
        depth.append((1 - c[i] / hi) * 100)
        lo = max(0, i - LOOKBACK + 1)
        liquid.append(float(np.median(c[lo : i + 1] * f.volume.to_numpy()[lo : i + 1])))
        ret = c[max(1, i - LOOKBACK + 1) : i + 1] / c[max(0, i - LOOKBACK) : i] - 1
        calm.append(float(np.std(ret, ddof=1)))
    tr["depth"], tr["liquid"], tr["calm"] = depth, liquid, calm
    tr["sig_i"] = [pos[d] for d in tr.signal_date]
    tr["ent_i"] = [pos[d] for d in tr.entry_date]
    tr["ext_i"] = [
        len(sess) if o else pos[d] for d, o in zip(tr.exit_date, tr.open_at_end, strict=True)
    ]
    tr["period"] = period_of(tr.entry_date)
    # each trade's mark to market (P: rupees gained so far) and exposure (X: shares x close)
    n, m = len(tr), len(sess)
    P = np.zeros((n, m), dtype=np.float32)
    X = np.zeros((n, m), dtype=np.float32)
    cols = {s: j for j, s in enumerate(stocks)}
    cf = close_ff.to_numpy()
    for i, r in enumerate(tr.itertuples()):
        e, x = r.ent_i, min(r.ext_i, m)
        mv = r.qty * cf[e:x, cols[r.stock]]
        X[i, e:x] = mv
        P[i, e:x] = mv - r.amount
        if r.ext_i < m:
            P[i, x:] = r.net_inr
    np.save(OUT / "P.npy", P)
    np.save(OUT / "X.npy", X)
    tr.to_pickle(OUT / "trades.pkl")
    pd.DataFrame({"nifty": nf.to_numpy(), "basket": basket, "stocks_with_bar": has_bar.sum(1)},
                 index=sess).to_pickle(OUT / "sessions.pkl")  # fmt: skip
    print(f"{n} trades, {m} sessions {sess[0].date()} to {sess[-1].date()}, {len(stocks)} stocks")


def load_prep() -> tuple[pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray]:
    tr = pd.read_pickle(OUT / "trades.pkl")
    ss = pd.read_pickle(OUT / "sessions.pkl")
    return tr, ss, np.load(OUT / "P.npy", mmap_mode="r"), np.load(OUT / "X.npy", mmap_mode="r")


def ranks(tr: pd.DataFrame) -> dict[str, np.ndarray]:
    """Lower first. The tie-break of every order is the stock key, as in the product."""
    desk = (tr.group != "desk18").to_numpy()
    key = tr.stock.to_numpy()
    out = {}
    out["PRODUCT"] = np.lexsort((key, -tr.depth.to_numpy(), desk))
    out["DEEP"] = np.lexsort((key, -tr.depth.to_numpy()))
    out["SHALLOW"] = np.lexsort((key, tr.depth.to_numpy()))
    out["LIQUID"] = np.lexsort((key, -tr.liquid.to_numpy()))
    out["CALM"] = np.lexsort((key, tr.calm.to_numpy()))
    return {k: np.argsort(v) for k, v in out.items()}  # position in the order = the rank


def book(tr: pd.DataFrame, rank: np.ndarray, k: int, money: float, n_sess: int, cash: bool = True,
         older_first: bool = True):  # fmt: skip
    return run_book(
        tr.sig_i.to_numpy(), tr.ent_i.to_numpy(), tr.ext_i.to_numpy(), tr.amount.to_numpy(),
        tr.net_inr.to_numpy(), rank, k, money, n_sess, cash, older_first,
    )  # fmt: skip


# ───────────────────────────── stage cross ─────────────────────────────


def stage_cross() -> None:
    tr, ss, _, _ = load_prep()
    sess = ss.index
    m = len(sess)
    pr = pd.read_csv(OUT / "product_trades.csv", parse_dates=["signal_date", "entry_session",
                                                               "exit_session"])  # fmt: skip
    L = [f"product: {len(pr)} trades, {int((pr.tier == 'cap').sum())} within the cap; "
         f"late {int(pr.late.sum())}"]  # fmt: skip

    # (a) my counting on the PRODUCT's own trades and depths must give its cap tier exactly
    def at(d: pd.Series) -> np.ndarray:
        v = np.searchsorted(sess.values, pd.DatetimeIndex(d).values)
        return np.where(pd.isna(d), m, v)

    sig = at(pr.signal_date)
    ent = np.where(pr.entry_session.isna(), m, at(pr.entry_session))
    ext = np.where(pr.exit_session.isna(), m, at(pr.exit_session))
    rk = np.argsort(np.lexsort((pr.stock.to_numpy(), -pr.depth_pct.to_numpy(),
                                (pr.group != "desk").to_numpy())))  # fmt: skip
    st, _ = run_book(sig, ent, ext, np.zeros(len(pr)), np.zeros(len(pr)), rk, 10, 0.0, m, False)
    mine = st != CAPPED
    prod = (pr.tier == "cap").to_numpy()
    L.append(f"(a) my place counting on the product's own trades: {int(mine.sum())} held; "
             f"same as the product's tier: {int((mine == prod).sum())} of {len(pr)}")  # fmt: skip
    for r in pr[mine != prod].head(20).itertuples():
        L.append(f"    differs: {r.stock} {r.signal_date.date()} product {r.tier}")
    # (b) the study's trades, PRODUCT order, K = 10, places only (no cash), against the product
    st2, _ = book(tr, ranks(tr)["PRODUCT"], 10, MONEY, m, cash=False)
    tr["mine"] = st2 != CAPPED
    j = tr.merge(pr[["stock", "signal_date", "tier", "depth_pct", "entry_session",
                     "exit_session"]], on=["stock", "signal_date"], how="outer",
                 indicator=True)  # fmt: skip
    both = j[j._merge == "both"]
    L.append(f"(b) the study's trades, K = 10, PRODUCT order, places only: {int(tr.mine.sum())} "
             f"held; trades in both lists {len(both)}, only the study {int((j._merge == 'left_only').sum())}, "
             f"only the product {int((j._merge == 'right_only').sum())}")  # fmt: skip
    same = (both.mine.astype(bool) == (both.tier == "cap")).sum()
    L.append(f"    same place decision on the trades in both lists: {same} of {len(both)}")
    for r in j[j._merge != "both"].itertuples():
        L.append(
            f"    only in {'the study' if r._merge == 'left_only' else 'the product'}: "
            f"{r.stock} {pd.Timestamp(r.signal_date).date()} tier {r.tier}"
        )
    diff = both[both.mine.astype(bool) != (both.tier == "cap")]
    for r in diff.head(40).itertuples():
        L.append(
            f"    differs: {r.stock} {r.signal_date.date()} mine "
            f"{'held' if r.mine else 'capped'}, product {r.tier}; depth {r.depth:.4f} / "
            f"{r.depth_pct:.4f}; exit {pd.Timestamp(r.exit_date).date()} / "
            f"{pd.Timestamp(r.exit_session).date() if pd.notna(r.exit_session) else 'open'}"
        )
    dd = (both.depth - both.depth_pct).abs()
    L.append(f"    depth: largest difference study against product {dd.max():.6f} points")
    ex = both[(both.exit_session.notna()) & (pd.to_datetime(both.exit_date) != both.exit_session)]
    L.append(f"    exit dates that differ between the lists: {len(ex)}")
    for r in ex.itertuples():
        L.append(
            f"      {r.stock} {r.signal_date.date()}: study {pd.Timestamp(r.exit_date).date()}"
            f", product {r.exit_session.date()}"
        )
    st3, _ = book(tr, ranks(tr)["PRODUCT"], 10, MONEY, m, cash=True)
    L.append(
        f"(c) with the cash rule (Rs 1 lakh): held {int(held(st3).sum())}, refused for cash "
        f"{int((st3 == REFUSED).sum())}, capped {int((st3 == CAPPED).sum())}"
    )
    txt = "\n".join(L)
    (OUT / "cross.txt").write_text(txt + "\n")
    print(txt)


# ───────────────────────────── the book's figures ─────────────────────────────


def bounds(sess: pd.DatetimeIndex, p: str) -> tuple[int, int]:
    """(a, b): the period runs from the close of session a to the close of session b."""
    per = period_of(sess)
    if p == "ALL":
        return 0, len(sess) - 1
    idx = np.flatnonzero(per == p)
    return max(0, idx[0] - 1), idx[-1]


def legs(V: np.ndarray, Xt: np.ndarray, ss: pd.DataFrame, a: int, b: int) -> dict[str, np.ndarray]:
    """Values from session a to b of the book and of B1, B2, B3, all starting at V[a]."""
    nf = ss.nifty.to_numpy()
    rn = np.r_[0.0, nf[1:] / nf[:-1] - 1]
    rb = ss.basket.to_numpy()
    v0 = V[a]
    out = {"BOOK": np.asarray(V[a : b + 1], float), "B1": v0 * nf[a : b + 1] / nf[a]}
    for name, r in (("B2", rn), ("B3", rb)):
        gain = Xt[a:b] * r[a + 1 : b + 1]  # rupees in the market at the close before
        out[name] = v0 + np.r_[0.0, np.cumsum(gain)]
    return out


def growth(v: np.ndarray, years: float) -> float:
    return float((v[-1] / v[0]) ** (1 / years) - 1) * 100 if v[-1] > 0 else float("nan")


def boot(
    lg: dict[str, np.ndarray],
    months: np.ndarray,
    years: float,
    seed: int = BOOT_SEED,
    draws: int = BOOT_DRAWS,
    block: int = 1,
) -> dict[str, dict]:
    """Book less B1, B2, B3 with a 95% interval: calendar months drawn with replacement, the
    book and its benchmark TOGETHER (the same months), 2,000 draws, a fixed seed. `block` > 1
    draws runs of that many neighbouring months (a sensitivity added after the first results;
    the contract's interval is block 1)."""
    um, inv = np.unique(months, return_inverse=True)
    M = len(um)
    per_year = len(months) / years
    cnt = np.bincount(inv, minlength=M)
    rng = np.random.default_rng(seed)
    nb = -(-M // block)
    starts = rng.integers(0, M - block + 1, size=(draws, nb))
    draw = (starts[:, :, None] + np.arange(block)).reshape(draws, nb * block)[:, :M]
    yrs = cnt[draw].sum(1) / per_year
    v0 = lg["BOOK"][0]

    def mlog(v: np.ndarray) -> np.ndarray:
        r = np.log(np.maximum(v[1:] / v[:-1], 1e-12))
        return np.bincount(inv, weights=r, minlength=M)

    lb = mlog(lg["BOOK"])[draw].sum(1)
    out = {}
    for name in ("B1", "B2", "B3"):
        lx = mlog(lg[name])[draw].sum(1)
        rup = v0 * (np.exp(lb) - np.exp(lx))
        ann = (np.exp(lb / yrs) - np.exp(lx / yrs)) * 100
        real_rup = lg["BOOK"][-1] - lg[name][-1]
        real_ann = growth(lg["BOOK"], years) - growth(lg[name], years)
        out[name] = {
            "rupees": float(real_rup), "rupees_lo": float(np.percentile(rup, 2.5)),
            "rupees_hi": float(np.percentile(rup, 97.5)), "pa": float(real_ann),
            "pa_lo": float(np.percentile(ann, 2.5)), "pa_hi": float(np.percentile(ann, 97.5)),
            "pa_share_ge0": float((ann >= 0).mean()),
        }  # fmt: skip
    return out


def figures(tr, ss, P, X, state, used, k, money, with_boot=True) -> dict:
    sess = ss.index
    h = held(state)
    V = money + np.asarray(P[h].sum(0, dtype=np.float64))
    Xt = np.asarray(X[h].sum(0, dtype=np.float64))
    out = {}
    for p in PERIODS:
        a, b = bounds(sess, p)
        sel = slice(a + 1 if p != "ALL" else 0, b + 1)
        q = tr[h & ((tr.period == p) | (p == "ALL"))]
        asked = tr[(state != WAIT) & ((tr.period == p) | (p == "ALL"))]
        years = (sess[b] - sess[a]).days / 365.25
        lg = legs(V, Xt, ss, a, b)
        v = lg["BOOK"]
        yr = pd.Series(v, index=sess[a : b + 1]).groupby(sess[a : b + 1].year).last()
        first = pd.Series([v[0]], index=[sess[a].year - 1])
        yret = pd.concat([first, yr]).pct_change().dropna() * 100
        u = used[sel]
        f = {
            "held": int(len(q)), "capped": int((asked.index.isin(tr.index[state == CAPPED])).sum()),
            "refused": int((asked.index.isin(tr.index[state == REFUSED])).sum()),
            "win": float((q.net_pct > 0).mean() * 100) if len(q) else float("nan"),
            "pct": float(q.net_pct.mean()) if len(q) else float("nan"),
            "t": tweek(q.net_pct.to_numpy(), q.entry_date) if len(q) else float("nan"),
            "start": float(v[0]), "end": float(v[-1]), "growth": growth(v, years),
            "maxdd": float((v / np.maximum.accumulate(v) - 1).min() * 100),
            "worst_year": float(yret.min()), "worst_year_at": int(yret.idxmin()),
            "places": float(u.mean()), "full": float((u >= k).mean() * 100),
            "B1_end": float(lg["B1"][-1]), "B2_end": float(lg["B2"][-1]),
            "B3_end": float(lg["B3"][-1]), "years": years,
            "B1_g": growth(lg["B1"], years), "B2_g": growth(lg["B2"], years),
            "B3_g": growth(lg["B3"], years),
            "exposure": float(np.mean(Xt[a : b + 1] / v) * 100),
        }  # fmt: skip
        if with_boot:
            months = sess[a + 1 : b + 1].to_period("M").astype(str).to_numpy()
            f["vs"] = boot(lg, months, years)
        out[p] = f
    return out


def stage_books() -> None:
    tr, ss, P, X = load_prep()
    m = len(ss)
    rk = ranks(tr)
    res: dict = {"order": {}, "cap": {}}
    states = {}
    for o in ORDERS:
        st, used = book(tr, rk[o], 10, MONEY, m)
        res["order"][o] = figures(tr, ss, P, X, st, used, 10, MONEY)
        states[f"order_{o}"] = st
        print(o, res["order"][o]["ALL"]["held"], round(res["order"][o]["ALL"]["growth"], 2))
    # no cap: the money is the largest number ever open x Rs 10,000 (places counted, no cash)
    _, used0 = book(tr, rk["PRODUCT"], 10**6, 0.0, m, cash=False)
    most = int(used0.max())
    res["most_open"] = most
    for k in CAPS:
        kk = most if k is None else k
        money = kk * UNIT
        st, used = book(tr, rk["PRODUCT"], kk if k else 10**6, money, m)
        res["cap"][str(k)] = figures(tr, ss, P, X, st, used, kk, money)
        res["cap"][str(k)]["money"] = money
        states[f"cap_{k}"] = st
        print("cap", k, money, res["cap"][str(k)]["ALL"]["held"])
    pd.DataFrame(states).to_pickle(OUT / "states.pkl")
    (OUT / "books.json").write_text(json.dumps(res, indent=1, default=float))


def stage_random() -> None:
    tr, ss, P, X = load_prep()
    m = len(ss)
    sess = ss.index
    rows = []
    for r in range(RANDOM_RUNS):
        rng = np.random.default_rng(RANDOM_SEED + r)
        rank = rng.permutation(len(tr))
        st, _ = book(tr, rank, 10, MONEY, m)
        h = held(st)
        V = MONEY + np.asarray(P[h].sum(0, dtype=np.float64))
        row = {"run": r}
        for p in PERIODS:
            a, b = bounds(sess, p)
            q = tr[h & ((tr.period == p) | (p == "ALL"))]
            row[f"pct_{p}"] = float(q.net_pct.mean())
            row[f"growth_{p}"] = growth(V[a : b + 1], (sess[b] - sess[a]).days / 365.25)
        row["held"] = int(h.sum())
        rows.append(row)
        if r % 50 == 0:
            print(r, row["held"], round(row["pct_ALL"], 3), round(row["growth_ALL"], 2))
    pd.DataFrame(rows).to_pickle(OUT / "random.pkl")


# ──────────────── stage sens (added after the first results, from the two reviews) ────────────


def stage_sens() -> None:
    """Sensitivities the contract did not fix, named as added after the first results: the
    bootstrap of ALL book less B3 (PRODUCT, K = 10) under other seeds, more draws and longer
    blocks; and the morning order of fills (older signal first, or rank alone) for each cap."""
    tr, ss, P, X = load_prep()
    sess = ss.index
    m = len(sess)
    rk = ranks(tr)["PRODUCT"]
    st, _ = book(tr, rk, 10, MONEY, m)
    h = held(st)
    V = MONEY + np.asarray(P[h].sum(0, dtype=np.float64))
    Xt = np.asarray(X[h].sum(0, dtype=np.float64))
    a, b = bounds(sess, "ALL")
    years = (sess[b] - sess[a]).days / 365.25
    lg = legs(V, Xt, ss, a, b)
    months = sess[a + 1 : b + 1].to_period("M").astype(str).to_numpy()
    out: dict = {"seeds": [], "blocks": {}, "fill": {}}
    for seed in range(40):
        v = boot(lg, months, years, seed=seed)["B3"]
        out["seeds"].append({"seed": seed, "lo": v["pa_lo"], "hi": v["pa_hi"],
                             "rup_hi": v["rupees_hi"]})  # fmt: skip
    out["draws_100k"] = boot(lg, months, years, draws=100_000)["B3"]
    for blk in (1, 3, 6, 12):
        out["blocks"][str(blk)] = boot(lg, months, years, block=blk)["B3"]
    _, used0 = book(tr, rk, 10**6, 0.0, m, cash=False)
    most = int(used0.max())
    for k in CAPS:
        kk = most if k is None else k
        row = {}
        for older in (True, False):
            s2, u2 = book(tr, rk, kk if k else 10**6, kk * UNIT, m, older_first=older)
            f = figures(tr, ss, P, X, s2, u2, kk, kk * UNIT, with_boot=False)
            row["older" if older else "rank"] = {
                "held": int(held(s2).sum()), "refused": int((s2 == REFUSED).sum()),
                "end": f["ALL"]["end"], "growth": f["ALL"]["growth"],
                "less_b3": {p: f[p]["growth"] - f[p]["B3_g"] for p in PERIODS},
                "pct": {p: f[p]["pct"] for p in PERIODS},
                "state": s2,
            }  # fmt: skip
        row["differ"] = int((held(row["older"]["state"]) != held(row["rank"]["state"])).sum())
        for kind in ("older", "rank"):
            del row[kind]["state"]
        out["fill"][str(k)] = row
        print("fill", k, row["differ"], row["older"]["held"], row["rank"]["held"])
    (OUT / "sens.json").write_text(json.dumps(out, indent=1, default=float))
    his = np.array([s["hi"] for s in out["seeds"]])
    print(f"seeds: upper end {his.min():+.2f} to {his.max():+.2f}, below zero {(his < 0).sum()}")


# ───────────────────────────── stage report ─────────────────────────────


def fs(x: float, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    s = f"{x:+,.{d}f}"
    return s.replace("-", "−")


def lakh(x: float) -> str:
    return f"{x / 1e5:,.2f}".replace("-", "−")


def stage_report() -> None:  # noqa: C901 - a report, read top to bottom
    tr, ss, P, X = load_prep()
    res = json.loads((OUT / "books.json").read_text())
    rnd = pd.read_pickle(OUT / "random.pkl")
    states = pd.read_pickle(OUT / "states.pkl")
    L: list[str] = []

    def table(title: str, block: dict, names: list[str]) -> None:
        L.append(f"\n## {title}\n")
        L.append("| Variant | Period | Held | Capped | Refused (cash) | Win % | % a trade (t) | "
                 "End (lakh) | Growth % a year | Worst fall % | Worst year % | Places used | "
                 "All full % |")  # fmt: skip
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for n in names:
            for p in PERIODS:
                f = block[n][p]
                L.append(
                    f"| {n} | {p} | {f['held']} | {f['capped']} | {f['refused']} | "
                    f"{f['win']:.0f} | {fs(f['pct'])} ({fs(f['t'], 1)}) | {lakh(f['end'])} | "
                    f"{fs(f['growth'])} | {fs(f['maxdd'], 1)} | {fs(f['worst_year'], 1)} "
                    f"({f['worst_year_at']}) | {f['places']:.1f} | {f['full']:.0f} |"
                )
        L.append(f"\n### {title}: against the benchmarks (book less B, 95% interval)\n")
        L.append(
            "| Variant | Period | B1 end | B2 end | B3 end | less B1 Rs | less B2 Rs | "
            "less B3 Rs | less B1 % a year | less B2 % a year | less B3 % a year |"
        )
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for n in names:
            for p in PERIODS:
                f = block[n][p]
                v = f["vs"]
                rup = [f"{fs(v[b]['rupees'], 0)} [{fs(v[b]['rupees_lo'], 0)} to "
                       f"{fs(v[b]['rupees_hi'], 0)}]" for b in ("B1", "B2", "B3")]  # fmt: skip
                pa = [f"{fs(v[b]['pa'])} [{fs(v[b]['pa_lo'])} to {fs(v[b]['pa_hi'])}]"
                      for b in ("B1", "B2", "B3")]  # fmt: skip
                L.append(
                    f"| {n} | {p} | {lakh(f['B1_end'])} | {lakh(f['B2_end'])} | "
                    f"{lakh(f['B3_end'])} | " + " | ".join(rup + pa) + " |"
                )

    table("Orders at K = 10", res["order"], list(ORDERS))
    table("Caps with the PRODUCT order", res["cap"], [str(k) for k in CAPS])
    L.append(
        f"\nNo cap: the largest number ever open is {res['most_open']}, money "
        f"Rs {res['most_open'] * UNIT:,.0f}."
    )
    # RANDOM
    L.append("\n## RANDOM (500 seeded shuffles) and where each fixed order stands\n")
    L.append("| | " + " | ".join(f"% a trade {p}" for p in PERIODS) + " | "
             + " | ".join(f"growth {p}" for p in PERIODS) + " |")  # fmt: skip
    L.append("|---|" + "---|" * 8)
    q = {s: rnd[[f"pct_{p}" for p in PERIODS] + [f"growth_{p}" for p in PERIODS]].quantile(s)
         for s in (0.025, 0.5, 0.975)}  # fmt: skip
    for s, lab in ((0.025, "RANDOM 2.5th"), (0.5, "RANDOM median"), (0.975, "RANDOM 97.5th")):
        L.append(f"| {lab} | " + " | ".join(fs(v) for v in q[s].to_numpy()) + " |")
    better = {}
    for o in ORDERS:
        cells, pcts = [], {}
        for metric in ("pct", "growth"):
            for p in PERIODS:
                real = res["order"][o][p]["pct" if metric == "pct" else "growth"]
                col = rnd[f"{metric}_{p}"]
                pc = float(((col < real).mean() + 0.5 * (col == real).mean()) * 100)
                pcts[(metric, p)] = pc
                cells.append(f"{fs(real)} ({pc:.0f})")
        L.append(f"| {o} (percentile) | " + " | ".join(cells) + " |")
        better[o] = {m_: bool(pcts[(m_, "TUNE")] > 97.5 and pcts[(m_, "CHOOSE")] > 50
                              and pcts[(m_, "TEST")] > 50) for m_ in ("pct", "growth")}  # fmt: skip
    L.append(f"\nBetter than RANDOM by the fixed rule (% a trade, growth): {better}")
    # refused against taken, K = 10, PRODUCT order
    st = states["order_PRODUCT"].to_numpy()
    h, cp, rf = held(st), st == CAPPED, st == REFUSED
    L.append("\n## What the cap refused, K = 10, PRODUCT order\n")
    L.append("| Trades | Period | n | % a trade (t) | Win % |")
    L.append("|---|---|---|---|---|")
    for lab, msk in (("held", h), ("refused by the cap", cp), ("refused for cash", rf)):
        for p in PERIODS:
            g = tr[msk & ((tr.period == p) | (p == "ALL"))]
            if len(g):
                L.append(
                    f"| {lab} | {p} | {len(g)} | {fs(g.net_pct.mean())} "
                    f"({fs(tweek(g.net_pct.to_numpy(), g.entry_date), 1)}) | "
                    f"{(g.net_pct > 0).mean() * 100:.0f} |"
                )
    per_ev = tr.groupby("sig_i").size()
    tr["ev_n"] = tr.sig_i.map(per_ev)
    nf = ss.nifty.to_numpy()
    roll_hi = pd.Series(nf).rolling(20, min_periods=1).max().to_numpy()
    tr["nifty_off"] = (1 - nf[tr.sig_i] / roll_hi[tr.sig_i]) * 100
    full_ev = set(tr.sig_i[cp])
    tr["full_ev"] = tr.sig_i.isin(full_ev)
    L.append(
        "\nEvenings with a signal: "
        f"{tr.sig_i.nunique()}; of them with a signal refused by the cap: {len(full_ev)}."
    )
    ev = tr.groupby("sig_i").agg(n=("stock", "size"), off=("nifty_off", "first"),
                                 full=("full_ev", "first"))  # fmt: skip
    for lab, g in (("full", ev[ev.full]), ("not full", ev[~ev.full])):
        L.append(
            f"- {lab} evenings: {len(g)}, signals that evening median {g.n.median():.0f} "
            f"(mean {g.n.mean():.1f}), NIFTY below its 20-session closing high median "
            f"{g.off.median():.1f}%"
        )
    L.append(
        "\n| Signals that evening | Held n | Held % a trade | Refused by the cap n | "
        "Refused by the cap % a trade |"
    )
    L.append("|---|---|---|---|---|")
    for lo, hi in ((1, 2), (3, 9), (10, 10**6)):
        g = tr[(tr.ev_n >= lo) & (tr.ev_n <= hi)]
        gh, gc = g[h[g.index]], g[cp[g.index]]
        lab = f"{lo} to {hi}" if hi < 10**6 else f"{lo} or more"
        L.append(
            f"| {lab} | {len(gh)} | {fs(gh.net_pct.mean())} | {len(gc)} | "
            f"{fs(gc.net_pct.mean()) if len(gc) else 'n/a'} |"
        )
    # the same evenings (a reviewer's probe, added after the first results): does the cap drop
    # the better signals OF AN EVENING, or does it fill its places on other evenings?
    L.append("\n### Held and refused on the same evenings (added after the first results)\n")
    L.append("| Trades | Period | n | % a trade (t) |")
    L.append("|---|---|---|---|")
    fe = tr.full_ev.to_numpy()
    for lab, msk in (("held, full evenings", h & fe), ("held, other evenings", h & ~fe),
                     ("refused by the cap", cp)):  # fmt: skip
        for p in PERIODS:
            g = tr[msk & ((tr.period == p) | (p == "ALL"))]
            if len(g):
                L.append(f"| {lab} | {p} | {len(g)} | {fs(g.net_pct.mean())} "
                         f"({fs(tweek(g.net_pct.to_numpy(), g.entry_date), 1)}) |")  # fmt: skip
    pair = pd.DataFrame({"ev": tr.sig_i, "x": tr.net_pct, "h": h, "c": cp})
    pair = pair[pair.h | pair.c]
    gm = pair.groupby(["ev", "c"]).x.agg(["mean", "size"]).unstack("c").dropna()
    d = gm[("mean", True)] - gm[("mean", False)]
    w = gm[("size", True)]
    L.append(
        f"\nEvenings with both held and refused-by-the-cap trades: {len(d)}. Refused less held, "
        f"mean over evenings {fs(d.mean())} (t over evenings "
        f"{fs(d.mean() / (d.std(ddof=1) / np.sqrt(len(d))), 2)}); weighted by the count refused "
        f"{fs(float((d * w).sum() / w.sum()))}."
    )
    # costs on the held trades against the shortfall to B3 (added after the first results)
    hc = tr[h & ~tr.open_at_end.to_numpy()]
    gross = float((hc.qty * (hc.exit_raw - hc.entry_open)).sum())
    net = float(hc.net_inr.sum())
    gap = res["order"]["PRODUCT"]["ALL"]["vs"]["B3"]["rupees"]
    L.append(
        f"\nCosts (added after the first results): the {len(hc)} closed held trades gained "
        f"Rs {gross:,.0f} before costs and slippage, Rs {net:,.0f} after: costs "
        f"Rs {gross - net:,.0f} ({(gross - net) / hc.amount.sum() * 100:.3f}% of the amount). "
        f"Book less B3 over ALL: Rs {gap:,.0f}. B1, B2 and B3 carry no costs."
    )
    # back-adjusted prices and the cash rule: EICHERMOT before its split of 2020-08-24 (1 to 10)
    eg = tr[(tr.stock == "EICHERMOT") & (tr.entry_date < pd.Timestamp("2020-08-24"))]
    big = eg[eg.entry_open * 10 > UNIT]
    L.append(
        f"\nEICHERMOT before its 2020-08-24 split: {len(eg)} trades, adjusted open Rs "
        f"{eg.entry_open.min():,.0f} to {eg.entry_open.max():,.0f}; {len(big)} would cost over "
        f"Rs 10,000 a share at the price of the day; {int(h[big.index].sum())} of those are held."
    )
    # power for the paper test
    L.append("\n## Power for the paper test (t = 2)\n")
    L.append(
        "Single trades: trades = (2 x SD / mean)^2, a floor. Week-clustered (added after the first "
        "results): months x (2 / t)^2, with t the study's t of weekly sums over those months."
    )
    sess = ss.index
    span_m = (sess[-1] - sess[0]).days / 30.4375
    L.append(
        "\n| Trades | Period | Trades a month | Mean % | SD % | Trades for t = 2 | Months, single "
        "trades | Weekly-sum t | Months, week-clustered |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|")
    for lab, msk in (("held, K = 10", h), ("tier all", np.ones(len(tr), bool))):
        for p in ("ALL", "TEST"):
            g = tr[msk & ((tr.period == p) | (p == "ALL"))]
            months = span_m if p == "ALL" else 12.0
            rate = len(g) / months
            mu, sd = g.net_pct.mean(), g.net_pct.std(ddof=1)
            need = (2 * sd / mu) ** 2 if mu > 0 else float("inf")
            tw = tweek(g.net_pct.to_numpy(), g.entry_date)
            mcl = months * (2 / tw) ** 2 if tw > 0 else float("inf")
            L.append(
                f"| {lab} | {p} | {rate:.1f} | {fs(mu)} | {sd:.2f} | {need:,.0f} | "
                f"{need / rate:,.0f} | {fs(tw)} | {mcl:,.0f} |"
            )
    L.append(
        "\nExposure (rupees in stocks over book value, mean %): "
        + ", ".join(f"{o} {res['order'][o]['ALL']['exposure']:.0f}" for o in ORDERS)
    )
    sp = OUT / "sens.json"
    if sp.exists():
        se = json.loads(sp.read_text())
        L.append("\n## Sensitivities added after the first results (PRODUCT, K = 10, ALL)\n")
        his = np.array([s["hi"] for s in se["seeds"]])
        los = np.array([s["lo"] for s in se["seeds"]])
        L.append(
            f"Book less B3, % a year, seeds 0 to 39 (2,000 draws each): lower end {fs(los.min())} "
            f"to {fs(los.max())}, upper end {fs(his.min())} to {fs(his.max())} (median "
            f"{fs(float(np.median(his)))}); upper end below zero for {int((his < 0).sum())} of 40."
        )
        d = se["draws_100k"]
        L.append(
            f"100,000 draws, seed {BOOT_SEED}: [{fs(d['pa_lo'])} to {fs(d['pa_hi'])}], draws at "
            f"or above zero {d['pa_share_ge0'] * 100:.1f}%; rupees [{fs(d['rupees_lo'], 0)} to "
            f"{fs(d['rupees_hi'], 0)}]."
        )
        for blk, v in se["blocks"].items():
            L.append(
                f"- blocks of {blk} month(s): [{fs(v['pa_lo'])} to {fs(v['pa_hi'])}], at or above "
                f"zero {v['pa_share_ge0'] * 100:.1f}%"
            )
        L.append("\n| K | Morning fills | Held | Refused (cash) | End (lakh) | Growth % a year | "
                 "less B3 TUNE / CHOOSE / TEST | Held sets differ |")  # fmt: skip
        L.append("|---|---|---|---|---|---|---|---|")
        for k, row in se["fill"].items():
            for kind in ("older", "rank"):
                r = row[kind]
                lb = " / ".join(fs(r["less_b3"][p]) for p in ("TUNE", "CHOOSE", "TEST"))
                L.append(
                    f"| {k} | {'older signal first' if kind == 'older' else 'rank alone'} | "
                    f"{r['held']} | {r['refused']} | {lakh(r['end'])} | {fs(r['growth'])} | {lb} "
                    f"| {row['differ']} |"
                )
    txt = "\n".join(L)
    (OUT / "report.md").write_text(txt + "\n")
    print(txt)


STAGES = {"prep": stage_prep, "cross": stage_cross, "books": stage_books,
          "random": stage_random, "sens": stage_sens, "report": stage_report}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=list(STAGES), required=True)
    STAGES[ap.parse_args().stage]()


if __name__ == "__main__":
    main()
