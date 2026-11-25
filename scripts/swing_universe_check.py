"""US-093 independent check of the swing-universe study (2026-11-24). Research only.

Written from the product's definition of the pullback rule (src/onelot/delivery/rule.py read as a
specification, never imported) and the cost table of config/costs.yaml. It does not import
scripts/swing_universe.py; it reads the study's DATA (daily.sqlite, the trade pickles) as inputs
and recomputes every figure of the note with its own code.

Stages (run from the MAIN checkout, see .agile/stories/RESEARCH-RULES.md section 1):

    ref        my P1 against the 247 reference trades of tests/fixtures/pullback, on the fixture's
               own bars and on daily.sqlite's bars, and the study's P1 file through the same test
    new        P1 on the 161 new names: trades, costs, periods, t by entry week
    costs      the round trip at about Rs 10,000 from config/costs.yaml
    bench      excess over the same stock's hold and random entry dates (100 runs)
    lookahead  truncation test of my P1 signal, and a deliberately leaking signal as a control
    data       the corporate-action rescalings, other big moves, duplicates, bad prices, starts
    book       signals a month, trades open at once, worst month and total from the P1 file
    all        every stage in that order
"""

from __future__ import annotations

import functools
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path.cwd().resolve()
OUT = ROOT / "data" / "research" / "swing_2026-11-24"
DB = OUT / "daily.sqlite"
FIX = ROOT / "tests" / "fixtures" / "pullback"
YAHOO = ROOT / "data" / "research" / "alpha_2026-11-12" / "daily_universe"
LAST = pd.Timestamp("2026-11-07")
SLIP, DP, UNIT = 0.0003, 14.75, 10000.0
# the five rescalings the note lists: (stock, first day of the new level)
ACTIONS = {"ADANIENT": "2015-06-03", "JSL": "2015-11-19", "TATACHEM": "2020-03-04",
           "BRITANNIA": "2010-03-08", "TMPV": "2011-01-03"}  # fmt: skip
LINES: list[str] = []


def say(s: str = "") -> None:
    print(s, flush=True)
    LINES.append(s)


# ───────────────────────────── data ─────────────────────────────


def load_sqlite(rescale: bool = True, last: pd.Timestamp | None = LAST) -> dict[str, pd.DataFrame]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = pd.read_sql("SELECT * FROM daily ORDER BY symbol, date", con, parse_dates=["date"])
    out = {}
    for sym, g in d.groupby("symbol"):
        f = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        f = f[(f[["open", "high", "low", "close"]] > 0).all(axis=1)]
        if rescale and sym in ACTIONS:
            i = f.index.get_loc(pd.Timestamp(ACTIONS[str(sym)]))
            k = f.close.iloc[i] / f.close.iloc[i - 1]
            f.iloc[:i, :4] = f.iloc[:i, :4] * k
            f.iloc[:i, 4] = f.iloc[:i, 4] / k
        out[str(sym)] = f if last is None else f[f.index <= last]
    return out


def load_fixture(sym: str) -> pd.DataFrame:
    f = pd.read_csv(FIX / f"daily_{sym}.csv.gz", index_col=0, parse_dates=True)
    return f.astype(float)


def nifty_long() -> pd.Series:
    """NIFTY closes: the fixture (the store's bars from 2017-01-02) and the alpha study's cached
    Yahoo closes before that."""
    fx = load_fixture("NIFTY").close
    y = pd.read_csv(YAHOO / "NIFTY.csv", index_col=0, parse_dates=True)["close"].dropna()
    y.index = pd.DatetimeIndex(y.index).normalize()
    both = pd.concat([y[y.index < fx.index[0]], fx]).sort_index()
    return both[~both.index.duplicated(keep="last")]


def roll_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1 :] = np.lib.stride_tricks.sliding_window_view(x, n).mean(axis=1)
    return out


def roll_max(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1 :] = np.lib.stride_tricks.sliding_window_view(x, n).max(axis=1)
    return out


def nifty_ok(nc: pd.Series, dates: pd.DatetimeIndex) -> np.ndarray:
    """NIFTY's close of the date above NIFTY's mean of its last 200 closes (that date included);
    False where NIFTY has no bar or fewer than 200 bars."""
    ok = pd.Series(nc.to_numpy() > roll_mean(nc.to_numpy(), 200), index=nc.index)
    return ok.reindex(dates).fillna(False).to_numpy(bool)


# ───────────────────────────── my P1, from the product's definition ─────────────────────────────


def p1_signal(c: np.ndarray, ok: np.ndarray, depth: float = 0.08) -> np.ndarray:
    """Signal on the close of session t: the close is at least `depth` under the highest close of
    sessions t-19..t, it was not so the session before (its own window), the close is above its
    200-session mean and NIFTY is. Needs 201 bars (t >= 200)."""
    n = len(c)
    hi = roll_max(c, 20)
    with np.errstate(invalid="ignore"):
        deep = c <= (1.0 - depth) * hi
        above = c > roll_mean(c, 200)
    sig = np.zeros(n, bool)
    sig[1:] = deep[1:] & ~deep[:-1]
    sig &= above & ok
    sig[:200] = False
    return sig


def p1_trades(o: np.ndarray, c: np.ndarray, ok: np.ndarray, depth: float = 0.08) -> list[tuple]:
    """(signal, entry, exit, exit price, at_close). Entry: next session's open. Exit signal on the
    close of u >= entry: close above its 20-session mean, or 30 sessions (entry session = 1), or
    close at or under 85% of the fill; sold at the next open (last close if no next bar). The
    sale session's close is the first that may signal again."""
    n = len(c)
    sig, s20 = p1_signal(c, ok, depth), roll_mean(c, 20)
    out, t = [], 0
    while t < n - 1:
        if not sig[t]:
            t += 1
            continue
        e = t + 1
        fill = o[e] * (1 + SLIP)
        x, px, at_close = n - 1, c[n - 1], True
        for u in range(e, n):
            if (c[u] > s20[u]) or (u - e + 1 >= 30) or (c[u] <= fill * 0.85):
                if u + 1 < n:
                    x, px, at_close = u + 1, o[u + 1], False
                else:
                    x, px, at_close = u, c[u], True
                break
        out.append((t, e, x, float(px), at_close))
        t = x
    return out


# ───────────────────────────── costs ─────────────────────────────


@functools.cache
def cost_table() -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "costs.yaml").read_text())
    return cfg["products"]["equity_delivery"] if "products" in cfg else cfg["equity_delivery"]


def leg_cost(turnover: np.ndarray | float, sell: bool, tab: dict | None = None):
    """Charges of one delivery leg in rupees, from the cost table: brokerage the least of Rs 20
    and 0.03% of turnover, STT both ways, exchange, SEBI and IPFT on turnover, stamp duty on the
    buy, GST 18% on brokerage plus the three exchange fees."""
    tab = tab or cost_table()
    b, t = tab["brokerage"], tab["taxes"]
    tv = np.asarray(turnover, float)
    brok = np.minimum(np.minimum(b["per_order_inr"], b["pct_of_turnover"] * tv), b["cap_inr"])
    fees = (t["exchange_txn_pct"] + t["sebi_turnover_pct"] + t["ipft_pct"]) * tv
    tot = brok + fees + t["gst_pct"] * (brok + fees)
    tot = tot + (t["stt_sell_pct"] if sell else t["stt_buy_pct"]) * tv
    return tot if sell else tot + t["stamp_duty_pct"] * tv


def qty_of(close) -> np.ndarray:
    return np.maximum(1, np.floor(UNIT / np.asarray(close, float))).astype(int)


def net_pct(buy_open, sell_px, qty) -> np.ndarray:
    """% of the entry value: 0.03% slippage each side, both legs' charges, the depository charge
    of Rs 14.75 on the sale."""
    ef, xf, q = np.asarray(buy_open) * (1 + SLIP), np.asarray(sell_px) * (1 - SLIP), np.asarray(qty)
    net = (xf - ef) * q - leg_cost(ef * q, False) - leg_cost(xf * q, True) - DP
    return net / (ef * q) * 100.0


# ───────────────────────────── statistics ─────────────────────────────


def period_of(d: pd.Series) -> np.ndarray:
    return np.select([d <= pd.Timestamp("2024-11-07"), d <= pd.Timestamp("2025-11-07")],
                     ["TUNE", "CHOOSE"], "TEST")  # fmt: skip


def week_groups(dates: pd.Series) -> list:
    iso = pd.DatetimeIndex(dates).isocalendar()
    return [iso.year.to_numpy(), iso.week.to_numpy()]


def t_week_sum(x: np.ndarray, dates: pd.Series) -> float:
    """The study's convention: the trades of one entry week are summed into one draw."""
    w = pd.Series(np.asarray(x, float)).groupby(week_groups(dates)).sum()
    return float(w.mean() / (w.std(ddof=1) / np.sqrt(len(w)))) if len(w) > 1 else np.nan


def t_cluster(x: np.ndarray, dates: pd.Series) -> float:
    """Cluster-robust t of the mean per trade, one entry week = one cluster."""
    x = np.asarray(x, float)
    m = x.mean()
    r = pd.Series(x - m).groupby(week_groups(dates)).sum()
    g = len(r)
    var = (r**2).sum() * g / (g - 1) / len(x) ** 2
    return float(m / np.sqrt(var))


def trades_frame(stocks: list[str], data: dict[str, pd.DataFrame], nc: pd.Series, depth=0.08):
    rows = []
    for s in stocks:
        f = data[s]
        o, c = f.open.to_numpy(), f.close.to_numpy()
        ok = nifty_ok(nc, f.index)
        for t, e, x, px, at_close in p1_trades(o, c, ok, depth):
            rows.append((s, f.index[t], f.index[e], f.index[x], x - e, o[e], px, c[t], at_close))
    df = pd.DataFrame(rows, columns=["stock", "signal", "entry", "exit", "days", "entry_open",
                                     "exit_px", "sig_close", "open_at_end"])  # fmt: skip
    df["qty"] = qty_of(df.sig_close)
    df["net_pct"] = net_pct(df.entry_open, df.exit_px, df.qty)
    df["amount"] = df.entry_open * (1 + SLIP) * df.qty
    df["net_inr"] = df.net_pct / 100 * df.amount
    df["period"] = period_of(df.entry)
    return df


def groups_of() -> dict[str, list[str]]:
    u = pd.read_csv(OUT / "universe.csv", dtype=str)
    return {g: sorted(q.symbol) for g, q in u.groupby("group")}


# ───────────────────────────── stage ref ─────────────────────────────


def match(mine: pd.DataFrame, ref: pd.DataFrame, label: str) -> None:
    key = ["stock", "entry", "exit"]
    a, b = mine[key].astype(str), ref[key].astype(str)
    both = a.merge(b, on=key, how="inner")
    sig = mine[["stock", "signal", "entry"]].astype(str).merge(
        ref[["stock", "signal", "entry"]].astype(str), on=["stock", "signal", "entry"])  # fmt: skip
    say(f"  {label}: {len(mine)} trades; reference {len(ref)}; same entry and exit date "
        f"{len(both)}; same signal and entry date {len(sig)}")  # fmt: skip


def explain(mine: pd.DataFrame, ref: pd.DataFrame, label: str, show: int = 6) -> None:
    """Classes of mismatch: entry date absent from mine, exit date differs, extra trades."""
    m = mine.assign(k=mine.stock + mine.entry.astype(str)).set_index("k")
    r = ref.assign(k=ref.stock + ref.entry.astype(str)).set_index("k")
    miss = [k for k in r.index if k not in m.index]
    extra = [k for k in m.index if k not in r.index]
    diff = [k for k in r.index if k in m.index and str(m.loc[k, "exit"]) != str(r.loc[k, "exit"])]
    say(f"  {label}: reference trades with no entry of mine {len(miss)}; entry matches but exit "
        f"differs {len(diff)}; my trades the reference lacks {len(extra)}")  # fmt: skip
    for k in diff[:show]:
        say(f"     exit differs {r.loc[k, 'stock']} entry {r.loc[k, 'entry']:%Y-%m-%d}: reference "
            f"{r.loc[k, 'exit']:%Y-%m-%d}, mine {m.loc[k, 'exit']:%Y-%m-%d}")  # fmt: skip
    for k in miss[:show]:
        say(f"     missing {r.loc[k, 'stock']} entry {r.loc[k, 'entry']:%Y-%m-%d} exit "
            f"{r.loc[k, 'exit']:%Y-%m-%d}")  # fmt: skip
    for k in extra[:show]:
        say(f"     extra {m.loc[k, 'stock']} entry {m.loc[k, 'entry']:%Y-%m-%d} exit "
            f"{m.loc[k, 'exit']:%Y-%m-%d}")  # fmt: skip


def stage_ref() -> None:
    say("1. P1 against the 247 reference trades (tests/fixtures/pullback/expected_trades.csv)")
    ref = pd.read_csv(FIX / "expected_trades.csv", parse_dates=["signal_date", "entry_date",
                                                                "exit_date"])  # fmt: skip
    ref = ref.rename(columns={"signal_date": "signal", "entry_date": "entry",
                              "exit_date": "exit"})  # fmt: skip
    desk = sorted(ref.stock.unique())
    say(f"  reference: {len(ref)} trades on {len(desk)} stocks, signals {ref.signal.min():%F} "
        f"to {ref.signal.max():%F}, {int(ref.open_at_end.sum())} open at the end")  # fmt: skip
    nc = nifty_long()
    nfx = load_fixture("NIFTY").close
    fix = {s: load_fixture(s) for s in desk}
    # A: my rule on the fixture's own bars and NIFTY (fixture window only)
    a = trades_frame(desk, fix, nfx)
    match(a, ref, "A my rule, fixture bars and fixture NIFTY")
    explain(a, ref, "A")
    # B: my rule on daily.sqlite's bars cut to the fixture window, same NIFTY
    raw = load_sqlite()
    w0, w1 = fix[desk[0]].index[0], fix[desk[0]].index[-1]
    win = {s: raw[s][(raw[s].index >= w0) & (raw[s].index <= w1)] for s in desk}
    b = trades_frame(desk, win, nfx)
    match(b, ref, "B my rule, Dhan daily bars cut to the fixture window")
    explain(b, ref, "B")
    # C: my rule on Dhan bars with the full history from 2010 (what the study does)
    c = trades_frame(desk, raw, nc)
    c = c[(c.entry >= ref.entry.min()) & (c.exit <= ref.exit.max())]
    match(c, ref, "C my rule, Dhan bars from 2010, entries in the reference span")
    explain(c, ref, "C")
    # D: the study's own P1 file
    st = pd.read_pickle(OUT / "trades_P1.pkl")
    st = st[st.stock.isin(desk)].rename(columns={"signal_date": "signal", "entry_date": "entry",
                                                 "exit_date": "exit"})  # fmt: skip
    d = st[(st.entry >= ref.entry.min()) & (st.exit <= ref.exit.max())]
    match(d, ref, "D the study's P1 file, same span")
    explain(d, ref, "D")
    # mine against the study's file on every stock of the desk and the whole span
    cm = trades_frame(desk, raw, nc)
    same = cm.merge(st[["stock", "entry", "exit"]], on=["stock", "entry", "exit"])
    say(f"  my rule on Dhan bars from 2010 vs the study's file, desk 18: mine {len(cm)}, study "
        f"{len(st)}, identical stock/entry/exit {len(same)}")  # fmt: skip
    # data differences: Dhan close against the fixture close on shared dates
    say("  Dhan close / fixture close on shared dates (median, min, max), stocks off by 0.1%+:")
    for s in desk:
        r = (win[s].close / fix[s].close).dropna()
        if r.max() / r.min() > 1.001 or abs(r.median() - 1) > 0.001:
            say(f"     {s:11s} {r.median():.4f} [{r.min():.4f}, {r.max():.4f}]")
    only = sorted({d for s in desk for d in win[s].index.difference(fix[s].index)})
    say(f"  sessions in Dhan's bars and not in the fixture (all 18 stocks, union): "
        f"{[str(d.date()) for d in only]}; in the fixture and not in Dhan: "
        f"{sum(len(fix[s].index.difference(win[s].index)) for s in desk)}")  # fmt: skip
    big = {s: int((abs(win[s].close / fix[s].close - 1) > 0.01).sum()) for s in desk}
    say(f"  days with Dhan close more than 1% off the fixture close, by stock: "
        f"{ {k: v for k, v in big.items() if v} }")  # fmt: skip
    # B2: the same Dhan bars without the sessions the fixture does not have
    same = {s: win[s][win[s].index.isin(fix[s].index)] for s in desk}
    b2 = trades_frame(desk, same, nfx)
    match(b2, ref, "B2 my rule, Dhan bars on the fixture's sessions only")
    explain(b2, ref, "B2", show=4)
    # B3: the fixture's closes and opens on Dhan's session list is not possible; instead the
    # share of trades whose entry open differs by more than 0.5% between the two sources
    m = ref.merge(b2, on=["stock", "entry"], suffixes=("_ref", "_mine"))
    dif = abs(m.entry_open_mine / m.entry_open_ref - 1)
    say(f"  matched entries: Dhan entry open vs the reference's, median difference "
        f"{dif.median() * 100:.3f}%, over 0.5% in {int((dif > 0.005).sum())} of {len(m)}")  # fmt: skip
    mm = m[m.exit_mine == m.exit_ref]
    say(f"  mean net % a trade (before the depository charge) over the {len(ref)} reference "
        f"trades {ref.net_pct_before_dp.mean():+.3f}; mine on fixture bars (A) "
        f"{(a.net_pct + DP / a.amount * 100).mean():+.3f}; on Dhan bars (B) "
        f"{(b.net_pct + DP / b.amount * 100).mean():+.3f}; exact-match trades {len(mm)}")  # fmt: skip


# ───────────────────────────── stage costs ─────────────────────────────


def stage_costs() -> None:
    say("3. Round trip at about Rs 10,000 (config/costs.yaml, equity_delivery)")
    tab = cost_table()
    for price in (50.0, 500.0, 5000.0):
        q = int(qty_of(price))
        tv = price * q
        buy, sell = float(leg_cost(tv, False, tab)), float(leg_cost(tv, True, tab))
        slip = 2 * SLIP * tv
        tot = buy + sell + DP + slip
        say(f"  price {price:7.0f} x {q:3d} = Rs {tv:8.0f}: buy {buy:.2f} sell {sell:.2f} "
            f"depository {DP} slippage {slip:.2f} = Rs {tot:.2f} = {tot / tv * 100:.3f}%")  # fmt: skip
    say(
        f"  charges without slippage at Rs 10,000: "
        f"{(float(leg_cost(1e4, False, tab)) + float(leg_cost(1e4, True, tab)) + DP) / 100:.3f}%"
    )


# ───────────────────────────── stage new ─────────────────────────────


def summary(g: pd.DataFrame, label: str) -> None:
    x = g.net_pct.to_numpy()
    win, loss = x[x > 0], x[x <= 0]
    per = " / ".join(f"{g[g.period == p].net_pct.mean():+.2f} ({(g.period == p).sum()})"
                     for p in ("TUNE", "CHOOSE", "TEST"))  # fmt: skip
    say(f"  {label}: {len(g)} trades, {g.entry.nunique()} entry days, {x.mean():+.2f}% a trade, "
        f"t(week sum) {t_week_sum(x, g.entry):.1f}, t(cluster) {t_cluster(x, g.entry):.1f}, "
        f"win {np.mean(x > 0) * 100:.0f}%, payoff {win.mean() / -loss.mean():.2f}; "
        f"TUNE/CHOOSE/TEST {per}")  # fmt: skip


def stage_new() -> dict[str, pd.DataFrame]:
    say("2. P1 on the 161 new names, my own trades")
    grp = groups_of()
    say("  group sizes: " + ", ".join(f"{k} {len(v)}" for k, v in grp.items()))
    raw, nc = load_sqlite(), nifty_long()
    allst = sorted(raw)
    tr = trades_frame(allst, raw, nc)
    g = {k: tr[tr.stock.isin(v)] for k, v in grp.items()}
    for k in ("new", "desk18", "alpha43"):
        summary(g[k], k)
    summary(tr, "all 222")
    leav = ["ACC", "AMBUJACEM", "BHEL", "DLF", "GAIL", "JINDALSTEL", "PNB", "RELINFRA", "RPOWER",
            "SAIL", "SIEMENS", "TATAPOWER", "VEDL"]  # fmt: skip
    summary(tr[tr.stock.isin(leav)], "13 leavers")
    summary(trades_frame(allst, raw, nc, 0.10), "all 222 at 10% (P1b)")
    st = pd.read_pickle(OUT / "trades_P1.pkl")
    st = st.rename(columns={"entry_date": "entry", "exit_date": "exit"})
    both = tr.merge(st[["stock", "entry", "exit", "net_pct"]], on=["stock", "entry", "exit"],
                    suffixes=("", "_study"))  # fmt: skip
    say(f"  against the study's P1 file: mine {len(tr)}, study {len(st)}, identical "
        f"stock/entry/exit {len(both)}, largest net % difference "
        f"{(both.net_pct - both.net_pct_study).abs().max():.4f}")  # fmt: skip
    gm = tr.net_pct.to_numpy()
    say(f"  share of trades in TUNE {np.mean(tr.period == 'TUNE') * 100:.0f}%; open at the end "
        f"{int(tr.open_at_end.sum())}; stocks costing over Rs 10,000 a share at a signal: "
        f"{tr[tr.sig_close > UNIT].stock.nunique()}")  # fmt: skip
    say(f"  median trade amount Rs {tr.amount.median():,.0f}; mean net % {gm.mean():+.3f}")
    allp = pd.concat(
        [
            pd.read_pickle(OUT / f"trades_{k}.pkl")
            for k in ("P1", "P1b", "M1a", "M1b", "M2a", "M2b", "M3", "R1")
        ]
    )
    last = {s_: float(f.close.iloc[-1]) for s_, f in raw.items()}
    say(f"  median amount, my P1 Rs {tr.amount.median():,.0f}; the study's trades of all eight "
        f"setups Rs {allp.amount.median():,.0f}, under Rs 9,000: "
        f"{(allp.amount < 9000).mean() * 100:.0f}%; stocks whose last close is over Rs 10,000: "
        f"{sorted(k for k, v in last.items() if v > UNIT)}")  # fmt: skip
    return g


# ───────────────────────────── stage bench ─────────────────────────────


def stage_bench(runs: int = 100) -> None:
    say("4. Benchmarks on P1, new names (and all 222)")
    grp = groups_of()
    raw, nc = load_sqlite(), nifty_long()
    tr = trades_frame(sorted(raw), raw, nc)
    arr = {s: (f.open.to_numpy(), f.close.to_numpy(), f.index) for s, f in raw.items()}
    cache: dict[tuple[str, int], float] = {}

    def hold_mean(s: str, d: int) -> float:
        if (s, d) not in cache:
            o, c, _ = arr[s]
            st = np.arange(252, len(c) - d)
            cache[(s, d)] = float(net_pct(o[st], o[st + d], qty_of(c[st - 1])).mean()) if len(
                st) else np.nan  # fmt: skip
        return cache[(s, d)]

    tr["hold"] = [hold_mean(s, max(1, int(d))) for s, d in zip(tr.stock, tr.days, strict=True)]
    tr["excess"] = tr.net_pct - tr.hold
    rng = np.random.default_rng(93)
    for label, sel in (("161 new", tr.stock.isin(grp["new"])), ("all 222", tr.stock.notna())):
        g = tr[sel]
        say(f"  {label}: {len(g)} trades, real {g.net_pct.mean():+.2f}%; same-stock hold "
            f"{g.hold.mean():+.2f}%; excess {g.excess.mean():+.2f}% "
            f"(t week-sum {t_week_sum(g.excess.to_numpy(), g.entry):.1f}, "
            f"t cluster {t_cluster(g.excess.to_numpy(), g.entry):.1f})")  # fmt: skip
        tot = np.zeros(runs)
        n_used, real_used = 0, 0.0
        for (s, y, d), q in g.groupby([g.stock, g.entry.dt.year, g.days.clip(lower=1)]):
            o, c, idx = arr[s]
            cand = np.flatnonzero((idx.year == y) & (np.arange(len(c)) >= 252))
            cand = cand[cand + int(d) < len(c)]
            if len(cand) == 0:
                continue
            pick = rng.choice(cand, size=(runs, len(q)))
            tot += net_pct(o[pick], o[pick + int(d)], qty_of(c[pick - 1])).sum(axis=1)
            n_used += len(q)
            real_used += float(q.net_pct.sum())
        m = tot / n_used
        say(f"     random entry dates, same stock, year and holding days ({runs} runs, "
            f"{n_used} trades with candidates): mean {m.mean():+.2f}% "
            f"[5% {np.percentile(m, 5):+.2f}, 95% {np.percentile(m, 95):+.2f}]; real on the same "
            f"trades {real_used / n_used:+.2f}%; runs at or above real {(m >= real_used / n_used).sum()}"
            f" of {runs}")  # fmt: skip
        say(f"     so the rule's timing is worth {real_used / n_used - m.mean():+.2f} points and "
            f"any entry in those stocks and years {m.mean():+.2f}")  # fmt: skip


# ───────────────────────────── stage lookahead ─────────────────────────────


def stage_lookahead() -> None:
    say("5. Truncation test of my P1 signal")
    raw, nc = load_sqlite(), nifty_long()
    rng = np.random.default_rng(5)
    tr = trades_frame(sorted(raw), raw, nc)
    pick = tr.iloc[rng.choice(len(tr), size=50, replace=False)]
    other = []
    stocks = sorted(raw)
    while len(other) < 50:
        s = stocks[rng.integers(len(stocks))]
        i = int(rng.integers(300, len(raw[s]) - 1))
        other.append((s, raw[s].index[i]))
    cases = [(r.stock, r.signal, True) for r in pick.itertuples()] + [
        (s, d, None) for s, d in other
    ]
    bad_p1 = bad_leak = 0
    for s, d, expect in cases:
        f = raw[s]
        full = p1_signal(f.close.to_numpy(), nifty_ok(nc, f.index))[f.index.get_loc(d)]
        fc = f[f.index <= d]
        cut = p1_signal(fc.close.to_numpy(), nifty_ok(nc[nc.index <= d], fc.index))[-1]
        bad_p1 += int(bool(cut) != bool(full))
        if expect:
            bad_p1 += int(not cut)
        # control: a signal that peeks one bar ahead must FAIL the same test
        lk_full = _leak(f.close.to_numpy())[f.index.get_loc(d)]
        lk_cut = _leak(fc.close.to_numpy())[-1]
        bad_leak += int(bool(lk_cut) != bool(lk_full))
    say(f"  {len(cases)} cases (50 random signals of mine, 50 random bars): signal on data cut "
        f"after the day differs from the full-data signal in {bad_p1}")  # fmt: skip
    say(f"  control, a signal that reads the next close: differs in {bad_leak} of {len(cases)}")
    nxt = 0
    for s, g in tr.groupby("stock"):
        idx = raw[s].index
        nxt += int(sum(idx.get_loc(e) == idx.get_loc(t) + 1 for t, e in zip(g.signal, g.entry,
                                                                              strict=True)))  # fmt: skip
    say(f"  entry is the next session of the signal session in {nxt} of {len(tr)} trades, at that "
        f"session's open (entry price = open: {int((tr.entry_open > 0).sum())} of {len(tr)})")  # fmt: skip


def _leak(c: np.ndarray) -> np.ndarray:
    """A deliberately wrong signal: today's close is under 99% of tomorrow's (needs t+1)."""
    out = np.zeros(len(c), bool)
    out[:-1] = c[:-1] < 0.99 * c[1:]
    return out


# ───────────────────────────── stage data ─────────────────────────────


def stage_data() -> None:
    say("6. Data")
    raw0 = load_sqlite(rescale=False, last=None)
    say("  the five rescalings (price before and after the day, volume median 10 days before "
        "and after, rescale factor):")  # fmt: skip
    for s, day in ACTIONS.items():
        f = raw0[s]
        i = f.index.get_loc(pd.Timestamp(day))
        k = f.close.iloc[i] / f.close.iloc[i - 1]
        v0, v1 = f.volume.iloc[i - 10 : i].median(), f.volume.iloc[i : i + 10].median()
        o_gap = f.open.iloc[i] / f.close.iloc[i - 1]
        say(f"     {s:10s} {day}: close {f.close.iloc[i - 1]:.2f} -> {f.close.iloc[i]:.2f} (x{k:.3f},"
            f" open gap x{o_gap:.3f}), volume x{v1 / v0:.2f}, next 20 sessions "
            f"{f.close.iloc[min(i + 20, len(f) - 1)] / f.close.iloc[i] - 1:+.0%}; bars before the "
            f"day {i}")  # fmt: skip
        r = load_sqlite()[s]
        j = r.index.get_loc(pd.Timestamp(day))
        say(f"        after the rescale the day's move is {r.close.iloc[j] / r.close.iloc[j - 1] - 1:+.1%}")  # fmt: skip
    say("  moves beyond 35% in one close (after the five rescalings), then 20 sessions:")
    fixed = load_sqlite(last=None)
    for s, f in sorted(fixed.items()):
        r = f.close / f.close.shift(1) - 1
        for d in r.index[r.abs() > 0.35]:
            i = f.index.get_loc(d)
            say(f"     {s:11s} {d:%F} {r.loc[d]:+.0%}; volume x"
                f"{f.volume.iloc[i : i + 5].median() / max(1, f.volume.iloc[i - 10 : i].median()):.1f}"
                f"; 20 sessions later {f.close.iloc[min(i + 20, len(f) - 1)] / f.close.iloc[i] - 1:+.0%}")  # fmt: skip
    say("  smaller moves (15% to 35%, either way) with a split signature (price ratio x volume "
        "ratio near 1 over 10 sessions, no reversal):")  # fmt: skip
    for s, f in sorted(fixed.items()):
        r = f.close / f.close.shift(1)
        for d in r.index[((r - 1).abs() > 0.15) & ((r - 1).abs() <= 0.35)]:
            i = f.index.get_loc(d)
            if i < 15 or i + 20 >= len(f):
                continue
            v = f.volume.iloc[i : i + 10].median() / max(1, f.volume.iloc[i - 10 : i].median())
            after = f.close.iloc[i + 20] / f.close.iloc[i] - 1
            if abs(np.log(r.loc[d] * v)) < 0.15 and abs(after) < 0.15:
                say(f"     {s:11s} {d:%F} {r.loc[d] - 1:+.0%}, volume x{v:.2f}, 20 sessions "
                    f"later {after:+.0%}")  # fmt: skip
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    say(f"  rows {q('SELECT COUNT(*) FROM daily')}; duplicate (symbol, date) "
        f"{q('SELECT COUNT(*) FROM (SELECT 1 FROM daily GROUP BY symbol, date HAVING COUNT(*)>1)')};"
        f" a price at or under zero "
        f"{q('SELECT COUNT(*) FROM daily WHERE open<=0 OR high<=0 OR low<=0 OR close<=0')}; "
        f"NULL price {q('SELECT COUNT(*) FROM daily WHERE open IS NULL OR close IS NULL')}")  # fmt: skip
    say(f"  high under low {q('SELECT COUNT(*) FROM daily WHERE high<low')}; close outside the "
        f"day's range {q('SELECT COUNT(*) FROM daily WHERE close>high OR close<low')}; open "
        f"outside {q('SELECT COUNT(*) FROM daily WHERE open>high OR open<low')}; zero volume "
        f"{q('SELECT COUNT(*) FROM daily WHERE volume<=0')}")  # fmt: skip
    gaps, weekend, unsorted = [], 0, 0
    for s, f in raw0.items():
        dd = f.index.to_series().diff().dt.days.dropna()
        gaps += [(s, int(v), str(i.date())) for i, v in dd[dd > 7].items()]
        weekend += int((f.index.dayofweek >= 5).sum())
        unsorted += int(not f.index.is_monotonic_increasing)
    say(f"  weekend dates {weekend}; unsorted series {unsorted}; gaps over 7 calendar days "
        f"{len(gaps)}: {gaps[:8]}")  # fmt: skip
    first = pd.Series({s: f.index[0] for s, f in raw0.items()})
    fy = first.dt.year.value_counts().sort_index()
    say("  first bar by year: " + " ".join(f"{y}:{n}" for y, n in fy.items()))
    late = sorted((s for s, d in first.items() if d >= pd.Timestamp("2023-11-08")))
    say(f"  stocks starting on or after 2023-11-08: {late or 'none'}; stocks in daily.sqlite "
        f"{len(raw0)}; with fewer than 2,000 bars {[s for s, f in raw0.items() if len(f) < 2000]}")  # fmt: skip
    ny = nifty_long()
    say(f"  NIFTY series {ny.index[0]:%F} to {ny.index[-1]:%F}, {len(ny)} closes; duplicate dates "
        f"{int(ny.index.duplicated().sum())}")  # fmt: skip
    # does the rescale of TMPV or BRITANNIA touch a P1 signal window (SMA200 of a signal bar)?
    nc = nifty_long()
    for s in ("TMPV", "BRITANNIA", "ADANIENT", "JSL", "TATACHEM"):
        for rs in (True, False):
            f = load_sqlite(rescale=rs)[s]
            t = trades_frame([s], {s: f}, nc)
            say(f"  P1 on {s} with rescale={rs}: {len(t)} trades, mean {t.net_pct.mean():+.2f}%, "
                f"first signal {t.signal.min():%F}")  # fmt: skip


def stage_calendar() -> None:
    say("6b. Sessions on a Saturday or Sunday in daily.sqlite")
    raw = load_sqlite(rescale=False, last=None)
    cnt: dict[pd.Timestamp, int] = {}
    for f in raw.values():
        for d in f.index[f.index.dayofweek >= 5]:
            cnt[d] = cnt.get(d, 0) + 1
    ser = pd.Series(cnt).sort_index()
    say(
        f"  {len(ser)} weekend dates; bars {int(ser.sum())}; stocks with a bar on the date, by date:"
    )
    say("  " + ", ".join(f"{d:%F}:{n}" for d, n in ser.items()))
    allp = pd.DatetimeIndex(sorted(set().union(*[set(f.index) for f in raw.values()])))
    share = pd.Series({d: sum(d in f.index for f in raw.values()) for d in allp[-300:]})
    say(f"  stocks with a bar a day over the last 300 dates: min {share.min()}, median "
        f"{int(share.median())}")  # fmt: skip


# ───────────────────────────── stage book ─────────────────────────────


def book(g: pd.DataFrame, cal: pd.DatetimeIndex) -> dict:
    months = max(1.0, (LAST - g.entry.min()).days / 30.44)
    e, x = g.entry.to_numpy(), g.exit.to_numpy()
    cal = cal[(cal >= g.entry.min()) & (cal <= LAST)]
    opn = np.array([int(((e <= d) & (x > d)).sum()) for d in cal.to_numpy()])
    seq = g.sort_values("exit", kind="stable").net_pct.to_numpy() <= 0
    run = best = 0
    for lose in seq:
        run = run + 1 if lose else 0
        best = max(best, run)
    mon = g.groupby(g.exit.dt.to_period("M")).net_inr.sum()
    yr = g.groupby(g.exit.dt.year).net_inr.sum()
    return {"per_month": len(g) / months, "open_median": float(np.median(opn)),
            "open_max": int(opn.max()), "lose_run": best, "worst_month": f"{mon.idxmin()} "
            f"{mon.min():,.0f}", "worst_year": f"{yr.idxmin()} {yr.min():,.0f}",
            "total": f"{g.net_inr.sum():,.0f}"}  # fmt: skip


def stage_book() -> None:
    say("7. The book figures (every P1 signal on the 222 stocks, Rs 10,000 each)")
    raw, nc = load_sqlite(), nifty_long()
    cal = pd.DatetimeIndex(sorted(set().union(*[set(f.index) for f in raw.values()])))
    st = pd.read_pickle(OUT / "trades_P1.pkl").rename(
        columns={"entry_date": "entry", "exit_date": "exit"})  # fmt: skip
    say("  from the study's P1 file: " + str(book(st, cal)))
    for depth, label in ((0.08, "P1"), (0.10, "P1b")):
        mine = trades_frame(sorted(raw), raw, nc, depth)
        say(f"  from my own {label} trades: {book(mine, cal)}")
    stb = pd.read_pickle(OUT / "trades_P1b.pkl").rename(
        columns={"entry_date": "entry", "exit_date": "exit"})  # fmt: skip
    say("  from the study's P1b file: " + str(book(stb, cal)))
    # the trade count a month by calendar month, not an average: median and busiest
    pm = st.groupby(st.entry.dt.to_period("M")).size()
    say(f"  signals a month by calendar month: median {pm.median():.0f}, busiest {pm.max()} "
        f"({pm.idxmax()})")  # fmt: skip


def main() -> None:
    stages = {
        "ref": stage_ref,
        "new": stage_new,
        "costs": stage_costs,
        "bench": stage_bench,
        "lookahead": stage_lookahead,
        "data": stage_data,
        "calendar": stage_calendar,
        "book": stage_book,
    }
    want = sys.argv[1] if len(sys.argv) > 1 else "all"
    for name, fn in stages.items():
        if want in ("all", name):
            say(f"--- {name}")
            fn()
            say()
    (OUT / f"verify_{want}.txt").write_text("\n".join(LINES) + "\n")


if __name__ == "__main__":
    main()
