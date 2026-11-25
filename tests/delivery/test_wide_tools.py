"""US-099 acceptance 7 and 8: the import tool and the replay tool, run as the lead runs them.

The import tool, on a private store: it adds the daily bars of delivery-only stocks with the
study's repairs, never a desk instrument's, never a bar twice, and `--dry-run` writes nothing.
The replay tool runs the product's evening round over a small research file made from the
fixtures and finds the reference trades of those stocks, every one.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from onelot.clock import IST
from onelot.data.store import _read_bars, save_bars
from onelot.store import init_database
from onelot.store.schema import bars

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "pullback"


def research_file(path: Path, rows: dict[str, list[tuple]]) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE daily (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume REAL, PRIMARY KEY (symbol, date))"
    )
    for sym, rs in rows.items():
        con.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?)", [(sym, *r) for r in rs])
    con.commit()
    con.close()
    return path


def tool(*args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-W", "ignore", *args], cwd=ROOT, capture_output=True, text=True,
        timeout=300, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def all_bars(engine) -> list[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(bars.c.series_key, bars.c.ts, bars.c.close, bars.c.source).order_by(
                bars.c.series_key, bars.c.ts
            )
        )
        return [tuple(r) for r in rows]


def bar(day: str, close: float) -> tuple:
    return (day, close, close * 1.01, close * 0.99, close, 1000.0)


def test_the_import_adds_delivery_only_bars_once_and_never_a_desk_instruments(tmp_path):
    src = research_file(
        tmp_path / "daily.sqlite",
        {
            "RELIANCE": [bar("2026-11-02", 1500.0), bar("2026-11-03", 1510.0)],  # a desk stock
            # a corporate action of the study's list: before 2015-06-03, times its close ratio
            "ADANIENT": [
                bar("2015-06-01", 400.0),
                bar("2015-06-02", 410.0),
                bar("2015-06-03", 205.0),
                bar("2015-06-04", 207.0),
            ],  # fmt: skip
            "ACC": [bar("2026-11-02", 2000.0), bar("2026-11-03", 2010.0), bar("2026-11-04", 0.0)],
        },
    )
    db = init_database(f"sqlite:///{tmp_path / 'store.db'}")
    at = dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    one = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [0.0]}
    )
    save_bars(db.engine, "RELIANCE", "1d", one.set_index(pd.DatetimeIndex([at])), "dhan")
    save_bars(
        db.engine, "ACC", "1d", one.assign(close=1999.0).set_index(pd.DatetimeIndex([at])), "dhan"
    )
    before = all_bars(db.engine)
    store = str(tmp_path / "store.db")

    out = tool(
        "scripts/import_daily_universe.py", "--from", str(src), "--store", store, "--dry-run"
    )
    assert "ADANIENT" in out and "would add" in out and "nothing written" in out
    assert all_bars(db.engine) == before

    out = tool("scripts/import_daily_universe.py", "--from", str(src), "--store", store)
    assert "added 5 daily bars in all" in out
    after = all_bars(db.engine)
    assert [r for r in after if r[0] == "RELIANCE"] == [r for r in before if r[0] == "RELIANCE"]
    acc = _read_bars(db.engine, "ACC", "1d")
    assert list(acc["close"]) == [1999.0, 2010.0]  # the stored bar kept, the bad one left out
    ad = _read_bars(db.engine, "ADANIENT", "1d")
    k = 205.0 / 410.0
    assert list(ad["close"].round(6)) == [round(400.0 * k, 6), round(410.0 * k, 6), 205.0, 207.0]
    assert {t.time() for t in pd.DatetimeIndex(ad.index).tz_convert(IST)} == {dt.time(9, 15)}

    out = tool("scripts/import_daily_universe.py", "--from", str(src), "--store", store)
    assert "added 0 daily bars in all" in out and all_bars(db.engine) == after


def test_the_replay_tool_finds_the_reference_trades_on_a_small_fixture_file(tmp_path):
    keys = ["SBIN", "ITC", "LT"]
    rows = {}
    for k in keys:
        b = pd.read_csv(FIX / f"daily_{k}.csv.gz")
        rows[k] = list(
            b[["date", "open", "high", "low", "close", "volume"]].itertuples(index=False)
        )
    src = research_file(
        tmp_path / "daily.sqlite", {k: [tuple(r) for r in v] for k, v in rows.items()}
    )
    ref = pd.read_csv(FIX / "expected_trades.csv")
    ref = ref[ref.stock.isin(keys)]
    trades = pd.DataFrame(
        {
            "stock": ref.stock,
            "signal_date": pd.to_datetime(ref.signal_date),
            "entry_date": pd.to_datetime(ref.entry_date),
            "exit_date": pd.to_datetime(ref.exit_date),
            "open_at_end": ref.open_at_end.astype(bool),
        }
    )
    trades.to_pickle(tmp_path / "trades.pkl")
    n = pd.read_csv(FIX / "daily_NIFTY.csv.gz")[["date", "close"]]
    n.to_csv(tmp_path / "nifty.csv", index=False)
    out = tool(
        "scripts/pullback_wide_replay.py", "--daily", str(src), "--trades",
        str(tmp_path / "trades.pkl"), "--nifty-csv", str(tmp_path / "nifty.csv"),
        "--to", "2026-11-06",
    )  # fmt: skip
    assert (
        f"study trades compared: {len(ref)}; matched by entry date, exit date and reason: {len(ref)}"
        in out
    )
    assert "MISMATCH" not in out and "sizes are not compared" in out


def test_the_backtest_note_carries_the_book_level_reading():
    """US-100 (25 Nov): the page must not show "+0.77% a trade" without the book-level result:
    a ten-trade book did not beat NIFTY held. The shipped file and its generator agree."""
    from pathlib import Path

    from onelot.delivery.universe import load_universe

    root = Path(__file__).resolve().parents[2]
    note = str(load_universe(root / "config" / "delivery_universe.yaml").backtest["note"])
    assert "Not proven" in note and "market's drift" in note
    assert "Rs 1.30 lakh" in note and "Rs 4.15 lakh" in note and "NIFTY simply held" in note
    template = (root / "scripts" / "write_delivery_universe.py").read_text("utf-8")
    assert note.split("stocks, after costs. ")[1] in template
