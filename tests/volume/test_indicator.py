"""US-089 part 1: every formula of VOL-API.md on hand-made bars (numbers checkable by hand)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from onelot.volume import indicator as ind

from .helpers import IST, day_bars, sessions


def test_turnover_is_volume_times_close():
    bars = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-05 09:15", tz=IST)]),
    )
    assert ind.turnover(bars).iloc[0] == 1050.0


def _bars(rows):
    idx = pd.date_range(pd.Timestamp("2026-01-05 09:15", tz=IST), periods=len(rows), freq="1min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def test_clv_rule():
    b = _bars([[10, 12, 8, 11, 100], [10, 10, 10, 10, 100], [10, 12, 8, 8, 1]])
    assert ind.up_share(b, "clv").tolist() == [0.75, 0.5, 0.0]


def test_tick_rule():
    b = _bars([[10, 12, 8, 11, 1], [10, 12, 8, 9, 1], [10, 12, 8, 10, 1]])
    assert ind.up_share(b, "tick").tolist() == [1.0, 0.0, 0.5]


def test_bvc_rule_is_phi_of_return_over_previous_sd():
    # 61 bars: returns alternate +1% / -1% (sd known), then a bar of exactly one sd
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
    r = pd.Series(closes).pct_change().iloc[1:]
    sd = float(r.iloc[-60:].std(ddof=1)) if len(r) >= 60 else float(r.std(ddof=1))
    closes.append(closes[-1] * (1 + sd))
    rows = [[c, c * 1.001, c * 0.999, c, 10] for c in closes]
    rows[0][0] = closes[0]
    b = _bars(rows)
    u = ind.up_share(b, "bvc", lookback=60)
    # the last bar: r = sd of the 60 returns before it -> Phi(1)
    assert u.iloc[-1] == pytest.approx(0.841345, abs=1e-4)
    # the first bar has no previous sd: 0.5
    assert u.iloc[0] == 0.5


def test_unknown_rule_is_an_error():
    with pytest.raises(ValueError):
        ind.up_share(_bars([[1, 1, 1, 1, 1]]), "nope")


def test_buying_selling_and_net():
    b = _bars([[10, 12, 8, 11, 100]])  # turnover 1100, u 0.75
    f = ind.flows(b, "clv").iloc[0]
    assert (f["turnover"], f["buy"], f["sell"], f["net"]) == (1100.0, 825.0, 275.0, 550.0)
    assert ind.flow_share([825.0], [275.0]) == 0.75
    assert ind.flow_share([0.0], [0.0]) is None


@pytest.mark.parametrize(
    ("share", "word"),
    [(0.6, "BUYING"), (0.59, "TWO_WAY"), (0.4, "SELLING"), (0.41, "TWO_WAY"), (None, "TWO_WAY")],
)
def test_direction(share, word):
    assert ind.direction(share) == word


def test_day_matrix_and_full_sessions():
    full = day_bars("2026-01-05", turnover=100.0)
    half = day_bars("2026-01-06", turnover=100.0, minutes=60)
    t = ind.turnover(pd.concat([full, half]))
    m = ind.day_matrix(t)
    assert m.shape == (2, 375)
    assert ind.bar_counts(m).tolist() == [375, 60]
    assert ind.full_days(m).tolist() == [True, False]
    assert m.iloc[0, 0] == 100.0 and np.isnan(m.iloc[1, 60])


def test_expected_is_median_of_previous_full_sessions_and_skips_half_days():
    days = sessions("2026-01-05", 5)
    frames = [
        day_bars(days[0], turnover=100.0),
        day_bars(days[1], turnover=300.0),
        day_bars(days[2], turnover=9e9, minutes=60),  # a half day: never a baseline
        day_bars(days[3], turnover=200.0),
        day_bars(days[4], turnover=50.0),
    ]
    m = ind.day_matrix(ind.turnover(pd.concat(frames)))
    e = ind.expected_matrix(m, lookback=20, min_n=1)
    assert e.iloc[0, 0] == 0.0  # no history: 0
    assert e.iloc[1, 0] == 100.0
    assert e.iloc[2, 0] == 200.0  # median(100, 300)
    assert e.iloc[3, 0] == 200.0  # the half day is not counted
    assert e.iloc[4, 0] == 200.0  # median(100, 300, 200)


def test_minutes_after_1514_have_no_expected_and_no_rvol():
    days = sessions("2026-08-03", 3)
    frames = [day_bars(d, turnover=100.0, minutes=360) for d in days]  # 09:15 to 15:14
    m = ind.day_matrix(ind.turnover(pd.concat(frames)))
    e = ind.expected_matrix(m, lookback=20, min_n=1)
    assert e.iloc[2, 359] == 100.0 and e.iloc[2, 365] == 0.0
    rv = ind.rvol_rows(m.iloc[2].to_numpy(), e.iloc[2].to_numpy())
    assert rv["rvol_1m"][359] == 1.0
    assert np.isnan(rv["rvol_1m"][365])  # no division error, no number
    assert ind.finite(rv["rvol_1m"][365]) is None


def test_relative_volume_one_five_and_day():
    actual = np.array([2.0, 4.0, 6.0, 8.0, 10.0, 12.0] + [np.nan] * 369)
    expected = np.full(375, 2.0)
    rv = ind.rvol_rows(actual, expected)
    assert rv["rvol_1m"][1] == 2.0
    # last 5 minutes at column 5: (4+6+8+10+12) / (5*2)
    assert rv["rvol_5m"][5] == pytest.approx(40 / 10)
    # at the open the window is shorter: column 1 -> (2+4)/(2+2)
    assert rv["rvol_5m"][1] == pytest.approx(6 / 4)
    # the day so far at column 5: 42 / 12
    assert rv["rvol_day"][5] == pytest.approx(42 / 12)
    assert np.isnan(rv["rvol_5m"][6])  # not closed yet


def test_spike_score_from_previous_sessions():
    # three full sessions whose 5-minute sums at 09:19 are e^1, e^2, e^3: mean 2, sd 1
    days = sessions("2026-01-05", 4)
    frames = []
    for d, k in zip(days, (1, 2, 3, 4), strict=True):
        f = day_bars(d, turnover=math.exp(k) / 5)
        frames.append(f)
    m = ind.day_matrix(ind.turnover(pd.concat(frames)))
    mu, sd = ind.zstats_matrix(m, lookback=60, min_n=2)
    assert mu.iloc[3, 4] == pytest.approx(2.0)
    assert sd.iloc[3, 4] == pytest.approx(1.0)
    q = ind.log_window(m.to_numpy())[3, 4]
    assert q == pytest.approx(4.0)
    assert ind.zscore(q, mu.iloc[3, 4], sd.iloc[3, 4]) == pytest.approx(2.0)
    assert np.isnan(mu.iloc[1, 4])  # one prior session is fewer than min_n


def test_states():
    assert ind.state(0.5, 0.0, 3.0) == "quiet"
    assert ind.state(1.0, 0.0, 3.0) == "normal"
    assert ind.state(1.5, 0.0, 3.0) == "busy"
    assert ind.state(0.5, 3.0, 3.0) == "spike"
    assert ind.state(None, None, 3.0) == "normal"


def test_influence_recovers_weights_and_clips_negatives():
    rng = np.random.default_rng(1)
    a, b, c = (rng.normal(0, 0.001, 600) for _ in range(3))
    y = pd.Series(0.6 * a + 0.4 * b - 0.3 * c)
    x = pd.DataFrame({"A": a, "B": b, "C": c})
    w, r2 = ind.influence(y, x)
    assert w["C"] == 0.0
    assert w.sum() == pytest.approx(1.0)
    assert w["A"] / w["B"] == pytest.approx(1.5, rel=0.05)
    assert 0 < r2 < 1
    w2, r2b = ind.influence(pd.Series(0.6 * a + 0.4 * b), x)
    assert w2["A"] == pytest.approx(0.6, abs=1e-6) and r2b == pytest.approx(1.0)


def test_influence_without_data():
    w, r2 = ind.influence(pd.Series([], dtype=float), pd.DataFrame({"A": []}))
    assert r2 is None and w["A"] == 0.0


def test_pressure():
    w = {"A": 0.5, "B": 0.3, "C": 0.2}
    net = {"A": 100.0, "B": -300.0, "C": 50.0}
    exp = {"A": 1000.0, "B": 1000.0, "C": 0.0}  # C has no expected turnover: left out
    assert ind.pressure(w, net, exp) == pytest.approx(0.5 * 0.1 + 0.3 * -0.3)
    assert ind.pressure(w, {}, exp) is None


def C(t, scope, key, z):  # noqa: N802
    return ind.Candidate(t, scope, key, z)


def test_alerts_cooldown_cap_and_order():
    cands = [
        C(0, "MARKET", "MARKET", 3.1),
        C(5, "MARKET", "MARKET", 3.5),  # inside the 15-minute cooldown
        C(15, "MARKET", "MARKET", 3.2),  # 15 minutes later: allowed
        C(15, "STOCK", "SBIN", 4.2),
        C(15, "STOCK", "INFY", 4.9),
        C(16, "STOCK", "TCS", 3.9),  # under the stock level
    ]
    got = ind.pick_alerts(cands, z_market=3.0, z_stock=4.0, cooldown_min=15, cap=12)
    assert [(c.t, c.key) for c in got] == [
        (0, "MARKET"),
        (15, "MARKET"),
        (15, "INFY"),
        (15, "SBIN"),
    ]
    capped = ind.pick_alerts(cands, z_market=3.0, z_stock=4.0, cooldown_min=15, cap=3)
    assert [c.key for c in capped] == ["MARKET", "MARKET", "INFY"]
    later = ind.pick_alerts(
        cands,
        z_market=3.0,
        z_stock=4.0,
        cooldown_min=15,
        cap=12,
        earlier=[(10, "MARKET", "MARKET")],
    )
    assert [(c.t, c.key) for c in later if c.scope == "MARKET"] == []  # 15 - 10 < 15
    nan = ind.pick_alerts([C(0, "MARKET", "MARKET", float("nan"))], z_market=3, z_stock=4,
                          cooldown_min=15, cap=12)  # fmt: skip
    assert nan == []


def test_profile_half_hours():
    m = np.ones((3, 375))
    m[:, :30] = 3.0  # the first half hour: 90 of 435
    prof = ind.profile(pd.DataFrame(m))
    first = prof["half_hours"][0]
    assert first == {"from": "09:15", "to": "09:44", "share": round(90 / 435, 4), "rank": 1}
    assert prof["half_hours"][-1]["from"] == "15:15" and prof["half_hours"][-1]["to"] == "15:29"
    assert prof["busiest"][0]["from"] == "09:15"
    assert all(q["from"] != "15:15" for q in prof["quietest"])  # whole half hours only
    assert prof["minutes"][0] == ["09:15", 3.0, 3.0, 3.0]
    assert sum(h["share"] for h in prof["half_hours"]) == pytest.approx(1.0, abs=1e-3)


def test_weekday_and_expiry_days():
    rv = pd.Series(
        [1.0, 2.0, 3.0], index=pd.to_datetime(["2026-01-05", "2026-01-12", "2026-01-06"])
    )
    wd = ind.weekday_rvol(rv)
    assert wd[0] == {"day": "Mon", "rvol": 1.5, "n": 2} and wd[1]["day"] == "Tue"
    days = pd.bdate_range("2025-08-25", "2025-09-05")
    exp = ind.nifty_expiry_days(days.drop(pd.Timestamp("2025-08-28")))  # a Thursday holiday
    assert pd.Timestamp("2025-08-27") in exp  # moved to the Wednesday
    assert pd.Timestamp("2025-09-02") in exp  # Tuesday from September 2025


def test_no_look_ahead_in_expected_z_and_alerts():
    """Cutting later bars never changes an earlier value (RESEARCH-RULES 6)."""
    rng = np.random.default_rng(5)
    days = sessions("2026-01-05", 70)
    frames = [day_bars(d, turnover=1.0, noise=rng) for d in days]
    t = ind.turnover(pd.concat(frames))
    m = ind.day_matrix(t)
    e_all = ind.expected_matrix(m)
    mu_all, sd_all = ind.zstats_matrix(m)
    for i in range(50):
        r = int(rng.integers(25, 70))
        c = int(rng.integers(0, 375))
        cut = t[t.index < days_ts(days[r]) + pd.Timedelta(minutes=c + 1)]
        mc = ind.day_matrix(cut)
        f = ind.full_days(mc).to_numpy()
        hist = mc.to_numpy()[:r]
        assert ind.expected_row(hist, f[:r])[c] == pytest.approx(e_all.iloc[r, c])
        mu, sd = ind.zstats_row(hist, f[:r])
        assert mu[c] == pytest.approx(mu_all.iloc[r, c], nan_ok=True)
        assert sd[c] == pytest.approx(sd_all.iloc[r, c], nan_ok=True)
        if i < 5:  # the study's matrix functions on the cut data, too
            assert ind.expected_matrix(mc).iloc[r, c] == e_all.iloc[r, c]
            mm, ms = ind.zstats_matrix(mc)
            assert mm.iloc[r, c] == pytest.approx(mu_all.iloc[r, c], nan_ok=True)
    z = rng.normal(2.5, 1, 300)
    cands = [C(i, "MARKET", "MARKET", float(z[i])) for i in range(300)]
    full = ind.pick_alerts(cands, z_market=3, z_stock=4, cooldown_min=15, cap=12)
    for k in (10, 50, 120, 299):
        part = ind.pick_alerts(cands[: k + 1], z_market=3, z_stock=4, cooldown_min=15, cap=12)
        assert part == [a for a in full if a.t <= k]


def days_ts(d) -> pd.Timestamp:
    return pd.Timestamp(f"{d} 09:15", tz=IST)


def test_row_versions_equal_the_matrix_versions():
    """The service's one-row baselines are the study's matrices' row for that session."""
    rng = np.random.default_rng(9)
    days = sessions("2026-01-05", 80)
    frames = [day_bars(d, turnover=1.0, noise=rng, minutes=60 if i == 40 else 375)
              for i, d in enumerate(days)]  # fmt: skip
    m = ind.day_matrix(ind.turnover(pd.concat(frames)))
    full = ind.full_days(m)
    e = ind.expected_matrix(m, full)
    mu, sd = ind.zstats_matrix(m, full)
    for r in (3, 30, 41, 79):
        hist, f = m.to_numpy()[:r], full.to_numpy()[:r]
        assert np.allclose(ind.expected_row(hist, f), e.to_numpy()[r])
        rm, rs = ind.zstats_row(hist, f)
        assert np.allclose(rm, mu.to_numpy()[r], equal_nan=True)
        assert np.allclose(rs, sd.to_numpy()[r], equal_nan=True)
