"""US-095 AC4 and point 7: a session is built in the worker, off the request.

Create answers BUILDING at once (with the cheap candles), an action on it is a 409, the read
turns RUNNING; a build that raises turns FAILED with a plain sentence; a newer choice
replaces the waiting build; ten quick creates leave one worker and no orphan; a stored
session's read is answered from its row while it is rebuilt (F5)."""

from __future__ import annotations

import threading
import time
import warnings
from unittest.mock import MagicMock

import pandas as pd
import pytest

from onelot.api.server import create_app
from onelot.clock import IST
from onelot.sim import engine as sim_engine
from onelot.sim import routes, worker
from onelot.sim.bars import StoreBars

from .conftest import flat_minutes, make_manager, store_frames, trading_days

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "test-token"
H = {"x-onelot-token": TOKEN}
DAYS = trading_days("2025-01-06", 15)
D = DAYS[-2]


def T(hhmm: str, day=None) -> str:
    return pd.Timestamp(f"{day or D} {hhmm}", tz=IST).isoformat()


def body(strategy: str = "ema_cross", tf: str = "5m", at: str = "10:00", day=None) -> dict:
    return {"instrument": "RELIANCE", "strategy": strategy, "timeframe": tf, "mode": "past",
            "start": T(at, day), "params": {}}  # fmt: skip


@pytest.fixture
def slow(monkeypatch):
    """Builds that take `slow.seconds` (and raise when `slow.fail` is set)."""
    real = sim_engine.build_signals
    state = {"seconds": 0.3, "fail": False, "calls": 0}

    def build(*a, **k):
        state["calls"] += 1
        time.sleep(state["seconds"])
        if state["fail"]:
            raise ValueError("the fixture bars cannot be read")
        return real(*a, **k)

    monkeypatch.setattr(sim_engine, "build_signals", build)
    return state


@pytest.fixture
def stored(db_engine):
    store_frames(
        db_engine,
        {
            "RELIANCE": flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0),
            "NIFTY": flat_minutes("2025-01-06", 15, 25000.0, 10.0),
        },
    )
    return db_engine


@pytest.fixture
def manager(stored, tmp_path):
    m = make_manager(stored, StoreBars(stored), tmp_path, build="thread")
    m.builder.idle_s = 0.2
    yield m
    m.builder.idle(10.0)


@pytest.fixture
def app(stored, tmp_path, monkeypatch):
    import onelot.api.server as server

    engine = MagicMock()
    engine.db.engine = stored
    engine.desk = None
    holder: dict = {}

    def factory(desk):
        holder["m"] = make_manager(stored, StoreBars(stored), tmp_path, build="thread")
        holder["m"].builder.idle_s = 0.2
        return holder["m"]

    monkeypatch.setattr(
        server, "register_sim", lambda a, e, tok: routes.register(a, e, tok, factory)
    )
    client = TestClient(create_app(engine, TOKEN), raise_server_exceptions=False)
    client.holder = holder  # type: ignore[attr-defined]
    yield client
    if "m" in holder:
        holder["m"].builder.idle(10.0)


def wait_status(c: TestClient, sid: str, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = c.get(f"/api/sim/sessions/{sid}").json()
        if st["session"]["status"] != "BUILDING":
            return st
        time.sleep(0.02)
    pytest.fail("still BUILDING")


def test_create_answers_building_at_once_then_running(app, slow):
    t0 = time.perf_counter()
    r = app.post("/api/sim/sessions", json=body(), headers=H)
    took = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    made = r.json()
    assert took < slow["seconds"], "the create waited for the build"
    assert made["session"]["status"] == "BUILDING"
    assert made["session"]["cursor"] == T("09:59")
    assert made["state"]["alert"] is None and made["state"]["trade"] is None
    assert made["build"]["step"] in ("reading bars", "building signals", "pricing")
    assert made["build"]["elapsed_ms"] >= 0 and made["state"]["build"] == made["build"]
    assert len(made["candles"]) == 300 and made["candles"][-1][0] + 300 <= (
        pd.Timestamp(T("10:00")).value // 10**9
    )
    sid = made["session"]["id"]
    r = app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 1}, headers=H)
    assert r.status_code == 409 and r.json()["detail"] == "The session is still being prepared."
    for action in ("take", "skip", "close", "end"):
        r = app.post(f"/api/sim/sessions/{sid}/{action}", json={"alert_id": "a_1"}, headers=H)
        assert r.status_code == 409
    got = wait_status(app, sid)
    assert got["session"]["status"] == "RUNNING" and "build" not in got
    assert got["candles"] == made["candles"]  # the cheap read drew what the session draws
    r = app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 1}, headers=H)
    assert r.status_code == 200 and r.json()["cursor"] == T("10:00")
    # the same choice again: ready, so RUNNING in the answer itself
    again = app.post("/api/sim/sessions", json=body(), headers=H).json()
    assert again["session"]["status"] == "RUNNING" and "build" not in again


def test_a_build_that_raises_turns_failed_with_a_plain_sentence(app, slow):
    slow["fail"] = True
    made = app.post("/api/sim/sessions", json=body(), headers=H).json()
    sid = made["session"]["id"]
    got = wait_status(app, sid)
    assert got["session"]["status"] == "FAILED"
    assert got["detail"] == "The session could not be prepared: the fixture bars cannot be read."
    r = app.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 1}, headers=H)
    assert r.status_code == 409 and "could not be prepared" in r.json()["detail"]
    # a new app (nothing in memory): the stored row still says FAILED, with its sentence
    app.holder.clear()
    later = TestClient(app.app).get(f"/api/sim/sessions/{sid}").json()
    assert later["session"]["status"] == "FAILED" and "could not be prepared" in later["detail"]


def test_a_newer_choice_replaces_the_waiting_build(manager, slow):
    a = manager.create(body())["session"]["id"]  # runs
    b = manager.create(body(day=DAYS[3]))["session"]["id"]  # waits (another block? no: joins)
    c = manager.create(body("supertrend_vol"))["session"]["id"]  # replaces the waiting one
    manager.builder.idle(10.0)
    status = {k: manager.entry(k).state_status for k in (a, b, c)}
    assert status[a] == "RUNNING" and status[c] == "RUNNING"
    if manager.entry(b).state_status == "FAILED":
        assert manager.entry(b).detail == (
            "A newer choice replaced this session before it was ready."
        )
    else:  # the same block as `a`: it joined a's build
        assert status[b] == "RUNNING"


def test_a_waiting_build_of_another_choice_is_replaced_and_failed(manager, slow):
    manager.create(body())
    b = manager.create(body("supertrend_vol"))["session"]["id"]
    c = manager.create(body("donchian_breakout"))["session"]["id"]
    manager.builder.idle(10.0)
    assert manager.entry(b).state_status == "FAILED"
    assert manager.entry(b).detail == "A newer choice replaced this session before it was ready."
    assert manager.entry(c).state_status == "RUNNING"


def test_ten_quick_creates_leave_one_worker_and_no_orphan(manager, slow, monkeypatch):
    slow["seconds"] = 0.05
    names = ["ema_cross", "supertrend_vol", "donchian_breakout", "mean_reversion", "squeeze"]
    before = set(threading.enumerate())
    sids = [manager.create(body(names[i % 5], at=f"10:{i:02d}"))["session"]["id"]
            for i in range(10)]  # fmt: skip

    def mine() -> list[threading.Thread]:
        return [
            t for t in threading.enumerate() if t.name == worker.THREAD_NAME and t not in before
        ]

    assert len(mine()) <= 1
    assert manager.builder.idle(10.0)
    status = [manager.entry(s).state_status for s in sids]
    assert "BUILDING" not in status and status[-1] == "RUNNING"
    assert manager.builder.threads_started == 1
    assert len(mine()) <= 1


def test_the_worker_ends_when_idle(stored, tmp_path, slow):
    slow["seconds"] = 0.0
    m = make_manager(stored, StoreBars(stored), tmp_path, build="thread")
    m.builder.idle_s = 0.05
    m.create(body())
    assert m.builder.idle(10.0)
    time.sleep(0.3)
    assert m.builder.thread is None
    m.create(body("supertrend_vol"))  # a new job starts a new worker
    assert m.builder.idle(10.0) and m.builder.threads_started == 2


def test_prepare_builds_in_the_background_and_joins(app, slow):
    qs = {"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m", "start": T("10:00")}
    r = app.get("/api/sim/prepare", params=qs).json()
    assert r["status"] == "building" and r["key"] == "RELIANCE|ema_cross|5m"
    r2 = app.get("/api/sim/prepare", params=qs).json()
    assert r2["status"] in ("building", "ready")
    app.holder["m"].builder.idle(10.0)
    r3 = app.get("/api/sim/prepare", params=qs).json()
    assert r3["status"] == "ready" and r3["ms"] >= slow["seconds"] * 1000 * 0.9
    assert slow["calls"] == 1  # the three asks shared one build
    made = app.post("/api/sim/sessions", json=body(), headers=H).json()
    assert made["session"]["status"] == "RUNNING"  # Play finds the work done


def test_a_warm_up_never_pushes_out_a_waiting_session(manager, slow):
    manager.create(body())  # runs
    waiting = manager.create(body("supertrend_vol"))["session"]["id"]
    got = manager.prepare({"instrument": "RELIANCE", "strategy": "squeeze", "timeframe": "5m",
                           "start": T("10:00")})  # fmt: skip
    assert got["status"] == "building"
    manager.builder.idle(10.0)
    assert manager.entry(waiting).state_status == "RUNNING"


def test_a_stored_session_is_read_from_its_row_while_it_is_rebuilt(stored, tmp_path, slow):
    """F5: the read of a session not in memory never waits for its rebuild."""
    slow["seconds"] = 0.0
    m = make_manager(stored, StoreBars(stored), tmp_path, build="thread")
    sid = m.create(body())["session"]["id"]
    m.builder.idle(10.0)
    s = m.get(sid)
    s.advance(100)
    s.save()
    slow["seconds"] = 0.5
    m2 = make_manager(stored, StoreBars(stored), tmp_path, build="thread")
    t0 = time.perf_counter()
    got = m2.read(sid)
    assert time.perf_counter() - t0 < 0.4
    assert got["session"]["status"] == "BUILDING" and got["session"]["cursor"] == s.cursor
    assert got["candles"] == s.candles_view()
    assert m2.options()["instruments"]  # the options never touch a session
    m2.builder.idle(10.0)
    again = m2.read(sid)
    assert again["session"]["status"] == "RUNNING" and again["session"]["cursor"] == s.cursor


def test_the_next_window_is_prepared_ahead_never_inside_an_advance(db_engine, tmp_path):
    """F6: the reload at the end of a window happens in the worker, ahead of time."""
    from .conftest import walk_minutes

    store_frames(
        db_engine,
        {
            "RELIANCE": walk_minutes("2025-01-06", 90, 1300.0, 0.0008, seed=5, volume=True),
            "NIFTY": walk_minutes("2025-01-06", 90, 25000.0, 0.0006, seed=6, volume=False),
        },
    )
    m = make_manager(db_engine, StoreBars(db_engine), tmp_path, build="thread")
    m.builder.idle_s = 0.2
    start = trading_days("2025-01-06", 90)[10]
    sid = m.create(body(day=start))["session"]["id"]
    s = m.get(sid)
    first = s.window
    assert first is not None and first.hi is not None
    crossed = False
    while not crossed:
        m.builder.idle(10.0)  # what the worker prepared happens between two advances
        builds = m.frame_cache.builds
        r = m.act(sid, lambda s: s.advance(390))
        assert not r["over"]
        if s.window != first:
            crossed = True
            assert m.frame_cache.builds == builds, "the advance built the next window itself"
    assert s.window.lo_strategy > first.lo_strategy
