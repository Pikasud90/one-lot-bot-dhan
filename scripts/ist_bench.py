"""US-096: what the app's IST zone object costs. Run it on the commit before and after.

    PYTHONPATH=src .venv/bin/python -W ignore scripts/ist_bench.py --store PATH/onelot.db

It imports `onelot` from PYTHONPATH (so the same file times another checkout: point
PYTHONPATH at that checkout's src) and prints, best of five (wall and CPU seconds):
  - normalize / tz_convert / .time / .hour on 36,000 one-minute IST stamps;
  - one strategy's signal generation (the Simulator's `sim/engine.build_signals`, micro trend
    3m) on the last 120 stored days of NIFTY one-minute bars;
  - one Lab backtest (`lab/runner.run_backtest`, micro trend 3m on NIFTY options, the last
    120 days, a fresh data cache each run so the store read and the cleaning are counted);
and, for each, the worst gap of a thread that ticks every 10 ms meanwhile: how long the rest
of the app (poll, feed, API) would have been frozen.

READ-ONLY: the store is opened through an SQLite URI with mode=ro; nothing is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from sqlalchemy import create_engine

from onelot.clock import IST

RUNS = 5


def ro_engine(path: Path):  # noqa: ANN201
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


class Ticker:
    """A thread that wants to run every 10 ms; `worst()` is its longest wait since `reset()`."""

    def __init__(self) -> None:
        self._gap = 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(0.01)
            now = time.perf_counter()
            self._gap = max(self._gap, now - last)
            last = now

    def reset(self) -> None:
        time.sleep(0.03)
        self._gap = 0.0

    def worst(self) -> float:
        return self._gap

    def stop(self) -> None:
        self._stop.set()
        self._t.join()


def best(fn: Callable[[], object], ticker: Ticker, runs: int = RUNS) -> tuple[float, float, float]:
    """(best wall seconds, best CPU seconds of this process, worst tick gap) over `runs`."""
    walls, cpus = [], []
    ticker.reset()
    for _ in range(runs):
        w0, c0 = time.perf_counter(), time.process_time()
        fn()
        walls.append(time.perf_counter() - w0)
        cpus.append(time.process_time() - c0)
    return min(walls), min(cpus), ticker.worst()


def line(name: str, r: tuple[float, float, float]) -> str:
    return f"{name:<44} wall {r[0]:9.4f} s   cpu {r[1]:9.4f} s   worst tick gap {r[2]:7.3f} s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", type=Path, required=True, help="the SQLite bar store (read-only)")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--runs", type=int, default=RUNS)
    a = ap.parse_args()
    logging.disable(logging.WARNING)
    import onelot

    print(f"onelot from {Path(onelot.__file__).parent}; IST = {IST!r}")
    print(f"best of {a.runs}; the machine is shared, so CPU time is the figure to compare")
    ticker = Ticker()

    utc = pd.date_range("2026-01-01 03:45", periods=36_000, freq="min", tz="UTC")
    idx = utc.tz_convert(IST)
    print(line("36,000 stamps: normalize()", best(idx.normalize, ticker, a.runs)))
    print(line("36,000 stamps: tz_convert(IST) from UTC", best(lambda: utc.tz_convert(IST).normalize(), ticker, a.runs)))  # fmt: skip
    print(line("36,000 stamps: .time", best(lambda: idx.time, ticker, a.runs)))
    print(line("36,000 stamps: .hour", best(lambda: idx.hour, ticker, a.runs)))

    from onelot.lab.runner import BacktestConfig, DataCache, run_backtest
    from onelot.sim.bars import StoreBars
    from onelot.sim.engine import build_signals
    from onelot.strategies.registry import get_strategy

    engine = ro_engine(a.store)
    src = StoreBars(engine)
    last = src.minute_range("NIFTY")
    assert last is not None, "no NIFTY one-minute bars in the store"
    end = pd.Timestamp(last[1]).tz_convert(IST).normalize() + pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=int(a.days * 7 / 5) + 30)
    minute = src.minute_bars("NIFTY", start, end)
    sessions = sorted(set(minute.index.normalize()))[-a.days :]
    minute = minute[minute.index >= sessions[0]]
    src.daily_bars("NIFTY")
    params = {"st_period": 14, "st_mult": 1.7, "exit_count": 3, "stop_buffer_atr": 3.0}

    def signals() -> None:
        strat = get_strategy("microtrend", {**params, "timeframe": "3m"})
        f, _ = build_signals(strat, None, "NIFTY", "3m", minute, src)  # type: ignore[arg-type]
        assert f is not None and len(f)

    n_days = len({t.date() for t in minute.index})
    print(line(f"generate(): micro trend 3m, {n_days} days of 1m", best(signals, ticker, a.runs)))

    first = sessions[0].date().isoformat()
    final = sessions[-1].date().isoformat()
    trades: list[int] = []

    def backtest() -> None:
        cfg = BacktestConfig(
            "microtrend", "NIFTY_OPT", params=params, timeframe="3m", start=first, end=final
        )
        res = run_backtest(cfg, DataCache(engine))
        trades.append(len(res.trades))

    print(line(f"Lab backtest: micro trend 3m NIFTY_OPT {first}..{final}", best(backtest, ticker, a.runs)))  # fmt: skip
    print(f"(the backtest made {trades[0]} trades each run: {sorted(set(trades))})")
    ticker.stop()
    print(f"done {dt.datetime.now(IST):%Y-%m-%d %H:%M:%S %Z}")


if __name__ == "__main__":
    main()
