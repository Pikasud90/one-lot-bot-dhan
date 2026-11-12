"""Alpha search of 2026-11-12: an independent re-implementation of rule R4 (PLAN.md A2).

R4: the close is above SMA200 and falls to DEPTH or more below the highest close of 20
sessions (first day of that condition); buy at the next open; sell at the open after the
first close above SMA20, or at the open after 30 sessions. Written separately from
scripts/alpha_stocks_daily.py (plain loops, different author) on both samples: the store's
daily bars (entries 2021-11-08..2026-11-07) and the older Yahoo bars saved by that script
(entries 2011-01-01..2021-11-07). Also: a closing stop and a market filter, stated in
advance as risk variants, and a like-for-like benchmark (the stock's own mean return over
the same number of sessions, open to open, with the same charges).

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_pullback_check.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402

SLIP = h.STOCK_SLIPPAGE
OLD_DIR = h.ROOT / "data" / "research" / "alpha_2026-11-12" / "daily_old"
SAMPLES = {
    "2011-2021": (dt.date(2011, 1, 1), dt.date(2021, 11, 7)),
    "2021-2026": (dt.date(2021, 11, 8), dt.date(2026, 11, 7)),
}


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def trades(o, c, depth, stop, ok_day):  # noqa: ANN001
    """(signal bar, entry bar, exit bar, closed at the last close) of every R4 trade."""
    s200, s20 = sma(c, 200), sma(c, 20)
    out, t, n = [], 200, len(c)
    while t < n - 1:
        hi = c[t - 19 : t + 1].max()
        hi_prev = c[t - 20 : t].max()
        first = c[t] <= (1 - depth) * hi and not c[t - 1] <= (1 - depth) * hi_prev
        if not (first and c[t] > s200[t] and ok_day[t]):
            t += 1
            continue
        e = t + 1
        buy = o[e] * (1 + SLIP)
        x, at_close = None, False
        for u in range(e, n):
            sessions = u - e + 1
            hit = c[u] > s20[u] or sessions >= 30 or (stop is not None and c[u] <= buy * (1 - stop))
            if hit:
                if u + 1 < n:
                    x = u + 1
                else:
                    x, at_close = u, True
                break
        if x is None:
            x, at_close = n - 1, True
        out.append((t, e, x, at_close))
        t = x  # no new signal is read before the bar the trade ends on
    return out


def load(sample: str) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    data = {}
    for inst in h.STOCKS + ["NIFTY_OPT"]:
        key = h._cache.spec(inst).series_key
        if sample == "2021-2026":
            b = h._cache.bars(key, "1d")
            d = np.array([x.date() for x in b.index])
            data[key] = (d, b["open"].to_numpy(float), b["close"].to_numpy(float))
        else:
            f = OLD_DIR / f"{key}.csv"
            if not f.exists():
                continue
            b = pd.read_csv(f)
            d = np.array([dt.date.fromisoformat(x) for x in b["date"]])
            data[key] = (d, b["open"].to_numpy(float), b["close"].to_numpy(float))
    return data


def run(sample: str, depth: float, stop: float | None, mkt: bool, cost, last_px: dict) -> dict:  # noqa: ANN001
    t0, t1 = SAMPLES[sample]
    data = load(sample)
    nd, _, nc = data["NIFTY"]
    n200 = sma(nc, 200)
    nifty_ok = dict(zip(nd.tolist(), (nc > n200).tolist(), strict=False))
    rows = []
    for inst in h.STOCKS:
        key = h._cache.spec(inst).series_key
        d, o, c = data[key]
        ok_day = np.array([nifty_ok.get(x, False) for x in d]) if mkt else np.ones(len(d), bool)
        win = (d >= t0) & (d <= t1)
        for _sig, e, x, at_close in trades(o, c, depth, stop, ok_day):
            if not (t0 <= d[e] <= t1):
                continue
            buy = o[e] * (1 + SLIP)
            sell = (c[x] if at_close else o[x]) * (1 - SLIP)
            ch = cost.leg(np.array([buy]), sell=False)[0] + cost.leg(np.array([sell]), sell=True)[0]
            net = (sell - buy - ch) / buy * 100.0
            days = x - e
            ow = o[win]
            if days > 0 and len(ow) > days:
                g = ow[days:] / ow[:-days]
                b_buy, b_sell = 1 + SLIP, g * (1 - SLIP)
                bch = cost.leg(np.array([1.0]), sell=False)[0] + cost.leg(b_sell, sell=True)
                bench = float(((b_sell - b_buy - bch) / b_buy * 100.0).mean())
            else:
                bench = 0.0
            wk = d[e].isocalendar()
            rows.append(
                (key, d[e], d[x], days, net, bench, wk[0] * 100 + wk[1], 14.75 / last_px[key] * 100)
            )
    net = np.array([r[4] for r in rows])
    exc = net - np.array([r[5] for r in rows])
    wk = np.array([r[6] for r in rows])

    def tcl(v: np.ndarray) -> float:
        u, inv = np.unique(wk, return_inverse=True)
        s = np.bincount(inv, v)
        return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))

    yr = np.array([r[1].year for r in rows])
    ent = np.array([r[1] for r in rows])
    if sample == "2021-2026":
        cuts, names = (dt.date(2024, 11, 8), dt.date(2025, 11, 8)), ("TUNE", "CHOOSE", "TEST")
    else:
        cuts, names = (dt.date(2015, 1, 1), dt.date(2019, 1, 1)), ("2011-14", "2015-18", "2019-21")
    per = np.array([0 if e < cuts[0] else (1 if e < cuts[1] else 2) for e in ent])
    periods = " ".join(
        f"{names[k]} {net[per == k].mean():+.2f}/{exc[per == k].mean():+.2f}({(per == k).sum()})"
        for k in range(3)
    )
    open_max = max(sum(1 for q in rows if q[1] <= r[1] < q[2]) for r in rows)
    return {
        "periods": periods, "open_max": open_max,
        "n": len(rows), "win": (net > 0).mean() * 100, "mean": net.mean(), "t": tcl(net),
        "after_dp": (net - np.array([r[7] for r in rows])).mean(), "days": np.mean([r[3] for r in rows]),
        "exc": exc.mean(), "tx": tcl(exc), "worst": net.min(), "p5": np.percentile(net, 5),
        "years": " ".join(f"{y % 100:02d}:{net[yr == y].mean():+.1f}" for y in np.unique(yr)),
        "neg_years": int(sum(net[yr == y].mean() < 0 for y in np.unique(yr))),
    }  # fmt: skip


def main() -> None:
    h._init_worker()
    cost = h.LegCost.from_config(h._cost_cfg.product("equity_delivery"))
    last_px = {}
    for inst in h.STOCKS:
        key = h._cache.spec(inst).series_key
        last_px[key] = float(h._cache.bars(key, "1d")["close"].iloc[-1])
    print(
        "rule                      sample      n  win%   mean (t)     afterDP  days   excess (t)    p5   worst  negative years | mean by entry year"
    )
    for name, depth, stop, mkt in (
        ("R4 8%", 0.08, None, False), ("R4 6%", 0.06, None, False), ("R4 10%", 0.10, None, False),
        ("R4 8% + stop 15%", 0.08, 0.15, False), ("R4 8% + stop 12%", 0.08, 0.12, False),
        ("R4 8% + NIFTY>SMA200", 0.08, None, True),
        ("R4 8% + NIFTY + stop 15%", 0.08, 0.15, True),
        ("R4 6% + NIFTY>SMA200", 0.06, None, True), ("R4 10% + NIFTY>SMA200", 0.10, None, True),
    ):  # fmt: skip
        for sample in SAMPLES:
            r = run(sample, depth, stop, mkt, cost, last_px)
            print(
                f"{name:25s} {sample}  {r['n']:4d} {r['win']:5.1f} {r['mean']:+6.2f} ({r['t']:4.1f})  {r['after_dp']:+6.2f} {r['days']:5.1f}"
                f"  {r['exc']:+6.2f} ({r['tx']:+4.1f}) {r['p5']:+6.1f} {r['worst']:+6.1f}  {r['neg_years']:2d} | {r['years']}"
                f" | mean/excess(n): {r['periods']} | most open at once {r['open_max']}"
            )


if __name__ == "__main__":
    main()
