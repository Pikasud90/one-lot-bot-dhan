"""US-100: dump the PRODUCT's own trades of the wide list, trade by trade, with their tier.
Read-only: it runs the same replay as `scripts/pullback_wide_replay.py` (the evening round of
`onelot.delivery.wide.WideDesk`, its book in a temporary file, the inputs opened read-only)
and writes one CSV: stock, group, signal date, entry and exit session, status, exit reason,
depth and tier ("cap" = within the cap of 10, "test" = recorded only).

    PYTHONPATH=src nice -n 10 .venv/bin/python scripts/pullback_book_product.py \
        --daily MAIN/data/research/swing_2026-11-24/daily.sqlite \
        --nifty-store MAIN/data/onelot.db --nifty-csv MAIN/.../NIFTY.csv --out FILE.csv

`scripts/pullback_book.py` reads that CSV in its cross-check stage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402

from onelot.clock import SessionClock, TradingCalendar  # noqa: E402
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
from onelot.delivery.wide import StockDays, WideDesk, replay_rounds  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402


def _replay_module():
    spec = importlib.util.spec_from_file_location(
        "pullback_wide_replay", ROOT / "scripts" / "pullback_wide_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daily", type=Path, required=True)
    ap.add_argument("--nifty-store", type=Path)
    ap.add_argument("--nifty-csv", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--to", type=dt.date.fromisoformat, default=dt.date(2026, 11, 7))
    a = ap.parse_args()
    t0 = time.perf_counter()
    rep = _replay_module()

    groups = {
        s.key: s.group for s in load_universe(ROOT / "config" / "delivery_universe.yaml").stocks
    }
    con = read_only(a.daily)
    keys = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily ORDER BY symbol")]
    frames = {}
    for k in keys:
        f = prepare_daily(k, research_daily(con, k))
        frames[k] = f[[d <= a.to for d in f.index]]
    con.close()
    days = {k: StockDays.of(k, stamped(f)) for k, f in frames.items() if len(f)}
    nifty = rep.nifty_closes(a.nifty_store, a.nifty_csv, a.to)
    cfg = load_desk_config()
    params = PullbackParams.from_mapping(cfg.delivery_slots()[0][1].params)
    costs = CostConfig.load(ROOT / "config" / "costs.yaml")
    calendar = TradingCalendar.from_yaml(ROOT / "config" / "nse_holidays.yaml")
    tmp = Path(tempfile.mkdtemp(prefix="wide-book-")) / "book.db"
    stocks = [UniverseStock(k, k, k, "0", "NSE_EQ", groups.get(k, "new")) for k in days]
    wide = WideDesk(
        create_engine(f"sqlite:///{tmp}"), stocks, params, delivery_model(costs),
        depository_paise(costs), calendar, SessionClock(SessionCfg(), calendar),
        cap_open=cfg.wide.cap_open, min_bars=cfg.delivery.min_bars, background=False,
    )  # fmt: skip
    sessions = sorted({d for sd in days.values() for d in sd.dates})
    print(f"{len(days)} stocks, {len(sessions)} rounds, cap {cfg.wide.cap_open}; book {tmp}")

    def progress(n: int, s: dt.date) -> None:
        if n % 500 == 0:
            print(f"  round {n}: {s} ({time.perf_counter() - t0:.0f} s)")

    replay_rounds(wide, days, list(nifty.index), nifty.to_numpy(float), sessions, progress=progress)
    rows = [
        {
            "stock": t.series_key,
            "group": groups.get(t.series_key, "new"),
            "signal_date": t.signal_date,
            "entry_session": t.entry_session,
            "exit_session": t.exit_session,
            "status": t.status,
            "exit_reason": t.exit_reason,
            "depth_pct": t.depth_pct,
            "tier": t.tier,
            "late": t.late,
        }
        for t in wide.book.all()
    ]
    out = pd.DataFrame(rows)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(a.out, index=False)
    print(f"{len(out)} trades, {int((out.tier == 'cap').sum())} within the cap -> {a.out}")
    print(f"{time.perf_counter() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
