"""Control strategy for research: random entries with the same exit model, costs and filters.

A real strategy must beat this out-of-sample; otherwise its 'edge' is indistinguishable from noise.
Never used for live alerts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from .base import Strategy, StrategyParams


class RandomParams(StrategyParams):
    entry_prob: float = Field(0.02, description="Probability of a random entry on any eligible bar")
    stop_atr: float = Field(1.0, description="Stop distance in ATR(14)")
    seed: int = Field(42, description="Random seed")
    market_filter: bool = Field(False, description="Block trades against NIFTY bias")
    regime_filter: bool = Field(False, description="Regime filter")


class RandomEntry(Strategy):
    name = "benchmark_random"
    label = "Random-entry benchmark"
    description = "Control: random direction entries with the shared exit model."
    Params = RandomParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: RandomParams = self.p  # type: ignore[assignment]
        # seed from the symbol's first timestamp so results are reproducible per symbol
        rng = np.random.default_rng(p.seed + int(f.index[0].value % 100_000))
        draw = rng.random(len(f))
        side = rng.random(len(f)) < 0.5
        hit = draw < p.entry_prob
        out = pd.DataFrame(index=f.index)
        out["long_entry"] = pd.Series(hit & side, index=f.index)
        out["short_entry"] = pd.Series(hit & ~side, index=f.index)
        out["long_stop"] = f["close"] - p.stop_atr * f["atr"]
        out["short_stop"] = f["close"] + p.stop_atr * f["atr"]
        out["long_score"] = 50.0
        out["short_score"] = 50.0
        return out
