"""US-067 D9 and D16: the `enabled` and `product` slot keys, paused slots, the scoreboard row.

A paused slot is not evaluated and raises no alert; its scoreboard row stays and says paused.
A paused slot whose trade is still active keeps being evaluated so the trade is managed as
before (D16); it still never alerts. A delivery slot is never built as a tracker strategy.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desk"))

from desk_harness import new_desk, small_cfg  # noqa: E402

from onelot.desk.config import (  # noqa: E402
    DeskInstrumentCfg,
    StrategySlotCfg,
    load_desk_config,
)
from onelot.paper.ledger import Slot, StrategyTotals  # noqa: E402
from onelot.paper.view import slot_scoreboard_view  # noqa: E402
from onelot.store import init_database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return init_database(f"sqlite:///{tmp_path / 'desk.db'}")


def write(tmp_path, body: str):
    p = tmp_path / "desk.yaml"
    p.write_text(body)
    return load_desk_config(p)


def test_enabled_and_product_are_read_with_safe_defaults(tmp_path):
    cfg = write(
        tmp_path,
        """
stocks:
  - key: SBIN
    strategies:
      - {name: ema_cross, timeframe: 15m}
      - {name: supertrend_vol, timeframe: 60m, enabled: false}
      - {name: pullback, timeframe: 1d, product: delivery, params: {unit_inr: 5000}}
""",
    )
    a, b, c = cfg.stocks[0].strategies
    assert (a.enabled, a.product) == (True, "intraday")
    assert (b.enabled, b.product) == (False, "intraday")
    assert (c.enabled, c.product, c.params) == (True, "delivery", {"unit_inr": 5000})
    assert cfg.paused_slots() == frozenset({Slot("supertrend_vol", "60m", "STOCK")})
    assert [(i.key, s.name) for i, s in cfg.delivery_slots()] == [("SBIN", "pullback")]


@pytest.mark.parametrize("bad", ['"false"', "0", "no_such"])
def test_enabled_must_be_true_or_false(tmp_path, bad):
    with pytest.raises(ValueError, match="enabled"):
        write(
            tmp_path,
            f"stocks:\n  - key: SBIN\n    strategies:\n      - {{name: x, enabled: {bad}}}\n",
        )


def test_an_unknown_product_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="product"):
        write(
            tmp_path, "stocks:\n  - key: SBIN\n    strategies:\n      - {name: x, product: swing}\n"
        )


def test_the_shipped_desk_runs_pullback_on_the_18_stocks_and_microtrend_on_the_indices():
    cfg = load_desk_config()  # US-068 (2026-11-13): no paused slot is left in the file
    on = {(s.name, s.timeframe) for i in cfg.instruments for s in i.strategies if s.enabled}
    assert on == {("pullback", "1d"), ("microtrend", "3m")}
    assert cfg.paused_slots() == frozenset()
    assert len(cfg.delivery_slots()) == 18
    assert all(s.product == "intraday" for i in cfg.indices for s in i.strategies)
    (_, slot) = cfg.delivery_slots()[0]
    assert slot.params["unit_inr"] == 10000 and "Not proven" in slot.params["evidence"]


def test_a_delivery_slot_is_not_a_tracker_strategy(cfg, db):
    sbin = DeskInstrumentCfg(
        "SBIN", "SBIN", "STOCK",
        (StrategySlotCfg("ema_cross", "10m"), StrategySlotCfg("pullback", "1d", {}, True, "delivery")),
    )  # fmt: skip
    desk, _, _ = new_desk(cfg, db, desk_cfg=small_cfg(stocks=(sbin,)))
    assert [s.cfg.name for s in desk.trackers["SBIN"].slots] == ["ema_cross"]


class Trade:
    status = "OPEN"

    def __init__(self, strategy: str, timeframe: str, series_key: str = "SBIN") -> None:
        self.strategy, self.timeframe, self.series_key = strategy, timeframe, series_key
        self.instrument_key, self.direction, self.alert_id = series_key, "long", "A1"


class Book:
    """A duck-typed paper book with one active trade."""

    def __init__(self, trades: list[Trade]) -> None:
        self.trades = trades
        self.steps = 0

    def open_trades(self) -> list[Trade]:
        return list(self.trades)

    def step(self, now, bars, strategy_exit=None):
        self.steps += 1
        self.strategy_exit = strategy_exit
        return []

    def on_alert(self, alert, now):
        raise AssertionError("a paused slot raised an alert")

    def status(self) -> dict[str, Any]:
        return {}


def paused_cfg(trade_slot: bool) -> Any:
    slots = (
        StrategySlotCfg("ema_cross", "10m", {}, False),
        StrategySlotCfg("supertrend_vol", "6m", {}, False),
    )
    return small_cfg(stocks=(DeskInstrumentCfg("SBIN", "SBIN", "STOCK", slots),), indices=())


def test_d9_a_paused_slot_is_not_evaluated_and_never_alerts(cfg, db):
    desk, notifier, _ = new_desk(cfg, db, desk_cfg=paused_cfg(False), paper=Book([]))
    desk.poll()
    for slot in desk.trackers["SBIN"].slots:
        assert slot.last_eval_at is None and slot.last_verdict.reason == "paused"
        assert slot.as_dict()["enabled"] is False
    assert notifier.sent == [] and desk.signals == {}


def test_d16_a_paused_slot_with_an_active_trade_is_still_evaluated_and_still_never_alerts(cfg, db):
    book = Book([Trade("ema_cross", "10m")])
    desk, notifier, _ = new_desk(cfg, db, desk_cfg=paused_cfg(True), paper=book)
    desk.poll()
    ema, st = desk.trackers["SBIN"].slots
    assert ema.last_eval_at is not None and ema.last_bar_ts is not None  # managed as before
    assert st.last_eval_at is None  # no trade: not evaluated
    assert book.steps == 1  # the book still steps its trades
    ema.last_flags = {"long_exit": True}
    assert book.strategy_exit(book.trades[0], desk.last_fetch_at)[0] == "SIGNAL_EXIT"
    assert notifier.sent == [] and desk.signals == {}


def test_d16_a_taken_position_keeps_its_paused_slot_evaluated(cfg, db):
    desk, _, _ = new_desk(cfg, db, desk_cfg=paused_cfg(True), paper=Book([]))
    desk.positions["P1"] = {
        "series_key": "SBIN", "instrument_key": "SBIN", "strategy": "supertrend_vol",
        "timeframe": "6m", "session_over": True,
    }  # fmt: skip
    desk._manage_positions = lambda now, window: (
        None
    )  # the position's own management is not under test
    desk.poll()
    ema, st = desk.trackers["SBIN"].slots
    assert st.last_eval_at is not None and ema.last_eval_at is None


def test_the_scoreboard_row_of_a_paused_slot_stays_and_says_paused():
    primary = (Slot("pullback", "1d", "STOCK"), Slot("supertrend_vol", "60m", "STOCK"))
    totals = {Slot("supertrend_vol", "60m", "STOCK"): StrategyTotals(trades=3, net_paise=500)}
    rows = slot_scoreboard_view(totals, primary, {}, {Slot("supertrend_vol", "60m", "STOCK")})
    assert [(r["strategy"], r["paused"], r["trades"]) for r in rows] == [
        ("pullback", False, 0),
        ("supertrend_vol", True, 3),
    ]
    assert all(r["paused"] is False for r in slot_scoreboard_view(totals, primary, {}))


def test_reload_keeps_paused_slots_built(cfg, db, tmp_path, monkeypatch):
    desk, _, _ = new_desk(cfg, db, desk_cfg=paused_cfg(False), paper=Book([]))
    monkeypatch.setattr("onelot.desk.desk.load_desk_config", lambda: paused_cfg(False))
    desk.reload()
    assert [s.cfg.enabled for s in desk.trackers["SBIN"].slots] == [False, False]
    assert dataclasses.is_dataclass(desk.cfg)
