"""US-089 AC4, AC5, AC7, AC8: the routes through the real app, the desk's poll, the order book,
the config block and the replay tool."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from onelot.api.server import create_app
from onelot.broker.base import QuoteSnapshot
from onelot.broker.dhan import depth_steps
from onelot.desk.config import load_desk_config
from onelot.volume.service import VolumeWatch, book_reading, build_baselines, compute_view

from .conftest import BASKET, DAYS, SPIKE_DAY, at, desk_cfg, make_bars

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def strict(r) -> dict:
    return json.loads(r.text, parse_constant=lambda c: pytest.fail(f"non-strict JSON: {c}"))


@pytest.fixture
def client(store):
    after = at(SPIKE_DAY + dt.timedelta(days=3), "08:00")
    watch = VolumeWatch(store, None, desk_cfg=desk_cfg(), sync=True, clock=lambda: after)
    engine = MagicMock()
    engine.db.engine = store
    engine.desk = SimpleNamespace(volume=watch)
    return TestClient(create_app(engine, "t"), raise_server_exceptions=False)


def test_four_routes_answer_with_the_contract_keys(client):
    r = client.get(f"/api/volume?date={DAYS[-2]}&index=BANKNIFTY")
    assert r.status_code == 200
    body = strict(r)
    assert body["source"] == "stored" and body["index"]["key"] == "BANKNIFTY"
    assert body["book"] is None and body["now"]["minute"] == "15:29"
    p = strict(client.get("/api/volume/profile"))
    assert {"as_of", "sessions", "minutes", "half_hours", "busiest", "quietest", "weekdays",
            "expiry", "text"} <= set(p)  # fmt: skip
    assert p["sessions"] > 0 and len(p["minutes"]) == 375 and len(p["minutes"][0]) == 4
    assert p["half_hours"][0]["from"] == "09:15" and p["expiry"]["note"] == "NIFTY expiry days"
    a = strict(client.get("/api/volume/alerts?limit=5"))
    assert a == {"alerts": []}
    s = strict(client.get("/api/volume/study"))
    assert {"as_of", "note", "facts", "alerts_per_day"} <= set(s)
    assert all({"id", "text", "value"} <= set(f) for f in s["facts"])


def test_bad_date_is_a_400_with_a_plain_sentence(client):
    r = client.get("/api/volume?date=yesterday")
    assert r.status_code == 400 and r.json()["detail"].endswith(".")
    r = client.get("/api/volume?date=2031-01-02")
    assert r.status_code == 400 and str(DAYS[0]) in r.json()["detail"]


def test_routes_without_a_desk_read_the_store(store):
    engine = MagicMock()
    engine.db.engine = store
    engine.desk = None
    c = TestClient(create_app(engine, "t"), raise_server_exceptions=False)
    body = strict(c.get(f"/api/volume?date={DAYS[-2]}"))
    # the shipped config's basket: the fixture holds three of its stocks, the rest are missing
    assert set(BASKET) <= {s["key"] for s in body["stocks"]}
    assert len(body["basket"]["missing"]) == body["basket"]["count"] - 3
    assert c.get("/api/volume/profile").status_code == 200


# ── AC4: the desk's poll survives the watch ──────────────────────────────
@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database
    from onelot.store import init_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def test_the_real_poll_survives_an_exception_in_the_volume_step(cfg, db):
    sys.path.insert(0, str(ROOT / "tests" / "desk"))
    try:
        from desk_harness import OpenClock, new_desk
    finally:
        sys.path.pop(0)
    desk, _, _ = new_desk(cfg, db, clock=OpenClock())
    assert isinstance(desk.volume, VolumeWatch)

    class Boom:
        def on_poll(self, now, market_open):
            raise ZeroDivisionError("inside the watch")

    desk.volume = Boom()
    desk.poll()
    desk.poll()
    assert desk.volume_errors == 2 and desk.volume_last_error == "ZeroDivisionError"
    assert desk.polls == 2  # the poll itself went on


# ── AC7: the order book ──────────────────────────────────────────────────
DHAN_QUOTE = {  # the shape of Dhan's /marketfeed/quote answer
    "NSE_EQ": {
        "1333": {
            "last_price": 1712.5,
            "volume": 1234567,
            "depth": {
                "buy": [
                    {"quantity": 1000 * (i + 1), "orders": 3 + i, "price": 1712.4 - 0.1 * i}
                    for i in range(5)
                ],
                "sell": [
                    {"quantity": 500 * (i + 1), "orders": 2 + i, "price": 1712.6 + 0.1 * i}
                    for i in range(5)
                ],
            },  # fmt: skip
        }
    }
}


def test_dhan_quote_parses_five_steps_and_keeps_the_best_step(monkeypatch):
    from onelot.broker.base import Contract
    from onelot.broker.dhan import DhanAdapter

    a = DhanAdapter.__new__(DhanAdapter)
    monkeypatch.setattr(a, "_post", lambda *args, **kw: DHAN_QUOTE, raising=False)
    q = a.quote([Contract("1333", "NSE_EQ", "HDFC Bank", "HDFCBANK", "EQUITY")])["1333"]
    assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (1712.4, 1712.6, 1000, 500)
    assert len(q.bids) == 5 and len(q.asks) == 5
    assert q.bids[4] == (pytest.approx(1712.0), 5000, 7)
    assert depth_steps([{"price": 0, "quantity": 5}, "x", {"price": 10, "quantity": 2}]) == (
        (10.0, 2, 0),
    )
    reading = book_reading({"HDFCBANK": q}, at(SPIKE_DAY, "11:00"))
    row = reading["stocks"][0]
    assert row == {"key": "HDFCBANK", "bid_qty": 15000, "ask_qty": 7500, "bid_share": 0.667,
                   "spread_bps": pytest.approx(1.17, abs=0.01)}  # fmt: skip
    assert reading["bid_share"] == pytest.approx(0.667, abs=0.001)
    assert (
        book_reading({"X": QuoteSnapshot("1", at(SPIKE_DAY, "11:00"))}, at(SPIKE_DAY, "11:00"))
        is None
    )


class BookDesk:
    fine_minutes = 1

    def __init__(self, adapter) -> None:
        self.trackers: dict = {}
        self.context_bars: dict = {}
        self.adapter = adapter
        self.cfg = desk_cfg()

    def _market_open(self, now):
        return True

    def _data_api_active(self):
        return True

    def _adapter_getter(self):
        return self.adapter


def test_a_broker_error_leaves_the_book_null_and_backs_off(store):
    calls = []

    class Failing:
        def quote(self, contracts):
            calls.append(len(contracts))
            raise TimeoutError("broker")

    now = at(SPIKE_DAY, "11:00")
    w = VolumeWatch(store, BookDesk(Failing()), desk_cfg=desk_cfg(), sync=True, clock=lambda: now)
    for i in range(5):
        w._read_book(now + dt.timedelta(minutes=i))
    assert len(calls) == 3 and w.book_last is None and w.book_view() is None
    assert w.book_error == "TimeoutError"


def test_the_mock_broker_book_is_stored(store, mock_broker):
    now = mock_broker.now()
    w = VolumeWatch(store, BookDesk(mock_broker), desk_cfg=desk_cfg(), sync=True, clock=lambda: now)
    w._read_book(now)
    assert w.book_last is not None and len(w.book_last["stocks"]) == len(BASKET) + 1
    assert w.book_view()["note"].startswith("Resting orders in the best five price steps.")


# ── config ───────────────────────────────────────────────────────────────
def test_volume_block_of_desk_yaml(tmp_path):
    shipped = load_desk_config()
    assert shipped.volume.alerts_on is True and shipped.volume.max_alerts_per_day == 12
    p = tmp_path / "desk.yaml"
    p.write_text("volume:\n  basket: [sbin, infy]\n  z_alert: 2.5\n  notify: false\n")
    v = load_desk_config(p).volume
    assert v.basket == ("SBIN", "INFY") and v.z_alert == 2.5 and v.notify is False
    p.write_text("volume:\n  zalert: 2\n")
    with pytest.raises(ValueError, match="unknown"):
        load_desk_config(p)
    p.write_text("volume:\n  notify: 'false'\n")
    with pytest.raises(ValueError):
        load_desk_config(p)


# ── speed ────────────────────────────────────────────────────────────────
def test_the_minute_step_is_fast_on_eighteen_stocks():
    keys = [f"S{i:02d}" for i in range(18)]
    days = DAYS[-25:]
    frames = {k: make_bars(i + 10, 300.0 + 20 * i, days=days) for i, k in enumerate(keys)}
    base = build_baselines(SPIKE_DAY, None, keys, [], lambda k: frames.get(k))
    cut = pd.Timestamp(dt.datetime.combine(SPIKE_DAY, dt.time(13, 0)), tz="Asia/Kolkata")
    today = {k: f[(f.index.date == SPIKE_DAY) & (f.index <= cut)] for k, f in frames.items()}
    compute_view(SPIKE_DAY, today, base, keys, "bvc")  # warm the basket cache
    # CPU time, not wall time: other work on a shared machine must not fail this test
    t0 = time.process_time()
    view = compute_view(SPIKE_DAY, today, base, keys, "bvc")
    took = time.process_time() - t0
    assert view is not None and view.last_col == 225
    assert took < 0.2, f"the minute's view took {took:.3f}s of CPU"
    w = VolumeWatch.__new__(VolumeWatch)  # the body's assembly alone, on the same view
    w.cfg, w.series, w.desk, w.basket = desk_cfg().volume, {}, None, keys
    w.index_cfg = [("NIFTY", True)]
    t0 = time.process_time()
    body = w._assemble(view, base, "NIFTY", None, [], "live")
    took = time.process_time() - t0
    assert body["now"]["minute"] == "13:00" and len(body["stocks"]) == 18
    assert took < 0.3, f"the body took {took:.3f}s of CPU"


# ── AC8: the replay tool ─────────────────────────────────────────────────
def test_volume_replay_runs_on_a_fixture_store(store, tmp_path):
    path = Path(store.url.database)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "volume_replay.py"), "--store", str(path),
         "--date", SPIKE_DAY.isoformat()],
        capture_output=True, text=True, timeout=120,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "check: OK" in r.stdout and "Volume watch" in r.stdout
    assert dataclasses.is_dataclass(desk_cfg())
