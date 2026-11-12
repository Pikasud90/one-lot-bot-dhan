"""DeskConfig.primary_slots() (the paper test, 2026-11-08): the primary strategies as the
desk runs them, one (name, timeframe, market) per configured slot, so the Home scoreboard
can count each apart from the same strategy's earlier timeframes."""

from __future__ import annotations

from onelot.desk.config import load_desk_config
from onelot.paper.ledger import Slot
from onelot.strategies.registry import REGISTRY, get_strategy


def test_the_paper_test_has_its_candidates_as_primary_slots():
    cfg = load_desk_config()  # the real config/desk.yaml
    assert cfg.primary_slots() == (
        Slot("pullback", "1d", "STOCK"),  # the delivery pullback rule, US-067 (2026-11-12)
        Slot("supertrend_vol", "30m", "INDEX"),
        Slot("supertrend_vol", "60m", "STOCK"),
        Slot("supertrend_adx", "30m", "STOCK"),
        Slot("supertrend_mtf", "5m", "INDEX"),  # the owner's multi-timeframe rule, 2026-11-11
    )


def test_the_multi_timeframe_slot_runs_the_owners_setting():
    """Supertrend(14, 1.7) on every timeframe, all six agreeing, the 60-minute line as the
    stop and the trail (the form the research of 2026-11-11 settled on)."""
    cfg = load_desk_config()
    for index in cfg.indices:
        (slot,) = [s for s in index.strategies if s.name == "supertrend_mtf"]
        assert slot.timeframe == "5m", index.key
        assert slot.params == {
            "allow_long": True,
            "allow_short": False,  # an index is traded by buying an option: calls only here
            "st_period": 14,
            "st_mult": 1.7,
            "gate": "all",
            "trend_tf": "30m",
            "flip_window": 4,
            "trend_adx_min": 25,
            "trend_rsi_min": 60,
            "entry_end": "13:00",
            "trail_tf": "60m",
            "stop_buffer_atr": 2.0,
            "max_trades_per_day": 1,
        }, index.key
        strategy = get_strategy(slot.name, {**slot.params, "timeframe": slot.timeframe})
        assert strategy.multi_timeframe and strategy.trail_timeframe == "60m"
    assert not any(s.name == "supertrend_mtf" for stock in cfg.stocks for s in stock.strategies)


def test_every_configured_setting_is_one_its_strategy_knows():
    """A mistyped setting name is dropped without a word (the strategies' settings ignore
    names they do not know), and the slot would run on the default instead. The real config
    must not have one, and every slot must build."""
    from onelot.delivery.rule import PullbackParams

    cfg = load_desk_config()
    seen = 0
    for inst in cfg.instruments:
        for slot in inst.strategies:
            if slot.product == "delivery":  # US-067: the pullback rule, not a REGISTRY strategy
                PullbackParams.from_mapping(slot.params)  # an unknown name raises
                continue
            known = set(REGISTRY[slot.name].Params.model_fields)
            unknown = sorted(set(slot.params) - known)
            assert not unknown, (inst.key, slot.name, slot.timeframe, unknown)
            get_strategy(slot.name, {**slot.params, "timeframe": slot.timeframe})
            seen += 1
    assert seen == 5 * 2 + 18 * 2  # two slots on each of the five indices and eighteen stocks


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
