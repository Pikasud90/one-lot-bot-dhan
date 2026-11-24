"""The Volume Watch on one stored session, checked against a direct computation (US-089).

    PYTHONPATH=src .venv/bin/python scripts/volume_replay.py --store data/onelot.db \\
        --date 2026-11-20 [--index NIFTY]

Opens the store READ-ONLY (SQLite URI mode=ro), runs the service for that date as
`GET /api/volume?date=` does, prints the day's headline, its alerts and the top stocks, then
recomputes the newest minute's turnover, expected turnover and spike score straight from the
stored bars with `onelot.volume.indicator` and says whether they agree. Exit 1 on a mismatch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

from onelot.clock import IST
from onelot.data.store import _read_bars
from onelot.volume import indicator as ind
from onelot.volume import service as vs


def read_only_engine(path: Path):  # noqa: ANN201
    return create_engine(f"sqlite:///file:{path.resolve()}?mode=ro&uri=true")


def direct(engine, watch: vs.VolumeWatch, day: dt.date, col: int, keys: list[str]) -> dict:
    """The newest minute from the raw bars: turnover, expected turnover, z of the basket."""
    start = dt.datetime.combine(day - dt.timedelta(days=vs.HISTORY_DAYS), dt.time(0), IST)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), IST)
    mats, fulls = {}, {}
    for s in keys:
        bars = _read_bars(engine, s, "1m", start, end)
        m = ind.day_matrix(ind.turnover(bars))
        f = ind.full_days(m)
        f.loc[pd.Timestamp(day)] = False  # the shown day is never its own baseline
        mats[s], fulls[s] = m, f
    idx = sorted(set().union(*(set(m.index) for m in mats.values())))
    idx = pd.DatetimeIndex(idx)
    row = idx.get_loc(pd.Timestamp(day))
    turn = sum(np.nan_to_num(mats[s].reindex(idx).to_numpy()[row, col]) for s in keys)
    expected = sum(
        ind.expected_matrix(mats[s].reindex(idx), fulls[s].reindex(idx).fillna(False)).to_numpy()[
            row, col
        ]
        for s in keys
    )
    tb = np.zeros((len(idx), ind.SESSION_MINUTES))
    nfull = np.zeros(len(idx))
    for s in keys:
        m = mats[s].reindex(idx)
        f = fulls[s].reindex(idx).fillna(False).to_numpy(dtype=bool)
        tb += np.nan_to_num(m.to_numpy())
        nfull += f
    full_b = pd.Series(nfull >= np.ceil(vs.BASKET_FULL * len(keys)), index=idx)
    full_b.iloc[row] = False
    tbf = pd.DataFrame(tb, index=idx, columns=range(ind.SESSION_MINUTES))
    mu, sd = ind.zstats_matrix(tbf.iloc[: row + 1], full_b.iloc[: row + 1])
    today = ind.fill_through(tb[row], col)
    z = ind.zscore(ind.log_window(today), mu.to_numpy()[row], sd.to_numpy()[row])[0, col]
    return {"turnover_1m": turn, "expected_1m": expected, "z": z}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, type=Path)
    ap.add_argument("--date", required=True)
    ap.add_argument("--index", default="NIFTY")
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.date)
    engine = read_only_engine(a.store)
    clock_at = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(8, 0), IST)
    watch = vs.VolumeWatch(engine, None, sync=True, clock=lambda: clock_at, create_tables=False)
    body = watch.body(day.isoformat(), a.index)
    now = body["now"]
    if now is None:
        print("no view:", "; ".join(body["notes"]))
        return 1
    print(f"{body['session']} ({body['source']}), newest minute {now['minute']}")
    print(f"  {now['text']}  state {now['state']}, rvol 5m {now['rvol_5m']}, z {now['z']}")
    print(f"  turnover {now['turnover_day']:,.0f} against {now['expected_day']:,.0f} expected")
    if body["basket"]["missing"]:
        print("  missing:", ", ".join(body["basket"]["missing"]))
    print(f"alerts ({len(body['alerts'])}):")
    for al in reversed(body["alerts"]):
        print(f"  {al['ts'][11:16]} {al['title']}. {al['text']}")
    print("top stocks by turnover:")
    for s in body["stocks"][:5]:
        print(f"  {s['label']:24s} share {s['share']}, rvol day {s['rvol_day']}, z {s['z']}")
    for i in body["indices"]:
        print(f"  {i['label']:22s} pressure 5m {i['pressure_5m']}, r2 {i['r2']}: {i['text']}")
    col = int(now["minute"][:2]) * 60 + int(now["minute"][3:]) - ind.OPEN_MINUTE
    keys = [s["key"] for s in body["stocks"] if s["key"] not in body["basket"]["missing"]]
    keys = [k for k in keys if watch.baselines(day, wait=True).has_history(k)]
    d = direct(engine, watch, day, col, keys)
    bad = []
    for k in ("turnover_1m", "expected_1m", "z"):
        got, want = now[k], d[k]
        tol = 0.011 if k == "z" else max(1.0, abs(want) * 1e-6)
        if got is None or not np.isfinite(want) or abs(got - want) > tol:
            if not (got is None and not np.isfinite(want)):
                bad.append(f"{k}: service {got} direct {want}")
    if bad:
        print("check: MISMATCH " + "; ".join(bad))
        return 1
    print(f"check: OK (turnover, expected and z of {now['minute']} recomputed from the bars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
