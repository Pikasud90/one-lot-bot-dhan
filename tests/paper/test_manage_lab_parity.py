"""The paper trail against the Lab's on a 15-minute strategy over 1-minute bars (US-002
review round 1, MAJOR-1; Manager decision option a).

`lab.simulator.simulate` runs on the strategy's own 15-minute bars (resampled with
`resample_bars`) and moves the trail once per strategy bar, from its close, with the
strategy's TRAIL ATR. `manage_step` walks the 1-minute bars; it must move the stop only at
the 1-minute closes that close a 15-minute bar, with the alert's frozen `atr_trail`, and so
close where the Lab does. `simulate` is the oracle only: the code under test never calls it.

The path (rupees; entry 100.00 at 10:00, stop 99.00, risk 1.00; ATR(14) 1.00, ATR(5) 0.40):

    09:15 .. 09:59  flat at 100.00; the 09:45 strategy bar signals the long
    10:00 .. 10:14  bar A climbs to a 101.50 close (+1.5 R): the stop trails to 101.10
    10:15 .. 10:29  bar B peaks at a 102.50 close at 10:24, then falls back to 101.50;
                    a FINE-bar trail would sit at 102.10 and stop out at 10:26
    10:30 .. 10:44  bar C falls 0.10 a minute and touches 101.10 at 10:33

The deliberate difference that remains (manage.py docstring): the paper freezes ATR(5) at
the alert while the Lab re-reads it at every strategy bar.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd
import pytest

from onelot.data.resample import resample_bars
from onelot.lab.simulator import SimTrade, simulate
from onelot.paper.events import PaperEvent
from onelot.paper.manage import ManageRules, PaperClose, PositionState, manage_step
from onelot.paper.position import rules_from_alert
from onelot.strategies.base import StrategyParams

from .conftest import THU, ist

ATR, ATR_TRAIL = 1.0, 0.40  # the stop is sized on ATR(14); the trail uses ATR(5)
MANAGE: dict[str, Any] = {
    "target_r": 0.0,
    "trail_atr_mult": 1.0,
    "trail_activate_r": 1.0,
    "max_bars": 0,
    "exit_time": "15:15",
    "atr": ATR,
    "atr_trail": ATR_TRAIL,
    "tf_min": 15,
}
LAB_PARAMS = StrategyParams(
    timeframe="15m",
    stop_atr_min=0.1,
    stop_atr_max=10,
    min_risk_pct=0.0,
    target_r=0.0,
    trail_atr_mult=1.0,
    trail_activate_r=1.0,
    cooldown_bars=0,
    max_trades_per_day=5,
    entry_start="09:15",
    entry_end="15:00",
)


def at(hh: int, mm: int) -> dt.datetime:
    return ist(THU, hh, mm)


def one_minute_bars() -> pd.DataFrame:
    rows: list[tuple[float, float, float, float]] = [(100.00, 100.10, 99.90, 100.00)] * 45
    for k in range(15):  # bar A: +0.10 a minute
        o = round(100.00 + 0.10 * k, 2)
        rows.append((o, round(o + 0.15, 2), round(o - 0.05, 2), round(o + 0.10, 2)))
    for j in range(15):  # bar B: up to a 102.50 close at 10:24, then down 0.20 a minute
        if j < 10:
            o = round(101.50 + 0.10 * j, 2)
            rows.append((o, round(o + 0.15, 2), round(o - 0.05, 2), round(o + 0.10, 2)))
        else:
            o = round(102.50 - 0.20 * (j - 10), 2)
            rows.append((o, round(o + 0.05, 2), round(o - 0.25, 2), round(o - 0.20, 2)))
    for k in range(15):  # bar C: down 0.10 a minute
        o = round(101.50 - 0.10 * k, 2)
        rows.append((o, round(o + 0.05, 2), round(o - 0.15, 2), round(o - 0.10, 2)))
    index = pd.date_range(at(9, 15), periods=len(rows), freq="1min")
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
    return frame.assign(volume=1000.0)


def lab_trade(atr_trail: list[float] | None = None) -> SimTrade:
    """The Lab's trade on the 15-minute bars: the 09:45 bar signals, 10:00 fills."""
    f = resample_bars(one_minute_bars(), "15m")
    f["atr"] = ATR
    f["atr_trail"] = ATR_TRAIL if atr_trail is None else atr_trail
    f["d_atr"] = np.nan
    for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
        f[col] = False
    f["long_stop"], f["short_stop"] = np.nan, np.nan
    f["long_score"], f["short_score"] = 70.0, 70.0
    f.loc[at(9, 45), ["long_entry", "long_stop"]] = [True, 99.00]
    (trade,) = simulate(f, LAB_PARAMS, "X", "s", slippage_pct=0.0)
    return trade


def paper(rules: ManageRules) -> tuple[PositionState, list[PaperEvent]]:
    state = PositionState(
        direction="long",
        kind="STOCK",
        qty=1,
        entry_paise=10_000,
        initial_stop_paise=9_900,
        stop_paise=9_900,
        target_paise=None,
        trail_active=False,
        opened_at=at(10, 0),
        last_bar_ts=None,
        bars_held=0,
        sessions_held=0,
        option=None,
    )
    return manage_step(state, one_minute_bars(), rules, at(10, 45))


def rules_of(manage: dict[str, Any]) -> ManageRules:
    return rules_from_alert({"manage": manage}, tick_paise=5, resolution=1, timeframe="15m")


class TestTheTrailAgreesWithTheLab:
    def test_the_lab_trails_once_per_strategy_bar_and_stops_at_101_10(self) -> None:
        trade = lab_trade()
        assert (trade.entry_ts, trade.entry_price, trade.stop_initial) == (
            pd.Timestamp(at(10, 0)),
            100.00,
            99.00,
        )
        (leg,) = trade.legs
        assert (leg.reason, leg.price, leg.ts) == ("trail", pytest.approx(101.10), at(10, 30))

    def test_the_paper_trade_closes_at_the_labs_level_inside_the_labs_bar(self) -> None:
        state, events = paper(rules_of(MANAGE))
        assert state.closed == PaperClose("TRAIL_STOP", 10_110, at(10, 33), gap=False)
        assert at(10, 30) <= state.closed.at < at(10, 45)  # inside the Lab's exit bar
        moves = [(e.ts, e.detail["to_paise"]) for e in events if e.kind == "STOP_MOVED"]
        assert moves == [(at(10, 15), 10_110)]  # bar A's close; bar B's close moves nothing

    def test_every_stop_move_is_at_a_strategy_bar_close(self) -> None:
        _, events = paper(rules_of(MANAGE))
        assert all(e.ts.minute % 15 == 0 for e in events if e.kind == "STOP_MOVED")


class TestWhatTheFixPrevents:
    def test_a_trail_on_every_fine_bar_would_stop_out_inside_bar_b(self) -> None:
        """The review's finding, pinned: with the fine bars as the strategy bars the stop
        follows the 10:24 peak to 102.10 and the 10:26 dip takes it, a level the Lab never
        had."""
        state, _ = paper(rules_of({**MANAGE, "tf_min": 1}))
        assert state.closed == PaperClose("TRAIL_STOP", 10_210, at(10, 26), gap=False)

    def test_trailing_with_the_regular_atr_would_stop_somewhere_else(self) -> None:
        """Without atr_trail (an older alert) the ATR(14) of 1.00 trails to 100.50 only."""
        manage = {k: v for k, v in MANAGE.items() if k != "atr_trail"}
        state, _ = paper(rules_of(manage))
        assert state.closed is not None and state.closed.price_paise == 10_050


class TestTheDeliberateDifference:
    def test_the_paper_trail_atr_is_frozen_while_the_labs_evolves(self) -> None:
        """The Lab re-reads ATR(5) at every strategy bar: 0.20 by bar B's close trails it to
        101.30. The paper keeps the 0.40 frozen at the alert (the signal bar's value) and
        stays at 101.10. Documented in manage.py; US-027 may revisit it."""
        lab = lab_trade(atr_trail=[ATR_TRAIL] * 4 + [0.20, 0.20])
        assert lab.legs[-1].price == pytest.approx(101.30)
        state, _ = paper(rules_of(MANAGE))
        assert state.closed is not None and state.closed.price_paise == 10_110
