"""US-077: GET /api/mllab/states and /instruments: shape, strict JSON, 400, the cache."""

from __future__ import annotations

import datetime as dt
import json
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from onelot.api.server import create_app
from onelot.mllab import states as st

from .test_mllab_service import Clock, minute_bars, sessions

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient


def daily_ohlc(days: int, seed: int) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [d + pd.Timedelta(hours=9, minutes=15) for d in sessions("2026-11-12", days)]
    )
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.012, days)))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * (1 + rng.uniform(0, 0.01, days)),
            "low": close * (1 - rng.uniform(0, 0.01, days)),
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )


def tracker(key: str, kind: str, seed: int, days: int = 400) -> SimpleNamespace:
    return SimpleNamespace(
        bars=minute_bars(days=3, seed=seed),
        daily_history=daily_ohlc(days, seed) if days else None,
        series=SimpleNamespace(key=key, label=key.title()),
        cfg=SimpleNamespace(kind=kind),
    )


@pytest.fixture(scope="module")
def client() -> TestClient:
    desk = SimpleNamespace(
        trackers={
            "RELIANCE": tracker("RELIANCE", "STOCK", 1),
            "NIFTY": tracker("NIFTY", "INDEX", 2),
            "SHORT": tracker("SHORT", "STOCK", 3, days=15),
            "EMPTY": tracker("EMPTY", "STOCK", 4, days=0),
        }
    )
    engine = MagicMock()
    engine.desk = desk
    return TestClient(create_app(engine, "test-token"), raise_server_exceptions=False)


def strict(text: str):
    return json.loads(text, parse_constant=lambda c: pytest.fail(f"non-strict JSON: {c}"))


def test_the_instruments_list(client):
    r = client.get("/api/mllab/states/instruments")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    rows = strict(r.text)["instruments"]
    assert [x["key"] for x in rows] == ["NIFTY", "RELIANCE", "SHORT", "EMPTY"]  # indices first
    assert set(rows[0]) == {"key", "label", "kind"}


def test_the_states_shape(client):
    r = client.get("/api/mllab/states?instrument=RELIANCE")
    assert r.status_code == 200
    body = strict(r.text)
    assert set(body) >= {
        "instrument", "as_of", "sessions", "base", "next", "size_next", "first_order",
        "second_order", "runs", "walk_forward", "note",
    }  # fmt: skip
    assert body["instrument"] == {"key": "RELIANCE", "label": "Reliance", "kind": "STOCK"}
    assert body["as_of"] == "2026-11-12" and len(body["sessions"]) == 60
    assert set(body["next"]) == {
        "context",
        "order",
        "n",
        "probs",
        "lean",
        "edge_pts",
        "z",
        "conviction",
        "score",
    }
    assert set(body["size_next"]) == {
        "context",
        "n",
        "p_active",
        "base_active",
        "lean",
        "edge_pts",
        "z",
        "conviction",
        "score",
    }
    assert set(body["next"]["probs"]) == {"bull", "flat", "bear"}
    assert body["walk_forward"]["n"] == 400 - 21 - st.WALK_START
    assert len(body["runs"]) == 6


def test_unknown_instrument_is_400(client):
    assert client.get("/api/mllab/states?instrument=NOPE").status_code == 400
    assert client.get("/api/mllab/states").status_code == 400


def test_little_or_no_history_gives_a_note_not_an_error(client):
    for key in ("SHORT", "EMPTY"):
        r = client.get(f"/api/mllab/states?instrument={key}")
        assert r.status_code == 200
        body = strict(r.text)
        assert body["note"] and body["sessions"] == [] and body["as_of"] is None


class TestCache:
    def lab(self, data):
        bars = {"A": (None, data)}
        return st.MarketStates(
            lambda k: bars[k],
            [{"key": "A", "label": "A", "kind": "INDEX"}],
            Clock("2026-11-13 18:00"),
        )

    def test_one_computation_until_the_last_daily_bar_changes(self):
        data = daily_ohlc(400, 9)
        lab = self.lab(data)
        a = lab.reading("A")
        b = lab.reading("A")
        assert lab.runs == 1 and a is b
        lab.bars_getter = lambda k: (None, data.iloc[:-1])  # a bar fewer: a new reading
        c = lab.reading("A")
        assert lab.runs == 2 and c["as_of"] != a["as_of"]

    def test_the_session_in_progress_is_not_used(self):
        data = daily_ohlc(400, 9)
        lab = st.MarketStates(
            lambda k: (None, data),
            [{"key": "A", "label": "A", "kind": "INDEX"}],
            lambda: dt.datetime(2026, 11, 12, 11, 0, tzinfo=data.index.tz),  # before the close
        )
        assert lab.reading("A")["as_of"] == "2026-11-11"


def test_thin_flag_and_size_words_over_the_api(client):
    short = strict(client.get("/api/mllab/states?instrument=SHORT").text)
    assert short["thin"] is True
    full = strict(client.get("/api/mllab/states?instrument=RELIANCE").text)
    assert full["thin"] is False
    assert {s["size"] for s in full["sessions"]} <= {"calm", "active"}
    assert {s["size"] for s in full["sessions"]} == {"calm", "active"}
