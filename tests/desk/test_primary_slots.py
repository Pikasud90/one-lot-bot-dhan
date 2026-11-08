"""DeskConfig.primary_slots() (the paper test, 2026-11-08): the primary strategies as the
desk runs them, one (name, timeframe, market) per configured slot, so the Home scoreboard
can count each apart from the same strategy's earlier timeframes."""

from __future__ import annotations

from onelot.desk.config import load_desk_config
from onelot.paper.ledger import Slot


def test_the_paper_test_has_its_three_candidates_as_primary_slots():
    cfg = load_desk_config()  # the real config/desk.yaml
    assert cfg.primary_slots() == (
        Slot("supertrend_vol", "30m", "INDEX"),
        Slot("supertrend_vol", "60m", "STOCK"),
        Slot("supertrend_adx", "30m", "STOCK"),
    )


def test_owner_order_first_then_configuration_order_each_slot_once(tmp_path):
    path = tmp_path / "desk.yaml"
    path.write_text(
        """
primary_strategies: [b_strat, a_strat]
defaults:
  index_strategies:
    - {name: a_strat, timeframe: 5m}
    - {name: b_strat, timeframe: 15m}
  stock_strategies:
    - {name: a_strat, timeframe: 5m}
    - {name: c_strat, timeframe: 15m}
indices:
  - {key: NIFTY}
  - {key: BANKNIFTY}
stocks:
  - {key: SBIN}
  - {key: INFY, strategies: [{name: a_strat, timeframe: 30m}]}
""",
        encoding="utf-8",
    )
    cfg = load_desk_config(path)
    assert cfg.primary_strategies() == ("b_strat", "a_strat", "c_strat")
    assert cfg.primary_slots() == (
        ("b_strat", "15m", "INDEX"),
        ("a_strat", "5m", "INDEX"),
        ("a_strat", "5m", "STOCK"),
        ("a_strat", "30m", "STOCK"),
        ("c_strat", "15m", "STOCK"),
    )


def test_no_instruments_no_slots(tmp_path):
    path = tmp_path / "desk.yaml"
    path.write_text("primary_strategies: [a_strat]\n", encoding="utf-8")
    assert load_desk_config(path).primary_slots() == ()
