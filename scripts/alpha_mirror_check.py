"""Alpha search of 2026-11-12: an independent re-implementation of the mirror rule C_N_M.

Written separately from scripts/alpha_stocks_daily.py (different author, plain loops) so the
two can be compared. Store daily bars only (the 2021-2026 sample). PLAN.md section A3.

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_mirror_check.py [N M]
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402

SLIP = h.STOCK_SLIPPAGE
T0, T1 = dt.date(2021, 11, 8), dt.date(2026, 11, 7)
CUTS = (dt.date(2024, 11, 8), dt.date(2025, 11, 8))


def mirror_trades(o: np.ndarray, c: np.ndarray, n: int, m: int) -> list[tuple[int, int, bool]]:
    """(entry bar, exit bar, still open) of the mirror trades; fills are at those bars' opens."""
    out: list[tuple[int, int, bool]] = []
    s1_long = False
    s1_had_trade = False
    c_entry: int | None = None
    for t in range(max(n, m), len(c) - 1):
        hi = c[t - n : t].max()
        lo = c[t - m : t].min()
        if not s1_long and c[t] > hi:  # S1 buys at the open of t+1: the mirror sells there
            s1_long = True
            if c_entry is not None:
                out.append((c_entry, t + 1, False))
                c_entry = None
        elif s1_long and c[t] < lo:  # S1 sells at the open of t+1: the mirror buys there
            s1_long = False
            s1_had_trade = True
            c_entry = t + 1
        _ = s1_had_trade
    if c_entry is not None:
        out.append((c_entry, len(c) - 1, True))
    return out


def main() -> None:
    n, m = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (55, 20)
    h._init_worker()
    cost = h.LegCost.from_config(h._cost_cfg.product("equity_delivery"))
    rows = []
    for inst in h.STOCKS:
        bars = h._cache.bars(h._cache.spec(inst).series_key, "1d")
        dates = np.array([d.date() for d in bars.index])
        o, c = bars["open"].to_numpy(float), bars["close"].to_numpy(float)
        win = (dates >= T0) & (dates <= T1)
        drift = (c[win][1:] / c[win][:-1] - 1.0).mean()
        last = c[win][-1]
        for e, x, still in mirror_trades(o, c, n, m):
            if not (T0 <= dates[e] <= T1):
                continue
            buy = o[e] * (1 + SLIP)
            sell = (c[x] if still else o[x]) * (1 - SLIP)
            charges = (
                cost.leg(np.array([buy]), sell=False)[0] + cost.leg(np.array([sell]), sell=True)[0]
            )
            net = (sell - buy - charges) / buy * 100.0
            per = 0 if dates[e] < CUTS[0] else (1 if dates[e] < CUTS[1] else 2)
            rows.append(
                (
                    inst,
                    dates[e],
                    dates[x],
                    x - e,
                    net,
                    drift * (x - e) * 100.0,
                    per,
                    14.75 / last * 100.0,
                )
            )
    net = np.array([r[4] for r in rows])
    bench = np.array([r[5] for r in rows])
    per = np.array([r[6] for r in rows])
    days = np.array([r[3] for r in rows])
    dp = np.array([r[7] for r in rows])
    print(
        f"C_{n}_{m}, store bars, entries {T0}..{T1}: trades {len(rows)}, winners {(net > 0).mean() * 100:.1f}%"
    )
    print(
        f"  mean before DP {net.mean():+.3f}%  after DP today {(net - dp).mean():+.3f}%  mean days {days.mean():.1f}  max days {days.max()}"
    )
    print(
        f"  benchmark {bench.mean():+.3f}%  excess {(net - bench).mean():+.3f}%  trade-level t of the excess {(net - bench).mean() / ((net - bench).std(ddof=1) / np.sqrt(len(net))):+.2f}"
    )
    for p, nm in enumerate(("TUNE", "CHOOSE", "TEST")):
        k = per == p
        print(
            f"  {nm:6s} n={k.sum():3d} mean {net[k].mean():+.3f}% excess {(net[k] - bench[k]).mean():+.3f}%"
        )
    worst = sorted(rows, key=lambda r: r[4])[:3]
    for r in worst:
        print(f"  worst: {r[0]} {r[1]} -> {r[2]} ({r[3]} sessions) {r[4]:+.2f}%")


if __name__ == "__main__":
    main()
