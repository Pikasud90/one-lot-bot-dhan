"""Shared fixtures. No test in this suite requires live broker credentials."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onelot.config import AppConfig, CostConfig  # noqa: E402
from onelot.costs import CostModel  # noqa: E402


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def cfg() -> AppConfig:
    return AppConfig.load(ROOT / "config" / "config.yaml", project_root=ROOT)


@pytest.fixture(scope="session")
def cost_cfg() -> CostConfig:
    return CostConfig.load(ROOT / "config" / "costs.yaml")


@pytest.fixture(scope="session")
def cost_model(cost_cfg) -> CostModel:
    return CostModel(cost_cfg)


@pytest.fixture(scope="session")
def rich_cfg(cfg):
    """Config with capital set, so sizing gates can actually pass."""
    return cfg.model_copy(
        update={"account": cfg.account.model_copy(update={"capital_inr": 1_000_000.0})}
    )


@pytest.fixture()
def mock_broker():
    """A mock broker positioned mid-session on a sane synthetic tape."""
    import datetime as dt

    from onelot.broker.mock import MockBroker, SyntheticConfig, SyntheticMarket
    from onelot.clock import IST

    m = SyntheticMarket(SyntheticConfig(seed=7, sessions=120, trend_strength=0.006))
    day = m.session_dates[-1]
    return MockBroker(m, now=dt.datetime.combine(day, dt.time(11, 0), tzinfo=IST))


@pytest.fixture()
def enriched_chain(mock_broker):
    """A polled, greek-validated chain snapshot."""
    from onelot.market.chain import ChainCache

    c = ChainCache(
        mock_broker,
        "BANKNIFTY",
        mock_broker.expiry_date.isoformat(),
        forward_price_fn=lambda: mock_broker.market.futures_price(mock_broker.now()),
    )
    assert c.poll_once(mock_broker.expiry_dt, mock_broker.now())
    return c
