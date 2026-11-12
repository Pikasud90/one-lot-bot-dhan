"""Pre-registered study of long-only multi-day stock strategies on daily bars (PLAN.md section A).

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --stage 1
    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --stage 2

Stage 1: the store's cleaned daily bars (read-only), entries 2021-11-08..2026-11-07.
Stage 2: Yahoo split-adjusted daily bars 2010-01-01..2022-03-31, entries 2011-01-01..2021-11-07.
Signal on the close of day t, fill at the open of day t+1, 0.03% slippage a side, one share,
delivery charges on both legs, Rs 14.75 depository charge per sale reported separately.
Research only: no orders, no writes to the store.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "alpha_2026-11-12"
OLD = OUT / "daily_old"
SLIP = h.STOCK_SLIPPAGE
DP = h.DP_CHARGE_INR

S1_START, S1_END = pd.Timestamp("2021-11-08"), pd.Timestamp("2026-11-07")
S1_PERIODS = [("TUNE", "2021-11-08", "2024-11-07"), ("CHOOSE", "2024-11-08", "2025-11-07"),
              ("TEST", "2025-11-08", "2026-11-07")]  # fmt: skip
S2_FETCH = ("2010-01-01", "2022-03-31")
S2_START, S2_END = pd.Timestamp("2011-01-01"), pd.Timestamp("2021-11-07")
S2_PERIODS = [("11-14", "2011-01-01", "2014-12-31"), ("15-18", "2015-01-01", "2018-12-31"),
              ("19-21", "2019-01-01", "2021-11-07")]  # fmt: skip


# ───────────────────────────── data ─────────────────────────────


def _naive_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["open", "high", "low", "close", "volume"]].astype(float).copy()
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = pd.DatetimeIndex(idx).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def load_store() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    h._init_worker()
    out = {}
    for s in h.STOCKS:
        b = _naive_daily(h._cache.bars(h._cache.spec(s).series_key, "1d"))
        out[s] = b[b.index <= S1_END]
    n = _naive_daily(h._cache.bars(h._cache.spec("NIFTY_OPT").series_key, "1d"))
    return out, n[n.index <= S1_END]


def yahoo_symbols() -> dict[str, str]:
    import yaml

    u = yaml.safe_load((ROOT / "config" / "universe.yaml").read_text())
    syms = {}

    def walk(x):
        if isinstance(x, dict):
            if "key" in x and "yahoo" in x:
                syms[x["key"]] = x["yahoo"]
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(u)
    return syms


def fetch_old() -> None:
    import yfinance as yf

    OLD.mkdir(parents=True, exist_ok=True)
    syms = yahoo_symbols()
    want = {s: syms[s] for s in h.STOCKS} | {"NIFTY": syms["NIFTY"]}
    for key, sym in want.items():
        f = OLD / f"{key}.csv"
        if f.exists():
            continue
        time.sleep(1.5)
        df = yf.download(sym, start=S2_FETCH[0], end="2022-04-01", interval="1d",
                         auto_adjust=False, actions=False, progress=False, threads=False)  # fmt: skip
        if df is None or df.empty:
            print(f"fetch failed or empty: {key} {sym}")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"
        df.to_csv(f)
        print(f"fetched {key} {sym}: {len(df)} rows {df.index[0].date()}..{df.index[-1].date()}")


def load_old() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    out = {}
    for s in h.STOCKS + ["NIFTY"]:
        f = OLD / f"{s}.csv"
        if f.exists():
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            df = _naive_daily(df.dropna(subset=["open", "high", "low", "close"]))
            df = df[(df.open > 0) & (df.close > 0)]
            out[s] = df
    nifty = out.pop("NIFTY")
    return out, nifty


# ───────────────────────────── costs ─────────────────────────────

_LC = None


def legcost():
    global _LC
    if _LC is None:
        if h._cost_cfg is None:
            h._init_worker()
        _LC = h.LegCost.from_config(h._cost_cfg.product("equity_delivery"))
    return _LC


def make_trade(strat, stock, df, sig_i, ent_i, ex_i, at_close=False):
    """Fill at open[ent_i]; exit at open[ex_i], or close[ex_i] when the trade is still open."""
    lc = legcost()
    ef = df.open.iat[ent_i] * (1 + SLIP)
    xraw = df.close.iat[ex_i] if at_close else df.open.iat[ex_i]
    xf = xraw * (1 - SLIP)
    charges = float(lc.leg(np.array([ef]), sell=False)[0] + lc.leg(np.array([xf]), sell=True)[0])
    net = xf - ef - charges
    return {
        "strategy": strat, "stock": stock, "signal_date": df.index[sig_i].date(),
        "entry_date": df.index[ent_i].date(), "entry_price": ef,
        "exit_date": df.index[ex_i].date(), "exit_price": xf, "days": int(ex_i - ent_i),
        "charges": charges, "net_inr_before_dp": net, "net_pct_before_dp": net / ef * 100,
        "net_pct_after_dp": (net - DP) / ef * 100, "open_at_end": bool(at_close),
    }  # fmt: skip


# ───────────────────────────── indicators (causal) ─────────────────────────────


def prior_max(x: pd.Series, n: int) -> pd.Series:  # max of x[t-n..t-1]
    return x.rolling(n, min_periods=n).max().shift(1)


def prior_min(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).min().shift(1)


def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()


def st_dir(df: pd.DataFrame, mult: float) -> np.ndarray:
    from onelot.indicators.core import supertrend

    return supertrend(df.high, df.low, df.close, 14, mult)["st_dir"].to_numpy()


def weekly_dir_on_daily(df: pd.DataFrame, mult: float) -> np.ndarray:
    """Weekly Supertrend direction of the last week whose final session is <= day t."""
    g = df.groupby(df.index.to_period("W-FRI"))
    wk = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                       "close": g.close.last(), "last": g.apply(lambda x: x.index[-1])})  # fmt: skip
    wk = wk.set_index("last")
    if len(wk) and wk.index[-1].weekday() != 4:  # the data's final week may still be running
        wk = wk.iloc[:-1]
    wd = pd.Series(st_dir(wk, mult), index=wk.index)
    # a week counts on day t only if its last session is <= t (completed by the close of t)
    m = pd.merge_asof(pd.DataFrame(index=df.index), wd.rename("w").to_frame(),
                      left_index=True, right_index=True, direction="backward")  # fmt: skip
    return m["w"].fillna(0).to_numpy()


def nifty_ok(df: pd.DataFrame, nifty: pd.DataFrame) -> np.ndarray:
    ok = (nifty.close > sma(nifty.close, 200)).astype(float)
    m = pd.merge_asof(pd.DataFrame(index=df.index), ok.rename("ok").to_frame(),
                      left_index=True, right_index=True, direction="backward")  # fmt: skip
    return m["ok"].fillna(0).to_numpy() > 0


# ───────────────────────────── per-stock signals ─────────────────────────────


def signals(name: str, df: pd.DataFrame, nifty: pd.DataFrame):
    """Return (entry[t], exit_fn) where exit_fn(t, entry_idx) -> exit signal at close t."""
    c, v = df.close, df.volume
    if name.startswith("S1_") or name == "S7_S1":
        n, m = (55, 20) if name == "S7_S1" else map(int, name[3:].split("_"))
        ent = (c > prior_max(c, n)).to_numpy()
        ex = (c < prior_min(c, m)).to_numpy()
        if name == "S7_S1":
            ent = ent & nifty_ok(df, nifty)
        return ent, lambda t, e: ex[t]
    if name == "S2":
        cond = (sma(c, 50) > sma(c, 200)).to_numpy()
        return cond, lambda t, e: not cond[t]
    if name.startswith("S4") or name == "S7_S4":
        ent = c > prior_max(c, 252)
        if name in ("S4_vol", "S7_S4"):
            ent = ent & (v >= 1.5 * sma(v, 50).shift(1))
        ent = ent.to_numpy()
        if name == "S7_S4":
            ent = ent & nifty_ok(df, nifty)
        ex = (c < sma(c, 50)).to_numpy()
        return ent, lambda t, e: ex[t]
    if name.startswith("S5_"):
        hold = int(name[3:])
        rng = (df.high - df.low).replace(0, np.nan)
        ent = ((c / c.shift(1) - 1 >= 0.03) & (v >= 3 * sma(v, 50).shift(1))
               & ((c - df.low) / rng >= 0.75)).to_numpy()  # fmt: skip
        low, cl = df.low.to_numpy(), c.to_numpy()
        return ent, lambda t, e: (t >= e + hold - 1) or (cl[t] < low[e - 1])
    if name.startswith("S6_"):
        mult = float(name[3:])
        d = st_dir(df, mult)
        w = weekly_dir_on_daily(df, mult)
        prev = np.r_[0, d[:-1]]
        ent = (d == 1) & (prev == -1) & (w == 1)
        return ent, lambda t, e: d[t] == -1
    raise ValueError(name)


def simulate(name, stock, df, nifty, start, end):
    ent, exit_fn = signals(name, df, nifty)
    dates = df.index
    n = len(df)
    trades, pos = [], None
    for t in range(n):
        if pos is not None:
            if t >= pos and exit_fn(t, pos):
                if t + 1 < n:
                    trades.append(make_trade(name, stock, df, pos - 1, pos, t + 1))
                    pos = None
                    continue  # no new entry on the close that exits
        if pos is None and ent[t] and t + 1 < n and start <= dates[t + 1] <= end:
            pos = t + 1
    if pos is not None:
        trades.append(make_trade(name, stock, df, pos - 1, pos, n - 1, at_close=True))
    return trades


def rotation(name, data, start, end):
    """S3: month-end rotation over the 18 stocks, top 5 with positive momentum, sell below rank 9."""
    look = 252 if name == "S3_12_1" else 126
    cal = sorted(set().union(*[set(d.index) for d in data.values()]))
    cal = pd.DatetimeIndex(cal)
    # momentum on each stock's own sessions (skip 21, look back `look`), carried to the calendar
    mom = pd.DataFrame({s: (d.close.shift(21) / d.close.shift(look) - 1) for s, d in data.items()})
    mom = mom.reindex(cal).ffill(limit=5)
    month_end = cal.to_series().groupby(cal.to_period("M")).max().values
    held = {}  # stock -> (sig_i on own index, ent_i)
    trades = []

    def next_idx(s, day):
        d = data[s].index
        j = d.searchsorted(day, side="right")
        return j if j < len(d) else None

    for me in month_end:
        me = pd.Timestamp(me)
        row = mom.loc[me].dropna()
        ranks = row.rank(ascending=False, method="first")
        # sells
        for s in list(held):
            r = ranks.get(s, np.inf)
            if r > 9 or row.get(s, -1) <= 0:
                j = next_idx(s, me)
                if j is not None:
                    sig_i, ent_i = held.pop(s)
                    trades.append(make_trade(name, s, data[s], sig_i, ent_i, j))
        # buys
        for s in ranks.sort_values().index:
            if len(held) >= 5:
                break
            if ranks[s] > 5 or row[s] <= 0 or s in held:
                continue
            j = next_idx(s, me)
            if j is None:
                continue
            ed = data[s].index[j]
            if start <= ed <= end:
                held[s] = (j - 1, j)
    for s, (sig_i, ent_i) in held.items():
        d = data[s]
        trades.append(make_trade(name, s, d, sig_i, ent_i, len(d) - 1, at_close=True))
    return trades


STRATS = ["S1_55_20", "S1_20_10", "S1_100_50", "S2", "S3_12_1", "S3_6_1", "S4_vol", "S4_novol",
          "S5_40", "S5_20", "S5_60", "S6_1.7", "S6_3.0", "S7_S1", "S7_S4"]  # fmt: skip


def run_all(data, nifty, start, end, drift):
    rows = []
    for name in STRATS:
        if name.startswith("S3"):
            rows += rotation(name, data, start, end)
        else:
            for s, df in data.items():
                rows += simulate(name, s, df, nifty, start, end)
    tr = pd.DataFrame(rows)
    tr["benchmark_pct"] = tr.stock.map(drift) * tr.days * 100
    tr["entry_date"] = pd.to_datetime(tr.entry_date)
    return tr


# ───────────────────────────── independent S1 check ─────────────────────────────


def s1_slow(stock, df, start, end, n=55, m=20):
    cfg = h._cost_cfg.product("equity_delivery")
    b, tx = cfg.brokerage, cfg.taxes

    def charges(turn, sell):
        brok = min(float(b.per_order_inr), float(b.pct_of_turnover) * turn, float(b.cap_inr))
        fees = (
            float(tx.exchange_txn_pct) + float(tx.sebi_turnover_pct) + float(tx.ipft_pct)
        ) * turn
        x = brok + fees + float(tx.gst_pct) * (brok + fees)
        x += (float(tx.stt_sell_pct) if sell else float(tx.stt_buy_pct)) * turn
        if not sell:
            x += float(tx.stamp_duty_pct) * turn
        return x

    O = list(df.open)
    C = list(df.close)
    D = list(df.index)
    out = []
    holding = False
    k = 0
    for t in range(len(C)):
        if holding:
            if t >= k and t >= m and C[t] < min(C[t - m : t]):
                if t + 1 < len(C):
                    ef = O[k] * (1 + SLIP)
                    xf = O[t + 1] * (1 - SLIP)
                    net = xf - ef - charges(ef, False) - charges(xf, True)
                    out.append((D[k].date(), D[t + 1].date(), net / ef * 100))
                    holding = False
                    continue
        if not holding and t >= n and C[t] > max(C[t - n : t]) and t + 1 < len(C):
            if start <= D[t + 1] <= end:
                holding, k = True, t + 1
    if holding:
        ef = O[k] * (1 + SLIP)
        xf = C[-1] * (1 - SLIP)
        net = xf - ef - charges(ef, False) - charges(xf, True)
        out.append((D[k].date(), D[-1].date(), net / ef * 100))
    return out


# ───────────────────────────── reporting ─────────────────────────────


def tstat(x):
    x = np.asarray(x, float)
    if len(x) < 2 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def table(tr, periods, after_dp=True):
    hdr = (f"{'strategy':10s} {'N':>5s} {'win%':>5s} {'meanB':>6s} {'tB':>5s} "
           + (f"{'meanA':>6s} {'tA':>5s} " if after_dp else "") + f"{'days':>5s} "
           + " ".join(f"{p[0]+'(n)':>14s}" for p in periods)
           + f" {'pos':>5s} " + (f"{'Rs1sh':>8s} " if after_dp else "")
           + f"{'bench':>6s} {'excB':>6s} {'tEx':>5s}")  # fmt: skip
    lines = [hdr]
    col = "net_pct_after_dp" if after_dp else "net_pct_before_dp"
    for name in STRATS:
        g = tr[tr.strategy == name]
        if g.empty:
            lines.append(f"{name:10s} no trades")
            continue
        b, a = g.net_pct_before_dp, g.net_pct_after_dp
        per = []
        for _p, lo, hi in periods:
            q = g[(g.entry_date >= lo) & (g.entry_date <= hi)][col]
            per.append(f"{q.mean() if len(q) else np.nan:+7.2f}({len(q):4d})")
        pos = int((g.groupby("stock")[col].sum() > 0).sum())
        exc = b - g.benchmark_pct
        s = (f"{name:10s} {len(g):5d} {100*(b>0).mean() if not after_dp else 100*(a>0).mean():5.1f} "
             f"{b.mean():+6.2f} {tstat(b):5.2f} "
             + (f"{a.mean():+6.2f} {tstat(a):5.2f} " if after_dp else "")
             + f"{g.days.mean():5.1f} " + " ".join(f"{x:>14s}" for x in per)
             + f" {pos:2d}/{g.stock.nunique():2d} "
             + (f"{(g.net_inr_before_dp - h.DP_CHARGE_INR).sum():8.0f} " if after_dp else "")
             + f"{g.benchmark_pct.mean():+6.2f} {exc.mean():+6.2f} {tstat(exc):5.2f}")  # fmt: skip
        lines.append(s)
    return lines


def candidates(tr):
    out = []
    for name in STRATS:
        g = tr[tr.strategy == name]
        tune = g[(g.entry_date >= "2021-11-08") & (g.entry_date <= "2024-11-07")]
        cho = g[(g.entry_date >= "2024-11-08") & (g.entry_date <= "2025-11-07")]
        tc = pd.concat([tune, cho])
        if len(tc) < 2:
            out.append(f"{name}: too few trades")
            continue
        a = tc.net_pct_after_dp
        pos = int((tc.groupby("stock").net_pct_after_dp.sum() > 0).sum())
        checks = {
            "TUNE>0": tune.net_pct_after_dp.mean() > 0,
            "CHOOSE>0": len(cho) > 0 and cho.net_pct_after_dp.mean() > 0,
            "t>=2": tstat(a) >= 2,
            "pos>=12": pos >= 12,
            "beats bench": a.mean() > tc.benchmark_pct.mean(),
        }
        fails = [k for k, v in checks.items() if not v]
        out.append(f"{name:10s} t(T+C,afterDP)={tstat(a):5.2f} pos(T+C)={pos:2d} "
                   f"mean={a.mean():+.2f} bench={tc.benchmark_pct.mean():+.2f} -> "
                   + ("CANDIDATE" if not fails else "fails: " + ", ".join(fails)))  # fmt: skip
    return out


def jumps(data):
    lines = []
    for s, d in data.items():
        r = d.close / d.close.shift(1) - 1
        bad = r[r.abs() > 0.20]
        lines.append(f"  {s}: " + ("none" if bad.empty else ", ".join(
            f"{i.date()} {v:+.1%}" for i, v in bad.items())))  # fmt: skip
    return lines


def lookahead_check(data, nifty):
    """Recompute every per-stock signal on data truncated at a cut and compare the prefix."""
    bad = 0
    for s in list(data)[:4]:
        df = data[s]
        for cut in (len(df) // 2, len(df) - 300, len(df) - 37):
            for name in STRATS:
                if name.startswith("S3"):
                    continue
                ef, xf = signals(name, df, nifty)
                et, xt = signals(name, df.iloc[:cut], nifty[nifty.index <= df.index[cut - 1]])
                e_ok = np.array_equal(ef[:cut], et[:cut])
                x_ok = all(
                    bool(xf(t, max(t - 5, 60))) == bool(xt(t, max(t - 5, 60)))
                    for t in range(cut - 200, cut)
                )
                bad += (not e_ok) + (not x_ok)
    return bad


def stage1():
    data, nifty = load_store()
    lines = ["STAGE 1  store daily bars, entries 2021-11-08..2026-11-07, one share, long only",
             "first daily bar per stock: " + ", ".join(
                 f"{s} {d.index[0].date()}" for s, d in data.items()),
             f"NIFTY first {nifty.index[0].date()}, last bar {nifty.index[-1].date()}"]  # fmt: skip
    drift = {}
    bh = {}
    for s, d in data.items():
        w = d[(d.index >= S1_START) & (d.index <= S1_END)]
        drift[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
        bh[s] = (w.close.iat[-1] / w.open.iat[0] - 1) * 100
    tr = run_all(data, nifty, S1_START, S1_END, drift)
    lines += [
        "",
        "meanB/meanA = mean % a trade before/after DP; t trade-level; periods after DP;",
        "pos = stocks with positive total after DP (all periods); Rs1sh = net Rs at one share "
        "after DP; bench = drift x days; excB = before-DP excess",
        "",
    ]
    lines += table(tr, S1_PERIODS, after_dp=True)
    lines += [
        "",
        "buy & hold % (first open on/after 2021-11-08 to last close): "
        + " ".join(f"{s} {v:+.0f}" for s, v in bh.items())
        + f" | mean {np.mean(list(bh.values())):+.1f}",
    ]
    lines += ["", "candidate rule (TUNE+CHOOSE, after DP):"] + candidates(tr)
    # validation
    lines += ["", "VALIDATION"]
    nb = lookahead_check(data, nifty)
    lines.append(f"1. truncation test (signals recomputed on data cut at 3 points, 4 stocks, 13 "
                 f"per-stock strategies): mismatches = {nb}")  # fmt: skip
    fast = tr[tr.strategy == "S1_55_20"].sort_values(["stock", "entry_date"])
    slow = []
    for s, d in data.items():
        slow += [(s, *x) for x in s1_slow(s, d, S1_START, S1_END)]
    sl = pd.DataFrame(slow, columns=["stock", "entry", "exit", "net"]).sort_values(
        ["stock", "entry"]
    )
    ok = (len(sl) == len(fast)
          and (sl.entry.values == fast.entry_date.dt.date.values).all()
          and (sl.exit.values == fast.exit_date.values).all()
          and np.allclose(sl.net.values, fast.net_pct_before_dp.values, atol=1e-9, rtol=0))  # fmt: skip
    mx = (
        np.abs(sl.net.values - fast.net_pct_before_dp.values).max()
        if len(sl) == len(fast)
        else np.nan
    )
    lines.append(
        f"2. S1 slow loop: {len(sl)} trades vs {len(fast)}; identical = {ok}; max |diff| {mx:.2e}"
    )
    lines.append("3. sample S1 trades:")
    for _, r in fast.sample(3, random_state=7).iterrows():
        lines.append(
            f"   {r.stock} sig {r.signal_date} buy {r.entry_date.date()} @ {r.entry_price:.4f} "
            f"sell {r.exit_date} @ {r.exit_price:.4f} charges Rs {r.charges:.4f} "
            f"net {r.net_pct_before_dp:+.4f}% beforeDP {r.net_pct_after_dp:+.4f}% afterDP"
        )
    lines.append("4. |close/prev close - 1| > 20% (full store history):")
    lines += jumps(data)
    lines.append(f"open-at-end trades (closed at last close): {int(tr.open_at_end.sum())}")
    txt = "\n".join(lines)
    (OUT / "stocks_stage1.txt").write_text(txt + "\n")
    cols = ["strategy", "stock", "entry_date", "entry_price", "exit_date", "exit_price", "days",
            "net_pct_before_dp", "net_pct_after_dp", "benchmark_pct", "open_at_end"]  # fmt: skip
    t2 = tr[cols].copy()
    t2["entry_date"] = t2.entry_date.dt.date
    t2.to_csv(OUT / "stocks_trades_stage1.csv", index=False, float_format="%.6f")
    print(txt)


def stage2():
    store, _ = load_store()
    fetch_old()
    old, nifty = load_old()
    lines = [
        "STAGE 2  Yahoo split-adjusted daily, entries 2011-01-01..2021-11-07, before DP",
        f"NIFTY {nifty.index[0].date()}..{nifty.index[-1].date()} ({len(nifty)} bars)",
        "continuity:",
    ]
    keep, keep_b = {}, {}
    for s in h.STOCKS:
        if s not in old:
            lines.append(f"  {s}: no data - dropped")
            continue
        y, st = old[s], store[s]
        j = pd.concat([y.close.rename("y"), st.close.rename("s")], axis=1, join="inner")
        j = j[(j.index >= "2021-11-08") & (j.index <= "2022-03-31")]
        ratio = j.y / j.s
        spread = ratio.max() / ratio.min() - 1 if len(j) else np.nan
        r = y.close / y.close.shift(1) - 1
        big = r[r.abs() > 0.20]
        ok = len(j) > 50 and spread <= 0.01 and big.empty
        # variant B: a jump also present in the store's cleaned bars (same day, within 0.5 pp) is a
        # real market move; jumps before the store starts cannot be cross-checked and still drop
        rs = st.close / st.close.shift(1) - 1
        confirmed = all(i in rs.index and abs(rs[i] - v) < 0.005 for i, v in big.items())
        ok_b = len(j) > 50 and spread <= 0.01 and confirmed
        lines.append(f"  {s}: overlap {len(j)} days, ratio {ratio.median():.4f}, spread {spread:.2%}, "
                     f"jumps>20%: " + (", ".join(f"{i.date()} {v:+.1%}" for i, v in big.items()) or "none")
                     + ("" if ok else "  -> DROPPED"))  # fmt: skip
        if ok:
            k = 1 / ratio.median()
            y = y.copy()
            y[["open", "high", "low", "close"]] *= k
            y["volume"] = y.volume / k
            keep[s] = y
        if ok_b:
            k = 1 / ratio.median()
            yb = old[s].copy()
            yb[["open", "high", "low", "close"]] *= k
            yb["volume"] = yb.volume / k
            keep_b[s] = yb
    drift = {}
    for s, d in keep.items():
        w = d[(d.index >= S2_START) & (d.index <= S2_END)]
        drift[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
    tr = run_all(keep, nifty, S2_START, S2_END, drift)
    lines += [
        "",
        f"stocks kept: {len(keep)}; pos = stocks with positive total before DP; "
        "bench drift on 2011-01-01..2021-11-07",
        "",
    ]
    lines += table(tr, S2_PERIODS, after_dp=False)
    lines.append(
        f"open-at-end trades (closed at last close 2022-03-31): {int(tr.open_at_end.sum())}"
    )
    tr.to_csv(OUT / "stocks_trades_stage2.csv", index=False, float_format="%.6f")
    drift_b = {}
    for s, d in keep_b.items():
        w = d[(d.index >= S2_START) & (d.index <= S2_END)]
        drift_b[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
    trb = run_all(keep_b, nifty, S2_START, S2_END, drift_b)
    lines += [
        "",
        f"VARIANT B (sensitivity, not the rule): also keep stocks whose >20% jumps are the same "
        f"day and size in the store's cleaned bars: {len(keep_b)} stocks "
        f"(added {', '.join(sorted(set(keep_b) - set(keep)))})",
        "",
    ]
    lines += table(trb, S2_PERIODS, after_dp=False)
    txt = "\n".join(lines)
    (OUT / "stocks_stage2.txt").write_text(txt + "\n")
    print(txt)


# ═════════════════════════════ family "reversion" (PLAN.md section A2) ═════════════════════════════

RSTRATS = ["R1_10", "R1_5", "R1_20", "R2", "R3_10", "R3_5", "R3_20", "R4_8", "R4_6", "R4_10",
           "R5_4", "R5_3", "R5_5", "R6_3", "R6_5", "R7"]  # fmt: skip


def rsignals(name: str, df: pd.DataFrame):
    """(entry[t], exit_fn(t, entry_idx)) from data up to the close of t only."""
    from onelot.indicators.core import rsi

    c = df.close
    above200 = (c > sma(c, 200)).to_numpy()

    def timed(cond: np.ndarray, n: int):  # exit on cond[t] or once the trade is n sessions old
        return lambda t, e: bool(cond[t]) or (t - e + 1 >= n)

    if name.startswith("R1_") or name == "R2":
        lvl = 10 if name == "R2" else int(name[3:])
        ent = (rsi(c, 2) < lvl).to_numpy()
        if name != "R2":
            ent = ent & above200
        return ent, timed((c > sma(c, 5)).to_numpy(), 10)
    if name.startswith("R3_"):
        n = int(name[3:])
        hold = {10: 15, 5: 10, 20: 30}[n]
        ent = (c <= c.rolling(n, min_periods=n).min()).to_numpy() & above200
        return ent, timed((c > sma(c, n)).to_numpy(), hold)
    if name.startswith("R4_"):
        pct = int(name[3:]) / 100
        cond = c <= (1 - pct) * c.rolling(20, min_periods=20).max()
        first = (cond & ~cond.shift(1, fill_value=False)).to_numpy()
        return first & above200, timed((c > sma(c, 20)).to_numpy(), 30)
    if name.startswith("R5_"):
        k = int(name[3:])
        ent = np.ones(len(c), bool)
        for j in range(k):
            ent &= (c.shift(j) < c.shift(j + 1)).to_numpy()
        return ent, timed(np.zeros(len(c), bool), 5)
    if name == "R7":
        m = sma(c, 20)
        sd = c.rolling(20, min_periods=20).std(ddof=0)
        ent = (c < m - 2 * sd).to_numpy() & above200
        return ent, timed((c > m).to_numpy(), 20)
    raise ValueError(name)


def rsimulate(name, stock, df, start, end):
    ent, exit_fn = rsignals(name, df)
    dates, n = df.index, len(df)
    trades, pos = [], None
    for t in range(n):
        if pos is not None and t >= pos and exit_fn(t, pos) and t + 1 < n:
            trades.append(make_trade(name, stock, df, pos - 1, pos, t + 1))
            pos = None
            continue
        if pos is None and ent[t] and t + 1 < n and start <= dates[t + 1] <= end:
            pos = t + 1
    if pos is not None:
        trades.append(make_trade(name, stock, df, pos - 1, pos, n - 1, at_close=True))
    return trades


def weekly_losers(name, data, start, end):
    """R6: at each calendar week's last session buy the K lowest 5-session returns, re-rank weekly."""
    k = int(name[3:])
    cal = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    week_end = cal.to_series().groupby(cal.to_period("W-FRI")).max().values
    held, trades = {}, []
    for we in week_end:
        we = pd.Timestamp(we)
        r5 = {}
        for s, d in data.items():
            if we in d.index:  # the stock trades on this session and has 5 prior sessions
                i = d.index.get_loc(we)
                if i >= 5:
                    r5[s] = d.close.iat[i] / d.close.iat[i - 5] - 1
        pick = set(pd.Series(r5).sort_values(kind="mergesort").index[:k]) if r5 else set()
        for s in list(held):
            if s not in pick:
                d = data[s]
                j = d.index.searchsorted(we, side="right")
                if j < len(d):
                    sig_i, ent_i = held.pop(s)
                    trades.append(make_trade(name, s, d, sig_i, ent_i, j))
        for s in sorted(pick):
            if s in held:
                continue
            d = data[s]
            j = d.index.searchsorted(we, side="right")
            if j < len(d) and start <= d.index[j] <= end:
                held[s] = (j - 1, j)
    for s, (sig_i, ent_i) in held.items():
        d = data[s]
        trades.append(make_trade(name, s, d, sig_i, ent_i, len(d) - 1, at_close=True))
    return trades


def run_reversion(data, start, end, drift, dp_today, sample):
    rows = []
    for name in RSTRATS:
        if name.startswith("R6"):
            rows += weekly_losers(name, data, start, end)
        else:
            for s, df in data.items():
                rows += rsimulate(name, s, df, start, end)
    tr = pd.DataFrame(rows)
    tr["benchmark_pct"] = tr.stock.map(drift) * tr.days * 100
    tr["excess_pct"] = tr.net_pct_before_dp - tr.benchmark_pct
    tr["dp_today_pct"] = tr.stock.map(dp_today)
    tr["net_pct_after_dp_today"] = tr.net_pct_before_dp - tr.dp_today_pct
    tr["entry_date"] = pd.to_datetime(tr.entry_date)
    tr["sample"] = sample
    return tr


def tclust(g, col):
    """t of the mean with trades summed by ISO entry week."""
    if len(g) < 2:
        return np.nan
    iso = g.entry_date.dt.isocalendar()
    w = g[col].groupby([iso.year.values, iso.week.values]).sum()
    if len(w) < 2 or w.std(ddof=1) == 0:
        return np.nan
    return w.mean() / (w.std(ddof=1) / np.sqrt(len(w)))


def rtable(tr, periods):
    hdr = (f"{'rule':6s} {'N':>5s} {'win%':>5s} {'meanB':>6s} {'tcl':>5s} {'A_tod':>6s} {'days':>5s} "
           f"{'bench':>6s} {'exc':>6s} {'tclX':>5s} {'pos':>5s} | "
           + " | ".join(f"{p[0]} B/exc(n)" for p in periods))  # fmt: skip
    lines = [hdr]
    for name in RSTRATS:
        g = tr[tr.strategy == name]
        if g.empty:
            lines.append(f"{name:6s} no trades")
            continue
        b = g.net_pct_before_dp
        pos = int((g.groupby("stock").net_pct_before_dp.sum() > 0).sum())
        per = []
        for _, lo, hi in periods:
            q = g[(g.entry_date >= lo) & (g.entry_date <= hi)]
            per.append(
                f"{q.net_pct_before_dp.mean():+.2f}/{q.excess_pct.mean():+.2f}({len(q)})"
                if len(q)
                else "-"
            )
        lines.append(f"{name:6s} {len(g):5d} {100*(b>0).mean():5.1f} {b.mean():+6.2f} "
                     f"{tclust(g,'net_pct_before_dp'):5.2f} {g.net_pct_after_dp_today.mean():+6.2f} "
                     f"{g.days.mean():5.1f} {g.benchmark_pct.mean():+6.2f} {g.excess_pct.mean():+6.2f} "
                     f"{tclust(g,'excess_pct'):5.2f} {pos:2d}/18 | " + " | ".join(per))  # fmt: skip
    return lines


def r1_slow(df, start, end):
    """R1 (RSI2<10, close>SMA200; exit close>SMA5 or 10 sessions) as a plain loop, own indicators."""
    cfg = h._cost_cfg.product("equity_delivery")
    b, tx = cfg.brokerage, cfg.taxes

    def charges(turn, sell):
        brok = min(float(b.per_order_inr), float(b.pct_of_turnover) * turn, float(b.cap_inr))
        fees = (
            float(tx.exchange_txn_pct) + float(tx.sebi_turnover_pct) + float(tx.ipft_pct)
        ) * turn
        x = brok + fees + float(tx.gst_pct) * (brok + fees)
        x += (float(tx.stt_sell_pct) if sell else float(tx.stt_buy_pct)) * turn
        if not sell:
            x += float(tx.stamp_duty_pct) * turn
        return x

    O, C, D = list(df.open), list(df.close), list(df.index)
    n = len(C)
    # Wilder RSI(2), seeded with the mean of the first two changes
    rsi = [None] * n
    au = ad = None
    for i in range(1, n):
        ch = C[i] - C[i - 1]
        u, dn = max(ch, 0.0), max(-ch, 0.0)
        if i == 2:
            au = (max(C[1] - C[0], 0.0) + u) / 2
            ad = (max(C[0] - C[1], 0.0) + dn) / 2
        elif i > 2:
            au = au + (u - au) / 2
            ad = ad + (dn - ad) / 2
        if au is not None:
            rsi[i] = (
                50.0 if (au == 0 and ad == 0) else (100.0 if ad == 0 else 100 - 100 / (1 + au / ad))
            )

    def avg(i, k):
        return sum(C[i - k + 1 : i + 1]) / k if i >= k - 1 else None

    out, k = [], None
    for t in range(n):
        if k is not None and t >= k:
            s5 = avg(t, 5)
            if ((s5 is not None and C[t] > s5) or t - k + 1 >= 10) and t + 1 < n:
                ef, xf = O[k] * (1 + SLIP), O[t + 1] * (1 - SLIP)
                out.append(
                    (
                        D[k].date(),
                        D[t + 1].date(),
                        (xf - ef - charges(ef, False) - charges(xf, True)) / ef * 100,
                    )
                )
                k = None
                continue
        if k is None and t + 1 < n and rsi[t] is not None and rsi[t] < 10:
            s200 = avg(t, 200)
            if s200 is not None and C[t] > s200 and start <= D[t + 1] <= end:
                k = t + 1
    if k is not None:
        ef, xf = O[k] * (1 + SLIP), C[-1] * (1 - SLIP)
        out.append(
            (
                D[k].date(),
                D[-1].date(),
                (xf - ef - charges(ef, False) - charges(xf, True)) / ef * 100,
            )
        )
    return out


def old_all18(store):
    """Yahoo bars for all 18 stocks rescaled to the store; only the overlap ratio test applies."""
    fetch_old()
    old, nifty = load_old()
    keep, lines = {}, []
    for s in h.STOCKS:
        y, st = old[s], store[s]
        j = pd.concat([y.close.rename("y"), st.close.rename("s")], axis=1, join="inner")
        j = j[(j.index >= "2021-11-08") & (j.index <= "2022-03-31")]
        ratio = j.y / j.s
        spread = ratio.max() / ratio.min() - 1
        if len(j) > 50 and spread <= 0.01:
            k = 1 / ratio.median()
            y = y.copy()
            y[["open", "high", "low", "close"]] *= k
            y["volume"] = y.volume / k
            keep[s] = y
        else:
            lines.append(f"{s} dropped: ratio spread {spread:.2%}")
    return keep, lines


def reversion():
    store, _ = load_store()
    dp_today = {s: DP / d.close.iat[-1] * 100 for s, d in store.items()}
    drift_new = {}
    for s, d in store.items():
        w = d[(d.index >= S1_START) & (d.index <= S1_END)]
        drift_new[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
    new = run_reversion(store, S1_START, S1_END, drift_new, dp_today, "2021-2026")
    old, drop_lines = old_all18(store)
    drift_old = {}
    for s, d in old.items():
        w = d[(d.index >= S2_START) & (d.index <= S2_END)]
        drift_old[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
    oldtr = run_reversion(old, S2_START, S2_END, drift_old, dp_today, "2011-2021")

    note = [
        "B = mean % a trade before DP; tcl = t clustered by ISO entry week; A_tod = mean after DP "
        "at today's price; exc = B - benchmark; pos = stocks with positive total B"
    ]
    dpl = "DP% today (14.75 / last store close): " + " ".join(
        f"{s} {v:.3f}" for s, v in dp_today.items()
    )
    L = (
        ["REVERSION family (PLAN A2), store daily bars, entries 2021-11-08..2026-11-07", dpl]
        + note
        + [""]
    )
    L += rtable(new, S1_PERIODS)
    # candidate rule
    L += [
        "",
        "candidate rule (before DP): mean>0 and excess>0 in OLD and in TUNE+CHOOSE; OLD tclX>=2; "
        "OLD pos>=12",
    ]
    for name in RSTRATS:
        o = oldtr[oldtr.strategy == name]
        tc = new[(new.strategy == name) & (new.entry_date <= "2025-11-07")]
        chk = {"OLD mean>0": o.net_pct_before_dp.mean() > 0, "OLD exc>0": o.excess_pct.mean() > 0,
               "T+C mean>0": tc.net_pct_before_dp.mean() > 0, "T+C exc>0": tc.excess_pct.mean() > 0,
               "OLD tclX>=2": tclust(o, "excess_pct") >= 2,
               "OLD pos>=12": (o.groupby("stock").net_pct_before_dp.sum() > 0).sum() >= 12}  # fmt: skip
        f = [k for k, v in chk.items() if not v]
        L.append(
            f"  {name:6s} T+C B {tc.net_pct_before_dp.mean():+.2f} exc {tc.excess_pct.mean():+.2f} "
            f"tclX {tclust(tc, 'excess_pct'):5.2f} -> "
            + ("CANDIDATE" if not f else "fails: " + ", ".join(f))
        )
    # validation
    L += ["", "VALIDATION"]
    bad = 0
    for s in list(store)[:4]:
        df = store[s]
        for cut in (len(df) // 2, len(df) - 300, len(df) - 37):
            for name in RSTRATS:
                if name.startswith("R6"):
                    continue
                ef, xf = rsignals(name, df)
                et, xt = rsignals(name, df.iloc[:cut])
                bad += not np.array_equal(ef[:cut], et[:cut])
                bad += not all(xf(t, t - 3) == xt(t, t - 3) for t in range(cut - 200, cut))
    L.append(f"1. truncation test (4 stocks x 3 cuts x 14 per-stock rules): mismatches = {bad}")
    for lab, data, tr, st, en in (
        ("2021-2026", store, new, S1_START, S1_END),
        ("2011-2021", old, oldtr, S2_START, S2_END),
    ):
        fast = tr[tr.strategy == "R1_10"].sort_values(["stock", "entry_date"])
        slow = []
        for s, d in data.items():
            slow += [(s, *x) for x in r1_slow(d, st, en)]
        sl = pd.DataFrame(slow, columns=["stock", "entry", "exit", "net"]).sort_values(
            ["stock", "entry"]
        )
        ok = (len(sl) == len(fast) and (sl.entry.values == fast.entry_date.dt.date.values).all()
              and (sl.exit.values == fast.exit_date.values).all()
              and np.allclose(sl.net.values, fast.net_pct_before_dp.values, atol=1e-9, rtol=0))  # fmt: skip
        L.append(f"2. R1 slow loop {lab}: {len(sl)} vs {len(fast)} trades, identical = {ok}")
    L.append("3. sample R1 trades (2021-2026):")
    for _, r in new[new.strategy == "R1_10"].sample(3, random_state=11).iterrows():
        L.append(
            f"   {r.stock} sig {r.signal_date} buy {r.entry_date.date()} @ {r.entry_price:.4f} sell "
            f"{r.exit_date} @ {r.exit_price:.4f} charges Rs {r.charges:.4f} net {r.net_pct_before_dp:+.4f}%"
        )
    L.append(f"open-at-end trades: {int(new.open_at_end.sum())}")
    (OUT / "stocks_reversion_new.txt").write_text("\n".join(L) + "\n")
    print("\n".join(L))

    M = (
        [
            "REVERSION family (PLAN A2), Yahoo daily, all 18 stocks (overlap ratio test only), entries "
            "2011-01-01..2021-11-07, benchmark drift on that window",
            dpl,
        ]
        + drop_lines
        + note
        + [""]
    )
    M += rtable(oldtr, S2_PERIODS)
    r1 = oldtr[oldtr.strategy == "R1_10"].net_pct_before_dp
    q = np.percentile(r1, [5, 25, 50, 75, 95])
    M += [
        "",
        "R1 old sample before DP: p5 {:+.2f} p25 {:+.2f} p50 {:+.2f} p75 {:+.2f} p95 {:+.2f} worst {:+.2f}".format(
            *q, r1.min()
        ),
    ]
    both = pd.concat([oldtr, new])
    b1 = both[both.strategy == "R1_10"]
    yr = b1.groupby(b1.entry_date.dt.year).net_pct_before_dp.agg(["mean", "size"])
    M.append(
        "R1 mean B by entry year (n): "
        + " ".join(f"{y} {m:+.2f}({int(n)})" for y, (m, n) in yr.iterrows())
    )
    yrs_old = (S2_END - S2_START).days / 365.25
    M.append(
        f"R1 trades per stock per year: old {len(r1) / 18 / yrs_old:.2f}, "
        f"new {len(new[new.strategy == 'R1_10']) / 18 / 5:.2f}"
    )
    M.append(f"open-at-end trades (closed at the 2022-03-31 close): {int(oldtr.open_at_end.sum())}")
    (OUT / "stocks_reversion_old.txt").write_text("\n".join(M) + "\n")
    print("\n".join(M))
    cols = ["sample", "strategy", "stock", "signal_date", "entry_date", "entry_price", "exit_date",
            "exit_price", "days", "charges", "net_pct_before_dp", "net_pct_after_dp_today",
            "benchmark_pct", "excess_pct", "open_at_end"]  # fmt: skip
    out = both[cols].copy()
    out["entry_date"] = out.entry_date.dt.date
    out.to_csv(OUT / "stocks_trades_reversion.csv", index=False, float_format="%.6f")


# ═════════════════════════════ family "mirror" (PLAN.md section A3) ═════════════════════════════

MSTRATS = ["C_55_20", "C_20_10", "C_100_50", "C_55_20_t120", "C_55_20_mkt", "C_55_20_sl15"]


def mirror(name, stock, df, nifty, start, end, with_s1=False):
    """C is long exactly when S1 is flat, from S1's first exit on.

    S1 runs as in `simulate` (entries from `start` on) but WITHOUT the end bound, so that C's exit
    (S1's next entry signal) is still seen after the entry window; C entries are bounded by `end`.
    """
    parts = name.split("_")
    n_, m_ = int(parts[1]), int(parts[2])
    var = parts[3] if len(parts) > 3 else ""
    ent, ex_fn = signals(f"S1_{n_}_{m_}", df, nifty)
    mkt = nifty_ok(df, nifty) if var == "mkt" else None
    dates, n = df.index, len(df)
    lc_open = df.open.to_numpy()
    cl = df.close.to_numpy()
    s1, c, out, s1_out = None, None, [], []
    for t in range(n):
        if s1 is not None:
            if t >= s1 and ex_fn(t, s1) and t + 1 < n:
                s1_out.append(make_trade("S1", stock, df, s1 - 1, s1, t + 1))
                s1 = None
                if start <= dates[t + 1] <= end and (mkt is None or mkt[t]):
                    c = t + 1
            continue
        if ent[t] and t + 1 < n and dates[t + 1] >= start:
            s1 = t + 1
            if c is not None:
                out.append(make_trade(name, stock, df, c - 1, c, t + 1))
                c = None
            continue
        if c is not None and t >= c and t + 1 < n:
            ef = lc_open[c] * (1 + SLIP)
            if (var == "t120" and t - c + 1 >= 120) or (var == "sl15" and cl[t] <= 0.85 * ef):
                out.append(make_trade(name, stock, df, c - 1, c, t + 1))
                c = None
    if c is not None:
        out.append(make_trade(name, stock, df, c - 1, c, n - 1, at_close=True))
    if s1 is not None:
        s1_out.append(make_trade("S1", stock, df, s1 - 1, s1, n - 1, at_close=True))
    return (out, s1_out) if with_s1 else out


def plain_charges(turn, sell):
    cfg = h._cost_cfg.product("equity_delivery")
    b, tx = cfg.brokerage, cfg.taxes
    brok = min(float(b.per_order_inr), float(b.pct_of_turnover) * turn, float(b.cap_inr))
    fees = (float(tx.exchange_txn_pct) + float(tx.sebi_turnover_pct) + float(tx.ipft_pct)) * turn
    x = brok + fees + float(tx.gst_pct) * (brok + fees)
    x += (float(tx.stt_sell_pct) if sell else float(tx.stt_buy_pct)) * turn
    if not sell:
        x += float(tx.stamp_duty_pct) * turn
    return x


def c_slow(df, start, end, N=55, M=20):
    """C_55_20 as a plain loop: a flag for 'S1 long', a flag for 'C long', windows by slicing."""
    O, C, D = list(df.open), list(df.close), list(df.index)
    n = len(C)
    s1_long, c_long, c_in, res = False, False, None, []
    s1_from = None
    for t in range(n - 1):
        if s1_long:
            if t >= s1_from and t >= M and C[t] < min(C[t - M : t]):
                s1_long = False
                if start <= D[t + 1] <= end:
                    c_long, c_in = True, t + 1
        elif t >= N and C[t] > max(C[t - N : t]) and D[t + 1] >= start:
            s1_long, s1_from = True, t + 1
            if c_long:
                ef, xf = O[c_in] * (1 + SLIP), O[t + 1] * (1 - SLIP)
                res.append(
                    (
                        D[c_in].date(),
                        D[t + 1].date(),
                        (xf - ef - plain_charges(ef, False) - plain_charges(xf, True)) / ef * 100,
                    )
                )
                c_long = False
    if c_long:
        ef, xf = O[c_in] * (1 + SLIP), C[-1] * (1 - SLIP)
        res.append(
            (
                D[c_in].date(),
                D[-1].date(),
                (xf - ef - plain_charges(ef, False) - plain_charges(xf, True)) / ef * 100,
            )
        )
    return res


def mtable(tr, periods, names):
    hdr = (f"{'rule':12s} {'N':>5s} {'win%':>5s} {'meanB':>6s} {'tcl':>5s} {'A_tod':>6s} {'days':>5s} "
           f"{'max':>4s} {'bench':>6s} {'exc':>6s} {'tclX':>5s} {'pos':>5s} | "
           + " | ".join(f"{p[0]} B/exc(n)" for p in periods))  # fmt: skip
    lines = [hdr]
    for name in names:
        g = tr[tr.strategy == name]
        b = g.net_pct_before_dp
        pos = int((g.groupby("stock").net_pct_before_dp.sum() > 0).sum())
        per = []
        for _, lo, hi in periods:
            q = g[(g.entry_date >= lo) & (g.entry_date <= hi)]
            per.append(
                f"{q.net_pct_before_dp.mean():+.2f}/{q.excess_pct.mean():+.2f}({len(q)})"
                if len(q)
                else "-"
            )
        lines.append(f"{name:12s} {len(g):5d} {100*(b>0).mean():5.1f} {b.mean():+6.2f} "
                     f"{tclust(g,'net_pct_before_dp'):5.2f} {g.net_pct_after_dp_today.mean():+6.2f} "
                     f"{g.days.mean():5.1f} {g.days.max():4d} {g.benchmark_pct.mean():+6.2f} "
                     f"{g.excess_pct.mean():+6.2f} {tclust(g,'excess_pct'):5.2f} {pos:2d}/18 | "
                     + " | ".join(per))  # fmt: skip
    return lines


def mirror_sample(data, nifty, start, end, drift, dp_today, sample, last_close):
    rows = []
    for name in MSTRATS:
        for s, df in data.items():
            rows += mirror(name, s, df, nifty, start, end)
    tr = pd.DataFrame(rows)
    tr["benchmark_pct"] = tr.stock.map(drift) * tr.days * 100
    tr["excess_pct"] = tr.net_pct_before_dp - tr.benchmark_pct
    tr["dp_today_pct"] = tr.stock.map(dp_today)
    tr["net_pct_after_dp_today"] = tr.net_pct_before_dp - tr.dp_today_pct
    tr["net_inr_after_dp_today"] = tr.net_inr_before_dp - tr.dp_today_pct / 100 * tr.entry_price
    tr["entry_date"] = pd.to_datetime(tr.entry_date)
    tr["exit_date"] = pd.to_datetime(tr.exit_date)
    tr["sample"] = sample
    # maximum adverse excursion on closes while held (entry day .. last close before the sale)
    mae = []
    for _, r in tr.iterrows():
        d = data[r.stock]
        seg = d.close[
            (d.index >= r.entry_date)
            & ((d.index < r.exit_date) | ((d.index == r.exit_date) & r.open_at_end))
        ]
        mae.append(seg.min() / r.entry_price * 100 - 100 if len(seg) else 0.0)
    tr["mae_pct"] = mae
    return tr


def c_details(tr, data, start, end):
    g = tr[tr.strategy == "C_55_20"].copy()
    b = g.net_pct_before_dp
    L = ["", "C_55_20 details (before DP unless said)"]
    q = np.percentile(b, [5, 25, 50, 75, 95])
    L.append("1. p5 {:+.2f} p25 {:+.2f} p50 {:+.2f} p75 {:+.2f} p95 {:+.2f}".format(*q))
    L.append(
        "   5 worst: "
        + "; ".join(
            f"{r.stock} {r.entry_date.date()}->{r.exit_date.date()} {r.days}d {r.net_pct_before_dp:+.1f}%"
            for _, r in g.nsmallest(5, "net_pct_before_dp").iterrows()
        )
    )
    L.append(
        f"   MAE on closes worse than -15%: {100 * (g.mae_pct < -15).mean():.1f}% of trades, "
        f"worse than -25%: {100 * (g.mae_pct < -25).mean():.1f}%"
    )
    yr = g.groupby(g.entry_date.dt.year).net_pct_before_dp.agg(["mean", "size"])
    L.append(
        "2. by entry year: " + " ".join(f"{y} {m:+.2f}({int(k)})" for y, (m, k) in yr.iterrows())
    )
    L.append("3. per stock: trades, mean B, mean after DP today")
    ps = g.groupby("stock").agg(
        n=("days", "size"), b=("net_pct_before_dp", "mean"), a=("net_pct_after_dp_today", "mean")
    )
    for s, r in ps.iterrows():
        L.append(f"   {s:11s} {int(r.n):3d} {r.b:+7.2f} {r.a:+7.2f}")
    L.append(
        f"   stocks positive after DP today: {int((ps.a > 0).sum())}/18; negative: "
        + ", ".join(ps.index[ps.a <= 0])
    )
    cal = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    cal = cal[(cal >= start) & (cal <= max(end, g.exit_date.max()))]
    cnt = np.zeros(len(cal), int)
    for _, r in g.iterrows():
        cnt += (cal >= r.entry_date) & (
            (cal < r.exit_date) | ((cal == r.exit_date) & r.open_at_end)
        )
    L.append(
        f"4. trades open at once: max {cnt.max()}, mean {cnt.mean():.2f} (sessions {cal[0].date()}..{cal[-1].date()})"
    )
    ey = g.groupby(g.exit_date.dt.year).net_inr_after_dp_today.sum()
    L.append(
        "   Rs after DP today by exit year: " + " ".join(f"{y} {v:+.0f}" for y, v in ey.items())
    )
    return L


def overlap_check(data, nifty, start, end):
    """S1_55_20 days + C_55_20 days per stock == sessions from S1's first entry to the last bar."""
    bad = []
    for s, df in data.items():
        c, s1 = mirror("C_55_20", s, df, nifty, start, pd.Timestamp("2100-01-01"), with_s1=True)
        if not s1:
            continue
        first = df.index.get_loc(pd.Timestamp(s1[0]["entry_date"]))
        tot = sum(x["days"] for x in s1) + sum(x["days"] for x in c)
        if tot != len(df) - 1 - first:
            bad.append(f"{s} {tot} vs {len(df) - 1 - first}")
        ivs = sorted([(x["entry_date"], x["exit_date"]) for x in s1 + c])
        if any(ivs[i][1] != ivs[i + 1][0] for i in range(len(ivs) - 1)):
            bad.append(f"{s} gap/overlap")
    return bad


def mirror_family():
    store, nifty_new = load_store()
    dp_today = {s: DP / d.close.iat[-1] * 100 for s, d in store.items()}
    last_close = {s: d.close.iat[-1] for s, d in store.items()}
    old, drop_lines = old_all18(store)
    _, nifty_old = load_old()
    dpl = "DP% today (14.75 / last store close): " + " ".join(
        f"{s} {v:.3f}" for s, v in dp_today.items()
    )
    note = (
        "B = mean % before DP; tcl = t clustered by ISO entry week; A_tod = after DP at today's price; "
        "max = longest trade in sessions; exc = B - benchmark; pos = stocks with positive total B"
    )
    alltr = []
    for lab, data, nifty, st, en, periods, fn in (
        ("2021-2026", store, nifty_new, S1_START, S1_END, S1_PERIODS, "stocks_mirror_new.txt"),
        ("2011-2021", old, nifty_old, S2_START, S2_END, S2_PERIODS, "stocks_mirror_old.txt"),
    ):
        drift = {}
        for s, d in data.items():
            w = d[(d.index >= st) & (d.index <= en)]
            drift[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
        tr = mirror_sample(data, nifty, st, en, drift, dp_today, lab, last_close)
        alltr.append(tr)
        L = (
            [
                f"MIRROR family (PLAN A3), {lab}, entries {st.date()}..{en.date()}, "
                f"{len(data)} stocks",
                dpl,
            ]
            + drop_lines
            + [note, ""]
        )
        L += mtable(tr, periods, MSTRATS)
        L += c_details(tr, data, st, en)
        L += ["", "VALIDATION"]
        fast = tr[tr.strategy == "C_55_20"].sort_values(["stock", "entry_date"])
        slow = []
        for s, d in data.items():
            slow += [(s, *x) for x in c_slow(d, st, en)]
        sl = pd.DataFrame(slow, columns=["stock", "entry", "exit", "net"]).sort_values(
            ["stock", "entry"]
        )
        ok = (len(sl) == len(fast) and (sl.entry.values == fast.entry_date.dt.date.values).all()
              and (sl.exit.values == fast.exit_date.dt.date.values).all()
              and np.allclose(sl.net.values, fast.net_pct_before_dp.values, atol=1e-9, rtol=0))  # fmt: skip
        L.append(f"plain-loop C_55_20: {len(sl)} vs {len(fast)} trades, identical = {ok}")
        bad = overlap_check(data, nifty, st, en)
        L.append(
            "S1+C no-overlap/no-gap check (S1 and C without the end bound): "
            + ("all 18 stocks OK" if not bad else "MISMATCH " + "; ".join(bad))
        )
        L.append("sample C_55_20 trades:")
        for _, r in fast.sample(3, random_state=5).iterrows():
            L.append(
                f"   {r.stock} S1-exit sig {r.signal_date} buy {r.entry_date.date()} @ {r.entry_price:.4f} "
                f"sell {r.exit_date.date()} @ {r.exit_price:.4f} ({r.days}d) charges Rs {r.charges:.4f} "
                f"net {r.net_pct_before_dp:+.4f}%"
            )
        L.append(f"open-at-end trades (closed at the last close): {int(tr.open_at_end.sum())}")
        (OUT / fn).write_text("\n".join(L) + "\n")
        print("\n".join(L))
    new, oldtr = alltr
    print("\ncandidate rule (before DP): OLD and T+C each mean>0, exc>0, tclX>=2; OLD pos>=12")
    for name in MSTRATS:
        o = oldtr[oldtr.strategy == name]
        tc = new[(new.strategy == name) & (new.entry_date <= "2025-11-07")]
        chk = {"OLD mean>0": o.net_pct_before_dp.mean() > 0, "OLD exc>0": o.excess_pct.mean() > 0,
               "OLD tclX>=2": tclust(o, "excess_pct") >= 2,
               "OLD pos>=12": (o.groupby("stock").net_pct_before_dp.sum() > 0).sum() >= 12,
               "T+C mean>0": tc.net_pct_before_dp.mean() > 0, "T+C exc>0": tc.excess_pct.mean() > 0,
               "T+C tclX>=2": tclust(tc, "excess_pct") >= 2}  # fmt: skip
        f = [k for k, v in chk.items() if not v]
        line = (
            f"  {name:12s} T+C B {tc.net_pct_before_dp.mean():+.2f} exc {tc.excess_pct.mean():+.2f} "
            f"tclX {tclust(tc, 'excess_pct'):5.2f} -> "
            + ("CANDIDATE" if not f else "fails: " + ", ".join(f))
        )
        print(line)
        with open(OUT / "stocks_mirror_new.txt", "a") as fh:
            fh.write(line + "\n")
    both = pd.concat(alltr)
    cols = ["sample", "strategy", "stock", "signal_date", "entry_date", "entry_price", "exit_date",
            "exit_price", "days", "charges", "net_pct_before_dp", "net_pct_after_dp_today",
            "benchmark_pct", "excess_pct", "mae_pct", "open_at_end"]  # fmt: skip
    out = both[cols].copy()
    out["entry_date"] = out.entry_date.dt.date
    out["exit_date"] = out.exit_date.dt.date
    out.to_csv(OUT / "stocks_trades_mirror.csv", index=False, float_format="%.6f")


# ═════════════════════════════ fair benchmarks and portfolio view ═════════════════════════════


def net_pct(o_in: np.ndarray, x_raw: np.ndarray) -> np.ndarray:
    """Net % of a one-share trade bought at o_in and sold at x_raw (slippage and delivery charges)."""
    lc = legcost()
    ef, xf = o_in * (1 + SLIP), x_raw * (1 - SLIP)
    return (xf - ef - lc.leg(ef, sell=False) - lc.leg(xf, sell=True)) / ef * 100


def fair_scores(tr, data, start, end):
    """Benchmark a (own stock, every start day, same length) and b (18-stock basket, same dates)."""
    cache_a = {}
    bench_a, bench_b = [], []
    arr = {s: (d.index, d.open.to_numpy(), d.close.to_numpy()) for s, d in data.items()}
    for r in tr.itertuples():
        idx, o, _ = arr[r.stock]
        key = (r.stock, int(r.days))
        if key not in cache_a:
            t = np.flatnonzero((idx >= start) & (idx <= end))
            t = t[t + r.days < len(o)]
            cache_a[key] = float(net_pct(o[t], o[t + r.days]).mean()) if len(t) else np.nan
        bench_a.append(cache_a[key])
        e, x = pd.Timestamp(r.entry_date), pd.Timestamp(r.exit_date)
        vals = []
        for _s, (ix, oo, cc) in arr.items():
            i = ix.searchsorted(e)
            j = min(ix.searchsorted(x), len(ix) - 1)
            if i >= len(ix) or j <= i:
                continue
            xr = cc[j] if r.open_at_end else oo[j]
            vals.append(float(net_pct(np.array([oo[i]]), np.array([xr]))[0]))
        bench_b.append(np.mean(vals))
    tr = tr.copy()
    tr["bench_a"], tr["bench_b"] = bench_a, bench_b
    tr["excA"] = tr.net_pct_before_dp - tr.bench_a
    tr["excB"] = tr.net_pct_before_dp - tr.bench_b
    return tr


def fair_table(tr, periods, names):
    L = [f"{'rule':12s} {'N':>5s} {'B':>6s} {'days':>6s} {'excA':>6s} {'t':>5s} {'excB':>6s} {'t':>5s} | "
         + " | ".join(f"{p[0]} excB(n)" for p in periods)]  # fmt: skip
    for name in names:
        g = tr[tr.strategy == name]
        if g.empty:
            continue
        per = []
        for _, lo, hi in periods:
            q = g[(g.entry_date >= lo) & (g.entry_date <= hi)]
            per.append(f"{q.excB.mean():+.2f}({len(q)})" if len(q) else "-")
        L.append(f"{name:12s} {len(g):5d} {g.net_pct_before_dp.mean():+6.2f} {g.days.mean():6.1f} "
                 f"{g.excA.mean():+6.2f} {tclust(g, 'excA'):5.2f} {g.excB.mean():+6.2f} "
                 f"{tclust(g, 'excB'):5.2f} | " + " | ".join(per))  # fmt: skip
    return L


PORT_RULES = ["S1_55_20", "S2", "S3_12_1", "R4_8", "C_55_20", "C_100_50"]


def portfolio(tr, data, w0, w1):
    """1/18 slice per stock, earning the stock's return only while the rule holds it."""
    lc = legcost()
    cal = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    cal = cal[(cal >= w0) & (cal <= w1)]
    eq = pd.Series(0.0, index=cal)
    held_days = 0
    for s, d in data.items():
        d = d[(d.index >= w0) & (d.index <= w1)]
        o, c = d.open.to_numpy(), d.close.to_numpy()
        f = np.ones(len(d))
        inv = np.zeros(len(d), bool)
        for r in tr[tr.stock == s].itertuples():
            e = d.index.searchsorted(pd.Timestamp(r.entry_date))
            if e >= len(d):
                continue
            x = d.index.searchsorted(pd.Timestamp(r.exit_date))
            ef = o[e] * (1 + SLIP)
            f[e] *= (1 - lc.leg(np.array([ef]), sell=False)[0] / ef) * c[e] / ef
            inv[e] = True
            closes_out = x < len(d) and not r.open_at_end
            last = x if closes_out else len(d)
            if last > e + 1:
                f[e + 1 : last] *= c[e + 1 : last] / c[e : last - 1]
                inv[e + 1 : last] = True
            if closes_out:
                xf = o[x] * (1 - SLIP)
                f[x] *= xf / c[x - 1] * (1 - lc.leg(np.array([xf]), sell=True)[0] / xf)
        held_days += int(inv.sum())
        v = pd.Series(np.cumprod(f) / 18, index=d.index).reindex(cal).ffill().fillna(1 / 18)
        eq += v
    return eq, held_days / (18 * len(cal))


def buy_hold(data, w0, w1):
    lc = legcost()
    cal = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    cal = cal[(cal >= w0) & (cal <= w1)]
    eq = pd.Series(0.0, index=cal)
    for d in data.values():
        d = d[(d.index >= w0) & (d.index <= w1)]
        ef = d.open.iat[0] * (1 + SLIP)
        k = 1 - lc.leg(np.array([ef]), sell=False)[0] / ef
        eq += (d.close / ef * k / 18).reindex(cal).ffill().fillna(1 / 18)
    return eq


def pf_stats(name, eq, share):
    r = eq.pct_change().fillna(eq.iat[0] - 1)
    tot = eq.iat[-1] - 1
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = (1 + tot) ** (1 / yrs) - 1
    vol = r.std(ddof=1) * np.sqrt(252)
    mdd = (eq / eq.cummax() - 1).min()
    yr = (1 + r).groupby(eq.index.year).prod() - 1
    return (f"{name:9s} tot {100*tot:+7.1f} ann {100*ann:+6.2f} vol {100*vol:5.2f} ret/vol "
            f"{ann/vol:5.2f} mdd {100*mdd:6.1f} inv {100*share:5.1f}% ann/inv {100*ann/share:+6.2f} | "
            + " ".join(f"{y % 100:02d}:{100*v:+.1f}" for y, v in yr.items()))  # fmt: skip


def fair_family():
    store, nifty_new = load_store()
    old, _ = old_all18(store)
    _, nifty_old = load_old()
    t1 = pd.read_csv(OUT / "stocks_trades_stage1.csv")
    t1["sample"] = "2021-2026"
    # the stage-2 trend CSV holds the strict 13-stock run; re-run trend on all 18 old stocks
    drift = {}
    for s, d in old.items():
        w = d[(d.index >= S2_START) & (d.index <= S2_END)]
        drift[s] = (w.close / w.close.shift(1) - 1).dropna().mean()
    t2 = run_all(old, nifty_old, S2_START, S2_END, drift)
    t2["sample"] = "2011-2021"
    cols = ["sample", "strategy", "stock", "entry_date", "exit_date", "days", "net_pct_before_dp",
            "open_at_end"]  # fmt: skip
    rv = pd.read_csv(OUT / "stocks_trades_reversion.csv")
    mi = pd.read_csv(OUT / "stocks_trades_mirror.csv")
    alltr = pd.concat([t1[cols], t2[cols], rv[cols], mi[cols]], ignore_index=True)
    alltr["entry_date"] = pd.to_datetime(alltr.entry_date)
    alltr["exit_date"] = pd.to_datetime(alltr.exit_date)
    alltr["open_at_end"] = alltr.open_at_end.astype(str) == "True"
    names = STRATS + RSTRATS + MSTRATS
    A = ["FAIR BENCHMARKS (before DP). excA = B - own stock same length (all start days in the sample);",
         "excB = B - 18-stock basket same dates; both with slippage and delivery charges; t clustered by "
         "entry week. Trend old sample re-run on all 18 stocks.", ""]  # fmt: skip
    P = ["PORTFOLIO VIEW: 1/18 per stock, earns while held, fills at opens with slippage and charges, "
         "no DP; positions open at the window end marked at the close, no exit charge. Buy & hold: "
         "1/18 each bought at the first open (with charges).", ""]  # fmt: skip
    for lab, data, st, en, periods in (
        ("2021-2026", store, S1_START, S1_END, S1_PERIODS),
        ("2011-2021", old, S2_START, S2_END, S2_PERIODS),
    ):
        tr = fair_scores(alltr[alltr["sample"] == lab], data, st, en)
        A += [f"== {lab}", *fair_table(tr, periods, names), ""]
        w1 = en if lab == "2021-2026" else S2_END
        bh = buy_hold(data, st, w1)
        P += [f"== {lab} (window {st.date()}..{w1.date()})", pf_stats("BUY&HOLD", bh, 1.0)]
        for name in PORT_RULES:
            eq, share = portfolio(tr[tr.strategy == name], data, st, w1)
            P.append(pf_stats(name, eq, share))
        P.append("")
    (OUT / "stocks_fair_benchmarks.txt").write_text("\n".join(A) + "\n")
    (OUT / "stocks_portfolio_view.txt").write_text("\n".join(P) + "\n")
    print("\n".join(A + P))


# ═════════════════════════════ family "universe" (PLAN.md section A4) ═════════════════════════════

UNI = OUT / "daily_universe"
LIST_B = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "BAJAJ-AUTO", "BAJAJFINSV", "BEL",
          "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM",
          "HCLTECH", "HINDALCO", "HINDUNILVR", "INDUSINDBK", "JSWSTEEL", "NESTLEIND", "NTPC", "ONGC",
          "POWERGRID", "SHRIRAMFIN", "TATACONSUM", "TECHM", "TITAN", "ULTRACEMCO", "WIPRO"]  # fmt: skip
LIST_C = ["ACC", "AMBUJACEM", "BHEL", "DLF", "GAIL", "JINDALSTEL", "PNB", "RELINFRA", "RPOWER",
          "SAIL", "SIEMENS", "TATAPOWER", "VEDL"]  # fmt: skip
U_START, U_END = pd.Timestamp("2011-01-01"), pd.Timestamp("2026-11-06")
U_PERIODS = [("11-14", "2011-01-01", "2014-12-31"), ("15-18", "2015-01-01", "2018-12-31"),
             ("19-21", "2019-01-01", "2021-12-31"), ("22-26", "2022-01-01", "2026-11-06")]  # fmt: skip
U_RULES = ["BASE", "FILTER", "FINAL", "FINAL_6", "FINAL_10"]


def fetch_universe() -> list[str]:
    import yfinance as yf

    UNI.mkdir(parents=True, exist_ok=True)
    syms = yahoo_symbols()
    want = {s: syms[s] for s in h.STOCKS} | {s: f"{s}.NS" for s in LIST_B + LIST_C}
    want["NIFTY"] = "^NSEI"
    notes = []
    for key, sym in want.items():
        f = UNI / f"{key}.csv"
        if f.exists():
            continue
        time.sleep(1.6)
        try:
            df = yf.download(sym, start="2010-01-01", end="2026-11-08", interval="1d",
                             auto_adjust=False, actions=False, progress=False, threads=False)  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{key} ({sym}): fetch error {type(exc).__name__}")
            continue
        if df is None or df.empty:
            notes.append(f"{key} ({sym}): no data")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"
        df.to_csv(f)
    return notes


def load_universe(keys):
    out, notes = {}, []
    for k in keys:
        f = UNI / f"{k}.csv"
        if not f.exists():
            notes.append(f"{k}: no file - skipped")
            continue
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        df = _naive_daily(df.dropna(subset=["open", "high", "low", "close"]))
        df = df[(df.open > 0) & (df.close > 0)]
        span = (df.index[-1] - df.index[0]).days / 365.25 if len(df) else 0
        if span < 8:
            notes.append(f"{k}: only {span:.1f} years of bars - skipped")
            continue
        out[k] = df
    return out, notes


def usim(rule, stock, df, nifty, start, end):
    depth = {"FINAL_6": 6, "FINAL_10": 10}.get(rule, 8)
    ent, exit_fn = rsignals(f"R4_{depth}", df)
    if rule != "BASE":
        ent = ent & nifty_ok(df, nifty)
    stop = rule.startswith("FINAL")
    o, c = df.open.to_numpy(), df.close.to_numpy()
    dates, n = df.index, len(df)
    trades, pos = [], None
    for t in range(n):
        if pos is not None and t >= pos and t + 1 < n:
            if exit_fn(t, pos) or (stop and c[t] <= 0.85 * o[pos] * (1 + SLIP)):
                trades.append(make_trade(rule, stock, df, pos - 1, pos, t + 1))
                pos = None
                continue
        if pos is None and ent[t] and t + 1 < n and start <= dates[t + 1] <= end:
            pos = t + 1
    if pos is not None:
        trades.append(make_trade(rule, stock, df, pos - 1, pos, n - 1, at_close=True))
    return trades


def bench_a(tr, data, start, end):
    cache, out = {}, []
    for r in tr.itertuples():
        key = (r.stock, int(r.days))
        if key not in cache:
            d = data[r.stock]
            o = d.open.to_numpy()
            t = np.flatnonzero((d.index >= start) & (d.index <= end))
            t = t[t + r.days < len(o)]
            cache[key] = float(net_pct(o[t], o[t + r.days]).mean()) if len(t) else np.nan
        out.append(cache[key])
    return np.array(out)


def utable(tr, n_run):
    L = [f"{'rule':9s} {'N':>5s} {'win%':>5s} {'mean':>6s} {'t':>5s} {'exc':>6s} {'t':>5s} {'days':>5s} "
         f"{'p5':>6s} {'worst':>6s} {'pos':>6s} | " + " | ".join(f"{p[0]} m/exc(n)" for p in U_PERIODS)]  # fmt: skip
    for rule in U_RULES:
        g = tr[tr.strategy == rule]
        if g.empty:
            L.append(f"{rule:9s} no trades")
            continue
        b = g.net_pct_before_dp
        pos = int((g.groupby("stock").net_pct_before_dp.sum() > 0).sum())
        per = []
        for _, lo, hi in U_PERIODS:
            q = g[(g.entry_date >= lo) & (g.entry_date <= hi)]
            per.append(
                f"{q.net_pct_before_dp.mean():+.2f}/{q.excess_pct.mean():+.2f}({len(q)})"
                if len(q)
                else "-"
            )
        L.append(f"{rule:9s} {len(g):5d} {100 * (b > 0).mean():5.1f} {b.mean():+6.2f} "
                 f"{tclust(g, 'net_pct_before_dp'):5.2f} {g.excess_pct.mean():+6.2f} "
                 f"{tclust(g, 'excess_pct'):5.2f} {g.days.mean():5.1f} {np.percentile(b, 5):+6.2f} "
                 f"{b.min():+6.1f} {pos:2d}/{n_run:<3d} | " + " | ".join(per))  # fmt: skip
    return L


def universe_family():
    fetch_notes = fetch_universe()
    nif, nn = load_universe(["NIFTY"])
    nifty = nif["NIFTY"]
    L = [
        "UNIVERSE test of the pullback rule (PLAN A4). Yahoo daily (split-adjusted, not dividend-"
        "adjusted), entries 2011-01-01..2026-11-06, no DP; mean = % a trade before DP; exc = mean - "
        "own stock same-length open-to-open hold (slippage + charges, start days in the window); "
        "t clustered by ISO entry week; trades whose entry..exit window holds a >35% close-to-close "
        "day are dropped.",
        f"NIFTY {nifty.index[0].date()}..{nifty.index[-1].date()}",
    ]
    L += [f"fetch: {x}" for x in fetch_notes + nn]
    alltr = []
    for lab, keys in (
        ("A (18 desk)", h.STOCKS),
        ("B (30 large)", LIST_B),
        ("C (13 ex-NIFTY)", LIST_C),
    ):
        data, notes = load_universe(keys)
        rows, jumps_l = [], []
        for s, d in data.items():
            for rule in U_RULES:
                rows += usim(rule, s, d, nifty, U_START, U_END)
        tr = pd.DataFrame(rows)
        tr["entry_date"] = pd.to_datetime(tr.entry_date)
        tr["exit_date"] = pd.to_datetime(tr.exit_date)
        bad_days = {}
        for s, d in data.items():
            r = d.close / d.close.shift(1) - 1
            bd = r[r.abs() > 0.35]
            bad_days[s] = list(bd.index)
            if len(bd):
                jumps_l.append(f"{s} " + ",".join(f"{i.date()} {v:+.0%}" for i, v in bd.items()))
        drop = np.array([any(r.entry_date <= x <= r.exit_date for x in bad_days[r.stock])
                         for r in tr.itertuples()])  # fmt: skip
        dropped = tr[drop].groupby("strategy").size().to_dict()
        tr = tr[~drop].copy()
        tr["benchmark_pct"] = bench_a(tr, data, U_START, U_END)
        tr["excess_pct"] = tr.net_pct_before_dp - tr.benchmark_pct
        tr["list"] = lab
        alltr.append(tr)
        L += [
            "",
            f"== LIST {lab}: {len(data)} of {len(keys)} stocks run"
            + (f"; {'; '.join(notes)}" if notes else ""),
        ]
        L.append("days >35%: " + ("; ".join(jumps_l) if jumps_l else "none"))
        L.append(
            "trades dropped for a >35% day: "
            + (", ".join(f"{k} {v}" for k, v in dropped.items()) or "none")
        )
        L += utable(tr, len(data))
        g = tr[tr.strategy == "FINAL"]
        yr = g.groupby(g.entry_date.dt.year).net_pct_before_dp.agg(["mean", "size"])
        L.append(
            "FINAL by entry year: "
            + " ".join(f"{y} {m:+.2f}({int(k)})" for y, (m, k) in yr.iterrows())
        )
        L.append("FINAL 3 worst: " + "; ".join(
            f"{r.stock} {r.entry_date.date()}->{r.exit_date.date()} {r.days}d {r.net_pct_before_dp:+.1f}%"
            for _, r in g.nsmallest(3, "net_pct_before_dp").iterrows()))  # fmt: skip
        if lab.startswith("C"):
            ps = g.groupby("stock").net_pct_before_dp.agg(["size", "mean"])
            L += [
                f"   FINAL {s:11s} {int(r['size']):3d} {r['mean']:+6.2f}" for s, r in ps.iterrows()
            ]
        if lab.startswith("A"):
            b = tr[(tr.strategy == "BASE") & (tr.entry_date >= S1_START)]
            L.append(
                f"check vs store: BASE (=R4_8) entries from 2021-11-08: {len(b)} trades, mean "
                f"{b.net_pct_before_dp.mean():+.2f}% (store run: 144, +1.10%)"
            )
    (OUT / "stocks_universe_test.txt").write_text("\n".join(L) + "\n")
    out = pd.concat(alltr)
    out.to_csv(OUT / "stocks_trades_universe.csv", index=False, float_format="%.6f")
    print("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, choices=[1, 2], default=1)
    ap.add_argument(
        "--family", choices=["trend", "reversion", "mirror", "fair", "universe"], default="trend"
    )
    a = ap.parse_args()
    if a.family == "universe":
        universe_family()
    elif a.family == "fair":
        fair_family()  # re-scores the saved trade lists, plus the portfolio view
    elif a.family == "mirror":
        mirror_family()  # runs both samples
    elif a.family == "reversion":
        reversion()  # runs both samples
    else:
        stage1() if a.stage == 1 else stage2()
