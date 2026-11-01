"""IV percentile cold-start behaviour — v1 defect B5.

On day one there is no IV history. v1 would compute a percentile over a
one-element series and get 0 or 100, silently passing or blocking every trade.
These tests pin the three explicit behaviours that replaced that.
"""

from __future__ import annotations

import numpy as np
import pytest

from onelot.analytics.ivp import IVPercentileEngine, atm_iv_from_chain

HIST = list(np.linspace(0.10, 0.30, 200))


class TestSufficiency:
    def test_day_one_blocks_in_strict_mode(self):
        r = IVPercentileEngine("strict", 252, 60).compute([], 0.14)
        assert not r.usable and not r.sufficient
        ok, why = r.gate_passes(45.0)
        assert not ok
        assert "history" in why.lower()

    def test_thin_history_blocks_in_strict_mode(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST[:10], 0.12)
        assert not r.usable
        assert not r.gate_passes(45.0)[0]
        assert "10/60" in r.note

    def test_thin_history_passes_but_is_flagged_in_degraded_mode(self):
        # 0.101 sits near the bottom of the first ten observations, so the
        # gate would pass -- the point is that it passes WITH a loud flag.
        r = IVPercentileEngine("degraded", 252, 60).compute(HIST[:10], 0.101)
        assert r.usable and not r.sufficient
        assert "DEGRADED" in r.note, "the human must be told the number is weak"
        assert r.gate_passes(45.0)[0]

    def test_degraded_mode_still_blocks_genuinely_high_iv(self):
        r = IVPercentileEngine("degraded", 252, 60).compute(HIST[:10], 0.109)
        assert r.usable
        assert not r.gate_passes(45.0)[0], "degraded must not mean permissive"

    def test_off_mode_skips_the_gate_entirely(self):
        r = IVPercentileEngine("off", 252, 60).compute([], 0.14)
        assert r.gate_passes(45.0)[0]
        assert r.mode == "off"

    def test_sufficient_history_is_usable(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST, 0.14)
        assert r.sufficient and r.usable
        assert r.n_observations == 200


class TestPercentileMath:
    def test_low_iv_ranks_low(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST, 0.11)
        assert r.percentile < 10
        assert r.gate_passes(45.0)[0]

    def test_high_iv_ranks_high_and_blocks(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST, 0.29)
        assert r.percentile > 90
        assert not r.gate_passes(45.0)[0]

    def test_midpoint_is_about_fifty(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST, 0.20)
        assert 45 <= r.percentile <= 55

    def test_percentile_and_rank_are_both_reported(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST, 0.20)
        assert 0 <= r.percentile <= 100
        assert 0 <= r.iv_rank <= 100

    def test_lookback_window_is_honoured(self):
        long_hist = [0.50] * 300 + [0.10] * 100
        r = IVPercentileEngine("strict", 100, 60).compute(long_hist, 0.11)
        assert r.n_observations == 100
        assert r.percentile > 90, "only the recent low-IV window should count"

    def test_missing_current_iv_is_unusable(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST, float("nan"))
        assert not r.usable
        assert not r.gate_passes(45.0)[0]

    def test_nans_in_history_are_dropped(self):
        r = IVPercentileEngine("strict", 252, 60).compute(HIST[:80] + [float("nan")] * 5, 0.12)
        assert r.n_observations == 80


class TestAtmIvExtraction:
    def _chain(self):
        return [
            {"strike": 53_800.0, "iv": 0.150, "type": "CE"},
            {"strike": 53_800.0, "iv": 0.152, "type": "PE"},
            {"strike": 54_000.0, "iv": 0.140, "type": "CE"},
            {"strike": 54_000.0, "iv": 0.144, "type": "PE"},
            {"strike": 54_200.0, "iv": 0.139, "type": "CE"},
            {"strike": 54_200.0, "iv": 0.141, "type": "PE"},
        ]

    def test_picks_nearest_strike_and_averages_both_sides(self):
        got = atm_iv_from_chain(self._chain(), 54_030.0)
        assert got == pytest.approx((0.140 + 0.144) / 2)

    def test_handles_underlying_between_strikes(self):
        assert atm_iv_from_chain(self._chain(), 53_899.0) == pytest.approx(0.151, abs=1e-9)

    def test_empty_chain_is_nan(self):
        assert np.isnan(atm_iv_from_chain([], 54_000.0))

    def test_zero_ivs_are_ignored(self):
        rows = [
            {"strike": 54_000.0, "iv": 0.0, "type": "CE"},
            {"strike": 54_000.0, "iv": 0.144, "type": "PE"},
        ]
        assert atm_iv_from_chain(rows, 54_000.0) == pytest.approx(0.144)
