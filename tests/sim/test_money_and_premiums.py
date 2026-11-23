"""US-075 AC8 (money to the paisa, by hand) and AC9 (a real premium from the option store
when it has the bar, the model when it has not)."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from onelot.analytics.blackscholes import price as b76
from onelot.clock import IST
from onelot.data.expired_options import Block, OptionBarStore, RollingKey
from onelot.lab.real_options import atm_rel
from onelot.settings import current_settings
from onelot.sim.manager import default_deps

from .conftest import MemBars, flat_minutes, make_manager, set_bar, trading_days

DAYS = trading_days("2025-01-06", 15)
D = DAYS[-2]
BT = current_settings().backtest


def T(hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{D} {hhmm}", tz=IST)


def ceil_tick(x: float) -> float:
    return round(math.ceil(x / 0.05 - 1e-9) * 0.05, 2)


def floor_tick(x: float) -> float:
    return round(math.floor(x / 0.05 + 1e-9) * 0.05, 2)


def gst(*parts: float) -> float:
    return 0.18 * sum(parts)


def equity_intraday_leg(turnover: float, side: str) -> float:
    """config/costs.yaml equity_intraday, written out."""
    brokerage = min(20.0, 0.0003 * turnover, 20.0)
    stt = 0.00025 * turnover if side == "sell" else 0.0
    exch, sebi, ipft = 0.0000297 * turnover, 0.000001 * turnover, 0.000001 * turnover
    stamp = 0.00003 * turnover if side == "buy" else 0.0
    return brokerage + stt + exch + sebi + ipft + stamp + gst(brokerage, exch, sebi, ipft)


def index_options_leg(turnover: float, side: str) -> float:
    """config/costs.yaml index_options, written out."""
    brokerage = 20.0
    stt = 0.0010 * turnover if side == "sell" else 0.0
    exch, sebi, ipft = 0.0003503 * turnover, 0.000001 * turnover, 0.000005 * turnover
    stamp = 0.00003 * turnover if side == "buy" else 0.0
    return brokerage + stt + exch + sebi + ipft + stamp + gst(brokerage, exch, sebi, ipft)


def session(db_engine, tmp_path, force, instrument, rel, nifty, deps=None):
    force(long=[f"{D} 10:10"])
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path, deps=deps)
    out = m.create(
        {"instrument": instrument, "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
         "start": T("10:00").isoformat()}
    )  # fmt: skip
    s = m.get(out["session"]["id"])
    while pd.Timestamp(s.cursor) < T("10:14"):
        s.advance(1)
    assert s.alert is not None
    return m, s


def test_a_stock_trade_to_the_paisa(db_engine, tmp_path, force):
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    set_bar(rel, f"{D} 10:17", 1000.0, 1010.0, 999.5, 1008.0)  # reaches the target 1009.30
    _, s = session(db_engine, tmp_path, force, "RELIANCE", rel, nifty)
    s.take(s.alert["id"])
    for _ in range(3):
        s.advance(1)
    t = s.closed[0]
    entry = 1000.0 * 1.0003  # the open with 0.03 % slippage
    exit_ = entry + 3 * 3.0  # the first target, three risks of 1.5 x ATR 2.00, at its level
    gross = (exit_ - entry) * 1
    charges = equity_intraday_leg(entry, "buy") + equity_intraday_leg(exit_, "sell")
    net = round(gross, 2) - round(charges, 2)
    assert t["exit_reason"] == "TARGET"
    assert (t["entry_price"], t["exit_price"]) == (round(entry, 2), round(exit_, 2))
    assert t["gross"] == round(gross, 2)
    assert t["charges"] == round(charges, 2)
    assert t["net"] == round(net, 2)
    assert t["r_multiple"] == round(net / 3.0, 4)


def _years(at: pd.Timestamp, expiry: dt.date) -> float:
    exp = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=IST)
    return max((exp - at.to_pydatetime()).total_seconds(), 60.0) / (365.0 * 86400)


def test_an_option_trade_on_model_premiums_to_the_paisa(db_engine, tmp_path, force):
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    set_bar(nifty, f"{D} 10:17", 25000.0, 25100.0, 24995.0, 25080.0)  # reaches 25090
    _, s = session(db_engine, tmp_path, force, "NIFTY", rel, nifty)
    s.take(s.alert["id"])
    c = s.trade.contract
    for _ in range(3):
        s.advance(1)
    t = s.closed[0]
    iv, r, lot = BT.option_iv_constant, BT.risk_free_rate, 65
    half, slip = BT.option_spread_pct, BT.option_slippage_ticks * 0.05
    k = c.strike
    raw_in = b76(25000.0, k, _years(T("10:15"), c.expiry), iv, r, "CE")
    entry = ceil_tick(raw_in * (1 + half) + slip)
    raw_out = b76(25090.0, k, _years(T("10:17"), c.expiry), iv, r, "CE")  # target 3 x 30
    exit_ = max(floor_tick(raw_out * (1 - half) - slip), 0.05)
    raw_stop = b76(24970.0, k, _years(T("10:15"), c.expiry), iv, r, "CE")
    stop_fill = max(floor_tick(raw_stop * (1 - half) - slip), 0.05)
    gross = (exit_ - entry) * lot
    charges = index_options_leg(entry * lot, "buy") + index_options_leg(exit_ * lot, "sell")
    net = round(gross, 2) - round(charges, 2)
    risk_inr = (entry - stop_fill) * lot
    assert t["exit_reason"] == "TARGET" and t["qty"] == lot
    assert t["traded"]["source"] == "model"
    assert (t["entry"], t["exit"]) == (25000.0, 25090.0)  # managed: the index
    assert (t["entry_price"], t["exit_price"]) == (entry, exit_)
    assert t["gross"] == round(gross, 2)
    assert t["charges"] == round(charges, 2)
    assert t["net"] == round(net, 2)
    assert t["r_multiple"] == round(net / risk_inr, 4)


def _block(c, minute: pd.Timestamp, spot: float, o: float) -> Block:
    rel = atm_rel(c.strike, spot, 50.0)
    epoch = int(minute.value // 1_000_000_000)
    one = {
        "ts": np.array([epoch], dtype="int64"),
        "open": np.array([o]),
        "high": np.array([o + 5]),
        "low": np.array([o - 5]),
        "close": np.array([o + 1]),
        "iv": np.array([15.0]),
        "volume": np.array([100.0]),
        "oi": np.array([1000.0]),
        "strike": np.array([c.strike]),
        "spot": np.array([spot]),
    }
    return Block(RollingKey("NIFTY", 1, c.opt, rel), minute.date(), True, one)


@pytest.mark.parametrize("stored", [True, False])
def test_the_fill_uses_the_real_premium_when_the_store_has_it(db_engine, tmp_path, force, stored):
    path = tmp_path / "expired_options.db"
    writer = OptionBarStore(path)
    deps = default_deps(None, option_store_path=path)
    assert deps.option_store is not None
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    _, s = session(db_engine, tmp_path, force, "NIFTY", rel, nifty, deps=deps)
    s.take(s.alert["id"])
    if stored:
        writer.put(_block(s.trade.contract, T("10:15"), 25000.0, 333.0))
    r = s.advance(1)
    t = r["trade"]
    if stored:
        assert t["source"] == "real"
        assert t["entry_price"] == ceil_tick(333.0 * (1 + BT.option_spread_pct) + 0.10)
        s.close()
        s.advance(1)  # 10:16: no real bar there, so the model prices the exit
        closed = s.closed[0]
        assert closed["traded"]["source"] == "real"
        assert "model" in closed["traded"]["note"]
    else:
        assert t["source"] == "model"
    writer.close()


@pytest.mark.parametrize(
    ("bar_at", "source"), [("10:15", "real"), ("10:16", "model"), ("10:18", "model")]
)
def test_a_fill_never_uses_a_later_minutes_premium(db_engine, tmp_path, force, bar_at, source):
    """US-083: the strike's bar of the SAME minute or the model, marked `model`: a bar of a
    later minute (not yet seen) is never used."""
    path = tmp_path / "expired_options.db"
    writer = OptionBarStore(path)
    deps = default_deps(None, option_store_path=path)
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    m, s = session(db_engine, tmp_path, force, "NIFTY", rel, nifty, deps=deps)
    s.take(s.alert["id"])
    blk = _block(s.trade.contract, T(bar_at), 25000.0, 333.0)
    for dr in (-1, 0, 1):  # the store holds the neighbouring rolling series too
        writer.put(
            Block(RollingKey("NIFTY", 1, blk.key.opt, blk.key.rel + dr), blk.day, True, blk.data)
        )
    t = s.advance(1)["trade"]  # the fill minute is 10:15
    assert t["source"] == source
    if source == "real":
        assert t["entry_price"] == ceil_tick(333.0 * (1 + BT.option_spread_pct) + 0.10)
    s.close()
    s.advance(1)
    detail = m.store.trades(session_id=s.setup.id, status="CLOSED")[0]["detail"]
    assert "entry_wait_min" not in detail and "exit_wait_min" not in detail
    writer.close()
