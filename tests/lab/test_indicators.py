import numpy as np
import pandas as pd
import pytest

from onelot.indicators import core as ind

talib = pytest.importorskip("talib")


def _close(a, b, tol=1e-6, skip=0):
    a = np.asarray(a, dtype=float)[skip:]
    b = np.asarray(b, dtype=float)[skip:]
    mask = ~(np.isnan(a) | np.isnan(b))
    assert mask.sum() > 50
    np.testing.assert_allclose(a[mask], b[mask], rtol=tol, atol=tol)


def test_sma_ema_match_talib(bars5):
    c = bars5["close"]
    _close(ind.sma(c, 20), talib.SMA(c.to_numpy(), 20))
    # pandas EMA seeds with the first value, TA-Lib with an SMA: identical after warm-up
    _close(ind.ema(c, 20), talib.EMA(c.to_numpy(), 20), tol=1e-6, skip=200)


def test_atr_rsi_adx_match_talib(bars5):
    h, l, c = bars5["high"], bars5["low"], bars5["close"]
    _close(ind.atr(h, l, c, 14), talib.ATR(h.to_numpy(), l.to_numpy(), c.to_numpy(), 14), tol=1e-8)
    _close(ind.rsi(c, 14), talib.RSI(c.to_numpy(), 14), tol=1e-8)
    a = ind.adx(h, l, c, 14)
    _close(
        a["plus_di"],
        talib.PLUS_DI(h.to_numpy(), l.to_numpy(), c.to_numpy(), 14),
        tol=1e-4,
        skip=100,
    )
    _close(
        a["minus_di"],
        talib.MINUS_DI(h.to_numpy(), l.to_numpy(), c.to_numpy(), 14),
        tol=1e-4,
        skip=100,
    )
    _close(a["adx"], talib.ADX(h.to_numpy(), l.to_numpy(), c.to_numpy(), 14), tol=1e-4, skip=150)


def test_macd_and_bbands_match_talib(bars5):
    c = bars5["close"]
    m = ind.macd(c)
    tm, ts, th = talib.MACD(c.to_numpy(), 12, 26, 9)
    # different EMA seeding; the gap decays geometrically and is negligible after warm-up
    _close(m["macd"], tm, tol=1e-4, skip=250)
    _close(m["macd_signal"], ts, tol=1e-4, skip=250)
    bb = ind.bollinger(c, 20, 2.0)
    tu, tmid, tl = talib.BBANDS(c.to_numpy(), 20, 2.0, 2.0)
    _close(bb["bb_upper"], tu, tol=1e-8)
    _close(bb["bb_lower"], tl, tol=1e-8)


def test_supertrend_flips_with_price(bars5):
    st = ind.supertrend(bars5["high"], bars5["low"], bars5["close"], 10, 3.0)
    d = st["st_dir"].to_numpy()
    c = bars5["close"].to_numpy()
    line = st["st_line"].to_numpy()
    valid = ~np.isnan(line)
    # in an uptrend the line is below price, in a downtrend above
    assert np.all(c[valid & (d == 1)] >= line[valid & (d == 1)] - 1e-9)
    assert np.all(c[valid & (d == -1)] <= line[valid & (d == -1)] + 1e-9)
    assert set(np.unique(d[valid])) <= {-1.0, 1.0}


def test_session_vwap_resets_daily_and_handles_zero_volume(bars5):
    v = ind.session_vwap(bars5)
    first_bars = ind.session_open_time_mask(bars5.index)
    tp = (bars5["high"] + bars5["low"] + bars5["close"]) / 3
    np.testing.assert_allclose(v[first_bars], tp[first_bars])
    idx_df = bars5.assign(volume=0.0)
    assert ind.session_vwap(idx_df).isna().all()


def test_daily_context_has_no_lookahead(bars5):
    ctx = ind.daily_context(bars5)
    days = sorted(set(bars5.index.date))
    d2 = bars5[bars5.index.date == days[1]]
    d1 = bars5[bars5.index.date == days[0]]
    assert ctx.loc[d2.index, "pd_high"].iloc[0] == pytest.approx(d1["high"].max())
    assert ctx.loc[d1.index, "pd_high"].isna().all()


def test_swing_points_are_lagged(bars5):
    sp = ind.swing_points(bars5["high"], bars5["low"], 3, 3)
    # a pivot can only be reported 3 bars after it happened
    raw = bars5["high"] == bars5["high"].rolling(7, center=True).max()
    first_raw = np.flatnonzero(raw.to_numpy())[0]
    first_rep = np.flatnonzero(sp["pivot_high"].notna().to_numpy())[0]
    assert first_rep == first_raw + 3


def test_tod_rvol_baseline(bars5):
    r = ind.tod_rvol(bars5["volume"], days=3)
    assert r.iloc[: 75 * 3].isna().all()
    assert r.iloc[75 * 3 :].notna().all()


def test_divergence_detection_simple():
    idx = pd.date_range("2026-01-01 09:15", periods=40, freq="5min", tz="Asia/Kolkata")
    # peaks at bar 10 (105) and bar 20 (107, higher high), trough at 15; oscillator peaks 70 then 60
    p = [100 + i * 0.5 for i in range(11)] + [105 - (i - 10) * 0.8 for i in range(11, 16)]
    p += [101 + (i - 15) * 1.2 for i in range(16, 21)] + [
        107 - (i - 20) * 0.7 for i in range(21, 40)
    ]
    o = [40 + i * 3 for i in range(11)] + [70 - (i - 10) * 4 for i in range(11, 16)]
    o += [50 + (i - 15) * 2 for i in range(16, 21)] + [60 - (i - 20) * 1.5 for i in range(21, 40)]
    price, osc = pd.Series(p, index=idx), pd.Series(o, index=idx)
    div = ind.bearish_divergence(price, osc, lookback=20, left=3, right=2)
    assert div.iloc[22]
    assert div.sum() == 1
