"""An option's friction is taken once, on its premium (Manager, 2026-11-08).

`run_backtest` used to hand `slippage_pct` (0.03 %) to the simulator for every instrument and
then `execute_trade` priced an option from scratch with its own half-spread and ticks, so an
option paid both: about two points of premium a round trip. Now an OPTIONS instrument is
simulated with 0 slippage, and a stock, a future and a currency future keep `slippage_pct`.

The runs here go through the real `run_backtest`, the real `simulate` and the real
`execute_trade` on one constructed frame; only the bar loading (`generate_for_instrument`) is
replaced. A deliberately large slippage (0.5 %) makes any leak visible.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.data.universe import load_specs
from onelot.lab import runner
from onelot.lab.runner import BacktestConfig, run_backtest
from onelot.strategies.base import StrategyParams

SLIP = 0.5  # percent: absurdly large on purpose
_, SPECS = load_specs()


def morning_frame(px: float, stop_dist: float, atr: float) -> pd.DataFrame:
    """Eight 5-minute bars: a long signal on bar 2, the fill at bar 3's open, a drop through the
    stop on bar 4. Prices are exactly representable so equality can be exact."""
    idx = pd.date_range("2026-09-01 10:00", periods=8, freq="5min", tz=IST)
    opens = [px, px, px, px, px, px - 2 * stop_dist, px - 2 * stop_dist, px - 2 * stop_dist]
    rows = []
    for i, o in enumerate(opens):
        low = o - stop_dist * 1.5 if i == 4 else o - 1.0
        rows.append((o, o + 1.0, low, o))
    f = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    f["volume"] = 1000.0
    f["atr"] = f["atr_trail"] = atr
    f["d_atr"] = np.nan
    for col in (
        "long_entry",
        "short_entry",
        "long_exit",
        "short_exit",
        "setup_long",
        "setup_short",
    ):
        f[col] = False
    f["long_stop"] = f["short_stop"] = np.nan
    f["long_score"] = f["short_score"] = 70.0
    f.loc[idx[2], ["long_entry", "setup_long", "long_stop"]] = [True, True, px - stop_dist]
    return f


def params() -> StrategyParams:
    return StrategyParams(
        stop_atr_min=0.1, stop_atr_max=10, min_risk_pct=0.0, max_risk_pct=5.0, target_r=50.0,
        trail_atr_mult=0.0, cooldown_bars=0, max_trades_per_day=5, entry_start="09:15",
        entry_end="15:00",
    )  # fmt: skip


class Spy:
    """Wraps the real `simulate`, keeping the slippage it was handed."""

    def __init__(self, real) -> None:
        self.real, self.slippage = real, []

    def __call__(self, f, p, symbol, strategy, slippage_pct=0.03, fine=None, start=None):
        self.slippage.append(slippage_pct)
        return self.real(f, p, symbol, strategy, slippage_pct, fine, start)


def backtest(monkeypatch, instrument_key: str, px: float, stop_dist: float, atr: float):
    spec = SPECS[instrument_key]
    frame = morning_frame(px, stop_dist, atr)
    start_day = pd.Timestamp("2026-09-01", tz=IST)
    strategy = SimpleNamespace(p=params())
    monkeypatch.setattr(
        runner,
        "generate_for_instrument",
        lambda cfg, cache: (spec, strategy, frame, None, start_day),
    )
    spy = Spy(runner.simulate)
    monkeypatch.setattr(runner, "simulate", spy)
    cache = SimpleNamespace(vix=lambda: pd.Series(dtype=float))
    cfg = BacktestConfig("supertrend_vol", instrument_key, slippage_pct=SLIP, timeframe="5m")
    return run_backtest(cfg, cache), spy  # type: ignore[arg-type]


class TestOptionsAreSimulatedWithoutSlippage:
    def test_the_simulator_is_handed_zero_for_an_option(self, monkeypatch):
        _, spy = backtest(monkeypatch, "BANKNIFTY_OPT", 45000.0, 100.0, 80.0)
        assert spy.slippage == [0.0]

    def test_the_underlying_entry_is_the_bar_open_exactly(self, monkeypatch):
        res, _ = backtest(monkeypatch, "BANKNIFTY_OPT", 45000.0, 100.0, 80.0)
        (trade,) = res.trades.to_dict("records")
        assert trade["detail"]["underlying_entry"] == 45000.0  # not 45,225 (+0.5 %)

    def test_the_underlying_exit_is_the_stop_level_exactly(self, monkeypatch):
        res, _ = backtest(monkeypatch, "BANKNIFTY_OPT", 45000.0, 100.0, 80.0)
        (trade,) = res.trades.to_dict("records")
        assert trade["exit_reason"] == "stop"
        assert trade["detail"]["underlying_exit"] == 44900.0  # not 44,675.5 (-0.5 %)

    def test_the_option_is_still_priced_with_its_own_spread_and_ticks(self, monkeypatch):
        """The slippage that went is the underlying's; the premium's half-spread (0.6 %) and
        two ticks stay, so the fills are still worse than the model's fair value."""
        res, _ = backtest(monkeypatch, "BANKNIFTY_OPT", 45000.0, 100.0, 80.0)
        d = res.trades.iloc[0]["detail"]
        assert d["pricing"] == "black76_synthetic" and res.metrics["pricing"] == "black76_synthetic"
        assert d["premium_entry"] > 0 and d["premium_exit"] > 0
        assert d["premium_entry"] > d["premium_exit"]  # a long call that lost

    def test_a_stock_option_is_an_option_too(self, monkeypatch):
        _, spy = backtest(monkeypatch, "RELIANCE_OPT", 1000.0, 10.0, 8.0)
        assert spy.slippage == [0.0]

    def test_the_slippage_is_not_added_back_in_execution(self, monkeypatch):
        """The same trade run twice, with 0.5 % and with 0.0 % configured, is identical: the
        option never sees `slippage_pct` at all."""
        wide, _ = backtest(monkeypatch, "BANKNIFTY_OPT", 45000.0, 100.0, 80.0)
        cfg_none = BacktestConfig("supertrend_vol", "BANKNIFTY_OPT", slippage_pct=0.0)
        monkeypatch.setattr(
            runner,
            "generate_for_instrument",
            lambda cfg, cache: (
                SPECS["BANKNIFTY_OPT"],
                SimpleNamespace(p=params()),
                morning_frame(45000.0, 100.0, 80.0),
                None,
                pd.Timestamp("2026-09-01", tz=IST),
            ),
        )
        none = run_backtest(cfg_none, SimpleNamespace(vix=lambda: pd.Series(dtype=float)))  # type: ignore[arg-type]
        a, b = wide.trades.iloc[0], none.trades.iloc[0]
        for col in ("entry_price", "exit_price", "gross_pnl", "costs", "net_pnl", "r_multiple"):
            assert a[col] == b[col], col


class TestLinearInstrumentsKeepTheirSlippage:
    def test_a_stock_is_handed_the_configured_slippage(self, monkeypatch):
        _, spy = backtest(monkeypatch, "RELIANCE", 1000.0, 10.0, 8.0)
        assert spy.slippage == [SLIP]

    def test_a_stocks_entry_carries_the_slippage_against_the_trader(self, monkeypatch):
        res, _ = backtest(monkeypatch, "RELIANCE", 1000.0, 10.0, 8.0)
        (trade,) = res.trades.to_dict("records")
        assert trade["entry_price"] == pytest.approx(1000.0 * (1 + SLIP / 100))  # 1,005.0

    def test_a_stocks_stop_exit_carries_it_too(self, monkeypatch):
        res, _ = backtest(monkeypatch, "RELIANCE", 1000.0, 10.0, 8.0)
        (trade,) = res.trades.to_dict("records")
        assert trade["exit_price"] == pytest.approx(990.0 * (1 - SLIP / 100))  # 985.05

    def test_a_future_keeps_it(self, monkeypatch):
        _, spy = backtest(monkeypatch, "BANKNIFTY_FUT", 45000.0, 100.0, 80.0)
        assert spy.slippage == [SLIP]

    def test_a_future_entry_carries_the_slippage(self, monkeypatch):
        res, _ = backtest(monkeypatch, "BANKNIFTY_FUT", 45000.0, 100.0, 80.0)
        (trade,) = res.trades.to_dict("records")
        assert trade["entry_price"] == pytest.approx(45000.0 * (1 + SLIP / 100))

    def test_a_currency_future_keeps_it(self, monkeypatch):
        _, spy = backtest(monkeypatch, "USDINR", 84.0, 0.2, 0.15)
        assert spy.slippage == [SLIP]

    def test_the_slippage_comes_from_the_run_not_from_a_constant(self, monkeypatch):
        spec = SPECS["RELIANCE"]
        frame = morning_frame(1000.0, 10.0, 8.0)
        monkeypatch.setattr(
            runner,
            "generate_for_instrument",
            lambda cfg, cache: (
                spec, SimpleNamespace(p=params()), frame, None, pd.Timestamp("2026-09-01", tz=IST)
            ),
        )  # fmt: skip
        spy = Spy(runner.simulate)
        monkeypatch.setattr(runner, "simulate", spy)
        cache = SimpleNamespace(vix=lambda: pd.Series(dtype=float))
        for slip in (0.0, 0.03, 0.2):
            run_backtest(BacktestConfig("s", "RELIANCE", slippage_pct=slip), cache)  # type: ignore[arg-type]
        assert spy.slippage == [0.0, 0.03, 0.2]


class TestTheRuleFollowsTheInstrumentNotTheStrategy:
    @pytest.mark.parametrize(
        ("key", "px", "dist", "atr", "expected"),
        [
            ("BANKNIFTY_OPT", 45000.0, 100.0, 80.0, 0.0),
            ("NIFTY_OPT", 25000.0, 60.0, 50.0, 0.0),
            ("RELIANCE_OPT", 1000.0, 10.0, 8.0, 0.0),
            ("RELIANCE", 1000.0, 10.0, 8.0, SLIP),
            ("BANKNIFTY_FUT", 45000.0, 100.0, 80.0, SLIP),
        ],
    )
    def test_each_instrument_gets_its_own_slippage(self, monkeypatch, key, px, dist, atr, expected):
        _, spy = backtest(monkeypatch, key, px, dist, atr)
        assert spy.slippage == [expected]
