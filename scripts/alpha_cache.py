"""Alpha search of 2026-11-12: a day-by-minute cache of the cleaned one-minute bars.

For every research instrument (5 indices, 18 stocks) this writes one .npz with matrices of
shape (sessions, 375) on the grid 09:15..15:29 IST: C (last close, forward-filled inside the
day), H, L (the minute's own high and low, NaN when the minute has no bar), V (volume, 0
when missing), plus per session: day (ordinal), o (first open), nbars. India VIX (daily) is
saved beside them. Read-only on the store.

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_cache.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402

OUT = h.ROOT / "data" / "research" / "alpha_2026-11-12" / "cache"
GRID0 = 9 * 60 + 15
NMIN = 375


def build(series: str) -> dict[str, np.ndarray]:
    bars = h._cache.bars(series, "1m")
    end = pd.Timestamp(h.T_END + pd.Timedelta(days=1), tz=bars.index.tz)
    bars = bars[bars.index < end]
    idx = bars.index
    minute = (idx.hour * 60 + idx.minute).to_numpy() - GRID0
    ords = np.array([d.toordinal() for d in idx.date])
    keep = (minute >= 0) & (minute < NMIN)
    minute, ords = minute[keep], ords[keep]
    o, hi, lo, c = (bars[k].to_numpy(float)[keep] for k in ("open", "high", "low", "close"))
    v = bars["volume"].to_numpy(float)[keep] if "volume" in bars.columns else np.zeros(keep.sum())
    days = np.unique(ords)
    row = np.searchsorted(days, ords)
    n = len(days)
    C = np.full((n, NMIN), np.nan)  # noqa: N806
    H = np.full((n, NMIN), np.nan)  # noqa: N806
    L = np.full((n, NMIN), np.nan)  # noqa: N806
    V = np.zeros((n, NMIN))  # noqa: N806
    C[row, minute], H[row, minute], L[row, minute], V[row, minute] = c, hi, lo, v
    nbars = np.isfinite(C).sum(axis=1)
    first = np.full(n, np.nan)
    seen = np.zeros(n, dtype=bool)
    for i in range(len(row)):  # bars are in time order: the first bar of a row is its open
        if not seen[row[i]]:
            seen[row[i]] = True
            first[row[i]] = o[i]
    # forward-fill the close inside the day (never across days)
    for j in range(1, NMIN):
        gap = ~np.isfinite(C[:, j])
        C[gap, j] = C[gap, j - 1]
    return {"day": days, "o": first, "nbars": nbars, "C": C, "H": H, "L": L, "V": V}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    h._init_worker()
    t0 = time.time()
    names = {}
    for inst in h.INDEX_OPTIONS + h.STOCKS:
        series = h._cache.spec(inst).series_key
        names[inst] = series
        d = build(series)
        np.savez_compressed(OUT / f"{series}.npz", **d)
        full = int((d["nbars"] >= 360).sum())
        print(
            f"{series:11s} sessions {len(d['day']):5d} (full {full:5d})  "
            f"{pd.Timestamp.fromordinal(int(d['day'][0])).date()}..{pd.Timestamp.fromordinal(int(d['day'][-1])).date()}  "
            f"volume>0 minutes {int((d['V'] > 0).sum()):8d}  [{time.time() - t0:5.0f}s]",
            flush=True,
        )
    vix = h._cache.vix()
    vix.to_frame("vix").to_csv(OUT / "vix.csv")
    print(
        "vix rows",
        len(vix),
        "first",
        vix.index[0],
        "last",
        vix.index[-1],
        "| type",
        type(vix).__name__,
    )
    print("done in", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()
