"""Strategy registry: add a module + one line here to plug in a new strategy."""

from __future__ import annotations

from .base import Strategy
from .benchmark import RandomEntry
from .donchian_breakout import DonchianBreakout
from .ema_cross import EMACross
from .gap_fade import GapFade
from .intraday_momentum import IntradayMomentum
from .mean_reversion import MeanReversion
from .orb import OpeningRangeBreakout
from .squeeze import SqueezeBreakout
from .supertrend_adx import SupertrendADX
from .supertrend_vol import SupertrendVol
from .vwap_pullback import VWAPPullback

# Order = how they appear in the Lab's dropdown: the operator's theses first,
# then the established setups, then the research-backed anomalies.
REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (
        EMACross, SupertrendVol,                                   # operator theses
        SupertrendADX, OpeningRangeBreakout, VWAPPullback,         # classic intraday setups
        SqueezeBreakout, DonchianBreakout, MeanReversion,          # volatility / trend / reversion
        IntradayMomentum, GapFade,                                 # documented anomalies
    )
}

# Which instrument categories each strategy is meant for. The Lab still lets
# you run anything on anything; this only drives the "suggested" badge.
SUITABLE: dict[str, tuple[str, ...]] = {
    "ema_cross": ("OPTIONS", "FUTURES", "STOCK", "CURRENCY"),
    "supertrend_vol": ("OPTIONS", "FUTURES", "STOCK", "CURRENCY"),
    "supertrend_adx": ("FUTURES", "STOCK", "OPTIONS"),
    "orb": ("FUTURES", "OPTIONS", "STOCK"),
    "vwap_pullback": ("STOCK", "FUTURES"),
    "squeeze": ("STOCK", "FUTURES", "CURRENCY"),
    "donchian_breakout": ("FUTURES", "CURRENCY", "STOCK"),
    "mean_reversion": ("STOCK", "FUTURES"),
    "intraday_momentum": ("FUTURES", "OPTIONS"),
    "gap_fade": ("STOCK",),
}

# Indicators each strategy draws on (shown in the Lab next to the tunable parameters).
INDICATORS: dict[str, tuple[str, ...]] = {
    "ema_cross": ("EMA fast/slow", "ADX(14)", "RSI(14)", "ATR(14)", "relative volume"),
    "supertrend_vol": ("Supertrend", "RSI(14)", "ADX(14)", "relative volume", "ATR(14)"),
    "supertrend_adx": ("Supertrend", "ADX/DI(14)", "MACD", "session VWAP", "ATR(14)", "relative volume"),
    "orb": ("opening range", "session VWAP", "daily ATR", "relative volume", "RSI(14)"),
    "vwap_pullback": ("session VWAP", "EMA20/50", "RSI(7)", "ADX(14)", "ATR(14)", "relative volume"),
    "squeeze": ("Bollinger(20)", "Keltner", "Donchian", "linear-regression momentum", "relative volume"),
    "donchian_breakout": ("Donchian N/M", "ADX(14)", "ATR(14)"),
    "mean_reversion": ("RSI(2)", "EMA20/50", "ATR(14)"),
    "intraday_momentum": ("opening-window return", "ATR(14)"),
    "gap_fade": ("opening gap vs NIFTY gap", "daily ATR", "session VWAP"),
}


# research-only controls (never offered for live alerts)
BENCHMARKS: dict[str, type[Strategy]] = {RandomEntry.name: RandomEntry}


def get_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        cls = REGISTRY.get(name) or BENCHMARKS[name]
    except KeyError as exc:
        raise KeyError(f"unknown strategy '{name}'; available: {', '.join(REGISTRY)}") from exc
    return cls(params or {})


def list_strategies() -> list[dict]:
    return [{"name": c.name, "label": c.label, "description": c.description, "regimes": list(c.regimes),
             "suitable_for": list(SUITABLE.get(c.name, ())), "indicators": list(INDICATORS.get(c.name, ())),
             "params": c.param_docs(), "space": {k: list(v) for k, v in c.param_space().items()}}
            for c in REGISTRY.values()]
