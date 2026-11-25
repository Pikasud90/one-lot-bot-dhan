"""The strategy's lines on the Simulator's chart (`plots`, SIM-API-2.md; US-095 point 5).

Each value is taken from the strategy's own frame (the columns its `generate` wrote), or, where
the frame does not hold the line itself (the CPR's pivot, BC and TC), computed from the
frame's own inputs with the strategy's own formula. A value belongs to a strategy-timeframe
candle and is sent only once that candle has closed at the cursor. A value that a later bar
could still change is not a value yet: the opening range's high and low are left out until
the range is complete (`tests/sim/test_plots.py` proves every line by truncation).

A strategy without a declaration here sends no lines.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..strategies.registry import REGISTRY


@dataclass(frozen=True)
class Plot:
    id: str
    label: str  # may name a parameter: "EMA {fast}"
    #: (the strategy's frame, its parameters, its timeframe) -> one value per candle (NaN: none)
    values: Callable[[pd.DataFrame, Any, str], np.ndarray]
    pane: str = "price"


def _col(name: str) -> Callable[[pd.DataFrame, Any, str], np.ndarray]:
    def get(f: pd.DataFrame, p: Any, tf: str) -> np.ndarray:
        if name not in f.columns:
            return np.full(len(f), np.nan)
        return f[name].to_numpy(dtype=float)

    return get


def _own_supertrend(f: pd.DataFrame, p: Any, tf: str) -> np.ndarray:
    """A multi-timeframe strategy's Supertrend line of its own timeframe."""
    return _col(f"mtf_line_{tf}")(f, p, tf)


def _opening_range(side: str) -> Callable[[pd.DataFrame, Any, str], np.ndarray]:
    """The opening range's high or low, from the first candle after the range on (`orb.py`
    writes the day's final range on every candle of the day, the range's own candles too)."""

    def get(f: pd.DataFrame, p: Any, tf: str) -> np.ndarray:
        vals = _col(f"or_{side}")(f, p, tf)
        if "bar_no" not in f.columns or len(f) < 2:
            return np.full(len(f), np.nan)
        idx = pd.DatetimeIndex(f.index)
        tf_min = max(1, int(round((idx[1] - idx[0]).total_seconds() / 60)))
        n_or = max(1, int(p.or_minutes) // tf_min)
        return np.where(f["bar_no"].to_numpy() >= n_or, vals, np.nan)

    return get


def _cpr(which: str) -> Callable[[pd.DataFrame, Any, str], np.ndarray]:
    """The prior session's pivot, bottom (BC) and top (TC) central levels, as `cpr_breakout.py`
    computes them: P = (H + L + C) / 3, BC = (H + L) / 2, TC = 2P - BC."""

    def get(f: pd.DataFrame, p: Any, tf: str) -> np.ndarray:
        if not {"pd_high", "pd_low", "pd_close"} <= set(f.columns):
            return np.full(len(f), np.nan)
        h, low, pc = (f[k].to_numpy(dtype=float) for k in ("pd_high", "pd_low", "pd_close"))
        pivot = (h + low + pc) / 3.0
        bc = (h + low) / 2.0
        return {"pivot": pivot, "bc": bc, "tc": 2.0 * pivot - bc}[which]

    return get


PLOTS: dict[str, tuple[Plot, ...]] = {
    "microtrend": (Plot("st", "Supertrend 3m", _own_supertrend),),
    "supertrend_vol": (Plot("st", "Supertrend", _col("st_line")),),
    "supertrend_adx": (Plot("st", "Supertrend", _col("st_line")),),
    "supertrend_mtf": (Plot("st", "Supertrend", _own_supertrend),),
    "ema_cross": (
        Plot("ema_fast", "EMA {fast}", _col("ema_fast")),
        Plot("ema_slow", "EMA {slow}", _col("ema_slow")),
    ),
    "ema_pullback": (Plot("ema", "EMA {ema_len}", _col("hg_ema")),),
    "vwap_pullback": (Plot("vwap", "VWAP", _col("vwap")),),
    "orb": (
        Plot("or_high", "Opening range high", _opening_range("high")),
        Plot("or_low", "Opening range low", _opening_range("low")),
    ),
    "donchian_breakout": (
        Plot("dc_upper", "Channel high", _col("dc_upper")),
        Plot("dc_lower", "Channel low", _col("dc_lower")),
    ),
    "cpr_breakout": (
        Plot("pivot", "Pivot", _cpr("pivot")),
        Plot("bc", "BC", _cpr("bc")),
        Plot("tc", "TC", _cpr("tc")),
    ),
}


def _label(plot: Plot, params: Any) -> str:
    try:
        return plot.label.format(**params.model_dump())
    except (KeyError, AttributeError, IndexError, ValueError):
        return plot.label


def plot_specs(name: str, params: Any = None) -> list[dict[str, str]]:
    """The contract's `plots` of a strategy: [{"id", "label", "pane"}] (default parameters
    when `params` is None)."""
    if params is None and name in REGISTRY:
        params = REGISTRY[name].Params.model_validate({})
    return [{"id": p.id, "label": _label(p, params), "pane": p.pane} for p in PLOTS.get(name, ())]


def plot_values(name: str, f: pd.DataFrame | None, params: Any, tf: str) -> dict[str, np.ndarray]:
    """{plot id: one float per row of the strategy's frame `f` (NaN where none)}."""
    if f is None:
        return {}
    out: dict[str, np.ndarray] = {}
    for p in PLOTS.get(name, ()):
        vals = np.asarray(p.values(f, params, tf), dtype=float)
        out[p.id] = vals if len(vals) == len(f) else np.full(len(f), np.nan)
    return out
