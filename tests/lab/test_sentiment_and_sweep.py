"""Sentiment gate, walk-forward window planning, sweep planning and the leaderboard."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from onelot.lab.optimizer import MIN_WF_SESSIONS, plan_windows
from onelot.lab.sentiment import apply_gate, bias_at, normalise, parse_import
from onelot.lab.sweep import leaderboard, plan, rank_score, summarise


class TestPlanWindows:
    def test_requested_windows_kept_when_they_fit(self):
        is_, oos, note = plan_windows(120, 40, 10)
        assert (is_, oos) == (40, 10) and "as requested" in note

    def test_old_defaults_on_a_small_corpus_are_resized_not_zero_folds(self):
        is_, oos, note = plan_windows(37, 40, 10)  # the live failure: 0 folds
        folds = (37 - is_) // oos
        assert folds >= 3 and is_ >= 2 * oos and "auto-sized" in note

    @pytest.mark.parametrize("n", [12, 15, 20, 37, 60, 250, 1200])
    def test_always_at_least_three_folds(self, n):
        is_, oos, _ = plan_windows(n)
        assert (n - is_) // oos >= 3 and is_ > 0 and oos > 0

    def test_too_little_data_is_an_error_not_silence(self):
        with pytest.raises(ValueError, match="at least"):
            plan_windows(MIN_WF_SESSIONS - 1)


class TestSentiment:
    def test_normalise_maps_zero_one_scale(self):
        s = pd.Series([0.0, 0.25, 0.5, 0.75, 1.0])
        assert normalise(s).tolist() == [-1.0, -0.5, 0.0, 0.5, 1.0]
        t = pd.Series([-0.8, 0.2, 0.9])
        assert normalise(t).tolist() == [-0.8, 0.2, 0.9]

    def test_bias_uses_only_scores_at_or_before_bar_close(self):
        idx = pd.date_range("2026-10-01 09:15", periods=6, freq="5min", tz="Asia/Kolkata")
        scores = pd.Series(
            [0.9, -0.9],
            index=pd.DatetimeIndex(
                [
                    pd.Timestamp(
                        "2026-10-01 09:00", tz="Asia/Kolkata"
                    ),  # before the session: applies
                    pd.Timestamp(
                        "2026-10-01 09:31", tz="Asia/Kolkata"
                    ),  # applies from the bar closing >= 09:31
                ]
            ),
        )
        b = bias_at(scores, idx, 5, threshold=0.2)
        # bars close at 09:20, 09:25, 09:30, 09:35, 09:40, 09:45
        assert b.tolist() == [1, 1, 1, -1, -1, -1]

    def test_stale_scores_are_ignored(self):
        idx = pd.date_range("2026-10-20 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
        scores = pd.Series(
            [0.9], index=pd.DatetimeIndex([pd.Timestamp("2026-10-01", tz="Asia/Kolkata")])
        )
        assert bias_at(scores, idx, 5, max_age_days=5).tolist() == [0, 0, 0]

    @pytest.mark.parametrize(
        "mode,bias,exp_long,exp_short",
        [
            ("off", 1, True, True),
            ("follow", -1, False, True),
            ("follow", 0, True, True),
            ("follow", 1, True, False),
            ("strict", 0, False, False),
            ("strict", 1, True, False),
            ("contrarian", 1, False, True),
            ("contrarian", -1, True, False),
        ],
    )
    def test_gate_modes(self, mode, bias, exp_long, exp_short):
        f = pd.DataFrame(
            {"long_entry": [True], "short_entry": [True]},
            index=[pd.Timestamp("2026-10-01 09:15", tz="Asia/Kolkata")],
        )
        g = apply_gate(f, np.array([bias]), mode)
        assert (
            bool(g["long_entry"].iloc[0]) is exp_long
            and bool(g["short_entry"].iloc[0]) is exp_short
        )
        assert bool(g["blk_sentiment_long"].iloc[0]) is (not exp_long)

    def test_parse_import_handles_headers_dates_and_scales(self):
        rows = parse_import(
            "date,score,conf\n2024-01-02,0.8,0.9\n2024-01-03 10:00,0.5\n# comment\n\n2024-01-04;0.1;0.4"
        )
        assert len(rows) == 3
        assert rows[0]["ts"].hour == 9 and rows[0]["ts"].minute == 15  # date-only -> 09:15 IST
        assert rows[1]["ts"].hour == 10
        assert [round(r["score"], 2) for r in rows] == [
            0.6,
            0.0,
            -0.8,
        ]  # 0..1 scale mapped to -1..1
        assert rows[2]["confidence"] == 0.4


class TestSweep:
    def test_plan_is_the_full_grid_over_the_desk_watchlist(self):
        combos = plan(["ema_cross", "orb"], None, ["5m", "15m"])
        insts = {c["instrument_key"] for c in combos}
        # the paper test's watchlist (2026-11-07): 5 index options + the retune's 18 stocks
        assert (
            "BANKNIFTY_OPT" in insts and "RELIANCE" in insts and "MM" in insts and len(insts) == 23
        )
        assert "ADANIGREEN" not in insts
        assert len(combos) == 2 * 23 * 2
        with pytest.raises(ValueError):
            plan(["nope"], ["SBIN"], ["5m"])

    def test_rank_score_prefers_more_evidence_and_penalises_losers(self):
        good = {"trades": 50, "expectancy_r": 0.2, "profit_factor": 1.5}
        few = {"trades": 5, "expectancy_r": 0.9, "profit_factor": 3.0}
        loser = {"trades": 50, "expectancy_r": 0.2, "profit_factor": 0.9}
        assert rank_score(few) == float("-inf")
        assert rank_score(good) > rank_score(loser)

    def test_leaderboard_reads_saved_runs(self, lab_engine):
        from onelot.data.db import utcnow
        from onelot.data.schema import backtest_runs

        def row(i, strat, inst, tf, trades, er, pf, kind="single", sweep=None):
            cfg = {"strategy": strat, "instrument_key": inst, "timeframe": tf, "params": {}}
            if sweep:
                cfg["_sweep_id"] = sweep
            return {
                "id": i,
                "created_at": utcnow(),
                "kind": kind,
                "label": f"{strat} {inst}",
                "config": cfg,
                "status": "DONE",
                "progress": 1.0,
                "metrics": {
                    "trades": trades,
                    "expectancy_r": er,
                    "profit_factor": pf,
                    "net_pnl": er * trades * 100,
                    "category": "STOCK",
                },
            }

        with lab_engine.begin() as conn:
            conn.execute(
                backtest_runs.insert(),
                [
                    row(1, "ema_cross", "SBIN", "5m", 40, 0.1, 1.3),
                    row(
                        2, "ema_cross", "SBIN", "5m", 45, 0.3, 1.6, sweep=9
                    ),  # newer duplicate config -> wins
                    row(3, "orb", "RELIANCE", "15m", 4, 1.0, 5.0),  # too few trades -> unranked
                    row(4, "orb", "RELIANCE", "15m", 60, 0.05, 1.1, kind="walkforward"),
                ],
            )
            conn.execute(
                backtest_runs.insert().values(
                    id=5,
                    created_at=utcnow(),
                    kind="single",
                    status="FAILED",
                    config={"strategy": "x", "instrument_key": "Y", "timeframe": "5m"},
                )
            )
        rows = leaderboard(lab_engine)
        assert [r["run_id"] for r in rows][:2] == [2, 4]
        assert rows[0]["sweep_id"] == 9 and rows[0]["trades"] == 45
        assert any(r["run_id"] == 3 and r["score"] is None for r in rows)
        assert not any(r["run_id"] == 1 for r in rows)  # superseded by run 2
        assert [r["run_id"] for r in leaderboard(lab_engine, sweep_id=9)] == [2]
        assert [r["run_id"] for r in leaderboard(lab_engine, kind="walkforward")] == [4]
        s = summarise(rows)
        assert s["best_by_instrument"]["SBIN"]["run_id"] == 2 and s["positive"] == 1
