"""The server review of the rebuilt Simulator (25 Nov 2026), findings F2 to F8 and R4.

* F2: the market index key is read from the universe file once per change of the file;
* F3: overlapping reads of one block of bars join one store read;
* F4: a prefetch that was dropped, replaced or failed is asked for again;
* F5: an entry evicted between its lookup and its lock is never acted on; FAILED entries do
  not pile up in the table;
* F7: the ATR timeframe of the plan is part of the frame key (two sessions that differ only
  in `stop_atr_tf` give what a cold build of each gives);
* F8: events, the open trade and the cursor are written in one transaction, and a BUILDING
  row keeps the window its create used;
* R4: the frame cache evicts the least recently USED frame and holds its byte cap.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from onelot.sim import engine as sim_engine
from onelot.sim.bars import StoreBars
from onelot.sim.frames import Frame, FrameCache, Window
from onelot.sim.worker import Builder, Job
from onelot.store import init_database

from .conftest import flat_minutes, make_manager, store_frames, trading_days

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import golden as G  # noqa: E402

DAYS = trading_days("2025-01-06", 15)


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


def body(strategy: str = "ema_cross", day=None, at: str = "10:00") -> dict:
    t = pd.Timestamp(f"{day or DAYS[-2]} {at}", tz="Asia/Kolkata").isoformat()
    return {"instrument": "RELIANCE", "strategy": strategy, "timeframe": "5m", "mode": "past",
            "start": t, "params": {}}  # fmt: skip


# ── F2 ────────────────────────────────────────────────────────────────────
def test_the_market_index_key_is_read_once_per_change_of_the_file(monkeypatch):
    calls = {"n": 0}
    real = sim_engine.market_index_key

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sim_engine, "market_index_key", counting)
    sim_engine._MKT.clear()
    first = sim_engine.market_key()
    for _ in range(50):
        assert sim_engine.market_key() == first
    assert calls["n"] == 1
    sim_engine._MKT["stamp"] = ("another file",)  # what a change of the file looks like
    assert sim_engine.market_key() == first and calls["n"] == 2


# ── F3 ────────────────────────────────────────────────────────────────────
def test_overlapping_reads_of_one_block_read_the_store_once(stored, monkeypatch):
    bars = StoreBars(stored)
    real = bars._read_block
    gate = threading.Event()

    def slow_read(key, block):
        gate.wait(5.0)
        return real(key, block)

    monkeypatch.setattr(bars, "_read_block", slow_read)
    lo = pd.Timestamp(f"{DAYS[2]} 09:15", tz="Asia/Kolkata")
    hi = lo + pd.Timedelta(hours=3)
    got: list[int] = []
    threads = [
        threading.Thread(target=lambda: got.append(len(bars.minute_bars("RELIANCE", lo, hi))))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    time.sleep(0.1)  # all eight are inside minute_bars, one of them reading
    gate.set()
    for t in threads:
        t.join(10.0)
    assert len(got) == 8 and len(set(got)) == 1 and got[0] > 0
    # the counted reads are those of `real` (one per block the window touches)
    assert bars.reads == len(bars._blocks)


def test_ten_overlapping_creates_of_one_choice_read_each_block_once(stored, tmp_path):
    alone = StoreBars(stored)  # the reads of ONE create, for reference
    one = make_manager(stored, alone, tmp_path, build="inline")
    one.create(body())
    bars = StoreBars(stored)
    m = make_manager(stored, bars, tmp_path, build="thread")
    m.builder.idle_s = 0.2
    out: list[str] = []
    threads = [
        threading.Thread(target=lambda: out.append(m.create(body())["session"]["status"]))
        for _ in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20.0)
    assert m.builder.idle(20.0)
    assert len(out) == 10
    # every block (RELIANCE and NIFTY) was read once, never once per create
    assert bars.reads == alone.reads
    assert m.frame_cache.builds == 1


# ── F4 ────────────────────────────────────────────────────────────────────
def test_a_dropped_prefetch_is_asked_for_again(db_engine, tmp_path):
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
    asked: list[Job] = []
    real = m._prefetch

    def first_dropped(setup, bars, window):
        if not asked:
            job = Job(("frame", "x"), lambda j: None, "prefetch")
            job.dropped = True  # what `Builder.submit` does while a session's build waits
            job._finish()
            asked.append(job)
            return job
        job = real(setup, bars, window)
        asked.append(job)
        return job

    m.frames.prefetch = first_dropped
    start = trading_days("2025-01-06", 90)[10]
    sid = m.create(body(day=start))["session"]["id"]
    s = m.get(sid)
    first = s.window
    crossed = False
    while not crossed:
        m.builder.idle(10.0)
        builds = m.frame_cache.builds
        r = m.act(sid, lambda s: s.advance(390))
        assert not r["over"]
        if s.window != first:
            crossed = True
            assert m.frame_cache.builds == builds, "the advance built the next window itself"
    assert len(asked) >= 2 and asked[0].dropped, "the dropped prefetch was never asked again"


def test_a_dropped_job_is_not_ok():
    b = Builder()
    gate = threading.Event()
    b.submit("a", lambda j: gate.wait(5.0), "create")
    end = time.monotonic() + 5.0
    while b.running is None and time.monotonic() < end:
        time.sleep(0.005)
    b.submit("b", lambda j: None, "create")  # waits
    warm = b.submit("c", lambda j: None, "prefetch")  # dropped: a session's build waits
    assert warm.dropped and warm.done.is_set() and not warm.ok
    gate.set()
    assert b.idle(5.0)


# ── F5 ────────────────────────────────────────────────────────────────────
def test_an_entry_evicted_before_its_lock_is_looked_up_again(stored, tmp_path):
    m = make_manager(stored, StoreBars(stored), tmp_path, build="inline")
    sid = m.create(body())["session"]["id"]
    old = m.entry(sid)
    m.act(sid, lambda s: s.advance(5))
    # the eviction happens between the lookup and the lock of the next request
    real_entry = m.entry
    evicted = {"done": False}

    def entry_then_evict(x):
        e = real_entry(x)
        if not evicted["done"]:
            evicted["done"] = True
            with e.lock:
                e.session.save()
                with m.lock:
                    m.sessions.pop(x, None)
                e.evicted = True
        return e

    m.entry = entry_then_evict  # type: ignore[method-assign]
    got = m.act(sid, lambda s: s.advance(1))
    new = m.sessions[sid]
    assert new is not old and old.evicted
    assert got["cursor"] == new.session.cursor  # the action ran on the one live copy
    assert m.read(sid)["session"]["cursor"] == new.session.cursor


def test_failed_entries_do_not_pile_up(stored, tmp_path):
    m = make_manager(stored, StoreBars(stored), tmp_path, build="inline", max_sessions=3)
    from onelot.sim.manager import Entry

    for i in range(10):
        made = Entry(m._setup(body(at=f"10:{i:02d}"), f"s_fail{i}"), "x", "FAILED")
        m._remember(made)
    m._evict()
    assert sum(1 for x in m.sessions.values() if x.status == "FAILED") == 3


# ── F7 ────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def golden_store(tmp_path_factory):
    path = tmp_path_factory.mktemp("atr_tf")
    db = init_database(f"sqlite:///{path / 'store.db'}")
    store_frames(db.engine, G.fixture_frames())
    yield db.engine, path
    db.dispose()


def _drive(m, pair, atr_tf: str) -> dict:
    b = G.body(*pair)
    b["params"] = {"stop_atr_tf": atr_tf}
    s = m.get(m.create(b)["session"]["id"])
    rec = G.digest(G.drive(s, minutes=375 * 12))
    return json.loads(json.dumps({k: rec[k] for k in ("events", "trades", "ends")}))


def _stops(rec: dict) -> list:
    """The first stop and target of every trade seen (open ones at a chunk's end too)."""
    seen = [t for _, t, _ in rec["ends"] if t is not None] + rec["trades"]
    return sorted({(t["entry_ts"], t.get("stop0"), t.get("target0")) for t in seen})


def test_the_atr_timeframe_is_part_of_the_frame_key(golden_store):
    engine, path = golden_store
    pair = G.PRESENT[0]  # NIFTY microtrend 3m: the 60m ATR differs from its own
    cold = {}
    for tf in ("60m", "own"):
        db = init_database(f"sqlite:///{path / f'cold_{tf}.db'}")
        try:
            cold[tf] = _stops(_drive(make_manager(db.engine, StoreBars(engine), path), pair, tf))
        finally:
            db.dispose()
    assert cold["60m"] and cold["60m"] != cold["own"], "the fixture does not tell them apart"
    db = init_database(f"sqlite:///{path / 'warm.db'}")
    try:
        m = make_manager(db.engine, StoreBars(engine), path)
        assert _stops(_drive(m, pair, "60m")) == cold["60m"]
        builds = m.frame_cache.builds
        assert _stops(_drive(m, pair, "own")) == cold["own"]  # never the 60m frame's ATR
        assert m.frame_cache.builds > builds, "the own-ATR session shared the 60m frame"
    finally:
        db.dispose()


# ── F8 ────────────────────────────────────────────────────────────────────
def test_events_trade_and_cursor_are_one_transaction(stored, tmp_path, monkeypatch):
    m = make_manager(stored, StoreBars(stored), tmp_path, build="inline")
    sid = m.create(body())["session"]["id"]
    s = m.get(sid)
    begins = {"n": 0}
    real = m.store.engine.begin

    def counting():
        begins["n"] += 1
        return real()

    monkeypatch.setattr(m.store.engine, "begin", counting)
    s.seq += 1
    ev = {"seq": s.seq, "_ts": pd.Timestamp(s.cursor_ns, tz="UTC").to_pydatetime(),
          "kind": "NOTE", "text": "x", "data": {}}  # fmt: skip
    s._save([ev], force=True)
    assert begins["n"] == 1


def test_a_building_row_keeps_the_window_of_its_create(stored, tmp_path):
    m = make_manager(stored, StoreBars(stored), tmp_path, build="thread")
    gate = threading.Event()
    real = sim_engine.build_signals

    def held(*a, **k):
        gate.wait(10.0)
        return real(*a, **k)

    sim_engine.build_signals = held
    try:
        made = m.create(body(day=DAYS[1], at="09:15"))
        row = m.store.session(made["session"]["id"])
        win = row["params"]["_state"]["window"]
        want = sim_engine.window_at(
            "ema_cross", sim_engine._ns_of(m._setup(body(day=DAYS[1], at="09:15"), "x").start)
        )
        assert win == [sim_engine._ts_ns(want.lo), sim_engine._ts_ns(want.lo_strategy),
                       sim_engine._ts_ns(want.hi)]  # fmt: skip
    finally:
        gate.set()
        sim_engine.build_signals = real
        m.builder.idle(10.0)


# ── R4 ────────────────────────────────────────────────────────────────────
def _frame(key: tuple, nbytes: int = 0) -> Frame:
    z = np.zeros(nbytes // 8 if nbytes else 1)
    e = np.zeros(0, np.int64)
    df = pd.DataFrame()
    w = Window(None, None, None)
    return Frame(key, w, df, e, z, z, z, z, z, e, e, None, df, e, e, {}, z, e, {})


def test_the_frame_cache_evicts_the_least_recently_used():
    c = FrameCache(entries=2, cap_mb=100.0)
    c.get(("A",), lambda: _frame(("A",)))
    c.get(("B",), lambda: _frame(("B",)))
    c.get(("A",), lambda: pytest.fail("A was cached"))  # A is now the most recently used
    c.get(("C",), lambda: _frame(("C",)))
    assert c.has(("A",)) and c.has(("C",)) and not c.has(("B",))


def test_the_frame_cache_holds_its_byte_cap():
    c = FrameCache(entries=10, cap_mb=1.0)
    for k in "ABCDEF":
        c.get((k,), lambda k=k: _frame((k,), 40_000))  # about 0.23 MB each
    assert c.cached_bytes() <= 1024 * 1024
    assert c.has(("F",)) and not c.has(("A",)) and len(c) < 6
