"""US-079 on the desk: an alert of a slot with `ratchet: true` and the position taken on it.

    _raise_signal       the first stop is 1.5 x ATR(14, Wilder) of 60m bars from the alert's
                        price, read on the last 60m bar closed by the signal; the target 3
                        risks away; `manage.ratchet` frozen; one line in the text says how
    _manage_positions   a taken position shows its paper trade's levels and stage, and one
                        notification per move says "raise your stop to X"

Through the REAL Desk and a REAL PaperBook (`paper.factory.build_paper_book`) on the test
database; the bars are the harness's random walk, set on the tracker by hand, with a
Thursday morning whose 10m signal bar 09:55 closes at 10:05.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np
import pandas as pd
import pytest
from desk_harness import ShutClock, new_desk, small_cfg, synthetic

from onelot.clock import IST
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.desk import Desk
from onelot.desk.tracker import Verdict
from onelot.manage import ratchet
from onelot.paper.factory import build_paper_book
from onelot.store import init_database

THU = dt.date(2026, 10, 29)
SIGNAL_BAR = dt.datetime.combine(THU, dt.time(9, 55), IST)
SIGNAL_CLOSE = SIGNAL_BAR + dt.timedelta(minutes=10)
NOW = SIGNAL_CLOSE + dt.timedelta(seconds=30)
RATCHET = StrategySlotCfg("ema_cross", "10m", {"ratchet": True})
PLAIN = StrategySlotCfg("ema_cross", "10m")


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def desk_with(cfg, db, slot: StrategySlotCfg, *, paper: bool = True) -> Desk:
    stocks = (DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (slot,)),)
    desk_cfg = dataclasses.replace(small_cfg(indices=(), stocks=stocks), stock_horizon="intraday")
    book = build_paper_book(cfg, db.engine, clock=lambda: NOW) if paper else None
    desk, notifier, _ = new_desk(cfg, db, desk_cfg=desk_cfg, clock=ShutClock(), paper=book)
    desk.notifier_log = notifier  # type: ignore[attr-defined]
    desk.source.served_by["SBIN"] = "dhan"  # a live grade: the book manages on wall time
    desk.trackers["SBIN"].bars = morning()
    return desk


def morning() -> pd.DataFrame:
    """Six sessions of 2-minute bars, the last one cut at 10:05 on THU (the signal's close)."""
    end = pd.Timestamp(SIGNAL_CLOSE)
    return synthetic(days=6, freq_min=2, base=800.0, seed=11, end=end)


def raise_alert(desk: Desk, direction: str = "long") -> dict:
    t = desk.trackers["SBIN"]
    close = float(t.bars["close"].iloc[-1])
    stop = close * (0.99 if direction == "long" else 1.01)  # the strategy's own stop
    v = Verdict(direction, "test", SIGNAL_BAR, stop, close, 60.0, ["why"], True)
    before = set(desk.signals)
    desk._raise_signal(t, t.slots[0], v, NOW)
    (sid,) = set(desk.signals) - before
    return desk.signals[sid]


def wilder_atr_60m(bars: pd.DataFrame, at: dt.datetime) -> float:
    """ATR(14, Wilder) of the 60m bins (from 09:15, cut at 15:30) closed by `at`, by hand."""
    local = bars.index.tz_convert(IST)
    minutes = local.hour * 60 + local.minute - (9 * 60 + 15)
    bins: dict[tuple[dt.date, int], list[tuple[float, float, float]]] = {}
    for day, m, h, low, c in zip(local.date, minutes, bars["high"], bars["low"], bars["close"],
                                 strict=True):  # fmt: skip
        bins.setdefault((day, m // 60), []).append((float(h), float(low), float(c)))
    rows = []
    for (day, k), g in bins.items():
        start = dt.datetime.combine(day, dt.time(9, 15), IST) + dt.timedelta(hours=k)
        end = min(start + dt.timedelta(hours=1), dt.datetime.combine(day, dt.time(15, 30), IST))
        rows.append((end, max(x[0] for x in g), min(x[1] for x in g), g[-1][2]))
    rows = [r for r in sorted(rows) if r[0] <= at]
    tr = [max(h - low, abs(h - pc), abs(low - pc))
          for (_, h, low, _), (_, _, _, pc) in zip(rows[1:], rows[:-1], strict=True)]  # fmt: skip
    atr = float(np.mean(tr[:14]))
    for x in tr[14:]:
        atr += (x - atr) / 14
    return atr


class TestTheAlert:
    def test_the_levels_are_the_rules(self, cfg, db) -> None:
        desk = desk_with(cfg, db, RATCHET)
        sig = raise_alert(desk)
        eco, block = sig["economics"], sig["manage"]["ratchet"]

        risk = 1.5 * wilder_atr_60m(desk.trackers["SBIN"].bars, SIGNAL_CLOSE)
        close = eco["underlying_entry"]
        assert block["risk"] == pytest.approx(risk, rel=1e-9, abs=1e-4)
        assert eco["underlying_stop"] == pytest.approx(close - risk, abs=1e-3)
        assert eco["underlying_target"] == pytest.approx(close + 3 * risk, abs=1e-3)
        assert (block["stop_atr"], block["atr_tf"], block["target_r"], block["extend"]) == (
            1.5,
            "60m",
            3.0,
            True,
        )
        assert block["cost"] == pytest.approx(eco["costs_inr"] / eco["qty"], abs=1e-4)
        assert sig["manage"]["target_r"] == 3.0 and sig["manage"]["trail_atr_mult"] == 0.0
        assert "line_trail" not in sig["manage"]
        assert (
            "Managed: stop above breakeven at 25% of the way, 35% locked at 50%, the target "
            "moves out from 80%." in sig["text"]
        )

    def test_bars_after_the_signal_change_nothing(self, cfg, db) -> None:
        desk = desk_with(cfg, db, RATCHET)
        t, slot = desk.trackers["SBIN"], desk.trackers["SBIN"].slots[0]
        v = Verdict("long", "test", SIGNAL_BAR, 790.0, 800.0, 60.0, [], True)
        rset = ratchet_settings(slot)
        cut = desk._ratchet_risk(t, slot, v, rset)
        walk = synthetic(days=2, freq_min=2, base=900.0, seed=5,
                         end=pd.Timestamp(SIGNAL_CLOSE + dt.timedelta(hours=3)))  # fmt: skip
        after = walk[walk.index >= pd.Timestamp(SIGNAL_CLOSE)]  # wild bars after the signal
        assert len(after) > 60
        t.bars = pd.concat([t.bars, after])
        t._fine_memo = None
        assert desk._ratchet_risk(t, slot, v, rset) == cut

    def test_the_phone_message_says_how_the_trade_is_managed(self, cfg, db) -> None:
        from onelot.notify.telegram import format_alert

        line = "Managed: stop above breakeven at 25% of the way, 35% locked at 50%"
        assert line in format_alert(raise_alert(desk_with(cfg, db, RATCHET)), NOW)

    def test_a_slot_without_the_rule_is_unchanged(self, cfg, db) -> None:
        desk = desk_with(cfg, db, PLAIN)
        sig = raise_alert(desk)
        assert "ratchet" not in sig["manage"] and "Managed:" not in sig["text"]
        from onelot.notify.telegram import format_alert

        assert "Managed:" not in format_alert(sig, NOW)
        assert sig["economics"]["underlying_stop"] == pytest.approx(
            sig["economics"]["underlying_entry"] * 0.99
        )


def ratchet_settings(slot):
    from onelot.manage.live import settings_of

    rset = settings_of(slot.strategy.p)
    assert rset is not None
    return rset


class TestTheTakenPosition:
    def test_it_follows_its_paper_trade_and_is_told_once_per_move(self, cfg, db) -> None:
        desk = desk_with(cfg, db, RATCHET)
        sig = raise_alert(desk)
        pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
        risk = sig["manage"]["ratchet"]["risk"]
        t = desk.trackers["SBIN"]
        base = float(t.bars["close"].iloc[-1])
        move = 3 * risk
        # 10:07 onwards (2m bars): the fill, then 30%, 55%, 85% of the planned move
        steps = [(0.0, 0.02), (0.05, 0.3), (0.28, 0.55), (0.5, 0.85), (0.84, 0.86)]
        sent_before = len(desk.notifier_log.sent)  # type: ignore[attr-defined]
        seen: list[tuple[float, float]] = []
        for k, (o, h) in enumerate(steps):
            ts = pd.Timestamp(SIGNAL_CLOSE + dt.timedelta(minutes=2 + 2 * k))
            row = {"open": base + o * move, "high": base + h * move,
                   "low": base + o * move - 0.01 * move, "close": base + h * move,
                   "volume": 1000.0}  # fmt: skip
            t.bars = pd.concat([t.bars, pd.DataFrame([row], index=pd.DatetimeIndex([ts]))])
            now = (ts + pd.Timedelta(minutes=2, seconds=5)).to_pydatetime()
            desk.paper.step(now, desk._paper_bars)  # type: ignore[union-attr]
            desk._manage_positions(now, None)
            p = desk.positions[pid]
            trade = desk.paper.for_alert(sig["id"])  # type: ignore[union-attr]
            state = trade.plan["position"]["ratchet"]["state"]
            assert (p["stop_underlying"], p["target_underlying"]) == (
                round(state["stop"], 2),
                round(state["target"], 2),
            )
            assert p["stage"] == ratchet.STAGES[state["stage"]]
            seen.append((state["stop"], state["target"]))

        told = [n for n in desk.notifier_log.sent[sent_before:]  # type: ignore[attr-defined]
                if "raise your stop" in n.title or "target moves out" in n.title]  # fmt: skip
        first = trade.plan["position"]["ratchet"]["plan"]
        levels = [(first["stop"], first["target"]), *seen]
        moves = sum(1 for a, b in zip(levels, levels[1:], strict=False) if a != b)
        assert p["stage"] == "TRAILING" and len(told) == moves >= 3
        assert f"raise your stop to {seen[-1][0]:,.2f}" in told[-1].title
        assert p["initial_stop_underlying"] < p["stop_underlying"]

    def test_without_a_paper_book_the_desk_walks_the_rule_itself(self, cfg, db) -> None:
        desk = desk_with(cfg, db, RATCHET, paper=False)
        sig = raise_alert(desk)
        pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
        risk, cost = sig["manage"]["ratchet"]["risk"], sig["manage"]["ratchet"]["cost"]
        t = desk.trackers["SBIN"]
        entry = sig["economics"]["underlying_entry"]
        plan = ratchet.plan_from_risk(1, entry, risk, 3.0, cost)
        rows = [(0.0, 0.3), (0.28, 0.55), (0.5, 0.85)]
        state, told_before = ratchet.start(plan), len(desk.notifier_log.sent)  # type: ignore[attr-defined]
        for k, (o, h) in enumerate(rows):
            ts = pd.Timestamp(SIGNAL_CLOSE + dt.timedelta(minutes=2 + 2 * k))
            bar = (entry + o * plan.move, entry + h * plan.move, entry + o * plan.move - 0.01)
            frame = pd.DataFrame([{"open": bar[0], "high": bar[1], "low": bar[2],
                                   "close": bar[1], "volume": 1.0}], index=pd.DatetimeIndex([ts]))  # fmt: skip
            t.bars = pd.concat([t.bars, frame])
            desk._manage_positions((ts + pd.Timedelta(minutes=2, seconds=5)).to_pydatetime(), None)
            assert ratchet.exit_on_bar(plan, state, *bar) is None
            state, _ = ratchet.advance(plan, state, bar[1], bar[2])
            p = desk.positions[pid]
            assert p["stop_underlying"] == round(state.stop, 2) and p["ratchet_source"] == "desk"
        told = [n for n in desk.notifier_log.sent[told_before:]  # type: ignore[attr-defined]
                if "raise your stop" in n.title]  # fmt: skip
        assert p["stage"] == "TRAILING" and len(told) == 3
