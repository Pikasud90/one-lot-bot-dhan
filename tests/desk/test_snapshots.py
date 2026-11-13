"""US-071: the market snapshot (module, routes). Read-only: no order is placed."""

from __future__ import annotations

import datetime as dt
import json
import warnings
from types import SimpleNamespace

import pytest

from onelot.api.server import create_app
from onelot.desk.snapshots import NoPrices, build_rows, list_snapshots, take_snapshot
from onelot.store.db import Database as Db

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

T0 = dt.datetime(2026, 11, 13, 1, 45, 32, tzinfo=dt.timezone.utc)


def inst(key, kind, price, chg=0.5, prev=100.0):
    return {
        "key": key, "label": key.title(), "kind": kind,
        "last": {"price": price, "change_pct": chg, "prev_close": prev, "bar_ts": "2026-11-13T07:15:00+05:30"},
        "health": {"source": "dhan", "delayed": False},
    }  # fmt: skip


def desk_snap(a=100.0, b=200.0):
    return {"indices": [inst("NIFTY", "INDEX", a)], "stocks": [inst("SBIN", "STOCK", b)]}


@pytest.fixture
def db(tmp_path):
    d = Db(f"sqlite:///{tmp_path / 's.db'}")
    d.create_all()
    return d


def test_rows_follow_the_desk_order_and_fields():
    rows = build_rows(desk_snap())
    assert [r["key"] for r in rows] == ["NIFTY", "SBIN"]
    assert rows[0] == {"key": "NIFTY", "label": "Nifty", "kind": "INDEX", "price": 100.0, "prev_close": 100.0,
                       "change_pct": 0.5, "bar_ts": "2026-11-13T07:15:00+05:30", "source": "dhan", "delayed": False}  # fmt: skip


def test_change_since_previous_first_is_null_then_computed(db):
    take_snapshot(db, desk_snap(100, 200), T0)
    take_snapshot(db, desk_snap(110, 150), T0 + dt.timedelta(minutes=5))
    newest, first = list_snapshots(db)
    assert newest["id"] > first["id"]  # newest first
    assert [r["change_since_prev_pct"] for r in first["rows"]] == [None, None]
    assert [round(r["change_since_prev_pct"], 6) for r in newest["rows"]] == [10.0, -25.0]
    assert newest["taken_at"].startswith("2026-11-13T01:50:32")


def test_a_missing_price_is_null_and_strict_json(db):
    take_snapshot(db, desk_snap(100, 200), T0)
    snap = take_snapshot(db, desk_snap(float("nan"), 150), T0 + dt.timedelta(minutes=1))
    assert [r["price"] for r in snap["rows"]] == [None, 150.0]
    assert [r["change_since_prev_pct"] for r in snap["rows"]][0] is None
    json.dumps(snap, allow_nan=False)


def test_no_prices_is_refused_and_nothing_saved(db):
    with pytest.raises(NoPrices):
        take_snapshot(db, desk_snap(None, float("nan")), T0)
    assert list_snapshots(db) == []


def test_a_second_engine_lists_what_the_first_saved(tmp_path):
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    one = Db(url)
    one.create_all()
    take_snapshot(one, desk_snap(), T0)
    two = Db(url)
    assert [s["id"] for s in list_snapshots(two)] == [1]


def client(db, desk):
    engine = SimpleNamespace(db=db, desk=desk)
    return TestClient(create_app(engine, "tok"), raise_server_exceptions=False)


def test_post_needs_the_token_and_get_is_open(db):
    c = client(db, SimpleNamespace(snapshot=lambda: desk_snap()))
    assert c.post("/api/snapshots").status_code == 401
    ok = c.post("/api/snapshots", headers={"x-onelot-token": "tok"})
    assert ok.status_code == 200 and ok.json()["count"] == 2
    got = c.get("/api/snapshots?limit=30").json()
    assert [s["id"] for s in got["snapshots"]] == [ok.json()["id"]]
    assert set(got["snapshots"][0]["rows"][0]) >= {"key", "label", "kind", "price", "prev_close", "change_pct",
                                                    "bar_ts", "source", "delayed", "change_since_prev_pct"}  # fmt: skip
    json.loads(c.get("/api/snapshots").text, parse_constant=lambda x: pytest.fail(x))


def test_post_with_no_prices_is_409_and_saves_nothing(db):
    c = client(db, SimpleNamespace(snapshot=lambda: {"indices": [], "stocks": []}))
    r = c.post("/api/snapshots", headers={"x-onelot-token": "tok"})
    assert r.status_code == 409 and "no prices" in r.json()["detail"]
    assert c.get("/api/snapshots").json() == {"snapshots": []}
