"""Tester (QA) cycle 2, Sprint 02a: adversarial probes of the Lab and Data fixes.

* BUG-7  recovery of leftover runs only after the single-instance lock is held;
* BUG-8  load_bars: differential against "every bar any base can give";
* BUG-9  leaderboard: figures that are missing or not finite trail, for every sort and order;
* BUG-10 effective_coverage names a base exactly when load_bars would read it;
* BUG-12 the single-flight coverage scan: a failed scan, a write during a scan, two databases.

Throw-away SQLite databases only; nothing touches the owner's data/onelot.db.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time

import numpy as np
import pandas as pd
import pytest
import us001_helpers as H
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.clock import IST
from onelot.data import store
from onelot.data.resample import RESOLUTION_ORDER, can_derive, resample_bars
from onelot.data.store import effective_coverage


def bars(days: int, freq: int, first_day: str, base: float = 1000.0) -> pd.DataFrame:
    """`days` NSE sessions (09:15-15:30 IST) of `freq`-minute bars from `first_day`."""
    rng = np.random.default_rng(5)
    idx: list[pd.Timestamp] = []
    day = pd.Timestamp(first_day, tz=IST)
    while len({d.date() for d in idx}) < days:
        if day.weekday() < 5:
            idx += list(
                pd.date_range(
                    day + pd.Timedelta(hours=9, minutes=15), periods=375 // freq, freq=f"{freq}min"
                )
            )
        day += pd.Timedelta(days=1)
    close = base * np.exp(np.cumsum(rng.normal(0, 0.001, len(idx))))
    return pd.DataFrame(
        {"open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 100.0},
        index=pd.DatetimeIndex(idx, name="ts"),
    )


@pytest.fixture
def lab_engine(tmp_path):
    from onelot.data.db import get_database, use_database
    from onelot.store import init_database

    db = init_database(f"sqlite:///{tmp_path / 'lab.db'}")
    use_database(db)
    yield db.engine
    db.dispose()
    get_database.__globals__["_db"] = None


# ══ BUG-7: recovery waits for the lock ════════════════════════════════════════════════════
class TestRecoveryWaitsForTheLock:
    def test_building_the_engine_touches_no_run_and_start_recovers_after_the_lock(
        self, tmp_path, monkeypatch
    ):
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs

        with H.real_engine(tmp_path) as eng:
            with eng.db.engine.begin() as conn:
                conn.execute(
                    backtest_runs.insert().values(
                        id=1, created_at=utcnow(), kind="single", status="RUNNING", config={}
                    )
                )
            order: list[str] = []
            monkeypatch.setattr(eng.lock, "acquire", lambda: order.append("lock"))

            def recover():
                order.append("recover")
                raise RuntimeError("stop here: the order is what is under test")

            monkeypatch.setattr(eng.lab_jobs, "recover_interrupted", recover)
            with pytest.raises(RuntimeError, match="stop here"):
                eng.start()
            assert order == ["lock", "recover"]
            with eng.db.engine.connect() as conn:
                status = conn.execute(backtest_runs.select()).mappings().one()["status"]
            assert status == "RUNNING", "constructing the engine must not recover anything"

    def test_a_second_launch_that_cannot_take_the_lock_leaves_the_live_jobs_alone(
        self, tmp_path, monkeypatch
    ):
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs, data_jobs

        with H.real_engine(tmp_path) as eng:
            with eng.db.engine.begin() as conn:
                conn.execute(
                    backtest_runs.insert().values(
                        id=1, created_at=utcnow(), kind="optimize", status="RUNNING", config={}
                    )
                )
                conn.execute(
                    data_jobs.insert().values(
                        id=1, kind="backfill", status="RUNNING", created_at=utcnow(), payload={}
                    )
                )

            def held():
                raise RuntimeError("another instance holds the lock")

            monkeypatch.setattr(eng.lock, "acquire", held)
            with pytest.raises(RuntimeError, match="another instance"):
                eng.start()
            with eng.db.engine.connect() as conn:
                runs = [r["status"] for r in conn.execute(backtest_runs.select()).mappings()]
                jobs = [r["status"] for r in conn.execute(data_jobs.select()).mappings()]
            assert runs == ["RUNNING"] and jobs == ["RUNNING"]

    def test_the_cli_sweeps_runner_leaves_live_runs_alone_and_the_apps_start_still_recovers(
        self, lab_engine
    ):
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs
        from onelot.lab.jobs import INTERRUPTED, JobRunner

        with lab_engine.begin() as conn:
            for i, status in enumerate(("RUNNING", "QUEUED", "DONE"), start=1):
                conn.execute(
                    backtest_runs.insert().values(
                        id=i, created_at=utcnow(), kind="single", status=status, config={}
                    )
                )
        JobRunner(lab_engine, workers=1)  # `python -m onelot sweep` builds one of these
        with lab_engine.connect() as conn:
            seen = {r["id"]: r["status"] for r in conn.execute(backtest_runs.select()).mappings()}
        assert seen == {1: "RUNNING", 2: "QUEUED", 3: "DONE"}
        runner = JobRunner(lab_engine, workers=1)
        assert runner.recover_interrupted() == 2  # what Engine.start() calls
        with lab_engine.connect() as conn:
            rows = {
                r["id"]: (r["status"], r["message"])
                for r in conn.execute(backtest_runs.select()).mappings()
            }
        assert rows[1] == ("FAILED", INTERRUPTED) and rows[3][0] == "DONE"
        assert runner.recover_interrupted() == 0, "asked again it finds nothing left"


# ══ BUG-8: load_bars against the previous algorithm ═════════════════════════════════════
def every_bar_any_base_gives(engine, key: str, tf: str, start=None, end=None) -> pd.DatetimeIndex:
    """What a loader that missed nothing would return: the union of the timestamps of the
    direct bars and of the bars derived from EVERY base that can make `tf`.

    (The algorithm of c19b9d8 merged a later base only when it was LONGER than what it already
    had, so two touching bases of equal size lost the second: this oracle is the intent.)"""
    avail = store.available_resolutions(engine, key)
    found = pd.DatetimeIndex([], tz=IST)
    if tf in avail:
        found = found.union(store._read_bars(engine, key, tf, start, end).index)
    for base in [r for r in RESOLUTION_ORDER if r in avail and r != tf and can_derive(r, tf)]:
        derived = resample_bars(store._read_bars(engine, key, base, start, end), tf)
        found = found.union(derived.index)
    return found


SESSIONS = pd.bdate_range("2026-08-03", periods=24, tz=IST)


@settings(
    max_examples=30,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    o1=st.integers(0, 6),
    n1=st.integers(1, 6),
    o5=st.integers(0, 12),
    n5=st.integers(1, 7),
    window=st.one_of(st.none(), st.tuples(st.integers(0, 16), st.integers(0, 16))),
    tf=st.sampled_from(["15m", "30m", "60m"]),
)
def test_load_bars_keeps_every_bar_any_of_two_whole_session_bases_can_give(
    lab_engine, o1, n1, o5, n5, window, tf
):
    """Two bases made of whole sessions (overlapping, touching or apart), any window of whole
    days: every bar either base can give, no duplicate, in order."""
    key = f"P{o1}{n1}{o5}{n5}{tf}"
    store.save_bars(lab_engine, key, "1m", bars(n1, 1, str(SESSIONS[o1].date())), "dhan")
    store.save_bars(lab_engine, key, "5m", bars(n5, 5, str(SESSIONS[o5].date())), "yahoo")
    start = end = None
    if window is not None:
        lo, hi = sorted(window)
        start = SESSIONS[lo]
        end = SESSIONS[hi] + pd.Timedelta(days=1)
    got = store.load_bars(lab_engine, key, tf, start=start, end=end)
    want = every_bar_any_base_gives(lab_engine, key, tf, start, end)
    assert got.index.is_unique and got.index.is_monotonic_increasing
    assert list(got.index) == list(want), (o1, n1, o5, n5, window, tf)


class TestLoadBarsEdges:
    def test_a_window_inside_the_widest_base_never_reads_the_newer_one_for_more_than_its_tail(
        self, lab_engine, monkeypatch
    ):
        store.save_bars(lab_engine, "E", "1m", bars(10, 1, "2026-08-03"), "dhan")
        store.save_bars(lab_engine, "E", "5m", bars(14, 5, "2026-08-10"), "yahoo")
        reads: list[tuple[str, object, object]] = []
        real = store._read_bars

        def spy(engine, series, res, start=None, end=None):
            reads.append((res, start, end))
            return real(engine, series, res, start, end)

        monkeypatch.setattr(store, "_read_bars", spy)
        out = store.load_bars(lab_engine, "E", "15m")
        assert out.index.min().date() == dt.date(2026, 8, 3)
        assert [r[0] for r in reads] == ["1m", "5m"], reads
        assert reads[1][1] is not None, "the 5m base is read from where the 1m base stops"

    def test_a_window_after_every_base_and_one_before_every_base_are_empty(self, lab_engine):
        store.save_bars(lab_engine, "E", "1m", bars(4, 1, "2026-08-03"), "dhan")
        store.save_bars(lab_engine, "E", "5m", bars(6, 5, "2026-08-05"), "yahoo")
        after = store.load_bars(lab_engine, "E", "15m", start=pd.Timestamp("2026-10-01", tz=IST))
        before = store.load_bars(
            lab_engine,
            "E",
            "15m",
            start=pd.Timestamp("2026-06-01", tz=IST),
            end=pd.Timestamp("2026-06-30", tz=IST),
        )
        assert after.empty and before.empty

    def test_a_window_that_starts_after_the_widest_base_serves_only_the_newer_bases_bars(
        self, lab_engine
    ):
        one = bars(5, 1, "2026-08-03")
        five = bars(12, 5, "2026-08-10")
        store.save_bars(lab_engine, "E", "1m", one, "dhan")
        store.save_bars(lab_engine, "E", "5m", five, "yahoo")
        start = pd.Timestamp("2026-08-12", tz=IST)
        out = store.load_bars(lab_engine, "E", "30m", start=start)
        assert not out.empty and out.index.min() >= start
        assert out.index.max().date() == five.index.max().date()
        days = pd.Series(out.index.date).value_counts()
        assert (days == 13).all(), "every session is 13 thirty-minute buckets (09:15 .. 15:15)"

    @pytest.mark.parametrize(
        "kind", ["string", "date", "naive datetime", "aware timestamp", "aware datetime"]
    )
    def test_the_late_window_works_whatever_type_the_start_comes_in(self, lab_engine, kind):
        store.save_bars(lab_engine, "E", "1m", bars(3, 1, "2026-08-03"), "dhan")
        store.save_bars(lab_engine, "E", "5m", bars(8, 5, "2026-08-10"), "yahoo")
        start = {
            "string": "2026-08-12",
            "date": dt.date(2026, 8, 12),
            "naive datetime": dt.datetime(2026, 8, 12),
            "aware timestamp": pd.Timestamp("2026-08-12", tz=IST),
            "aware datetime": dt.datetime(2026, 8, 12, tzinfo=IST),
        }[kind]
        out = store.load_bars(lab_engine, "E", "15m", start=start)
        assert not out.empty and out.index.min().date() >= dt.date(2026, 8, 12)

    def test_a_start_in_the_middle_of_a_session_after_the_widest_base_still_loads(self, lab_engine):
        store.save_bars(lab_engine, "E", "1m", bars(3, 1, "2026-08-03"), "dhan")
        store.save_bars(lab_engine, "E", "5m", bars(8, 5, "2026-08-10"), "yahoo")
        start = pd.Timestamp("2026-08-12 11:07", tz=IST)
        out = store.load_bars(lab_engine, "E", "15m", start=start)
        assert not out.empty
        assert out.index.is_unique and out.index.is_monotonic_increasing


# ══ BUG-9: the leaderboard's missing and non-finite figures ═══════════════════════════════
FIGURES = st.one_of(
    st.none(),
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
    st.integers(-5, 5),
    st.floats(-5, 5, allow_nan=False, allow_infinity=False),
)


def save_figures(engine, rows) -> None:
    from onelot.data.db import utcnow

    values = []
    for i, (trades, win, profit, er, pf, net) in enumerate(rows, start=1):
        values.append(
            {
                "id": i,
                "created_at": utcnow(),
                "kind": "single",
                "label": f"run {i}",
                "config": {
                    "strategy": f"s{i}",
                    "instrument_key": "SBIN",
                    "timeframe": "15m",
                    "params": {"n": i},
                },
                "status": "DONE",
                "progress": 1.0,
                "metrics": {
                    "trades": trades,
                    "win_rate": win,
                    "total_return_pct": profit,
                    "expectancy_r": er,
                    "profit_factor": pf,
                    "net_pnl": net,
                    "category": "STOCK",
                },
                "artifacts": {},
            }
        )
    with engine.begin() as conn:
        from onelot.data.schema import backtest_runs as table

        conn.execute(table.insert(), values)


@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    st.lists(
        st.tuples(st.integers(0, 60), FIGURES, FIGURES, FIGURES, FIGURES, FIGURES),
        min_size=1,
        max_size=12,
    ),
    st.sampled_from(["score", "win_rate", "profit_pct", "profit_factor", "expectancy_r"]),
    st.sampled_from(["asc", "desc"]),
)
def test_a_missing_or_non_finite_figure_always_trails_and_the_rest_are_in_order(
    lab_engine, rows, sort, order
):
    from onelot.data.schema import backtest_runs
    from onelot.lab.sweep import leaderboard

    with lab_engine.begin() as conn:
        conn.execute(backtest_runs.delete())
    save_figures(lab_engine, rows)
    got = leaderboard(lab_engine, sort=sort, order=order, min_trades=1, limit=100)
    assert len(got) == len(rows), "no run is lost"
    values = [r.get(sort) for r in got]

    def known(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    flags = [known(v) for v in values]
    assert flags == sorted(flags, reverse=True), values  # every known figure before every unknown
    figures = [v for v in values if known(v)]
    ordered = sorted(figures, reverse=order == "desc")
    assert figures == ordered, (sort, order, values)


# ══ BUG-10: a base exactly when load_bars reads it ═══════════════════════════════════════
STORABLE = ["1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m", "45m", "60m", "120m", "240m", "1d"]
TARGETS = ["2m", "5m", "10m", "15m", "30m", "60m", "1d"]


@pytest.mark.parametrize("stored", STORABLE)
def test_a_stored_resolution_is_a_base_for_a_timeframe_exactly_when_load_bars_serves_it(
    lab_engine, stored
):
    freq = 375 if stored == "1d" else int(stored[:-1])
    store.save_bars(lab_engine, "ONLY", stored, bars(3, freq, "2026-09-07"), "yahoo")
    coverage = store.coverage_report(lab_engine).to_dict("records")
    offered = {r["timeframe"] for r in effective_coverage(coverage, tuple(TARGETS))}
    served = {tf for tf in TARGETS if not store.load_bars(lab_engine, "ONLY", tf).empty}
    assert offered == served, (stored, offered ^ served)


# ══ BUG-12: the single-flight scan ════════════════════════════════════════════════════════
class TestTheSingleFlightScan:
    def test_a_scan_that_fails_does_not_wedge_the_lock_and_the_next_caller_scans(
        self, lab_engine, monkeypatch
    ):
        store.save_bars(lab_engine, "X", "5m", bars(2, 5, "2026-10-05"), "yahoo")
        real = store._coverage_scan
        state = {"n": 0}

        def flaky(engine):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("scan failed")
            return real(engine)

        monkeypatch.setattr(store, "_coverage_scan", flaky)
        with pytest.raises(RuntimeError, match="scan failed"):
            store.coverage_report(lab_engine)
        assert list(store.coverage_report(lab_engine)["series_key"]) == ["X"]
        assert not store._coverage_scan_lock.locked()

    def test_callers_waiting_on_a_scan_that_fails_each_get_a_scan_or_an_error_never_a_hang(
        self, lab_engine, monkeypatch
    ):
        store.save_bars(lab_engine, "X", "5m", bars(2, 5, "2026-10-05"), "yahoo")
        real = store._coverage_scan
        state = {"n": 0}
        lock = threading.Lock()

        def flaky(engine):
            with lock:
                state["n"] += 1
                n = state["n"]
            time.sleep(0.15)
            if n == 1:
                raise RuntimeError("scan failed")
            return real(engine)

        monkeypatch.setattr(store, "_coverage_scan", flaky)
        results: list[object] = []

        def call():
            try:
                results.append(len(store.coverage_report(lab_engine)))
            except RuntimeError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert not any(t.is_alive() for t in threads) and len(results) == 5
        assert results.count("scan failed") == 1 and results.count(1) == 4
        assert state["n"] == 2, "one failed scan, one good scan shared by the other four"

    def test_a_bar_written_during_a_scan_makes_the_next_answer_fresh(self, lab_engine, monkeypatch):
        store.save_bars(lab_engine, "X", "5m", bars(2, 5, "2026-10-05"), "yahoo")
        real = store._coverage_scan
        entered, release = threading.Event(), threading.Event()
        calls = {"n": 0}

        def slow(engine):
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                assert release.wait(10)
            return real(engine)

        monkeypatch.setattr(store, "_coverage_scan", slow)
        first: list[pd.DataFrame] = []
        t = threading.Thread(target=lambda: first.append(store.coverage_report(lab_engine)))
        t.start()
        assert entered.wait(10)
        store.save_bars(lab_engine, "Y", "5m", bars(2, 5, "2026-10-05"), "dhan")  # during the scan
        release.set()
        t.join(10)
        assert not t.is_alive()
        later = store.coverage_report(lab_engine)
        assert set(later["series_key"]) == {"X", "Y"}, "the stale scan was not kept as fresh"

    def test_two_databases_are_never_given_each_others_coverage(self, tmp_path):
        from onelot.store import init_database

        a = init_database(f"sqlite:///{tmp_path / 'a.db'}")
        b = init_database(f"sqlite:///{tmp_path / 'b.db'}")
        try:
            store.save_bars(a.engine, "AAA", "5m", bars(2, 5, "2026-10-05"), "yahoo")
            store.save_bars(b.engine, "BBB", "5m", bars(2, 5, "2026-10-05"), "dhan")
            assert list(store.coverage_report(a.engine)["series_key"]) == ["AAA"]
            assert list(store.coverage_report(b.engine)["series_key"]) == ["BBB"]
            assert list(store.coverage_report(a.engine)["series_key"]) == ["AAA"]
        finally:
            a.dispose()
            b.dispose()
