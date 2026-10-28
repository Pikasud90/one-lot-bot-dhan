"""Live Desk: config, one-unit economics, tracker evaluation and the signal -> position flow.

Everything runs on synthetic bars through a fake bar source; no network, no broker.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST, now_ist
from onelot.config import AppConfig
from onelot.desk import Desk, load_desk_config
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.economics import alert_text, index_option_economics, stock_economics
from onelot.desk.tracker import InstrumentTracker
from onelot.notify.base import Notification
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo


def synthetic(days: int = 12, freq_min: int = 2, base: float = 1000.0, drift: float = 0.0, seed: int = 3,
              end: pd.Timestamp | None = None) -> pd.DataFrame:
    """`days` NSE sessions of 2-minute bars (09:15..15:29, like Yahoo), ending at the latest
    weekday on or before `end`; today's bars are cut at `end` so only closed bars exist."""
    rng = np.random.default_rng(seed)
    end = end or pd.Timestamp.now(tz=IST).floor("2min")
    day = end.normalize()
    sessions = []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx = []
    for d in sorted(sessions):
        idx.extend(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=188, freq=f"{freq_min}min"))
    idx = pd.DatetimeIndex([t for t in idx if t + pd.Timedelta(minutes=freq_min) <= end])
    n = len(idx)
    close = base * np.exp(np.cumsum(rng.normal(drift, 0.0012, n)))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, 0.0012, n)) * close
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread,
                         "close": close, "volume": rng.integers(1000, 5000, n).astype(float)}, index=idx)


class FakeSource:
    name, delayed, resolution = "fake", False, "2m"
    primary = None
    last_error = None

    def __init__(self):
        self.served_by = {}
        self.frames = {}

    def fetch(self, specs, days, since=None):
        return {k: self.frames.get(k, synthetic(seed=hash(k) % 1000)) for k in specs}


class FakeNotifier:
    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {"fake": True}

    def info(self, title, body="", dedup_key=None):
        return self.send(Notification(title=title, body=body))


class OpenClock:
    """A session window that is always open with entries allowed."""

    class W:
        def __init__(self, now):
            self.open_at = now - dt.timedelta(hours=1)
            self.close_at = now + dt.timedelta(hours=3)
            self.mandatory_squareoff_at = now + dt.timedelta(hours=2)

        def is_open(self, ts):
            return True

        def entries_allowed(self, ts):
            return True

        def past_squareoff(self, ts):
            return False

    def current_window(self, ts=None):
        return OpenClock.W(ts or now_ist())


@pytest.fixture
def cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def small_cfg() -> DeskConfig:
    return DeskConfig(poll_seconds=60, history_days=10, source="yahoo", signal_valid_minutes=10, context=(),
                      indices=(DeskInstrumentCfg("BANKNIFTY", "BANKNIFTY_OPT", "INDEX", (StrategySlotCfg("ema_cross", "10m"),)),),
                      stocks=(DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (StrategySlotCfg("ema_cross", "10m"),
                                                                        StrategySlotCfg("supertrend_vol", "6m"))),))


class TestConfig:
    def test_desk_yaml_loads_the_watchlist(self):
        c = load_desk_config()
        assert [i.key for i in c.indices] == ["BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
        assert len(c.stocks) == 10 and {s.key for s in c.stocks} >= {"RELIANCE", "MM", "TMPV", "HEROMOTOCO"}
        assert all(len(i.strategies) >= 2 for i in c.instruments)
        assert any(x.manual for x in c.context)             # GIFT NIFTY is a manual slot


class TestEconomics:
    def test_stock_is_one_share_with_charges_in_the_risk(self):
        from onelot.data.universe import load_specs

        _, inst = load_specs()
        e = stock_economics(inst["SBIN"], "long", 950.0, 942.0, 2.0)
        assert e.qty == 1 and e.side == "BUY" and e.target_price == 966.0
        assert e.risk_inr > 8.0 and e.costs_inr > 0 and 0 < e.rr_net < 2.0
        s = stock_economics(inst["SBIN"], "short", 950.0, 958.0, 0.0)
        assert s.side == "SELL" and s.target_price is None and s.reward_inr is None
        txt = alert_text("State Bank of India", "SBIN", e, "ema_cross", "15m", now_ist(), "15:15", ["EMA 9 > 21"])
        assert "BUY 1 share" in txt and "Stop ₹942.00" in txt and "Square off by 15:15" in txt

    def test_index_is_one_option_lot_priced_synthetically(self):
        from onelot.data.universe import load_specs

        _, inst = load_specs()
        e = index_option_economics(inst["BANKNIFTY_OPT"], "long", 54000.0, 53850.0, 2.0, now_ist(), 16.0)
        assert e.side == "BUY_CE" and e.qty == inst["BANKNIFTY_OPT"].lot_size and e.synthetic
        assert e.option["opt_type"] == "CE" and 0.5 < e.option["delta"] < 0.8 and e.option["strike"] % 100 == 0
        assert e.entry_price > e.stop_price and e.target_price > e.entry_price
        assert e.risk_inr > 0 and e.reward_inr > 0 and e.outlay == pytest.approx(e.entry_price * e.qty, rel=1e-6)
        p = index_option_economics(inst["BANKNIFTY_OPT"], "short", 54000.0, 54150.0, 2.0, now_ist(), 16.0)
        assert p.side == "BUY_PE" and p.option["opt_type"] == "PE"
        txt = alert_text("NIFTY BANK", "BANKNIFTY", e, "ema_cross", "12m", None, "15:15", [])
        assert "BUY 1 lot" in txt and "CE" in txt and "SELL the same option" in txt


class TestTracker:
    def test_tracker_warms_up_then_evaluates_on_closed_bars(self):
        from onelot.data.universe import load_specs

        series, inst = load_specs()
        ic = DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (StrategySlotCfg("ema_cross", "10m"),))
        t = InstrumentTracker(ic, series["SBIN"], inst["SBIN"], 2)
        t.update_bars(synthetic(days=1, end=pd.Timestamp("2026-10-27 12:00", tz=IST)))   # ~80 x 2m bars -> 16 x 10m
        fired = t.evaluate(now_ist(), None, True)
        assert fired == [] and "warming" in t.slots[0].last_verdict.reason
        t.update_bars(synthetic(days=12))
        t.evaluate(now_ist(), None, True)
        v = t.slots[0].last_verdict
        assert v.bar_ts is not None and t.regime.get("regime") and t.slots[0].last_features.get("atr")
        d = t.as_dict(now_ist(), "fake", False)
        assert d["unit"] == "1 share" and d["health"]["bars"] > 1000 and d["strategies"][0]["timeframe"] == "10m"
        # the same bar is not re-evaluated
        again = t.evaluate(now_ist(), None, True)
        assert again == [] and t.slots[0].last_verdict.reason == "no new bar yet"


class TestDeskFlow:
    def _desk(self, cfg, db):
        repo = Repo(db)
        gov = RiskGovernor(cfg.risk, cfg.account.capital_inr, cfg.mode, now_ist().date().isoformat(), repo)
        notifier = FakeNotifier()
        d = Desk(db.engine, cfg, notifier, gov, OpenClock(), repo=repo, desk_cfg=small_cfg())
        d.source = FakeSource()
        d.fine_minutes = 2
        return d, notifier, gov

    def test_signal_then_take_manage_close(self, cfg, db, monkeypatch):
        d, notifier, gov = self._desk(cfg, db)
        d.poll()
        snap = d.snapshot()
        assert {r["key"] for r in snap["stocks"]} == {"SBIN"} and snap["indices"][0]["tracked"] == "BANKNIFTY_OPT"
        assert all(r["state"] == "TRACKING" for r in snap["stocks"] + snap["indices"])

        # force the SBIN ema_cross slot to fire a long on its last closed bar
        t = d.trackers["SBIN"]
        slot = t.slots[0]
        real_generate = slot.strategy.generate

        def fire(df, ctx=None):
            f = real_generate(df, ctx)
            f.loc[f.index[-1], "long_entry"] = True
            f.loc[f.index[-1], "long_stop"] = float(f["close"].iloc[-1]) * 0.99
            return f
        slot.strategy.generate = fire
        slot.last_bar_ts = None
        # cut the feed where a fine bar completes a 10-minute bar (bins start 09:15 + k*10)
        last_day = t.bars.index[-1].normalize()
        t.bars = t.bars[t.bars.index <= last_day + pd.Timedelta(hours=15, minutes=13)]
        end = t.bars.index[-1] + pd.Timedelta(minutes=2)
        monkeypatch.setattr("onelot.desk.tracker.MIN_BARS", 10)
        fired = t.evaluate(end, None, True)
        assert fired and fired[0][1].direction == "long"
        d._raise_signal(t, fired[0][0], fired[0][1], end)
        assert len(d.signals) == 1
        sig = next(iter(d.signals.values()))
        assert sig["side"] == "BUY" and sig["economics"]["qty"] == 1 and "BUY 1 share" in sig["text"]
        assert any(n.level.value == "SIGNAL" for n in notifier.sent)

        # second fire on the same instrument is suppressed while one is pending
        d._raise_signal(t, fired[0][0], fired[0][1], end)
        assert len(d.signals) == 1

        res = d.acknowledge(sig["id"], "TAKE", fill_price=sig["economics"]["entry_price"] + 0.5)
        assert res["accepted"] and len(d.positions) == 1 and not d.signals
        pid = res["position_id"]
        pos = d.positions[pid]
        assert pos["entry_price"] == pytest.approx(sig["economics"]["entry_price"] + 0.5)
        assert gov.open_positions == 1 and t.trades_today == 1

        # price runs to the target -> EXIT advice + alert
        t.bars.loc[t.bars.index[-1], "close"] = pos["target_underlying"] + 1
        d._manage_positions(now_ist(), OpenClock().current_window())
        assert d.positions[pid]["exit_advice"] == "TARGET"
        assert any(n.level.value == "EXIT" for n in notifier.sent)
        snap = d._rebuild_snapshot(now_ist(), OpenClock().current_window()) or d.snapshot()
        assert snap["stocks"][0]["state"] == "HOLD"

        out = d.close_position(pid, pos["target_underlying"], "TARGET")
        assert out["closed"] and out["net_pnl"] > 0 and not d.positions
        assert gov.open_positions == 0 and gov.realized_pnl == pytest.approx(out["net_pnl"], abs=0.01)
        j = d.journal()
        assert len(j) == 1 and j[0]["exit_reason"] == "TARGET" and j[0]["qty"] == 1

    def test_skip_and_expiry_and_recovery(self, cfg, db):
        d, notifier, gov = self._desk(cfg, db)
        d.poll()
        t = d.trackers["SBIN"]
        from onelot.desk.tracker import Verdict
        v = Verdict("short", "test", t.bars.index[-1].to_pydatetime(), float(t.bars["close"].iloc[-1]) * 1.01,
                    float(t.bars["close"].iloc[-1]), 55.0, ["test"], True)
        d._raise_signal(t, t.slots[0], v, now_ist())
        sid = next(iter(d.signals))
        assert d.acknowledge(sid, "SKIP")["accepted"] and not d.signals
        d._raise_signal(t, t.slots[0], v, now_ist() - dt.timedelta(minutes=30))
        d._expire_signals(now_ist())
        assert not d.signals and d.recent_signals[0]["status"] == "EXPIRED"
        # an open position survives a restart
        d._raise_signal(t, t.slots[0], v, now_ist())
        sid = next(iter(d.signals))
        d.acknowledge(sid, "TAKE")
        d2 = Desk(db.engine, cfg, notifier, gov, OpenClock(), repo=Repo(db), desk_cfg=small_cfg())
        assert len(d2.positions) == 1 and next(iter(d2.positions.values()))["side"] == "SELL"

    def test_risk_governor_blocks_but_still_alerts(self, cfg, db):
        d, notifier, gov = self._desk(cfg, db)
        d.poll()
        gov.manual_panic_halt()
        t = d.trackers["SBIN"]
        from onelot.desk.tracker import Verdict
        v = Verdict("long", "test", t.bars.index[-1].to_pydatetime(), float(t.bars["close"].iloc[-1]) * 0.99,
                    float(t.bars["close"].iloc[-1]), 60.0, [], True)
        d._raise_signal(t, t.slots[0], v, now_ist())
        sig = next(iter(d.signals.values()))
        assert sig["risk_allowed"] is False and "HALT" in sig["risk_reason"].upper()
        assert notifier.sent[-1].title.startswith("[BLOCKED by risk]")
