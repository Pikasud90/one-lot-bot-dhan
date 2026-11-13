"""DeskConfig.primary_slots() (the paper test, 2026-11-08): the primary strategies as the
desk runs them, one (name, timeframe, market) per configured slot, so the Home scoreboard
can count each apart from the same strategy's earlier timeframes."""

from __future__ import annotations

from onelot.desk.config import load_desk_config
from onelot.paper.ledger import Slot
from onelot.strategies.registry import REGISTRY, get_strategy


def test_the_paper_test_has_its_candidates_as_primary_slots():
    cfg = load_desk_config()  # the real config/desk.yaml
    # US-068 (owner, 2026-11-13): two rules, nothing else; the paused slots left the file
    assert cfg.primary_slots() == (
        Slot("pullback", "1d", "STOCK"),  # the delivery pullback rule, US-067 (2026-11-12)
        Slot("microtrend", "3m", "INDEX"),  # the owner's options rule, US-068
    )


def test_the_micro_trend_slot_runs_the_owners_setting():
    """US-068: Supertrend(14, 1.7) on 3m, 5m, 10m, 15m, 30m, 60m and 4h; out at 3 of 7; the
    backstop 3 ATR(60m) beyond the 60m line; the evidence line on every alert."""
    cfg = load_desk_config()
    for index in cfg.indices:
        (slot,) = index.strategies
        assert (slot.name, slot.timeframe, slot.enabled) == ("microtrend", "3m", True), index.key
        settings = {k: v for k, v in slot.params.items() if k != "evidence"}
        assert settings == {"st_period": 14, "st_mult": 1.7, "exit_count": 3,
                            "stop_buffer_atr": 3.0}, index.key  # fmt: skip
        assert "Not an edge: this is the paper test." in slot.params["evidence"]
        strategy = get_strategy(slot.name, {**settings, "timeframe": slot.timeframe})
        assert strategy.multi_timeframe
        assert strategy.ladder_timeframes == ("3m", "5m", "10m", "15m", "30m", "60m", "240m")
    assert all(
        [(s.name, s.product) for s in stock.strategies] == [("pullback", "delivery")]
        for stock in cfg.stocks
    )


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
            known = set(REGISTRY[slot.name].Params.model_fields) | {"evidence"}  # US-068
            unknown = sorted(set(slot.params) - known)
            assert not unknown, (inst.key, slot.name, slot.timeframe, unknown)
            settings = {k: v for k, v in slot.params.items() if k != "evidence"}
            get_strategy(slot.name, {**settings, "timeframe": slot.timeframe})
            seen += 1
    assert seen == 5  # US-068: one intraday slot on each of the five indices


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
