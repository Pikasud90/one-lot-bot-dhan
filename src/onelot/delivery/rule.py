"""The pullback rule (US-067 R1 to R8), pure: arrays in, readings out. No I/O, no clock.

R1 entry on the close of session t: close > SMA200; NIFTY's close of that date > NIFTY's SMA200;
the close is at least `depth` below the highest close of the last `high_lookback` sessions and
was not so on t-1 (its own window); the caller adds "no open or pending trade in the stock".
R2 entry fill: next open x (1 + slippage). R3 exit on the close of u >= entry session, the first
of: close > SMA20; open `max_sessions` sessions (the entry session counts as 1); close <= entry
fill x (1 - stop). Exit fill: next open x (1 - slippage). R4 after an exit signal on u the
earliest new entry is the close of u + 1. R5 quantity nearest `unit_inr` rupees.

The SMAs are taken with a cumulative sum over the whole array, as the reference implementation
(`scripts/alpha_pullback_check.py`) does, so the fixtures' 247 trades are reproduced exactly.
A value at t depends on the bars up to t only (R8).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np

from ..costs import CostModel

#: R3's exit reasons, in R3's order (the order a reason is shown in when several hold)
EXIT_SMA = "SMA20"
EXIT_TIME = "TIME"
EXIT_STOP = "STOP"
EXIT_ORDER = (EXIT_SMA, EXIT_TIME, EXIT_STOP)


@dataclass(frozen=True)
class PullbackParams:
    """The rule's settings (`config/desk.yaml`, slot `pullback`)."""

    depth: float = 0.08
    high_lookback: int = 20
    trend_sma: int = 200
    exit_sma: int = 20
    max_sessions: int = 30
    stop: float = 0.15
    slippage: float = 0.0003
    unit_inr: float = 10000.0

    def __post_init__(self) -> None:
        if not 0 < self.depth < 1 or not 0 < self.stop < 1 or not 0 <= self.slippage < 0.01:
            raise ValueError(f"pullback: depth, stop or slippage out of range: {self}")
        if min(self.high_lookback, self.trend_sma, self.exit_sma, self.max_sessions) < 2:
            raise ValueError(f"pullback: a window or the session limit is below 2: {self}")
        if not self.unit_inr > 0:
            raise ValueError(f"pullback: unit_inr must be positive, not {self.unit_inr!r}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> PullbackParams:
        """From a slot's params; an unknown name is an error, never silently ignored. Keys
        that are not rule settings (`evidence`) are left to the caller and allowed here."""
        raw = dict(raw or {})
        names = {f.name: f.type for f in fields(cls)}
        extra = sorted(set(raw) - set(names) - NON_RULE_KEYS)
        if extra:
            raise ValueError(f"pullback: unknown setting(s) {extra}")
        kw: dict[str, Any] = {}
        for k in names:
            if k in raw:
                kw[k] = int(raw[k]) if k in INT_KEYS else float(raw[k])
        return cls(**kw)

    @property
    def min_bars(self) -> int:
        """Bars needed before the first possible signal (index trend_sma, so trend_sma + 1)."""
        return max(self.trend_sma, self.high_lookback) + 1


INT_KEYS = frozenset({"high_lookback", "trend_sma", "exit_sma", "max_sessions"})
#: slot params that are not rule settings
NON_RULE_KEYS = frozenset({"evidence"})


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average, NaN before the n-th value (the reference's cumsum form)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def nifty_ok(
    dates: Sequence[Any], nifty_dates: Sequence[Any], nifty_close: np.ndarray, p: PullbackParams
) -> np.ndarray:
    """R1(b) per date of `dates`: NIFTY's close of that date is above its SMA200. False where
    NIFTY has no bar that date or fewer than `trend_sma` bars up to it."""
    nc = np.asarray(nifty_close, dtype=float)
    above = nc > sma(nc, p.trend_sma)  # NaN compares False
    by_date = dict(zip(list(nifty_dates), above.tolist(), strict=True))
    return np.array([bool(by_date.get(d, False)) for d in dates], dtype=bool)


def quantity(close: float, unit_inr: float) -> int:
    """R5: max(1, floor(unit_inr / close + 0.5)) shares, fixed at the signal.

    Delivery trades only. The rest of the desk keeps one unit (one share, one option lot); the
    owner changed this on purpose for the pullback delivery trades on 2026-11-12."""
    if not (math.isfinite(close) and close > 0):
        raise ValueError(f"pullback: no quantity for a close of {close!r}")
    return max(1, math.floor(unit_inr / close + 0.5))


def entry_fill(open_: float, p: PullbackParams) -> float:
    return float(open_) * (1.0 + p.slippage)


def exit_fill(open_: float, p: PullbackParams) -> float:
    return float(open_) * (1.0 - p.slippage)


def _deep(close: np.ndarray, t: int, p: PullbackParams) -> tuple[bool, float]:
    """R1(c) at t: (close[t] <= (1 - depth) x the highest close of t-L+1..t, that high)."""
    hi = float(np.max(close[t - p.high_lookback + 1 : t + 1]))
    return bool(close[t] <= (1.0 - p.depth) * hi), hi


@dataclass(frozen=True)
class EntryReading:
    """What R1 sees on the close of session t (also the stock card's pullback reading)."""

    t: int
    ready: bool  # enough bars for the rule
    signal: bool  # R1 (a) to (d) all hold; (e) is the caller's
    close: float | None = None
    high: float | None = None  # highest close of the last high_lookback sessions
    below_high_pct: float | None = None  # how far the close is below that high, in %
    needed_pct: float = 8.0
    deep: bool = False  # R1(c)
    first_day: bool = False  # R1(d)
    sma_trend: float | None = None
    above_trend: bool = False  # R1(a)
    market_ok: bool = False  # R1(b)
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "signal": self.signal,
            "close": _r(self.close),
            "high": _r(self.high),
            "below_high_pct": _r(self.below_high_pct),
            "needed_pct": self.needed_pct,
            "deep": self.deep,
            "first_day": self.first_day,
            "sma200": _r(self.sma_trend),
            "above_sma200": self.above_trend,
            "nifty_above_sma200": self.market_ok,
            "why": self.why,
        }


def entry_reading(
    close: np.ndarray,
    ok: np.ndarray,
    t: int,
    p: PullbackParams,
    sma_trend: np.ndarray | None = None,
) -> EntryReading:
    """R1 (a) to (d) on the close of session t. `ok` is R1(b) per session (`nifty_ok`)."""
    c = np.asarray(close, dtype=float)
    needed = p.depth * 100.0
    if t < 0 or t >= len(c):
        raise IndexError(f"pullback: session {t} outside the {len(c)} bars")
    if t < p.min_bars - 1:
        return EntryReading(
            t, False, False, float(c[t]), needed_pct=needed,
            why=f"{t + 1} daily bars, {p.min_bars} needed",
        )  # fmt: skip
    s = sma_trend if sma_trend is not None else sma(c, p.trend_sma)
    deep, hi = _deep(c, t, p)
    deep_before, _ = _deep(c, t - 1, p)
    above = bool(c[t] > s[t])
    market = bool(ok[t])
    first = deep and not deep_before
    signal = first and above and market
    if signal:
        why = "all tests hold"
    elif not above:
        why = "close not above SMA200"
    elif not market:
        why = "NIFTY not above its SMA200"
    elif not deep:
        why = f"less than {needed:g}% below the {p.high_lookback}-session closing high"
    else:
        why = "already that deep on the session before"
    return EntryReading(
        t, True, signal, float(c[t]), hi, (1.0 - float(c[t]) / hi) * 100.0, needed, deep,
        first, float(s[t]), above, market, why,
    )  # fmt: skip


@dataclass(frozen=True)
class ExitReading:
    """What R3 sees on the close of session u for a trade that entered on session e."""

    u: int
    sessions: int  # the entry session counts as 1
    hit: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    sma_exit: float | None = None  # a close above this exits
    stop_level: float | None = None  # a close at or below this exits

    @property
    def reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


def exit_reading(
    close: np.ndarray,
    entry_idx: int,
    fill: float,
    u: int,
    p: PullbackParams,
    sma_exit: np.ndarray | None = None,
) -> ExitReading:
    """R3 on the close of session u >= entry_idx, for a trade filled at `fill`."""
    c = np.asarray(close, dtype=float)
    if u < entry_idx:
        raise ValueError(f"pullback: exit session {u} before the entry session {entry_idx}")
    s = sma_exit if sma_exit is not None else sma(c, p.exit_sma)
    sessions = u - entry_idx + 1
    stop_level = float(fill) * (1.0 - p.stop)
    level = float(s[u]) if np.isfinite(s[u]) else None
    reasons = []
    if level is not None and c[u] > level:
        reasons.append(EXIT_SMA)
    if sessions >= p.max_sessions:
        reasons.append(EXIT_TIME)
    if c[u] <= stop_level:
        reasons.append(EXIT_STOP)
    return ExitReading(u, sessions, bool(reasons), tuple(reasons), level, stop_level)


@dataclass(frozen=True)
class RuleTrade:
    """One trade of `simulate`, as bar indices: signal t, entry e = t + 1, exit bar x (the
    open it sells at, or the last close when `at_close`), and the exit signal's reasons."""

    signal: int
    entry: int
    exit: int
    at_close: bool
    reasons: tuple[str, ...] = ()


def simulate(
    open_: np.ndarray, close: np.ndarray, ok: np.ndarray, p: PullbackParams
) -> list[RuleTrade]:
    """Every trade of the rule on one stock's daily bars, as the reference loop takes them:
    a signal on the last bar has no entry bar and is not a trade; a trade whose exit signal
    comes on the last bar, or never, ends at the last close (`at_close`)."""
    o = np.asarray(open_, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)
    s_trend, s_exit = sma(c, p.trend_sma), sma(c, p.exit_sma)
    out: list[RuleTrade] = []
    t = p.min_bars - 1
    while t < n - 1:
        if not entry_reading(c, ok, t, p, s_trend).signal:
            t += 1
            continue
        e = t + 1
        fill = entry_fill(o[e], p)
        x, at_close = n - 1, True
        reasons: tuple[str, ...] = ()
        for u in range(e, n):
            r = exit_reading(c, e, fill, u, p, s_exit)
            if r.hit:
                x, at_close, reasons = (
                    (u + 1, False, r.reasons) if u + 1 < n else (u, True, r.reasons)
                )
                break
        out.append(RuleTrade(t, e, x, at_close, reasons))
        t = x  # R4: the sale day's close is the first that may enter again
    return out


def net_pct(buy: float, sell: float, model: CostModel) -> float:
    """Net result in % of the buy price for one share, after both legs' charges and before
    the depository charge (the reference's measure)."""
    charges = model.leg(buy, 1, "buy").total + model.leg(sell, 1, "sell").total
    return (sell - buy - charges) / buy * 100.0


def _r(v: float | None) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), 4)
