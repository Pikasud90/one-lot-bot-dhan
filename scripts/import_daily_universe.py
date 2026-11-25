"""US-099: copy the research file's daily bars of the wide list's DELIVERY-ONLY stocks into the
app's store, as daily bars, through the app's own writer. Never an order; nothing is fetched.

    PYTHONPATH=src .venv/bin/python scripts/import_daily_universe.py \
        --from "MAIN/data/research/swing_2026-11-24/daily.sqlite" --store PATH/onelot.db [--dry-run]

- Which stocks: those of `config/delivery_universe.yaml` that are not desk instruments
  (`config/desk.yaml`, indices and stocks). A desk instrument's series is never touched.
- Which bars: every bar of the research file with the study's repairs (prices that are missing
  or not positive left out, five corporate actions rescaled: `onelot.delivery.universe`), for
  a date the store does not have yet for that series. A bar the store has is never written
  again, so a second run adds nothing.
- How: `onelot.data.store.save_bars` (the app's writer), 500 bars a commit, so the desk's own
  writes are never held up for long. Daily bars are stamped 09:15 IST, as Dhan's are.
- `--dry-run` opens the store read-only (URI mode=ro) and writes nothing: it prints what it
  would add. The research file is always opened read-only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from onelot.clock import IST  # noqa: E402
from onelot.delivery.universe import (  # noqa: E402
    load_universe,
    prepare_daily,
    read_only,
    research_daily,
    stamped,
)
from onelot.desk.config import load_desk_config  # noqa: E402

BATCH = 500


def stored_dates(engine, key: str) -> set:  # noqa: ANN001
    from onelot.data.store import _read_bars

    frame = _read_bars(engine, key, "1d")
    idx = pd.DatetimeIndex(frame.index)
    if len(idx) and idx.tz is not None:
        idx = idx.tz_convert(IST)
    return set(idx.date)


def run(src: Path, store: Path, dry: bool, universe: Path, desk: Path | None) -> int:
    from onelot.data.store import save_bars

    if not store.exists():
        raise SystemExit(f"{store}: no such store")
    cfg = load_desk_config(desk)
    desk_keys = {i.key for i in cfg.instruments}
    stocks = load_universe(universe).delivery_only(desk_keys)
    if dry:
        uri = f"file:{store.resolve()}?mode=ro"
        engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    else:
        from onelot.store.db import Database

        engine = Database(f"sqlite:///{store.resolve()}").engine  # the app's pragmas, no DDL
    con = read_only(src)
    have_src = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily")}
    total, done = 0, 0
    print(f"{len(stocks)} delivery-only stocks; desk instruments never touched: {len(desk_keys)}")
    for s in stocks:
        if s.key in desk_keys:  # belt and braces: delivery_only already left them out
            continue
        if s.key not in have_src:
            print(f"  {s.key:12s} not in the research file")
            continue
        frame = prepare_daily(s.key, research_daily(con, s.key))
        have = stored_dates(engine, s.key)
        new = frame[[d not in have for d in frame.index]]
        if not len(new):
            print(f"  {s.key:12s} nothing to add ({len(have)} daily bars stored)")
            continue
        span = f"{new.index[0]} to {new.index[-1]}"
        total += len(new)
        if dry:
            print(f"  {s.key:12s} would add {len(new):5d} daily bars, {span}")
            continue
        bars = stamped(new)
        for i in range(0, len(bars), BATCH):
            save_bars(engine, s.key, "1d", bars.iloc[i : i + BATCH], "research")
        done += len(new)
        print(f"  {s.key:12s} added {len(new):5d} daily bars, {span}")
    con.close()
    verb = "would add" if dry else "added"
    print(
        f"{verb} {total if dry else done} daily bars in all"
        + (" (dry run: nothing written)" if dry else "")
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--from", dest="src", type=Path, required=True, help="research file (read only)"
    )
    ap.add_argument("--store", type=Path, required=True, help="the app's store (SQLite file)")
    ap.add_argument("--dry-run", action="store_true", help="write nothing: say what would be added")
    ap.add_argument("--universe", type=Path, default=ROOT / "config" / "delivery_universe.yaml")
    ap.add_argument("--desk", type=Path, default=None, help="desk.yaml (default: config/desk.yaml)")
    a = ap.parse_args()
    return run(a.src, a.store, a.dry_run, a.universe, a.desk)


if __name__ == "__main__":
    raise SystemExit(main())
