"""US-077: market states: classify, model, walk_forward (pure functions, hand-checked)."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from onelot.mllab import states as st


def daily(returns, span=0.01, last_span=None) -> pd.DataFrame:
    """Daily bars from log returns; high/low are +/- `span` around the close."""
    r = np.r_[0.0, np.asarray(returns, float)]
    close = 100 * np.exp(np.cumsum(r))
    sp = np.full(len(close), span)
    if last_span is not None:
        sp[-1] = last_span
    idx = pd.bdate_range("2024-01-01", periods=len(close))
    return pd.DataFrame(
        {"open": close, "high": close * (1 + sp), "low": close * (1 - sp), "close": close},
        index=idx,
    )


def frame(dirs, sizes=None) -> pd.DataFrame:
    """A states frame as `classify` returns it, made by hand."""
    n = len(dirs)
    sizes = sizes if sizes is not None else ["calm", "active"] * (n // 2 + 1)
    return pd.DataFrame(
        {
            "r": 0.0,
            "z": 0.0,
            "rng": 0.0,
            "rng_med": 0.0,
            "direction": list(dirs),
            "size": list(sizes)[:n],
        },
        index=pd.bdate_range("2020-01-01", periods=n),
    )


class TestClassify:
    def test_hand_checked_rows(self):
        # 20 returns of +/-1% (mean 0), then +2%: z = 0.02 / sd, sd = 0.01 * sqrt(20/19)
        r = [0.01, -0.01] * 10 + [0.02]
        out = st.classify(daily(r, last_span=0.03))
        last = out.iloc[-1]
        assert last["z"] == pytest.approx(0.02 / (0.01 * math.sqrt(20 / 19)), rel=1e-9)
        assert last["direction"] == "bull"
        assert last["size"] == "active"  # a 6% range against a median of about 2%
        flat = st.classify(daily([0.01, -0.01] * 10 + [0.004], last_span=0.002)).iloc[-1]
        assert flat["direction"] == "flat" and flat["size"] == "calm"  # z about 0.39
        bear = st.classify(daily([0.01, -0.01] * 10 + [-0.012])).iloc[-1]
        assert bear["direction"] == "bear"  # z about -1.17

    def test_the_cut_is_half_a_standard_deviation(self):
        base = [0.01, -0.01] * 10
        sd = 0.01 * math.sqrt(20 / 19)
        assert st.classify(daily(base + [0.51 * sd])).iloc[-1]["direction"] == "bull"
        assert st.classify(daily(base + [0.49 * sd])).iloc[-1]["direction"] == "flat"
        assert st.classify(daily(base + [-0.51 * sd])).iloc[-1]["direction"] == "bear"

    def test_no_state_in_the_first_21_sessions(self):
        rng = np.random.default_rng(1)
        out = st.classify(daily(rng.normal(0, 0.01, 60)))  # 61 sessions
        assert out["direction"].iloc[:21].isna().all() and out["size"].iloc[:21].isna().all()
        assert out["direction"].iloc[21:].notna().all()
        assert out["size"].iloc[21:].notna().all()

    def test_a_state_uses_nothing_after_its_session(self):
        rng = np.random.default_rng(2)
        d = daily(rng.normal(0, 0.01, 80), span=0.012)
        d["high"] *= 1 + rng.uniform(0, 0.01, len(d))
        full = st.classify(d)
        for t in (21, 30, 55, 80):
            cut = st.classify(d.iloc[: t + 1]).iloc[-1]
            assert cut["direction"] == full.iloc[t]["direction"]
            assert cut["size"] == full.iloc[t]["size"]
        changed = d.copy()
        changed.iloc[60:, changed.columns.get_loc("close")] *= 3  # the future moves
        again = st.classify(changed)
        assert (
            again["direction"].iloc[:60].fillna("-") == full["direction"].iloc[:60].fillna("-")
        ).all()

    def test_a_zero_spread_gives_no_state_not_an_error(self):
        out = st.classify(daily([0.0] * 30))  # no move at all: sd 0
        assert out["direction"].isna().all()


class TestConviction:
    @pytest.mark.parametrize(
        ("edge", "z", "want"),
        [
            (0.10, 0.99, "none"),
            (0.10, -1.5, "weak"),
            (0.10, 2.5, "moderate"),
            (0.06, 3.0, "strong"),
            (0.04, 4.0, "moderate"),  # strong needs 5 points of edge as well
            (0.0, float("nan"), "none"),
        ],
    )
    def test_bands(self, edge, z, want):
        assert st._conviction(edge, z) == want

    def test_score(self):
        assert st._score(1.0) == 25 and st._score(-2.0) == 50 and st._score(9.0) == 100


class TestModel:
    def test_counts_and_probabilities_on_a_made_sequence(self):
        seq = ["bull", "bull", "bear", "flat"] * 30  # 120 sessions
        m = st.model(frame(seq))
        assert m["n_states"] == 120
        assert m["base"]["bull"] == pytest.approx(0.5) and m["base"]["bear"] == pytest.approx(0.25)
        first = {tuple(r["context"]): r for r in m["first_order"]}
        # after bull: bull 30 times, bear 30 times (119 transitions: the last bull has no next)
        assert first[("bull",)]["counts"] == {"bull": 30, "flat": 0, "bear": 30}
        assert first[("bull",)]["n"] == 60
        p = first[("bull",)]["probs"]
        assert p["bull"] == pytest.approx((30 + 10 * 0.5) / 70, abs=1e-4)
        assert p["flat"] == pytest.approx((0 + 10 * 0.25) / 70, abs=1e-4)
        assert sum(p.values()) == pytest.approx(1.0, abs=1e-3)
        second = {tuple(r["context"]): r for r in m["second_order"]}
        assert second[("bull", "bull")]["counts"]["bear"] == 30
        json.dumps(m, allow_nan=False)

    def test_second_order_when_it_has_60_cases_else_the_first(self):
        long = st.model(frame(["bull", "bear"] * 200))
        assert long["next"]["order"] == 2 and long["next"]["n"] >= 60
        assert long["next"]["context"] == ["bull", "bear"]
        short = st.model(frame(["bull", "bear"] * 40))  # 80 sessions: 39 cases of a pair
        assert short["next"]["order"] == 1 and short["next"]["context"] == ["bear"]
        assert short["next"]["n"] == 39

    def test_a_strongly_repeating_sequence_gives_strong(self):
        m = st.model(frame(["bull", "bear"] * 200))
        assert m["next"]["lean"] == "bull" and m["next"]["conviction"] == "strong"
        assert m["next"]["score"] == 100 and m["next"]["edge_pts"] > 40
        assert m["size_next"]["conviction"] == "strong"  # calm/active alternate too

    def test_no_memory_gives_no_lean(self):
        rng = np.random.default_rng(3)
        dirs = rng.choice(st.DIRS, 600, p=[0.3, 0.4, 0.3])
        sizes = rng.choice(st.SIZES, 600)
        m = st.model(frame(dirs, sizes))
        assert m["next"]["conviction"] in ("none", "weak")
        assert abs(m["next"]["z"]) < 3

    def test_runs(self):
        seq = ["bull", "flat", "bull", "bull", "flat", "bull", "bull", "bull", "bull", "flat"]
        runs = {(r["direction"], r["length"]): r for r in st.model(frame(seq * 3))["runs"]}
        # run 1 ends at index 0, 2... a run of 1 continues when the next is bull (never here)
        assert runs[("bull", 1)]["continued"] is not None
        assert runs[("bull", 2)]["n"] > 0 and runs[("bull", 3)]["n"] > 0
        assert runs[("bear", 1)]["n"] == 0 and runs[("bear", 1)]["continued"] is None

    def test_too_little_history(self):
        assert "note" in st.model(frame(["bull", "bear"]))


class TestWalkForward:
    def test_real_memory_beats_the_naive_rule(self):
        rng = np.random.default_rng(4)
        d = ["bull"]
        for _ in range(1499):
            d.append(d[-1] if rng.random() < 0.9 else ("bear" if d[-1] == "bull" else "bull"))
        w = st.walk_forward(frame(d))
        assert w["n"] == 1500 - st.WALK_START
        assert w["hit"] > w["hit_naive"] + 0.2

    def test_shuffled_states_do_not(self):
        rng = np.random.default_rng(5)
        d = rng.choice(st.DIRS, 1500, p=[0.3, 0.4, 0.3])
        s = rng.choice(st.SIZES, 1500)
        w = st.walk_forward(frame(d, s))
        assert w["hit"] <= w["hit_naive"] + 0.02
        assert w["size_hit"] <= w["size_hit_naive"] + 0.03

    def test_an_early_session_is_named_from_earlier_sessions_only(self):
        rng = np.random.default_rng(6)
        d = list(rng.choice(st.DIRS, 400))
        a = st.walk_steps(frame(d))
        d2 = d[:]
        d2[-1] = "bull" if d[-1] != "bull" else "bear"  # only the last session changes
        b = st.walk_steps(frame(d2))
        assert (a["lean"].iloc[:-1] == b["lean"].iloc[:-1]).all()
        assert (a["conviction"].iloc[:-1] == b["conviction"].iloc[:-1]).all()

    def test_too_few_sessions(self):
        assert st.walk_forward(frame(["bull", "bear"] * 50))["n"] == 0


def test_reading_is_strict_json_and_complete():
    rng = np.random.default_rng(7)
    d = daily(rng.normal(0, 0.01, 400), span=0.01)
    d["high"] *= 1 + rng.uniform(0, 0.01, len(d))
    out = st.reading(d)
    json.dumps(out, allow_nan=False)
    assert out["as_of"] == str(d.index[-1].date())
    assert len(out["sessions"]) == 60 and set(out["sessions"][0]) == {
        "date",
        "r_pct",
        "z",
        "direction",
        "size",
    }
    assert set(out["base"]) == {"bull", "flat", "bear", "active"}
    assert set(out["walk_forward"]) == {"n", "hit", "hit_naive", "size_hit", "size_hit_naive"}
    assert len(out["first_order"]) == 3 and len(out["second_order"]) == 9
    assert len(out["runs"]) == 6
    short = st.reading(daily(rng.normal(0, 0.01, 10)))
    assert short["note"] and short["sessions"] == []


def test_session_sizes_are_words_and_both_appear():
    rng = np.random.default_rng(3)
    d = daily(rng.normal(0, 0.01, 120), span=0.01)
    d["high"] *= 1 + rng.uniform(0, 0.02, len(d))  # ranges vary: calm and active days
    sizes = [s["size"] for s in st.reading(d)["sessions"]]
    assert set(sizes) == {"calm", "active"}


def test_thin_is_true_under_250_stated_sessions_and_false_at_250():
    rng = np.random.default_rng(4)
    d = daily(rng.normal(0, 0.01, 400), span=0.01)
    d["high"] *= 1 + rng.uniform(0, 0.01, len(d))
    assert st.reading(d.iloc[: 21 + 249])["thin"] is True
    assert st.reading(d.iloc[: 21 + 250])["thin"] is False
    assert st.reading(d.iloc[:10])["thin"] is True
