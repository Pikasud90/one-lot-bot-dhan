"""US-023 AC4: economics._costs_for parses config/costs.yaml once per process, not per call.

Every desk alert prices its charges through `_costs_for`; before US-023 each call re-read and
re-validated the YAML (and re-logged the UNVERIFIED warning). The cost table is now parsed once
per file and one CostModel is kept per cost product.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from onelot.config import CostConfig
from onelot.data.universe import InstrumentSpec
from onelot.desk import economics

STOCK = InstrumentSpec("SBIN", "SBIN", "STOCK", "SBIN", 1, 0.05, "equity_intraday", 0.0)
OPTION = InstrumentSpec(
    "BANKNIFTY_OPT", "BN", "OPTIONS", "BANKNIFTY", 30, 0.05, "index_options", 100.0
)


@pytest.fixture
def loads(monkeypatch) -> Iterator[list[Path]]:
    """Every CostConfig.load call (its path), from a cold cache; the cache is cleared after."""
    seen: list[Path] = []
    real = CostConfig.load.__func__  # type: ignore[attr-defined]

    def counting(cls: Any, path: Any) -> CostConfig:
        seen.append(Path(path))
        return real(cls, path)

    economics._clear_cost_cache()
    monkeypatch.setattr(CostConfig, "load", classmethod(counting))
    yield seen
    economics._clear_cost_cache()


def test_forty_calls_over_two_products_parse_the_table_once(loads):
    models = [economics._costs_for(STOCK if i % 2 == 0 else OPTION) for i in range(40)]
    assert len(loads) == 1 and loads[0].name == "costs.yaml"
    stock, option = models[0], models[1]
    assert stock.cfg.taxes.stt_sell_pct == 0.00025 and stock.cfg.brokerage.mode == "min_of"
    assert option.cfg.taxes.stt_sell_pct == 0.0010 and option.cfg.brokerage.mode == "flat"
    assert all(m is stock for m in models[0::2]) and all(m is option for m in models[1::2])


def test_the_cached_model_prices_like_a_freshly_parsed_one(loads, project_root):
    fresh = CostConfig.load(project_root / "config" / "costs.yaml").product("equity_intraday")
    cached = economics._costs_for(STOCK)
    assert cached.cfg == fresh
    # BUY 1000.30: 0.42153; SELL 1010.00: 0.64782 (equity_intraday, hand-derived)
    assert cached.round_trip_cost(1000.3, 1010.0, 1) == pytest.approx(1.06935, abs=1e-5)


def test_stock_economics_reads_the_table_once_however_often_it_runs(loads):
    for _ in range(25):
        economics.stock_economics(STOCK, "long", 1000.0, 990.0, 2.0)
    assert len(loads) == 1


def test_a_new_project_root_is_its_own_table(loads, monkeypatch, tmp_path, project_root):
    """Keyed by the file: a settings reload onto another project never reuses the old table."""
    economics._costs_for(STOCK)
    other = tmp_path / "config"
    other.mkdir()
    (other / "costs.yaml").write_text((project_root / "config" / "costs.yaml").read_text())

    class Settings:
        def __init__(self, root: Path) -> None:
            self.project_root = root

    monkeypatch.setattr(economics, "current_settings", lambda: Settings(tmp_path))
    economics._costs_for(STOCK)
    economics._costs_for(STOCK)
    assert [p.parent.parent for p in loads] == [project_root, tmp_path]
