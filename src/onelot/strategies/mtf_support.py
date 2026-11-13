"""The multi-timeframe Supertrend ladder as a strategy reads it.

A strategy decides at the close of each of its own bars. For each of those moments this
module says what the last CLOSED bar of every ladder timeframe said (direction and line) and
what a filter needs from the bars of one of them, with no look-ahead (`indicators/mtf.py`).

Where the ladder comes from:

* `ctx.extras["minute_bars"]`: the instrument's finest bars. The Lab gives the stored
  one-minute history, the desk its live bars; `ctx.extras["minute_resolution"]` names their
  resolution ("1m" unless said otherwise).
* `ctx.daily`: the daily series of the "1d" rung. Without it the days are resampled from the
  fine bars, which is only warm after about 70 sessions.
* With no fine bars the strategy's own bars are the base.

A rung the base bars cannot give exactly (one finer than the base, or one the base does not
divide: a 5-minute bar from 2-minute bars) is unavailable: its direction is 0, so a gate that
needs it stays shut rather than guessing.

Building a ladder costs a Supertrend pass over every bar, so it is kept in
`ctx.extras["mtf_cache"]` (a `LadderCache`) when the caller gives one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..data.resample import can_derive, normalize_resolution, resample_bars
from ..indicators import core as ind
from ..indicators import mtf
from .base import Context

#: "bars since the flip" before the first flip of a timeframe
NO_FLIP = 10**6
#: which timeframes must agree, by name
GATES: tuple[str, ...] = ("all", "slower", "faster", "daily", "trend")
#: period of the ATR, ADX and RSI a filter reads from a rung
FILTER_PERIOD = 14

Frames = dict[str, pd.DataFrame | None]


class LadderCache(dict):
    """Ladders by data and setting; the least recently used goes once `maxsize` is reached."""

    def __init__(self, maxsize: int = 4) -> None:
        super().__init__()
        self.maxsize = max(1, int(maxsize))
        #: the resampled bars of the data last seen (they are the same for every setting)
        self.frames: tuple[tuple, Frames] | None = None


@dataclass(frozen=True)
class RungInputs:
    """Filter inputs of one timeframe, one value per bar of it."""

    atr: np.ndarray
    adx: np.ndarray
    rsi: np.ndarray
    rvol: np.ndarray  #: NaN without traded volume: a volume filter is then neutral


class Ladder:
    """The Supertrend of every ladder timeframe, all from the same base bars."""

    def __init__(
        self, base: str, frames: Frames, period: int, mult: float, has_volume: bool = True
    ) -> None:
        self.base = base
        self.period, self.mult = int(period), float(mult)
        self.has_volume = has_volume
        self._frames = frames
        self.trends: dict[str, mtf.TimeframeTrend] = {
            tf: mtf.timeframe_trend(
                bars if bars is not None else pd.DataFrame(), tf, self.period, self.mult
            )
            for tf, bars in frames.items()
        }
        self._runs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._inputs: dict[str, RungInputs] = {}

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Timeframes the base bars cannot give."""
        return tuple(tf for tf, bars in self._frames.items() if bars is None or bars.empty)

    def position(self, timeframe: str, times_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Row of the last bar of `timeframe` closed at or before each time, and whether one is."""
        closes = self.trends[timeframe].close_ns
        pos = np.searchsorted(closes, np.asarray(times_ns, dtype=np.int64), side="right") - 1
        known = pos >= 0
        return np.where(known, pos, 0), known

    def runs(self, timeframe: str) -> tuple[np.ndarray, np.ndarray]:
        """Per bar: the number of the trend in force, and the bars since the flip that began it
        (0 on the flip bar; `NO_FLIP` before the first flip)."""
        if timeframe not in self._runs:
            d = self.trends[timeframe].direction.astype(np.int8)
            prev = np.r_[np.int8(0), d[:-1]] if len(d) else d
            flip = (d != prev) & (prev != 0) & (d != 0)
            k = np.arange(len(d))
            last = np.maximum.accumulate(np.where(flip, k, -1)) if len(d) else k
            self._runs[timeframe] = (np.cumsum(flip), np.where(last >= 0, k - last, NO_FLIP))
        return self._runs[timeframe]

    def inputs(self, timeframe: str) -> RungInputs:
        """ATR, ADX, RSI and relative volume of the bars of `timeframe`."""
        if timeframe not in self._inputs:
            bars = self._frames.get(timeframe)
            if bars is None or bars.empty:
                none = np.zeros(0)
                self._inputs[timeframe] = RungInputs(none, none, none, none)
            else:
                hh, ll, cc = bars["high"], bars["low"], bars["close"]
                vol = bars["volume"] if "volume" in bars.columns else None
                if self.has_volume and vol is not None and float(vol.fillna(0).sum()) > 0:
                    rv = ind.rvol(vol, 20)
                    if timeframe != "1d":
                        rv = ind.tod_rvol(vol, days=10).fillna(rv)
                    rvol = rv.to_numpy(float)
                else:
                    rvol = np.full(len(bars), np.nan)
                self._inputs[timeframe] = RungInputs(
                    ind.atr(hh, ll, cc, FILTER_PERIOD).to_numpy(float),
                    ind.adx(hh, ll, cc, FILTER_PERIOD)["adx"].to_numpy(float),
                    ind.rsi(cc, FILTER_PERIOD).to_numpy(float),
                    rvol,
                )
        return self._inputs[timeframe]


def gate_timeframes(gate: str, trend_tf: str) -> tuple[str, ...]:
    """The timeframes that must agree for `gate`, given the entry timeframe."""
    tfs = mtf.LADDER_TIMEFRAMES
    tf = normalize_resolution(trend_tf)
    if tf not in tfs:
        raise ValueError(f"trend timeframe {trend_tf!r} is not on the ladder {', '.join(tfs)}")
    e = tfs.index(tf)
    if gate == "all":
        return tfs
    if gate == "slower":
        return tfs[e:]
    if gate == "faster":
        return tfs[: e + 1]
    if gate == "daily":
        return (tf,) if tf == "1d" else (tf, "1d")
    if gate == "trend":
        return (tf,)
    raise ValueError(f"gate {gate!r} is not one of {', '.join(GATES)}")


def rung_frames(
    base_bars: pd.DataFrame,
    base: str,
    daily: pd.DataFrame | None,
    timeframes: tuple[str, ...] = mtf.LADDER_TIMEFRAMES,
) -> Frames:
    """The bars of every ladder timeframe from bars of resolution `base` (None: unavailable).
    `timeframes` is the owner's ladder unless a strategy names its own (US-068 microtrend)."""
    base = normalize_resolution(base)
    frames: Frames = {}
    for tf in (normalize_resolution(t) for t in timeframes):
        if tf == "1d":
            given = daily is not None and not daily.empty
            frames[tf] = daily if given else resample_bars(base_bars, "1d")
        elif tf == base:
            frames[tf] = base_bars
        elif base != "1d" and can_derive(base, tf):
            frames[tf] = resample_bars(base_bars, tf)
        else:
            frames[tf] = None
    return frames


def build(
    base_bars: pd.DataFrame,
    base: str,
    daily: pd.DataFrame | None = None,
    period: int = mtf.ST_PERIOD,
    mult: float = mtf.ST_MULT,
    has_volume: bool = True,
    timeframes: tuple[str, ...] = mtf.LADDER_TIMEFRAMES,
) -> Ladder:
    """The ladder of `base_bars`, which are bars of resolution `base`."""
    base = normalize_resolution(base)
    frames = rung_frames(base_bars, base, daily, timeframes)
    return Ladder(base, frames, period, mult, has_volume)


def ladder_for(
    df: pd.DataFrame,
    ctx: Context,
    timeframe: str,
    period: int,
    mult: float,
    timeframes: tuple[str, ...] = mtf.LADDER_TIMEFRAMES,
) -> Ladder:
    """The ladder a strategy on `df` (bars of `timeframe`) reads, from `ctx` or `df` itself."""
    extras = ctx.extras
    fine = extras.get("minute_bars")
    if fine is not None and len(fine):
        base_bars = fine
        base = normalize_resolution(str(extras.get("minute_resolution") or "1m"))
    else:
        base_bars, base = df, normalize_resolution(timeframe)
    daily = ctx.daily if ctx.daily is not None and len(ctx.daily) else None
    has_volume = bool(extras.get("has_volume", True))
    store = extras.get("mtf_cache")
    if store is None:
        return build(base_bars, base, daily, period, mult, has_volume, timeframes)
    data_key = (
        ctx.symbol, base, has_volume, tuple(timeframes), *fingerprint(base_bars),
        *fingerprint(daily),
    )  # fmt: skip
    key = (*data_key, int(period), round(float(mult), 6))
    hit = store.pop(key, None)
    if hit is None:
        kept = getattr(store, "frames", None)
        if kept is not None and kept[0] == data_key:
            frames = kept[1]
        else:
            frames = rung_frames(base_bars, base, daily, timeframes)
            if isinstance(store, LadderCache):
                store.frames = (data_key, frames)
        hit = Ladder(base, frames, period, mult, has_volume)
    store[key] = hit  # the most recently used is last
    limit = int(getattr(store, "maxsize", 4))
    while len(store) > limit:
        store.pop(next(iter(store)))
    return cast(Ladder, hit)


def fingerprint(bars: pd.DataFrame | None) -> tuple[Any, ...]:
    """Enough of a bar frame to tell one data set from another without reading all of it."""
    if bars is None or bars.empty:
        return (0, 0, 0, 0.0)
    index = pd.DatetimeIndex(bars.index)
    return (len(bars), int(index[0].value), int(index[-1].value), float(bars["close"].iloc[-1]))


def decision_ns(index: pd.DatetimeIndex, timeframe: str) -> np.ndarray:
    """When each bar of a strategy on `timeframe` decides: at its close."""
    return mtf.nominal_close_ns(pd.DatetimeIndex(index), timeframe)


def ladder_columns(ladder: Ladder, times_ns: np.ndarray) -> dict[str, np.ndarray]:
    """Per decision time: `mtf_<tf>` (direction), `mtf_line_<tf>`, and how many point each way."""
    dirs, lines = mtf.ladder_at(ladder.trends, times_ns)
    out: dict[str, np.ndarray] = {}
    up = np.zeros(len(times_ns), dtype=np.int64)
    down = np.zeros(len(times_ns), dtype=np.int64)
    for tf in ladder.trends:
        out[f"mtf_{tf}"] = dirs[tf].astype(np.int8)
        out[f"mtf_line_{tf}"] = lines[tf]
        up += dirs[tf] == 1
        down += dirs[tf] == -1
    out["mtf_up"], out["mtf_down"] = up, down
    return out


def trend_columns(ladder: Ladder, timeframe: str, times_ns: np.ndarray) -> dict[str, np.ndarray]:
    """Per decision time, from the last closed bar of `timeframe`: the number of its trend, the
    bars since the flip, and the filter inputs (`mtf_atr`, `mtf_adx`, `mtf_rsi`, `mtf_rvol`)."""
    tf = normalize_resolution(timeframe)
    pos, known = ladder.position(tf, times_ns)
    seg, since = ladder.runs(tf)
    inp = ladder.inputs(tf)
    n = len(times_ns)

    def pick(values: np.ndarray, missing: float) -> np.ndarray:
        if len(values) == 0:
            return np.full(n, missing)
        return np.where(known, values[pos], missing)

    return {
        "mtf_seg": pick(seg, -1).astype(np.int64),
        "mtf_since": pick(since, NO_FLIP).astype(np.int64),
        "mtf_atr": pick(inp.atr, np.nan),
        "mtf_adx": pick(inp.adx, np.nan),
        "mtf_rsi": pick(inp.rsi, np.nan),
        "mtf_rvol": pick(inp.rvol, np.nan),
    }


def trail_columns(ladder: Ladder, timeframe: str, times_ns: np.ndarray) -> dict[str, np.ndarray]:
    """Per decision time, from the last closed bar of the trail timeframe: its direction, its
    Supertrend line and its ATR (`mtf_trail_dir`, `mtf_trail_line`, `mtf_trail_atr`)."""
    tf = normalize_resolution(timeframe)
    direction, line = ladder.trends[tf].at(times_ns)
    pos, known = ladder.position(tf, times_ns)
    atr = ladder.inputs(tf).atr
    trail_atr = np.where(known, atr[pos], np.nan) if len(atr) else np.full(len(times_ns), np.nan)
    return {"mtf_trail_dir": direction, "mtf_trail_line": line, "mtf_trail_atr": trail_atr}


def snapshot(ladder: Ladder, now: pd.Timestamp) -> dict[str, Any]:
    """The ladder as the desk shows it at `now`."""
    rows = mtf.snapshot(ladder.trends, now)
    dirs = [cast(int, r["direction"]) for r in rows]
    aligned = 1 if all(d == 1 for d in dirs) else -1 if all(d == -1 for d in dirs) else 0
    return {
        "period": ladder.period,
        "mult": ladder.mult,
        "base": ladder.base,
        "rows": rows,
        "up": sum(d == 1 for d in dirs),
        "down": sum(d == -1 for d in dirs),
        "aligned": aligned,
        "unavailable": list(ladder.unavailable),
    }
