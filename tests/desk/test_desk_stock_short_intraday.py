"""ADR-001 amendment 8: a stock short is intraday only.

The broker squares an intraday short off at the end of the session, so the Desk must never
carry one overnight: a stock SELL alert, the position taken from it and its manage block are
intraday whatever `stock_horizon` says, and the position gets the same-day square-off advice.
A stock BUY keeps `stock_horizon` (swing by default) and is not squared off at the close.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_desk import FakeNotifier, FakeSource, OpenClock

from onelot.clock import IST, now_ist
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.tracker import Verdict
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def _desk(cfg, db) -> Desk:
    slot = (StrategySlotCfg("ema_cross", "10m"),)
    desk_cfg = DeskConfig(
        poll_seconds=60,
        history_days=10,
        source="yahoo",
        signal_valid_minutes=10,
        stock_horizon="swing",
        context=(),
        indices=(),
        stocks=(DeskInstrumentCfg("SBIN", "SBIN", "STOCK", slot),),
    )
    repo = Repo(db)
    governor = RiskGovernor(
        cfg.risk, cfg.account.capital_inr, cfg.mode, now_ist().date().isoformat(), repo
    )
    desk = Desk(db.engine, cfg, FakeNotifier(), governor, OpenClock(), repo=repo, desk_cfg=desk_cfg)
    desk.source = FakeSource()
    desk.fine_minutes = 2
    desk.poll()
    return desk


def _raise(desk: Desk, direction: str) -> dict:
    """A fresh `direction` verdict on SBIN's last bar, stop 1 % beyond the close."""
    t = desk.trackers["SBIN"]
    close = float(t.bars["close"].iloc[-1])
    stop = close * (0.99 if direction == "long" else 1.01)
    verdict = Verdict(
        direction, "test", t.bars.index[-1].to_pydatetime(), stop, close, 60.0, [], True
    )
    desk._raise_signal(t, t.slots[0], verdict, now_ist())
    return next(iter(desk.signals.values()))


def _advice_after_the_close(desk: Desk, pid: str) -> str | None:
    """Manage the position at 15:20 IST today, the price unchanged (no stop, no target) and
    the strategy silent, so only the horizon decides whether there is advice."""
    desk.trackers["SBIN"].slots[0].exit_advice = lambda side: None  # type: ignore[method-assign]
    late = dt.datetime.combine(now_ist().date(), dt.time(15, 20), tzinfo=IST)
    desk._manage_positions(late, OpenClock().current_window(late))
    return desk.positions[pid]["exit_advice"]


def test_a_stock_sell_alert_is_intraday_even_when_stocks_default_to_swing(cfg, db):
    desk = _desk(cfg, db)

    sig = _raise(desk, "short")

    assert sig["side"] == "SELL"
    assert sig["horizon"] == "intraday" and sig["manage"]["horizon"] == "intraday"
    pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
    assert desk.positions[pid]["horizon"] == "intraday"


def test_a_stock_short_gets_the_square_off_advice_the_same_day(cfg, db):
    desk = _desk(cfg, db)
    sig = _raise(desk, "short")
    pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]

    assert _advice_after_the_close(desk, pid) == "SQUARE_OFF"


def test_a_stock_buy_keeps_the_configured_stock_horizon_and_is_carried(cfg, db):
    desk = _desk(cfg, db)
    sig = _raise(desk, "long")

    assert sig["side"] == "BUY"
    assert sig["horizon"] == "swing" and sig["manage"]["horizon"] == "swing"
    pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
    assert desk.positions[pid]["horizon"] == "swing"
    assert _advice_after_the_close(desk, pid) is None
