"""US-070: Home's "Today" strip, the 1-7 tab keys, and the captions that no longer hold.

The one-line `const name = ...;` helpers are lifted out of app.js and run in node, as the
delivery and trend-ladder tests do. Skipped without node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
APP_JS = UI / "app.js"
HELPERS = ["esc", "hhmm", "hms", "delayNote", "trendRows", "trendVerdict", "tokenHoursLeft",
           "todayLines", "todayStrip", "tabForKey"]  # fmt: skip
NOW = "2026-11-13T10:00:00+05:30"

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
        f"const NOW = new Date({json.dumps(NOW)});",
        f"console.log(JSON.stringify({expression}));",
    ]
    done = subprocess.run(
        ["node", "-e", "\n".join(lines), json.dumps(payload)],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def trend(*dirs: int) -> dict:
    return {"rows": [{"timeframe": tf, "direction": d, "trigger": i == 0}
                     for i, (tf, d) in enumerate(zip(["3m", "5m", "10m", "15m", "30m", "60m", "4h"], dirs, strict=False))]}  # fmt: skip


SNAP = {
    "updated_at": "2026-11-13T09:59:58+05:30",
    "session": {"trading_day": True, "open": True, "entries_allowed": True,
                "open_at": "2026-11-13T09:15:00+05:30", "close_at": "2026-11-13T15:30:00+05:30"},
    "source": {"name": "dhan", "delayed": False, "delayed_keys": [], "last_error": None},
    "indices": [{"key": "NIFTY", "label": "NIFTY", "trend": trend(1, 1, 1, 1, 1, 1, 1)},
                {"key": "BANKNIFTY", "label": "BANKNIFTY", "trend": trend(1, -1, 1, 1, 1, 1, 1)}],
    "positions": [{"label": "NIFTY 24500 CE"}],
    "delivery": {"enabled": True, "pending": [{"series_key": "SBIN"}], "open": [],
                 "history_to": "2026-11-12", "stale": []},
}  # fmt: skip
TOKEN_OK = "13/11/2026 20:00"  # ten hours after NOW


@needs_node
def test_the_strip_has_a_line_for_each_thing_the_owner_needs():
    lines = dict((k, [v, c]) for k, v, c in js("todayLines(P[0], P[1], NOW)", [SNAP, TOKEN_OK]))
    assert lines["Session"][0] == "open · closes 15:30"
    assert lines["Feed"] == ["live (dhan)", "ok"]
    assert lines["Dhan token"] == ["10 h 0 min left", "ok"]
    assert (
        lines["Options · micro-trend"][0] == "1 of 2 aligned: NIFTY up · open trade: NIFTY 24500 CE"
    )
    assert lines["Stocks · pullback delivery"][0] == (
        "1 pending alert · 0 open delivery trades · daily history complete to 2026-11-12"
    )
    assert lines["Next"] == ["nothing to do: the desk is watching", "ok"]


@needs_node
def test_a_short_token_a_stale_history_and_a_delayed_feed_say_what_to_do():
    snap = {**SNAP, "session": {**SNAP["session"], "open": False, "open_at": "2026-11-13T11:15:00+05:30"},
            "source": {"name": "yahoo", "delayed": True, "delayed_keys": ["NIFTY"], "last_error": None},
            "indices": [{"key": "NIFTY", "label": "NIFTY", "trend": trend(1, -1, 0, 1, 1, 1, 1)}], "positions": [],
            "delivery": {**SNAP["delivery"], "stale": ["SBIN", "TCS"]}}  # fmt: skip
    lines = dict(
        (k, [v, c]) for k, v, c in js("todayLines(P[0], P[1], NOW)", [snap, "13/11/2026 11:00"])
    )
    assert lines["Session"][0] == "closed · opens at 11:15"
    assert lines["Feed"] == ["delayed ~15 min: NIFTY (yahoo bars)", "warn"]
    assert lines["Dhan token"] == ["1 h 0 min left", "bad"]
    assert lines["Options · micro-trend"][0] == "no alignment · no open trade"
    assert (
        "Renew the Dhan token" in lines["Next"][0]
        and "daily history is stale (SBIN, TCS)" in lines["Next"][0]
    )
    expired = dict(
        (k, [v, c]) for k, v, c in js("todayLines(P[0], P[1], NOW)", [SNAP, "13/11/2026 09:00"])
    )
    assert expired["Dhan token"] == ["EXPIRED", "bad"]


@needs_node
def test_the_strip_escapes_and_survives_no_desk():
    html = js(
        "todayStrip(P, null, NOW)", {**SNAP, "source": {"name": "<b>x</b>", "delayed": False}}
    )
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert "time left unknown" in html
    assert js("todayLines(null, null, NOW)") == [["Desk", "the Live Desk is not running", "warn"]]


@needs_node
def test_number_keys_switch_tabs_but_not_while_typing():
    tabs = ["home", "live", "lab", "data", "news", "mllab", "settings"]
    assert js("P.map(k => tabForKey(k, 'BODY', false, false, " + json.dumps(tabs) + "))",
              ["1", "2", "7", "8", "a"]) == ["home", "live", "settings", None, None]  # fmt: skip
    for tag, editable, mods in [("INPUT", False, False), ("select", False, False), ("TEXTAREA", False, False),
                                ("DIV", True, False), ("BODY", False, True)]:  # fmt: skip
        assert (
            js(
                f"tabForKey('2', {json.dumps(tag)}, {json.dumps(editable)}, {json.dumps(mods)}, P)",
                tabs,
            )
            is None
        )


def test_the_today_strip_sits_at_the_very_top_of_home():
    html = (UI / "index.html").read_text(encoding="utf-8")
    home = html.split('<section data-tab="home" class="tab">', 1)[1]
    assert home.index('id="today-strip"') < home.index('id="acct-tiles"')
    assert 'id="desk-updated"' in html and 'id="trend-legend"' in html and "3m trigger" in html


def test_the_captions_that_contradict_the_desk_are_gone():
    html = (UI / "index.html").read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    for gone in ["buy / sell · 1 share · intraday", "one unit each", "every alert opens one · one unit ·",
                 "one unit per trade, always", "bullish → buy CE", "primary strategies ·"]:  # fmt: skip
        assert gone not in html, gone
    assert "1 share · intraday" not in app
    assert '<span class="num" style="font-size:15px">1 share</span>' not in app
    assert (
        "Daily loss limit"
        not in app.split("function accRow", 1)[1].split("function renderDesk", 1)[0]
    )
    assert (
        "<h4>Risk governor</h4>" not in app.split("function renderFocus", 1)[1].split("\n}\n", 1)[0]
    )
    assert "One unit per trade, every alert" not in app
