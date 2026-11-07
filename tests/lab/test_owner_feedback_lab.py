"""Owner feedback of 2026-11-07 on the Backtest Lab.

- Trade times were shown in UTC ("04:15" for a 09:45 IST entry).
- The Matrix never loaded and the leaderboard could not be sorted by win rate or profit %.
- A single backtest could stay RUNNING forever after a "database is locked" error.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from onelot.clock import IST

UTC = dt.timezone.utc


def _locked_error() -> OperationalError:
    return OperationalError("UPDATE backtest_runs ...", {}, Exception("database is locked"))


class TestTimesAreIST:
    def test_a_utc_timestamp_from_the_database_is_shown_in_ist(self):
        from onelot.lab.jobs import _clean

        stored = dt.datetime(2026, 5, 29, 4, 15, tzinfo=UTC)  # how TZDateTime reads it back
        assert _clean(stored) == "2026-05-29T09:45:00+05:30"
        assert _clean(pd.Timestamp("2026-05-29 04:15", tz="UTC")) == "2026-05-29T09:45:00+05:30"

    def test_nested_values_and_naive_times_are_left_as_they_are(self):
        from onelot.lab.jobs import _clean

        naive = dt.datetime(2026, 5, 29, 9, 45)
        out = _clean({"trades": [{"entry_ts": dt.datetime(2026, 5, 29, 4, 15, tzinfo=UTC)}]})
        assert out["trades"][0]["entry_ts"].startswith("2026-05-29T09:45")
        assert _clean(naive) == "2026-05-29T09:45:00"

    def test_a_saved_run_serves_its_trades_in_ist(self, lab_engine):
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs, backtest_trades
        from onelot.lab.jobs import JobRunner

        entry = dt.datetime(2026, 5, 29, 9, 45, tzinfo=IST)
        with lab_engine.begin() as conn:
            conn.execute(
                backtest_runs.insert().values(
                    id=1, created_at=utcnow(), kind="single", status="DONE", config={}
                )
            )
            conn.execute(
                backtest_trades.insert().values(
                    run_id=1,
                    strategy="gap_fade",
                    instrument_key="RELIANCE",
                    side="BUY",
                    signal_ts=entry,
                    entry_ts=entry,
                    exit_ts=dt.datetime(2026, 5, 29, 15, 15, tzinfo=IST),
                    entry_price=1.0,
                    exit_price=1.0,
                )
            )
        run = JobRunner(lab_engine, workers=1).get_run(1)
        t = run["trades"][0]
        assert t["entry_ts"] == "2026-05-29T09:45:00+05:30"
        assert t["exit_ts"] == "2026-05-29T15:15:00+05:30"


def _runs(lab_engine, specs):
    from onelot.data.db import utcnow

    rows = []
    for i, (strat, inst, tf, trades, win, profit) in enumerate(specs, start=1):
        rows.append(
            {
                "id": i,
                "created_at": utcnow(),
                "kind": "single",
                "label": f"{strat} {inst}",
                "config": {
                    "strategy": strat,
                    "instrument_key": inst,
                    "timeframe": tf,
                    "params": {"n": i},
                },
                "status": "DONE",
                "progress": 1.0,
                "metrics": {
                    "trades": trades,
                    "win_rate": win,
                    "total_return_pct": profit,
                    "expectancy_r": 0.1,
                    "profit_factor": 1.3,
                    "net_pnl": 100.0,
                    "category": "STOCK",
                },
                "artifacts": {"chart": {"candles": [[0, 1, 1, 1, 1]] * 50}},
            }
        )
    with lab_engine.begin() as conn:
        from onelot.data.schema import backtest_runs as t

        conn.execute(t.insert(), rows)


class TestLeaderboardSorting:
    SPECS = [
        ("ema_cross", "SBIN", "15m", 40, 45.0, -1.5),
        ("gap_fade", "SBIN", "15m", 40, 60.0, 0.5),
        ("orb", "SBIN", "15m", 40, 30.0, 3.0),
        ("cpr_breakout", "SBIN", "15m", 40, None, None),
    ]

    def test_win_rate_high_first_and_low_first(self, lab_engine):
        from onelot.lab.sweep import leaderboard

        _runs(lab_engine, self.SPECS)
        desc = leaderboard(lab_engine, sort="win_rate", order="desc")
        asc = leaderboard(lab_engine, sort="win_rate", order="asc")
        assert [r["strategy"] for r in desc] == ["gap_fade", "ema_cross", "orb", "cpr_breakout"]
        assert [r["strategy"] for r in asc] == ["orb", "ema_cross", "gap_fade", "cpr_breakout"]

    def test_profit_percent_is_served_and_sortable_both_ways(self, lab_engine):
        from onelot.lab.sweep import leaderboard

        _runs(lab_engine, self.SPECS)
        desc = leaderboard(lab_engine, sort="profit_pct", order="desc")
        assert [r["profit_pct"] for r in desc] == [3.0, 0.5, -1.5, None]
        asc = leaderboard(lab_engine, sort="profit_pct", order="asc")
        assert [r["profit_pct"] for r in asc][:3] == [-1.5, 0.5, 3.0]

    def test_an_unknown_sort_is_refused(self, lab_engine):
        from onelot.lab.sweep import leaderboard

        with pytest.raises(ValueError):
            leaderboard(lab_engine, sort="artifacts")

    def test_the_matrix_answers_without_reading_run_artifacts(self, lab_engine, monkeypatch):
        """The Matrix used to read every run's artifacts (1.3 GB on the owner's database)."""
        from onelot.lab import sweep

        _runs(lab_engine, self.SPECS)
        seen: list[str] = []
        real_execute = None

        from sqlalchemy.engine import Connection

        real_execute = Connection.execute

        def spy(self, stmt, *a, **k):
            seen.append(str(stmt))
            return real_execute(self, stmt, *a, **k)

        monkeypatch.setattr(Connection, "execute", spy)
        m = sweep.matrix(lab_engine, min_trades=5)
        assert m["runs"] == 4 and len(m["strategies"]) == 4
        reads = [q for q in seen if "FROM backtest_runs" in q]
        assert reads and not any("artifacts" in q for q in reads)


class TestJobsNeverHangRunning:
    def test_runs_left_running_by_a_restart_are_marked_failed(self, lab_engine):
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs, data_jobs
        from onelot.lab.jobs import INTERRUPTED, JobRunner

        with lab_engine.begin() as conn:
            for i, st in enumerate(["RUNNING", "QUEUED", "DONE"], start=1):
                conn.execute(
                    backtest_runs.insert().values(
                        id=i, created_at=utcnow(), kind="single", status=st, config={}
                    )
                )
            conn.execute(
                data_jobs.insert().values(
                    id=1, kind="backfill", status="RUNNING", created_at=utcnow(), payload={}
                )
            )
        runner = JobRunner(lab_engine, workers=1)
        assert runner.recovered == 3
        with lab_engine.connect() as conn:
            st = dict(conn.execute(backtest_runs.select()).mappings().all()[0])
            runs = {
                r["id"]: (r["status"], r["message"])
                for r in conn.execute(backtest_runs.select()).mappings()
            }
            job = conn.execute(data_jobs.select()).mappings().first()
        assert st["finished_at"] is not None
        assert runs[1] == ("FAILED", INTERRUPTED) and runs[2] == ("FAILED", INTERRUPTED)
        assert runs[3][0] == "DONE"
        assert job["status"] == "FAILED"

    def test_a_locked_database_is_retried_until_the_write_lands(self, lab_engine, monkeypatch):
        from onelot.lab import jobs

        monkeypatch.setattr(jobs, "LOCK_RETRY_DELAYS", (0.0, 0.0, 0.0))
        monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def fn(conn):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _locked_error()
            return "landed"

        assert jobs._retry_write(lab_engine, fn, "test") == "landed"
        assert calls["n"] == 3

    def test_a_lock_that_never_clears_is_raised_not_swallowed(self, lab_engine, monkeypatch):
        from onelot.lab import jobs

        monkeypatch.setattr(jobs, "LOCK_RETRY_DELAYS", (0.0, 0.0))
        monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

        def fn(conn):
            raise _locked_error()

        with pytest.raises(OperationalError):
            jobs._retry_write(lab_engine, fn, "test")

    def test_a_failing_backtest_still_ends_failed_when_the_first_write_hits_a_lock(
        self, lab_engine, monkeypatch
    ):
        """The owner's run #8337 stayed RUNNING at 1 %: the error write itself hit the lock."""
        from onelot.data.schema import backtest_runs
        from onelot.lab import jobs

        monkeypatch.setattr(jobs, "LOCK_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0))
        monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
        runner = jobs.JobRunner(lab_engine, workers=1)
        with lab_engine.begin() as conn:
            conn.execute(
                backtest_runs.insert().values(
                    id=7, created_at=jobs.utcnow(), kind="single", status="QUEUED", config={}
                )
            )
        real = jobs._retry_write
        hits = {"n": 0}

        def flaky(engine, fn, what):
            hits["n"] += 1
            if hits["n"] == 1:  # the RUNNING write meets a lock once, then the retry lands
                calls = {"n": 0}

                def once(conn):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise _locked_error()
                    return fn(conn)

                return real(engine, once, what)
            return real(engine, fn, what)

        monkeypatch.setattr(jobs, "_retry_write", flaky)
        runner._run_backtest(7, {"strategy": "no_such_strategy", "instrument_key": "X"})
        with lab_engine.connect() as conn:
            row = conn.execute(backtest_runs.select().where(backtest_runs.c.id == 7)).first()
        assert row.status == "FAILED" and row.finished_at is not None


class TestBarsReadOnce:
    def test_a_coarse_frame_is_derived_from_the_widest_base_only(self, lab_engine, monkeypatch):
        from onelot.data import store

        from .conftest import make_intraday

        one = make_intraday(days=20, freq_min=1)
        five = make_intraday(days=3, freq_min=5, start="2026-09-24")  # a short, later base
        store.save_bars(lab_engine, "SBIN", "1m", one, source="dhan")
        store.save_bars(lab_engine, "SBIN", "5m", five, source="yahoo")
        reads: list[str] = []
        real = store._read_bars

        def spy(engine, series, res, start=None, end=None):
            reads.append(res)
            return real(engine, series, res, start, end)

        monkeypatch.setattr(store, "_read_bars", spy)
        out = store.load_bars(lab_engine, "SBIN", "15m")
        assert reads.count("1m") == 1 and "2m" not in reads
        assert out.index.min().date() == one.index.min().date()
        assert out.index.tz is not None and str(out.index.tz) == "Asia/Kolkata"
        assert out.index.min().strftime("%H:%M") == "09:15"
