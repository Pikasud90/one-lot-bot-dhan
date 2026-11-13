"""US-069: GET /api/mllab: its shape, the 400 on an unknown timeframe, strict JSON."""

from __future__ import annotations

import json
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from onelot.api.server import create_app
from onelot.mllab import models as m

from .test_mllab_service import daily_bars, minute_bars

with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient


def tracker(key: str, kind: str, seed: int) -> SimpleNamespace:
    return SimpleNamespace(
        bars=minute_bars(days=20, seed=seed),
        daily_history=daily_bars(days=300, seed=seed),
        series=SimpleNamespace(key=key, label=key.title()),
        cfg=SimpleNamespace(kind=kind),
    )


@pytest.fixture(scope="module")
def client() -> TestClient:
    desk = SimpleNamespace(
        trackers={
            "RELIANCE": tracker("RELIANCE", "STOCK", 1),
            "NIFTY": tracker("NIFTY", "INDEX", 2),
            "EMPTY": SimpleNamespace(
                bars=minute_bars(days=1).iloc[:0],
                daily_history=None,
                series=SimpleNamespace(key="EMPTY", label="Empty"),
                cfg=SimpleNamespace(kind="STOCK"),
            ),
        }
    )
    engine = MagicMock()
    engine.desk = desk
    return TestClient(create_app(engine, "test-token"), raise_server_exceptions=False)


@pytest.mark.parametrize("tf", ["15m", "30m", "60m", "4h", "1d"])
def test_the_matrix_shape(client, tf):
    r = client.get(f"/api/mllab?timeframe={tf}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = json.loads(r.text, parse_constant=lambda c: pytest.fail(f"non-strict JSON: {c}"))
    assert set(body) == {"timeframe", "generated_at", "models", "rows"}
    assert body["timeframe"] == tf and [x["key"] for x in body["models"]] == list(m.MODELS)
    assert [row["key"] for row in body["rows"]] == ["NIFTY", "RELIANCE", "EMPTY"]  # indices first
    for row in body["rows"]:
        assert set(row) == {
            "key",
            "label",
            "kind",
            "last_close",
            "last_bar_ts",
            "next_bar_ts",
            "cells",
        }
        assert set(row["cells"]) == set(m.MODELS)
    nifty = body["rows"][0]["cells"]["ewma"]  # 20 sessions: 4h has 39 bars, too few for arima
    assert {"mean", "sd", "p_up", "lo68", "hi68", "lo95", "hi95", "n", "note"} <= set(nifty)
    assert set(nifty["backtest"]) == {"steps", "in68", "in95", "direction"}
    assert body["rows"][2]["cells"]["garch"]["note"] == "no closed bars yet"


def test_the_default_is_15m(client):
    assert client.get("/api/mllab").json()["timeframe"] == "15m"


@pytest.mark.parametrize("tf", ["5m", "1m", "2h", "", "1D"])
def test_an_unknown_timeframe_is_a_400(client, tf):
    r = client.get(f"/api/mllab?timeframe={tf}")
    assert r.status_code == 400 and "timeframe must be one of" in r.json()["detail"]


def test_without_a_desk_it_is_a_503():
    engine = MagicMock()
    engine.desk = None
    c = TestClient(create_app(engine, "t"), raise_server_exceptions=False)
    assert c.get("/api/mllab?timeframe=15m").status_code == 503


def test_the_route_is_a_read():
    routes = [
        (r.path, sorted(r.methods))
        for r in create_app(MagicMock(), "t").routes
        if getattr(r, "path", "") == "/api/mllab"
    ]
    assert routes == [("/api/mllab", ["GET"])]
