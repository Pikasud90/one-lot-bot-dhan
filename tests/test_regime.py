"""Regime model tests — chiefly, proving the look-ahead leak is closed.

The headline test does something unusual: it also demonstrates that v1's
approach DOES leak. A leakage test that passes trivially is worthless, so we
show the same harness catching Viterbi red-handed.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from onelot.analytics.regime import (
    Regime,
    RegimeModel,
    build_features,
    compute_for_date,
    filtered_state_probabilities,
    intraday_chop_filter,
    rule_based_regime,
)


def synth_closes(n=600, seed=11):
    """Alternating trend/chop regimes, so an HMM has something to find."""
    rng = np.random.default_rng(seed)
    out, px, i = [], 54000.0, 0
    while len(out) < n:
        block = int(rng.integers(25, 60))
        mode = i % 3
        mu = {0: 0.0022, 1: -0.0022, 2: 0.0}[mode]
        sig = {0: 0.008, 1: 0.010, 2: 0.013}[mode]
        for _ in range(block):
            px *= float(np.exp(rng.normal(mu, sig)))
            out.append(px)
        i += 1
    return np.array(out[:n])


def synth_dates(n, end=dt.date(2026, 8, 20)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


class TestLookAheadLeak:
    """*** v1 defect C1 *** — the leak that makes an HMM backtest fiction."""

    @staticmethod
    def _filtered_label_at(model, closes, index_in_features):
        """Filtered label at a given feature index of `closes`."""
        from onelot.analytics.regime import _log_gaussian_diag

        feats = build_features(closes, model.vol_window)
        flp = _log_gaussian_diag(feats, model._means, model._covars)
        filt = filtered_state_probabilities(flp, model._startprob, model._transmat)
        probs = {}
        for j, prob in enumerate(filt[index_in_features]):
            lbl = model._label_map[j].value
            probs[lbl] = probs.get(lbl, 0.0) + float(prob)
        return Regime(max(probs, key=probs.get))

    def test_filtered_label_is_unchanged_when_future_data_is_appended(self):
        """THE test. Infer at time t, then append 100 future observations and
        re-infer for the SAME instant. A causal estimator gives the same answer."""
        closes = synth_closes(600)
        m = RegimeModel(min_confidence=0.0)
        assert m.fit(closes[:400]), "model must fit for this test to mean anything"

        for cut in range(420, 480, 5):
            now_label = m.infer(closes[:cut]).regime
            idx = len(build_features(closes[:cut], m.vol_window)) - 1
            with_future = self._filtered_label_at(m, closes[: cut + 100], idx)
            assert now_label == with_future, (
                f"FILTERED label at cut={cut} changed once future data existed "
                f"({now_label} -> {with_future}). The forward pass is leaking."
            )

    def test_viterbi_the_v1_approach_demonstrably_leaks(self):
        """Proves the test above has teeth.

        v1's `model.predict()` is Viterbi over the whole sequence. Leakage is
        intermittent rather than constant — measured at ~19% of
        (seed, cut, horizon) configurations on this generator, rewriting past
        labels when it happens. So we sweep rather than cherry-pick one case:
        a single fixed configuration can pass by luck and prove nothing.
        """
        pytest.importorskip("hmmlearn")
        import warnings

        from hmmlearn.hmm import GaussianHMM

        leaky, total, rewritten = 0, 0, 0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for seed in (11, 23, 47, 101, 202):
                feats = build_features(synth_closes(600, seed), 10)
                m = GaussianHMM(n_components=3, covariance_type="diag", n_iter=200, random_state=42)
                m.fit(feats[:300])
                for cut in (320, 360, 380, 420, 460):
                    for extra in (20, 60, 120):
                        if cut + extra > len(feats):
                            continue
                        changed = int(
                            np.sum(m.predict(feats[:cut]) != m.predict(feats[: cut + extra])[:cut])
                        )
                        total += 1
                        rewritten += changed
                        leaky += bool(changed)

        assert leaky > 0, (
            f"Viterbi rewrote no past labels across {total} configurations. If this "
            "ever fails, the leakage test above is proving nothing and needs a "
            "harder generator."
        )
        assert rewritten > 0

    def test_smoothed_posterior_also_leaks(self):
        """predict_proba is forward-BACKWARD; it is conditioned on the future too."""
        pytest.importorskip("hmmlearn")
        import warnings

        from hmmlearn.hmm import GaussianHMM

        feats = build_features(synth_closes(600), 10)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m = GaussianHMM(n_components=3, covariance_type="diag", n_iter=200, random_state=42)
            m.fit(feats[:300])
        cut = 380
        p_short = m.predict_proba(feats[:cut])[-1]
        p_long = m.predict_proba(feats[: cut + 120])[cut - 1]
        assert not np.allclose(p_short, p_long, atol=1e-6), (
            "smoothed posteriors should differ once future data is available"
        )

    def test_compute_for_date_never_sees_the_target_day(self):
        closes = synth_closes(400)
        dates = synth_dates(400)
        target = dates[-1]

        r1, m = compute_for_date(dates, closes, target)
        # Corrupt the target day; the answer must not move.
        poisoned = closes.copy()
        poisoned[-1] = closes[-1] * 5.0
        r2, _ = compute_for_date(dates, poisoned, target, model=m)
        assert r1.regime == r2.regime
        assert r1.as_of == r2.as_of == dates[-2].isoformat()

    def test_forward_filter_is_causal_by_construction(self):
        """Directly: alpha_t depends only on observations 1..t."""
        rng = np.random.default_rng(0)
        flp = rng.normal(size=(50, 3))
        start = np.array([0.4, 0.3, 0.3])
        trans = np.array([[0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.15, 0.15, 0.7]])
        full = filtered_state_probabilities(flp, start, trans)
        partial = filtered_state_probabilities(flp[:30], start, trans)
        assert np.allclose(full[:30], partial, atol=1e-12)


class TestLabelStability:
    """*** v1 defect C2 *** — hmmlearn state indices are arbitrary."""

    def test_labels_are_assigned_by_mean_return_not_by_index(self):
        closes = synth_closes(600)
        m = RegimeModel()
        assert m.fit(closes)
        means = {m._label_map[i]: m._means[i][0] for i in range(m.n_states)}
        assert means[Regime.BULL] > means[Regime.CHOP] > means[Regime.BEAR]

    def test_refit_preserves_semantics(self):
        closes = synth_closes(600)
        a, b = RegimeModel(random_state=1), RegimeModel(random_state=99)
        assert a.fit(closes) and b.fit(closes)
        for m in (a, b):
            bull = [i for i, lbl in m._label_map.items() if lbl is Regime.BULL][0]
            bear = [i for i, lbl in m._label_map.items() if lbl is Regime.BEAR][0]
            assert m._means[bull][0] > m._means[bear][0], (
                "BULL must always be the higher-mean-return state, whatever "
                "index EM happened to assign it"
            )

    def test_model_hash_changes_when_the_model_changes(self):
        a, b = RegimeModel(), RegimeModel()
        a.fit(synth_closes(600, seed=1))
        b.fit(synth_closes(600, seed=2))
        assert a.model_hash and b.model_hash and a.model_hash != b.model_hash

    def test_save_and_load_round_trip(self, tmp_path):
        closes = synth_closes(600)
        m = RegimeModel()
        m.fit(closes)
        p = tmp_path / "regime.pkl"
        m.save(p)
        loaded = RegimeModel.load(p)
        assert loaded is not None
        assert loaded.model_hash == m.model_hash
        assert loaded.infer(closes).regime == m.infer(closes).regime

    def test_loading_a_missing_model_returns_none(self, tmp_path):
        assert RegimeModel.load(tmp_path / "nope.pkl") is None


class TestConfidenceFloor:
    def test_low_confidence_is_treated_as_chop(self):
        """v1 had no floor: a 34/33/33 split counted as a directional regime."""
        m = RegimeModel(min_confidence=0.99)
        closes = synth_closes(600)
        assert m.fit(closes)
        r = m.infer(closes)
        assert r.regime is Regime.CHOP
        assert "confidence" in r.note

    def test_probabilities_sum_to_one(self):
        m = RegimeModel(min_confidence=0.0)
        closes = synth_closes(600)
        m.fit(closes)
        r = m.infer(closes)
        assert sum(r.probabilities.values()) == pytest.approx(1.0, abs=1e-6)


class TestDirectionGating:
    def test_bull_permits_only_calls(self):
        assert Regime.BULL.allows("CE") and not Regime.BULL.allows("PE")

    def test_bear_permits_only_puts(self):
        assert Regime.BEAR.allows("PE") and not Regime.BEAR.allows("CE")

    def test_chop_blocks_both_sides(self):
        assert not Regime.CHOP.allows("CE") and not Regime.CHOP.allows("PE")

    def test_unknown_blocks_both_sides(self):
        assert not Regime.UNKNOWN.allows("CE") and not Regime.UNKNOWN.allows("PE")


class TestFallback:
    def test_rule_based_detects_a_clear_uptrend(self):
        closes = 54000 * np.exp(np.cumsum(np.full(200, 0.003)))
        assert rule_based_regime(closes).regime is Regime.BULL

    def test_rule_based_detects_a_clear_downtrend(self):
        closes = 54000 * np.exp(np.cumsum(np.full(200, -0.003)))
        assert rule_based_regime(closes).regime is Regime.BEAR

    def test_rule_based_calls_a_flat_market_chop(self):
        rng = np.random.default_rng(5)
        closes = 54000 + np.cumsum(rng.normal(0, 8, 200))
        assert rule_based_regime(closes).regime in (Regime.CHOP, Regime.UNKNOWN)

    def test_short_history_is_unknown_not_a_guess(self):
        assert rule_based_regime(np.array([1.0, 2.0, 3.0])).regime is Regime.UNKNOWN

    def test_unfitted_model_reports_unknown(self):
        assert RegimeModel().infer(synth_closes(200)).regime is Regime.UNKNOWN

    def test_insufficient_history_does_not_fit(self):
        assert not RegimeModel().fit(synth_closes(20))


class TestIntradayChopFilter:
    """v1 defect C3: a daily regime cannot gate a 5-minute entry."""

    def _series(self, kind, n=300, seed=4):
        rng = np.random.default_rng(seed)
        if kind == "trend":
            c = 54000 + np.cumsum(np.full(n, 22.0) + rng.normal(0, 5, n))
        else:
            # A mean-reverting process, not a sine wave. A clean sine actually
            # scores ADX ~22 because it trends steadily between its turns;
            # real chop is directionless, which is what O-U reproduces.
            x, vals = 54000.0, []
            for _ in range(n):
                x += -0.35 * (x - 54000.0) + rng.normal(0, 45)
                vals.append(x)
            c = np.array(vals)
        return c + 20, c - 20, c

    def test_trending_series_passes(self):
        h, l_, c = self._series("trend")
        r = intraday_chop_filter(h, l_, c, adx_min=20.0)
        assert r.is_trending, r.reason

    def test_choppy_series_is_blocked(self):
        h, l_, c = self._series("chop")
        r = intraday_chop_filter(h, l_, c, adx_min=20.0)
        assert not r.is_trending
        assert "chop" in r.reason.lower()

    def test_dead_volatility_is_blocked(self):
        h, l_, c = self._series("trend")
        r = intraday_chop_filter(h, l_, c, adx_min=5.0, atr_pct_min=99.0)
        assert not r.is_trending and "quiet" in r.reason

    def test_extreme_volatility_is_blocked(self):
        h, l_, c = self._series("trend")
        r = intraday_chop_filter(h, l_, c, adx_min=5.0, atr_pct_min=0.0, atr_pct_max=1.0)
        assert not r.is_trending and "extreme" in r.reason

    def test_insufficient_bars_blocks_rather_than_guesses(self):
        r = intraday_chop_filter(np.array([1.0, 2.0]), np.array([0.5, 1.0]), np.array([0.8, 1.5]))
        assert not r.is_trending and "warm-up" in r.reason
