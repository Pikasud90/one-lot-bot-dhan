"""Tester (QA) probes for owner feedback OF-3 (Data) and OF-4 (Lab leaderboard / jobs).

The owner's words:

* "filter data based on its source i.e. Dhan or Yahoo, also based on timeframes 5m 15m 30m 60m 1d"
  and "for all the currency and many other stocks we have only 40 days of data";
* "leaderboard sortable asc/desc by win rate and profit %";
* "single backtest stalled; walk-forward and optimise didn't work; Matrix empty".

Everything runs on throw-away SQLite databases; nothing touches the owner's data/onelot.db.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from onelot.data import store
from onelot.data.store import LAB_TIMEFRAMES, effective_coverage


# ── fixtures ─────────────────────────────────────────────────────────────────────────
def intraday(days: int, freq_min: int, start: str, base: float = 1000.0) -> pd.DataFrame:
    """NSE-session bars (09:15-15:30 IST) as a deterministic random walk."""
    rng = np.random.default_rng(7)
    idx: list[pd.Timestamp] = []
    day = pd.Timestamp(start, tz="Asia/Kolkata")
    while len({d.date() for d in idx}) < days:
        if day.weekday() < 5:
            idx.extend(
                pd.date_range(
                    day + pd.Timedelta(hours=9, minutes=15),
                    periods=375 // freq_min,
                    freq=f"{freq_min}min",
                )
            )
        day += pd.Timedelta(days=1)
    close = base * np.exp(np.cumsum(rng.normal(0, 0.0015, len(idx))))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, 0.0015, len(idx))) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.integers(10_000, 50_000, len(idx)).astype(float),
        },
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


def row(key, res, source, first, last, bars=100):
    return {
        "series_key": key,
        "resolution": res,
        "source": source,
        "first": first,
        "last": last,
        "bars": bars,
    }


def by(rows):
    return {(r["series_key"], r["timeframe"]): r for r in rows}


# ══ OF-3: what the Lab can load ═══════════════════════════════════════════════════════════
class TestEffectiveCoverageEdges:
    def test_a_series_with_only_two_minute_bars_offers_30m_60m_1d_and_not_5m_or_15m(
        self, lab_engine
    ):
        """2m divides 30 and 60 but not 5 or 15; the claim must match what load_bars delivers."""
        store.save_bars(lab_engine, "ONLY2", "2m", intraday(5, 2, "2026-10-05"), "yahoo")
        coverage = store.coverage_report(lab_engine)
        got = by(effective_coverage(coverage.to_dict("records")))
        assert {tf for (k, tf) in got if k == "ONLY2"} == {"30m", "60m", "1d"}
        assert store.load_bars(lab_engine, "ONLY2", "15m").empty
        assert not store.load_bars(lab_engine, "ONLY2", "30m").empty

    def test_the_claimed_history_is_what_load_bars_really_serves(self, lab_engine):
        """Mixed corpus: long Dhan 1m, short later Yahoo 5m, medium 60m, long daily."""
        store.save_bars(lab_engine, "MIX", "1m", intraday(30, 1, "2026-06-01"), "dhan")
        store.save_bars(lab_engine, "MIX", "5m", intraday(12, 5, "2026-07-06"), "yahoo")
        store.save_bars(lab_engine, "MIX", "60m", intraday(40, 60, "2026-05-04"), "yahoo")
        store.save_bars(lab_engine, "MIX", "1d", intraday(80, 375, "2026-03-02"), "dhan")
        coverage = store.coverage_report(lab_engine)
        rows = by(effective_coverage(coverage.to_dict("records")))
        for tf in LAB_TIMEFRAMES:
            claim = rows.get(("MIX", tf))
            assert claim is not None, tf
            loaded = store.load_bars(lab_engine, "MIX", tf)
            assert not loaded.empty, tf
            assert str(loaded.index.min().date()) == claim["first"][:10], (tf, claim)
            assert str(loaded.index.max().date()) == claim["last"][:10], (tf, claim)

    def test_a_resolution_the_loader_does_not_know_is_not_counted_as_a_base(self, lab_engine):
        """12m is not in RESOLUTION_ORDER, so load_bars never reads it: the Data tab must not
        promise history that only such a row holds."""
        store.save_bars(lab_engine, "ODD", "60m", intraday(5, 60, "2026-10-12"), "yahoo")
        store.save_bars(lab_engine, "ODD", "12m", intraday(5, 5, "2026-09-07"), "yahoo")
        coverage = store.coverage_report(lab_engine)
        claim = by(effective_coverage(coverage.to_dict("records")))[("ODD", "60m")]
        loaded = store.load_bars(lab_engine, "ODD", "60m")
        assert str(loaded.index.min().date()) == claim["first"][:10], claim

    def test_a_garbage_resolution_row_never_takes_the_whole_data_tab_down(self):
        rows = [
            row("X", "5m", "yahoo", "2026-08-31", "2026-10-27 15:25"),
            row("X", "abc", "yahoo", "2026-01-01", "2026-10-27 15:25"),
        ]
        got = by(effective_coverage(rows))
        assert ("X", "15m") in got


class TestLoadBarsKeepsTheNewestBars:
    def test_a_window_after_the_widest_base_ends_still_loads_from_the_newer_base(self, lab_engine):
        """Five years of Dhan 1m bars that stop at the token's expiry, and Yahoo 5m bars that
        carry on: a Lab window that starts after the 1m bars end must still get the 5m ones
        (it did before the single-base read)."""
        one = intraday(20, 1, "2026-08-03")  # ends 2026-08-28
        five = intraday(30, 5, "2026-08-17")  # runs on to 2026-09-25
        store.save_bars(lab_engine, "X", "1m", one, "dhan")
        store.save_bars(lab_engine, "X", "5m", five, "yahoo")
        start = pd.Timestamp("2026-09-01", tz="Asia/Kolkata")
        out = store.load_bars(lab_engine, "X", "15m", start=start)
        assert not out.empty, "bars exist from 2026-09-01 to 2026-09-25 in the 5m base"
        assert out.index.min() >= start
        assert out.index.max().date() == five.index.max().date()

    def test_a_window_that_spans_both_bases_has_no_gap_and_no_duplicate_bar(self, lab_engine):
        one = intraday(20, 1, "2026-08-03")
        five = intraday(30, 5, "2026-08-17")
        store.save_bars(lab_engine, "X", "1m", one, "dhan")
        store.save_bars(lab_engine, "X", "5m", five, "yahoo")
        out = store.load_bars(lab_engine, "X", "15m")
        assert out.index.is_unique and out.index.is_monotonic_increasing
        assert out.index.min().date() == one.index.min().date()
        assert out.index.max().date() == five.index.max().date()
        days = pd.Series(out.index.date).value_counts()
        assert days.min() >= 20, "every session carries (almost) all 25 fifteen-minute bars"


class TestTheCoverageCache:
    def test_a_returned_frame_cannot_corrupt_the_cached_answer(self, lab_engine):
        store.save_bars(lab_engine, "X", "5m", intraday(3, 5, "2026-10-05"), "yahoo")
        first = store.coverage_report(lab_engine)
        first.loc[:, "series_key"] = "HACKED"
        first.drop(first.index, inplace=True)
        again = store.coverage_report(lab_engine)
        assert list(again["series_key"]) == ["X"]

    def test_a_scan_older_than_the_ttl_is_taken_again(self, lab_engine, monkeypatch):
        store.save_bars(lab_engine, "X", "5m", intraday(3, 5, "2026-10-05"), "yahoo")
        calls = []
        real = store._coverage_scan
        monkeypatch.setattr(store, "_coverage_scan", lambda e: calls.append(1) or real(e))
        clock = {"t": 1000.0}
        monkeypatch.setattr(store.time, "monotonic", lambda: clock["t"])
        store.coverage_report(lab_engine)
        clock["t"] += store.COVERAGE_TTL_S - 1
        store.coverage_report(lab_engine)
        assert len(calls) == 1
        clock["t"] += 2
        store.coverage_report(lab_engine)
        assert len(calls) == 2

    def test_an_empty_corpus_reports_nothing_and_the_effective_view_is_empty(self, lab_engine):
        assert store.coverage_report(lab_engine).empty
        assert effective_coverage([]) == []


# ══ OF-4: the leaderboard ═════════════════════════════════════════════════════════════════
def save_runs(engine, specs):
    from onelot.data.db import utcnow
    from onelot.data.schema import backtest_runs

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
                    "expectancy_r": 0.1 * (i % 3 - 1),
                    "profit_factor": 1.3,
                    "net_pnl": 100.0 * i,
                    "category": "STOCK",
                },
                "artifacts": {},
            }
        )
    with engine.begin() as conn:
        conn.execute(backtest_runs.insert(), rows)


SPECS = [
    ("ema_cross", "SBIN", "15m", 40, 45.0, -1.5),
    ("gap_fade", "SBIN", "15m", 40, 60.0, 0.5),
    ("orb", "SBIN", "15m", 40, 30.0, 3.0),
    ("cpr_breakout", "SBIN", "15m", 3, 100.0, 9.0),  # too few trades: unranked
]


class TestLeaderboardSortEdges:
    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_unranked_runs_never_lead_the_list_whichever_way_the_score_is_read(
        self, lab_engine, order
    ):
        """Other columns keep a run without the figure last, either way; so must the score
        (the first page of 'low score first' must not be 700 unranked rows)."""
        from onelot.lab.sweep import leaderboard

        save_runs(lab_engine, SPECS)
        rows = leaderboard(lab_engine, sort="score", order=order)
        scores = [r["score"] for r in rows]
        ranked = [s for s in scores if s is not None]
        assert scores[: len(ranked)] == ranked, scores

    @pytest.mark.parametrize("sort", ["win_rate", "profit_pct", "trades", "net_pnl"])
    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_the_limit_applies_after_the_sort(self, lab_engine, sort, order):
        from onelot.lab.sweep import leaderboard

        save_runs(lab_engine, SPECS)
        full = leaderboard(lab_engine, sort=sort, order=order, min_trades=1)
        top = leaderboard(lab_engine, sort=sort, order=order, min_trades=1, limit=2)
        assert [r["run_id"] for r in top] == [r["run_id"] for r in full][:2]

    def test_the_http_leaderboard_sorts_filters_and_refuses_unknown_columns(self, lab_engine):
        import warnings

        from onelot.api.server import create_app

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient
        save_runs(lab_engine, SPECS)
        engine = MagicMock()
        engine.db = SimpleNamespace(engine=lab_engine)
        client = TestClient(create_app(engine, "t"))

        def board(**q):
            return client.get("/api/lab/leaderboard", params={"min_trades": 1, **q})

        low = board(sort="win_rate", order="asc").json()
        assert [r["strategy"] for r in low["rows"]] == [
            "orb",
            "ema_cross",
            "gap_fade",
            "cpr_breakout",
        ]
        assert (low["sort"], low["order"]) == ("win_rate", "asc")
        only = board(strategies="gap_fade, orb", sort="profit_pct", order="desc").json()
        assert [r["strategy"] for r in only["rows"]] == ["orb", "gap_fade"]
        assert board(sort="artifacts").status_code == 422
        assert board(order="sideways").status_code == 422
        # the list is JSON-safe whatever the metrics held
        json.dumps(low, allow_nan=False)


# ══ OF-4: jobs that never hang, shown in IST ══════════════════════════════════════════════
class TestJobsAreRecoveredOnceNotRepeatedly:
    def test_a_second_runner_on_the_same_database_does_not_fail_the_first_ones_live_jobs(
        self, lab_engine
    ):
        """The CLI (`python -m onelot sweep`) builds its own JobRunner on the SAME database file
        while the app is running: it must not mark the app's RUNNING run FAILED ('interrupted:
        the app restarted') - the app did not restart."""
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs
        from onelot.lab.jobs import JobRunner

        JobRunner(lab_engine, workers=1)  # the app's runner, built at start
        with lab_engine.begin() as conn:  # a run the app is executing right now
            conn.execute(
                backtest_runs.insert().values(
                    id=1, created_at=utcnow(), kind="single", status="RUNNING", config={}
                )
            )
        JobRunner(lab_engine, workers=1)  # a second process starts a runner (CLI sweep)
        with lab_engine.connect() as conn:
            status = conn.execute(backtest_runs.select()).mappings().one()["status"]
        assert status == "RUNNING"


# ══ OF-5, then the paper test (owner's go-ahead 2026-11-07) ═══════════════════════════════
# Until 2026-11-07 the desk ran the owner's seven on 15-minute bars. The owner then approved a
# paper test of the cross-instrument retune's closest candidates, with the retune's exact
# settings (docs/research/CROSS_RETUNE_2026-11-07.md): this list replaced the seven.
# On 2026-11-11 the owner's multi-timeframe Supertrend rule joined it as a second index slot
# (data/research/mtf_2026-11-11/NOTES.md); the retune's three slots are unchanged.
# 2026-11-13 (owner, US-068): the desk runs two rules, the micro-trend rule on the index
# options and the pullback rule on the stocks (a delivery rule, not a Lab strategy, so not on
# the Lab's list). The retune's slots and supertrend_mtf left desk.yaml (git keeps them).
OWNERS_PRIMARY = ("microtrend",)
RESEARCH_STOCKS = {
    "RELIANCE", "MM", "SBIN", "HDFCBANK", "ITC", "TMPV", "MARUTI", "ICICIBANK", "HEROMOTOCO",
    "INFY", "TCS", "AXISBANK", "KOTAKBANK", "LT", "BAJFINANCE", "BHARTIARTL", "TATASTEEL",
    "SUNPHARMA",
}  # fmt: skip
# The owner's micro-trend rule as configured (US-068); `evidence` is the alert's line, not a
# setting, and is compared apart
PAPER_TEST = {
    ("INDEX", "microtrend", "3m"): {
        "st_period": 14, "st_mult": 1.7, "exit_count": 3, "stop_buffer_atr": 3.0,
    },
}  # fmt: skip
# fmt: skip


class TestThePrimaryStrategies:
    def test_the_desk_runs_exactly_the_paper_test_settings(self):
        from onelot.desk.config import load_desk_config
        from onelot.strategies.registry import REGISTRY, get_strategy

        cfg = load_desk_config()
        assert cfg.primary_strategies() == OWNERS_PRIMARY
        assert {s.key for s in cfg.stocks} == RESEARCH_STOCKS  # Adani Green excluded
        assert len(cfg.indices) == 5
        assert cfg.option_expiry == "monthly" and cfg.option_roll_days == 3
        for inst in cfg.instruments:
            # US-068 (2026-11-13): one live slot each, no paused ones; the delivery pullback
            # slot is covered by tests/delivery.
            assert all(s.enabled for s in inst.strategies)
            got = {
                (inst.kind, s.name, s.timeframe): {
                    k: v for k, v in s.params.items() if k != "evidence"
                }
                for s in inst.strategies
                if s.product == "intraday"
            }
            want = {k: v for k, v in PAPER_TEST.items() if k[0] == inst.kind}
            assert got == want, inst.key
        assert set(OWNERS_PRIMARY) <= set(REGISTRY)
        for (_, name, tf), params in PAPER_TEST.items():  # every key is a real parameter
            strat = get_strategy(name, {**params, "timeframe": tf})
            assert all(getattr(strat.p, k) == v for k, v in params.items())

    def test_the_lab_offers_the_same_list_and_filters_the_leaderboard_by_it(self, lab_engine):
        import warnings

        from onelot.api.server import create_app
        from onelot.desk.config import load_desk_config

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient
        saved = [(n, "SBIN", "30m", 40, 50.0, 1.0) for n in (*OWNERS_PRIMARY, "orb")]
        save_runs(lab_engine, saved)
        engine = MagicMock()
        engine.db = SimpleNamespace(engine=lab_engine)
        engine.desk = SimpleNamespace(cfg=load_desk_config())
        client = TestClient(create_app(engine, "t"))
        focus = client.get("/api/lab/focus").json()
        assert tuple(focus["primary_strategies"]) == OWNERS_PRIMARY
        listed = ",".join(focus["primary_strategies"])
        assert len(listed) < 400, "the UI sends the whole list as one query value"
        res = client.get("/api/lab/leaderboard", params={"min_trades": 1, "strategies": listed})
        assert res.status_code == 200
        assert {r["strategy"] for r in res.json()["rows"]} == set(OWNERS_PRIMARY)
