"""US-069: the Machine Learning Lab's five models on series where the answer is known."""

from __future__ import annotations

import math

import numpy as np
import pytest

from onelot.mllab import models as m


def ar1_prices(n: int = 1500, phi: float = 0.5, sd: float = 0.01, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    e = rng.normal(0, sd, n)
    r = np.zeros(n)
    for t in range(1, n):
        r[t] = phi * r[t - 1] + e[t]
    return 100 * np.exp(np.cumsum(r))


def garch_returns(n: int, seed: int = 2, omega=2e-6, alpha=0.1, beta=0.85) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = omega / (1 - alpha - beta)
    r = np.zeros(n)
    for t in range(n):
        r[t] = math.sqrt(v) * rng.standard_normal()
        v = omega + alpha * r[t] ** 2 + beta * v
    return r


def prices(r: np.ndarray) -> np.ndarray:
    return 100 * np.exp(np.concatenate([[0.0], np.cumsum(r)]))


def random_walk(n: int = 600, seed: int = 3) -> np.ndarray:
    return prices(np.random.default_rng(seed).normal(0.0002, 0.008, n - 1))


class TestKnownAnswers:
    def test_arima_recovers_phi_of_an_ar1(self):
        r = np.diff(np.log(ar1_prices()))
        c, phi, theta, _ = m._arma_fit(r)
        # ARMA(1,1) on an AR(1): theta near 0, phi near 0.5 (phi and theta trade off a little)
        assert abs(phi - 0.5) < 0.1 and abs(theta) < 0.1

    def test_arima_forecast_follows_the_last_return(self):
        p = ar1_prices()
        f = m.arima(p)
        r_last = math.log(p[-1] / p[-2])
        assert np.sign(math.log(f.mean / p[-1])) == np.sign(r_last)
        assert (f.p_up > 0.5) == (r_last > 0)

    def test_garch_band_is_wider_after_a_large_move(self):
        r = garch_returns(1500)
        calm = m.garch(prices(np.append(r, 0.0001)))
        shock = m.garch(prices(np.append(r, -0.05)))
        width = lambda f: (f.hi95 - f.lo95) / f.mean  # noqa: E731
        assert width(shock) > 1.5 * width(calm)

    def test_garch_keeps_its_constraints(self):
        p = m._garch_fit(garch_returns(1500))
        _, omega, alpha, beta = p
        assert omega > 0 and alpha >= 0 and beta >= 0 and alpha + beta < 1

    def test_ewma_is_the_hand_formula(self):
        p = random_walk()
        r = np.diff(np.log(p))
        v = float(np.mean(r * r))
        for x in r:
            v = 0.94 * v + 0.06 * x * x
        f = m.ewma(p)
        s = math.sqrt(v)
        assert f.mean == pytest.approx(p[-1])
        assert f.p_up == pytest.approx(0.5)
        assert f.lo68 == pytest.approx(p[-1] * math.exp(-m.Z68 * s), rel=1e-12)
        assert f.hi95 == pytest.approx(p[-1] * math.exp(m.Z95 * s), rel=1e-12)

    def test_empirical_is_numpys_percentiles(self):
        p = random_walk()
        r = np.diff(np.log(p))[-250:]
        q = np.percentile(r, [2.5, 16, 50, 84, 97.5])
        f = m.empirical(p)
        got = [f.lo95, f.lo68, f.mean, f.hi68, f.hi95]
        assert got == pytest.approx(list(p[-1] * np.exp(q)), rel=1e-12)
        assert f.p_up == pytest.approx(float(np.mean(r > 0)))

    def test_sarima_finds_a_seasonal_term(self):
        rng = np.random.default_rng(5)
        n, s = 2000, 7
        r = np.zeros(n)
        e = rng.normal(0, 0.01, n)
        for t in range(s, n):
            r[t] = 0.4 * r[t - s] + e[t]
        c, phi, theta, Phi = m._arma_fit(r, s)
        assert abs(Phi - 0.4) < 0.1 and "s=7" in m.sarima(prices(r), s).detail


@pytest.mark.parametrize("model", m.MODELS)
class TestEveryModel:
    def test_bands_are_ordered_and_p_up_is_a_probability(self, model):
        for p in (ar1_prices(800), random_walk(), prices(garch_returns(800))):
            f = m.forecast(model, p, 7)
            assert f.note is None, f.note
            assert f.lo95 < f.lo68 < f.mean < f.hi68 < f.hi95
            assert 0 < f.p_up < 1 and f.sd > 0 and f.n == len(p)

    @pytest.mark.parametrize(
        "bad",
        [[], [1.0], [np.nan] * 100, np.ones(100), [0, -1, 2] * 40, "junk", None, [np.inf] * 80],
    )
    def test_bad_input_gives_a_note_and_never_raises(self, model, bad):
        f = m.forecast(model, bad, 7)
        assert f.note and f.mean is None and f.lo95 is None

    def test_nan_inside_a_good_series_is_dropped(self, model):
        p = random_walk().copy()
        p[100] = np.nan
        assert m.forecast(model, p, 7).note is None

    def test_too_few_bars(self, model):
        p = random_walk(45)
        f = m.forecast(model, p, 7)
        if model in ("ewma", "empirical"):
            assert f.note is None  # 30 bars are enough for the simple ones
        else:
            assert f.note == "too few bars to trust: 45 of 60"

    def test_the_backtest_never_looks_ahead(self, model):
        p = random_walk(400)
        full = m.walk_forward(p, model, last=100, season=7)
        cut = 340  # bars from 340 on are replaced: forecasts for bars up to 340 must not move
        other = p.copy()
        other[cut:] = other[cut:] * np.random.default_rng(9).uniform(0.5, 1.5, len(p) - cut)
        changed = m.walk_forward(other, model, last=100, season=7)
        steps_before_cut = cut - 300 + 1  # forecasts for bars 300..340 use bars up to 339
        for (a, *_), (b, *_) in zip(
            full[:steps_before_cut], changed[:steps_before_cut], strict=True
        ):
            assert a == b
        assert full[steps_before_cut][0] != changed[steps_before_cut][0]  # bar 341 sees 340
        # the first step is exactly the model fitted on the bars before it
        assert full[0][0].mean == pytest.approx(m.forecast(model, p[:300], 7).mean)

    def test_coverage_is_near_68_and_95_on_a_long_series(self, model):
        p = prices(np.random.default_rng(11).normal(0, 0.01, 1500))
        bt = m.backtest(p, model, last=1000, season=7)
        assert bt["steps"] == 1000
        assert abs(bt["in68"] - 0.68) < 0.05 and abs(bt["in95"] - 0.95) < 0.03


def test_ewma_has_no_direction():
    assert m.backtest(random_walk(), "ewma")["direction"] is None


def test_backtest_on_too_few_bars_has_no_steps():
    assert m.backtest([1, 2, 3], "garch") == {
        "steps": 0,
        "in68": None,
        "in95": None,
        "direction": None,
    }
