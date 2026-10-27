"""Simulator edge cases (fills, stops, targets, trailing, partials, square-off) and
the one-unit execution models."""

import numpy as np
import pandas as pd
import pytest

from onelot.config import BacktestCfg, CostConfig
from onelot.costs import CostModel
from onelot.data.universe import InstrumentSpec
from onelot.lab.execution import (
    OptionPricer,
    execute_trade,
    monthly_expiry,
    strike_for_delta,
    thirty_percent_rule,
)
from onelot.lab.simulator import simulate
from onelot.strategies.base import StrategyParams


def frame(ohlc, start="2026-09-01 09:15", freq="5min"):
    idx = pd.date_range(start, periods=len(ohlc), freq=freq, tz="Asia/Kolkata")
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx, dtype=float)
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


P = {"stop_atr_min": 0.1, "stop_atr_max": 10, "min_risk_pct": 0.0, "target_r": 2.0, "trail_atr_mult": 0.0,
         "cooldown_bars": 0, "max_trades_per_day": 5, "entry_start": "09:15", "entry_end": "15:00"}


def flat(n, px=100.0):
    return [[px, px + 0.1, px - 0.1, px]] * n


def test_long_hits_target():
    bars = flat(3) + [[100, 100.5, 99.5, 100.2], [100.2, 103, 100, 102.5]] + flat(3, 102.5)
    df = frame(bars)
    df.loc[df.index[2], "long_entry"] = True
    df.loc[df.index[2], "long_stop"] = 99.0
    t = simulate(df, StrategyParams(**P), "X", "s", slippage_pct=0.0)
    assert len(t) == 1
    tr = t[0]
    assert tr.entry_ts == df.index[3] and tr.entry_price == 100.0
    assert tr.exit_reason == "target" and tr.legs[-1].price == pytest.approx(102.0)
    assert tr.r_multiple == pytest.approx(2.0)


def test_gap_through_stop_exits_at_open():
    bars = flat(3) + [[100, 100.4, 99.8, 100], [97.0, 97.5, 96.5, 97.2]] + flat(2, 97)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    t = simulate(df, StrategyParams(**P), "X", "s", slippage_pct=0.0)
    assert t[0].exit_reason == "stop" and t[0].legs[-1].price == 97.0
    assert t[0].r_multiple == pytest.approx(-3.0)


def test_entry_skipped_when_open_gaps_beyond_stop():
    bars = flat(3) + [[98.5, 99, 98, 98.6]] + flat(3, 98.6)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    assert simulate(df, StrategyParams(**P), "X", "s", slippage_pct=0.0) == []


def test_same_bar_stop_and_target_is_conservative_without_fine_data():
    bars = flat(3) + [[100, 100.2, 99.9, 100], [100, 103, 98.5, 101]] + flat(2, 101)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    t = simulate(df, StrategyParams(**P), "X", "s", slippage_pct=0.0)
    assert t[0].exit_reason == "stop"


def test_fine_path_resolves_target_first():
    bars = flat(3) + [[100, 100.2, 99.9, 100], [100, 103, 98.5, 101]] + flat(2, 101)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    amb = df.index[4]
    fine = pd.DataFrame({"open": [100, 101, 102.5, 101, 99], "high": [101, 102.5, 103, 101.5, 99.5],
                         "low": [99.9, 100.8, 102, 100, 98.5], "close": [101, 102.4, 102.6, 100.2, 99]},
                        index=pd.date_range(amb, periods=5, freq="1min"))
    t = simulate(df, StrategyParams(**P), "X", "s", slippage_pct=0.0, fine=fine)
    assert t[0].exit_reason == "target"


def test_square_off_and_no_overnight():
    df = frame(flat(14), start="2026-09-01 14:15")
    i = list(df.index.strftime("%H:%M")).index("15:05")
    p = StrategyParams(**{**P, "entry_end": "15:10"})
    df.loc[df.index[i], ["long_entry", "long_stop"]] = [True, 99.0]
    t = simulate(df, p, "X", "s", slippage_pct=0.0)
    assert len(t) == 1 and t[0].exit_reason == "square_off"
    assert t[0].exit_ts.strftime("%H:%M") == "15:15"


def test_currency_session_square_off_honoured():
    """A currency instrument squares off at 16:45, not 15:15."""
    df = frame(flat(24), start="2026-09-01 15:00")
    i = list(df.index.strftime("%H:%M")).index("16:30")
    p = StrategyParams(**{**P, "entry_end": "16:40", "exit_time": "16:45"})
    df.loc[df.index[i], ["long_entry", "long_stop"]] = [True, 99.0]
    t = simulate(df, p, "X", "s", slippage_pct=0.0)
    assert len(t) == 1 and t[0].exit_reason == "square_off"
    assert t[0].exit_ts.strftime("%H:%M") == "16:45"


def test_signal_on_last_bar_of_day_is_dropped():
    d1 = frame(flat(3), start="2026-09-01 15:10")
    d2 = frame(flat(3), start="2026-09-02 09:15")
    df = pd.concat([d1, d2])
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    assert simulate(df, StrategyParams(**P), "X", "s", slippage_pct=0.0) == []


def test_trailing_stop_ratchets_and_exits():
    bars = flat(3) + [[100, 100.1, 99.9, 100], [100, 102.2, 100, 102], [102, 104.2, 102, 104],
                      [104, 104.1, 102.4, 102.5]] + flat(2, 102.5)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    p = StrategyParams(**{**P, "target_r": 0.0, "trail_atr_mult": 1.5, "trail_activate_r": 1.0})
    t = simulate(df, p, "X", "s", slippage_pct=0.0)
    assert t[0].exit_reason == "trail" and t[0].legs[-1].price == pytest.approx(102.5)


def test_min_risk_floor_widens_stop():
    bars = flat(3) + [[100, 100.1, 99.9, 100]] + flat(4)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.95]
    p = StrategyParams(**{**P, "min_risk_pct": 0.5})
    t = simulate(df, p, "X", "s", slippage_pct=0.0)
    assert t[0].risk_ps == pytest.approx(0.5) and t[0].stop_initial == pytest.approx(99.5)


def test_partial_then_breakeven():
    bars = flat(3) + [[100, 100.1, 99.9, 100], [100, 101.2, 100.5, 101], [101, 101.1, 99.9, 100]] + flat(2, 100)
    df = frame(bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    p = StrategyParams(**{**P, "target_r": 3.0, "partial_r": 1.0, "partial_frac": 0.5})
    t = simulate(df, p, "X", "s", slippage_pct=0.0)
    legs = t[0].legs
    assert legs[0].reason == "partial" and legs[0].price == pytest.approx(101.0)
    assert legs[1].reason == "breakeven" and legs[1].price == pytest.approx(100.0)
    assert t[0].r_multiple == pytest.approx(0.5)


# ── execution models (one unit) ───────────────────────────────────────────

@pytest.fixture(scope="module")
def cost_cfg(project_root):
    return CostConfig.load(project_root / "config" / "costs.yaml")


def _one_trade(px_entry=100.0, px_exit=103.0, stop=99.0, side=1):
    bars = flat(3, px_entry) + [[px_entry, px_entry + 0.1, px_entry - 0.1, px_entry],
                                 [px_entry, max(px_entry, px_exit) + 0.5, min(px_entry, px_exit) - 0.5, px_exit]] + flat(3, px_exit)
    df = frame(bars)
    if side == 1:
        df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, stop]
    else:
        df.loc[df.index[2], ["short_entry", "short_stop"]] = [True, stop]
    # atr is 1.0 in these frames, so lift the ATR/percent caps that would skip a wide stop
    p = StrategyParams(**{**P, "target_r": 0.0, "max_bars": 2, "stop_atr_max": 1e9, "max_risk_pct": 100.0})
    trades = simulate(df, p, "X", "s", slippage_pct=0.0)
    assert len(trades) == 1
    return trades[0]


def test_stock_is_one_share(cost_cfg):
    spec = InstrumentSpec("RELIANCE", "Reliance", "STOCK", "RELIANCE", 1, 0.05, "equity_intraday")
    t = _one_trade()
    row = execute_trade(t, spec, CostModel(cost_cfg.product("equity_intraday")), BacktestCfg(), 0.0)
    assert row["qty"] == 1
    assert row["gross_pnl"] == pytest.approx((t.avg_exit - 100.0), abs=0.01)
    assert row["costs"] > 0 and row["net_pnl"] < row["gross_pnl"]


def test_futures_is_one_lot_and_short_profits_on_fall(cost_cfg):
    spec = InstrumentSpec("BANKNIFTY_FUT", "BN fut", "FUTURES", "BANKNIFTY", 35, 0.05, "index_futures")
    t = _one_trade(px_entry=100.0, px_exit=97.0, stop=101.0, side=-1)
    row = execute_trade(t, spec, CostModel(cost_cfg.product("index_futures")), BacktestCfg(), 0.0)
    assert row["qty"] == 35 and row["side"] == "SHORT"
    assert row["gross_pnl"] == pytest.approx((100.0 - t.avg_exit) * 35, abs=0.05)
    assert row["risk_inr"] == pytest.approx(1.0 * 35)


def test_currency_lot_and_price_per(cost_cfg):
    spec = InstrumentSpec("JPYINR", "JPY/INR", "CURRENCY", "JPYINR", 100000, 0.0025, "currency_futures",
                          price_per=100)
    t = _one_trade(px_entry=56.0, px_exit=56.5, stop=55.7)
    row = execute_trade(t, spec, CostModel(cost_cfg.product("currency_futures")), BacktestCfg(), 0.0)
    assert row["gross_pnl"] == pytest.approx((t.avg_exit - 56.0) * 100000 / 100, abs=0.05)


def test_option_buy_ce_on_long_and_pe_on_short(cost_cfg):
    spec = InstrumentSpec("BANKNIFTY_OPT", "BN opt", "OPTIONS", "BANKNIFTY", 35, 0.05, "index_options",
                          strike_step=100)
    bt = BacktestCfg(option_iv_source="constant", option_iv_constant=0.15)
    pricer = OptionPricer(bt, None)
    t = _one_trade(px_entry=50000.0, px_exit=50400.0, stop=49800.0)
    row = execute_trade(t, spec, CostModel(cost_cfg.product("index_options")), bt, 0.0, pricer)
    d = row["detail"]
    assert d["opt_type"] == "CE" and 0.55 <= d["delta_entry"] <= 0.75
    assert d["strike"] < 50000 and d["strike"] % 100 == 0
    assert row["net_pnl"] > 0 and row["qty"] == 35
    t2 = _one_trade(px_entry=50000.0, px_exit=49600.0, stop=50200.0, side=-1)
    row2 = execute_trade(t2, spec, CostModel(cost_cfg.product("index_options")), bt, 0.0, pricer)
    assert row2["detail"]["opt_type"] == "PE" and row2["detail"]["strike"] > 50000
    assert row2["net_pnl"] > 0


def test_option_loss_bounded_by_premium_and_stop_in_rupees(cost_cfg):
    spec = InstrumentSpec("NIFTY_OPT", "N opt", "OPTIONS", "NIFTY", 75, 0.05, "index_options", strike_step=50)
    bt = BacktestCfg(option_iv_source="constant", option_iv_constant=0.12)
    pricer = OptionPricer(bt, None)
    t = _one_trade(px_entry=25000.0, px_exit=24850.0, stop=24900.0)
    row = execute_trade(t, spec, CostModel(cost_cfg.product("index_options")), bt, 0.0, pricer)
    assert row["net_pnl"] < 0
    assert abs(row["net_pnl"]) <= row["detail"]["premium_entry"] * 75 + row["costs"]
    assert row["risk_inr"] > 0


def test_monthly_expiry_and_strike_grid():
    # Tuesday-anchored monthly expiry, rolled when inside the min-days window
    e = monthly_expiry(pd.Timestamp("2026-10-20").date(), weekday=1, min_days=3)
    assert e.weekday() == 1 and e >= pd.Timestamp("2026-10-23").date()
    k = strike_for_delta(50000.0, 20 / 365, 0.15, "CE", 0.65, 100)
    assert k % 100 == 0 and k < 50000


def test_thirty_percent_rule_math():
    df = pd.DataFrame({"net_pnl": [3000, -1000, -1000, -1000, 3000, -1000, -1000, 3000, -1000, -1000]})
    r = thirty_percent_rule(df)
    assert r["payoff"] == pytest.approx(3.0)
    assert r["breakeven_win_rate"] == pytest.approx(25.0)
    assert r["pf_at_30pct_win_rate"] == pytest.approx(0.3 * 3000 / (0.7 * 1000), abs=0.01)
