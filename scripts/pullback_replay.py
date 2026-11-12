"""US-067 D13: replay stored bars session by session through the product's delivery path and
compare its trades with the reference trades. READ-ONLY on the store.

    PYTHONPATH=src .venv/bin/python scripts/pullback_replay.py --store PATH/onelot.db
    PYTHONPATH=src .venv/bin/python scripts/pullback_replay.py --make-test-store PATH/test.db

The store is opened through an SQLite URI with mode=ro, never through the app's engine or
settings, so nothing in it can be written. The product path is `onelot.delivery.runner
.DeliveryDesk`: its paper book lives in an in-memory database. For each session of the window
the desk is given the stored days before it and that session's one-minute bars as its live
bars, and polled after the close; fills come from the next session's first bar.

`--make-test-store` writes a small store from the test fixtures (daily bars, and one-minute
bars for the last sessions made from each day's open and close) to prove the script works.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from onelot.clock import IST, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402
from onelot.desk.history import rule_daily  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "pullback"


def read_only_engine(path: Path):  # noqa: ANN201
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


def minute_bars(path: Path, key: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """The stored one-minute bars of `key` from `start` to `end` (inclusive), read-only."""
    from onelot.data.store import _read_bars

    eng = read_only_engine(path)
    lo = pd.Timestamp(start).tz_localize(IST)
    hi = pd.Timestamp(end + dt.timedelta(days=1)).tz_localize(IST)
    return _read_bars(eng, key, "1m", start=lo, end=hi)


def replay(store: Path, sessions: int, expected: Path) -> int:
    from onelot.delivery.runner import DeliveryDesk

    desk_cfg = load_desk_config()
    keys = [inst.key for inst, _ in desk_cfg.delivery_slots()] + ["NIFTY"]
    eng = read_only_engine(store)
    now = dt.datetime.now(IST)
    t0 = time.perf_counter()
    full = {k: rule_daily(eng, k, now) for k in keys}  # what the desk reads once a day
    took = time.perf_counter() - t0
    print(f"rule_daily for {len(keys)} series: {took:.2f} s ({took / len(keys):.3f} s each)")
    # the replay reaches further back: the window plus the rule's history before its start
    back = int(sessions * 1.5) + 30
    full = {k: rule_daily(eng, k, now, fine_days=365 + back, daily_days=600 + back) for k in keys}
    nifty_days = sorted(set(pd.DatetimeIndex(full["NIFTY"][0].index).tz_convert(IST).date))
    window = nifty_days[-sessions:]
    print(f"window: {window[0]} to {window[-1]} ({len(window)} sessions)")
    minutes = {k: minute_bars(store, k, window[0], window[-1]) for k in keys}
    book_engine = create_engine("sqlite://")
    calendar = TradingCalendar.from_yaml(ROOT / "config" / "nse_holidays.yaml")
    desk = DeliveryDesk(
        book_engine, desk_cfg, CostConfig.load(ROOT / "config" / "costs.yaml"), SessionCfg(),
        calendar, notifier=_Quiet(), started_at=dt.datetime(2000, 1, 1, tzinfo=IST),
        background=False,
    )  # fmt: skip
    by_day = {}
    for k, frame in minutes.items():
        idx = pd.DatetimeIndex(frame.index).tz_convert(IST) if len(frame) else frame.index
        by_day[k] = {d: frame[idx.date == d] for d in window} if len(frame) else {}
    for i, day in enumerate(window):
        prev = window[i - 1] if i else None
        for k in keys:
            daily, last = full[k]
            didx = pd.DatetimeIndex(daily.index).tz_convert(IST).date
            desk._stored[k] = (daily[didx < day], {d: t for d, t in last.items() if d < day})
        desk._history_day = day

        def bars(key: str, day: dt.date = day, prev: dt.date | None = prev) -> pd.DataFrame:
            parts = [by_day[key].get(d) for d in (prev, day) if d is not None]
            parts = [p for p in parts if p is not None and len(p)]
            return pd.concat(parts) if parts else pd.DataFrame()

        desk.on_poll(
            dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 30), tzinfo=IST),
            bars,
            lambda key: None,
        )
    got = desk.book._select()
    for k, note in sorted(desk.notes.items()):
        print(f"note {k}: {note}")
    return compare(got, expected, window)


class _Quiet:
    def send(self, n, dedup_key=None):  # noqa: ANN001, ANN201
        return {}


def compare(got, expected: Path, window: list[dt.date]) -> int:  # noqa: ANN001
    ref = pd.read_csv(expected)
    first, last = window[1], window[-2]  # the first session is a start; the last has no next
    ref = ref[(ref.signal_date >= first.isoformat()) & (ref.signal_date <= last.isoformat())]
    mine = {}
    for t in got:
        if not (first <= t.signal_date <= last):
            continue
        entry_open = None if t.entry_paise is None else t.entry_paise / 100 / 1.0003
        exit_open = None if t.exit_paise is None else t.exit_paise / 100 / 0.9997
        mine[(t.series_key, t.signal_date.isoformat())] = (t, entry_open, exit_open)
    matched, differ = 0, []
    for r in ref.itertuples():
        m = mine.pop((r.stock, r.signal_date), None)
        if m is None:
            differ.append(f"only in the reference: {r.stock} {r.signal_date}")
            continue
        t, eo, xo = m
        same = (
            t.entry_session is not None
            and t.entry_session.isoformat() == r.entry_date
            and eo is not None
            and abs(eo - r.entry_open) < 0.01
        )
        if t.exit_paise is not None:
            same = same and t.exit_ts.date().isoformat() == r.exit_date
            same = same and xo is not None and abs(xo - r.exit_price) < 0.01
        elif r.exit_date <= window[-1].isoformat():
            same = False
        if same:
            matched += 1
        else:
            differ.append(
                f"differs: {r.stock} {r.signal_date} reference {r.entry_date} {r.entry_open} -> "
                f"{r.exit_date} {r.exit_price}; replay {t.entry_session} {eo} -> "
                f"{t.exit_ts} {xo} ({t.status})"
            )
    for (k, d), (t, _, _) in sorted(mine.items()):
        differ.append(f"only in the replay: {k} {d} ({t.status})")
    print(f"reference trades signalled in the window: {len(ref)}; matched: {matched}")
    for line in differ:
        print(line)
    print("RESULT:", "PASS" if not differ else f"{len(differ)} difference(s)")
    return 0 if not differ else 1


def make_test_store(path: Path, minute_sessions: int) -> None:
    """A small store from the fixtures: every daily bar, and one-minute bars for the last
    `minute_sessions` sessions that open at the day's open and close at its close."""
    from onelot.data.store import save_bars
    from onelot.store import init_database

    if path.exists():
        raise SystemExit(f"{path} exists: give a new path")
    db = init_database(f"sqlite:///{path}")
    for f in sorted(FIX.glob("daily_*.csv.gz")):
        key = f.name[len("daily_") : -len(".csv.gz")]
        with gzip.open(f, "rt") as fh:
            b = pd.read_csv(fh)
        idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in b["date"]])
        daily = b.drop(columns=["date"]).set_index(idx)
        save_bars(db.engine, key, "1d", daily, "fixture")
        last = "15:29" if key == "NIFTY" else "15:14"
        parts = []
        for d, row in zip(
            idx[-minute_sessions:], b.tail(minute_sessions).itertuples(), strict=True
        ):
            minutes = pd.date_range(
                d + pd.Timedelta("9h15min"),
                d + pd.Timedelta(last.replace(":", "h") + "min"),
                freq="1min",
            )
            path_ = np.linspace(row.open, row.close, len(minutes) + 1)
            o, c = path_[:-1], path_[1:]
            parts.append(pd.DataFrame(
                {"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c, "volume": 1.0},
                index=minutes,
            ))  # fmt: skip
        save_bars(db.engine, key, "1m", pd.concat(parts), "fixture")
    print(f"test store written: {path}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--store", type=Path, help="the store's SQLite file, opened read-only")
    ap.add_argument("--sessions", type=int, default=120, help="sessions replayed (default 120)")
    ap.add_argument("--expected", type=Path, default=FIX / "expected_trades.csv")
    ap.add_argument("--make-test-store", type=Path, help="write a small store from the fixtures")
    ap.add_argument("--minute-sessions", type=int, default=80)
    a = ap.parse_args()
    if a.make_test_store:
        make_test_store(a.make_test_store, a.minute_sessions)
        return 0
    if not a.store:
        ap.error("--store is required")
    return replay(a.store, a.sessions, a.expected)


if __name__ == "__main__":
    raise SystemExit(main())
