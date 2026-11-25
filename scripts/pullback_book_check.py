"""US-100 check: an independent re-derivation of the capped pullback book (research, read-only).

Written from the contract (`.agile/stories/US-100.md`) and the product's place counting
(`src/onelot/delivery/wide.py`) alone, apart from the builder's `scripts/pullback_book.py`.
pandas, numpy and sqlite3 only. Nothing is written but the cache and the text in the output
folder; the stores are opened with mode=ro and only NIFTY's daily bars are read from the live one.

    nice -n 10 .venv/bin/python scripts/pullback_book_check.py [--draws 500]

Definitions used (each read from the contract or from wide.py):
- A trade is active from the evening of its signal until the morning its exit fills (so it is
  active on evening d when signal_date <= d < exit_date). On the morning of d the exits fill
  first, then the entries; an entry the cash cannot pay is refused and its place is free again.
- On evening d the signals take the free places in the order given: PRODUCT is the desk 18
  first, then the deepest pullback (the close's fall below the highest close of the last 20
  sessions, the day's own close included), then the stock's name.
- Cash: Rs 1,00,000, never topped up. A fill costs the trade's `amount`; a sale returns
  `amount + net_inr`. The book is marked on the daily closes (shares x close).
- Entries of one morning are filled older signal first, then in the order (K = 5 depends on it:
  without it the book holds 1,346 trades and ends at Rs 65,423, with it 1,358 and Rs 57,936).
- For a sub-period the benchmark is the book's value at the period start plus the benchmark's
  gain since (the same rupees); B1 is compared on its own value path.
- B1 NIFTY held; B2 and B3 earn the previous close's market exposure of the book times the day's
  return of NIFTY, or of the equal-weight basket of the stocks with a bar on both days.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MAIN = Path("/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN")
SWING = MAIN / "data" / "research" / "swing_2026-11-24"
OUT = MAIN / "data" / "research" / "pullback_book_2026-11-25"
NIFTY_CSV = MAIN / "data" / "research" / "alpha_2026-11-12" / "daily_universe" / "NIFTY.csv"
LIVE_DB = MAIN / "data" / "onelot.db"

LAST = pd.Timestamp("2026-11-07")
CASH0 = 100_000.0
UNIT = 10_000.0
PERIODS = (
    ("TUNE", None, pd.Timestamp("2024-11-07")),
    ("CHOOSE", pd.Timestamp("2024-11-08"), pd.Timestamp("2025-11-07")),
    ("TEST", pd.Timestamp("2025-11-08"), pd.Timestamp("2026-11-07")),
)
# the five listed corporate actions of the study's bars (src/onelot/delivery/universe.py)
ACTIONS = {
    "ADANIENT": ["2015-06-03"],
    "JSL": ["2015-11-19"],
    "TATACHEM": ["2020-03-04"],
    "BRITANNIA": ["2010-03-08"],
    "TMPV": ["2011-01-03"],
}
DAYS_A_YEAR = 365.25


# ------------------------------------------------------------------------------------ data


def load_bars() -> dict[str, pd.DataFrame]:
    con = sqlite3.connect(f"file:{SWING / 'daily.sqlite'}?mode=ro", uri=True)
    d = pd.read_sql("SELECT * FROM daily ORDER BY symbol, date", con, parse_dates=["date"])
    out = {}
    for sym, g in d.groupby("symbol"):
        f = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        f = f[(f.open > 0) & (f.high > 0) & (f.low > 0) & (f.close > 0)]
        for day in ACTIONS.get(str(sym), ()):
            ts = pd.Timestamp(day)
            if ts not in f.index:
                continue
            i = f.index.get_loc(ts)
            if i == 0:
                continue
            k = f.close.iloc[i] / f.close.iloc[i - 1]
            f.iloc[:i, :4] = f.iloc[:i, :4].to_numpy() * k
            f.iloc[:i, 4] = f.iloc[:i, 4].to_numpy() / k
        out[str(sym)] = f[f.index <= LAST]
    return out


def load_nifty() -> pd.Series:
    uri = f"file:{LIVE_DB}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    rows = con.execute(
        "SELECT ts, close FROM bars WHERE series_key = 'NIFTY' AND resolution = '1d' ORDER BY ts"
    ).fetchall()
    con.close()
    ts = pd.to_datetime([r[0] for r in rows], utc=True).tz_convert("Asia/Kolkata")
    st = pd.Series([r[1] for r in rows], index=pd.DatetimeIndex(ts.date))
    st = st[~st.index.duplicated(keep="last")]
    y = pd.read_csv(NIFTY_CSV, index_col=0, parse_dates=True)["close"].dropna()
    y = y[y.index < st.index[0]]
    both = pd.concat([y, st]).sort_index()
    return both[both.index <= LAST]


# ---------------------------------------------------------------------------------- order keys


def order_features(trades: pd.DataFrame, bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Depth, turnover and calm of each trade's stock on the close of its signal day."""
    depth = np.full(len(trades), np.nan)
    turn = np.full(len(trades), np.nan)
    vol = np.full(len(trades), np.nan)
    for sym, idx in trades.groupby("stock").groups.items():
        f = bars[sym]
        c = f.close
        hi = c.rolling(20).max()
        dep = (hi - c) / hi * 100.0
        tov = (c * f.volume).rolling(20).median()
        sd = c.pct_change().rolling(20).std()
        sig = trades.loc[idx, "signal_date"]
        depth[trades.index.get_indexer(idx)] = dep.reindex(sig).to_numpy()
        turn[trades.index.get_indexer(idx)] = tov.reindex(sig).to_numpy()
        vol[trades.index.get_indexer(idx)] = sd.reindex(sig).to_numpy()
    return pd.DataFrame({"depth": depth, "turn": turn, "vol": vol}, index=trades.index)


# ------------------------------------------------------------------------------- the book


class Book:
    """The trades, the sessions and the closes the books are run on."""

    def __init__(self, trades: pd.DataFrame, bars: dict[str, pd.DataFrame], nifty: pd.Series):
        self.trades = trades.reset_index(drop=True)
        t = self.trades
        sess = sorted(set().union(*[set(f.index) for f in bars.values()]))
        sess = [s for s in sess if s >= t.signal_date.min()]
        self.sessions = pd.DatetimeIndex(sess)
        n = len(self.sessions)
        self.n = n
        self.syms = sorted(bars)
        cm = np.full((n, len(self.syms)), np.nan)
        for j, s in enumerate(self.syms):
            cm[:, j] = bars[s].close.reindex(self.sessions).to_numpy()
        self.close_raw = cm
        self.close_ff = pd.DataFrame(cm).ffill().to_numpy()
        self.col = {s: j for j, s in enumerate(self.syms)}
        pos = lambda d: int(self.sessions.get_loc(d))  # noqa: E731
        self.sig = np.array([pos(d) for d in t.signal_date])
        self.ent = np.array([pos(d) for d in t.entry_date])
        ex = np.array([pos(d) for d in t.exit_date])
        self.open_end = t.open_at_end.to_numpy(bool)
        self.ex = np.where(self.open_end, n + 1, ex)  # an open trade never frees its place
        self.qty = t.qty.to_numpy(float)
        self.amount = t.amount.to_numpy(float)
        self.net_inr = t.net_inr.to_numpy(float)
        self.net_pct = t.net_pct.to_numpy(float)
        self.stk = np.array([self.col[s] for s in t.stock])
        # 3 trades enter 2 sessions on: the stock had no bar on the session after its signal
        # value of a held trade on each session of its life, for the marks
        self.unreal = []
        for i in range(len(t)):
            e, x = self.ent[i], min(self.ex[i], n)
            self.unreal.append(self.qty[i] * self.close_ff[e:x, self.stk[i]])
        self.nifty = nifty.reindex(self.sessions).ffill().to_numpy(float)
        # the equal-weight basket's daily return: stocks with a bar on both days
        r = self.close_raw[1:] / self.close_raw[:-1] - 1.0
        br = np.zeros(n)
        br[1:] = np.nanmean(np.where(np.isfinite(r), r, np.nan), axis=1)
        self.basket_ret = np.nan_to_num(br)
        nr = np.zeros(n)
        nr[1:] = self.nifty[1:] / self.nifty[:-1] - 1.0
        self.nifty_ret = nr
        # week of entry for the clustered t
        self.week = t.entry_date.dt.to_period("W").astype(str).to_numpy()
        # sessions by signal
        order = np.argsort(self.sig, kind="stable")
        self.by_sig = {}
        for i in order:
            self.by_sig.setdefault(int(self.sig[i]), []).append(int(i))
        self.entries_on = {}
        self.exits_on = {}
        for i in range(len(t)):
            self.entries_on.setdefault(int(self.ent[i]), []).append(i)
            if self.ex[i] <= n:
                self.exits_on.setdefault(int(self.ex[i]), []).append(i)

    def run(
        self,
        rank: np.ndarray,
        k: int | None,
        cash0: float = CASH0,
        cash_rule: bool = True,
        older_first: bool = True,
    ):
        """Run the book. `rank` is a float per trade (lower is taken first)."""
        n = self.n
        held = np.zeros(len(self.trades), bool)
        cap_refused = np.zeros(len(self.trades), bool)
        cash_refused = np.zeros(len(self.trades), bool)
        placed = np.zeros(len(self.trades), bool)
        active = 0
        cash = cash0
        use = np.zeros(n)
        places = np.zeros(n, dtype=int)
        signals = np.zeros(n, dtype=int)
        full = np.zeros(n, bool)
        cap_hit = np.zeros(n, bool)
        for d in range(n):
            # the morning: exits fill, then entries
            for i in self.exits_on.get(d, ()):
                if placed[i] and held[i]:
                    cash += self.amount[i] + self.net_inr[i]
                    active -= 1
                elif placed[i]:
                    raise AssertionError("a placed trade exits without an entry")
            for i in sorted(
                self.entries_on.get(d, ()),
                key=lambda j: (self.sig[j], rank[j]) if older_first else (0, rank[j]),
            ):
                if not placed[i]:
                    continue
                if cash_rule and cash + 1e-9 < self.amount[i]:
                    cash_refused[i] = True
                    active -= 1
                    placed[i] = False
                    continue
                cash -= self.amount[i]
                held[i] = True
            # the evening: signals take the free places
            sg = sorted(self.by_sig.get(d, ()), key=lambda j: rank[j])
            signals[d] = len(sg)
            free = 10**9 if k is None else k - active
            if sg:
                cap_hit[d] = len(sg) > max(free, 0)
            for i in sg:
                if free > 0:
                    placed[i] = True
                    active += 1
                    free -= 1
                else:
                    cap_refused[i] = True
            places[d] = active
            full[d] = (k is not None) and active >= k
            use[d] = cash
        return {
            "held": held,
            "cap_refused": cap_refused,
            "cash_refused": cash_refused,
            "places": places,
            "signals": signals,
            "full": full,
            "cap_hit": cap_hit,
            "cash0": cash0,
        }

    def equity(self, res: dict) -> tuple[np.ndarray, np.ndarray]:
        """Equity on each session's close, and the market value of the open positions."""
        n = self.n
        held = np.flatnonzero(res["held"])
        value = np.zeros(n)
        realised = np.zeros(n + 1)
        paid = np.zeros(n + 1)  # cash paid for open shares: value less cost
        for i in held:
            e, x = self.ent[i], min(self.ex[i], n)
            value[e:x] += self.unreal[i]
            paid[e] += self.amount[i]
            if self.ex[i] <= n:
                realised[self.ex[i]] += self.net_inr[i]
                paid[self.ex[i]] -= self.amount[i]
        eq = res["cash0"] + np.cumsum(realised[:n]) + value - np.cumsum(paid[:n])
        return eq, value

    def benchmarks(self, expo: np.ndarray, cash0: float = CASH0) -> dict[str, np.ndarray]:
        """B1, B2, B3 as value series, from the book's exposure on each close."""
        prev = np.concatenate([[0.0], expo[:-1]])
        b1 = cash0 * self.nifty / self.nifty[0]
        b2 = cash0 + np.cumsum(prev * self.nifty_ret)
        b3 = cash0 + np.cumsum(prev * self.basket_ret)
        return {"B1": b1, "B2": b2, "B3": b3}


# ----------------------------------------------------------------------------- statistics


def clustered(x: np.ndarray, groups: np.ndarray) -> tuple[float, float, float]:
    """Mean, its standard error with one cluster per group, and t."""
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else np.nan), np.nan, np.nan
    m = x.mean()
    df = pd.DataFrame({"r": x - m, "g": groups}).groupby("g").r.sum()
    se = np.sqrt((df**2).sum()) / n
    return float(m), float(se), float(m / se) if se > 0 else np.nan


def tweek(x: np.ndarray, entry_dates: pd.Series) -> float:
    """t of the mean weekly sum: the trades of one ISO entry week are ONE draw (the sprint's
    wording of RESEARCH-RULES section 4), the weeks with a trade being the draws."""
    if len(x) < 2:
        return np.nan
    iso = pd.DatetimeIndex(entry_dates).isocalendar()
    w = pd.Series(np.asarray(x, float)).groupby([iso.year.values, iso.week.values]).sum()
    if len(w) < 2 or w.std(ddof=1) == 0:
        return np.nan
    return float(w.mean() / (w.std(ddof=1) / np.sqrt(len(w))))


def growth(v0: float, v1: float, d0: pd.Timestamp, d1: pd.Timestamp) -> float:
    yrs = (d1 - d0).days / DAYS_A_YEAR
    return (v1 / v0) ** (1.0 / yrs) - 1.0 if yrs > 0 and v1 > 0 else np.nan


def period_bounds(book: Book):
    """(name, first index, last index) of each period and the whole span, on the sessions."""
    s = book.sessions
    out = []
    for name, a, b in PERIODS:
        lo = 0 if a is None else int(s.searchsorted(a))
        hi = int(s.searchsorted(b, side="right")) - 1
        out.append((name, lo, hi))
    out.append(("ALL", 0, book.n - 1))
    return out


def seg_growth(book: Book, v: np.ndarray, lo: int, hi: int, cash0: float = CASH0) -> float:
    """Yearly growth over sessions lo..hi (the base is the close before lo, or the start)."""
    s = book.sessions
    if lo == 0:
        v0, d0 = cash0, s[0]
    else:
        v0, d0 = v[lo - 1], s[lo - 1]
    return growth(v0, v[hi], d0, s[hi])


def drawdown(v: np.ndarray, cash0: float = CASH0) -> float:
    peak = np.maximum.accumulate(np.concatenate([[cash0], v]))[1:]
    return float((v / peak - 1.0).min())


def worst_year(book: Book, v: np.ndarray, cash0: float = CASH0) -> tuple[int, float]:
    y = book.sessions.year.to_numpy()
    worst = (0, 9.0)
    prev = cash0
    for yr in np.unique(y):
        last = v[np.flatnonzero(y == yr)[-1]]
        r = last / prev - 1.0
        if r < worst[1]:
            worst = (int(yr), float(r))
        prev = last
    return worst


def month_returns(book: Book, v: np.ndarray, lo: int, hi: int, cash0: float = CASH0) -> np.ndarray:
    """Calendar-month returns of a value series over sessions lo..hi."""
    s = book.sessions[lo : hi + 1]
    ym = s.year.to_numpy() * 100 + s.month.to_numpy()
    base = cash0 if lo == 0 else v[lo - 1]
    out = []
    for m in pd.unique(ym):
        idx = np.flatnonzero(ym == m)
        end = v[lo + idx[-1]]
        out.append(end / base - 1.0)
        base = end
    return np.array(out)


def boot_diff(
    rb: np.ndarray,
    rx: np.ndarray,
    years: float,
    base: float,
    draws: int = 2000,
    seed: int = 20261125,
):
    """Block bootstrap over calendar months: the book less a benchmark, in % a year and Rs."""
    rng = np.random.default_rng(seed)
    n = len(rb)
    idx = rng.integers(0, n, size=(draws, n))
    gb = np.prod(1 + rb[idx], axis=1)
    gx = np.prod(1 + rx[idx], axis=1)
    ya = gb ** (1.0 / years) - gx ** (1.0 / years)
    rs = base * (gb - gx)
    q = lambda a: [float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5))]  # noqa: E731
    return q(ya * 100), q(rs)


# ------------------------------------------------------------------------------------- main


def pct(x: float) -> str:
    return f"{x:+.2f}".replace("-", "−")


def per_period_trade_stats(book: Book, mask: np.ndarray):
    """Trades and % a trade (clustered t) by period of the ENTRY date, and for the whole span."""
    out = {}
    s = book.sessions
    for name, lo, hi in period_bounds(book):
        a, b = s[lo], s[hi]
        sel = mask & (book.ent >= lo) & (book.ent <= hi)
        _ = (a, b)
        x = book.net_pct[sel]
        m, se, t = clustered(x, book.week[sel]) if sel.sum() > 1 else (np.nan, np.nan, np.nan)
        out[name] = {
            "n": int(sel.sum()),
            "mean": m,
            "t": t,
            "tw": tweek(x, book.trades.entry_date[sel]) if sel.sum() > 1 else np.nan,
            "win": float((x > 0).mean() * 100) if len(x) else np.nan,
            "sd": float(x.std(ddof=1)) if len(x) > 1 else np.nan,
        }
    return out


def book_report(book: Book, res: dict, label: str) -> dict:
    eq, value = book.equity(res)
    bm = book.benchmarks(value, res["cash0"])
    rep = {"label": label, "cash0": res["cash0"], "end": float(eq[-1])}
    rep["trades"] = per_period_trade_stats(book, res["held"])
    rep["cap_refused"] = per_period_trade_stats(book, res["cap_refused"])
    rep["cash_refused"] = per_period_trade_stats(book, res["cash_refused"])
    rep["growth"] = {}
    rep["bench_end"] = {k: float(v[-1]) for k, v in bm.items()}
    rep["diff"] = {}
    c0 = res["cash0"]
    for name, lo, hi in period_bounds(book):
        rep["growth"][name] = seg_growth(book, eq, lo, hi, c0)
        pre = 0 if lo == 0 else lo - 1
        b0 = c0 if lo == 0 else eq[lo - 1]
        yrs = (book.sessions[hi] - book.sessions[pre]).days / DAYS_A_YEAR
        rb = month_returns(book, eq, lo, hi, c0)
        for k, v in bm.items():
            v_start = c0 if lo == 0 else v[lo - 1]
            # (a) the benchmark's own value path; (b) the same rupees: the book's value at the
            # start of the period plus the benchmark's gain since
            own = seg_growth(book, v, lo, hi, c0)
            same = v - v_start + b0
            g_same = growth(b0, same[hi], book.sessions[pre], book.sessions[hi])
            ci, rci = boot_diff(rb, month_returns(book, same, lo, hi, b0), yrs, b0)
            rep["diff"][f"{name}:{k}"] = {
                "pct_year": (rep["growth"][name] - g_same) * 100,
                "pct_year_own": (rep["growth"][name] - own) * 100,
                "ci": ci,
                "rs": float((eq[hi] - b0) - (v[hi] - v_start)),
                "rs_ci": rci,
                "bench_growth": g_same,
            }
    rep["drawdown"] = drawdown(eq, c0)
    rep["worst_year"] = worst_year(book, eq, c0)
    rep["avg_places"] = float(res["places"].mean())
    rep["share_full"] = float(res["full"].mean())
    rep["max_places"] = int(res["places"].max())
    return rep


def fmt_report(r: dict) -> str:
    L = [f"== {r['label']}: cash {r['cash0']:,.0f}, end {r['end']:,.0f}"]
    for nm, tag in (
        ("trades", "HELD"),
        ("cap_refused", "CAP-REFUSED"),
        ("cash_refused", "CASH-REFUSED"),
    ):
        for p, d in r[nm].items():
            L.append(
                f"  {tag:12s} {p:6s} n={d['n']:5d} mean={d['mean']:+.3f} t={d['t']:+.2f} tweek={d['tw']:+.2f} "
                f"win={d['win']:.1f} sd={d['sd']:.2f}"
            )
    L.append("  growth/yr: " + ", ".join(f"{k} {v * 100:+.2f}" for k, v in r["growth"].items()))
    L.append(f"  max drawdown {r['drawdown'] * 100:+.1f}, worst year {r['worst_year']}")
    L.append(
        f"  avg places {r['avg_places']:.2f}, share full {r['share_full']:.3f}, max {r['max_places']}"
    )
    L.append("  bench end: " + ", ".join(f"{k} {v:,.0f}" for k, v in r["bench_end"].items()))
    for k, d in r["diff"].items():
        L.append(
            f"  less {k:9s} {d['pct_year']:+.2f} %/yr (own base {d['pct_year_own']:+.2f}) [{d['ci'][0]:+.2f}, {d['ci'][1]:+.2f}] "
            f"Rs {d['rs']:+,.0f} [{d['rs_ci'][0]:+,.0f}, {d['rs_ci'][1]:+,.0f}]"
        )
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    trades = pd.read_pickle(SWING / "trades_P1.pkl")
    trades = trades.sort_values(["signal_date", "stock"]).reset_index(drop=True)
    bars = load_bars()
    nifty = load_nifty()
    book = Book(trades, bars, nifty)
    feat = order_features(trades, bars)
    print("sessions", book.n, "trades", len(trades), "nifty bars", len(nifty))
    print("missing depth:", int(feat.depth.isna().sum()), "turn:", int(feat.turn.isna().sum()))
    desk = (trades.group == "desk18").to_numpy()
    name_rank = trades.stock.rank(method="first").to_numpy()
    depth = feat.depth.to_numpy()
    ranks = {
        "PRODUCT": np.where(desk, 0.0, 1e6) - depth + name_rank * 1e-9,
        "DEEP": -depth + name_rank * 1e-9,
        "SHALLOW": depth + name_rank * 1e-9,
        "LIQUID": -feat.turn.to_numpy() + name_rank * 1e-9,
        "CALM": feat.vol.to_numpy() + name_rank * 1e-9,
    }
    # the places the evening could hold at most
    allres = book.run(ranks["PRODUCT"], None, cash0=1e12, cash_rule=False)
    maxopen = int(allres["places"].max())
    print("largest number ever active without a cap:", maxopen)

    lines = []
    reports = {}
    # the product's cap tier: places only
    nores = book.run(ranks["PRODUCT"], 10, cash_rule=False)
    lines.append(
        f"PRODUCT K=10 places only: held {int(nores['held'].sum())} "
        f"cap-refused {int(nores['cap_refused'].sum())}"
    )
    res = book.run(ranks["PRODUCT"], 10)
    lines.append(
        f"PRODUCT K=10 with cash: held {int(res['held'].sum())} cap-refused "
        f"{int(res['cap_refused'].sum())} cash-refused {int(res['cash_refused'].sum())}"
    )
    # same-name check: the cap tier reading of the study's all-tier world
    for lab, kk, cash0 in (
        ("PRODUCT K=10", 10, CASH0),
        ("DEEP K=10", 10, CASH0),
        ("SHALLOW K=10", 10, CASH0),
        ("LIQUID K=10", 10, CASH0),
        ("CALM K=10", 10, CASH0),
        ("PRODUCT K=5", 5, 5 * UNIT),
        ("PRODUCT K=20", 20, 20 * UNIT),
        ("PRODUCT nocap", None, maxopen * UNIT),
    ):
        o = lab.split()[0]
        r = book.run(ranks[o], kk, cash0=cash0)
        reports[lab] = book_report(book, r, lab)
        lines.append(fmt_report(reports[lab]))
        if lab == "PRODUCT K=10":
            main_res = r
    # the evening types, the power, the cap's cost
    held = main_res["held"]
    capr = main_res["cap_refused"]
    lines.append("== cap cost, PRODUCT K=10")
    for nm, m in (
        ("held", held),
        ("cap-refused", capr),
        ("cash-refused", main_res["cash_refused"]),
    ):
        st = per_period_trade_stats(book, m)
        lines.append(
            f"  {nm:12s} "
            + "; ".join(f"{p} n={d['n']} {d['mean']:+.2f} (t {d['t']:+.1f})" for p, d in st.items())
        )
    sig = main_res["signals"]
    dd = np.flatnonzero(sig > 0)
    nh = np.array(
        [
            np.max(nifty.reindex(book.sessions).ffill().to_numpy()[max(0, d - 19) : d + 1])
            for d in dd
        ]
    )
    nc = book.nifty[dd]
    below = (nh - nc) / nh * 100
    fullm = main_res["places"][dd] >= 10
    lines.append(f"  signal evenings {len(dd)}; full after taking places {int(fullm.sum())}")
    lines.append(
        f"  full evenings: median signals {np.median(sig[dd][fullm])}, NIFTY below its 20-session "
        f"high {np.median(below[fullm]):.2f}%; others: {np.median(sig[dd][~fullm])}, "
        f"{np.median(below[~fullm]):.2f}%"
    )
    caphit = main_res["cap_hit"][dd]
    lines.append(
        f"  evenings where signals exceeded free places: {int(caphit.sum())}; "
        f"median signals {np.median(sig[dd][caphit])}, NIFTY below high {np.median(below[caphit]):.2f}%"
    )

    # the signals-per-evening table of the note's answer 5
    ev = pd.Series(sig[book.sig]).to_numpy()  # signals on each trade's evening
    lines.append("== by signals on the evening (held / cap-refused): n, % a trade")
    for lo, hi in ((1, 2), (3, 9), (10, 10**6)):
        g = (ev >= lo) & (ev <= hi)
        a, b2 = held & g, capr & g
        lines.append(
            f"  {lo}-{hi}: held n={int(a.sum())} {book.net_pct[a].mean():+.2f}; capped "
            f"n={int(b2.sum())} {book.net_pct[b2].mean():+.2f}"
        )
    ch = main_res["cap_hit"][dd]
    lines.append(
        f"  evenings with a refusal for no place: {int(ch.sum())} median signals "
        f"{np.median(sig[dd][ch])} mean {sig[dd][ch].mean():.1f} NIFTY {np.median(below[ch]):.2f}%; "
        f"others {np.median(sig[dd][~ch])} mean {sig[dd][~ch].mean():.1f} "
        f"NIFTY {np.median(below[~ch]):.2f}%"
    )
    cr = main_res["cash_refused"]
    yrs = book.trades.entry_date[cr].dt.year.value_counts().sort_index().to_dict()
    lines.append(f"  cash refusals by year {yrs}")
    lines.append(
        "  cash refusals by stock (top 5) "
        + str(book.trades.stock[cr].value_counts().head(5).to_dict())
    )
    # places and full share by period, and the worst fall's dates
    eq, _ = book.equity(main_res)
    for name, lo, hi in period_bounds(book):
        lines.append(
            f"  {name}: places {main_res['places'][lo : hi + 1].mean():.1f}, "
            f"all full {np.mean(main_res['full'][lo : hi + 1]) * 100:.0f}%"
        )
    pk = np.maximum.accumulate(eq)
    j = int(np.argmin(eq / pk))
    i = int(np.argmax(eq[: j + 1]))
    lines.append(f"  worst fall from {book.sessions[i].date()} to {book.sessions[j].date()}")
    # the entry tie-break: K = 5 depends on the order of fills of one morning
    for ofirst in (True, False):
        r5 = book.run(ranks["PRODUCT"], 5, cash0=5 * UNIT, older_first=ofirst)
        e5, v5 = book.equity(r5)
        b35 = book.benchmarks(v5, 5 * UNIT)["B3"]
        lines.append(
            f"  K=5, entries of a morning older signal first={ofirst}: held "
            f"{int(r5['held'].sum())} cash-refused {int(r5['cash_refused'].sum())} end "
            f"{e5[-1]:,.0f}; less B3 ALL {(growth(5 * UNIT, e5[-1], book.sessions[0], book.sessions[-1]) - growth(5 * UNIT, b35[-1], book.sessions[0], book.sessions[-1])) * 100:+.2f}"
        )
    # the interval's seed: how often 'behind B3 beyond chance' holds over the whole span
    rb = month_returns(book, eq, 0, book.n - 1)
    _, vv = book.equity(main_res)
    b3 = book.benchmarks(vv)["B3"]
    rx = month_returns(book, b3, 0, book.n - 1)
    yrs_all = (book.sessions[-1] - book.sessions[0]).days / DAYS_A_YEAR
    ups = []
    for sd_ in range(1, 41):
        ci, _ = boot_diff(rb, rx, yrs_all, CASH0, seed=sd_)
        ups.append(ci[1])
    ups = np.array(ups)
    lines.append(
        f"  ALL less B3, upper end of the 95% interval over 40 other seeds: min {ups.min():+.2f} "
        f"max {ups.max():+.2f}; seeds with the upper end below zero {int((ups < 0).sum())} of 40"
    )
    months = (book.sessions[-1] - book.sessions[0]).days / DAYS_A_YEAR * 12
    for lab, m in (("capped(held)", held), ("tier all", np.ones(len(held), bool))):
        x = book.net_pct[m]
        mean, sd = x.mean(), x.std(ddof=1)
        need = (2 * sd / mean) ** 2
        rate = m.sum() / months
        s = book.sessions
        tl = int(s.searchsorted(PERIODS[2][2], side="right")) - 1
        tlo = int(s.searchsorted(PERIODS[1][2], side="right"))
        tm = m & (book.ent >= tlo)
        tmean = book.net_pct[tm].mean()
        trate = tm.sum() / ((s[tl] - s[tlo]).days / DAYS_A_YEAR * 12)
        tsd = book.net_pct[tm].std(ddof=1)
        tneed = (2 * tsd / tmean) ** 2 if tmean > 0 else np.nan
        lines.append(
            f"  power {lab}: {rate:.1f}/month; mean {mean:+.3f} sd {sd:.2f}: need {need:,.0f} trades "
            f"= {need / rate:,.0f} months; TEST {trate:.1f}/month mean {tmean:+.3f} sd {tsd:.2f}: need "
            f"{tneed:,.0f} = {tneed / trate:,.0f} months"
        )
    lines.append(
        f"  trades a month of tier all: {len(held) / months:.1f}; span months {months:.1f}"
    )

    # RANDOM
    rng = np.random.default_rng(args.seed)
    rnd = {p: {"pt": [], "gr": []} for p in ("TUNE", "CHOOSE", "TEST")}
    bounds = {n: (lo, hi) for n, lo, hi in period_bounds(book)}
    cache = OUT / f"check_random_{args.draws}_{args.seed}.json"
    if cache.exists():
        rnd = json.loads(cache.read_text())
    else:
        for k in range(args.draws):
            r = book.run(rng.random(len(trades)), 10)
            eq, _ = book.equity(r)
            st = per_period_trade_stats(book, r["held"])
            for p in rnd:
                rnd[p]["pt"].append(st[p]["mean"])
                rnd[p]["gr"].append(seg_growth(book, eq, *bounds[p]))
            if k % 100 == 0:
                print("random", k, flush=True)
        cache.write_text(json.dumps(rnd))

    def stand(p: str, key: str, val: float) -> float:
        a = np.array(rnd[p][key])
        a = a[np.isfinite(a)]
        return float((a < val).mean() * 100)

    lines.append(f"== RANDOM ({args.draws} shuffles, K=10, cash rule)")
    for p in rnd:
        a = np.array(rnd[p]["pt"])
        g = np.array(rnd[p]["gr"])
        lines.append(
            f"  {p}: % a trade median {np.nanmedian(a):+.3f} p2.5 {np.nanpercentile(a, 2.5):+.3f} "
            f"p97.5 {np.nanpercentile(a, 97.5):+.3f}; growth median {np.nanmedian(g) * 100:+.2f}"
        )
    for lab in ("PRODUCT K=10", "DEEP K=10", "SHALLOW K=10", "LIQUID K=10", "CALM K=10"):
        rp = reports[lab]
        lines.append(
            f"  {lab}: pctile of % a trade "
            + " / ".join(f"{stand(p, 'pt', rp['trades'][p]['mean']):.0f}" for p in rnd)
            + "; of growth "
            + " / ".join(f"{stand(p, 'gr', rp['growth'][p]):.0f}" for p in rnd)
        )
    text = "\n".join(lines)
    (OUT / "check_numbers.txt").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
