"""Indicator correctness. Reference values are recomputed independently inside
the tests rather than copied from the implementation, so a bug in the module
cannot make its own test pass."""

from __future__ import annotations

import numpy as np
import pytest

from onelot.analytics.indicators import (
    adx,
    atr,
    ema,
    has_tradeable_volume,
    is_rising,
    percentile_rank,
    rsi,
    sma,
    true_range,
    vwap,
)

rng = np.random.default_rng(42)


def _trend(n=120, start=54000.0, step=25.0, noise=8.0):
    return start + np.cumsum(np.full(n, step) + rng.normal(0, noise, n))


def _chop(n=120, mid=54000.0, amp=90.0):
    return mid + amp * np.sin(np.arange(n) / 3.0) + rng.normal(0, 5, n)


def _ohlc(close, width=40.0):
    close = np.asarray(close, float)
    high = close + width / 2 + rng.uniform(0, width / 4, len(close))
    low = close - width / 2 - rng.uniform(0, width / 4, len(close))
    return high, low, close


class TestWarmup:
    """v1 defect B4: undefined values must be NaN, never 0 — a zero silently
    passes a `> threshold` comparison."""

    def test_ema_is_nan_before_seed(self):
        out = ema(np.arange(50, dtype=float), 21)
        assert np.all(np.isnan(out[:20]))
        assert not np.isnan(out[20])

    def test_rsi_is_nan_before_seed(self):
        out = rsi(_trend(60), 14)
        assert np.all(np.isnan(out[:14]))
        assert not np.isnan(out[14])

    def test_adx_needs_roughly_two_periods(self):
        h, l_, c = _ohlc(_trend(80))
        a, _, _ = adx(h, l_, c, 14)
        assert np.all(np.isnan(a[:20])), "ADX must not be defined in the first 20 bars"
        assert not np.isnan(a[-1])

    def test_short_input_returns_all_nan_not_an_exception(self):
        assert np.all(np.isnan(ema([1.0, 2.0], 21)))
        assert np.all(np.isnan(rsi([1.0, 2.0], 14)))
        a, p, m = adx([1.0], [0.5], [0.8], 14)
        assert np.all(np.isnan(a))


class TestEma:
    def test_matches_independent_recursion(self):
        v = _trend(80)
        got = ema(v, 21)
        alpha = 2 / 22
        want = np.full(len(v), np.nan)
        want[20] = v[:21].mean()
        for i in range(21, len(v)):
            want[i] = alpha * v[i] + (1 - alpha) * want[i - 1]
        assert np.allclose(got[20:], want[20:])

    def test_seeded_with_sma(self):
        v = _trend(60)
        assert ema(v, 21)[20] == pytest.approx(sma(v, 21)[20])

    def test_constant_series_equals_the_constant(self):
        assert ema(np.full(60, 100.0), 21)[-1] == pytest.approx(100.0)


class TestRsi:
    def test_bounded_zero_to_hundred(self):
        for series in (_trend(200), _chop(200), -_trend(200)):
            out = rsi(series, 14)
            valid = out[~np.isnan(out)]
            assert valid.min() >= 0.0 and valid.max() <= 100.0

    def test_monotonic_rise_saturates_high(self):
        assert rsi(np.arange(100, dtype=float) * 10 + 1000, 14)[-1] > 95

    def test_monotonic_fall_saturates_low(self):
        assert rsi(2000 - np.arange(100, dtype=float) * 10, 14)[-1] < 5

    def test_matches_independent_wilder_recursion(self):
        v = _trend(90)
        d = np.diff(v)
        g = np.where(d > 0, d, 0.0)
        ls = np.where(d < 0, -d, 0.0)

        def _r(gain, loss):
            if loss == 0.0:
                return 100.0 if gain > 0 else 50.0
            return 100 - 100 / (1 + gain / loss)

        ag, al = g[:14].mean(), ls[:14].mean()
        want = [_r(ag, al)]
        for i in range(14, len(v) - 1):
            ag = (ag * 13 + g[i]) / 14
            al = (al * 13 + ls[i]) / 14
            want.append(_r(ag, al))
        got = rsi(v, 14)
        assert np.allclose(got[14:], np.array(want))


class TestAtrAdx:
    def test_true_range_covers_gaps(self):
        h = np.array([100.0, 130.0])
        l_ = np.array([90.0, 125.0])
        c = np.array([95.0, 128.0])
        tr = true_range(h, l_, c)
        assert tr[0] == pytest.approx(10.0)
        assert tr[1] == pytest.approx(35.0), "gap up: high(130) - prev close(95)"

    def test_atr_positive_and_scales_with_range(self):
        h1, l1, c1 = _ohlc(_trend(120), width=20.0)
        h2, l2, c2 = _ohlc(_trend(120), width=200.0)
        assert atr(h2, l2, c2, 14)[-1] > atr(h1, l1, c1, 14)[-1]
        assert atr(h1, l1, c1, 14)[-1] > 0

    def test_adx_bounded(self):
        h, l_, c = _ohlc(_chop(200))
        a, p, m = adx(h, l_, c, 14)
        for arr in (a, p, m):
            v = arr[~np.isnan(arr)]
            assert v.min() >= 0.0 and v.max() <= 100.0

    def test_adx_higher_in_trend_than_chop(self):
        ht, lt, ct = _ohlc(_trend(250, step=30.0, noise=4.0))
        hc, lc, cc = _ohlc(_chop(250))
        assert np.nanmean(adx(ht, lt, ct, 14)[-40:]) > np.nanmean(adx(hc, lc, cc, 14)[-40:])

    def test_plus_di_dominates_in_uptrend(self):
        h, l_, c = _ohlc(_trend(200, step=30.0, noise=4.0))
        _, p, m = adx(h, l_, c, 14)
        assert np.nanmean(p[-30:]) > np.nanmean(m[-30:])

    def test_minus_di_dominates_in_downtrend(self):
        h, l_, c = _ohlc(60000 - _trend(200, start=0.0, step=30.0, noise=4.0))
        _, p, m = adx(h, l_, c, 14)
        assert np.nanmean(m[-30:]) > np.nanmean(p[-30:])

    def test_is_rising_is_explicit(self):
        assert is_rising([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        assert not is_rising([5.0, 4.0, 3.0, 2.0, 1.0], 3)
        assert not is_rising([1.0, 2.0], 3), "insufficient data is not 'rising'"


class TestVwapIndexVolumeBug:
    """*** v1 defect B3 *** — the flaw that would have silently broken the
    entire Guardian: the Bank Nifty INDEX has no traded volume."""

    def test_index_series_returns_nan_not_a_plausible_number(self):
        c = _trend(75)
        h, l_, _ = _ohlc(c)
        zero_volume = np.zeros(len(c))  # exactly what an index feed returns
        out = vwap(h, l_, c, zero_volume)
        assert np.all(np.isnan(out)), (
            "VWAP on a zero-volume index series must be NaN so the gate blocks. "
            "Returning any number here is how v1 would have traded on 0/0."
        )

    def test_has_tradeable_volume_guard(self):
        assert not has_tradeable_volume(np.zeros(50))
        assert not has_tradeable_volume([])
        assert has_tradeable_volume(np.full(50, 1200.0))

    def test_futures_series_produces_real_vwap(self):
        c = _trend(75)
        h, l_, _ = _ohlc(c)
        vol = rng.uniform(1000, 9000, len(c))
        out = vwap(h, l_, c, vol)
        assert not np.isnan(out[-1])
        assert out[-1] > 0

    def test_vwap_sits_inside_the_price_range(self):
        c = _trend(75)
        h, l_, _ = _ohlc(c)
        out = vwap(h, l_, c, rng.uniform(1000, 9000, len(c)))
        assert l_.min() <= out[-1] <= h.max()

    def test_vwap_equals_price_when_price_is_flat(self):
        n = 40
        c = np.full(n, 54000.0)
        h = c.copy()
        l_ = c.copy()
        out = vwap(h, l_, c, np.full(n, 500.0))
        assert out[-1] == pytest.approx(54000.0)

    def test_heavier_volume_pulls_vwap(self):
        c = np.array([100.0, 200.0])
        h = c.copy()
        l_ = c.copy()
        assert vwap(h, l_, c, [1.0, 99.0])[-1] > vwap(h, l_, c, [99.0, 1.0])[-1]


class TestPercentileRank:
    def test_basic(self):
        h = np.arange(100, dtype=float)
        assert percentile_rank(h, 50.0) == pytest.approx(50.0)
        assert percentile_rank(h, 0.0) == pytest.approx(0.0)
        assert percentile_rank(h, 1000.0) == pytest.approx(100.0)

    def test_empty_history_is_nan_not_zero(self):
        assert np.isnan(percentile_rank([], 5.0))
