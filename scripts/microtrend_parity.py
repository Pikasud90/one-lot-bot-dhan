"""US-068: the product's `microtrend` strategy against the lead's reference loop on real bars.

For each of the five indices: the reference trades (`microtrend_backtest.trades_of` on
`mtf_bruteforce.prepare(inst, tfs=TFS)`), and the PRODUCT strategy run through its own signal
generation (`Strategy.generate` on 3-minute bars with the one-minute bars as the ladder's base,
the context the desk's tracker gives it), taken one trade at a time per index: entries inside
09:30 to 14:45, out at the first 3-minute close with `long_exit` / `short_exit`, else at the
15:15 option exit. Compared on the entry minute and side, then also on the exit minute.

READ-ONLY: the store is opened through an SQLite URI with mode=ro (`--store`, default the main
checkout's data/onelot.db); the harness's `_init_worker` is not called (it would open the
project's own database for writing), its cache is given the read-only engine instead.

    PYTHONPATH=src:scripts .venv/bin/python -W ignore scripts/microtrend_parity.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import microtrend_backtest as ref  # noqa: E402
import mtf_bruteforce as h  # noqa: E402

from onelot.clock import IST  # noqa: E402
from onelot.data.resample import resample_bars  # noqa: E402
from onelot.strategies.base import Context  # noqa: E402
from onelot.strategies.registry import get_strategy  # noqa: E402

MAIN_STORE = Path("/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN/data/onelot.db")
EXIT = dt.time(15, 15)


def read_only(path: Path):  # noqa: ANN201
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


def init_read_only(store: Path) -> None:
    """`h._init_worker` with the cache on a read-only engine (nothing is created or written)."""
    from onelot.config import CostConfig
    from onelot.lab.runner import DataCache
    from onelot.settings import current_settings

    logging.disable(logging.WARNING)
    h._cache = DataCache(engine=read_only(store))
    h._settings = current_settings()
    h._cost_cfg = CostConfig.load(h._settings.project_root / "config" / "costs.yaml")


def reference(p: h.Prep) -> list[tuple[pd.Timestamp, int, pd.Timestamp]]:
    tr = ref.trades_of(p)
    ok = p.year[tr["e"]] >= 0
    return [
        (p.index[e], int(s), p.index[x])
        for e, x, s in zip(tr["e"][ok], tr["x"][ok], tr["s"][ok], strict=True)
    ]


def product(p: h.Prep) -> list[tuple[pd.Timestamp, int, pd.Timestamp]]:
    """The product's trades, one at a time, at the moments the reference counts: a decision is
    the close of a 3-minute bar (the open of the next one-minute bar)."""
    spec = h._cache.spec(p.inst)
    bars = h._cache.bars(spec.series_key, "1m")
    bars = bars[bars.index < pd.Timestamp(h.T_END + dt.timedelta(days=1), tz=bars.index.tz)]
    three = resample_bars(bars, "3m")
    sess = spec.session
    s = get_strategy(
        "microtrend",
        {"timeframe": "3m", "entry_start": sess.get("first_entry", "09:30"),
         "entry_end": sess.get("last_entry", "14:45"), "exit_time": "15:15"},
    )  # fmt: skip
    ctx = Context(
        symbol=spec.series_key,
        extras={"minute_bars": bars, "minute_resolution": "1m", "has_volume": False},
    )
    f = s.generate(three, ctx)
    at = pd.DatetimeIndex(f.index) + pd.Timedelta(minutes=3)  # the decision moment
    first_day, last_day = h.T0 + dt.timedelta(days=0), h.T_END
    days = np.asarray(at.tz_convert(IST).date)
    keep = (days >= h.YEAR_STARTS[0]) & (days <= last_day) & (days >= first_day)
    le, se = f["long_entry"].to_numpy(bool), f["short_entry"].to_numpy(bool)
    lx, sx = f["long_exit"].to_numpy(bool), f["short_exit"].to_numpy(bool)
    out: list[tuple[pd.Timestamp, int, pd.Timestamp]] = []
    busy_until = None
    n = len(f)
    i = 0
    while i < n:
        t = at[i]
        if keep[i] and (busy_until is None or t > busy_until) and (le[i] or se[i]):
            side = 1 if le[i] else -1
            day = days[i]
            limit = pd.Timestamp(dt.datetime.combine(day, EXIT), tz=IST)
            if t < limit:
                exits = lx if side == 1 else sx
                x = None
                for j in range(i + 1, n):
                    if days[j] != day or at[j] >= limit:
                        break
                    if exits[j]:
                        x = at[j]
                        break
                if x is None:  # the option exit: the first one-minute bar at or after 15:15,
                    # or, in a short session (a Budget Saturday) that ends before it, its last bar
                    today = bars.index[bars.index.date == day]
                    late = today[today >= limit]
                    x = late[0] if len(late) else today[-1]
                out.append((t, side, x))
                busy_until = x
        i += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", type=Path, default=MAIN_STORE)
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    init_read_only(a.store)
    for inst in h.INDEX_OPTIONS:
        t0 = time.perf_counter()
        p = h.prepare(inst, tfs=ref.TFS)
        r, q = reference(p), product(p)
        rk = {(e, s): x for e, s, x in r}
        qk = {(e, s): x for e, s, x in q}
        both = set(rk) & set(qk)
        same_exit = sum(1 for k in both if rk[k] == qk[k])
        print(
            f"{inst:15s} reference {len(r):5d}  product {len(q):5d}  matched entry {len(both):5d}"
            f"  and exit {same_exit:5d}  ({time.perf_counter() - t0:.0f} s)"
        )
        diffs = sorted(
            [("only ref", k, rk[k]) for k in set(rk) - set(qk)]
            + [("only product", k, qk[k]) for k in set(qk) - set(rk)]
            + [("exit differs", k, (rk[k], qk[k])) for k in both if rk[k] != qk[k]],
            key=lambda d: d[1][0],
        )
        for kind, (e, side), x in diffs[:5]:
            print(f"    {kind:12s} {e:%Y-%m-%d %H:%M} {'call' if side == 1 else 'put '} -> {x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
