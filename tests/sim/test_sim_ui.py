"""US-076: the Simulator tab. Pure helpers lifted out of sim.js and run in node, the fake of the engine,
and the static rules (no colour literal in sim.css, no small type, the fake is loaded only on request).
Skipped without node (the static tests still run)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
SIM_JS, SIM_CSS, SIM_FAKE, APP_JS, INDEX = (
    UI / "sim.js",
    UI / "sim.css",
    UI / "sim_fake.js",
    UI / "app.js",
    UI / "index.html",
)
APP_ONE_LINERS = ["esc", "fmt", "cls", "hhmm"]
SIM_ONE_LINERS = [
    "IST",
    "DAY_OPEN",
    "DAY_MINUTES",
    "tfMinutes",
    "unixOf",
    "money",
    "minutePath",
    "minuteSeconds",
    "minutesToAsk",
    "sourceWord",
    "sideWord",
    "clampPct",
    "tradedBy",
    "stageText",
    "toIsoIst",
    "defaultStart",
    "WEEK",
    "MONTH",
    "minutesLeft",
    "eventLine",
    "cardNeedsScroll",
    "NO_HISTORY",
]
SIM_BLOCKS = ["STAGE_TEXT", "REASON_TEXT"]
SIM_FUNCTIONS = [
    "reasonText",
    "binStart",
    "mergeBar",
    "foldMinutes",
    "partialMinute",
    "stepMinutes",
    "strategiesFor",
    "plusMinute",
    "resumeLine",
    "mergeRange",
    "clampStart",
    "clockText",
    "alertCardHtml",
    "positionCardHtml",
    "summaryTilesHtml",
]

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# A day starts at 09:15 IST = 03:45 UTC. 2026-10-05 is a Monday.
DAY1 = 1791171900  # 2026-10-05 09:15 IST
DAY2 = DAY1 + 86400


def lifted() -> str:
    app, sim, parts = APP_JS.read_text("utf-8"), SIM_JS.read_text("utf-8"), []
    for name in APP_ONE_LINERS:
        m = re.search(rf"^const {name} = .*;$", app, re.M)
        assert m, f"const {name} not found as one line in app.js"
        parts.append(m.group(0))
    for name in SIM_ONE_LINERS:
        m = re.search(rf"^const {name} = .*$", sim, re.M)
        assert m, f"const {name} not found as one line in sim.js"
        parts.append(m.group(0))
    for name in SIM_BLOCKS:
        m = re.search(rf"^const {name} = \{{.*?^\}};$", sim, re.M | re.S)
        assert m, f"const {name} block not found in sim.js"
        parts.append(m.group(0))
    for name in SIM_FUNCTIONS:
        m = re.search(rf"^function {name}\(.*?^\}}$", sim, re.M | re.S)
        assert m, f"function {name} not found in sim.js"
        parts.append(m.group(0))
    return "\n".join(parts)


def run(body: str) -> Any:
    script = (
        lifted()
        + f"\n(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def run_fake(body: str) -> Any:
    script = (
        f"const fake = require({json.dumps(str(SIM_FAKE))});\n"
        f"(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


ALERT = {
    "id": "a_17",
    "ts": "2026-10-05T09:18:00+05:30",
    "expires": "2026-10-05T09:28:00+05:30",
    "side": "LONG",
    "action": "BUY CALL",
    "entry": 25016.2,
    "stop": 24970.0,
    "target": 25154.8,
    "risk": 46.2,
    "reward": 138.6,
    "qty": 75,
    "traded": {
        "what": "NIFTY 25000 CE 28 Oct",
        "price": 412.5,
        "source": "real",
        "stop_price": 382.0,
        "target_price": 502.0,
    },
    "risk_inr": 2287.5,
    "reward_inr": 6712.5,
    "costs_inr": 92.4,
    "reasons": ["all seven trends up", '<b>bold</b> & "quoted"'],
}
OPEN_TRADE = {
    "id": 41,
    "status": "OPEN",
    "side": "LONG",
    "action": "BUY CALL",
    "qty": 75,
    "entry_ts": "2026-10-05T09:19:00+05:30",
    "entry": 25018.0,
    "entry_price": 413.2,
    "source": "real",
    "stop": 25031.5,
    "target": 25154.8,
    "stop0": 24970.0,
    "target0": 25154.8,
    "stage": "BREAKEVEN",
    "best": 25055.0,
    "progress": 0.27,
    "mark": 25049.0,
    "mark_price": 431.0,
    "open_pnl": 1335.0,
    "exit_ts": None,
    "exit": None,
    "exit_price": None,
    "exit_reason": None,
    "gross": None,
    "charges": None,
    "net": None,
    "r_multiple": None,
    "traded": {"what": "NIFTY 25000 CE 28 Oct", "source": "real"},
}


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
            f"""return foldMinutes([], [[{DAY1},10,12,9,11,1],[{DAY1}+60,11,15,10,14,2],[{DAY1}+120,14,14,8,9,3]], 3);"""
        )
        assert out == [[DAY1, 10, 15, 8, 9, 6]]

    def test_candles_roll_over_a_day_boundary(self):
        out = run(
            f"""const m = [[{DAY1}+60*374,5,6,4,5.5,1],[{DAY2},7,8,6,7.5,1]];
            return [foldMinutes([], m, 3).map(c => c[0]), foldMinutes([], m, 60).map(c => c[0])];"""
        )
        # 15:29 sits in the 3m bin that opened at minute 372 and in the 60m bin at minute 360
        assert out == [[DAY1 + 372 * 60, DAY2], [DAY1 + 360 * 60, DAY2]]

    def test_60m_has_a_short_last_bin_and_a_15m_bin_is_complete(self):
        out = run(
            f"""const m = []; for (let i = 0; i < 375; i++) m.push([{DAY1} + 60*i, 1, 2, 0.5, 1.5, 1]);
            return [foldMinutes([], m, 60).length, foldMinutes([], m, 15).length, foldMinutes([], m, 3).length, foldMinutes([], m, 15)[24][5]];"""
        )
        assert out == [7, 25, 125, 15]

    def test_merging_into_the_forming_candle_keeps_its_open(self):
        out = run(
            f"return mergeBar([{DAY1}, 10, 11, 9, 10.5, 4], [{DAY1}+60, 10.5, 13, 10, 12, 2], 3);"
        )
        assert out == [DAY1, 10, 13, 9, 12, 6]


@needs_node
class TestAnimationAndClock:
    def test_an_up_minute_goes_open_low_high_close(self):
        assert run("return minutePath([0, 10, 14, 8, 13]);") == [10, 8, 14, 13]

    def test_a_down_minute_goes_open_high_low_close(self):
        assert run("return minutePath([0, 10, 14, 8, 9]);") == [10, 14, 8, 9]

    def test_the_forming_minute_draws_along_the_path_and_ends_as_the_bar(self):
        out = run(
            """const b = [0, 10, 14, 8, 13];
            return [0, 0.5, 1].map(f => partialMinute(b, f)).concat([partialMinute(b, 1 / 3)]);"""
        )
        # at the start only the open is seen; halfway it is between the low and the high; at the end it is the bar
        assert out[0] == [0, 10, 10, 10, 10, 0]
        assert out[1][2] > 10 and out[1][3] == 8 and 8 < out[1][4] < 14
        assert out[2] == [0, 10, 14, 8, 13, 0]
        assert out[3] == [0, 10, 10, 8, 8, 0]

    def test_a_minute_takes_sixty_over_speed_real_seconds(self):
        assert run("return [1, 2, 5, 10].map(minuteSeconds);") == [60, 30, 12, 6]

    def test_up_to_ten_x_asks_one_minute_a_call(self):
        assert run("return [1, 2, 5, 10].map(s => minutesToAsk(s, 0));") == [1, 1, 1, 1]

    def test_sixty_and_three_hundred_x_ask_for_what_the_elapsed_time_needs(self):
        out = run(
            "return [[60, 59], [60, 61], [60, 200], [300, 0.5 * 300], [300, 300 * 2.2], [300, 300 * 100]].map(a => minutesToAsk(...a));"
        )
        assert out == [0, 1, 3, 2, 11, 390]

    def test_a_step_ends_on_a_candle_close(self):
        out = run(
            f"""const iso = (u) => new Date(u * 1000).toISOString();
            return [[{DAY1}-60, 3], [{DAY1}, 3], [{DAY1}+60, 1], [{DAY1}+60*2, 3], [{DAY1}+60*374, 3], [{DAY1}+60*373, 1]].map(([u, tf]) => stepMinutes(iso(u), tf));"""
        )
        assert out == [3, 2, 1, 3, 3, 1]

    def test_the_clock_reads_indian_time(self):
        assert run(f"return clockText({DAY1});") == "Mon 05 Oct 2026  09:15"


@needs_node
class TestWords:
    def test_every_stage_has_plain_words(self):
        out = run(
            "return ['INITIAL', 'BREAKEVEN', 'LOCKED', 'TRAILING', undefined].map(stageText);"
        )
        assert out[1] == "Stop above breakeven"
        assert "35%" in out[2] and "Trailing" in out[3]
        assert out[0] == out[4] and "First stop" in out[0]

    def test_every_exit_reason_has_plain_words_and_a_gap_says_so(self):
        out = run(
            """return ['STOP','BREAKEVEN_STOP','LOCK_STOP','TRAIL_STOP','TARGET','SIGNAL_EXIT','SESSION_END','MANUAL','DATA_END','GAP_STOP','GAP_TARGET','ODD',null].map(reasonText);"""
        )
        assert out[0] == "Stopped out at the first stop"
        assert out[1] == "Stopped at breakeven" and "35%" in out[2] and out[3] == "Trailing stop"
        assert (
            out[4] == "Target reached"
            and out[5] == "Strategy exit"
            and out[6] == "Session end"
            and out[7] == "Closed by you"
        )
        assert "gapped" in out[9] and out[9].startswith("Stopped out") and "gapped" in out[10]
        assert out[11] == "ODD" and out[12] == "–"
        assert all(not re.fullmatch(r"[A-Z_]+", x) for x in out[:11])


@needs_node
class TestCards:
    def test_the_alert_card_shows_what_to_do_levels_rupees_and_the_traded_price(self):
        html = run(f"return alertCardHtml({json.dumps(ALERT)}, '2026-10-05T09:18:00+05:30');")
        for part in [
            "BUY CALL",
            "25016.20",
            "24970.00",
            "25154.80",
            "₹2,287.50",
            "₹6,712.50",
            "₹92.40",
            "NIFTY 25000 CE 28 Oct",
            "412.50",
            ">real<",
            'data-act="take"',
            'data-act="skip"',
            "9 market minutes",
        ]:
            assert part in html, part

    def test_a_modelled_price_is_marked_modelled(self):
        a = {**ALERT, "traded": {**ALERT["traded"], "source": "model"}}
        assert ">modelled<" in run(f"return alertCardHtml({json.dumps(a)}, null);")

    def test_values_from_the_server_are_escaped(self):
        html = run(f"return alertCardHtml({json.dumps(ALERT)}, null);")
        assert (
            "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt; &amp; &quot;quoted&quot;" in html
        )
        evil = {
            **OPEN_TRADE,
            "action": "<img src=x onerror=1>",
            "traded": {"what": "<script>", "source": "real"},
        }
        html = run(f"return positionCardHtml({json.dumps(evil)}, false);")
        assert "<img" not in html and "<script>" not in html

    def test_no_alert_no_card(self):
        assert run("return [alertCardHtml(null), positionCardHtml(null)];") == ["", ""]

    def test_the_position_card_shows_stage_progress_marks_and_open_profit(self):
        html = run(f"return positionCardHtml({json.dumps(OPEN_TRADE)}, false);")
        for part in [
            "Stop above breakeven",
            "25031.50",
            "25154.80",
            "₹1,335.00",
            "width:27%",
            "left:25%",
            "left:50%",
            "left:80%",
            'data-act="close"',
            "Quantity 75".lower(),
        ]:
            assert part in html, part
        assert "the strategy says close" not in html.lower()

    def test_an_exit_signal_is_said_in_plain_words(self):
        assert "The strategy says close this trade." in run(
            f"return positionCardHtml({json.dumps(OPEN_TRADE)}, true);"
        )

    def test_a_trade_waiting_for_its_fill_says_so(self):
        t = {**OPEN_TRADE, "status": "PENDING_FILL", "entry": None}
        html = run(f"return positionCardHtml({json.dumps(t)}, false);")
        assert "Waiting for the fill at the next minute" in html and 'data-act="close"' not in html

    def test_a_closed_trade_shows_its_reason_and_result(self):
        t = {
            **OPEN_TRADE,
            "status": "CLOSED",
            "exit_reason": "GAP_TRAIL_STOP",
            "exit": 25100.0,
            "gross": 3000.0,
            "charges": 92.4,
            "net": 2907.6,
            "r_multiple": 1.27,
        }
        html = run(f"return positionCardHtml({json.dumps(t)}, false);")
        assert (
            "Trailing stop (the price gapped past it)" in html
            and "₹2,907.60" in html
            and "1.27 R" in html
            and "won" in html
        )

    def test_progress_never_leaves_the_bar(self):
        t = {**OPEN_TRADE, "progress": 1.7}
        assert "width:100%" in run(f"return positionCardHtml({json.dumps(t)}, false);")

    def test_summary_tiles(self):
        s = {
            "trades": 3,
            "winners": 1,
            "net": -412.5,
            "gross": -180.0,
            "charges": 232.5,
            "avg_win": 2210.0,
            "avg_loss": -1311.2,
            "best": 2210.0,
            "worst": -1800.0,
            "equity": 99587.5,
        }
        html = run(f"return summaryTilesHtml({json.dumps(s)}, 100000);")
        for part in [
            "−₹412.50",
            "₹232.50",
            "₹2,210.00",
            "−₹1,311.20",
            "−₹1,800.00",
            "₹99,587.50",
            "₹1,00,000.00",
            "1 winners",
        ]:
            assert part in html, part


@needs_node
class TestSetupHelpers:
    OPTS = {
        "strategies": [
            {"name": "a", "kinds": ["INDEX"]},
            {"name": "b", "kinds": ["STOCK"]},
            {"name": "c", "kinds": ["INDEX", "STOCK"]},
        ]
    }
    INST = {
        "key": "NIFTY",
        "kind": "INDEX",
        "first": "2021-11-08T09:15:00+05:30",
        "last": "2026-11-20T15:29:00+05:30",
    }

    def test_only_strategies_for_the_kind(self):
        assert run(
            f"return strategiesFor({json.dumps(self.OPTS)}, {{kind: 'STOCK'}}).map(s => s.name);"
        ) == ["b", "c"]

    def test_the_default_start_is_the_last_session_at_0915(self):
        assert run(f"return defaultStart({json.dumps(self.INST)});") == "2026-11-20T09:15"

    def test_a_start_is_clamped_into_the_stored_range_and_sent_with_the_ist_offset(self):
        out = run(
            f"return [clampStart('2020-01-01T09:15', {json.dumps(self.INST)}), clampStart('2030-01-01T09:15', {json.dumps(self.INST)}), toIsoIst('2026-10-05T09:15'), toIsoIst('')];"
        )
        # the engine needs one stored minute before a start: the earliest start is the first minute + 1
        assert out == ["2021-11-08T09:16", "2026-11-20T15:29", "2026-10-05T09:15:00+05:30", None]

    def test_timeframes_read_as_minutes(self):
        assert run("return ['1m', '3m', '15m', '60m', '1h', 'x'].map(tfMinutes);") == [
            1,
            3,
            15,
            60,
            60,
            1,
        ]


@needs_node
class TestTheFake:
    START = "2026-10-09T09:15:00+05:30"

    def test_it_serves_the_options_contract(self):
        o = run_fake("return await fake.handle('/api/sim/options');")
        assert {"instruments", "strategies", "speeds", "defaults"} <= set(o)
        assert any(not i["trade"] for i in o["instruments"]) and any(
            i["live"] for i in o["instruments"]
        )
        assert o["speeds"] == [1, 2, 5, 10, 60, 300]

    def test_a_session_starts_the_minute_before_the_start_with_history(self):
        r = run_fake(
            f"return await fake.handle('/api/sim/sessions', {{instrument:'NIFTY', strategy:'microtrend', timeframe:'3m', mode:'past', start:'{self.START}', params:{{}}}});"
        )
        assert (
            r["session"]["cursor"] == "2026-10-09T09:14:00+05:30"
            and r["session"]["tf_minutes"] == 3
        )
        assert 0 < len(r["candles"]) <= 300 and len(r["candles"][0]) == 6
        assert r["state"]["trade"] is None and r["state"]["alert"] is None

    def test_a_whole_trade_alert_take_fill_stages_close(self):
        out = run_fake(
            f"""const s = await fake.handle('/api/sim/sessions', {{instrument:'NIFTY', strategy:'microtrend', timeframe:'3m', mode:'past', start:'{self.START}', params:{{}}}});
            const id = s.session.id, kinds = [];
            let r = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 15}});
            r.events.forEach(e => kinds.push(e.kind));
            const alert = r.alert;
            const taken = await fake.handle(`/api/sim/sessions/${{id}}/take`, {{alert_id: alert.id}});
            let closed = null, fill = null;
            for (let i = 0; i < 200 && !closed; i++) {{
              r = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 5}});
              r.events.forEach(e => kinds.push(e.kind));
              if (r.events.some(e => e.kind === 'FILLED')) fill = r.trade;
              if (r.events.some(e => e.kind === 'CLOSED')) closed = r;
            }}
            const st = await fake.handle(`/api/sim/sessions/${{id}}`);
            const all = await fake.handle('/api/sim/trades?limit=10');
            return {{alert, taken: taken.trade.status, kinds, fill: fill && fill.status, closed: !!closed, trades: st.trades.length, all: all.trades.length, reason: st.trades[0].exit_reason, net: st.summary.net}};"""
        )
        assert out["alert"]["action"] == "BUY CALL" and out["taken"] == "PENDING_FILL"
        assert (
            out["kinds"][0] == "ALERT"
            and "FILLED" in out["kinds"]
            and "STAGE" in out["kinds"]
            and "STOP_MOVED" in out["kinds"]
            and out["kinds"][-1] in ("CLOSED", "DAY_END")
        )
        assert out["closed"] and out["trades"] == 1 and out["all"] >= 1 and out["reason"]

    def test_take_without_an_alert_is_a_409(self):
        out = run_fake(
            f"""const s = await fake.handle('/api/sim/sessions', {{instrument:'NIFTY', strategy:'microtrend', timeframe:'3m', mode:'past', start:'{self.START}', params:{{}}}});
            try {{ await fake.handle(`/api/sim/sessions/${{s.session.id}}/take`, {{alert_id: 'a_1'}}); return 'no error'; }} catch (e) {{ return e.status; }}"""
        )
        assert out == 409


class TestStatic:
    def test_sim_css_has_no_colour_literal_and_no_small_type(self):
        css = re.sub(r"/\*.*?\*/", "", SIM_CSS.read_text("utf-8"), flags=re.S)
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(", css)
        values = re.sub(r"var\([^)]*\)", "", " ".join(re.findall(r":\s*([^;{}]+)", css)))
        named = r"\b(?:white|black|red|green|blue|yellow|orange|purple|pink|gray|grey|cyan|magenta|teal|navy|maroon|gold|silver|lime)\b"
        assert not re.findall(named, values, re.I)
        sizes = [float(x) for x in re.findall(r"font-size\s*:\s*([\d.]+)px", css)]
        assert sizes and min(sizes) >= 13

    def test_sim_js_and_the_fake_hard_code_no_colour(self):
        for path in (SIM_JS, SIM_FAKE):
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text("utf-8"), flags=re.S)
            assert not re.findall(
                r"(?<![\w&-])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\s*\(", code
            ), path.name

    def test_the_fake_is_loaded_only_with_simfake_in_the_url(self):
        js = SIM_JS.read_text("utf-8")
        assert js.count("/static/sim_fake.js") == 1 and "simfake=1" in js
        assert "sim_fake" not in INDEX.read_text("utf-8")

    def test_the_engine_missing_message_is_in_the_page_code(self):
        assert "The simulator's engine is not running in this build." in SIM_JS.read_text("utf-8")

    def test_the_markup_has_every_id_the_script_uses(self):
        html, js = INDEX.read_text("utf-8"), SIM_JS.read_text("utf-8")
        used = set(re.findall(r'\$\("(sim-[\w-]+)"\)', js))
        assert used
        for i in used:
            assert f'id="{i}"' in html, i

    def test_only_the_sim_root_changed_in_the_page(self):
        html = INDEX.read_text("utf-8")
        assert (
            html.count('<div id="sim-root">') == 1
            and '<script src="/static/sim.js" defer></script>' in html
        )


def test_the_page_shows_the_premium_note_and_heals_a_candle_it_joined_midway():
    js = SIM_JS.read_text("utf-8")
    html = (UI / "index.html").read_text("utf-8")
    assert 'id="sim-premium"' in html and "premium_note" in js
    assert "markPartial()" in js and "healCandle()" in js
    assert "S.candles = st.candles || []" in js  # a resume gets the chart's history back


@needs_node
def test_the_earliest_start_is_one_minute_after_the_first_stored_bar():
    assert run(
        "return [plusMinute('2026-05-04T09:15:00+05:30'), plusMinute('2026-05-04T23:59')];"
    ) == [
        "2026-05-04T09:16",
        "2026-05-05T00:00",
    ]


def test_a_live_session_with_no_new_minutes_says_so():
    js = SIM_JS.read_text("utf-8")
    assert "Waiting for live minutes" in js and "liveStaleSeconds" in js


@needs_node
def test_a_card_scrolls_only_when_it_is_not_fully_inside_the_viewport():
    assert run(
        "return [cardNeedsScroll(10, 300, 800), cardNeedsScroll(-1, 300, 800), cardNeedsScroll(600, 801, 800), cardNeedsScroll(0, 800, 800)];"
    ) == [
        False,
        True,
        True,
        False,
    ]


@needs_node
def test_the_resume_line_says_a_live_session_jumps_to_now():
    inst = {"label": "NIFTY 50"}
    live = {"instrument": inst, "strategy": "microtrend", "timeframe": "3m", "mode": "live",
            "start": "2026-11-13T15:29:00+05:30", "cursor": "2026-11-13T15:29:00+05:30"}  # fmt: skip
    past = {**live, "mode": "past", "cursor": "2026-10-05T10:06:00+05:30"}
    out = run(f"return [resumeLine({json.dumps(live)}), resumeLine({json.dumps(past)})];")
    assert "live session started 2026-11-13 15:29: it will jump to now." in out[0]
    assert out[1] == "NIFTY 50, microtrend 3m, at 2026-10-05 10:06."


@needs_node
def test_an_instrument_without_stored_bars_does_not_break_the_start_picker():
    none = {"key": "MM", "kind": "STOCK", "first": None, "last": None}
    assert run(
        f"return [plusMinute(null), clampStart('2026-10-05T09:15', {json.dumps(none)}), defaultStart({json.dumps(none)})];"
    ) == ["", "2026-10-05T09:15", ""]
    assert (
        run("return NO_HISTORY;")
        == "No stored history for this instrument yet: run the backfill, or use Live"
    )


def test_the_alert_panel_sits_beside_the_chart_on_wide_screens_and_below_on_narrow():
    css = (UI / "sim.css").read_text("utf-8")
    html = (UI / "index.html").read_text("utf-8")
    js = SIM_JS.read_text("utf-8")
    assert re.search(r"@media \(min-width:1100px\)\{#sim-root \.sim-main\{display:grid", css)
    assert re.search(r"#sim-root \.sim-main\{display:block\}", css)  # narrow: stacked
    assert (
        html.index('class="sim-side"') > html.index('id="sim-chart"')
        and 'id="sim-alert"' in html.split('class="sim-side"')[1]
    )
    flash = re.search(r"@keyframes sim-flash\{.*?\}\}", css, re.S).group(0)
    assert "var(--accent2)" in flash and not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb", flash)
    assert 'scrollIntoView({ block: "nearest", behavior: "smooth" })' in js
    assert "No stored history" in js and "applyAction(res)" in js


@needs_node
def test_the_fake_answers_actions_with_events_and_minutes_and_cancels_an_unfilled_trade():
    out = run_fake(
        f"""const s = await fake.handle('/api/sim/sessions', {{instrument:'NIFTY', strategy:'microtrend', timeframe:'3m', mode:'past', start:'{TestTheFake.START}', params:{{}}}});
        const id = s.session.id;
        let r = await fake.handle(`/api/sim/sessions/${{id}}/advance`, {{minutes: 15}});
        const taken = await fake.handle(`/api/sim/sessions/${{id}}/take`, {{alert_id: r.alert.id}});
        const closed = await fake.handle(`/api/sim/sessions/${{id}}/close`, {{}});
        return [Object.keys(taken).sort(), closed.events.map(e => e.kind), closed.trade, closed.minutes.length];"""
    )
    assert out[0] == ["alert", "events", "minutes", "session", "summary", "trade", "trades"]
    assert out[1:] == [["CANCELLED"], None, 0]


@needs_node
def test_the_chart_scale_holds_the_entry_stop_and_target_and_lets_go_without_them():
    r = run(
        "const o = {priceRange: {minValue: 100, maxValue: 110}};"
        "return [mergeRange(o, [105, 90, 130]), mergeRange(o, [null, 'x', 104]), mergeRange(o, []), mergeRange(null, [5, 7]), mergeRange(o, null)];"
    )
    assert r[0] == {"priceRange": {"minValue": 90, "maxValue": 130}}
    assert r[1] == {"priceRange": {"minValue": 100, "maxValue": 110}}  # only finite levels count
    assert r[2] == {"priceRange": {"minValue": 100, "maxValue": 110}}  # none: the candles' own
    assert r[3] == {"priceRange": {"minValue": 5, "maxValue": 7}}
    assert r[4] == {"priceRange": {"minValue": 100, "maxValue": 110}}


@needs_node
def test_the_alert_card_says_when_stop_and_target_prices_are_modelled_on_an_implied_volatility():
    a = {"id": "a_1", "side": "LONG", "action": "BUY CALL", "qty": 75, "entry": 1, "stop": 0, "target": 2,
         "traded": {"what": "X", "price": 5, "source": "real", "iv_source": "implied"}}  # fmt: skip
    plain = {**a, "traded": {"what": "X", "price": 5, "source": "real"}}
    out = run(
        f"return [alertCardHtml({json.dumps(a)}, null), alertCardHtml({json.dumps(plain)}, null)];"
    )
    assert "implied by the real price" in out[0] and "implied" not in out[1]
