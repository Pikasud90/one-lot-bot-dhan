"""US-067 R1 to R6 and R8: the pullback rule, pure, on small hand-made series."""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.config import CostConfig
from onelot.delivery.money import (
    delivery_model,
    depository_paise,
    rupees_to_paise,
    trade_result,
)
from onelot.delivery.rule import (
    EXIT_SMA,
    EXIT_STOP,
    EXIT_TIME,
    PullbackParams,
    entry_fill,
    entry_reading,
    exit_fill,
    exit_reading,
    nifty_ok,
    quantity,
    simulate,
)

P = PullbackParams()
N = 260


def rising(n: int = N, start: float = 100.0, step: float = 0.2) -> np.ndarray:
    """A steady climb: always above SMA200, never 8% below its 20-day high."""
    return start + step * np.arange(n, dtype=float)


def with_dip(c: np.ndarray, t: int, frac: float = 0.10) -> np.ndarray:
    """The close of t falls `frac` below the previous close, then the climb resumes there."""
    c = c.copy()
    c[t:] = c[t:] - c[t - 1] * frac - (c[t] - c[t - 1])
    return c


ALL_OK = np.ones(N, dtype=bool)


# ── R1 ──────────────────────────────────────────────────────────────────────────────────────


def test_r1_a_first_close_8pct_below_the_high_above_sma200_with_nifty_up_signals():
    c = with_dip(rising(), 230)
    r = entry_reading(c, ALL_OK, 230, P)
    assert r.signal and r.deep and r.first_day and r.above_trend and r.market_ok
    assert r.below_high_pct == pytest.approx((1 - c[230] / c[229]) * 100)
    assert r.high == c[229] and r.needed_pct == 8.0


def test_r1a_close_not_above_sma200_no_signal():
    c = with_dip(rising(step=0.0) + 0.0, 230, 0.10)
    c[:200] = 200.0  # a high SMA200 above every later close
    r = entry_reading(c, ALL_OK, 230, P)
    assert r.deep and r.first_day and not r.above_trend and not r.signal


def test_r1b_nifty_below_its_sma200_or_missing_no_signal():
    c = with_dip(rising(), 230)
    ok = ALL_OK.copy()
    ok[230] = False
    assert not entry_reading(c, ok, 230, P).signal
    dates = list(range(N))
    nifty = rising(150)  # fewer than 200 bars up to the date: no signal
    assert not nifty_ok(dates, list(range(150)), nifty, P).any()
    full = nifty_ok(dates, [d for d in dates if d != 230], np.delete(rising(), 230), P)
    assert not full[230] and full[229] and full[231]  # no NIFTY bar that day: no signal


def test_r1c_less_than_the_depth_below_the_high_no_signal():
    c = with_dip(rising(), 230, 0.07)
    r = entry_reading(c, ALL_OK, 230, P)
    assert not r.deep and not r.signal


def test_r1c_exactly_8pct_below_the_high_is_deep():
    c = rising()
    c[230] = 0.92 * c[229]
    assert entry_reading(c, ALL_OK, 230, P).deep


def test_r1d_only_the_first_day_of_the_condition_signals():
    c = with_dip(rising(step=0.0) + 100, 230)
    c[:200] = 50.0  # keep the dip above SMA200
    c[231] = c[230] - 1  # deeper still on the next close: deep, but not the first day
    assert entry_reading(c, ALL_OK, 230, P).signal
    r = entry_reading(c, ALL_OK, 231, P)
    assert r.deep and not r.first_day and not r.signal


def test_r1d_the_day_before_uses_its_own_window():
    """t-1 is tested against the high of t-20..t-1: a high that has left t's window counts."""
    c = np.full(N, 100.0)
    c[:200] = 50.0
    c[209] = 120.0  # in t-1's window (209..228) and not in t's (210..229)
    c[228] = 100.0  # 100 <= 0.92 * 120: deep at t-1 by its own window
    c[229] = 100.0  # t: the high of 210..229 is 100, not deep
    assert entry_reading(c, ALL_OK, 228, P).deep
    assert not entry_reading(c, ALL_OK, 229, P).deep


def test_r1_needs_201_bars():
    c = with_dip(rising(), 199)
    r = entry_reading(c, ALL_OK, 199, P)
    assert not r.ready and not r.signal and "201 needed" in r.why
    assert entry_reading(with_dip(rising(), 200), ALL_OK, 200, P).ready
    assert P.min_bars == 201


# ── R2, R3, R4 ──────────────────────────────────────────────────────────────────────────────


def test_r2_fills_with_the_slippage():
    assert entry_fill(100.0, P) == pytest.approx(100.03)
    assert exit_fill(100.0, P) == pytest.approx(99.97)


def test_r3a_a_close_above_sma20_exits_even_on_the_entry_session():
    c = rising()
    r = exit_reading(c, 240, 140.0, 240, P)
    assert r.hit and r.reasons == (EXIT_SMA,) and r.sessions == 1
    assert r.sma_exit == pytest.approx(float(np.mean(c[221:241])))


def test_r3b_thirty_sessions_with_the_entry_session_as_one():
    c = np.full(N, 100.0)  # never above SMA20 (equal), never at the stop
    assert not exit_reading(c, 200, 100.0, 228, P).hit
    r = exit_reading(c, 200, 100.0, 229, P)
    assert r.hit and r.reasons == (EXIT_TIME,) and r.sessions == 30


def test_r3c_the_stop_is_on_the_entry_fill():
    c = np.full(N, 120.0)
    fill = 100.0 / 0.85
    c[210] = fill * 0.85
    r = exit_reading(c, 205, fill, 210, P)
    assert r.hit and r.reasons == (EXIT_STOP,) and r.stop_level == pytest.approx(100.0)
    c[210] = fill * 0.85 + 0.01
    assert not exit_reading(c, 205, fill, 210, P).hit


def test_r3_several_reasons_are_listed_in_r3s_order():
    c = np.full(N, 100.0)
    c[229] = 101.0  # above SMA20 on the 30th session
    assert exit_reading(c, 200, 100.0, 229, P).reasons == (EXIT_SMA, EXIT_TIME)


def test_r4_the_exit_close_never_enters_and_the_sale_day_may():
    """A trade whose exit signal is on u sells at u+1's open; the first new signal may be
    the close of u+1, never the close of u."""
    c = np.full(N, 100.0)
    c[:200] = 50.0
    c[200:205] = 110.0
    c[205] = 100.0  # first deep close (100 <= 0.92 x 110): signal t=205, entry 206
    c[206:] = 100.0
    c[206] = 102.0  # above SMA20 (~ 100.x) on the entry session: exit signal u=206
    c[207] = 93.0  # sale day u+1 = 207: 93 <= 0.92 x 102, first deep -> new signal
    trades = simulate(c.copy(), c, np.ones(N, bool), P)
    assert (trades[0].signal, trades[0].entry, trades[0].exit) == (205, 206, 207)
    assert trades[1].signal == 207


def test_r4_a_signal_on_the_exit_close_is_not_taken():
    c = np.full(N, 86.0)
    c[:180] = 50.0
    c[180:205] = 110.0
    c[205] = 100.0  # first deep close: signal 205, entry 206
    c[206] = 101.5  # not deep, below SMA20: holds
    c[207] = 86.0  # at the stop (fill 101.53 x 0.85) AND the first deep close again
    r = entry_reading(c, ALL_OK, 207, P)
    assert r.signal  # on its own the close of 207 would enter ...
    (tr,) = simulate(c.copy(), c, ALL_OK, P)  # ... but it is the exit close: one trade only
    assert (tr.signal, tr.entry, tr.exit, tr.reasons) == (205, 206, 208, (EXIT_STOP,))


def test_simulate_a_signal_on_the_last_bar_is_not_a_trade_and_an_exit_on_it_ends_at_the_close():
    c = with_dip(rising(N), N - 1)
    assert simulate(c, c, ALL_OK, P) == []
    n = 230
    c = np.full(n, 90.0)
    c[:180] = 50.0
    c[180:205] = 110.0
    c[205] = 100.0
    c[n - 1] = 200.0  # the exit signal comes on the last bar
    (tr,) = simulate(c, c, np.ones(n, bool), P)
    assert tr.at_close and tr.exit == n - 1


# ── R5 ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("close", "qty"),
    [(1017.5, 10), (1000.0, 10), (952.4, 10), (2000.0, 5), (2222.3, 4), (15000.0, 1),
     (40000.0, 1), (291.9, 34), (6666.7, 1), (6666.6, 2)],
)  # fmt: skip
def test_r5_the_number_of_shares_nearest_the_unit(close, qty):
    assert quantity(close, 10000.0) == qty


def test_r5_unit_inr_is_configuration():
    p = PullbackParams.from_mapping({"unit_inr": 5000})
    assert quantity(1000.0, p.unit_inr) == 5


def test_params_reject_an_unknown_name_and_bad_values():
    with pytest.raises(ValueError, match="unknown"):
        PullbackParams.from_mapping({"depht": 0.08})
    with pytest.raises(ValueError):
        PullbackParams(depth=1.5)
    assert PullbackParams.from_mapping({"evidence": "x", "max_sessions": 30}) == P


# ── R6 ──────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def costs() -> CostConfig:
    from pathlib import Path

    return CostConfig.load(Path(__file__).resolve().parents[2] / "config" / "costs.yaml")


def test_r6_delivery_charges_on_both_legs_plus_the_depository_charge(costs):
    model = delivery_model(costs)
    dp = depository_paise(costs)
    assert dp == 1475
    r = trade_result(model, 100000, 110000, 10, dp)  # Rs 1000 -> Rs 1100, 10 shares
    buy = model.leg(1000.0, 10, "buy").total
    sell = model.leg(1100.0, 10, "sell").total
    assert r.gross_paise == 100000
    assert r.charges_paise == round(buy * 100) + round(sell * 100)
    assert r.net_paise == 100000 - r.charges_paise - 1475
    assert r.net_pct == pytest.approx(r.net_paise / 1000000 * 100)
    assert buy == pytest.approx(10000 * (0.0003 * 1.18 + 0.001 + 0.0000317 * 1.18 + 0.00015))


def test_r6_other_products_have_no_depository_charge(costs):
    assert costs.products["equity_intraday"].depository_per_sale_inr == 0.0


def test_rupees_to_paise_rounds_half_up_to_the_paisa():
    assert rupees_to_paise(100.03) == 10003
    assert rupees_to_paise(1017.805) == 101781
    assert rupees_to_paise(0.005) == 1


# ── R8 ──────────────────────────────────────────────────────────────────────────────────────


@settings(max_examples=40, deadline=None)
@given(
    seed=st.integers(0, 10_000),
    cut=st.integers(205, 399),
)
def test_r8_cutting_the_bars_after_t_leaves_every_signal_up_to_t_unchanged(seed, cut):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.025, 400)))
    o = c * np.exp(rng.normal(0, 0.005, 400))
    ok = rng.random(400) > 0.2
    full = [entry_reading(c, ok, t, P).signal for t in range(cut + 1)]
    short = [entry_reading(c[: cut + 1], ok[: cut + 1], t, P).signal for t in range(cut + 1)]
    assert full == short
    whole = [(tr.signal, tr.entry, tr.exit) for tr in simulate(o, c, ok, P) if tr.exit < cut]
    part = [
        (tr.signal, tr.entry, tr.exit)
        for tr in simulate(o[: cut + 1], c[: cut + 1], ok[: cut + 1], P)
        if tr.exit < cut
    ]
    assert whole == part


def test_quantity_rejects_a_bad_close():
    for bad in (0.0, -1.0, math.nan):
        with pytest.raises(ValueError):
            quantity(bad, 10000.0)
