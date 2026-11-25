"""A new Simulator session starts with a clean chart (owner's report of 25 November 2026).

After a NIFTY session with an alert or a trade, a RELIANCE session kept the index's stop,
entry and target: the price scale ran from -2,218 to 30,720 and the share's candles got 2 of
430 pixels (audit finding SIM-01). The page was rebuilt (US-094); these tests protect the same
four things on the rebuilt page:

1. no level, line or marker of an earlier session survives a switch;
2. a change asks before it closes an open trade;
3. the wait is said (a loading veil at once, the build's step while a session is built);
4. the replay waits while another tab is shown.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .test_sim_ui import DAY1, DAY2, OPEN_TRADE, needs_node, run_chart

SIM_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "sim.js"


def function_source(name: str) -> str:
    text = SIM_JS.read_text(encoding="utf-8")
    found = re.search(rf"^(?:async )?function {name}\(.*?^}}$", text, re.S | re.M)
    assert found, f"function {name} not found at the top level of sim.js"
    return found.group(0)


def test_a_switch_drops_the_old_session_and_resets_the_chart_before_anything_is_awaited():
    choose = function_source("choose")
    assert choose.index("dropSession()") < choose.index("loadPicture()")
    dropped = function_source("dropSession")
    for needed in (
        "S.alert = null",
        "S.trade = null",
        "S.markers = []",
        "S.overlays = {}",
        "S.events = []",
    ):
        assert needed in dropped
    load = function_source("loadPicture")
    assert load.index("S.chart.reset()") < load.index("await ")


@needs_node
def test_no_level_line_marker_or_zone_of_a_nifty_trade_survives_a_switch_to_reliance():
    out = run_chart(
        f"""const {{ LW, log }} = makeLW(); const ch = SC.create({{}}, LW, {{}});
        const nifty = []; for (let i = 0; i < 300; i++) nifty.push([{DAY1} + 180*i, 24000 + i, 24010 + i, 23990 + i, 24005 + i, 0]);
        ch.load({{ key: 'NIFTY', label: 'NIFTY 50', kind: 'INDEX', tick: 0.05 }}, '3m', 3, nifty, null);
        const trade = {json.dumps(OPEN_TRADE)};
        ch.setLevels(trade, 'trade', C.levelLabels(trade, 'trade'));
        ch.setZones({{ minute: {DAY1} + 180*290, entry: trade.entry, stop: trade.stop, target: trade.target }});
        ch.setMarkers([C.markerFor({{kind: 'FILLED', ts: '2026-10-05T09:19:00+05:30', data: {{entry: 25018}}}}, 'LONG')]);
        const before = log.lines.filter(l => l.o.lineVisible).length;
        ch.reset();
        const rel = []; for (let i = 0; i < 300; i++) rel.push([{DAY2} + 300*i, 1150, 1152, 1149, 1151, 5000]);
        ch.load({{ key: 'RELIANCE', label: 'Reliance', kind: 'STOCK', tick: 0.1 }}, '5m', 5, rel, null);
        const r = log.series[0].opts.autoscaleInfoProvider(() => ({{ priceRange: {{ minValue: 1149, maxValue: 1152 }} }}));
        return {{ before, lines: log.lines.filter(l => l.o.lineVisible || l.o.axisLabelVisible).length,
          markers: log.markers.length, levels: ch.levels, zones: ch.zones.get(), pinned: ch.pinned.length, range: r.priceRange }};"""
    )
    assert out["before"] >= 3
    assert out["lines"] == 0 and out["markers"] == 0 and out["levels"] is None
    assert out["zones"] is None and out["pinned"] == 0
    assert out["range"] == {"minValue": 1149, "maxValue": 1152}


def test_a_change_asks_before_it_closes_an_open_trade():
    choose = function_source("choose")
    assert "tradeOpen()" in choose and "await confirm(" in choose
    assert "Keep the trade" in choose
    assert choose.index("await confirm(") < choose.index("endOld()")


def test_the_wait_is_said():
    load = function_source("loadPicture")
    assert load.index("veil(`Loading") < load.index("await ")
    assert "sim-build" in function_source("showBuild")


def test_the_replay_waits_while_another_tab_is_shown():
    frame = function_source("frame")
    assert '!OL.tabShown("sim")' in frame and "setPlaying(false" in frame
    assert "hashchange" in SIM_JS.read_text(encoding="utf-8")
