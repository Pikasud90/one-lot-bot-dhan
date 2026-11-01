"""US-004: the Desk payload names the symbol you type into Dhan (`trading_symbol`).

The watchlist key MM is Mahindra & Mahindra, whose NSE symbol is M&M (config/universe.yaml);
a symbol without an override (SBIN) is its own key. The alert carries it, the position taken
from that alert keeps it, so the phone message can show what to type.
"""

from __future__ import annotations

import pytest
from test_desk import FakeNotifier, FakeSource, OpenClock

from onelot.clock import now_ist
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
        context=(),
        indices=(),
        stocks=(
            DeskInstrumentCfg("MM", "MM", "STOCK", slot),
            DeskInstrumentCfg("SBIN", "SBIN", "STOCK", slot),
        ),
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


def _raise(desk: Desk, key: str) -> dict:
    t = desk.trackers[key]
    close = float(t.bars["close"].iloc[-1])
    verdict = Verdict(
        "long", "test", t.bars.index[-1].to_pydatetime(), close * 0.99, close, 60.0, [], True
    )
    desk._raise_signal(t, t.slots[0], verdict, now_ist())
    return next(s for s in desk.signals.values() if s["series_key"] == key)


def test_the_alert_and_its_position_carry_the_dhan_trading_symbol(cfg, db):
    desk = _desk(cfg, db)

    mm = _raise(desk, "MM")
    sbin = _raise(desk, "SBIN")

    assert (mm["series_key"], mm["trading_symbol"]) == ("MM", "M&M")
    assert sbin["trading_symbol"] == "SBIN", "no override: the key is the symbol"
    pid = desk.acknowledge(mm["id"], "TAKE")["position_id"]
    assert desk.positions[pid]["trading_symbol"] == "M&M"
