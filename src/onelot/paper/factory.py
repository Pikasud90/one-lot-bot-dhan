"""Production wiring for the paper book (US-001 AC8): build a PaperBook from the app config.

The book itself stays hermetic (ADR-001 amendment 10): everything it needs is resolved here
once and injected. costs.yaml is parsed once per build and one CostModel is cached per cost
product; premium_at is Black-76 with the option frozen at the alert (`onelot.paper.pricing`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import Engine

from ..clock import now_ist
from ..config import AppConfig, CostConfig
from ..costs import CostModel
from ..data.universe import load_specs
from .book import PaperBook
from .pricing import black76_premium


def build_paper_book(
    app_cfg: AppConfig, engine: Engine, clock: Callable[[], dt.datetime] = now_ist
) -> PaperBook:
    """The PaperBook the Engine attaches to the Desk.

    Raises when the cost table or the instrument specs cannot be read: the Engine logs that
    and runs the desk without a book rather than with a half-configured one.
    """
    _, instruments = load_specs()
    cost_cfg = CostConfig.load(app_cfg.project_root / "config" / "costs.yaml")  # parsed once
    models: dict[str, CostModel] = {}

    def cost_for(instrument_key: str) -> CostModel:
        spec = instruments.get(instrument_key)
        if spec is None:
            raise KeyError(f"paper: no instrument spec for {instrument_key!r}")
        if spec.product not in models:
            models[spec.product] = CostModel(cost_cfg.product(spec.product))
        return models[spec.product]

    rate = app_cfg.backtest.risk_free_rate

    def premium_at(option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float | None:
        return black76_premium(option, underlying, ts, rate)

    return PaperBook(
        engine, app_cfg.paper, app_cfg.backtest, instruments, cost_for, premium_at, clock=clock
    )
