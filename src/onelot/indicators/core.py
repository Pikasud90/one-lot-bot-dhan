"""Vectorised technical indicators (numpy/pandas). Same functions serve backtests and the live engine,
which recomputes on each bar close over a trailing window - so live and backtest signals match.

Conventions: inputs are pandas Series aligned on a DatetimeIndex (bar start, IST); Wilder smoothing
(RMA) is seeded with an SMA like TA-Lib; outputs keep the input index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------- moving averages
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average, SMA-seeded (TA-Lib compatible)."""
    x = s.to_numpy(dtype="float64")
    out = np.full_like(x, np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < n:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed_end = start + n
    if seed_end > len(x):
        return pd.Series(out, index=s.index)
    out[seed_end - 1] = np.nanmean(x[start:seed_end])
    a = 1.0 / n
    prev = out[seed_end - 1]
    for i in range(seed_end, len(x)):
        xi = x[i]
        if not np.isnan(xi):
            prev = prev + a * (xi - prev)
        out[i] = prev
    return pd.Series(out, index=s.index)


def hma(s: pd.Series, n: int) -> pd.Series:
    """Hull moving average (low lag)."""
    half, root = max(1, n // 2), max(1, int(np.sqrt(n)))

    def wma(x: pd.Series, k: int) -> pd.Series:
        w = np.arange(1, k + 1, dtype="float64")
        return x.rolling(k, min_periods=k).apply(lambda v: np.dot(v, w) / w.sum(), raw=True)

    return wma(2 * wma(s, half) - wma(s, n), root)


# ----------------------------------------------------------------------------- volatility
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    if len(tr):
        tr.iloc[0] = np.nan  # undefined without a previous close (TA-Lib convention)
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), n)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower)
    return pd.DataFrame(
        {"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width, "bb_pctb": pct_b}
    )


def keltner(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = 20,
    mult: float = 1.5,
    atr_n: int = 10,
) -> pd.DataFrame:
    mid = ema(close, n)
    a = atr(high, low, close, atr_n)
    return pd.DataFrame({"kc_mid": mid, "kc_upper": mid + mult * a, "kc_lower": mid - mult * a})


def donchian(
    high: pd.Series, low: pd.Series, n: int = 20, exclude_current: bool = True
) -> pd.DataFrame:
    h, l = (high.shift(1), low.shift(1)) if exclude_current else (high, low)
    return pd.DataFrame(
        {
            "dc_upper": h.rolling(n, min_periods=n).max(),
            "dc_lower": l.rolling(n, min_periods=n).min(),
        }
    )


def choppiness(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Choppiness Index: ~>61.8 range-bound, ~<38.2 trending."""
    tr_sum = true_range(high, low, close).rolling(n, min_periods=n).sum()
    rng = high.rolling(n, min_periods=n).max() - low.rolling(n, min_periods=n).min()
    with np.errstate(divide="ignore", invalid="ignore"):
        ci = 100 * np.log10(tr_sum / rng) / np.log10(n)
    return ci.replace([np.inf, -np.inf], np.nan)


def percentile_rank(s: pd.Series, window: int = 100) -> pd.Series:
    """Rank of the latest value within the trailing window, 0..100."""
    return s.rolling(window, min_periods=max(10, window // 4)).rank(pct=True) * 100


# ----------------------------------------------------------------------------- momentum / trend
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = rma(up, n), rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        out = 100 - 100 / (1 + rs)
    out = out.where(ad != 0, 100.0).where(~(au.isna() | ad.isna()))
    out = out.where(~((au == 0) & (ad == 0)), 50.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    line = line.where(np.arange(len(close)) >= slow - 1)
    sig = sig.where(np.arange(len(close)) >= slow + signal - 2)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    """Wilder ADX with +DI / -DI."""
    up = high.diff()
    dn = -low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    tr = true_range(high, low, close)
    atr_ = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * rma(plus_dm, n) / atr_
        mdi = 100 * rma(minus_dm, n) / atr_
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    dx = dx.replace([np.inf, -np.inf], np.nan)
    return pd.DataFrame({"adx": rma(dx, n), "plus_di": pdi, "minus_di": mdi})


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, mult: float = 3.0
) -> pd.DataFrame:
    """Supertrend line and direction (+1 up / -1 down), standard final-band algorithm."""
    a = atr(high, low, close, period).to_numpy()
    h, l, c = high.to_numpy(float), low.to_numpy(float), close.to_numpy(float)
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * a, hl2 - mult * a
    n = len(c)
    fub, flb = np.full(n, np.nan), np.full(n, np.nan)
    line, direction = np.full(n, np.nan), np.zeros(n)
    first = np.flatnonzero(~np.isnan(a))
    if len(first) == 0:
        return pd.DataFrame({"st_line": line, "st_dir": direction}, index=close.index)
    s = first[0]
    fub[s], flb[s] = ub[s], lb[s]
    direction[s] = 1 if c[s] >= hl2[s] else -1
    line[s] = flb[s] if direction[s] == 1 else fub[s]
    for i in range(s + 1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if c[i] < flb[i] else 1
        else:
            direction[i] = 1 if c[i] > fub[i] else -1
        line[i] = flb[i] if direction[i] == 1 else fub[i]
    return pd.DataFrame({"st_line": line, "st_dir": direction}, index=close.index)


def roc(close: pd.Series, n: int = 10) -> pd.Series:
    return close.pct_change(n) * 100


def linreg_slope(s: pd.Series, n: int = 20) -> pd.Series:
    x = np.arange(n, dtype="float64")
    xm = x - x.mean()
    denom = (xm**2).sum()
    return s.rolling(n, min_periods=n).apply(lambda v: np.dot(xm, v - v.mean()) / denom, raw=True)


# ----------------------------------------------------------------------------- session / volume
def session_key(index: pd.DatetimeIndex) -> np.ndarray:
    return index.normalize().asi8


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP from typical price. NaN where cumulative volume is 0 (e.g. indices)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].fillna(0)
    key = session_key(df.index)
    cum_pv = (tp * vol).groupby(key).cumsum()
    cum_v = vol.groupby(key).cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        v = cum_pv / cum_v
    return v.where(cum_v > 0)


def session_open_time_mask(index: pd.DatetimeIndex) -> np.ndarray:
    key = session_key(index)
    return np.r_[True, key[1:] != key[:-1]]


def bar_of_day(index: pd.DatetimeIndex) -> pd.Series:
    key = pd.Series(session_key(index), index=index)
    return key.groupby(key).cumcount()


def rvol(volume: pd.Series, n: int = 20) -> pd.Series:
    """Volume relative to its trailing n-bar mean (excluding the current bar)."""
    base = volume.shift(1).rolling(n, min_periods=max(3, n // 2)).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        return (volume / base).replace([np.inf, -np.inf], np.nan)


def tod_rvol(volume: pd.Series, days: int = 10) -> pd.Series:
    """Time-of-day relative volume: this bar vs the same bar-of-day over the prior `days` sessions."""
    slot = bar_of_day(volume.index).to_numpy()
    base = volume.groupby(slot).transform(lambda v: v.shift(1).rolling(days, min_periods=3).mean())
    with np.errstate(divide="ignore", invalid="ignore"):
        out = volume / base
    return out.replace([np.inf, -np.inf], np.nan)


def cumulative_session_volume(df: pd.DataFrame) -> pd.Series:
    return df["volume"].groupby(session_key(df.index)).cumsum()


# ----------------------------------------------------------------------------- structure
def swing_points(high: pd.Series, low: pd.Series, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Confirmed pivot highs/lows (known only `right` bars later -> shifted to avoid look-ahead)."""
    w = left + right + 1
    is_high = high == high.rolling(w, center=True, min_periods=w).max()
    is_low = low == low.rolling(w, center=True, min_periods=w).min()
    ph = high.where(is_high).shift(right)
    pl = low.where(is_low).shift(right)
    return pd.DataFrame({"pivot_high": ph, "pivot_low": pl})


def bearish_divergence(
    price_high: pd.Series, osc: pd.Series, lookback: int = 20, left: int = 3, right: int = 2
) -> pd.Series:
    """True on bars where the latest confirmed price pivot high is higher than the previous one
    within `lookback` bars while the oscillator made a lower high."""
    piv = swing_points(price_high, price_high, left, right)["pivot_high"]
    osc_at = osc.shift(right).where(piv.notna())
    out = np.zeros(len(price_high), dtype=bool)
    last_p = last_o = None
    last_i = -(10**9)
    for i, (p, o) in enumerate(zip(piv.to_numpy(), osc_at.to_numpy(), strict=True)):
        if not np.isnan(p):
            if last_p is not None and i - last_i <= lookback and p > last_p and o < last_o:
                out[i] = True
            last_p, last_o, last_i = p, o, i
    return pd.Series(out, index=price_high.index)


def bullish_divergence(
    price_low: pd.Series, osc: pd.Series, lookback: int = 20, left: int = 3, right: int = 2
) -> pd.Series:
    inv = bearish_divergence(-price_low, -osc, lookback, left, right)
    return inv


def volume_divergence(
    close: pd.Series, volume: pd.Series, n: int = 20, factor: float = 0.7, bars: int = 3
) -> pd.Series:
    """New n-bar high made on volume below `factor` x its average for `bars` bars (fading push)."""
    new_high = close >= close.rolling(n, min_periods=n).max()
    weak = (volume < factor * volume.rolling(n, min_periods=n).mean()).rolling(
        bars, min_periods=bars
    ).sum() >= bars
    return new_high & weak


# ----------------------------------------------------------------------------- daily context
def daily_context(
    intraday: pd.DataFrame, daily: pd.DataFrame | None = None, atr_n: int = 14
) -> pd.DataFrame:
    """Map previous-session OHLC and daily ATR onto each intraday bar (no look-ahead)."""
    if daily is None or daily.empty:
        key = intraday.index.normalize()
        daily = intraday.groupby(key).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        )
    d = daily.copy()
    d.index = pd.DatetimeIndex(d.index).normalize()
    d["d_atr"] = atr(d["high"], d["low"], d["close"], atr_n)
    prev = d[["open", "high", "low", "close", "d_atr"]].shift(1)
    prev.columns = ["pd_open", "pd_high", "pd_low", "pd_close", "d_atr"]
    key = intraday.index.normalize()
    mapped = prev.reindex(key)
    mapped.index = intraday.index
    return mapped
