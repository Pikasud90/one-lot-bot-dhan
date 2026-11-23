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


# ── US-083: a real premium calibrates the model (one curve per trade) ─────────
def _put_bars(writer, c, minutes: list[pd.Timestamp], spot: float, px: float) -> None:
    rel = atm_rel(c.strike, spot, 50.0)
    n = len(minutes)
    one = {
        "ts": np.array([int(m.value // 1_000_000_000) for m in minutes], dtype="int64"),
        "open": np.full(n, px), "high": np.full(n, px), "low": np.full(n, px),
        "close": np.full(n, px), "iv": np.full(n, 15.0), "volume": np.full(n, 100.0),
        "oi": np.full(n, 1000.0), "strike": np.full(n, c.strike), "spot": np.full(n, spot),
    }  # fmt: skip
    for dr in (-1, 0, 1):
        writer.put(Block(RollingKey("NIFTY", 1, c.opt, rel + dr), minutes[0].date(), True, one))


def _t(c, ts: pd.Timestamp) -> float:
    from onelot.lab.execution import expiry_datetime, year_frac

    return year_frac(ts.to_pydatetime(), expiry_datetime(c.expiry))


def test_a_real_premium_calibrates_the_stop_target_risk_and_the_modelled_exit(
    db_engine, tmp_path, force
):
    from onelot.analytics.blackscholes import implied_vol
    from onelot.analytics.blackscholes import price as bs_price

    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    # first a session on the model alone: the contract and the VIX-model premium at the alert
    _, s0 = session(db_engine, tmp_path, force, "NIFTY", rel, nifty)
    from onelot.sim.pricing import Contract

    a0 = s0.alert
    c = Contract.from_dict(a0["_contract"])
    when = T("10:15")
    model_alert = s0.pricer.model_premium(c, 25000.0, when)
    # now the store says the market pays 6% LESS than that model
    path = tmp_path / "expired_options.db"
    writer = OptionBarStore(path)
    deps = default_deps(None, option_store_path=path)
    real_alert = round(model_alert * 0.94, 2)
    _put_bars(writer, c, [T("10:14"), T("10:15")], 25000.0, real_alert)  # alert and fill minutes
    _, s = session(db_engine, tmp_path, force, "NIFTY", rel, nifty, deps=deps)
    a = s.alert
    assert a["traded"]["source"] == "real" and a["traded"]["iv_source"] == "implied"
    r = BT.risk_free_rate
    iv = implied_vol(real_alert, 25000.0, c.strike, _t(c, when), r, c.opt)
    entry = ceil_tick(real_alert * (1 + BT.option_spread_pct) + 0.10)
    stop_hand = floor_tick(
        bs_price(a["stop"], c.strike, _t(c, when), iv, r, c.opt) * (1 - BT.option_spread_pct) - 0.10
    )
    risk_hand = (entry - stop_hand) * a["qty"]
    assert a["risk_inr"] == pytest.approx(risk_hand, rel=0.02)
    assert a["traded"]["stop_price"] == pytest.approx(stop_hand, rel=0.02)
    assert a["risk_inr"] != s0.alert["risk_inr"]  # not the VIX-model risk
    # take, fill at the real open, then close: no real bar at 10:16, so the model prices the exit
    s.take(a["id"])
    s.advance(1)
    t = s.trade
    assert t.iv_source == "implied" and t.iv == pytest.approx(iv, rel=1e-3)
    s.close()
    s.advance(1)
    closed = s.closed[0]
    exit_hand = floor_tick(
        bs_price(25000.0, c.strike, _t(c, T("10:16")), iv, r, c.opt) * (1 - BT.option_spread_pct)
        - 0.10
    )
    assert closed["exit_price"] == pytest.approx(exit_hand, abs=0.1)
    detail = s.store.trades(session_id=s.setup.id, status="CLOSED")[0]["detail"]
    assert detail["iv_source"] == "implied" and detail["iv_used"] == pytest.approx(iv, rel=1e-3)
    assert detail["exit_source"] == "model"
    stop_fill = floor_tick(
        bs_price(t.plan.stop, c.strike, _t(c, T("10:15")), iv, r, c.opt)
        * (1 - BT.option_spread_pct)
        - 0.10
    )
    assert detail["risk_inr"] == pytest.approx(
        (closed["entry_price"] - stop_fill) * a["qty"], rel=0.02
    )
    risk = detail["risk_inr"]
    assert closed["r_multiple"] == pytest.approx(closed["net"] / risk, abs=1e-3)
    writer.close()


def test_with_no_real_premium_the_alert_has_no_implied_volatility(db_engine, tmp_path, force):
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    m, s = session(db_engine, tmp_path, force, "NIFTY", rel, nifty)
    assert "iv_source" not in s.alert["traded"] and s.alert["_iv"] is None
    s.take(s.alert["id"])
    s.advance(1)
    assert s.trade.iv is None and s.trade.iv_source == "vix"
    s.close()
    s.advance(1)
    d = m.store.trades(session_id=s.setup.id, status="CLOSED")[0]["detail"]
    assert d["iv_source"] == "vix" and d["iv_used"] > 0
