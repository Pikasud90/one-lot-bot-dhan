"""US-097 point 4: an option alert names the spread of its contract from a quote at hand.

With a quote of the alerted contract at most 60 seconds old (the fill-cost recorder's, or the
option chain the economics read) the alert carries `spread_pct`, half the bid-ask spread in %
of the mid, and from 1% on the line "Wide spread now: about 1.8% a side" in its text (the card
and the computer show the text) and in the Telegram message. A stale or missing quote gives no
key and no line. Raising the alert never waits for a quote. The alert's prices do not change.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading
import time

import pandas as pd
import pytest
from desk_harness import ShutClock, new_desk, small_cfg, synthetic

from onelot.broker.base import ChainRow, ChainSnapshot, QuoteSnapshot
from onelot.clock import IST
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.desk import Desk
from onelot.desk.fillcost import FillCostRecorder
from onelot.desk.tracker import Verdict
from onelot.notify.telegram import format_alert
from onelot.store import init_database

THU = dt.date(2026, 10, 29)
SIGNAL_BAR = dt.datetime.combine(THU, dt.time(9, 55), IST)
NOW = SIGNAL_BAR + dt.timedelta(minutes=10, seconds=30)
SID = "70002"


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def chain(bid: float = 99.9, ask: float = 100.1) -> ChainSnapshot:
    rows = [
        ChainRow(44800.0, "CE", "70001", ltp=300.0, bid=299.0, ask=301.0, delta=0.82),
        ChainRow(45000.0, "CE", SID, ltp=100.0, bid=bid, ask=ask, delta=0.66),
        ChainRow(45200.0, "CE", "70003", ltp=50.0, bid=49.5, ask=50.5, delta=0.51),
    ]
    return ChainSnapshot(NOW, "BANKNIFTY", "2026-11-25", 45000.0, rows)


def desk_for(cfg, db, monkeypatch, snap: ChainSnapshot | None, cache_chain: bool) -> Desk:
    slot = StrategySlotCfg("ema_cross", "10m")
    indices = (DeskInstrumentCfg("BANKNIFTY", "BANKNIFTY_OPT", "INDEX", (slot,)),)
    desk_cfg = dataclasses.replace(small_cfg(indices=indices, stocks=()))
    desk, notifier, _ = new_desk(cfg, db, desk_cfg=desk_cfg, clock=ShutClock())
    desk.notifier_log = notifier  # type: ignore[attr-defined]
    desk.source.served_by["BANKNIFTY"] = "dhan"
    t = desk.trackers["BANKNIFTY"]
    t.bars = synthetic(days=6, freq_min=2, base=45000.0, seed=11, end=pd.Timestamp(NOW))

    def live_chain(tracker, now):
        if snap is not None and cache_chain:
            desk._chain_cache[tracker.cfg.key] = (now, snap)
        return snap

    monkeypatch.setattr(desk, "_live_chain", live_chain)
    return desk


def raise_alert(desk: Desk) -> dict:
    t = desk.trackers["BANKNIFTY"]
    close = float(t.bars["close"].iloc[-1])
    v = Verdict("long", "test", SIGNAL_BAR, close * 0.99, close, 60.0, ["why"], True)
    before = set(desk.signals)
    desk._raise_signal(t, t.slots[0], v, NOW)
    (sid,) = set(desk.signals) - before
    return desk.signals[sid]


def test_a_fresh_recorder_quote_puts_the_spread_and_the_line_on_the_alert(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(), cache_chain=False)
    desk.fillcost._recent[SID] = (NOW - dt.timedelta(seconds=20), 100.0, 103.6)
    sig = raise_alert(desk)
    assert sig["spread_pct"] == pytest.approx(1.77, abs=0.01)  # 1.8 / 101.8
    assert "Wide spread now: about 1.8% a side" in sig["text"]
    sent = desk.notifier_log.sent[-1]  # type: ignore[attr-defined]
    assert "Wide spread now: about 1.8% a side" in sent.body
    # the alert's prices are the chain's, as before: the quote only adds the line
    assert sig["economics"]["entry_price"] == pytest.approx(100.1)


def test_a_fresh_chain_quote_counts_too_and_under_1_percent_there_is_no_line(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(99.5, 100.5), cache_chain=True)
    sig = raise_alert(desk)
    assert sig["spread_pct"] == pytest.approx(0.5)
    assert "Wide spread" not in sig["text"]


def test_a_wide_chain_quote_gives_the_line(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(96.0, 104.0), cache_chain=True)
    sig = raise_alert(desk)
    assert sig["spread_pct"] == pytest.approx(4.0)
    assert "Wide spread now: about 4.0% a side" in sig["text"]


def test_a_stale_quote_gives_no_key_and_no_line(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(96.0, 104.0), cache_chain=False)
    desk.fillcost._recent[SID] = (NOW - dt.timedelta(seconds=61), 96.0, 104.0)
    desk._chain_cache["BANKNIFTY"] = (NOW - dt.timedelta(seconds=61), chain(96.0, 104.0))
    sig = raise_alert(desk)
    assert "spread_pct" not in sig and "Wide spread" not in sig["text"]


def test_no_quote_gives_no_key_and_a_synthetic_option_none_either(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(), cache_chain=False)
    sig = raise_alert(desk)
    assert "spread_pct" not in sig and "Wide spread" not in sig["text"]
    synthetic_desk = desk_for(cfg, db, monkeypatch, None, cache_chain=False)
    sig = raise_alert(synthetic_desk)
    assert sig["economics"]["synthetic"] is True and "spread_pct" not in sig


def test_a_one_sided_quote_gives_no_key(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(), cache_chain=False)
    desk.fillcost._recent[SID] = (NOW - dt.timedelta(seconds=5), None, 104.0)
    assert "spread_pct" not in raise_alert(desk)


def test_a_failing_spread_read_leaves_the_alert_as_before(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(), cache_chain=False)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(desk.fillcost, "recent_quote", broken)
    sig = raise_alert(desk)
    assert "spread_pct" not in sig and desk.hook_errors >= 1


class SlowAdapter:
    """A quote source that takes two seconds to answer."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def quote(self, contracts):
        self.started.set()
        time.sleep(2.0)
        return {
            str(c.security_id): QuoteSnapshot(str(c.security_id), NOW, ltp=100.0, bid=96.0,
                                              ask=104.0)
            for c in contracts
        }  # fmt: skip


def test_raising_the_alert_never_waits_for_a_quote(cfg, db, monkeypatch):
    desk = desk_for(cfg, db, monkeypatch, chain(), cache_chain=False)
    slow = SlowAdapter()
    recorder = FillCostRecorder(
        db.engine, lambda: slow, lambda: True, lambda now: False, lambda: [], lambda: [],
        clock=lambda: NOW,
    )  # fmt: skip
    recorder._recent[SID] = (NOW - dt.timedelta(seconds=30), 100.0, 103.6)
    recorder.on_alert({"id": "x", "instrument_key": "BANKNIFTY_OPT",
                       "economics": {"option": {"security_id": SID, "strike": 45000.0,
                                                "opt_type": "CE", "expiry": "2026-11-25"}}})  # fmt: skip
    desk.fillcost = recorder
    worker = threading.Thread(target=recorder.tick, args=(NOW,), daemon=True)
    worker.start()
    assert slow.started.wait(5)  # the recorder is inside its slow request now
    t0 = time.perf_counter()
    sig = raise_alert(desk)
    took = time.perf_counter() - t0
    worker.join(10)
    assert took < 1.0, f"the alert waited {took:.2f} s"
    assert sig["spread_pct"] == pytest.approx(1.77, abs=0.01)  # the quote at hand, not the slow one


def test_the_recorder_keeps_the_last_quote_of_each_contract(db):
    from types import SimpleNamespace

    adapter = SimpleNamespace(
        quote=lambda cs: {
            str(c.security_id): QuoteSnapshot(str(c.security_id), NOW, 100.0, 99.0, 101.0)
            for c in cs
        }  # fmt: skip
    )
    recorder = FillCostRecorder(
        db.engine, lambda: adapter, lambda: True, lambda now: False, lambda: [], lambda: [],
        clock=lambda: NOW,
    )  # fmt: skip
    recorder.on_alert({"id": "x", "instrument_key": "BANKNIFTY_OPT",
                       "economics": {"option": {"security_id": SID, "strike": 45000.0,
                                                "opt_type": "CE", "expiry": "2026-11-25"}}})  # fmt: skip
    recorder.tick(NOW)
    assert recorder.recent_quote(SID, NOW + dt.timedelta(seconds=60)) == (NOW, 99.0, 101.0)
    assert recorder.recent_quote(SID, NOW + dt.timedelta(seconds=61)) is None
    assert recorder.recent_quote("nope", NOW) is None


# ── the phone ─────────────────────────────────────────────────────────────────
def _alert(**over):
    base = {
        "id": "abcdef123456", "created_at": NOW.isoformat(),
        "expires_at": (NOW + dt.timedelta(minutes=10)).isoformat(), "series_key": "BANKNIFTY",
        "trading_symbol": "BANKNIFTY", "label": "NIFTY BANK", "kind": "INDEX", "side": "BUY_CE",
        "horizon": "intraday", "strategy": "microtrend", "timeframe": "3m",
        "confidence": {"score": 50, "label": "medium"}, "data": {"delayed": False},
        "economics": {"qty": 30, "entry_price": 100.1, "stop_price": 80.0, "target_price": 150.0,
                      "underlying_stop": 44800.0, "underlying_target": 45600.0,
                      "synthetic": False, "option": {"strike": 45000.0, "expiry": "2026-11-25",
                                                     "lot": 30}},
    }  # fmt: skip
    base.update(over)
    return base


def test_the_telegram_text_has_the_line_from_1_percent_and_not_below():
    wide = format_alert(_alert(spread_pct=1.77), NOW)
    assert "Wide spread now: about 1.8% a side" in wide
    assert "Wide spread" not in format_alert(_alert(spread_pct=0.99), NOW)
    assert "Wide spread" not in format_alert(_alert(), NOW)
    assert "Wide spread" not in format_alert(_alert(spread_pct="garbage"), NOW)
