"""US-095 AC2 and AC3: caches never change an answer (SIM-API-2 rule 11).

Five strategy and instrument pairs on fixture bars stored in a scratch store (`StoreBars`, so
every cache is in play) give, minute for minute and event for event, what the engine as it was
BEFORE US-095 gave (`tests/sim/golden/present_engine.json`, `tests/sim/tools/golden.py`):
built cold, built after another instrument's session, built again from warm caches, and
rebuilt from its rows half way. `until: "event"` and `until: "alert"` give the same events, in
the same minutes, as advancing minute by minute. A second create on the same choice reads no
bar again; writing bars to a series drops what was kept for it; the caps hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.data.store import save_bars
from onelot.sim.bars import StoreBars
from onelot.store import init_database

from .conftest import make_manager, store_frames

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import golden as G  # noqa: E402


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("sameness")
    db = init_database(f"sqlite:///{path / 'store.db'}")
    store_frames(db.engine, G.fixture_frames())
    yield db.engine, path
    db.dispose()


@pytest.fixture(scope="module")
def gold() -> dict:
    return json.loads(G.GOLDEN.read_text())


def fresh(store, name: str, bars: StoreBars | None = None, **kw):
    engine, path = store
    db = init_database(f"sqlite:///{path / f'{name}.db'}")
    return make_manager(db.engine, bars or StoreBars(engine), path, **kw), db


def renumber(rec: dict) -> dict:
    """Trade ids are the database's: number them by first appearance (a second session in
    one database starts after the first one's trades)."""
    rec = json.loads(json.dumps(rec))
    ids: dict[int, int] = {}

    def new(i):
        if i is None:
            return None
        return ids.setdefault(int(i), len(ids) + 1)

    for e in rec["events"]:
        if "trade_id" in e["data"]:
            e["data"]["trade_id"] = new(e["data"]["trade_id"])
    for _, trade, _ in rec["ends"]:
        if trade is not None:
            trade["id"] = new(trade["id"])
    for t in rec["trades"]:
        t["id"] = new(t["id"])
    return rec


def same(rec: dict, ref: dict) -> None:
    got = renumber(G.digest(rec))
    ref = renumber(ref)
    for k in ref:
        assert got[k] == ref[k], f"{k} differs"


def drive_new(m, pair: tuple[str, str, str], **kw) -> dict:
    s = m.get(m.create(G.body(*pair))["session"]["id"])
    return G.drive(s, **kw)


@pytest.mark.parametrize("pair", G.PRESENT, ids=lambda p: "|".join(p))
def test_cold_and_rebuilt_from_rows_equal_the_present_engine(store, gold, pair):
    ref = gold["|".join(pair)]
    m, db = fresh(store, f"cold_{pair[0]}_{pair[1]}")
    try:
        same(drive_new(m, pair), ref)
        # half way, the desk restarts: a new manager rebuilds the session from its rows
        sid = m.create(G.body(*pair))["session"]["id"]
        holder = {"s": m.get(sid), "calls": 0}

        def act(_s, kind, *args):
            holder["calls"] += 1
            if holder["calls"] == 120:
                holder["s"].save()  # what leaving memory does
                m2 = make_manager(db.engine, StoreBars(store[0]), store[1])
                holder["s"] = m2.get(sid)
            return getattr(holder["s"], kind)(*args)

        same(G.drive(holder["s"], act=act), ref)
        assert holder["calls"] > 120
    finally:
        db.dispose()


def test_after_another_instrument_and_warm_equal_the_present_engine(store, gold):
    m, db = fresh(store, "warm")
    m.frame_cache.entries = 40  # every window of the five pairs stays
    try:
        for pair in G.PRESENT:  # each after another instrument's session
            same(drive_new(m, pair), gold["|".join(pair)])
        bars = m.store_bars
        reads, builds = bars.reads, m.frame_cache.builds
        for pair in G.PRESENT:  # again: everything is warm
            same(drive_new(m, pair), gold["|".join(pair)])
        assert bars.reads == reads, "a warm create read bars again"
        assert m.frame_cache.builds == builds, "a warm create built its frame again"
    finally:
        db.dispose()


@pytest.mark.parametrize("pair", [G.PRESENT[1], G.PRESENT[4]], ids=lambda p: "|".join(p))
def test_until_event_and_alert_give_the_minute_by_minute_events(store, pair):
    m, db = fresh(store, f"until_{pair[0]}_{pair[1]}")
    try:
        n = 375 * 8
        one = drive_new(m, pair, minutes=n, chunks=(1,), close_every=0)
        for until in ("event", "alert"):
            got = drive_new(m, pair, minutes=n, chunks=(390,), until=until, close_every=0)
            a, b = renumber(got), renumber(one)
            cut = one["ends"][-1][0]  # the last minute both saw (`end` comes after it)
            ea = [e for e in a["events"] if e["ts"] <= cut]
            eb = [e for e in b["events"] if e["ts"] <= cut]
            assert ea == eb, until
            assert got["minutes"][: len(one["minutes"]) - 1] == one["minutes"][:-1]
        kinds = {e["kind"] for e in one["events"]}
        assert {"ALERT", "FILLED", "CLOSED"} <= kinds  # the comparison saw trades
    finally:
        db.dispose()


def test_until_stops_where_the_contract_says(store):
    m, db = fresh(store, "until_stops")
    try:
        pair = G.PRESENT[1]
        s = m.get(m.create(G.body(*pair))["session"]["id"])
        checked = False
        while not checked:
            r = s.advance(390, "event")
            assert not r["over"], "no alert to take"
            if not r["events"]:
                continue
            last = r["minutes"][-1][0]
            for e in r["events"]:  # every event of the answer belongs to its last minute
                t = pd.Timestamp(e["ts"]).value // 10**9
                assert t in (last, last + 60), e  # the minute's start (a fill) or its close
            if r["alert"] is not None and r["trade"] is None:
                s.take(r["alert"]["id"])
                r = s.advance(390, "alert")  # FILLED and the ratchet do not stop it
                kinds = [e["kind"] for e in r["events"]]
                assert "FILLED" in kinds
                assert kinds[-1] in ("ALERT", "EXIT_SIGNAL", "CLOSED", "CANCELLED", "DAY_END",
                                     "DATA_END"), kinds  # fmt: skip
                checked = True
    finally:
        db.dispose()


def test_writing_bars_drops_what_was_kept_for_that_series(store, tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'own.db'}")
    try:
        frames = G.fixture_frames()
        store_frames(db.engine, {"NIFTY": frames["NIFTY"]})
        m = make_manager(db.engine, StoreBars(db.engine), tmp_path)
        pair = G.PRESENT[1]
        m.get(m.create(G.body(*pair))["session"]["id"])
        bars = m.store_bars
        first = bars.minute_range("NIFTY")
        reads, builds = bars.reads, m.frame_cache.builds
        m.get(m.create(G.body(*pair))["session"]["id"])
        assert (bars.reads, m.frame_cache.builds) == (reads, builds)
        # one more day of NIFTY: its first and last, its blocks and its frames are dropped
        extra = frames["NIFTY"].iloc[-375:].copy()
        extra.index = extra.index + pd.Timedelta(days=7)
        save_bars(db.engine, "NIFTY", "1m", extra, "test")
        assert bars.minute_range("NIFTY")[1] > first[1]
        m.get(m.create(G.body(*pair))["session"]["id"])
        assert bars.reads > reads and m.frame_cache.builds == builds + 1
        assert len(m.frame_cache) >= 1
    finally:
        db.dispose()


def test_the_caps_hold(store, tmp_path):
    engine, _ = store
    bars = StoreBars(engine, cap_mb=0.5)
    first = pd.Timestamp(G.start_iso(0), tz=IST)
    for key in ("NIFTY", "BANKNIFTY", "RELIANCE"):
        bars.minute_bars(key, first, first + pd.Timedelta(days=170))
        assert bars.cached_bytes() <= 0.5 * 1024 * 1024 or len(bars._blocks) == 1
    assert len({k[0] for k in bars._blocks}) <= 2  # the oldest series went first
    reads = bars.reads
    bars.minute_bars("NIFTY", first, first + pd.Timedelta(days=5))
    assert bars.reads > reads, "an evicted block is read again"
    m, db = fresh(store, "caps", bars=StoreBars(engine))
    try:
        m.frame_cache.entries = 2
        for pair in G.PRESENT[:3]:
            m.get(m.create(G.body(*pair))["session"]["id"])
        assert len(m.frame_cache) == 2
    finally:
        db.dispose()
