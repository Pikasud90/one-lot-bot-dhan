"""US-099 points 5 and 6: the Wide list block on the page and its read-only route.

    wideBlock(view, compact)   the block of Home (whole) and of the Live Desk's Stocks card
                               (compact: no closed trades, no "closest" table)

The one-line `const name = ...;` helpers are lifted out of app.js and run in node, as the
delivery panel's tests do (skipped without node). `GET /api/delivery/wide` is read over the
real app factory with a stub engine.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from onelot.api.server import create_app
from onelot.data.store import save_bars

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wide_helpers import (
    BASE,
    START,
    Broker,
    at,
    closes,
    frame,
    source_of,
    stock,
    weekdays,
    wide_desk,
)  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "onelot" / "ui" / "app.js"
HELPERS = ["fmt", "rup", "cls", "esc", "spct", "wideTier", "wideStatus", "wideScoreRow",
           "wideRound", "wideOpenRow", "wideClosedRow", "wideAlert", "wideBlock"]  # fmt: skip
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def js(expression: str, payload: Any = None) -> Any:
    source = APP_JS.read_text(encoding="utf-8")
    lines = []
    for name in HELPERS:
        found = re.search(rf"^const {name} = .*?;$", source, re.M | re.S)
        assert found, name
        lines.append(found.group(0))
    lines += [
        "const P = JSON.parse(process.argv[1]);",
        f"console.log(JSON.stringify({expression}));",
    ]
    done = subprocess.run(
        ["node", "-e", "\n".join(lines), json.dumps(payload)],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


OPEN = [
    {"series_key": "ACC", "name": "ACC <b>Ltd</b>", "group": "alpha", "qty": 4, "tier": "cap",
     "status": "OPEN", "signal_date": "2026-11-20", "entry": 2101.5, "exit_reason": None,
     "note": None},
    {"series_key": "BEL", "name": "Bharat Electronics", "group": "new", "qty": 25, "tier": "test",
     "status": "PENDING_ENTRY", "signal_date": "2026-11-24", "entry": None, "exit_reason": None,
     "note": "recorded for the test, not alerted: the cap of 10 is full"},
]  # fmt: skip
VIEW = {
    "enabled": True, "stocks": 204, "cap_open": 10, "cap_used": 3, "desk_active": 1,
    "open_cap": 1, "open_test": 1, "note": None,
    "pending": [{"series_key": "ACC", "name": "ACC", "qty": 4, "status": "EXIT_PENDING",
                 "exit_signal_date": "2026-11-24", "exit_reason": "SMA20", "signal_date": "2026-11-20",
                 "depth_pct": 9.1}],
    "last_round": {"session": "2026-11-24", "finished_at": "2026-11-24T16:12:00+05:30",
                   "stocks": 204, "scanned": 201, "signals": 2, "alerted": 1, "recorded": 1,
                   "refetched": ["JSL"], "stale": ["IDEA"], "error_count": 1,
                   "errors": ["PEL: ConnectionError: refused"], "note": None},
    "score": {"all": {"trades": 12, "winners": 8, "net_pct": 0.91, "net": 1093.5},
              "cap": {"trades": 5, "winners": 3, "net_pct": -0.2, "net": -101.0}},
    "backtest": {"trades": 8085, "net_pct": 0.774, "winners_pct": 67.6,
                 "period": "2010-10-19 to 2026-11-05", "note": "Not proven."},
    "open": OPEN,
    "closed": [{"series_key": "DLF", "name": "DLF", "qty": 12, "tier": "test",
                "entry_session": "2026-10-01", "exit_session": "2026-10-09", "exit_reason": "STOP",
                "net_pct": -15.3, "net": -1530.0}],
    "closest": [{"series_key": "GAIL", "name": "GAIL", "group": "alpha", "session": "2026-11-24",
                 "below_high_pct": 7.4, "needed_pct": 8.0, "signal": False}],
}  # fmt: skip


@needs_node
def test_nothing_is_shown_without_the_wide_list():
    assert js("[wideBlock(null), wideBlock({}), wideBlock({enabled: false}), wideBlock(P)]", {}) == [
        "", "", "", "",
    ]  # fmt: skip


@needs_node
def test_the_block_shows_the_round_both_tiers_the_score_and_the_backtest():
    html = js("wideBlock(P, false)", VIEW)
    for text in (
        "Wide list", "204 more stocks", "3 of 10 places within the cap in use", "(1 by the desk 18)",
        "Last round: session 2026-11-24, finished 16:12", "201 of 204 stocks read",
        "2 signals (1 alerted, 1 recorded only)", "history fetched again: JSL",
        "stale, not read: IDEA", "PEL: ConnectionError: refused",
        "SELL 4 ACC", "wide list", "Every signal (all)", "Within the cap (alerted)", "+0.91%",
        "Backtest 2010-10-19 to 2026-11-05", "8085", "67.6%", "+0.77%", "within the cap",
        "recorded only", "recorded for the test, not alerted: the cap of 10 is full",
        "Closed trades", "-15.30%", "Closest to a signal", "7.40% of 8%",
    ):  # fmt: skip
        assert text in html, text
    assert "<b>Ltd</b>" not in html and "&lt;b&gt;Ltd&lt;/b&gt;" in html  # escaped


@needs_node
def test_the_compact_block_of_the_live_desk_leaves_out_the_long_tables():
    html = js("wideBlock(P, true)", VIEW)
    assert "Open trades" in html and "Closed trades" not in html and "Closest" not in html


@needs_node
def test_before_the_first_round_the_block_says_when_it_runs():
    html = js("wideBlock(P, true)", {**VIEW, "last_round": None, "open": [], "pending": []})
    assert "No evening round yet" in html and "never between 09:00 and 15:35" in html
    assert "No open wide-list trade." in html


def client_for(desk: Any) -> TestClient:
    engine = MagicMock()
    engine.desk = desk
    return TestClient(create_app(engine, "t"), raise_server_exceptions=False)


def test_the_route_answers_disabled_without_the_wide_list():
    assert client_for(SimpleNamespace(delivery=None)).get("/api/delivery/wide").json() == {
        "enabled": False
    }
    off = SimpleNamespace(delivery=SimpleNamespace(wide=None))
    assert client_for(off).get("/api/delivery/wide").json() == {"enabled": False}


def test_the_route_reads_the_wide_list_and_writes_nothing(tmp_path):
    dates = weekdays(START, BASE + 5)
    frames = {"AAA": frame(dates, closes([(BASE, -0.10), (BASE + 1, 0.03)], BASE + 5))}
    w = wide_desk([stock("AAA")], dates, source=source_of(frames), path=tmp_path / "w.db")
    for i in range(BASE - 1, BASE + 2):
        w.run_round(at(dates[i]), dates[i])
    version = w.book.version
    got = client_for(SimpleNamespace(delivery=SimpleNamespace(wide=w))).get("/api/delivery/wide")
    body = got.json()
    assert got.status_code == 200 and body["enabled"] and body["stocks"] == 1
    assert [t["series_key"] for t in body["open"]] == ["AAA"] and body["open"][0]["tier"] == "cap"
    assert body["last_round"]["session"] == dates[BASE + 1].isoformat()
    assert {"closed", "closest", "stale", "score", "backtest"} <= set(body)
    assert w.book.version == version
    # a POST is not a route here: the page only reads
    assert (
        client_for(SimpleNamespace(delivery=SimpleNamespace(wide=w)))
        .post("/api/delivery/wide")
        .status_code
        == 405
    )


@needs_node
def test_a_round_waiting_for_the_broker_shows_not_finished_and_its_note(tmp_path):
    # US-102: the round of a session the broker has not published yet, through the real desk
    dates = weekdays(START, BASE + 5)
    f = frame(dates, closes([], BASE + 5))
    broker = Broker({"AAA": f})
    broker.published = dates[BASE - 1]
    w = wide_desk(
        [stock("AAA")], dates, fetch=broker.fetch, data_api_active=lambda: True,
        sleep=lambda s: None, path=tmp_path / "w.db",
    )  # fmt: skip
    save_bars(w.engine, "AAA", "1d", f.iloc[:BASE], "dhan")
    w.maybe_round(at(dates[BASE], "15:42"))
    note = (
        f"Dhan has not published the daily bars of {dates[BASE].isoformat()} yet (checked "
        "15:42): the round looks again every 30 minutes"
    )
    html = js("wideRound(P)", w.summary()["last_round"])
    assert "not finished" in html and note in html
    assert note in js("wideBlock(P, true)", w.summary())
