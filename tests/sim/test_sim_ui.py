"""US-094: the Simulator page, rebuilt as a replay workspace.

The pure helpers (sim_core.js) and the chart controller (sim_chart.js, with a stub of the chart library) are loaded
into node with require(); the fake of the engine (sim_fake.js) is driven through its handle(); the static rules (no
colour literal, no type under 14px, every id the script uses exists, the fake loaded only on request, all five themes)
are read from the files. The node tests are skipped without node; the static tests always run."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
SIM_JS, SIM_CSS, SIM_FAKE, SIM_CORE, SIM_CHART, INDEX, STYLES = (
    UI / "sim.js",
    UI / "sim.css",
    UI / "sim_fake.js",
    UI / "sim_core.js",
    UI / "sim_chart.js",
    UI / "index.html",
    UI / "styles.css",
)
PAGE_SCRIPTS = (SIM_JS, SIM_CORE, SIM_CHART)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# A day starts at 09:15 IST = 03:45 UTC. 2026-10-05 is a Monday.
DAY1 = 1791171900  # 2026-10-05 09:15 IST
DAY2 = DAY1 + 86400


def _node(script: str) -> Any:
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def run(body: str) -> Any:
    """Run `body` with the helpers of sim_core.js in scope (C and every name it exports)."""
    script = (
        f"const C = require({json.dumps(str(SIM_CORE))});\n"
        "const {" + ", ".join(sorted(CORE_NAMES)) + "} = C;\n"
        f"(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    return _node(script)


def run_fake(body: str) -> Any:
    script = (
        f"const fake = require({json.dumps(str(SIM_FAKE))});\n"
        f"(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    return _node(script)


CORE_NAMES = {
    "binStart", "binEnd", "mergeBar", "foldMinutes", "minutePath", "partialMinute", "playMinutes", "minuteSeconds",
    "speedInForce", "chunkSize", "speedStep", "tfStep", "stepMinutes", "clockText", "readyText", "countdownText",
    "eventMinute", "stageText", "reasonText", "strategiesFor", "defaultDay", "snapDay", "shiftDay", "randomDay",
    "startIsoOf", "minuteBefore", "resumeLine", "scaleRule", "pinnedText", "legendParts", "legendLine", "keyAction",
    "ticketModel", "positionModel", "resultModel", "numbersModel", "tradeCells", "eventCells", "levelLabels",
    "markerFor", "equityPath", "money", "rupees", "num", "volText", "tfMinutes", "dayText", "marketOpenAt",
}  # fmt: skip

ALERT = {
    "id": "a_17", "ts": "2026-10-05T09:18:00+05:30", "expires": "2026-10-05T09:28:00+05:30", "side": "LONG",
    "action": "BUY CALL", "entry": 25016.2, "stop": 24970.0, "target": 25154.8, "risk": 46.2, "reward": 138.6,
    "qty": 75, "lot": 75, "unit": "lot", "expires_in_min": 9,
    "traded": {"what": "NIFTY 25000 CE 28 Oct", "price": 412.5, "source": "real", "stop_price": 382.0, "target_price": 502.0},
    "risk_inr": 2287.5, "reward_inr": 6712.5, "costs_inr": 92.4, "reasons": ["all seven trends up", '<b>bold</b> & "quoted"'],
}  # fmt: skip
STOCK_ALERT = {
    "id": "a_3", "side": "LONG", "action": "BUY", "entry": 1412.4, "stop": 1398.0, "target": 1455.6, "qty": 1, "lot": 1,
    "unit": "share", "expires_in_min": 4,
    "traded": {"what": "Reliance Industries", "price": 1412.4, "source": "real", "stop_price": 1398.0, "target_price": 1455.6},
    "risk_inr": 14.4, "reward_inr": 43.2, "costs_inr": 18.6, "reasons": [],
}  # fmt: skip
OPEN_TRADE = {
    "id": 41, "status": "OPEN", "side": "LONG", "action": "BUY CALL", "qty": 75, "lot": 75, "unit": "lot",
    "entry_ts": "2026-10-05T09:19:00+05:30", "entry": 25018.0, "entry_price": 413.2, "source": "real",
    "stop": 25031.5, "target": 25154.8, "stop0": 24970.0, "target0": 25154.8, "stop_price": 420.1, "target_price": 482.0,
    "risk_inr": 2310.0, "open_r": 0.58, "minutes_in_trade": 12, "stage": "BREAKEVEN", "best": 25055.0, "progress": 0.27,
    "mark": 25049.0, "mark_price": 431.0, "open_pnl": 1335.0, "exit_ts": None, "exit": None, "exit_price": None,
    "exit_reason": None, "gross": None, "charges": None, "net": None, "r_multiple": None,
    "traded": {"what": "NIFTY 25000 CE 28 Oct", "source": "real"},
}  # fmt: skip
STOCK_TRADE = {
    **OPEN_TRADE, "action": "BUY", "qty": 1, "lot": 1, "unit": "share", "entry": 1412.4, "entry_price": 1412.4,
    "stop": 1398.0, "target": 1455.6, "stop0": 1398.0, "target0": 1455.6, "stop_price": 1398.0, "target_price": 1455.6,
    "mark": 1420.0, "mark_price": 1420.0, "open_pnl": 7.6, "risk_inr": 14.4, "stage": "FIRST_STOP",
    "traded": {"what": "Reliance Industries", "source": "real"},
}  # fmt: skip


# ══ candles ═══════════════════════════════════════════════════════════════════════════════
@needs_node
class TestCandles:
    def test_bins_are_anchored_at_0915_for_3m_15m_and_60m(self):
        out = run(
            f"return [[3,15,60].map(tf => [{DAY1}, {DAY1}+60*7, {DAY1}+60*20, {DAY1}+60*200].map(u => (binStart(u, tf) - {DAY1}) / 60))];"
        )
        assert out == [[[0, 6, 18, 198], [0, 0, 15, 195], [0, 0, 0, 180]]]

    def test_a_one_minute_bar_before_the_open_still_gets_a_bin(self):
        assert run(f"return binStart({DAY1} - 60, 15);") == DAY1 - 15 * 60

    def test_minutes_fold_into_one_candle_with_high_low_and_volume(self):
        out = run(
            f"return foldMinutes([], [[{DAY1},10,12,9,11,1],[{DAY1}+60,11,15,10,14,2],[{DAY1}+120,14,14,8,9,3]], 3);"
        )
        assert out == [[DAY1, 10, 15, 8, 9, 6]]

    def test_candles_roll_over_a_day_boundary(self):
        out = run(
            f"""const m = [[{DAY1}+60*374,5,6,4,5.5,1],[{DAY2},7,8,6,7.5,1]];
            return [foldMinutes([], m, 3).map(c => c[0]), foldMinutes([], m, 60).map(c => c[0])];"""
        )
        assert out == [[DAY1 + 372 * 60, DAY2], [DAY1 + 360 * 60, DAY2]]

    def test_every_chart_timeframe_builds_a_whole_day_into_the_right_number_of_candles(self):
        out = run(
            f"""const m = []; for (let i = 0; i < 375; i++) m.push([{DAY1} + 60*i, 1, 2, 0.5, 1.5, 1]);
            return [1,3,5,10,15,30,60].map(tf => {{ const c = foldMinutes([], m, tf); return [c.length, c[c.length-1][5], c.reduce((a, x) => a + x[5], 0)]; }});"""
        )
        # 375 minutes: 1m 375, 3m 125, 5m 75, 10m 38 (last 5), 15m 25, 30m 13 (last 15), 60m 7 (last 15); no minute lost
        assert out == [[375, 1, 375], [125, 3, 375], [75, 5, 375], [38, 5, 375], [25, 15, 375], [13, 15, 375], [7, 15, 375]]  # fmt: skip

    def test_merging_into_the_forming_candle_keeps_its_open(self):
        out = run(
            f"return mergeBar([{DAY1}, 10, 11, 9, 10.5, 4], [{DAY1}+60, 10.5, 13, 10, 12, 2], 3);"
        )
        assert out == [DAY1, 10, 13, 9, 12, 6]

    def test_a_candle_closes_at_its_bin_end_or_at_1530(self):
        out = run(
            f"return [binEnd({DAY1}+60, 3) - {DAY1}, binEnd({DAY1}+360*60+30, 60) - {DAY1}, countdownText({DAY1}+150, 3), countdownText({DAY1}+180, 3), countdownText({DAY1}-60, 3)];"
        )
        assert out == [180, 375 * 60, "0:30", "3:00", ""]


# ══ the drawn path of a minute and the clock ═══════════════════════════════════════════════
@needs_node
class TestClock:
    def test_an_up_minute_goes_open_low_high_close_and_a_down_minute_open_high_low_close(self):
        assert run("return [minutePath([0, 10, 14, 8, 13]), minutePath([0, 10, 14, 8, 9])];") == [
            [10, 8, 14, 13],
            [10, 14, 8, 9],
        ]

    def test_the_drawn_minute_never_leaves_the_bar_and_ends_on_the_real_close(self):
        out = run(
            """const b = [0, 10, 14, 8, 13, 300];
            const steps = []; for (let i = 0; i <= 60; i++) steps.push(partialMinute(b, i / 60));
            return [steps[0], steps[60], steps.every(s => s[2] <= 14 && s[3] >= 8 && s[4] <= 14 && s[4] >= 8 && s[1] === 10), partialMinute(b, 1/3)];"""
        )
        assert out[0] == [0, 10, 10, 10, 10, 0]
        assert out[1] == [0, 10, 14, 8, 13, 300]
        assert out[2] is True
        assert out[3] == [0, 10, 10, 8, 8, 100]

    def test_one_market_minute_takes_sixty_over_speed_real_seconds_at_every_speed(self):
        out = run(
            "return [1, 2, 5, 10, 60, 300].map(s => [minuteSeconds(s), playMinutes(minuteSeconds(s) * 1000, s), playMinutes(1000, s)]);"
        )
        assert [x[0] for x in out] == [60, 30, 12, 6, 1, 0.2]
        assert all(
            abs(x[1] - 1) < 1e-9 for x in out
        )  # the minute's own real time reveals exactly one minute
        assert [round(x[2], 4) for x in out] == [0.0167, 0.0333, 0.0833, 0.1667, 1, 5]

    def test_the_chunk_is_one_second_of_play_with_an_alert_or_trade_else_three_from_1_to_30(self):
        out = run(
            "return [1, 2, 5, 10, 60, 300].map(s => [chunkSize(s, false), chunkSize(s, true)]).concat([[chunkSize(900, false), chunkSize(0, false)]]);"
        )
        assert out == [[1, 1], [1, 1], [1, 1], [1, 1], [3, 1], [15, 5], [30, 1]]

    def test_a_trade_holds_the_speed_to_ten_x_when_asked(self):
        assert run(
            "return [speedInForce(300, true, true), speedInForce(300, true, false), speedInForce(300, false, true), speedInForce(5, true, true)];"
        ) == [10, 300, 300, 5]

    def test_up_and_down_step_through_the_speeds_and_brackets_through_the_chart_timeframes(self):
        out = run(
            "const sp = [1,2,5,10,60,300]; return [speedStep(sp, 10, 1), speedStep(sp, 10, -1), speedStep(sp, 300, 1), speedStep(sp, 1, -1), speedStep(sp, 7, 1), tfStep(null, '3m', 1), tfStep(null, '1m', -1), tfStep(null, '60m', 1)];"
        )
        assert out == [60, 5, 300, 1, 10, "5m", "1m", "60m"]

    def test_shift_right_goes_to_the_close_of_the_chart_candle(self):
        out = run(
            f"return [[{DAY1}-60, 3], [{DAY1}, 3], [{DAY1}+60, 1], [{DAY1}+60*2, 3], [{DAY1}+60*374, 3], [{DAY1}+60*373, 1]].map(([u, tf]) => stepMinutes(u, tf));"
        )
        assert out == [3, 2, 1, 3, 3, 1]

    def test_the_clock_reads_indian_time_and_ready_before_the_first_minute(self):
        out = run(
            f"return [clockText({DAY1}), readyText({DAY1}), clockText({DAY1}, {{hidden: true}}), dayText('2026-11-03')];"
        )
        assert out == [
            "Mon 05 Oct 09:15",
            "Ready: Mon 05 Oct 09:15",
            "A hidden day 09:15",
            "Tue 03 Nov 2026",
        ]

    def test_every_event_is_put_at_its_own_minute(self):
        out = run(
            """const ev = (kind, ts, data) => ({kind, ts, data: data || {}});
            const u = (s) => Math.floor(Date.parse(s) / 1000);
            return [ev('ALERT', '2026-11-03T11:43:00+05:30'), ev('FILLED', '2026-11-03T11:43:00+05:30'),
              ev('CLOSED', '2026-11-03T12:10:00+05:30', {exit_reason: 'MANUAL'}), ev('CLOSED', '2026-11-03T12:10:00+05:30', {exit_reason: 'TARGET'}),
              ev('STAGE', '2026-11-03T11:50:00+05:30')].map(e => (eventMinute(e) - u('2026-11-03T11:00:00+05:30')) / 60);"""
        )
        # an alert at the 11:43 close belongs to the 11:42 minute; a fill or a close by you at 11:43's open to 11:43
        assert out == [42, 43, 70, 69, 49]


# ══ the price scale (owner's first bug) ════════════════════════════════════════════════════
@needs_node
class TestScaleRule:
    def test_levels_near_the_candles_widen_the_scale(self):
        out = run(
            "return scaleRule({minValue: 100, maxValue: 110}, [{name: 'entry', price: 105}, {name: 'stop', price: 96}, {name: 'target', price: 115}]);"
        )
        assert out == {"range": {"minValue": 96, "maxValue": 115}, "pinned": []}

    def test_a_level_more_than_sixty_percent_away_is_pinned_not_squashed_in(self):
        out = run(
            "return scaleRule({minValue: 100, maxValue: 110}, [{name: 'stop', label: 'Stop', price: 93.9}, {name: 'target', label: 'Target', price: 116.1}, {name: 'entry', label: 'Entry', price: 106}]);"
        )
        assert out["range"] == {"minValue": 100, "maxValue": 110}
        assert [(p["name"], p["edge"]) for p in out["pinned"]] == [
            ("stop", "bottom"),
            ("target", "top"),
        ]

    def test_exactly_sixty_percent_still_widens(self):
        out = run(
            "return scaleRule({minValue: 100, maxValue: 110}, [{name: 'target', price: 116}]).range;"
        )
        assert out == {"minValue": 100, "maxValue": 116}

    def test_an_index_level_never_enters_a_stock_scale(self):
        out = run(
            "return scaleRule({minValue: 1152.5, maxValue: 1281}, [{name: 'stop', label: 'Stop', price: 23552.53}, {name: 'entry', label: 'Entry', price: 23697.6}, {name: 'target', label: 'Target', price: 24132.8}]);"
        )
        assert out["range"] == {"minValue": 1152.5, "maxValue": 1281}
        assert all(p["edge"] == "top" for p in out["pinned"]) and len(out["pinned"]) == 3

    def test_no_levels_leave_the_candles_alone_and_junk_is_ignored(self):
        out = run(
            "const r = {minValue: 1, maxValue: 2}; return [scaleRule(r, null).range, scaleRule(r, [{name: 'x', price: null}, {name: 'y', price: 'z'}]).range, scaleRule(null, [{price: 5}]).pinned];"
        )
        assert out == [{"minValue": 1, "maxValue": 2}, {"minValue": 1, "maxValue": 2}, []]

    def test_a_pinned_tag_says_what_and_where(self):
        assert run(
            "return [pinnedText({label: 'Target', price: 24110, edge: 'top'}), pinnedText({label: 'Stop', price: 23521.9, edge: 'bottom'})];"
        ) == ["Target 24,110.00 ↑", "Stop 23,521.90 ↓"]


# ══ words: legend, keys, cards ═════════════════════════════════════════════════════════════
@needs_node
class TestWords:
    def test_the_legend_reads_like_a_chart_legend(self):
        out = run(
            f"""const p = legendParts('NIFTY 50', '3m', [{DAY1}, 25010.5, 25031, 25001.2, 25022.4, 0], [{DAY1}-180, 0, 0, 0, 24992.4, 0], false);
            const s = legendParts('Reliance Industries', '5m', [{DAY1}, 1400, 1405, 1398, 1401.5, 123456], null, true);
            return [legendLine(p), legendLine(s), legendParts('X', '1m', null).o];"""
        )
        assert (
            out[0] == "NIFTY 50 · 3m   O 25,010.50  H 25,031.00  L 25,001.20  C 25,022.40  +0.12%"
        )
        assert out[1].endswith("+0.11%  Vol 1.2 L") and out[2] == "–"

    def test_volume_and_rupees_in_indian_units(self):
        assert run(
            "return [volText(950), volText(12345), volText(123456), volText(12345678), money(-2287.5), money(100000), rupees(6712.5), rupees(-2287.5), num(25010.5), rupees(-0.27), rupees(1.1)];"
        ) == ["950", "12.3 K", "1.2 L", "1.2 Cr", "−₹2,287.50", "₹1,00,000.00", "+₹6,713", "−₹2,288", "25,010.50", "−₹0.27", "+₹1.10"]  # fmt: skip

    def test_the_key_map(self):
        out = run(
            """const k = (key, extra) => keyAction(Object.assign({key, code: key === ' ' ? 'Space' : ''}, extra || {}));
            return [k(' '), k(' ', {tag: 'BUTTON'}), k('ArrowRight'), k('ArrowRight', {shiftKey: true}), k('n'), k('N'), k('ArrowUp'), k('ArrowDown'),
              k('t'), k('s'), k('x'), k('f'), k('l'), k('['), k(']'), k('?'), k('Escape'), k('1'), k('t', {tag: 'INPUT'}), k('t', {tag: 'SELECT'}),
              k('t', {editable: true}), k('t', {ctrlKey: true}), k('ArrowDown', {shiftKey: true})];"""
        )
        assert out == ["play", None, "step", "candle", "next", "next", "faster", "slower", "take", "skip", "close", "fit",
                       "panel", "tf-down", "tf-up", "help", "escape", None, None, None, None, None, "play"]  # fmt: skip

    def test_every_stage_and_exit_reason_has_plain_words_and_a_gap_says_so(self):
        out = run(
            """return [['FIRST_STOP','INITIAL','BREAKEVEN','LOCKED','TRAILING',undefined].map(stageText),
            ['STOP','BREAKEVEN_STOP','LOCK_STOP','TRAIL_STOP','TARGET','SIGNAL_EXIT','SESSION_END','MANUAL','DATA_END','GAP_STOP','GAP_TARGET','ODD',null].map(reasonText)];"""
        )
        stages, reasons = out
        assert "First stop" in stages[0] and stages[0] == stages[1] == stages[5]
        assert "breakeven" in stages[2] and "35%" in stages[3] and "Trailing" in stages[4]
        assert reasons[0] == "Stopped out at the first stop" and reasons[4] == "Target reached"
        assert reasons[7] == "Closed by you" and "gapped" in reasons[9] and "gapped" in reasons[10]
        assert reasons[11] == "ODD" and reasons[12] == "–"
        assert all(not re.fullmatch(r"[A-Z_]+", x) for x in reasons[:11])

    def test_the_ticket_for_an_option_shows_the_premium_plan_first_and_the_index_second(self):
        m = run(f"return ticketModel({json.dumps(ALERT)}, '2026-10-05T09:18:00+05:30');")
        assert m["action"] == "BUY CALL" and m["what"] == "NIFTY 25000 CE 28 Oct · one lot of 75"
        assert (
            m["buy"] == "Buy at ₹412.50"
            and m["stop"] == "Stop ₹382.00"
            and m["target"] == "Target ₹502.00"
        )
        assert m["risk"] == "₹2,287.50" and m["reward"] == "₹6,712.50" and m["costs"] == "₹92.40"
        assert m["ratio"] == "2.9 to 1" and m["source"] == "real"
        assert m["triggers"] == "Index triggers: entry 25,016.20, stop 24,970.00, target 25,154.80"
        assert m["left"] == "This alert ends in 9 market minutes."
        assert m["reasons"][1] == '<b>bold</b> & "quoted"'  # put in with textContent: never markup

    def test_the_ticket_for_a_share_has_one_plan_and_no_index_line(self):
        m = run(f"return ticketModel({json.dumps(STOCK_ALERT)}, null);")
        assert m["what"] == "Reliance Industries · one share" and m["buy"] == "Buy at ₹1,412.40"
        assert (
            m["stop"] == "Stop ₹1,398.00"
            and m["target"] == "Target ₹1,455.60"
            and m["triggers"] == ""
        )
        assert m["left"] == "This alert ends in 4 market minutes."

    def test_a_modelled_premium_is_said_and_an_implied_volatility_too(self):
        a = {**ALERT, "traded": {**ALERT["traded"], "source": "model", "iv_source": "implied"}}
        m = run(f"return ticketModel({json.dumps(a)}, null);")
        assert m["source"] == "modelled" and "implied by the real price" in m["implied"]

    def test_the_position_of_an_option_shows_premium_and_index_rupees_percent_and_r(self):
        m = run(
            f"return positionModel({json.dumps(OPEN_TRADE)}, [0, 25060.0, 433.2, 1500.0], false);"
        )
        assert (
            m["inOut"] == "Premium ₹413.20 → ₹433.20"
            and m["index"] == "Index 25,018.00 → 25,060.00"
        )
        assert m["pnl"] == "+₹1,500.00" and m["pnlTone"] == "pos" and m["r"] == "+0.65 R"
        assert m["pct"] == "+4.84%" and m["stage"] == "Stop moved to breakeven"
        assert (
            m["stop"] == "Stop ₹420.10 (index 25,031.50)"
            and m["target"] == "Target ₹482.00 (index 25,154.80)"
        )
        assert m["minutes"] == "12 min in the trade" and m["way"] == "27% of the way to the target"
        bar = m["bar"]
        assert bar["stop"] > bar["entry"] > 0 and 0 <= bar["price"] <= 100
        assert bar["ticks"] == sorted(bar["ticks"]) and bar["ticks"][2] < 100

    def test_the_position_of_a_share_has_one_price_and_no_index_line(self):
        m = run(f"return positionModel({json.dumps(STOCK_TRADE)}, null, true);")
        assert m["inOut"] == "₹1,412.40 → ₹1,420.00" and m["index"] == ""
        assert m["stop"] == "Stop ₹1,398.00" and m["what"] == "Reliance Industries · one share"
        assert m["exitSignal"] == "The strategy says close this trade."

    def test_a_trade_waiting_for_its_fill_says_so(self):
        t = {**OPEN_TRADE, "status": "PENDING_FILL", "entry": None}
        m = run(f"return positionModel({json.dumps(t)}, null, false);")
        assert m["pending"] and "Waiting for the fill at the next minute" in m["note"]

    def test_a_result_says_why_what_and_how_much(self):
        t = {**OPEN_TRADE, "status": "CLOSED", "exit_reason": "GAP_TRAIL_STOP", "exit": 25100.0, "exit_price": 452.0,
             "gross": 2910.0, "charges": 92.4, "net": 2817.6, "r_multiple": 1.22}  # fmt: skip
        m = run(f"return resultModel({json.dumps(t)});")
        assert (
            m["reason"] == "Trailing stop (the price gapped past it)" and m["net"] == "+₹2,817.60"
        )
        assert m["r"] == "+1.22 R" and m["won"] and m["charges"] == "₹92.40"
        assert m["inOut"] == "Premium ₹413.20 → ₹452.00, index 25,018.00 → 25,100.00"
        assert "80%" in m["rule"]

    def test_a_trade_row_carries_premium_in_and_out_and_the_index_levels(self):
        t = {**OPEN_TRADE, "status": "CLOSED", "exit_ts": "2026-10-05T10:02:00+05:30", "exit_reason": "MANUAL",
             "exit": 25021.0, "exit_price": 410.4, "net": -302.4, "r_multiple": -0.13}  # fmt: skip
        c = run(f"return tradeCells({json.dumps(t)});")
        assert c["when"] == "09:19 → 10:02" and c["price"] == "₹413.20 → ₹410.40"
        assert (
            c["index"] == "25,018.00 → 25,021.00"
            and c["net"] == "−₹302.40"
            and c["netTone"] == "neg"
        )
        s = run(
            f"return tradeCells({json.dumps({**STOCK_TRADE, 'exit': 1450.0, 'exit_price': 1450.0, 'net': 19.0})});"
        )
        assert s["index"] == "" and s["price"] == "₹1,412.40 → ₹1,450.00"

    def test_the_session_numbers_and_the_equity_line(self):
        s = {"trades": 3, "winners": 1, "net": -412.5, "charges": 232.5, "avg_win": 2210.0, "avg_loss": -1311.2,
             "best": 2210.0, "worst": -1800.0, "equity": 99587.5}  # fmt: skip
        rows = run(f"return numbersModel({json.dumps(s)}, 100000);")
        d = {r[0]: r[1] for r in rows}
        assert d["Trades"] == "3" and d["Winners"] == "1 of 3" and d["Net"] == "−₹412.50"
        assert d["Equity"] == "₹99,587.50" and d["Worst"] == "−₹1,800.00"
        path = run("return [equityPath([100, -50], 1000, 100, 10), equityPath([], 1000, 100, 10)];")
        assert path == ["M0.0,10.0 L50.0,0.0 L100.0,5.0", ""]

    def test_the_line_labels_name_the_level_and_the_rupees(self):
        out = run(
            f"return [levelLabels({json.dumps(ALERT)}, 'alert'), levelLabels({json.dumps(OPEN_TRADE)}, 'trade')];"
        )
        # short words beside the price scale, whose own label says the price (review R3)
        assert out[0] == {"entry": "Alert", "stop": "Stop −₹2,288", "target": "Target +₹6,713"}
        assert out[1]["stop"] == "Stop +₹518" and out[1]["entry"] == "In"

    def test_markers_say_what_happened(self):
        out = run(
            """const e = (kind, ts, data) => ({kind, ts, data: data || {}});
            return [markerFor(e('ALERT', '2026-11-03T11:43:00+05:30', {side: 'LONG'}), 'LONG').text,
              markerFor(e('FILLED', '2026-11-03T11:43:00+05:30', {entry: 25000, entry_price: 413.2}), 'LONG').text,
              markerFor(e('STAGE', '2026-11-03T11:50:00+05:30', {stage: 'LOCKED'}), 'LONG').text,
              markerFor(e('CLOSED', '2026-11-03T12:10:00+05:30', {exit_reason: 'TARGET', net: 6713, r_multiple: 2.9}), 'LONG'),
              markerFor(e('STOP_MOVED', '2026-11-03T11:50:00+05:30'), 'LONG')];"""
        )
        assert out[:3] == ["Alert", "In", "35%"]
        assert out[3]["text"] == "Out +₹6,713" and out[3]["color"] == "--ok"  # short (review R3)
        assert out[4] is None


@needs_node
class TestChoosing:
    DAYS = [
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
        "2026-11-09",
        "2026-11-10",
        "2026-11-12",
    ]

    def test_the_start_is_several_trading_days_before_the_last(self):
        assert run(f"return [defaultDay({json.dumps(self.DAYS)}), defaultDay([]), startIsoOf('2026-11-03'), minuteBefore('2026-11-03T09:15:00+05:30')];") == [
            "2026-11-04",
            None,
            "2026-11-03T09:15:00+05:30",
            "2026-11-03T09:14:00+05:30",
        ]  # fmt: skip

    def test_previous_next_and_a_typed_date_land_on_stored_days(self):
        d = json.dumps(self.DAYS)
        assert run(
            f"return [shiftDay({d}, '2026-11-10', 1), shiftDay({d}, '2026-11-12', 1), shiftDay({d}, '2026-11-02', -1), snapDay({d}, '2026-11-11'), snapDay({d}, '2027-01-01'), shiftDay({d}, '2026-11-07', -1)];"
        ) == ["2026-11-12", None, None, "2026-11-12", "2026-11-12", "2026-11-06"]

    def test_a_random_day_is_never_the_one_shown_nor_the_last_two(self):
        d = json.dumps(self.DAYS)
        out = run(
            f"const r = []; for (let i = 0; i < 200; i++) r.push(randomDay({d}, '2026-11-03', () => i / 200)); return [...new Set(r)].sort();"
        )
        assert "2026-11-03" not in out and "2026-11-10" not in out and "2026-11-12" not in out
        assert len(out) == 5

    def test_only_strategies_for_the_kind(self):
        o = {"strategies": [{"name": "a", "kinds": ["INDEX"]}, {"name": "b", "kinds": ["STOCK"]}, {"name": "c", "kinds": ["INDEX", "STOCK"]}]}  # fmt: skip
        assert run(
            f"return strategiesFor({json.dumps(o)}, {{kind: 'STOCK'}}).map(s => s.name);"
        ) == ["b", "c"]

    def test_the_resume_line_and_a_live_session_jumps_to_now(self):
        inst = {"label": "NIFTY 50"}
        live = {"instrument": inst, "strategy": "microtrend", "timeframe": "3m", "mode": "live",
                "start": "2026-11-13T15:29:00+05:30", "cursor": "2026-11-13T15:29:00+05:30"}  # fmt: skip
        past = {
            **live,
            "mode": "past",
            "cursor": "2026-10-05T10:06:00+05:30",
            "strategy_label": "Micro trend",
        }
        out = run(f"return [resumeLine({json.dumps(live)}), resumeLine({json.dumps(past)})];")
        assert "live session started 2026-11-13 15:29: it will jump to now." in out[0]
        assert out[1] == "NIFTY 50, Micro trend 3m, at Mon 05 Oct 10:07."

    def test_the_market_is_open_only_on_weekdays_from_0915_to_1530_indian_time(self):
        assert run(
            f"return [{DAY1}, {DAY1} - 60, {DAY1} + 374*60, {DAY1} + 375*60, {DAY1} - 2*86400].map(u => marketOpenAt(u * 1000));"
        ) == [True, False, True, False, False]


# ══ the chart controller with a stub of the library (SIM-01) ═══════════════════════════════
STUB = r"""
function makeLW() {
  const log = { lines: [], series: [], scale: [], markers: null, primitives: 0, ranges: [], positions: [] };
  const series = (type, opts) => {
    const s = { type, opts: { ...opts }, data: [], lines: [],
      setData(d) { this.data = d.slice(); }, update(b) { const n = this.data.length; if (n && this.data[n-1].time === b.time) this.data[n-1] = b; else this.data.push(b); },
      applyOptions(o) { Object.assign(this.opts, o); },
      createPriceLine(o) { const l = { o: { ...o }, applyOptions(x) { Object.assign(this.o, x); }, options() { return this.o; } }; this.lines.push(l); log.lines.push(l); return l; },
      removePriceLine(l) { this.lines = this.lines.filter(x => x !== l); },
      attachPrimitive(p) { log.primitives += 1; p.attached({ chart: chartObj, series: s, requestUpdate() {} }); },
      priceToCoordinate(p) { return 500 - p / 100; }, data_() { return this.data; } };
    log.series.push(s); return s;
  };
  const ts = { opts: {}, range: null, applyOptions(o) { Object.assign(this.opts, o); }, setVisibleLogicalRange(r) { this.range = r; log.ranges.push(r); },
    getVisibleLogicalRange() { return this.range; }, scrollToRealTime() {}, width() { return 1008; }, scrollToPosition(p) { log.positions.push(p); }, subscribeVisibleLogicalRangeChange() {}, timeToCoordinate() { return 10; } };
  const chartObj = { opts: {}, applyOptions(o) { Object.assign(this.opts, o); }, addSeries(type, o) { return series(type, o); },
    priceScale(id) { return { applyOptions(o) { log.scale.push([id, o]); } }; }, timeScale() { return ts; }, subscribeCrosshairMove() {}, paneSize() { return { height: 400 }; } };
  return { log, LW: { CandlestickSeries: 'candles', HistogramSeries: 'hist', LineSeries: 'line',
    createChart() { return chartObj; }, createSeriesMarkers(s, m) { const p = { m: m.slice(), setMarkers(x) { this.m = x.slice(); log.markers = this.m; }, markers() { return this.m; } }; return p; } } };
}
"""


def run_chart(body: str) -> Any:
    script = (
        f"const C = require({json.dumps(str(SIM_CORE))}); const SC = require({json.dumps(str(SIM_CHART))});\n"
        + STUB
        + f"\n(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    return _node(script)


@needs_node
class TestChartController:
    def test_the_chart_is_built_once_with_its_series_lines_markers_and_zones(self):
        out = run_chart(
            """const { LW, log } = makeLW(); const ch = SC.create({}, LW, {});
            return [log.series.map(s => s.type), log.lines.length, log.primitives, typeof ch.reset];"""
        )
        assert out == [["candles", "hist", "line", "line", "line", "line"], 5, 1, "function"]

    def test_sim01_nothing_of_a_nifty_trade_survives_a_switch_to_reliance(self):
        """SIM-01: a NIFTY session with a taken trade, then a RELIANCE session: no price line, marker or level of the
        first remains, and the scale rule sees only RELIANCE numbers."""
        out = run_chart(
            f"""const {{ LW, log }} = makeLW(); const ch = SC.create({{}}, LW, {{}});
            const nifty = []; for (let i = 0; i < 300; i++) nifty.push([{DAY1} + 180*i, 24000 + i, 24010 + i, 23990 + i, 24005 + i, 0]);
            ch.load({{ key: 'NIFTY', label: 'NIFTY 50', kind: 'INDEX', tick: 0.05, hasVolume: false }}, '3m', 3, nifty, null);
            const trade = {json.dumps(OPEN_TRADE)};
            ch.setLevels(trade, 'trade', C.levelLabels(trade, 'trade'));
            ch.setZones({{ minute: {DAY1} + 180*290, entry: trade.entry, stop: trade.stop, target: trade.target }});
            ch.setMarkers([C.markerFor({{kind: 'FILLED', ts: '2026-10-05T09:19:00+05:30', data: {{entry: 25018, entry_price: 413.2}}}}, 'LONG')]);
            const before = {{ lines: log.lines.filter(l => l.o.lineVisible).map(l => l.o.price), markers: log.markers.length, levels: ch.levels.length, zones: !!ch.zones.get() }};
            const candles = log.series[0];
            const niftyRange = candles.opts.autoscaleInfoProvider(() => ({{ priceRange: {{ minValue: 24200, maxValue: 24300 }} }}));
            before.pinned = ch.pinned.map(p => p.name);
            // the switch: RELIANCE, a stock near 1,200
            const rel = []; for (let i = 0; i < 300; i++) rel.push([{DAY2} + 300*i, 1150 + i/3, 1152 + i/3, 1149 + i/3, 1151 + i/3, 5000]);
            ch.load({{ key: 'RELIANCE', label: 'Reliance', kind: 'STOCK', tick: 0.1, hasVolume: true }}, '5m', 5, rel, null);
            const after = {{ lines: log.lines.filter(l => l.o.lineVisible || l.o.axisLabelVisible).map(l => l.o.price), markers: log.markers.length,
              levels: ch.levels, zones: ch.zones.get(), pinned: ch.pinned.length, autoScale: log.scale.filter(x => x[0] === 'right').slice(-1)[0][1].autoScale,
              minMove: candles.opts.priceFormat.minMove, volume: log.series[1].opts.visible, firstData: candles.data[0].close }};
            const relRange = candles.opts.autoscaleInfoProvider(() => ({{ priceRange: {{ minValue: 1149, maxValue: 1252 }} }}));
            return {{ before, niftyRange, after, relRange }};"""
        )
        assert (
            out["before"]["lines"]
            and out["before"]["markers"] == 1
            and out["before"]["levels"] == 3
        )
        assert out["before"]["zones"] is True
        # NIFTY candles at 24,200 to 24,300 and a trade near 25,000: the levels are pinned, the candles keep the scale
        assert out["niftyRange"]["priceRange"] == {"minValue": 24200, "maxValue": 24300}
        assert out["before"]["pinned"] == ["entry", "stop", "target"]
        after = out["after"]
        assert after["lines"] == [] and after["markers"] == 0 and after["levels"] is None
        assert after["zones"] is None and after["pinned"] == 0 and after["autoScale"] is True
        assert after["minMove"] == 0.1 and after["volume"] is True and after["firstData"] < 2000
        assert out["relRange"] == {"priceRange": {"minValue": 1149, "maxValue": 1252}}

    def test_a_far_level_is_pinned_and_a_near_one_widens_the_scale(self):
        out = run_chart(
            f"""const {{ LW, log }} = makeLW(); const ch = SC.create({{}}, LW, {{}});
            const bars = []; for (let i = 0; i < 200; i++) bars.push([{DAY1} + 180*i, 100, 101, 99, 100.5, 0]);
            ch.load({{ key: 'X', label: 'X', kind: 'STOCK', tick: 0.05 }}, '3m', 3, bars, null);
            ch.setLevels({{ entry: 100, stop: 98.5, target: 120 }}, 'alert', {{ entry: 'e', stop: 's', target: 't' }});
            const r = log.series[0].opts.autoscaleInfoProvider(() => ({{ priceRange: {{ minValue: 99, maxValue: 101 }} }}));
            return [r.priceRange, ch.pinned.map(p => [p.name, p.edge])];"""
        )
        assert out[0] == {"minValue": 98.5, "maxValue": 101}
        assert out[1] == [["target", "top"]]

    def test_levels_are_moved_never_removed_and_recreated(self):
        out = run_chart(
            """const { LW, log } = makeLW(); const ch = SC.create({}, LW, {});
            const n0 = log.lines.length;
            ch.setLevels({ entry: 10, stop: 9, target: 13, stop0: 9, target0: 13 }, 'trade', {});
            ch.setLevels({ entry: 10, stop: 10.2, target: 13, stop0: 9, target0: 13 }, 'trade', {});
            const stop = log.lines[1].o, first = log.lines[3].o;
            return [n0, log.lines.length, stop.price, stop.lineVisible, first.price, first.lineVisible, first.lineStyle];"""
        )
        assert out == [5, 5, 10.2, True, 9, True, 2]  # the first stop stays as a faint dashed line

    def test_the_start_view_shows_about_120_candles_with_the_newest_at_the_right(self):
        out = run_chart(
            f"""const {{ LW, log }} = makeLW(); const ch = SC.create({{}}, LW, {{}});
            const bars = []; for (let i = 0; i < 300; i++) bars.push([{DAY1} + 60*i, 1, 2, 0.5, 1.5, 0]);
            ch.load({{ key: 'X', label: 'X', kind: 'INDEX', tick: 0.05 }}, '1m', 1, bars, null);
            const ts = LW.createChart().timeScale();
            return [ts.opts.barSpacing, log.positions.slice(-1)[0], log.ranges.length];"""
        )
        # 1008 px over 120 candles and 6 empty bars, anchored 6 bars right of the newest (it follows new bars)
        assert out == [8, 6, 0]

    def test_a_drawn_minute_updates_the_forming_candle_and_ends_as_the_real_bar(self):
        out = run_chart(
            f"""const {{ LW, log }} = makeLW(); const ch = SC.create({{}}, LW, {{}});
            ch.load({{ key: 'X', label: 'X', kind: 'INDEX', tick: 0.05 }}, '3m', 3, [[{DAY1} - 180, 10, 11, 9, 10, 0]], null);
            const bar = [{DAY1}, 10, 14, 8, 13, 0];
            ch.drawMinute(bar, 0.5); const mid = ch.last().slice();
            ch.drawMinute(bar, 1); ch.drawMinute([{DAY1} + 60, 13, 15, 12, 12.5, 0], 1);
            return [mid[0] === {DAY1}, mid[2] <= 14 && mid[3] >= 8, ch.last(), ch.bars.length];"""
        )
        assert out == [True, True, [DAY1, 10, 15, 8, 12.5, 0], 2]


# ══ the fake of the second contract ════════════════════════════════════════════════════════
@needs_node
class TestTheFake:
    START = "2026-11-13T09:15:00+05:30"

    def _create(self, inst: str = "NIFTY", strat: str = "microtrend", tf: str = "3m") -> str:
        return f"await fake.handle('/api/sim/sessions', {{instrument:'{inst}', strategy:'{strat}', timeframe:'{tf}', mode:'past', start:'{self.START}', params:{{}}}})"

    def test_options_carry_tick_lot_unit_plots_defaults_and_chart_timeframes(self):
        o = run_fake("return await fake.handle('/api/sim/options');")
        assert o["speeds"] == [1, 2, 5, 10, 60, 300]
        assert o["chart_timeframes"] == ["1m", "3m", "5m", "10m", "15m", "30m", "60m"]
        assert {"instrument", "strategy", "timeframe"} <= set(o["defaults"])
        insts = {i["key"]: i for i in o["instruments"]}
        assert {"tick", "lot", "unit"} <= set(insts["NIFTY"]) and insts["NIFTY"]["unit"] == "lot"
        assert insts["RELIANCE"]["unit"] == "share" and not insts["MIDCPNIFTY"]["trade"]
        assert insts["ETERNAL"]["first"] is None  # an instrument with no stored bars
        assert all("plots" in s for s in o["strategies"])

    def test_an_index_and_two_stocks_at_very_different_prices(self):
        out = run_fake(
            "return await Promise.all(['NIFTY','RELIANCE','TATASTEEL'].map(async k => (await fake.handle(`/api/sim/candles?instrument=${k}&timeframe=3m`)).candles.slice(-1)[0][4]));"
        )
        assert out[0] > 20000 and 1000 < out[1] < 2000 and out[2] < 300

    def test_days_and_candles_with_a_forming_candle_and_a_clamped_until(self):
        out = run_fake(
            """const d = await fake.handle('/api/sim/days?instrument=NIFTY');
            const c = await fake.handle('/api/sim/candles?instrument=RELIANCE&timeframe=5m&until=' + encodeURIComponent('2026-11-13T11:42:00+05:30') + '&count=300');
            const early = await fake.handle('/api/sim/candles?instrument=NIFTY&timeframe=3m&until=' + encodeURIComponent('2020-01-01T09:15:00+05:30'));
            let bad = null; try { await fake.handle('/api/sim/candles?instrument=NIFTY&timeframe=7m'); } catch (e) { bad = [e.status, e.message]; }
            return [d.days.length, d.days[0], c.candles.length, c.forming && c.forming.length, c.until, !!early.note, bad];"""
        )
        assert (
            out[0] == 15 and out[2] == 300 and out[3] == 6 and out[4] == "2026-11-13T11:42:00+05:30"
        )
        assert out[5] is True and out[6][0] == 400 and "Allowed" in out[6][1]

    def test_a_session_starts_running_the_minute_before_the_start_with_candles_overlays_and_events(
        self,
    ):
        r = run_fake(f"return {self._create()};")
        s = r["session"]
        assert s["status"] == "RUNNING" and s["cursor"] == "2026-11-13T09:14:00+05:30"
        assert s["chart"] == {"tick": 0.05, "lot": 75, "unit": "lot"}
        assert 0 < len(r["candles"]) <= 300 and len(r["candles"][0]) == 6
        assert list(r["overlays"]) == ["ema"] and r["events"] == [] and r["marks"] == []

    def test_slow_builds_first_then_runs_after_three_seconds_and_actions_wait(self):
        out = run_fake(
            f"""fake.configure({{ slow: true }});
            const p = await fake.handle('/api/sim/prepare?instrument=NIFTY&strategy=microtrend&timeframe=3m');
            const r = {self._create()};
            let err = null; try {{ await fake.handle(`/api/sim/sessions/${{r.session.id}}/advance`, {{minutes: 1}}); }} catch (e) {{ err = [e.status, e.message]; }}
            const g1 = await fake.handle(`/api/sim/sessions/${{r.session.id}}`);
            await new Promise(res => setTimeout(res, 3100));
            const g2 = await fake.handle(`/api/sim/sessions/${{r.session.id}}`);
            const p2 = await fake.handle('/api/sim/prepare?instrument=NIFTY&strategy=microtrend&timeframe=3m');
            return [p.status, r.session.status, r.build.step, err, g1.session.status, g2.session.status, p2.status];"""
        )
        assert out[0] == "building" and out[1] == "BUILDING" and out[2] == "reading bars"
        assert out[3] == [409, "The session is still being prepared."]
        assert out[4] == "BUILDING" and out[5] == "RUNNING" and out[6] == "ready"

    def test_the_error_switch_fails_a_build_and_a_read(self):
        out = run_fake(
            f"""fake.configure({{ fail: 'create' }});
            const r = {self._create()};
            await new Promise(res => setTimeout(res, 1600));
            const g = await fake.handle(`/api/sim/sessions/${{r.session.id}}`);
            fake.configure({{ fail: 'candles' }});
            let e2 = null; try {{ await fake.handle('/api/sim/candles?instrument=NIFTY&timeframe=3m'); }} catch (e) {{ e2 = e.status; }}
            return [g.session.status, g.detail, e2];"""
        )
        assert out[0] == "FAILED" and "demo failure" in out[1] and out[2] == 500

    def test_chunks_stop_after_the_first_minute_with_an_event_or_at_what_needs_the_user(self):
        out = run_fake(
            f"""const r = {self._create()}; const id = r.session.id;
            const a = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 30, until: 'event'}});
            await fake.handle(`/api/sim/sessions/${{id}}/take`, {{alert_id: a.alert.id}});
            const b = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 30, until: 'event'}});
            const c = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 390, until: 'alert'}});
            let bad = null; try {{ await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 1.5}}); }} catch (e) {{ bad = e.status; }}
            return {{ a: [a.minutes.length, a.events.map(e => e.kind), a.alert.expires_in_min, a.alert.unit],
              b: [b.minutes.length, b.events.map(e => e.kind), b.events[0].ts === new Date((b.minutes[0][0] + 19800) * 1000).toISOString().slice(0, 19) + '+05:30', b.marks.length, b.trade.unit, b.trade.stop_price !== null, b.trade.risk_inr],
              c: [c.events.slice(-1)[0].kind, c.events.filter(e => e.kind === 'STOP_MOVED').length > 0, Object.keys(c.overlays)], bad }};"""
        )
        assert out["a"] == [15, ["ALERT"], 10, "lot"]
        assert (
            out["b"][0] == 1 and out["b"][1] == ["FILLED"] and out["b"][2] is True
        )  # the fill at its own minute's open
        assert out["b"][3] == 1 and out["b"][4] == "lot" and out["b"][5] and out["b"][6] > 0
        assert out["c"][0] in ("CLOSED", "DAY_END") and out["c"][1] and out["c"][2] == ["ema"]
        assert out["bad"] == 400

    def test_a_whole_trade_and_resume_restores_events_marks_and_trades(self):
        out = run_fake(
            f"""const r = {self._create()}; const id = r.session.id;
            const a = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 30, until: 'event'}});
            await fake.handle(`/api/sim/sessions/${{id}}/take`, {{alert_id: a.alert.id}});
            await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 5, until: null}});
            const mid = await fake.handle(`/api/sim/sessions/${{id}}`);
            let closed = null;
            for (let i = 0; i < 300 && !closed; i++) {{ const x = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 30, until: 'alert'}}); if (x.events.some(e => e.kind === 'CLOSED')) closed = x; }}
            const st = await fake.handle(`/api/sim/sessions/${{id}}`);
            const all = await fake.handle('/api/sim/trades?limit=10');
            const ce = closed.events.find(e => e.kind === 'CLOSED');
            return [mid.marks.length > 0, mid.events.map(e => e.kind).slice(0, 2), st.trades.length, all.trades.length, Object.keys(ce.data).sort(), st.trades[0].exit_price !== st.trades[0].exit];"""
        )
        assert out[0] is True and out[1] == ["ALERT", "FILLED"]
        assert out[2] == 1 and out[3] >= 1
        assert out[4] == ["exit", "exit_price", "exit_reason", "net", "r_multiple", "trade_id"]
        assert out[5] is True  # an option's premium out differs from the index level out

    def test_actions_answer_with_events_minutes_and_marks_and_an_unfilled_close_is_cancelled(self):
        out = run_fake(
            f"""const r = {self._create()}; const id = r.session.id;
            const a = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 15}});
            const taken = await fake.handle(`/api/sim/sessions/${{id}}/take`, {{alert_id: a.alert.id}});
            const closed = await fake.handle(`/api/sim/sessions/${{id}}/close`, {{}});
            let e409 = null; try {{ await fake.handle(`/api/sim/sessions/${{id}}/take`, {{alert_id: 'a_9'}}); }} catch (e) {{ e409 = e.status; }}
            return [Object.keys(taken).sort(), closed.events.map(e => e.kind), closed.trade, closed.minutes.length, e409];"""
        )
        assert out[0] == [
            "alert",
            "events",
            "marks",
            "minutes",
            "session",
            "summary",
            "trade",
            "trades",
        ]
        assert out[1:] == [["CANCELLED"], None, 0, 409]


# ══ static rules ═══════════════════════════════════════════════════════════════════════════
class TestStatic:
    def test_sim_css_has_no_colour_literal_and_no_type_under_14px(self):
        css = re.sub(r"/\*.*?\*/", "", SIM_CSS.read_text("utf-8"), flags=re.S)
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(", css)
        values = re.sub(r"var\([^)]*\)", "", " ".join(re.findall(r":\s*([^;{}]+)", css)))
        named = r"\b(?:white|black|red|green|blue|yellow|orange|purple|pink|gray|grey|cyan|magenta|teal|navy|maroon|gold|silver|lime)\b"
        assert not re.findall(named, values, re.I)
        sizes = [float(x) for x in re.findall(r"font-size\s*:\s*([\d.]+)px", css)]
        sizes += [float(x) for x in re.findall(r"font\s*:[^;{}]*?\b([\d.]+)px", css)]
        assert sizes and min(sizes) >= 14, sorted(sizes)[:5]

    def test_the_page_scripts_and_the_fake_hard_code_no_colour(self):
        for path in (*PAGE_SCRIPTS, SIM_FAKE):
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text("utf-8"), flags=re.S)
            assert not re.findall(
                r"(?<![\w&-])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\s*\(", code
            ), path.name

    def test_the_theme_variables_the_page_uses_are_in_all_five_themes(self):
        styles = STYLES.read_text("utf-8")
        used = set()
        for path in (SIM_CSS, *PAGE_SCRIPTS):
            used |= set(re.findall(r"var\((--[\w-]+)", path.read_text("utf-8")))
            used |= set(re.findall(r"[\"'](--[\w-]+)[\"']", path.read_text("utf-8")))
        used -= {
            "--sim-h",
            "--panel-h",
            "--dock-w",
            "--ctl",
            "--slot",
            "--sw",
            "--legend-r",
            "--pin-top",
        }  # the page's own
        root = re.search(r":root\{(.*?)\n\}", styles, re.S).group(1)
        for theme in ("night", "laxmi", "ramayana", "batman"):
            block = re.search(rf':root\[data-theme="{theme}"\]\{{(.*?)\n\}}', styles, re.S).group(1)
            for v in ("--vol-up", "--vol-dn"):
                assert v + ":" in block, (theme, v)
        for v in sorted(used):
            assert v + ":" in root, v

    def test_the_fake_is_loaded_only_with_simfake_in_the_url(self):
        js = SIM_JS.read_text("utf-8")
        assert js.count("/static/sim_fake.js") == 1 and "simfake=1" in js
        assert "sim_fake" not in INDEX.read_text("utf-8")
        for path in (SIM_CORE, SIM_CHART):
            assert "sim_fake" not in path.read_text("utf-8")

    def test_the_engine_missing_message_and_the_no_history_words_are_in_the_page_code(self):
        js = SIM_JS.read_text("utf-8")
        assert "The simulator's engine is not running in this build." in js
        assert "No stored history for this instrument yet" in js

    def test_the_markup_has_every_id_the_scripts_use(self):
        html = INDEX.read_text("utf-8")
        used = set()
        for path in PAGE_SCRIPTS:
            used |= set(
                re.findall(
                    r'(?:\$|\bset|\bshow|\bdisable|\bsetTone)\("(sim-[\w-]+)"',
                    path.read_text("utf-8"),
                )
            )
        used = {
            u for u in used if not u.endswith("-")
        }  # $("sim-p-" + t): the four panes, checked below
        assert len(used) > 80
        for i in used:
            assert f'id="{i}"' in html, i
        for t in ("trades", "log", "numbers", "how"):
            assert f'id="sim-pt-{t}"' in html and f'id="sim-p-{t}"' in html

    def test_the_section_loads_the_three_scripts_in_order_and_holds_one_root(self):
        html = INDEX.read_text("utf-8")
        assert html.count('<div id="sim-root"') == 1
        order = [
            html.index(f'<script src="/static/{n}" defer></script>')
            for n in ("sim_core.js", "sim_chart.js", "sim.js")
        ]
        assert (
            order == sorted(order)
            and html.index("lightweight-charts.standalone.production.js") < order[0]
        )

    def test_the_workspace_is_a_grid_that_fills_the_window_and_folds_on_narrow_screens(self):
        css = SIM_CSS.read_text("utf-8")
        assert 'grid-template-areas:"strip strip" "chart dock" "panel panel"' in css
        assert "height:var(--sim-h" in css and "minmax(0,1fr)" in css
        assert re.search(
            r"@media \(max-width:1199px\) and \(min-width:700px\)\{\s*#sim-root\{grid-template-columns:minmax\(0,1fr\);grid-template-rows:auto auto minmax\(0,1fr\) auto",
            css,
        )
        assert "@media (max-width:699px)" in css and "50svh" in css
        js = SIM_JS.read_text("utf-8")
        assert (
            "ResizeObserver" in js and "getBoundingClientRect().top" in js
        )  # sized from where the tab starts

    def test_the_page_follows_the_contract(self):
        js = SIM_JS.read_text("utf-8")
        for route in ("/api/sim/options", "/api/sim/days", "/api/sim/candles", "/api/sim/prepare", "/api/sim/sessions",
                      "/advance", "/take", "/skip", "/close", "/end", "/api/sim/trades"):  # fmt: skip
            assert route in js, route
        assert "until: until || null" in js and '"event"' in js and '"alert"' in js
        assert (
            "AbortController" in js and "my !== S.req" in js
        )  # a late answer never overwrites a newer choice
        assert (
            "requestAnimationFrame" in js and "setInterval" not in js
        )  # one clock on animation frames

    def test_the_guards_stay(self):
        js = SIM_JS.read_text("utf-8")
        assert "if (S.starting) return;" in js and "S.acting" in js and 'kind === "end"' in js
        assert "confirm(" in js

    def test_panels_are_updated_in_place_not_rebuilt_each_minute(self):
        js = SIM_JS.read_text("utf-8")
        frame = re.search(r"function frame\(now\) \{.*?\n\}", js, re.S).group(0)
        drain = re.search(r"function drain\(\) \{.*?\n\}", js, re.S).group(0)
        finish = re.search(r"function finishMinute\(ch, bar\) \{.*?\n\}", js, re.S).group(0)
        live = re.search(r"function renderPositionLive\(\) \{.*?\n\}", js, re.S).group(0)
        for part in (frame, drain, finish, live):
            assert "innerHTML" not in part
        log = re.search(r"function logEvent\(ev, restoring\) \{.*?\n\}", js, re.S).group(0)
        assert "appendChild" in log and "innerHTML" not in log

    def test_a_switch_veils_and_clears_at_once_and_reads_the_days_beside_the_candles(self):
        # the owner's second bug: the old picture must not wait for the new instrument's days (one more round trip)
        js = SIM_JS.read_text("utf-8")
        choose = re.search(
            r"async function choose\(patch, opts = \{\}\) \{.*?\n\}", js, re.S
        ).group(0)
        assert choose.index("loadPicture()") < choose.index("await daysOf(")
        load = re.search(r"async function loadPicture\(\) \{.*?\n\}", js, re.S).group(0)
        first_await = load.index("await ")
        assert (
            load.index("S.chart.reset()") < first_await
            and load.index("veil(`Loading") < first_await
        )


# ══ found on real bars (the join of US-094 and US-095, 25 Nov) ═══════════════════════════════
class TestOnRealBars:
    def test_a_resumed_open_trade_gets_its_levels_and_zones_back(self):
        js = SIM_JS.read_text("utf-8")
        body = re.search(r"async function running\(st, r, resumed\) \{.*?\n\}", js, re.S).group(0)
        assert body.index("takeState(st, true)") < body.index("syncChart()")

    def test_a_close_reveals_the_minute_whose_open_fills_it_paused_or_playing(self):
        """Review R4: a close while playing waited a whole minute of play (60 s at 1x)."""
        js = SIM_JS.read_text("utf-8")
        body = re.search(r"async function act\(kind\) \{.*?\n\}", js, re.S).group(0)
        assert 'kind === "close" && tradeOpen() && !S.over' in body and "forward(1, null)" in body
        assert (
            "!S.playing"
            not in body.split("forward(1, null)")[0].split('kind === "close" && tradeOpen()')[-1]
        )
        assert "if (wasPlaying" in body  # and play goes on after the fill

    def test_a_pair_of_rupee_amounts_wraps_instead_of_being_cut(self):
        css = SIM_CSS.read_text("utf-8")
        rule = re.search(r"#sim-root \.sim-pair\{[^}]*\}", css).group(0)
        assert "flex-wrap:wrap" in rule


@needs_node
def test_live_mode_says_when_the_feed_writes_no_minutes():
    # DAY1 + 3h = 12:15 IST on a Monday; a newest minute 4 minutes old is fine, 2 days old is said
    now_ms = (DAY1 + 3 * 3600) * 1000
    out = run(
        f"return [C.liveLagText({DAY1} + 3*3600 - 240, {now_ms}), C.liveLagText({DAY1} - 2*86400, {now_ms}),"
        f" C.liveLagText({DAY1} - 2*86400, {now_ms} + 8*3600*1000)];"
    )
    assert out[0] is None and out[2] is None
    assert out[1].startswith("Waiting for the feed: the newest stored minute is Sat 03 Oct 09:15.")


@needs_node
def test_a_theme_change_keeps_the_zoom_and_the_price_margins():
    out = run_chart(
        f"""const {{ LW, log }} = makeLW(); const ch = SC.create({{}}, LW, {{}});
        const bars = []; for (let i = 0; i < 300; i++) bars.push([{DAY1} + 60*i, 1, 2, 0.5, 1.5, 0]);
        ch.load({{ key: 'X', label: 'X', kind: 'INDEX', tick: 0.05 }}, '1m', 1, bars, null);
        const c = LW.createChart(); c.opts = {{}}; ch.theme();
        return [c.opts.timeScale.barSpacing, c.opts.timeScale.rightOffset, c.opts.rightPriceScale.scaleMargins, !!c.opts.layout];"""
    )
    assert out == [None, None, None, True]


@needs_node
def test_a_strategy_line_breaks_where_the_server_left_points_out():
    # 3m: yesterday's last candle 15:27, today's opening range only from 09:45 -> a gap at today's open, no diagonal
    y_last = DAY1 - 86400 + 375 * 60 - 180
    out = run(
        f"""const C2 = C; return [
          C2.overlayData([[{y_last}, 10], [{DAY1} + 1800, 12], [{DAY1} + 1980, 12]], 3, null),
          C2.overlayData([[{y_last}, 10], [{DAY1}, 11]], 3, null),
          C2.overlayData([[{DAY1} + 1800, 12]], 3, [{DAY1}, 11]),
          C2.overlayData([[{DAY1} + 180, 12]], 3, [{DAY1}, 11])];"""
    )
    gap = {"time": y_last, "value": 10, "color": "transparent"}
    assert out[0] == [gap, {"time": DAY1 + 1800, "value": 12}, {"time": DAY1 + 1980, "value": 12}]
    assert out[1] == [
        {"time": y_last, "value": 10},
        {"time": DAY1, "value": 11},
    ]  # Supertrend goes on at the open
    # a gap after the last point drawn: that point is sent again, transparent
    assert out[2] == [
        {"time": DAY1, "value": 11, "color": "transparent"},
        {"time": DAY1 + 1800, "value": 12},
    ]
    assert out[3] == [{"time": DAY1 + 180, "value": 12}]


# ══ the three reviews of 25 Nov (page and rules) ═════════════════════════════════════════════
def _phone_block(css: str) -> str:
    start = css.index("@media (max-width:699px){")
    depth, i = 0, start
    while True:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
        i += 1


class TestReviews:
    def test_touch_targets_on_a_phone_are_44px_or_more(self):
        """Rules review R5 and page review R10: every height or control size in the phone block is at least 44 px, and
        the chart's toolbar, the segments, the panel's view toggle and tabs are raised to it."""
        block = _phone_block(SIM_CSS.read_text("utf-8"))
        for m in re.finditer(r"(?<![-\w])(height|--ctl|min-height):\s*(\d+)px", block):
            assert int(m.group(2)) >= 44, m.group(0)
        for rule in (".sim-chartbar{--ctl:44px}", ".sim-seg button{height:44px}",
                     ".sim-phead .sim-seg button{height:44px}", ".sim-ptoggle{width:44px;height:44px}"):  # fmt: skip
            assert rule in block, rule
        assert re.search(r"\.sim-ptab\{height:44px", block)

    def test_the_build_poll_gives_up_and_says_so(self):
        """Rules review R1: a 404 or five failed reads in a row end in failed(), with a back-off between tries."""
        js = SIM_JS.read_text("utf-8")
        body = re.search(r"function pollBuild\(my, fails = 0\) \{.*?\n\}", js, re.S).group(0)
        assert "e.status === 404" in body and "POLL_FAILS" in body and "failed(" in body
        assert "pollBuild(my, fails + 1)" in body and "2 ** fails" in body

    def test_a_keep_clock_switch_loads_the_days_of_the_new_instrument(self):
        """Page review R1: after a switch in the middle of a session the day buttons died (the days were never read)."""
        js = SIM_JS.read_text("utf-8")
        body = re.search(r"async function choose\(patch, opts = \{\}\) \{.*?\n\}", js, re.S).group(
            0
        )
        assert "if (keepClock) daysOf(S.choice.instrument).then(" in body
        assert body.index("daysOf(S.choice.instrument).then(") < body.index(
            "if (keepClock || known)"
        )
        day = re.search(r"function renderDay\(\) \{.*?\n\}", js, re.S).group(0)
        assert "Loading the stored days" in day

    def test_sheets_take_focus_give_it_back_and_hold_the_keys(self):
        js = SIM_JS.read_text("utf-8")
        assert "first.focus()" in js and "refocus(S.sheetOpener)" in js and "refocus(opener)" in js
        assert 'if (sheetOpen() && a !== "help") return;' in js
        assert 'if (S.note === SHEET_NOTE) status("")' in js  # review R7: no stale line

    def test_every_key_that_acts_is_in_the_key_sheet(self):
        out = run(
            """const keys = C.KEYS.map(k => k[0]);
            return [keys.includes("Shift + ↓"), C.keyAction({key: "ArrowDown", shiftKey: true})];"""
        )
        assert out == [True, "play"]

    def test_reduced_motion_turns_off_the_flash_and_the_smooth_minute(self):
        js = SIM_JS.read_text("utf-8")
        assert "prefers-reduced-motion: reduce" in js and "smooth: !REDUCED" in js
        assert "if (!el || REDUCED) return;" in js
        assert re.search(
            r"prefers-reduced-motion:reduce\)\{[^\n]*\.sim-flash\{animation:none\}",
            SIM_CSS.read_text("utf-8"),
        )

    def test_fit_root_writes_only_on_a_change_and_does_not_watch_the_root(self):
        js = SIM_JS.read_text("utf-8")
        body = re.search(r"function fitRoot\(\) \{.*?\n\}", js, re.S).group(0)
        assert 'getPropertyValue("--sim-h") === hv' in body
        assert '$("warnings")].forEach' in js

    def test_pins_sit_left_below_the_legend_and_the_alert_box_left_the_candles(self):
        css = SIM_CSS.read_text("utf-8")
        assert "#sim-root .sim-pin{position:absolute;left:8px;" in css
        assert "var(--pin-top" in css
        js = SIM_JS.read_text("utf-8")
        flag = re.search(r"function renderFlag\(\) \{.*?\n\}", js, re.S).group(0)
        assert "take it (T)" not in flag
