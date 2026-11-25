"""A new Simulator session starts with a clean chart (owner's report of 25 November 2026).

After a NIFTY session with an alert or a trade, a RELIANCE session kept the index's stop,
entry and target: the price scale ran from -2,218 to 30,720 and the share's candles got 2 of
430 pixels. `resetRun` emptied `S.lineKey` to "" and `syncLines` then saw the same empty key
and returned before it dropped the old levels (audit finding SIM-01).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SIM_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "sim.js"
NODE = shutil.which("node")


def function_source(name: str) -> str:
    text = SIM_JS.read_text(encoding="utf-8")
    found = re.search(rf"^function {name}\(.*?^}}$", text, re.S | re.M)
    assert found, f"function {name} not found at the top level of sim.js"
    return found.group(0)


def test_a_new_session_drops_every_level_of_the_old_one_in_the_source():
    body = function_source("resetRun")
    assert "clearTradeLines()" in body
    assert "S.lineKey = null" in body  # a key that never equals a computed one
    cleared = function_source("clearTradeLines")
    for needed in ("S.scaleLevels = null", "removePriceLine", "setMarkers([])", "autoScale: true"):
        assert needed in cleared


def test_start_asks_before_it_closes_an_open_trade_and_says_it_is_loading():
    text = SIM_JS.read_text(encoding="utf-8")
    assert "A trade is open in this session." in text
    assert 'go.textContent = "Loading…"' in text
    assert '!OL.tabShown("sim")' in function_source("tick")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_new_session_drops_every_level_of_the_old_one_when_run():
    script = (
        """
const calls = [];
const S = { scaleLevels: [23552.5, 23697.6, 24132.8], lines: ["stop", "entry", "target"],
  lineKey: "t|23697.6|23552.5|24132.8||", trade: null, alert: null,
  series: { removePriceLine: (l) => calls.push("rm:" + l), createPriceLine: (o) => o },
  marks: { setMarkers: (m) => calls.push("marks:" + m.length) },
  chart: { priceScale: () => ({ applyOptions: (o) => calls.push("auto:" + o.autoScale) }) } };
const cssv = () => "x";
"""
        + function_source("clearTradeLines")
        + "\n"
        + function_source("syncLines")
        + """
S.lineKey = null; clearTradeLines();          // what resetRun does for the next session
const after = { levels: S.scaleLevels, lines: S.lines.length, calls: calls.slice() };
syncLines();                                  // the new session has no alert and no trade yet
console.log(JSON.stringify({ after, key: S.lineKey, levels: S.scaleLevels, lines: S.lines.length }));
"""
    )
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["after"]["levels"] is None
    assert got["after"]["lines"] == 0
    for call in ("rm:stop", "rm:entry", "rm:target", "marks:0", "auto:true"):
        assert call in got["after"]["calls"]
    assert got["key"] == "" and got["levels"] is None and got["lines"] == 0
