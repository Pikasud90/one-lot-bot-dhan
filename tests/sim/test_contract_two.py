"""US-095 AC1, AC5, AC6 and points 6 and 7: the second contract's routes, fields and rules
through the real app (`api/server.py` registers the routes), on bars in a scratch store."""

from __future__ import annotations

import json
import warnings
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pandas as pd
import pytest
from sqlalchemy import event, func, select

from onelot.api.server import create_app
from onelot.clock import IST
from onelot.sim.bars import StoreBars
from onelot.sim.candles import CHART_TIMEFRAMES
from onelot.store.schema import sim_events, sim_sessions
from onelot.strategies.registry import REGISTRY

from .conftest import (
    MemBars,
    flat_minutes,
    make_manager,
    set_bar,
    store_frames,
    trading_days,
    walk_minutes,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "test-token"
H = {"x-onelot-token": TOKEN}
DAYS = trading_days("2025-01-06", 15)
D = DAYS[-2]


def T(hhmm: str, day=None) -> str:
    return pd.Timestamp(f"{day or D} {hhmm}", tz=IST).isoformat()


def q(**kw) -> str:
    """A query string, encoded as the page's helper encodes it (`+` of the offset too)."""
    return urlencode({k: v for k, v in kw.items() if v is not None})


def strict(r) -> dict:
    assert r.status_code == 200, r.text
    return json.loads(r.text, parse_constant=lambda c: pytest.fail(f"non-strict JSON: {c}"))


@pytest.fixture
def frames():
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = walk_minutes("2025-01-06", 15, 25000.0, 0.0006, seed=3, volume=False)
    return {"RELIANCE": rel, "NIFTY": nifty}


@pytest.fixture
def app(db_engine, frames, tmp_path, monkeypatch):
    """The real app on the scratch store; `app.holder["m"]` is its simulator manager."""
    import onelot.api.server as server
    from onelot.sim import routes

    store_frames(db_engine, frames)
    engine = MagicMock()
    engine.db.engine = db_engine
    engine.desk = None
    holder: dict = {}

    def factory(desk):
        holder["m"] = make_manager(db_engine, StoreBars(db_engine), tmp_path)
        return holder["m"]

    monkeypatch.setattr(
        server, "register_sim", lambda a, e, tok: routes.register(a, e, tok, factory)
    )
    client = TestClient(create_app(engine, TOKEN), raise_server_exceptions=False)
    client.holder = holder  # type: ignore[attr-defined]
    return client


BODY = {"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
        "start": T("10:00"), "params": {}}  # fmt: skip


# ── AC1: options, days, candles, prepare ─────────────────────────────────────
def test_options_carry_the_second_contract(app):
    body = strict(app.get("/api/sim/options"))
    inst = {i["key"]: i for i in body["instruments"]}
    assert inst["NIFTY"]["unit"] == "lot" and inst["NIFTY"]["lot"] > 1
    assert inst["NIFTY"]["tick"] == 0.05
    assert inst["RELIANCE"]["unit"] == "share" and inst["RELIANCE"]["lot"] == 1
    assert inst["RELIANCE"]["tick"] > 0
    strategies = {s["name"]: s for s in body["strategies"]}
    assert set(strategies) == set(REGISTRY)
    assert strategies["microtrend"]["plots"] == [
        {"id": "st", "label": "Supertrend 3m", "pane": "price"}
    ]
    assert [p["label"] for p in strategies["ema_cross"]["plots"]] == ["EMA 9", "EMA 21"]
    assert [p["id"] for p in strategies["cpr_breakout"]["plots"]] == ["pivot", "bc", "tc"]
    assert strategies["gap_fade"]["plots"] == []
    assert body["defaults"]["instrument"] == "NIFTY"
    assert body["defaults"]["strategy"] == "microtrend"
    assert body["defaults"]["timeframe"] == "3m"
    assert body["chart_timeframes"] == ["1m", "3m", "5m", "10m", "15m", "30m", "60m"]


def test_options_never_count_the_bars(app, db_engine):
    seen: list[str] = []
    event.listen(db_engine, "before_cursor_execute", lambda c, cur, st, *a: seen.append(st))
    strict(app.get("/api/sim/options"))
    assert seen and not any("count(" in s.lower() for s in seen)
    seen.clear()
    strict(app.get("/api/sim/options"))  # kept: no bar query at all
    assert not any("FROM bars" in s for s in seen)


def test_days(app, db_engine, frames):
    short = frames["RELIANCE"].iloc[:200].copy()  # a day with 200 bars is not a trading day
    short.index = short.index - pd.Timedelta(days=3)  # the Friday before the first Monday
    store_frames(db_engine, {"RELIANCE": short})
    body = strict(app.get("/api/sim/days?instrument=RELIANCE"))
    assert body["instrument"] == "RELIANCE"
    assert body["days"] == [str(d) for d in DAYS]
    assert body["first"] == T("09:15", short.index[0].date())
    assert body["last"] == T("15:29", DAYS[-1])
    assert app.get("/api/sim/days?instrument=NOPE").status_code == 400


def test_candles_shape_count_until_and_errors(app, frames):
    body = strict(
        app.get("/api/sim/candles?" + q(instrument="NIFTY", timeframe="3m", until=T("11:42")))
    )
    assert body["instrument"] == {"key": "NIFTY", "label": "NIFTY 50", "kind": "INDEX",
                                  "tick": 0.05}  # fmt: skip
    assert body["timeframe"] == "3m" and body["tf_minutes"] == 3
    assert body["until"] == T("11:42")
    c = body["candles"]
    assert len(c) == 300 and all(len(x) == 6 for x in c)
    assert [x[0] for x in c] == sorted(x[0] for x in c)
    assert all(x[5] == 0 for x in c)  # an index has no volume
    u = pd.Timestamp(T("11:42")).value // 10**9
    assert c[-1][0] + 180 <= u + 60  # the last candle ended at or before the 11:42 minute
    assert c[-1][0] == pd.Timestamp(T("11:39")).value // 10**9
    assert body["forming"][0] == u  # the 11:42 to 11:45 candle holds only 11:42 so far
    body = strict(
        app.get("/api/sim/candles?" + q(instrument="NIFTY", timeframe="5m", until=T("11:42")))
    )
    m = frames["NIFTY"]
    part = m[(m.index >= pd.Timestamp(T("11:40"))) & (m.index <= pd.Timestamp(T("11:42")))]
    f = body["forming"]
    assert f[0] == pd.Timestamp(T("11:40")).value // 10**9
    assert f[1] == round(part["open"].iloc[0], 2) and f[4] == round(part["close"].iloc[-1], 2)
    assert f[2] == round(part["high"].max(), 2) and f[3] == round(part["low"].min(), 2)
    assert body["candles"][-1][0] == pd.Timestamp(T("11:35")).value // 10**9
    # count, defaults and errors
    assert len(strict(app.get("/api/sim/candles?instrument=RELIANCE&count=50"))["candles"]) == 50
    last = strict(app.get("/api/sim/candles?instrument=RELIANCE&timeframe=1m"))
    assert last["until"] == T("15:29", DAYS[-1]) and last["forming"] is None
    assert last["candles"][-1][5] == 1000.0  # a stock's volume
    for qs, word in (("count=49", "count"), ("count=1001", "count"), ("count=x", "count"),
                    ("timeframe=7m", "1m, 3m, 5m"), ("until=nonsense", "until")):  # fmt: skip
        r = app.get(f"/api/sim/candles?instrument=RELIANCE&{qs}")
        assert r.status_code == 400 and word in r.json()["detail"], qs
    assert app.get("/api/sim/candles?instrument=NOPE").status_code == 400
    early = strict(
        app.get("/api/sim/candles?instrument=RELIANCE&until=2020-01-01T10:00:00%2B05:30")
    )
    assert "begin" in early["note"] and early["until"] == T("09:15", DAYS[0])
    late = strict(app.get("/api/sim/candles?instrument=RELIANCE&until=2030-01-01T10:00:00%2B05:30"))
    assert "end" in late["note"] and late["until"] == T("15:29", DAYS[-1])
    for tf in CHART_TIMEFRAMES:
        strict(app.get(f"/api/sim/candles?instrument=RELIANCE&timeframe={tf}"))


def test_candles_never_show_a_minute_after_until(db_engine, frames, tmp_path):
    """AC5: the answer at `until` equals the answer of a store that ends at `until`."""
    store_frames(db_engine, frames)
    full = make_manager(db_engine, StoreBars(db_engine), tmp_path)
    for hhmm in ("10:01", "11:42", "13:07", "15:29"):
        u = pd.Timestamp(T(hhmm))
        cut = {k: v[v.index <= u] for k, v in frames.items()}
        part = make_manager(db_engine, MemBars(cut), tmp_path)
        for tf in ("1m", "5m", "15m", "60m"):
            a = full.candles("NIFTY", tf, u.isoformat(), 200)
            b = part.candles("NIFTY", tf, u.isoformat(), 200)
            assert a["candles"] == b["candles"] and a["forming"] == b["forming"], (hhmm, tf)


def test_the_candles_route_draws_what_a_session_draws(app):
    s = strict(app.post("/api/sim/sessions", json=BODY, headers=H))
    sid = s["session"]["id"]
    strict(app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 77}, headers=H))
    got = strict(app.get(f"/api/sim/sessions/{sid}"))
    cur = got["session"]["cursor"]
    c = strict(app.get("/api/sim/candles?" + q(instrument="RELIANCE", timeframe="5m", until=cur)))
    assert c["candles"] == got["candles"]
    first = strict(
        app.get("/api/sim/candles?" + q(instrument="RELIANCE", timeframe="5m", until=T("09:59")))
    )
    assert first["candles"] == s["candles"]


def test_prepare_warms_and_writes_nothing(app, db_engine):
    qs = q(instrument="RELIANCE", strategy="ema_cross", timeframe="5m", start=T("10:00"))
    r = strict(app.get(f"/api/sim/prepare?{qs}"))
    assert r["key"] == "RELIANCE|ema_cross|5m" and r["status"] == "ready" and r["ms"] >= 0
    m = app.holder["m"]
    builds = m.frame_cache.builds
    assert strict(app.get(f"/api/sim/prepare?{qs}"))["status"] == "ready"
    assert m.frame_cache.builds == builds  # identical keys: one build
    with db_engine.connect() as c:
        assert c.execute(select(func.count()).select_from(sim_sessions)).scalar() == 0
    s = strict(app.post("/api/sim/sessions", json=BODY, headers=H))
    assert s["session"]["status"] == "RUNNING" and m.frame_cache.builds == builds
    for bad, word in (("instrument=NOPE&strategy=ema_cross", "Unknown instrument"),
                      ("instrument=RELIANCE&strategy=nope", "Unknown strategy"),
                      ("instrument=RELIANCE&strategy=microtrend", "not meant for")):  # fmt: skip
        r = app.get(f"/api/sim/prepare?{bad}")
        assert r.status_code == 400 and word in r.json()["detail"]
    assert strict(app.get("/api/sim/prepare?instrument=NIFTY&strategy=orb"))["status"] == "ready"


# ── AC1: the session's new fields ────────────────────────────────────────────
def test_create_read_advance_and_actions_carry_the_new_fields(app, force):
    force(long=[f"{D} 10:10"])
    made = strict(app.post("/api/sim/sessions", json=BODY, headers=H))
    assert {"events", "overlays", "marks"} <= set(made)
    assert made["session"]["chart"] == {"tick": 0.05, "lot": 1, "unit": "share"} or (
        made["session"]["chart"]["unit"] == "share" and made["session"]["chart"]["lot"] == 1
    )
    assert made["events"] == [] and made["marks"] == []
    assert set(made["overlays"]) == {"ema_fast", "ema_slow"}
    sid = made["session"]["id"]
    r = strict(app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 15}, headers=H))
    a = r["alert"]
    assert a["lot"] == 1 and a["unit"] == "share" and a["expires_in_min"] == 10
    assert set(r["overlays"]) == {"ema_fast", "ema_slow"} and r["marks"] == []
    pts = r["overlays"]["ema_fast"]
    assert pts and all(len(p) == 2 for p in pts)
    r = strict(app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 3}, headers=H))
    assert r["alert"]["expires_in_min"] == 7
    t = strict(app.post(f"/api/sim/sessions/{sid}/take", json={"alert_id": a["id"]}, headers=H))
    assert t["marks"] == [] and t["trade"]["status"] == "PENDING_FILL"
    assert t["trade"]["minutes_in_trade"] == 0 and t["trade"]["open_r"] is None
    r = strict(app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 5}, headers=H))
    tr = r["trade"]
    assert tr["status"] == "OPEN" and tr["lot"] == 1 and tr["unit"] == "share"
    assert tr["stop_price"] == tr["stop"] and tr["target_price"] == tr["target"]  # a stock
    assert tr["risk_inr"] > 0 and tr["minutes_in_trade"] == 5
    assert tr["open_r"] == pytest.approx(tr["open_pnl"] / tr["risk_inr"], abs=1e-3)
    assert len(r["marks"]) == 5
    last = r["marks"][-1]
    assert last[0] == r["minutes"][-1][0] and last[1] == tr["mark"]
    assert last[2] == tr["mark_price"] and last[3] == tr["open_pnl"]
    got = strict(app.get(f"/api/sim/sessions/{sid}"))
    assert got["marks"] == r["marks"]
    kinds = [e["kind"] for e in got["events"]]
    assert kinds == ["ALERT", "FILLED"] and [e["seq"] for e in got["events"]] == [1, 2]
    assert set(got["overlays"]) == {"ema_fast", "ema_slow"}
    last_candle = got["candles"][-1][0]
    assert got["overlays"]["ema_fast"][-1][0] == last_candle
    end = strict(app.post(f"/api/sim/sessions/{sid}/end", json={}, headers=H))
    assert end["marks"] == [] and end["session"]["status"] == "ENDED"
    closed = end["trades"][0]
    assert closed["lot"] == 1 and closed["unit"] == "share" and closed["risk_inr"] > 0
    assert closed["minutes_in_trade"] == 6 and closed["open_r"] is None
    listed = strict(app.get(f"/api/sim/trades?session_id={sid}"))["trades"][0]
    assert listed["minutes_in_trade"] == 6


def test_advance_until_is_checked(app):
    sid = strict(app.post("/api/sim/sessions", json=BODY, headers=H))["session"]["id"]
    r = app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 5, "until": "x"}, headers=H)
    assert r.status_code == 400 and "until" in r.json()["detail"]
    r = strict(app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 5, "until": None},
                        headers=H))  # fmt: skip
    assert len(r["minutes"]) == 5


def test_the_read_carries_at_most_200_events_oldest_first(app, force, tmp_path):
    every = pd.date_range("09:30", "14:30", freq="5min").strftime("%H:%M")
    force(long=[f"{d} {h}" for d in DAYS[2:] for h in every])
    sid = strict(app.post("/api/sim/sessions", json={**BODY, "start": T("10:00", DAYS[2])},
                          headers=H))["session"]["id"]  # fmt: skip
    for _ in range(40):
        r = strict(app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 390}, headers=H))
        if r["over"]:
            break
    got = strict(app.get(f"/api/sim/sessions/{sid}"))
    seqs = [e["seq"] for e in got["events"]]
    assert len(seqs) == 200 and seqs == sorted(seqs) and seqs[-1] > 200
    # after a restart (a new manager), the same events come from the table
    m = app.holder["m"]
    m2 = make_manager(m.engine, m.store_bars, tmp_path)
    assert m2.read(sid)["events"] == got["events"]


# ── AC6: event times ─────────────────────────────────────────────────────────
def test_a_fill_carries_its_open_and_a_stop_its_close(db_engine, tmp_path, force):
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    set_bar(rel, f"{D} 11:50", 1000.0, 1000.5, 990.0, 991.0)  # through the stop (997)
    force(long=[f"{D} 11:35"])
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    s = m.get(m.create({**BODY, "start": T("11:30")})["session"]["id"])
    r = s.advance(10)  # 11:30 to 11:39: the 11:35 candle's alert at 11:40
    while s.alert is None:
        r = s.advance(1)
    assert s.cursor == T("11:39")
    r = s.advance(3)
    assert s.cursor == T("11:42")
    s.take(s.alert["id"])
    r = s.advance(1)
    filled = [e for e in r["events"] if e["kind"] == "FILLED"]
    assert filled[0]["ts"] == T("11:43")  # the fill at the 11:43 open
    r = s.advance(8)
    closed = [e for e in r["events"] if e["kind"] == "CLOSED"]
    assert closed[0]["data"]["exit_reason"] == "STOP"
    assert closed[0]["ts"] == T("11:51")  # hit in the 11:50 minute: known at its close


# ── point 6: fewer writes ────────────────────────────────────────────────────
def test_the_cursor_is_saved_on_events_actions_and_every_30_minutes(db_engine, tmp_path):
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    s = m.get(m.create({**BODY, "start": T("10:00")})["session"]["id"])
    updates: list[str] = []

    def seen(conn, cur, statement, *a):
        if statement.startswith("UPDATE sim_sessions"):
            updates.append(statement)

    event.listen(db_engine, "before_cursor_execute", seen)
    for _ in range(100):
        r = s.advance(1)
        assert r["events"] == []  # a flat stock: nothing happens
    assert len(updates) == 3  # at 30, 60 and 90 revealed minutes, not 100 times
    sid = s.setup.id
    with db_engine.connect() as c:
        saved = c.execute(select(sim_sessions.c.cursor_ts).where(sim_sessions.c.id == sid)).scalar()
    assert pd.Timestamp(saved) == pd.Timestamp(T("11:29"))
    # a restart resumes from the last saved cursor and says so in the read
    m2 = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    got = m2.read(sid)
    assert got["session"]["cursor"] == T("11:29") and got["session"]["status"] == "RUNNING"
    s2 = m2.get(sid)
    assert s2.advance(1)["cursor"] == T("11:30")
    s.skip  # noqa: B018 - the action names exist
    updates.clear()
    s.end()
    assert len(updates) == 1  # a pause-like action always writes
    with db_engine.connect() as c:
        n = c.execute(select(func.count()).select_from(sim_events)).scalar()
    assert n == 0


def test_older_sessions_leave_memory_and_come_back_the_same(db_engine, tmp_path, force):
    force(long=[f"{D} 10:10"])
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path,
                     max_sessions=2)  # fmt: skip
    first = m.create(BODY)["session"]["id"]
    s = m.get(first)
    s.advance(15)
    s.take(s.alert["id"])
    s.advance(20)
    before = s.read()
    for _ in range(2):
        m.create(BODY)
    assert first not in m.sessions  # the least recently used left memory, saved
    after = m.read(first)  # rebuilt from its rows
    assert after == before
