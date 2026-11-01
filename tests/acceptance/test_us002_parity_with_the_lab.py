"""US-002 NFR: a parity test on intraday fixtures documents the deliberate differences between
`manage_step` and the Lab's `simulate` (ADR-001 amendment 2: the rules are PORTED, never called).

Same trade, same bars, `slippage_pct = 0`, 1-minute bars. Where the rules agree the two engines
close on the same bar at the same price with the same reason (stop touch, gap through the stop,
a target traded through, the session end, the end-of-day fallback, a strategy exit, a time
stop). Where they deliberately differ, the difference is pinned so nobody "fixes" it by accident:

  * a target is filled only when TRADED THROUGH by one tick (the Lab fills on a touch)
  * the trail arms on the bar CLOSE (the Lab arms on the best price of the bar)
  * there are no partial exits (the Lab books a partial leg)
  * the end-of-day fallback closes SESSION_END; no end-of-day force exists for a trade that is
    meant to be carried (swing carry is US-027, and not part of this story)

`simulate` is only the oracle here: the code under test never calls it.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U

from onelot.lab.simulator import simulate
from onelot.strategies.base import StrategyParams

P = {
    "stop_atr_min": 0.1,
    "stop_atr_max": 10,
    "min_risk_pct": 0.0,
    "target_r": 2.0,
    "trail_atr_mult": 0.0,
    "cooldown_bars": 0,
    "max_trades_per_day": 5,
    "entry_start": "09:15",
    "entry_end": "15:00",
}
REASON = {
    "stop": "STOP",
    "trail": "TRAIL_STOP",
    "target": "TARGET",
    "signal": "SIGNAL_EXIT",
    "time": "TIME_STOP",
    "square_off": "SESSION_END",
    "eod": "SESSION_END",
}
FLAT = (100.0, 100.1, 99.9, 100.0)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def lab_frame(rows, start="2026-10-29 10:00"):
    """The Lab simulator's input frame (indicator columns pinned: atr = atr_trail = 1.0)."""
    idx = pd.date_range(start, periods=len(rows), freq="1min", tz="Asia/Kolkata")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 1000.0
    df["atr"] = 1.0
    df["atr_trail"] = 1.0
    df["d_atr"] = np.nan
    for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
        df[col] = False
    df["long_stop"] = np.nan
    df["short_stop"] = np.nan
    df["long_score"] = 70.0
    df["short_score"] = 70.0
    return df


def lab_run(rows, *, start="2026-10-29 10:00", signal_i=2, stop=99.0, exit_i=None, **params):
    """The Lab's one trade for a long signalled on bar `signal_i` (filled at the next open)."""
    df = lab_frame(rows, start)
    df.loc[df.index[signal_i], ["long_entry", "long_stop"]] = [True, stop]
    if exit_i is not None:
        df.loc[df.index[exit_i], "long_exit"] = True
    trades = simulate(df, StrategyParams(**{**P, **params}), "X", "s", slippage_pct=0.0)
    assert len(trades) == 1, "the fixture must produce exactly one Lab trade"
    return df, trades[0]


def paper_run(df, trade, *, rules=None, now=None, se=None):
    """The same trade through manage_step: opened at the Lab's fill bar, same levels."""
    to_paise = H.to_paise
    entry = to_paise(trade.entry_price)
    state = U.manage().PositionState(
        direction="long",
        kind="STOCK",
        qty=1,
        entry_paise=entry,
        initial_stop_paise=to_paise(trade.stop_initial),
        stop_paise=to_paise(trade.stop_initial),
        target_paise=None if trade.target is None else to_paise(trade.target),
        trail_active=False,
        opened_at=trade.entry_ts.to_pydatetime(),
        last_bar_ts=None,
        bars_held=0,
        sessions_held=0,
        option=None,
        **({"trade_id": 7} if "trade_id" in U.manage().PositionState.__dataclass_fields__ else {}),
    )
    bars = df.loc[:, ["open", "high", "low", "close", "volume"]]
    when = now or (df.index[-1] + pd.Timedelta(minutes=1)).to_pydatetime()
    return U.manage().manage_step(state, bars, rules or U.mk_rules(), when, se)


def agree(trade, events):
    """The Lab trade and the paper close: same reason, same level, same bar."""
    close = U.close_of(events)
    assert close is not None, "manage_step did not close where the Lab did"
    assert close.reason == REASON[trade.exit_reason], (close.reason, trade.exit_reason)
    assert close.price == H.to_paise(trade.legs[-1].price)
    assert (
        trade.exit_ts.to_pydatetime()
        <= close.at
        <= trade.exit_ts.to_pydatetime() + dt.timedelta(minutes=1)
    )
    return close


def rows_with(bar, n_after=3):
    """flat, flat, flat (signal on the 3rd), the fill bar, then `bar`, then flat bars."""
    return [FLAT, FLAT, FLAT, FLAT, bar] + [FLAT] * n_after


class TestWhereTheTwoEnginesAgree:
    def test_a_stop_touch_inside_a_bar_closes_at_the_stop(self):
        df, trade = lab_run(rows_with((100.0, 100.4, 98.9, 99.5)))
        close = agree(trade, paper_run(df, trade)[1])
        assert close.price == 9900

    def test_a_gap_through_the_stop_closes_at_the_open(self):
        df, trade = lab_run(rows_with((98.5, 98.7, 98.0, 98.6)))
        close = agree(trade, paper_run(df, trade)[1])
        assert close.price == 9850 and close.gap is True

    def test_stop_and_target_in_one_bar_is_a_stop_in_both(self):
        df, trade = lab_run(rows_with((100.0, 103.0, 98.5, 101.0)))
        assert trade.exit_reason == "stop"
        agree(trade, paper_run(df, trade)[1])

    def test_a_target_traded_through_fills_at_the_target_in_both(self):
        df, trade = lab_run(rows_with((100.0, 102.2, 99.9, 102.0)))
        close = agree(trade, paper_run(df, trade)[1])
        assert close.price == 10200

    def test_the_session_end_closes_at_the_first_open_from_exit_time(self):
        rows = [FLAT] * 25  # 14:58 .. 15:22; the signal bar 15:00 fills at 15:01
        df, trade = lab_run(rows, start="2026-10-29 14:58")
        assert trade.exit_ts.strftime("%H:%M") == "15:15"
        close = agree(trade, paper_run(df, trade)[1])
        assert close.at == H.at(15, 15)

    def test_the_end_of_day_fallback_closes_at_the_last_bars_close(self):
        rows = [FLAT, FLAT, FLAT, FLAT, (100.0, 100.2, 99.9, 100.17)]  # the feed stops at 15:02
        df, trade = lab_run(rows, start="2026-10-29 14:58")
        assert trade.exit_reason == "eod"
        close = agree(trade, paper_run(df, trade, now=H.at(15, 40))[1])
        assert close.price == 10017

    def test_a_strategy_exit_closes_at_the_next_bar_open(self):
        rows = rows_with((100.0, 100.3, 99.8, 100.2), n_after=3)
        rows[6] = (100.3, 100.4, 100.2, 100.35)
        df, trade = lab_run(rows, exit_i=4)  # the exit flag is on bar 4, closing at df.index[5]
        se = U.strategy_exit("SIGNAL_EXIT", df.index[5].to_pydatetime())
        close = agree(trade, paper_run(df, trade, se=se)[1])
        assert close.at == df.index[5].to_pydatetime()

    def test_a_time_stop_closes_at_the_open_after_the_last_allowed_bar(self):
        rows = [FLAT, FLAT, FLAT] + [(100.0 + 0.05 * i, 100.3, 99.8, 100.0) for i in range(8)]
        df, trade = lab_run(rows, max_bars=3)
        rules = U.mk_rules(max_bars=3, tf_min=1)
        close = agree(trade, paper_run(df, trade, rules=rules)[1])
        assert close.reason == "TIME_STOP"

    def test_an_exit_due_at_the_open_closes_before_a_dip_later_in_that_bar_in_both(self):
        """Manager decision 1: chronological bars. The Lab closes a pending exit at the open
        before it looks inside the bar (simulator lines 215-217); so does the paper rule."""
        rows = rows_with((100.3, 100.4, 100.2, 100.35), n_after=0)
        rows += [(100.2, 100.3, 98.8, 99.4), FLAT]  # bar 6: the exit is due at 100.20, then a dip
        df, trade = lab_run(rows, exit_i=4)  # the exit flag on bar 4, due at the open of bar 5
        assert trade.exit_reason == "signal"
        se = U.strategy_exit("SIGNAL_EXIT", df.index[5].to_pydatetime())
        close = agree(trade, paper_run(df, trade, se=se)[1])
        assert close.at == df.index[5].to_pydatetime()

    def test_the_session_end_open_closes_before_a_dip_later_in_that_bar_in_both(self):
        rows = [FLAT] * 17 + [(100.0, 100.1, 98.8, 99.4)] + [FLAT] * 6  # the 15:15 bar dips
        df, trade = lab_run(rows, start="2026-10-29 14:58")
        assert trade.exit_reason == "square_off" and trade.exit_ts.strftime("%H:%M") == "15:15"
        close = agree(trade, paper_run(df, trade)[1])
        assert close.at == H.at(15, 15) and close.reason == "SESSION_END"

    def test_the_trail_agrees_when_the_close_is_what_arms_it(self):
        """A bar that CLOSES at +1.2 R arms the trail in both; the next bar touches it."""
        rows = rows_with((100.0, 101.3, 99.9, 101.2), n_after=0) + [(101.2, 101.3, 100.5, 100.8)]
        df, trade = lab_run(rows, trail_atr_mult=0.5, trail_activate_r=1.0)
        assert trade.exit_reason == "trail"
        rules = U.trail_rules(mult=0.5, activate=1.0, atr=1.0)
        close = agree(trade, paper_run(df, trade, rules=rules)[1])
        assert close.price == 10070  # 101.20 - 0.5 x 1.00


class TestTheDeliberateDifferences:
    def test_a_target_is_filled_only_when_traded_through_the_lab_fills_on_a_touch(self):
        df, trade = lab_run(rows_with((100.0, 102.0, 99.9, 101.0)))
        assert trade.exit_reason == "target" and H.to_paise(trade.legs[-1].price) == 10200
        _, events = paper_run(df.iloc[:5], trade)  # through the bar whose high is exactly T
        assert U.close_of(events) is None, "a bar that only REACHES the target does not fill it"

    def test_the_trail_arms_on_the_close_the_lab_arms_on_the_bars_best_price(self):
        """High +1.5 R but a weak close (+0.2 R): the Lab arms (stop -> 99.70) and exits there on
        the next dip; the paper trail stays unarmed and the same dip (99.60) is no stop."""
        rows = rows_with((100.0, 101.5, 99.9, 100.2), n_after=0) + [(100.0, 100.1, 99.6, 99.8)]
        df, trade = lab_run(rows + [FLAT] * 2, trail_atr_mult=0.5, trail_activate_r=1.0)
        assert trade.exit_reason == "trail"
        rules = U.trail_rules(mult=0.5, activate=1.0, atr=1.0)
        state, events = paper_run(df, trade, rules=rules)
        assert U.close_of(events) is None or U.close_of(events).reason != "TRAIL_STOP"
        assert state.trail_active is False and state.stop_paise == 9900

    def test_there_are_no_partial_exits_the_lab_books_a_partial_leg(self):
        rows = [FLAT] * 3 + [FLAT, (100.0, 101.2, 99.9, 101.0)] + [FLAT] * 35  # 14:40 .. 15:19
        df, trade = lab_run(
            rows,
            start="2026-10-29 14:40",
            partial_r=1.0,
            partial_frac=0.5,
            breakeven_after_partial=False,
        )
        assert len(trade.legs) == 2 and trade.legs[0].reason == "partial"
        state, events = paper_run(df, trade)
        assert len(U.closes_of(events)) == 1, "one close, no partial event"
        assert all(str(H.val(e.kind)) != "PARTIAL" for e in events)

    def test_the_session_end_fallback_is_a_reason_of_its_own_not_the_labs_eod(self):
        rows = [FLAT, FLAT, FLAT, FLAT, (100.0, 100.2, 99.9, 100.17)]
        df, trade = lab_run(rows, start="2026-10-29 14:58")
        close = U.close_of(paper_run(df, trade, now=H.at(15, 40))[1])
        assert trade.exit_reason == "eod" and close.reason == "SESSION_END"
