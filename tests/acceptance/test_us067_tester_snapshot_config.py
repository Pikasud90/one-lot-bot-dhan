"""US-067 independent tests: Desk.snapshot in every state (D7, D8, D11, D14), the shipped
configuration (D9), the paused slots and the intraday paths (D10, D16)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "delivery"))
sys.path.insert(0, str(HERE.parent / "desk"))

from delivery_helpers import at  # noqa: E402
from test_delivery_end_to_end import L, poll, trades, world  # noqa: E402,F401

from onelot.desk.config import load_desk_config  # noqa: E402

ROOT = HERE.parents[1]


def snap_ok(desk):
    s = desk.snapshot()
    json.dumps(s, allow_nan=False)
    return s


def test_snapshot_serialises_in_every_state_and_has_the_d7_fields(world):  # noqa: F811
    start, sent = world
    desk = start(at(L[1], "08:00"))
    s = snap_ok(desk)  # before anything
    poll(desk, at(L[2], "15:45"))
    s = snap_ok(desk)
    assert s["delivery"]["pending"] == [] and s["delivery"]["open"] == []
    assert s["delivery"]["history_to"] == L[2].isoformat()  # "daily history complete to"
    assert "nifty_history_to" in s["delivery"]
    poll(desk, at(L[3], "15:45"))
    s = snap_ok(desk)  # alert pending
    pend = {r["series_key"]: r for r in s["delivery"]["pending"]}
    assert set(pend) == {"SBIN", "ITC"} and "BUY 51 shares of SBIN" in pend["SBIN"]["alert"]
    poll(desk, at(L[4], "09:20"))
    poll(desk, at(L[4], "12:00"))
    s = snap_ok(desk)  # open
    row = next(r for r in s["delivery"]["open"] if r["series_key"] == "SBIN")
    for f in (
        "last",
        "unrealised",
        "unrealised_pct",
        "sessions",
        "max_sessions",
        "sma_exit",
        "stop",
    ):
        assert f in row and row[f] is not None, f
    assert row["max_sessions"] == 30 and row["sessions"] == 1
    poll(desk, at(L[5], "15:45"))
    s = snap_ok(desk)  # exit pending
    assert any(r["status"] == "EXIT_PENDING" for r in s["delivery"]["pending"])
    poll(desk, at(L[6], "09:20"))
    s = snap_ok(desk)  # closed
    assert all(r["series_key"] != "ITC" for r in s["delivery"]["open"])
    card = next(c for c in s["stocks"] if c["key"] == "SBIN")["pullback"]
    assert card["evidence"] and card["reading"]["needed_pct"] == 8.0
    assert "history_to" in card


def test_the_shipped_configuration_d9_d14():
    cfg = load_desk_config()
    on = [(i.key, s.name, s.timeframe) for i in cfg.instruments for s in i.strategies if s.enabled]
    assert {(n, t) for _, n, t in on} == {("pullback", "1d")}
    assert len(on) == 18 and len({k for k, _, _ in on}) == 18
    assert {i.key for i in cfg.indices}.isdisjoint({k for k, _, _ in on})
    assert all(i.kind == "STOCK" for i in cfg.stocks)
    slots = cfg.delivery_slots()
    assert len(slots) == 18 and all(i.kind == "STOCK" for i, _ in slots)
    (_, slot) = slots[0]
    assert slot.timeframe == "1d" and slot.product == "delivery"
    assert slot.params["unit_inr"] == 10000
    assert slot.params["evidence"] == (
        "Backtest: +1.2% to +1.4% a trade after charges, about 7 winners in 10, two samples. "
        "Not proven: this is the paper test."
    )
    assert all(s.params == slot.params for _, s in slots)
    paused = [(i.key, s.name) for i in cfg.instruments for s in i.strategies if not s.enabled]
    assert paused  # the other slots are paused, not deleted


def test_config_paused_stocks_have_no_tracker_strategy_for_delivery_and_no_order_api():
    import re

    for f in (ROOT / "src/onelot/delivery").glob("*.py"):
        text = f.read_text()
        assert not re.search(r"place_order|\.order\(|submit_order|/orders", text), f.name
