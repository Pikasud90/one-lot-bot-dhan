"""US-099: replay the PRODUCT's evening round of the wide list day by day over a research file of
daily bars and compare its "all" tier with the study's trades of the same rule. Read-only.

    PYTHONPATH=src .venv/bin/python scripts/pullback_wide_replay.py \
        --daily "MAIN/data/research/swing_2026-11-24/daily.sqlite" \
        --nifty-store "MAIN/data/onelot.db" [--nifty-csv FILE] [--from DATE] [--to DATE]

What runs is `onelot.delivery.wide.WideDesk.run_round`, once per session of the file, with no
adapter (nothing is fetched) and its book in a temporary database. Every stock of the file is
a wide-list stock here (the desk 18 too, as in the study) and the desk 18's own book is empty,
so the cap counts the wide trades only. The stocks' bars are the file's with the study's repairs
(`onelot.delivery.universe.prepare_daily`, as the import tool writes them), up to `--to`
(default 2026-11-07, the study's last day).

NIFTY's closes, which R1(b) reads, are built as the study built them: the store's daily bars of
NIFTY with its cleaned one-minute bars (`onelot.lab.runner.DataCache`, the store opened with
mode=ro) and, before the store's first NIFTY bar (2017-01-02), the CSV given by `--nifty-csv`
(date, close), if any. Without it no signal can be read before NIFTY's 200th stored session:
the study's earlier trades are reported as such.

The comparison is by stock and signal date: the entry date, the exit date and the exit reason
(R3's reasons, recomputed from the bars for the study's trades). Sizes are NOT compared: the
study bought the whole shares at or just under Rs 10,000, the product the number nearest it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from onelot.clock import IST, SessionClock, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.delivery.money import delivery_model, depository_paise  # noqa: E402
from onelot.delivery.rule import PullbackParams  # noqa: E402
from onelot.delivery.universe import (  # noqa: E402
    UniverseStock,
    load_universe,
    prepare_daily,
    read_only,
    research_daily,
    stamped,
)
from onelot.delivery.wide import CLOSED, StockDays, WideDesk, replay_rounds  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402

STUDY_LAST = dt.date(2026, 11, 7)


def nifty_closes(store: Path | None, csv: Path | None, to: dt.date) -> pd.Series:
    """NIFTY's daily closes by date, as the study built them (see the module docstring)."""
    parts = []
    first = None
    if store is not None:
        from onelot.lab.runner import DataCache

        uri = f"file:{store.resolve()}?mode=ro"
        eng = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
        b = DataCache(engine=eng).bars("NIFTY", "1d")
        idx = pd.DatetimeIndex(b.index)
        idx = idx.tz_convert(IST) if idx.tz is not None else idx
        st = pd.Series(b["close"].to_numpy(float), index=list(idx.date))
        st = st[~st.index.duplicated(keep="last")]
        first = st.index[0]
        parts.append(st)
    if csv is not None:
        y = pd.read_csv(csv)
        col = "date" if "date" in y.columns else y.columns[0]
        ys = pd.Series(
            y["close"].to_numpy(float), index=[pd.Timestamp(d).date() for d in y[col]]
        ).dropna()
        if first is not None:
            ys = ys[[d < first for d in ys.index]]
        parts.insert(0, ys)
    if not parts:
        raise SystemExit("NIFTY is needed: give --nifty-store and/or --nifty-csv")
    out = pd.concat(parts).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out[[d <= to for d in out.index]]


def study_reasons(f: pd.DataFrame, entry: dt.date, u_day: dt.date, p: PullbackParams) -> str:
    """R3's reasons on the close of `u_day` for a trade that entered at `entry`'s open, from
    the bars (written apart from the product's code: a rolling mean, a count, a level)."""
    dates = list(f.index)
    u, e = dates.index(u_day), dates.index(entry)
    c = f["close"].to_numpy(float)
    out = []
    if u + 1 >= p.exit_sma and c[u] > float(np.mean(c[u + 1 - p.exit_sma : u + 1])):
        out.append("SMA20")
    if u - e + 1 >= p.max_sessions:
        out.append("TIME")
    if c[u] <= float(f["open"].iloc[e]) * (1 + p.slippage) * (1 - p.stop):
        out.append("STOP")
    return ",".join(out)


def sma_tie(f: pd.DataFrame, u_day: dt.date, p: PullbackParams) -> bool:
    """The close of `u_day` equals its SMA20 but for floating-point noise (1e-9 of the price):
    two ways of summing the same 20 closes may read it either side."""
    dates = list(f.index)
    u = dates.index(u_day)
    c = f["close"].to_numpy(float)
    if u + 1 < p.exit_sma:
        return False
    return abs(c[u] - float(np.mean(c[u + 1 - p.exit_sma : u + 1]))) <= 1e-9 * abs(c[u])


def paisa_stop(f: pd.DataFrame, entry: dt.date, exit_: dt.date, p: PullbackParams) -> bool:
    """The study's stop exit on the close before `exit_` is not one on the product's booked
    fill (rounded to the paisa, as the desk 18's book keeps it): the close lies between the two
    stop levels."""
    dates = list(f.index)
    e, u = dates.index(entry), dates.index(exit_) - 1
    fill = float(f["open"].iloc[e]) * (1 + p.slippage)
    close = float(f["close"].iloc[u])
    booked = round(fill * 100) / 100
    return booked * (1 - p.stop) < close <= fill * (1 - p.stop)


def main() -> int:  # noqa: C901 - a driver, read top to bottom
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--daily", type=Path, required=True, help="research file (read only)")
    ap.add_argument("--trades", type=Path, help="the study's trades (default: trades_P1.pkl)")
    ap.add_argument("--nifty-store", type=Path, help="the store, read only, for NIFTY")
    ap.add_argument("--nifty-csv", type=Path, help="NIFTY closes (date, close) before the store's")
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--to", type=dt.date.fromisoformat, default=STUDY_LAST)
    ap.add_argument("--universe", type=Path, default=ROOT / "config" / "delivery_universe.yaml")
    ap.add_argument("--show", type=int, default=8, help="examples shown per kind of mismatch")
    a = ap.parse_args()
    t0 = time.perf_counter()

    groups = {s.key: s.group for s in load_universe(a.universe).stocks}
    con = read_only(a.daily)
    keys = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily ORDER BY symbol")]
    frames: dict[str, pd.DataFrame] = {}
    for k in keys:
        f = prepare_daily(k, research_daily(con, k))
        frames[k] = f[[d <= a.to for d in f.index]]
    con.close()
    days = {k: StockDays.of(k, stamped(f)) for k, f in frames.items() if len(f)}
    nifty = nifty_closes(a.nifty_store, a.nifty_csv, a.to)
    print(f"{len(days)} stocks; NIFTY {nifty.index[0]} to {nifty.index[-1]} ({len(nifty)} days)")

    cfg = load_desk_config()
    params = PullbackParams.from_mapping(cfg.delivery_slots()[0][1].params)
    costs = CostConfig.load(ROOT / "config" / "costs.yaml")
    calendar = TradingCalendar.from_yaml(ROOT / "config" / "nse_holidays.yaml")
    tmp = Path(tempfile.mkdtemp(prefix="wide-replay-")) / "book.db"
    stocks = [UniverseStock(k, k, k, "0", "NSE_EQ", groups.get(k, "new")) for k in days]
    wide = WideDesk(
        create_engine(f"sqlite:///{tmp}"), stocks, params, delivery_model(costs),
        depository_paise(costs), calendar, SessionClock(SessionCfg(), calendar),
        cap_open=cfg.wide.cap_open, min_bars=cfg.delivery.min_bars, background=False,
    )  # fmt: skip
    sessions = sorted({d for sd in days.values() for d in sd.dates})
    if a.start:
        sessions = [d for d in sessions if d >= a.start]

    def progress(n: int, s: dt.date) -> None:
        if n % 250 == 0:
            print(f"  round {n} of {len(sessions)}: {s} ({time.perf_counter() - t0:.0f} s)")

    print(f"book: {tmp}; {len(sessions)} rounds, {sessions[0]} to {sessions[-1]}")
    replay_rounds(wide, days, list(nifty.index), nifty.to_numpy(float), sessions, progress=progress)
    mine = {(t.series_key, t.signal_date): t for t in wide.book.all()}
    tiers = Counter(t.tier for t in mine.values())
    print(f"product: {len(mine)} trades in tier all, of them {tiers.get('cap', 0)} within the cap")

    tr = pd.read_pickle(a.trades or a.daily.parent / "trades_P1.pkl")
    tr = tr.assign(
        signal=[pd.Timestamp(x).date() for x in tr.signal_date],
        entry=[pd.Timestamp(x).date() for x in tr.entry_date],
        exit=[pd.Timestamp(x).date() for x in tr.exit_date],
    )
    if a.start:
        tr = tr[[d >= a.start for d in tr.signal]]
    # the first session NIFTY's SMA200 can be read on: before it R1(b) never holds
    nifty_from = nifty.index[params.trend_sma - 1] if len(nifty) >= params.trend_sma else None
    kinds: dict[str, list[str]] = defaultdict(list)
    matched = 0
    for r in tr.itertuples():
        f = frames[r.stock]
        last = f.index[-1]
        t = mine.pop((r.stock, r.signal), None)
        if r.open_at_end:
            ex = study_reasons(f, r.entry, r.exit, params) if r.exit == last else ""
        else:
            prev = f.index[list(f.index).index(r.exit) - 1]
            ex = study_reasons(f, r.entry, prev, params)
        if t is None:
            if nifty_from is None or r.signal < nifty_from:
                kinds["only in the study: before NIFTY's 200th session"].append(
                    f"{r.stock} {r.signal}"
                )
            else:
                kinds["only in the study"].append(f"{r.stock} {r.signal} -> {r.exit} {ex}")
            continue
        got_exit = t.exit_session
        if t.entry_session != r.entry:
            kinds["entry date differs"].append(
                f"{r.stock} {r.signal}: {r.entry} / {t.entry_session}"
            )
        elif r.open_at_end and not ex:
            if t.status == CLOSED:
                kinds["study open at the end, product closed"].append(f"{r.stock} {r.signal}")
            else:
                matched += 1
        elif r.open_at_end:  # the exit signal on the last bar: the study sold at that close
            if t.status == "EXIT_PENDING" and t.exit_reason == ex:
                matched += 1
            else:
                kinds["exit signalled on the last bar differs"].append(
                    f"{r.stock} {r.signal}: {ex} / {t.status} {t.exit_reason}"
                )
        elif got_exit != r.exit:
            kind = "exit date differs"
            if "STOP" in ex and paisa_stop(f, r.entry, r.exit, params):
                kind += ": the stop read on the fill rounded to the paisa (the product's book)"
            kinds[kind].append(
                f"{r.stock} {r.signal}: {r.exit} {ex} / {got_exit} {t.exit_reason} ({t.status})"
            )
        elif (t.exit_reason or "") != ex:
            prev = f.index[list(f.index).index(r.exit) - 1]
            kind = "exit reason differs"
            if sma_tie(f, prev, params):
                kind += ": a floating-point tie of the close and its SMA20"
            kinds[kind].append(f"{r.stock} {r.signal}: {ex or 'none'} / {t.exit_reason}")
        else:
            matched += 1
    held = defaultdict(list)  # the study's trades signalled before NIFTY's 200th session
    for r in tr.itertuples():
        if nifty_from is None or r.signal < nifty_from:
            held[r.stock].append((r.entry, r.exit))
    for (k, d), t in sorted(mine.items()):
        if a.start is None or d >= a.start:
            kind = "only in the product"
            if any(e <= d <= x for e, x in held.get(k, ())):
                kind += ": the study still held a trade signalled before NIFTY's 200th session"
            kinds[kind].append(f"{k} {d} ({t.status}, {t.exit_reason})")
    print(
        f"study trades compared: {len(tr)}; matched by entry date, exit date and reason: {matched}"
    )
    for kind, rows in sorted(kinds.items(), key=lambda x: -len(x[1])):
        print(f"MISMATCH {kind}: {len(rows)}")
        for row in rows[: a.show]:
            print(f"    {row}")
    print("sizes are not compared: the study bought the whole shares at or just under Rs 10,000,")
    print("the product buys the number nearest Rs 10,000 (rule R5).")
    print(f"{time.perf_counter() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
