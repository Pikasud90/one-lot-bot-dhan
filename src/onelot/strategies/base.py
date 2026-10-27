"""Plug-and-play strategy framework.

A strategy turns bars into *closed-bar* entry signals (decide at bar close t, act at bar t+1 open)
with an initial stop and a 0-100 strength score built from four pillars: price, momentum,
volatility and volume. Exits (stop, target, ATR trailing, partials, time stop, square-off) are
handled uniformly by the backtest simulator and, live, by the Guardian, so every strategy shares
one exit model whose knobs live in the strategy's parameters.

Parameters are pydantic fields. `json_schema_extra={"opt": [lo, hi, step]}` marks a field as
tunable: the dashboard renders sliders from it and the optimizer searches it.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ..indicators import core as ind
from .regime import NEUTRAL, RANGE, TREND, VOLATILE, classify_regime, market_bias


def opt(lo: float, hi: float, step: float, desc: str = "") -> dict:
    return {"opt": [lo, hi, step], "desc": desc}


class StrategyParams(BaseModel):
    """Common parameters shared by every strategy (entry hygiene + exit model)."""

    timeframe: str = Field("5m", description="Signal timeframe")
    allow_long: bool = Field(True, description="Generate long (buy) signals")
    allow_short: bool = Field(True, description="Generate intraday short (sell) signals")
    entry_start: str = Field("09:30", description="No new entries before (HH:MM)")
    entry_end: str = Field("14:45", description="No new entries after (HH:MM)")
    exit_time: str = Field("15:15", description="Intraday exit time (HH:MM)")
    # risk / exits
    stop_atr_min: float = Field(0.4, description="Minimum initial risk in ATR(14) units (stop widened to this)",
                                json_schema_extra=opt(0.2, 1.0, 0.1))
    stop_atr_max: float = Field(2.5, description="Skip setups whose initial risk exceeds this many ATR(14)",
                                json_schema_extra=opt(1.0, 4.0, 0.5))
    min_risk_pct: float = Field(0.35, description="Friction floor: initial risk at least this % of price "
                                "(keeps slippage+charges a small fraction of R)", json_schema_extra=opt(0.0, 1.0, 0.1))
    stop_datr_min: float = Field(0.0, description="Initial risk at least this fraction of the prior day's ATR (0 = off)",
                                 json_schema_extra=opt(0.0, 0.5, 0.05))
    max_risk_pct: float = Field(2.0, description="Skip setups whose initial risk exceeds this % of price")
    target_r: float = Field(2.0, description="Profit target in R multiples (0 = no fixed target)",
                            json_schema_extra=opt(0.0, 4.0, 0.5))
    trail_atr_mult: float = Field(1.5, description="Trailing stop distance k in k x ATR(n) (0 = off)",
                                  json_schema_extra=opt(0.0, 3.0, 0.5))
    trail_atr_period: int = Field(5, description="ATR period n for the trailing stop",
                                  json_schema_extra=opt(3, 14, 1))
    trail_activate_r: float = Field(1.0, description="Start trailing once the trade is this many R in profit",
                                    json_schema_extra=opt(0.0, 2.0, 0.5))
    partial_r: float = Field(0.0, description="Book a partial at this R (0 = off)",
                             json_schema_extra=opt(0.0, 2.0, 0.5))
    partial_frac: float = Field(0.5, description="Fraction booked at the partial target")
    breakeven_after_partial: bool = Field(True, description="Move stop to entry after the partial")
    max_bars: int = Field(0, description="Time stop in bars (0 = off)", json_schema_extra=opt(0, 36, 6))
    # signal hygiene
    min_strength: float = Field(0.0, description="Minimum strength score (0-100) to act on a signal",
                                json_schema_extra=opt(0, 80, 10))
    market_filter: bool = Field(True, description="Block longs when NIFTY is strongly bearish (and vice versa)")
    regime_filter: bool = Field(True, description="Only trade in the regimes this strategy is designed for")
    cooldown_bars: int = Field(6, description="Bars to wait after an exit before re-entering the same symbol",
                               json_schema_extra=opt(0, 24, 3))
    max_trades_per_day: int = Field(2, description="Per symbol per day", json_schema_extra=opt(1, 4, 1))


@dataclass
class Context:
    symbol: str = ""
    daily: pd.DataFrame | None = None     # daily bars of the symbol (for prior-day levels / daily ATR)
    market: pd.DataFrame | None = None    # NIFTY bars at the same timeframe (for market bias)
    extras: dict[str, Any] = field(default_factory=dict)


SIGNAL_COLUMNS = ["long_entry", "short_entry", "long_stop", "short_stop", "long_score", "short_score",
                  "long_exit", "short_exit"]


class Strategy(ABC):
    name: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    regimes: ClassVar[tuple[str, ...]] = (TREND, NEUTRAL, RANGE, VOLATILE)
    Params: ClassVar[type[StrategyParams]] = StrategyParams
    # pillar weights for the strength score
    weights: ClassVar[dict[str, float]] = {"price": 0.3, "momentum": 0.3, "volatility": 0.2, "volume": 0.2}

    def __init__(self, params: dict | StrategyParams | None = None) -> None:
        if isinstance(params, StrategyParams):
            self.p = params
        else:
            self.p = self.Params(**(params or {}))

    # ------------------------------------------------------------------ introspection
    @classmethod
    def param_space(cls) -> dict[str, tuple[float, float, float]]:
        out = {}
        for name, f in cls.Params.model_fields.items():
            extra = f.json_schema_extra or {}
            if isinstance(extra, dict) and "opt" in extra:
                out[name] = tuple(extra["opt"])
        return out

    @classmethod
    def param_docs(cls) -> dict[str, dict]:
        out = {}
        for name, f in cls.Params.model_fields.items():
            extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
            out[name] = {"default": f.default, "description": f.description or extra.get("desc", ""),
                         "opt": extra.get("opt"), "type": type(f.default).__name__}
        return out

    # ------------------------------------------------------------------ features
    def features(self, df: pd.DataFrame, ctx: Context | None = None) -> pd.DataFrame:
        """Common features every strategy can use (cached per data window when ctx provides a cache)."""
        ctx = ctx or Context()
        cache, key = ctx.extras.get("fcache"), ctx.extras.get("fkey")
        if cache is not None and key is not None:
            full_key = (*key, int(self.p.trail_atr_period))
            hit = cache.get(full_key)
            if hit is not None:
                return hit.copy()
            f = self._compute_features(df, ctx)
            cache[full_key] = f
            return f.copy()
        return self._compute_features(df, ctx)

    def _compute_features(self, df: pd.DataFrame, ctx: Context) -> pd.DataFrame:
        f = df.copy()
        h, l, c = f["high"], f["low"], f["close"]
        f["atr"] = ind.atr(h, l, c, 14)
        f["atr_trail"] = ind.atr(h, l, c, int(self.p.trail_atr_period))
        f["atr_pct"] = ind.percentile_rank(f["atr"] / c, 100)
        f["vwap"] = ind.session_vwap(f)
        f["vwap"] = f["vwap"].fillna(ind.ema(c, 20))  # indices have no volume -> EMA proxy
        f["rsi"] = ind.rsi(c, 14)
        a = ind.adx(h, l, c, 14)
        f["adx"], f["plus_di"], f["minus_di"] = a["adx"], a["plus_di"], a["minus_di"]
        f["chop"] = ind.choppiness(h, l, c, 14)
        f["ema20"], f["ema50"] = ind.ema(c, 20), ind.ema(c, 50)
        # Indices and spot FX have no traded volume. Relative volume is left
        # NaN there so every volume filter is explicitly NEUTRAL (strategies
        # test `(rvol >= x) | rvol.isna()`), never silently always-false.
        if float(f["volume"].fillna(0).sum()) > 0:
            f["rvol"] = ind.tod_rvol(f["volume"], days=10).fillna(ind.rvol(f["volume"], 20))
        else:
            f["rvol"] = np.nan
        f["regime"] = classify_regime(f["adx"], f["chop"], f["atr_pct"])
        f["bar_no"] = ind.bar_of_day(f.index).to_numpy()
        f["new_day"] = ind.session_open_time_mask(f.index)
        f["mkt_bias"] = market_bias(ctx.market, f.index) if ctx.market is not None else 0.0
        dctx = ind.daily_context(f, ctx.daily)
        for col in dctx.columns:
            f[col] = dctx[col].to_numpy()
        f["mkt_gap"] = market_gap(ctx, f.index)
        return f

    # ------------------------------------------------------------------ signals
    @abstractmethod
    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        """Return SIGNAL_COLUMNS (stops/scores may be NaN when no entry). Pillar sub-scores in [0,1]
        may be returned as columns `pl_<pillar>_<side>` and are combined into the strength."""

    def generate(self, df: pd.DataFrame, ctx: Context | None = None) -> pd.DataFrame:
        f = self.features(df, ctx)
        raw = self.raw_signals(f)
        out = f.join(raw[[c for c in raw.columns if c not in f.columns]])
        for side in ("long", "short"):
            score = out.get(f"{side}_score")
            if score is None or score.isna().all():
                out[f"{side}_score"] = self._strength(out, side)
        out = self._apply_filters(out)
        return out

    def _strength(self, f: pd.DataFrame, side: str) -> pd.Series:
        total = pd.Series(0.0, index=f.index)
        wsum = 0.0
        for pillar, w in self.weights.items():
            col = f"pl_{pillar}_{side}"
            if col in f.columns:
                total = total + w * f[col].clip(0, 1).fillna(0)
                wsum += w
        return (100 * total / wsum).round(1) if wsum else pd.Series(50.0, index=f.index)

    def _apply_filters(self, f: pd.DataFrame) -> pd.DataFrame:
        p = self.p
        t = decision_times(f.index, p.timeframe)
        start, end = _hhmm(p.entry_start), _hhmm(p.entry_end)
        in_window = (t >= start) & (t <= end)
        f["blk_window"] = ~in_window
        f["blk_regime"] = (~f["regime"].isin(self.regimes)) if p.regime_filter else False
        f["blk_mkt_long"] = (f["mkt_bias"] <= -2) if p.market_filter else False
        f["blk_mkt_short"] = (f["mkt_bias"] >= 2) if p.market_filter else False
        f["blk_strength_long"] = f["long_score"] < p.min_strength
        f["blk_strength_short"] = f["short_score"] < p.min_strength
        f["setup_long"] = f["long_entry"].fillna(False).astype(bool)
        f["setup_short"] = f["short_entry"].fillna(False).astype(bool)
        f["long_entry"] = (f["setup_long"] & in_window & ~f["blk_regime"] & ~f["blk_mkt_long"]
                           & ~f["blk_strength_long"] & p.allow_long)
        f["short_entry"] = (f["setup_short"] & in_window & ~f["blk_regime"] & ~f["blk_mkt_short"]
                            & ~f["blk_strength_short"] & p.allow_short)
        for side in ("long", "short"):
            if f"{side}_exit" not in f.columns:
                f[f"{side}_exit"] = False
            f[f"{side}_exit"] = f[f"{side}_exit"].fillna(False).astype(bool)
        return f

    # ------------------------------------------------------------------ explanations (live alerts)
    def explain(self, f: pd.DataFrame, i: int, side: str) -> dict:
        """Human-readable reasons/blockers for bar i. Strategies extend `_reasons`."""
        row = f.iloc[i]
        reasons, blockers = self._reasons(f, i, side)
        sign = 1 if side == "long" else -1
        if row.get("blk_window", False):
            blockers.append("Outside the entry time window")
        if row.get("blk_regime", False):
            blockers.append(f"Regime '{row['regime']}' not suited to {self.label}")
        if row.get(f"blk_mkt_{side}", False):
            blockers.append("NIFTY is strongly against this direction")
        if row.get(f"blk_strength_{side}", False):
            blockers.append(f"Strength {row[f'{side}_score']:.0f} below minimum {self.p.min_strength:.0f}")
        vw = row.get("vwap")
        if vw and not np.isnan(vw):
            rel = "above" if row["close"] > vw else "below"
            reasons.append(f"Price {rel} VWAP {vw:.2f} ({(row['close'] / vw - 1) * 100:+.2f}%)")
        if not np.isnan(row.get("rvol", np.nan)):
            reasons.append(f"Relative volume {row['rvol']:.1f}x")
        reasons.append(f"Regime: {row['regime']}; ADX {row['adx']:.0f}")
        mb = row.get("mkt_bias", 0)
        reasons.append(f"NIFTY bias {mb:+.0f} ({'supportive' if mb * sign > 0 else 'neutral' if mb == 0 else 'against'})")
        return {"reasons": reasons, "blockers": blockers, "implies": self.implies(side)}

    def _reasons(self, f: pd.DataFrame, i: int, side: str) -> tuple[list[str], list[str]]:
        return [], []

    def implies(self, side: str) -> str:
        return ""


def market_gap(ctx: Context, index: pd.DatetimeIndex) -> pd.Series:
    """NIFTY's opening gap (%) for each bar's session: today's first intraday open vs the prior close
    (prior close from NIFTY daily bars when available, else the last intraday close of the prior session)."""
    m = ctx.market
    if m is None or m.empty:
        return pd.Series(np.nan, index=index)
    mkey = m.index.normalize()
    day_open = m["open"].groupby(mkey).first()
    md = ctx.extras.get("market_daily")
    if md is not None and not md.empty:
        dclose = md["close"].copy()
        dclose.index = pd.DatetimeIndex(dclose.index).normalize()
        prev_close = dclose.shift(1).reindex(day_open.index)
        fallback = m["close"].groupby(mkey).last().shift(1)
        prev_close = prev_close.fillna(fallback)
    else:
        prev_close = m["close"].groupby(mkey).last().shift(1)
    gap = (day_open / prev_close - 1) * 100
    return pd.Series(gap.reindex(index.normalize()).to_numpy(), index=index)


def decision_times(index: pd.DatetimeIndex, timeframe: str) -> np.ndarray:
    """Signals are decided at bar CLOSE = bar start + timeframe."""
    from ..data.resample import minutes_of

    mins = minutes_of(timeframe) if timeframe != "1d" else 0
    return (index + pd.Timedelta(minutes=mins)).time


def _hhmm(v: str) -> dt.time:
    h, m = v.split(":")
    return dt.time(int(h), int(m))


def first_in_day(mask: pd.Series) -> pd.Series:
    """Keep only the first True of each session."""
    key = ind.session_key(mask.index)
    csum = mask.astype(int).groupby(key).cumsum()
    return mask & (csum == 1)


def first_since(mask: pd.Series, reset: pd.Series) -> pd.Series:
    """Keep only the first True after each `reset` event (e.g. a Supertrend flip)."""
    seg = reset.astype(int).cumsum()
    csum = mask.astype(int).groupby(seg.to_numpy()).cumsum()
    return mask & (csum == 1)


def clip01(x: pd.Series | np.ndarray) -> pd.Series:
    return pd.Series(np.clip(x, 0, 1), index=getattr(x, "index", None))


def vol_pillar(atr_pct: pd.Series) -> pd.Series:
    """Healthy volatility (20th-85th percentile) scores 1; dead or extreme volatility scores lower."""
    x = atr_pct.fillna(50)
    score = np.where(x < 20, 0.3 + 0.7 * x / 20, np.where(x > 85, np.maximum(0.3, 1 - (x - 85) / 20), 1.0))
    return pd.Series(score, index=atr_pct.index)


def volume_pillar(rvol: pd.Series) -> pd.Series:
    return ((rvol.fillna(1.0) - 0.7) / 1.3).clip(0, 1)
