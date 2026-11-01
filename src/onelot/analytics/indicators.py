"""Technical indicators as pure functions over numpy arrays.

Conventions:
  * Every function returns an array the same length as its input.
  * Values that are not yet defined are NaN, never zero and never forward-filled.
    A zero would silently pass a threshold test; NaN cannot (v1 defect B4).
  * Wilder's smoothing is used for RSI/ATR/ADX, matching standard charting
    packages. Simple EMA smoothing gives visibly different values and would
    make the system disagree with what you see on your broker's chart.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

ArrayLike = np.ndarray | list[float]


def _arr(x: ArrayLike) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(values: ArrayLike, period: int) -> np.ndarray:
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    if period <= 0 or len(v) < period:
        return out
    c = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1 :] = (c[period:] - c[:-period]) / period
    return out


def ema(values: ArrayLike, period: int) -> np.ndarray:
    """EMA seeded with the SMA of the first `period` values (TA-Lib convention)."""
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    n = len(v)
    if period <= 0 or n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = v[:period].mean()
    for i in range(period, n):
        out[i] = alpha * v[i] + (1.0 - alpha) * out[i - 1]
    return out


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's recursive average: seed with a simple mean, then 1/period decay."""
    out = np.full(values.shape, np.nan)
    n = len(values)
    if period <= 0 or n < period:
        return out
    out[period - 1] = np.nanmean(values[:period])
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def rsi(values: ArrayLike, period: int = 14) -> np.ndarray:
    """Wilder's RSI. Returns NaN until `period` changes are available."""
    v = _arr(values)
    n = len(v)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    delta = np.diff(v)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()

    def _rsi_from(g: float, ls: float) -> float:
        if ls == 0.0:
            return 100.0 if g > 0 else 50.0
        rs = g / ls
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def true_range(high: ArrayLike, low: ArrayLike, close: ArrayLike) -> np.ndarray:
    h, l_, c = _arr(high), _arr(low), _arr(close)
    n = len(h)
    tr = np.full(n, np.nan)
    if n == 0:
        return tr
    tr[0] = h[0] - l_[0]
    if n > 1:
        prev = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l_[1:], np.abs(h[1:] - prev), np.abs(l_[1:] - prev)])
    return tr


def atr(high: ArrayLike, low: ArrayLike, close: ArrayLike, period: int = 14) -> np.ndarray:
    return _wilder_smooth(true_range(high, low, close), period)


def adx(
    high: ArrayLike, low: ArrayLike, close: ArrayLike, period: int = 14
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wilder's ADX. Returns (adx, plus_di, minus_di).

    ADX is defined only after roughly 2*period bars: `period` to smooth DM/TR
    and another `period` to smooth DX. The warm-up loader must supply enough
    prior-session bars or this is NaN for the first ~2.5 hours (v1 defect B4).
    """
    h, l_, c = _arr(high), _arr(low), _arr(close)
    n = len(h)
    nan = np.full(n, np.nan)
    if n < period + 1:
        return nan, nan.copy(), nan.copy()

    up = np.zeros(n)
    dn = np.zeros(n)
    up_move = h[1:] - h[:-1]
    dn_move = l_[:-1] - l_[1:]
    up[1:] = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    dn[1:] = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)

    tr = true_range(h, l_, c)
    # Wilder smoothing over bars 1..n (bar 0 has no directional movement).
    str_ = _wilder_smooth(tr[1:], period)
    sup = _wilder_smooth(up[1:], period)
    sdn = _wilder_smooth(dn[1:], period)

    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di[1:] = np.where(str_ > 0, 100.0 * sup / str_, np.nan)
        minus_di[1:] = np.where(str_ > 0, 100.0 * sdn / str_, np.nan)

    denom = plus_di + minus_di
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)

    adx_out = np.full(n, np.nan)
    valid = np.where(~np.isnan(dx))[0]
    if len(valid) >= period:
        start = valid[period - 1]
        adx_out[start] = np.nanmean(dx[valid[:period]])
        for i in range(start + 1, n):
            if not np.isnan(dx[i]) and not np.isnan(adx_out[i - 1]):
                adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    return adx_out, plus_di, minus_di


def is_rising(series: ArrayLike, lookback: int = 3) -> bool:
    """'Rising' made explicit — v1 said 'ADX > 25 and rising' without defining it."""
    v = _arr(series)
    valid = v[~np.isnan(v)]
    if len(valid) < lookback + 1:
        return False
    return bool(valid[-1] > valid[-1 - lookback])


def vwap(high: ArrayLike, low: ArrayLike, close: ArrayLike, volume: ArrayLike) -> np.ndarray:
    """Session-cumulative VWAP over typical price.

    *** v1 defect B3 ***
    VWAP requires real traded volume. The NIFTY BANK INDEX has none — it is a
    computed number that does not trade, and the feed returns volume=0 for it.
    Applying this to index candles yields 0/0.

    This function returns NaN rather than a plausible-looking number, so the
    mistake surfaces as a blocked gate instead of a silent lie. Feed it the
    FUTURES series.
    """
    h, l_, c, v = _arr(high), _arr(low), _arr(close), _arr(volume)
    n = len(h)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    if np.nansum(v) <= 0:
        log.debug("vwap(): total volume is zero — returning NaN (index series?)")
        return out
    tp = (h + l_ + c) / 3.0
    cum_pv = np.nancumsum(tp * v)
    cum_v = np.nancumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(cum_v > 0, cum_pv / cum_v, np.nan)
    return out


def has_tradeable_volume(volume: ArrayLike) -> bool:
    """Guard for anything volume-based. Call before trusting VWAP or divergence."""
    v = _arr(volume)
    return bool(len(v) > 0 and np.nansum(v) > 0)


def percentile_rank(history: ArrayLike, value: float) -> float:
    """Percent of historical observations strictly below `value`, in [0, 100]."""
    h = _arr(history)
    h = h[~np.isnan(h)]
    if len(h) == 0:
        return float("nan")
    return float(100.0 * np.sum(h < value) / len(h))


def rolling_percentile_of_last(series: ArrayLike, lookback: int) -> float:
    """Percentile rank of the most recent value within its own trailing window."""
    v = _arr(series)
    v = v[~np.isnan(v)]
    if len(v) < 2:
        return float("nan")
    window = v[-lookback:] if lookback and len(v) > lookback else v
    return percentile_rank(window[:-1], float(window[-1]))
