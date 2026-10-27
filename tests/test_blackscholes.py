"""Black-76 validation.

Checked against closed-form identities (put-call parity, delta bounds, the
F*n(d1)=K*n(d2) theta symmetry) rather than magic constants, so the tests stay
valid if the implementation is refactored.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from onelot.analytics.blackscholes import (
    cross_check_delta,
    greeks,
    greeks_from_quote,
    implied_forward,
    implied_vol,
    price,
    year_fraction,
)

F = 54_000.0
T = 20 / 365.0
SIG = 0.14


class TestPricing:
    def test_put_call_parity(self):
        """C - P = e^{-rT}(F - K). If this fails, everything downstream is wrong."""
        for k in (52_000.0, 54_000.0, 56_000.0):
            for r in (0.0, 0.065):
                c = price(F, k, T, SIG, r, "CE")
                p = price(F, k, T, SIG, r, "PE")
                assert c - p == pytest.approx(math.exp(-r * T) * (F - k), abs=1e-6)

    def test_price_exceeds_intrinsic(self):
        k = 53_000.0
        assert price(F, k, T, SIG, 0.0, "CE") > (F - k)

    def test_price_increases_with_volatility(self):
        prev = -1.0
        for s in (0.05, 0.10, 0.20, 0.40):
            cur = price(F, 54_000.0, T, s, 0.0, "CE")
            assert cur > prev
            prev = cur

    def test_at_expiry_collapses_to_intrinsic(self):
        assert price(F, 53_000.0, 0.0, SIG, 0.0, "CE") == pytest.approx(1000.0)
        assert price(F, 55_000.0, 0.0, SIG, 0.0, "CE") == pytest.approx(0.0)
        assert price(F, 55_000.0, 0.0, SIG, 0.0, "PE") == pytest.approx(1000.0)

    def test_zero_vol_collapses_to_intrinsic(self):
        assert price(F, 53_000.0, T, 0.0, 0.0, "CE") == pytest.approx(1000.0, abs=1e-6)


class TestGreeks:
    def test_put_delta_is_negative(self):
        """*** v1 defect A8 *** — `0.60 <= delta <= 0.70` matches ZERO puts."""
        g = greeks(F, 55_000.0, T, SIG, 0.0, "PE")
        assert g.delta < 0, "put delta must be negative"
        assert 0 < abs(g.delta) < 1

    def test_call_delta_is_positive_and_bounded(self):
        g = greeks(F, 53_000.0, T, SIG, 0.0, "CE")
        assert 0 < g.delta < 1

    def test_a_realistic_65_delta_put_exists_and_is_negative(self):
        """The exact contract v1's filter would have silently rejected forever."""
        target, best, best_k = 0.65, None, None
        for k in np.arange(52_000, 56_500, 100.0):
            g = greeks(F, float(k), T, SIG, 0.0, "PE")
            if best is None or abs(abs(g.delta) - target) < abs(abs(best.delta) - target):
                best, best_k = g, float(k)
        assert best.delta < 0
        assert 0.60 <= abs(best.delta) <= 0.70
        assert best_k > F, "a 0.65-delta put is in the money, i.e. strike above forward"

    def test_delta_parity(self):
        """delta_call - delta_put = e^{-rT}"""
        for r in (0.0, 0.065):
            c = greeks(F, 54_000.0, T, SIG, r, "CE").delta
            p = greeks(F, 54_000.0, T, SIG, r, "PE").delta
            assert c - p == pytest.approx(math.exp(-r * T), abs=1e-9)

    def test_theta_is_negative_for_long_options(self):
        assert greeks(F, 54_000.0, T, SIG, 0.0, "CE").theta < 0
        assert greeks(F, 54_000.0, T, SIG, 0.0, "PE").theta < 0

    def test_theta_per_hour_is_consistent_with_per_day(self):
        g = greeks(F, 54_000.0, T, SIG, 0.0, "CE")
        assert g.theta_per_hour == pytest.approx(g.theta / 24.0, rel=1e-9)

    def test_gamma_and_vega_positive_and_peak_atm(self):
        atm = greeks(F, 54_000.0, T, SIG, 0.0, "CE")
        otm = greeks(F, 58_000.0, T, SIG, 0.0, "CE")
        assert atm.gamma > 0 and atm.vega > 0
        assert atm.gamma > otm.gamma
        assert atm.vega > otm.vega

    def test_deep_itm_call_delta_approaches_one(self):
        assert greeks(F, 30_000.0, T, SIG, 0.0, "CE").delta > 0.99

    def test_deep_otm_put_delta_approaches_zero(self):
        assert abs(greeks(F, 30_000.0, T, SIG, 0.0, "PE").delta) < 0.01

    def test_gamma_identical_for_call_and_put(self):
        c = greeks(F, 54_000.0, T, SIG, 0.0, "CE")
        p = greeks(F, 54_000.0, T, SIG, 0.0, "PE")
        assert c.gamma == pytest.approx(p.gamma)
        assert c.vega == pytest.approx(p.vega)


class TestImpliedVol:
    @pytest.mark.parametrize("k", [51_000.0, 53_000.0, 54_000.0, 55_000.0, 57_000.0])
    @pytest.mark.parametrize("sig", [0.08, 0.14, 0.25, 0.45])
    @pytest.mark.parametrize("opt", ["CE", "PE"])
    def test_round_trip_recovers_sigma(self, k, sig, opt):
        px = price(F, k, T, sig, 0.0, opt)
        if px < 0.05:
            pytest.skip("price below one tick — IV is not identifiable")
        assert implied_vol(px, F, k, T, 0.0, opt) == pytest.approx(sig, abs=1e-4)

    def test_arbitrage_violating_price_returns_nan(self):
        assert np.isnan(implied_vol(0.10, F, 53_000.0, T, 0.0, "CE")), "below intrinsic"
        assert np.isnan(implied_vol(F * 2, F, 54_000.0, T, 0.0, "CE")), "above upper bound"

    def test_never_raises_on_garbage(self):
        for args in [(-5.0, F, 54_000.0, T), (100.0, 0.0, 54_000.0, T),
                     (100.0, F, 0.0, T), (100.0, F, 54_000.0, 0.0)]:
            assert np.isnan(implied_vol(*args)) or implied_vol(*args) >= 0

    def test_greeks_from_quote_end_to_end(self):
        px = price(F, 53_000.0, T, 0.16, 0.0, "CE")
        g = greeks_from_quote(px, F, 53_000.0, T, 0.0, "CE")
        assert g is not None
        assert g.iv == pytest.approx(0.16, abs=1e-4)
        assert 0.6 < g.delta < 0.85

    def test_unsolvable_quote_returns_none(self):
        assert greeks_from_quote(0.05, F, 53_000.0, T, 0.0, "CE") is None


class TestImpliedForward:
    def test_recovers_the_forward_from_parity(self):
        k = 54_500.0
        c = price(F, k, T, SIG, 0.0, "CE")
        p = price(F, k, T, SIG, 0.0, "PE")
        assert implied_forward(c, p, k, T, 0.0) == pytest.approx(F, abs=1e-6)


class TestBrokerGreekCrossCheck:
    """v1 defect A9: broker greeks were trusted blindly."""

    def test_agreeing_broker_delta_is_trusted(self):
        g = greeks(F, 53_000.0, T, SIG, 0.0, "CE")
        ok, why = cross_check_delta(g.delta + 0.01, g, 0.08)
        assert ok and "agree" in why

    def test_disagreeing_broker_delta_is_rejected(self):
        g = greeks(F, 53_000.0, T, SIG, 0.0, "CE")
        ok, why = cross_check_delta(0.20, g, 0.08)
        assert not ok and "differ" in why

    def test_sign_convention_does_not_cause_false_rejection(self):
        """Some feeds report put delta unsigned. Comparing ABS avoids a spurious reject."""
        g = greeks(F, 55_000.0, T, SIG, 0.0, "PE")
        ok, _ = cross_check_delta(abs(g.delta), g, 0.08)
        assert ok

    def test_missing_broker_delta_falls_back_to_local(self):
        g = greeks(F, 53_000.0, T, SIG, 0.0, "CE")
        ok, why = cross_check_delta(None, g, 0.08)
        assert ok and "local" in why

    def test_failed_local_solve_is_untrustworthy(self):
        ok, why = cross_check_delta(0.65, None, 0.08)
        assert not ok


class TestYearFraction:
    def test_calendar_time_includes_weekends(self):
        now = dt.datetime(2026, 8, 20, 10, 0)
        exp = dt.datetime(2026, 8, 25, 15, 30)
        assert year_fraction(now, exp) == pytest.approx((5 * 24 + 5.5) / (365 * 24), rel=1e-6)

    def test_past_expiry_is_zero_not_negative(self):
        now = dt.datetime(2026, 8, 26, 10, 0)
        assert year_fraction(now, dt.datetime(2026, 8, 25, 15, 30)) == 0.0

    def test_intraday_decay_is_resolved(self):
        exp = dt.datetime(2026, 8, 25, 15, 30)
        morning = year_fraction(dt.datetime(2026, 8, 25, 9, 15), exp)
        afternoon = year_fraction(dt.datetime(2026, 8, 25, 14, 15), exp)
        assert morning > afternoon > 0
