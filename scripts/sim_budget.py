"""The Simulator's budgets (`.agile/stories/SIM-API-2.md`), measured on a store, READ-ONLY.

    PYTHONPATH=src .venv/bin/python scripts/sim_budget.py --store PATH [--runs 5]
        [--options PATH] [--start YYYY-MM-DD]

The store is opened with SQLite's `mode=ro` (nothing is ever written to it); the simulator's
own tables live in a temporary file that is deleted at the end. `--options` names an
expired-options store (also read-only); without it option premiums are modelled.

For NIFTY micro trend 3m, BANKNIFTY and two stocks with two strategies each it prints:
`GET /api/sim/options` cold and warm, `GET /api/sim/candles` cold and warm, the create
answer, the time to RUNNING cold (nothing kept) and warm (the same choice again), `advance`
p50 and p95 for chunks of 1 and 30 minutes, and what a thread ticking every 10 ms saw while
the builds ran (its worst gap and 99th percentile), each against its budget with PASS or MISS.

"Cold" is a new manager with empty caches (the store's pages may still sit in the operating
system's cache: a first run after a reboot can be slower). Times are wall clock, the BEST of
`--runs` runs (the machine is shared); the CPU time of the cold builds is printed beside.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine

from onelot.data import store as data_store
from onelot.sim.bars import StoreBars
from onelot.sim.manager import SimManager, default_deps
from onelot.store import init_database

#: (instrument, strategy, timeframe)
CASES: tuple[tuple[str, str, str], ...] = (
    ("NIFTY", "microtrend", "3m"),
    ("BANKNIFTY", "microtrend", "3m"),
    ("BANKNIFTY", "intraday_momentum", "5m"),
    ("RELIANCE", "vwap_pullback", "5m"),
    ("RELIANCE", "ema_cross", "5m"),
    ("HDFCBANK", "vwap_pullback", "5m"),
    ("HDFCBANK", "squeeze", "15m"),
)
#: what -> (budget, unit); every budget is "under"
BUDGETS: dict[str, tuple[float, str]] = {
    "options cold": (1000.0, "ms"),
    "options warm": (100.0, "ms"),
    "candles cold": (1000.0, "ms"),
    "candles warm": (200.0, "ms"),
    "create answer": (150.0, "ms"),
    "RUNNING cold": (2500.0, "ms"),
    "RUNNING warm": (400.0, "ms"),
    "advance 1 p95": (20.0, "ms"),
    "advance 30 p95": (20.0, "ms"),
    "ticker worst gap": (250.0, "ms"),
    "ticker p99 gap": (50.0, "ms"),
}


def read_only(path: str) -> Engine:
    uri = f"file:{Path(path).resolve()}?mode=ro"

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(uri, uri=True, check_same_thread=False)

    return create_engine("sqlite://", creator=connect)


class Ticker:
    """A thread that wakes every 10 ms and keeps the gaps between its wake-ups."""

    def __init__(self, every: float = 0.010) -> None:
        self.every = every
        self.gaps: list[float] = []
        self.on = False
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="budget-ticker", daemon=True)

    def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(self.every)
            now = time.perf_counter()
            if self.on:
                self.gaps.append((now - last) * 1000.0)
            last = now

    def __enter__(self) -> Ticker:
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._t.join(1.0)


def _forget_spans() -> None:
    with data_store._spans_lock:
        data_store._spans_cache.clear()


def _ms(fn: Callable[[], Any]) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    json.dumps(out, allow_nan=False)  # the route's answer is strict JSON
    return (time.perf_counter() - t0) * 1000.0, out


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q / 100.0 * (len(s) - 1))))]


def measure(
    store_path: str,
    runs: int = 5,
    options_path: str | None = None,
    start_day: str | None = None,
    cases: tuple[tuple[str, str, str], ...] = CASES,
    scratch: str | None = None,
    advances: int = 120,
) -> dict[str, Any]:
    ro = read_only(store_path)
    tmp = Path(tempfile.mkdtemp(prefix="sim_budget_", dir=scratch))
    sim_db = init_database(f"sqlite:///{tmp / 'sim.db'}")
    deps = default_deps(None, option_store_path=options_path or tmp / "no_options.db")
    found: dict[str, list[float]] = {k: [] for k in BUDGETS}
    cpu_cold: list[float] = []
    notes: list[str] = []

    def manager() -> SimManager:
        return SimManager(sim_db.engine, deps=deps, bars=StoreBars(ro), build="thread")

    try:
        probe = manager()
        have = {i["key"]: i for i in probe.options()["instruments"]}
        todo = [c for c in cases if have.get(c[0], {}).get("last")]
        for c in cases:
            if c not in todo:
                notes.append(f"{c[0]} has no stored bars: {c[1]} {c[2]} skipped")

        def start_for(key: str) -> str:
            days = probe.days(key)["days"]
            day = start_day if start_day in days else days[max(0, len(days) - 15)]
            return f"{day}T09:15:00+05:30"

        starts = {c[0]: start_for(c[0]) for c in todo}
        with Ticker() as ticker:
            for _ in range(runs):
                _forget_spans()
                m = manager()
                found["options cold"].append(_ms(m.options)[0])
                found["options warm"].append(min(_ms(m.options)[0] for _ in range(3)))
                for inst, strategy, tf in todo:
                    until = starts[inst]
                    t, _ = _ms(lambda inst=inst, tf=tf, until=until: m.candles(inst, tf, until, 300))
                    found["candles cold"].append(t)
                    found["candles warm"].append(
                        min(_ms(lambda inst=inst, tf=tf, u=until: m.candles(inst, tf, u, 300))[0]
                            for _ in range(3))
                    )  # fmt: skip
                for inst, strategy, tf in todo:
                    body = {"instrument": inst, "strategy": strategy, "timeframe": tf,
                            "mode": "past", "start": starts[inst], "params": {}}  # fmt: skip
                    c0 = time.process_time()
                    t0 = time.perf_counter()
                    ticker.on = True
                    t_answer, made = _ms(lambda body=body: m.create(body))
                    sid = made["session"]["id"]
                    while True:
                        st = m.entry(sid).state_status
                        if st != "BUILDING":
                            break
                        time.sleep(0.005)
                    running = (time.perf_counter() - t0) * 1000.0
                    m.builder.idle(30.0)
                    ticker.on = False
                    cpu_cold.append((time.process_time() - c0) * 1000.0)
                    if st == "FAILED":
                        notes.append(f"{inst} {strategy} {tf} FAILED: {m.entry(sid).detail}")
                        continue
                    found["create answer"].append(t_answer)
                    found["RUNNING cold"].append(running)
                    t0 = time.perf_counter()
                    t_answer2, again = _ms(lambda body=body: m.create(body))
                    sid2 = again["session"]["id"]
                    while m.entry(sid2).state_status == "BUILDING":
                        time.sleep(0.005)
                    found["RUNNING warm"].append((time.perf_counter() - t0) * 1000.0)
                    found["create answer"].append(t_answer2)
                    one, thirty = [], []
                    for k in range(advances):
                        n = 1 if k % 2 == 0 else 30
                        t, r = _ms(lambda sid=sid, n=n: m.act(sid, lambda s: s.advance(n, None)))
                        (one if n == 1 else thirty).append(t)
                        if r["over"]:
                            break
                    found["advance 1 p95"].append(_pct(one, 95))
                    found["advance 30 p95"].append(_pct(thirty, 95))
                    found.setdefault("advance 1 p50", []).append(_pct(one, 50))
                    found.setdefault("advance 30 p50", []).append(_pct(thirty, 50))
                gaps = list(ticker.gaps)
                ticker.gaps.clear()
                found["ticker worst gap"].append(max(gaps) if gaps else 0.0)
                found["ticker p99 gap"].append(_pct(gaps, 99))
                m.builder.idle(30.0)
    finally:
        sim_db.dispose()
        ro.dispose()
        for f in sorted(tmp.glob("*"), reverse=True):
            f.unlink()
        tmp.rmdir()
    return {"found": found, "cpu_cold_ms": cpu_cold, "notes": notes, "cases": todo,
            "starts": starts}  # fmt: skip


def report(got: dict[str, Any], runs: int) -> tuple[list[str], bool]:
    """Lines to print and whether every budget holds."""
    found = got["found"]
    lines = [f"Simulator budgets (best of {runs} runs; per-case values: their worst)"]
    ok_all = True
    for what in list(BUDGETS) + ["advance 1 p50", "advance 30 p50"]:
        vals = found.get(what) or []
        if not vals:
            continue
        # best of the runs per case, then the worst case: one value against the budget
        per_run = len(vals) // runs if runs and len(vals) >= runs else 1
        if per_run > 1:
            cases = [min(vals[i::per_run]) for i in range(per_run)]
            value = max(cases)
        else:
            value = min(vals)
        budget = BUDGETS.get(what)
        if budget is None:
            lines.append(f"  {what:<18} {value:8.1f} ms")
            continue
        ok = value < budget[0]
        ok_all &= ok
        spread = f"   (runs {min(vals):.1f} to {max(vals):.1f})" if len(vals) > 1 else ""
        lines.append(
            f"  {what:<18} {value:8.1f} ms   budget under {budget[0]:.0f} ms   "
            f"{'PASS' if ok else 'MISS'}{spread}"
        )
    cpu = got.get("cpu_cold_ms") or []
    if cpu:
        lines.append(
            f"  cold build CPU     {min(cpu):8.1f} to {max(cpu):.1f} ms of process CPU per session"
            " (all threads)"
        )
    lines.append("  cases: " + "; ".join(f"{i} {s} {t} from {got['starts'][i][:10]}"
                                         for i, s, t in got["cases"]))  # fmt: skip
    for n in got.get("notes") or []:
        lines.append(f"  note: {n}")
    return lines, ok_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, help="the bars store, opened read-only")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--options", default=None, help="an expired-options store (read-only)")
    ap.add_argument("--start", default=None, help="the day the sessions start (YYYY-MM-DD)")
    ap.add_argument("--scratch", default=None, help="where the temporary tables go")
    a = ap.parse_args(argv)
    got = measure(a.store, a.runs, a.options, a.start, scratch=a.scratch)
    lines, ok = report(got, a.runs)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
