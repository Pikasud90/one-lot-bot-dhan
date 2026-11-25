"""US-075 through the real app (`api/server.py` calls `register_sim`): AC10 persistence and
the trade lists, AC12 errors, AC13 the paper test untouched, AC14 the options."""

from __future__ import annotations

import csv
import io
import json
import warnings
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import func, select

from onelot.api.server import create_app
from onelot.clock import IST
from onelot.data.store import save_bars
from onelot.store.schema import desk_positions, desk_signals, paper_events, paper_trades
from onelot.strategies.registry import REGISTRY

from .conftest import flat_minutes, trading_days

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "test-token"
H = {"x-onelot-token": TOKEN}
DAYS = trading_days("2025-01-06", 15)
D = DAYS[-2]


def T(hhmm: str) -> str:
    return pd.Timestamp(f"{D} {hhmm}", tz=IST).isoformat()


@pytest.fixture
def app_engine(db_engine):
    save_bars(db_engine, "RELIANCE", "1m", flat_minutes("2025-01-06", 15, 1000.0, 1.0, 1000.0), "t")
    save_bars(db_engine, "NIFTY", "1m", flat_minutes("2025-01-06", 15, 25000.0, 10.0), "t")
    engine = MagicMock()
    engine.db.engine = db_engine
    engine.desk = None
    return engine


def client(engine) -> TestClient:
    return TestClient(create_app(engine, TOKEN), raise_server_exceptions=False)


def strict(r) -> dict:
    return json.loads(r.text, parse_constant=lambda c: pytest.fail(f"non-strict JSON: {c}"))


BODY = {"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
        "start": T("10:00"), "params": {}}  # fmt: skip


def run_a_trade(c: TestClient) -> str:
    """Create, wait for the 10:15 alert, take it, ride it to the 15:15 session end."""
    r = c.post("/api/sim/sessions", json=BODY, headers=H)
    assert r.status_code == 200, r.text
    body = strict(r)
    # SIM-API-2: the create answer gains the last events, the plots' points and the marks
    assert set(body) == {"session", "candles", "state", "events", "overlays", "marks"}
    sid = body["session"]["id"]
    r = strict(c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 15}, headers=H))
    assert r["alert"] is not None and r["cursor"] == T("10:14")
    assert set(r) == {"cursor", "minutes", "events", "alert", "trade", "summary", "last_price",
                      "over", "day_end", "marks", "overlays"}  # fmt: skip
    r = strict(
        c.post(f"/api/sim/sessions/{sid}/take", json={"alert_id": r["alert"]["id"]}, headers=H)
    )
    assert r["trade"]["status"] == "PENDING_FILL"
    r = strict(c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 390}, headers=H))
    assert r["day_end"] is True and r["trade"] is None
    return sid


def test_a_session_is_rebuilt_at_its_cursor_and_the_trades_are_listed(app_engine, force):
    force(long=[f"{D} 10:10"])
    c = client(app_engine)
    sid = run_a_trade(c)
    before = strict(c.get(f"/api/sim/sessions/{sid}"))
    assert before["summary"]["trades"] == 1 and before["trades"][0]["exit_reason"] == "SESSION_END"
    # a new app: a new manager, nothing in memory
    c2 = client(app_engine)
    after = strict(c2.get(f"/api/sim/sessions/{sid}"))
    assert after["session"]["cursor"] == before["session"]["cursor"] == T("15:29")
    assert after["session"]["status"] == "DAY_END"
    assert after["trades"] == before["trades"] and after["summary"] == before["summary"]
    r = strict(c2.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 1}, headers=H))
    assert r["cursor"] == pd.Timestamp(f"{DAYS[-1]} 09:15", tz=IST).isoformat()
    # a second session's trade: the lists hold both, newest first
    sid2 = run_a_trade(c2)
    rows = strict(c2.get("/api/sim/trades"))["trades"]
    assert [t["session_id"] for t in rows] == [sid2, sid]
    assert {"instrument", "strategy", "timeframe", "mode"} <= set(rows[0])
    assert rows[0]["instrument"] == "RELIANCE" and rows[0]["mode"] == "past"
    one = strict(c2.get(f"/api/sim/trades?session_id={sid}"))["trades"]
    assert [t["session_id"] for t in one] == [sid]
    text = c2.get("/api/sim/trades.csv").text
    table = list(csv.reader(io.StringIO(text)))
    assert table[0][0] == "session_id" and len(table) == 3


def test_a_full_trade_leaves_the_paper_test_empty(app_engine, force):
    force(long=[f"{D} 10:10"])
    run_a_trade(client(app_engine))
    with app_engine.db.engine.connect() as conn:
        for table in (paper_trades, paper_events, desk_signals, desk_positions):
            assert conn.execute(select(func.count()).select_from(table)).scalar() == 0


@pytest.mark.parametrize(
    "change, word",
    [
        ({"instrument": "NOPE"}, "Unknown instrument"),
        ({"strategy": "nope"}, "Unknown strategy"),
        ({"timeframe": "7m"}, "runs on"),
        ({"strategy": "microtrend", "timeframe": "3m"}, "not meant for stock"),
        ({"start": "2020-01-01T10:00:00+05:30"}, "outside the stored data"),
        ({"start": "2030-01-01T10:00:00+05:30"}, "outside the stored data"),
        ({"mode": "live", "start": None}, "live desk is not running"),
    ],
)
def test_a_bad_request_is_a_400_with_a_plain_sentence(app_engine, change, word):
    r = client(app_engine).post("/api/sim/sessions", json={**BODY, **change}, headers=H)
    assert r.status_code == 400 and word in r.json()["detail"]


def test_an_index_only_strategy_on_a_stock_and_a_stock_one_on_an_index(app_engine):
    c = client(app_engine)
    r = c.post(
        "/api/sim/sessions", json={**BODY, "instrument": "NIFTY", "strategy": "gap_fade"}, headers=H
    )
    assert r.status_code == 400 and "not meant for index" in r.json()["detail"]


def test_token_unknown_session_and_conflicts(app_engine, force):
    force()
    c = client(app_engine)
    assert c.post("/api/sim/sessions", json=BODY).status_code == 401
    sid = strict(c.post("/api/sim/sessions", json=BODY, headers=H))["session"]["id"]
    assert c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 1}).status_code == 401
    r = c.post(f"/api/sim/sessions/{sid}/take", json={"alert_id": "a_1"}, headers=H)
    assert r.status_code == 409
    assert c.post(f"/api/sim/sessions/{sid}/close", json={}, headers=H).status_code == 409
    assert c.get("/api/sim/sessions/s_nothere").status_code == 404
    r = c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 0}, headers=H)
    assert r.status_code == 400
    r = strict(c.post(f"/api/sim/sessions/{sid}/end", json={}, headers=H))
    assert r["session"]["status"] == "ENDED"
    assert set(r) == {"session", "alert", "trade", "summary", "trades", "events", "minutes",
                      "marks"}  # fmt: skip
    got = strict(c.get(f"/api/sim/sessions/{sid}"))
    assert set(got) == {"session", "alert", "trade", "summary", "trades", "candles", "events",
                        "overlays", "marks"}  # fmt: skip
    r = c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 1}, headers=H)
    assert r.status_code == 409


def test_the_options(app_engine):
    body = strict(client(app_engine).get("/api/sim/options"))
    inst = {i["key"]: i for i in body["instruments"]}
    assert {"NIFTY", "BANKNIFTY", "MIDCPNIFTY", "RELIANCE"} <= set(inst)
    assert inst["MIDCPNIFTY"]["trade"] is False and inst["NIFTY"]["trade"] is True
    assert inst["NIFTY"]["kind"] == "INDEX" and inst["RELIANCE"]["kind"] == "STOCK"
    assert inst["NIFTY"]["first"] == pd.Timestamp(f"{DAYS[0]} 09:15", tz=IST).isoformat()
    assert inst["NIFTY"]["last"] == pd.Timestamp(f"{DAYS[-1]} 15:29", tz=IST).isoformat()
    assert inst["BANKNIFTY"]["first"] is None and inst["NIFTY"]["live"] is False
    strategies = {s["name"]: s for s in body["strategies"]}
    assert set(strategies) == set(REGISTRY)
    assert strategies["microtrend"]["kinds"] == ["INDEX"]
    assert strategies["microtrend"]["timeframes"] == ["3m"]
    assert strategies["gap_fade"]["kinds"] == ["STOCK"]
    assert body["speeds"] == [1, 2, 5, 10, 60, 300]
    assert body["defaults"]["stop_atr_k"] == 1.5 and body["defaults"]["target_r"] == 3.0
    assert body["defaults"]["capital"] == 100000
