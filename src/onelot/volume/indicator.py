"""The Volume Watch indicator (`.agile/stories/VOL-API.md`): pure functions, no I/O.

Index bars carry no volume, so the market's volume is read from a basket of large stocks.
Everything here takes frames or arrays and returns frames, arrays or plain values:

* turnover (volume x close, rupees) and the bar's up share by one of three rules;
* day matrices: one row per session, one column per minute of the cash session (09:15 is
  column 0, 15:29 is column 374), the bar START in IST;
* baselines from the sessions BEFORE a day only: the expected turnover (median of the
  previous 20 full sessions at that minute) and the spike score's mean and sd (the previous
  60 full sessions); a session with fewer than 300 one-minute bars is not full;
* relative volume, flow share, states, influence weights (non-negative least squares) and
  pressure, alert decisions with a cooldown and a daily cap, and the day's profile.

The research script loads this module by file path, so it imports only numpy, pandas, scipy
and the standard library.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

IST = "Asia/Kolkata"
OPEN_MINUTE = 9 * 60 + 15  # 09:15, column 0 of a day matrix
SESSION_MINUTES = 375  # 09:15 to 15:29
FULL_MIN_BARS = 300  # a session with fewer one-minute bars is not full (Muhurat, half days)
EXPECTED_LOOKBACK = 20
EXPECTED_MIN = 5  # fewer prior full sessions than this: no expected turnover (0)
Z_LOOKBACK = 60
Z_MIN = 20  # fewer prior full sessions than this: no spike score
WINDOW = 5  # the "last 5 minutes"
BVC_LOOKBACK = 60
RULES: tuple[str, ...] = ("clv", "tick", "bvc")
STATES: tuple[str, ...] = ("quiet", "normal", "busy", "spike")


# ── small helpers ────────────────────────────────────────────────────────
def finite(x: Any) -> float | None:
    """A plain float, or None for None, NaN or infinity (the routes answer strict JSON)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def safe_ratio(num: Any, den: Any) -> Any:
    """num / den where den > 0, else NaN; works on scalars and arrays alike."""
    n = np.asarray(num, dtype=float)
    d = np.asarray(den, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(d > 0, n / np.where(d > 0, d, 1.0), np.nan)
    return float(out) if out.ndim == 0 else out


def minute_label(col: int) -> str:
    m = OPEN_MINUTE + int(col)
    return f"{m // 60:02d}:{m % 60:02d}"


def _ist_index(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    return idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)


IST_OFFSET_NS = (5 * 3600 + 30 * 60) * 1_000_000_000  # IST has no daylight saving
DAY_NS = 86_400 * 1_000_000_000


def _ist_ns(index: pd.Index) -> np.ndarray:
    """Nanoseconds of IST wall time since the epoch (fast: plain integer arithmetic)."""
    idx = _ist_index(index)
    return np.asarray(idx.asi8, dtype=np.int64) + IST_OFFSET_NS


def minute_of_day(index: pd.Index) -> np.ndarray:
    """IST minute of the day (09:15 = 555) of each bar start."""
    return np.asarray((_ist_ns(index) % DAY_NS) // 60_000_000_000, dtype=int)


def session_dates(index: pd.Index) -> pd.DatetimeIndex:
    """The IST date of each bar as a naive midnight timestamp."""
    return pd.DatetimeIndex((_ist_ns(index) // DAY_NS * DAY_NS).astype("datetime64[ns]"))


# ── turnover and up share ────────────────────────────────────────────────
def turnover(bars: pd.DataFrame) -> pd.Series:
    """Rupees traded in each bar: volume x close."""
    return (bars["volume"].astype(float) * bars["close"].astype(float)).rename("turnover")


def minute_returns(close: pd.Series) -> pd.Series:
    """Close-to-close simple returns inside a session; the first bar of each session is NaN
    (the overnight gap is not a minute's move)."""
    c = close.astype(float)
    days = session_dates(c.index)
    r = c.pct_change()
    first = np.r_[True, days[1:] != days[:-1]]
    r[first] = np.nan
    return r


def first_of_session(index: pd.Index) -> np.ndarray:
    """True for the first bar of each session."""
    d = _ist_ns(index) // DAY_NS
    return np.r_[True, d[1:] != d[:-1]] if len(d) else np.zeros(0, dtype=bool)


def up_share_values(
    o: np.ndarray,
    h: np.ndarray,
    lo: np.ndarray,
    c: np.ndarray,
    first: np.ndarray,
    rule: str = "clv",
    lookback: int = BVC_LOOKBACK,
) -> np.ndarray:
    """`up_share` on plain arrays (`first` marks the first bar of each session)."""
    if rule == "clv":
        rng = h - lo
        with np.errstate(divide="ignore", invalid="ignore"):
            u = np.where(rng > 0, (c - lo) / np.where(rng > 0, rng, 1.0), 0.5)
    elif rule == "tick":
        u = np.where(c > o, 1.0, np.where(c < o, 0.0, 0.5))
    elif rule == "bvc":
        prev = np.r_[np.nan, c[:-1]]
        with np.errstate(divide="ignore", invalid="ignore"):
            r_open = np.where(o > 0, c / np.where(o > 0, o, 1.0) - 1.0, 0.0)
            r = np.where(first, r_open, c / prev - 1.0)
        sd = (
            pd.Series(r)
            .shift(1)
            .rolling(lookback, min_periods=max(10, lookback // 3))
            .std(ddof=1)
            .to_numpy()
        )
        z = safe_ratio(r, sd)
        from scipy.special import ndtr  # type: ignore[import-untyped]

        u = np.where(np.isfinite(z), ndtr(np.nan_to_num(z)), 0.5)
    else:
        raise ValueError(f"unknown up-share rule {rule!r}; one of {RULES}")
    return np.clip(np.asarray(u, dtype=float), 0.0, 1.0)


def up_share(bars: pd.DataFrame, rule: str = "clv", lookback: int = BVC_LOOKBACK) -> pd.Series:
    """The part of a bar's turnover counted as buying, from 0 to 1.

    clv:  (close - low) / (high - low); 0.5 when high equals low.
    tick: 1 when close > open, 0 when close < open, 0.5 when equal.
    bvc:  Phi(r / sd), r the bar's close-to-close return inside the session (the first bar of
          a session: close against open), sd of the previous `lookback` such returns (this
          bar not included); 0.5 without an sd.
    """
    o, h, lo, c = (bars[k].astype(float).to_numpy() for k in ("open", "high", "low", "close"))
    u = up_share_values(o, h, lo, c, first_of_session(bars.index), rule, lookback)
    return pd.Series(u, index=bars.index, name="up")


def flows(bars: pd.DataFrame, rule: str = "clv") -> pd.DataFrame:
    """turnover, buying, selling and net of each bar."""
    t = turnover(bars)
    u = up_share(bars, rule)
    buy = t * u
    sell = t - buy
    return pd.DataFrame({"turnover": t, "buy": buy, "sell": sell, "net": buy - sell})


def flow_share(buy: Any, sell: Any) -> float | None:
    """buying / (buying + selling); None when nothing traded."""
    b, s = float(np.nansum(buy)), float(np.nansum(sell))
    return b / (b + s) if (b + s) > 0 else None


def direction(share: float | None) -> str:
    """BUYING at 0.60 or more, SELLING at 0.40 or less, TWO_WAY otherwise."""
    if share is None:
        return "TWO_WAY"
    if share >= 0.60:
        return "BUYING"
    if share <= 0.40:
        return "SELLING"
    return "TWO_WAY"


# ── day matrices ─────────────────────────────────────────────────────────
def day_matrix(values: pd.Series) -> pd.DataFrame:
    """Sessions x 375 minutes; NaN where the series has no bar. Bars outside 09:15 to 15:29
    are dropped. Rows are naive session dates, oldest first."""
    if values is None or len(values) == 0:
        return pd.DataFrame(
            np.empty((0, SESSION_MINUTES)), index=pd.DatetimeIndex([]), columns=range(375)
        )
    col = minute_of_day(values.index) - OPEN_MINUTE
    keep = (col >= 0) & (col < SESSION_MINUTES)
    days = session_dates(values.index)[keep]
    col = col[keep]
    v = np.asarray(values.to_numpy(dtype=float))[keep]
    uniq, row = np.unique(days.to_numpy(), return_inverse=True)
    out = np.full((len(uniq), SESSION_MINUTES), np.nan)
    out[row, col] = v
    return pd.DataFrame(out, index=pd.DatetimeIndex(uniq), columns=range(SESSION_MINUTES))


def bar_counts(mat: pd.DataFrame) -> pd.Series:
    """One-minute bars a session has (non-NaN cells of a day matrix)."""
    return mat.notna().sum(axis=1)


def full_days(mat: pd.DataFrame, min_bars: int = FULL_MIN_BARS) -> pd.Series:
    """True for a session with at least `min_bars` one-minute bars."""
    return bar_counts(mat) >= min_bars


def fill_through(row: np.ndarray, last_col: int) -> np.ndarray:
    """A session's row with the minutes up to `last_col` that had no bar set to 0 (no trade),
    and every minute after it NaN (not closed yet)."""
    out = np.array(row, dtype=float)
    head = out[: last_col + 1]
    head[np.isnan(head)] = 0.0
    out[last_col + 1 :] = np.nan
    return out


def window_sum(mat: Any, n: int = WINDOW) -> np.ndarray:
    """Sum over the minute and the n - 1 before it inside each session (fewer at the open);
    NaN where the minute itself is NaN."""
    a = np.atleast_2d(np.asarray(mat, dtype=float))
    cs = np.cumsum(np.nan_to_num(a), axis=1)
    lag = np.zeros_like(cs)
    lag[:, n:] = cs[:, :-n]
    out = cs - lag
    out[np.isnan(a)] = np.nan
    return out


def log_window(mat: Any, n: int = WINDOW) -> np.ndarray:
    """ln of the n-minute window sum; NaN where the sum is not above zero."""
    s = window_sum(mat, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(s > 0, np.log(np.where(s > 0, s, 1.0)), np.nan)


def _prior_full(
    mat: pd.DataFrame, full: pd.Series, lookback: int, min_n: int, how: str
) -> pd.DataFrame:
    """For every row of `mat`: a statistic per column over the `lookback` full sessions
    strictly BEFORE that row (NaN with fewer than `min_n`)."""
    f = full.reindex(mat.index).fillna(False).to_numpy(dtype=bool)
    rows = mat.loc[f]
    roll = rows.rolling(lookback, min_periods=min_n)
    if how == "median":
        stat = roll.median()
    elif how == "mean":
        stat = roll.mean()
    elif how == "std":
        stat = roll.std(ddof=1)
    else:  # pragma: no cover - internal
        raise ValueError(how)
    pos = np.searchsorted(rows.index.to_numpy(), mat.index.to_numpy(), side="left") - 1
    out = np.full(mat.shape, np.nan)
    ok = pos >= 0
    if ok.any() and len(stat):
        out[ok] = stat.to_numpy()[pos[ok]]
    return pd.DataFrame(out, index=mat.index, columns=mat.columns)


def expected_matrix(
    turn: pd.DataFrame,
    full: pd.Series | None = None,
    lookback: int = EXPECTED_LOOKBACK,
    min_n: int = EXPECTED_MIN,
) -> pd.DataFrame:
    """Expected turnover per session and minute: the median of that minute over the previous
    `lookback` full sessions (a full session's minute without a bar counts 0). 0 where there
    is no history."""
    full = full_days(turn) if full is None else full
    filled = turn.copy()
    f = full.reindex(turn.index).fillna(False).to_numpy(dtype=bool)
    filled.loc[f] = filled.loc[f].fillna(0.0)
    out = _prior_full(filled, full, lookback, min_n, "median")
    return out.fillna(0.0)


def zstats_matrix(
    turn: pd.DataFrame,
    full: pd.Series | None = None,
    lookback: int = Z_LOOKBACK,
    min_n: int = Z_MIN,
    n: int = WINDOW,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(mean, sd) per session and minute of ln(n-minute turnover) over the previous
    `lookback` full sessions; NaN where there is not enough history."""
    full = full_days(turn) if full is None else full
    f = full.reindex(turn.index).fillna(False).to_numpy(dtype=bool)
    filled = turn.copy()
    filled.loc[f] = filled.loc[f].fillna(0.0)
    q = pd.DataFrame(log_window(filled.to_numpy(), n), index=turn.index, columns=turn.columns)
    return (
        _prior_full(q, full, lookback, min_n, "mean"),
        _prior_full(q, full, lookback, min_n, "std"),
    )


def _prior_rows(rows: np.ndarray, full: Any, lookback: int, min_n: int, how: str) -> np.ndarray:
    """One row: the statistic per column over the last `lookback` full rows of `rows` (every
    row lies before the target session); NaN where fewer than `min_n` values. The same numbers
    as the matrix versions' row of that session, without the whole history's rolling."""
    a = np.asarray(rows, dtype=float).reshape(-1, SESSION_MINUTES)
    sel = a[np.asarray(full, dtype=bool)][-lookback:]
    out = np.full(SESSION_MINUTES, np.nan)
    if len(sel) == 0:
        return out
    cnt = np.isfinite(sel).sum(axis=0)
    ok = cnt >= max(min_n, 2 if how == "std" else 1)
    if not ok.any():
        return out
    part = sel[:, ok]
    if how == "median":
        out[ok] = np.nanmedian(part, axis=0)
    elif how == "mean":
        out[ok] = np.nanmean(part, axis=0)
    else:
        out[ok] = np.nanstd(part, axis=0, ddof=1)
    return out


def _filled(rows: Any, full: Any) -> np.ndarray:
    a = np.array(rows, dtype=float).reshape(-1, SESSION_MINUTES)
    f = np.asarray(full, dtype=bool)
    a[f] = np.nan_to_num(a[f])
    return a


def expected_row(
    hist: Any, full: Any, lookback: int = EXPECTED_LOOKBACK, min_n: int = EXPECTED_MIN
) -> np.ndarray:
    """`expected_matrix` for the session right after `hist` (rows oldest first)."""
    return np.nan_to_num(_prior_rows(_filled(hist, full), full, lookback, min_n, "median"))


def zstats_row(
    hist: Any, full: Any, lookback: int = Z_LOOKBACK, min_n: int = Z_MIN, n: int = WINDOW
) -> tuple[np.ndarray, np.ndarray]:
    """`zstats_matrix` for the session right after `hist` (rows oldest first)."""
    q = log_window(_filled(hist, full), n)
    return (
        _prior_rows(q, full, lookback, min_n, "mean"),
        _prior_rows(q, full, lookback, min_n, "std"),
    )


def zscore(q: Any, mean: Any, sd: Any) -> Any:
    """(q - mean) / sd where sd > 0, else NaN."""
    return safe_ratio(np.asarray(q, dtype=float) - np.asarray(mean, dtype=float), sd)


def rvol_rows(actual: np.ndarray, expected: np.ndarray, n: int = WINDOW) -> dict[str, np.ndarray]:
    """Relative volume along one session's minutes: 1 minute, the last n minutes (sums), and
    the day so far (sums). NaN where the expected part is 0."""
    a = np.asarray(actual, dtype=float)
    e = np.asarray(expected, dtype=float)
    e_masked = np.where(np.isnan(a), np.nan, e)
    a5 = window_sum(a, n)[0]
    e5 = window_sum(e_masked, n)[0]
    ad = np.where(np.isnan(a), np.nan, np.nancumsum(a))
    ed = np.where(np.isnan(a), np.nan, np.nancumsum(np.nan_to_num(e_masked)))
    return {
        "rvol_1m": safe_ratio(a, e),
        "rvol_5m": safe_ratio(a5, e5),
        "rvol_day": safe_ratio(ad, ed),
        "actual_5m": a5,
        "expected_5m": e5,
        "actual_day": ad,
        "expected_day": ed,
    }


def state(
    rvol_5m: float | None, z: float | None, z_alert: float, quiet: float = 0.6, busy: float = 1.5
) -> str:
    """quiet (rvol_5m below `quiet`), normal, busy (`busy` or more), spike (z at or above the
    alert level). Spike wins."""
    if z is not None and math.isfinite(z) and z >= z_alert:
        return "spike"
    if rvol_5m is None or not math.isfinite(rvol_5m):
        return "normal"
    if rvol_5m < quiet:
        return "quiet"
    if rvol_5m >= busy:
        return "busy"
    return "normal"


# ── influence and pressure ───────────────────────────────────────────────
def influence(index_ret: pd.Series, stock_rets: pd.DataFrame) -> tuple[pd.Series, float | None]:
    """Non-negative least squares of the index's one-minute returns on the stocks' (no
    intercept); weights scaled to sum to 1, and r2 = 1 - SSE / SST of that fit. Rows where
    the index has no return are dropped; a stock without a bar that minute counts 0."""
    from scipy.optimize import nnls  # type: ignore[import-untyped]

    cols = list(stock_rets.columns)
    zero = pd.Series(0.0, index=cols, dtype=float)
    y = index_ret.dropna()
    if y.empty or not cols:
        return zero, None
    x = stock_rets.reindex(y.index).fillna(0.0)
    ok = np.isfinite(y.to_numpy()) & np.isfinite(x.to_numpy()).all(axis=1)
    yv, xv = y.to_numpy()[ok], x.to_numpy()[ok]
    if len(yv) < max(30, 2 * len(cols)):
        return zero, None
    w, _ = nnls(xv, yv, maxiter=50 * len(cols))
    resid = yv - xv @ w
    sst = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / sst if sst > 0 else None
    total = float(w.sum())
    weights = pd.Series(w / total if total > 0 else w * 0.0, index=cols, dtype=float)
    return weights, r2


def pressure(
    weights: Mapping[str, float], net: Mapping[str, float], expected: Mapping[str, float]
) -> float | None:
    """sum of weight x (net flow / expected turnover of the same minutes); a stock without an
    expected turnover adds nothing. None without any usable stock."""
    total, used = 0.0, 0
    for k, w in weights.items():
        e = expected.get(k)
        nf = net.get(k)
        if not w or e is None or nf is None or not (e > 0) or not math.isfinite(nf):
            continue
        total += float(w) * float(nf) / float(e)
        used += 1
    return total if used else None


# ── alert decisions ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Candidate:
    """A minute at which a scope may alert: t in minutes (any monotone clock)."""

    t: int
    scope: str  # MARKET | STOCK
    key: str
    z: float


def pick_alerts(
    candidates: Iterable[Candidate],
    *,
    z_market: float,
    z_stock: float,
    cooldown_min: int,
    cap: int,
    earlier: Sequence[tuple[int, str, str]] = (),
) -> list[Candidate]:
    """The candidates that become alerts, in time order: z at or above the scope's level, no
    alert of the same scope and key in the `cooldown_min` minutes before, and fewer than `cap`
    alerts that day (`earlier` counts: the alerts already raised, as (t, scope, key)). Inside
    one minute the market goes first, then stocks by z, largest first. A decision depends on
    the candidates up to its own minute only."""
    last: dict[tuple[str, str], int] = {}
    count = 0
    for t, scope, key in earlier:
        k = (scope, key)
        last[k] = max(last.get(k, t), t)
        count += 1
    ordered = sorted(candidates, key=lambda c: (c.t, 0 if c.scope == "MARKET" else 1, -c.z, c.key))
    out: list[Candidate] = []
    for c in ordered:
        if count >= cap:
            break
        level = z_market if c.scope == "MARKET" else z_stock
        if not math.isfinite(c.z) or c.z < level:
            continue
        prev = last.get((c.scope, c.key))
        if prev is not None and c.t - prev < cooldown_min:
            continue
        out.append(c)
        last[(c.scope, c.key)] = c.t
        count += 1
    return out


# ── the day's profile ────────────────────────────────────────────────────
def half_hours() -> list[tuple[int, int]]:
    """(first column, last column) of each half hour of the session; the last is 15 minutes."""
    return [(a, min(a + 29, SESSION_MINUTES - 1)) for a in range(0, SESSION_MINUTES, 30)]


def profile(turn_full: pd.DataFrame) -> dict[str, Any]:
    """The shape of a normal day from full sessions only (sessions x 375 basket turnover):
    per minute the median, 25th and 75th percentile; each half hour's mean share of the
    day's turnover with its rank (1 = busiest); the two busiest and two quietest whole half
    hours; and the quietest whole hour."""
    m = turn_full.fillna(0.0).to_numpy(dtype=float)
    if m.size == 0:
        return {"sessions": 0, "minutes": [], "half_hours": [], "busiest": [], "quietest": []}
    q25, q50, q75 = np.percentile(m, [25, 50, 75], axis=0)
    totals = m.sum(axis=1)
    keep = totals > 0
    shares = []
    for a, b in half_hours():
        part = m[keep, a : b + 1].sum(axis=1) / totals[keep]
        shares.append(float(part.mean()) if keep.any() else 0.0)
    order = sorted(range(len(shares)), key=lambda i: -shares[i])
    rank = {i: r + 1 for r, i in enumerate(order)}
    hh = [
        {
            "from": minute_label(a),
            "to": minute_label(b),
            "share": round(shares[i], 4),
            "rank": rank[i],
            "whole": (b - a + 1) == 30,
        }
        for i, (a, b) in enumerate(half_hours())
    ]
    whole = [h for h in hh if h["whole"]]
    busiest = sorted(whole, key=lambda h: -h["share"])[:2]
    quietest = sorted(whole, key=lambda h: h["share"])[:2]
    pairs = [(whole[i], whole[i + 1]) for i in range(len(whole) - 1)]
    quiet_hour = min(pairs, key=lambda p: p[0]["share"] + p[1]["share"]) if pairs else None
    return {
        "sessions": int(len(m)),
        "minutes": [
            [minute_label(j), float(q50[j]), float(q25[j]), float(q75[j])]
            for j in range(SESSION_MINUTES)
        ],
        "half_hours": [{k: v for k, v in h.items() if k != "whole"} for h in hh],
        "busiest": [{"from": h["from"], "to": h["to"], "share": h["share"]} for h in busiest],
        "quietest": [{"from": h["from"], "to": h["to"], "share": h["share"]} for h in quietest],
        "quiet_hour": (quiet_hour[0]["from"], quiet_hour[1]["to"]) if quiet_hour else None,
        "first_share": hh[0]["share"] if hh else None,
    }


def day_rvol(turn: pd.DataFrame, expected: pd.DataFrame) -> pd.Series:
    """A session's turnover over its expected turnover (whole-day sums); NaN without one."""
    a = turn.fillna(0.0).sum(axis=1).to_numpy()
    e = expected.reindex(turn.index).fillna(0.0).sum(axis=1).to_numpy()
    return pd.Series(safe_ratio(a, e), index=turn.index)


def weekday_rvol(rv: pd.Series) -> list[dict[str, Any]]:
    """Mean day rvol by weekday, Monday first; weekdays without sessions are left out."""
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    s = rv.dropna()
    out = []
    for d in range(7):
        part = s[pd.DatetimeIndex(s.index).weekday == d]
        if len(part):
            out.append({"day": names[d], "rvol": round(float(part.mean()), 3), "n": len(part)})
    return out


def nifty_expiry_days(sessions: Iterable[Any]) -> set[pd.Timestamp]:
    """NIFTY's weekly expiry sessions among `sessions`: Thursday until 2025-08-31, Tuesday
    from 2025-09-01 (SEBI's change); a holiday moves it to the session before in that week."""
    days = sorted({pd.Timestamp(d).normalize() for d in sessions})
    by_week: dict[tuple[int, int], list[pd.Timestamp]] = {}
    for d in days:
        iso = d.isocalendar()
        by_week.setdefault((iso[0], iso[1]), []).append(d)
    out: set[pd.Timestamp] = set()
    switch = pd.Timestamp("2025-09-01")
    for week in by_week.values():
        target_wd = 3 if week[0] < switch else 1
        on_or_before = [d for d in week if d.weekday() <= target_wd]
        if on_or_before:
            out.add(max(on_or_before))
    return out
