"""US-090: the Volume tab. Pure helpers lifted out of volume.js and run in node, the contract's example body
rendered through the whole script with a tiny DOM stub, the fake of the engine, and the static rules
(no colour literal, no small type, the fake is loaded only on request, the nav and the keys).
Skipped without node (the static tests still run)."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"
VOL_JS, VOL_CSS, VOL_FAKE, APP_JS, INDEX = (
    UI / "volume.js",
    UI / "volume.css",
    UI / "volume_fake.js",
    UI / "app.js",
    UI / "index.html",
)
APP_ONE_LINERS = ["esc", "fmt"]
VOL_ONE_LINERS = [
    "IST",
    "NO_BOOK",
    "CLOSED_TEXT",
    "WATCH_NOT_ALERT",
    "MONTH",
    "STATE_TEXT",
    "num",
    "crore",
    "times",
    "timesNormal",
    "buyersPct",
    "buyersText",
    "stateWord",
    "unixOf",
    "clockOf",
    "dayWord",
    "signed",
    "clampHalf",
    "sharePct",
    "closedBanner",
    "sideOfMinute",
    "bookIsEmpty",
    "stateChip",
    "shareBar",
    "pressureWord",
    "pressBar",
    "nameOf",
    "newestFirst",
    "errorText",
    "tile",
]
VOL_BLOCKS = ["STATE_HELP", "SORT_COLS", "LEGEND"]
VOL_CONST_BLOCKS = ["statusLine", "indexOptions"]
VOL_WRAPPED = ["minuteReadout"]  # one statement over several lines
VOL_FUNCTIONS = [
    "sortStocks",
    "chartPoints",
    "alertTime",
    "nowTilesHtml",
    "headlineHtml",
    "stocksTableHtml",
    "indexCardHtml",
    "indicesHtml",
    "bookHtml",
    "alertCardHtml",
    "alertsHtml",
    "earlierHtml",
    "profileHtml",
    "studyHtml",
    "missingHtml",
    "notesHtml",
    "sectionsHtml",
    "rangeText",
]

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

DAY = 1764046500  # the contract example row: 10:25 IST (the contract's own number)


def ist(unix: int) -> str:
    return dt.datetime.fromtimestamp(unix, dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat()


EXAMPLE: dict[str, Any] = {
    "as_of": "2026-11-25T11:42:00+05:30",
    "session": "2026-11-25",
    "market_open": True,
    "source": "live",
    "basket": {"count": 18, "label": "18 large stocks", "missing": ["TMPV"]},
    "now": {
        "minute": "11:41",
        "turnover_1m": 412000000.0,
        "expected_1m": 300000000.0,
        "rvol_1m": 1.37,
        "rvol_5m": 1.31,
        "rvol_day": 1.08,
        "z": 1.4,
        "state": "normal",
        "flow_share_5m": 0.58,
        "flow_share_day": 0.51,
        "turnover_day": 34000000000.0,
        "expected_day": 31500000000.0,
        "net_flow_day": 680000000.0,
        "up": 11,
        "down": 6,
        "flat": 1,
        "text": "Volume is 1.3 times normal for this time of day. Buyers 58%.",
    },
    "minutes": [
        [DAY, 412000000.0, 300000000.0, 239000000.0, 173000000.0, 680000000.0, 1.31, 1.4],
        [DAY + 60, 300000000.0, 310000000.0, 100000000.0, 200000000.0, 580000000.0, 1.2, 1.0],
    ],
    "index": {"key": "NIFTY", "label": "NIFTY 50", "minutes": [[DAY, 25016.2], [DAY + 60, 25010.0]]},
    "stocks": [
        {
            "key": "HDFCBANK",
            "label": "HDFC Bank",
            "last": 1712.5,
            "chg_pct": -0.8,
            "turnover_day": 4100000000.0,
            "share": 0.12,
            "rvol_5m": 2.1,
            "rvol_day": 1.2,
            "z": 2.8,
            "flow_share_5m": 0.31,
            "flow_share_day": 0.47,
            "state": "busy",
        },
        {
            "key": "SBIN",
            "label": "State Bank <of> India",
            "last": 842.6,
            "chg_pct": 0.4,
            "turnover_day": 2000000000.0,
            "share": 0.06,
            "rvol_5m": 0.5,
            "rvol_day": 0.9,
            "z": None,
            "flow_share_5m": None,
            "flow_share_day": 0.5,
            "state": "quiet",
        },
    ],
    "indices": [
        {
            "key": "BANKNIFTY",
            "label": "NIFTY BANK",
            "trade": True,
            "pressure_5m": -0.42,
            "pressure_day": -0.10,
            "r2": 0.93,
            "top": [["HDFCBANK", 0.31], ["ICICIBANK", 0.27], ["SBIN", 0.10]],
            "text": "Selling pressure, led by HDFC Bank and ICICI Bank.",
        },
        {
            "key": "MIDCPNIFTY",
            "label": "NIFTY MIDCAP SELECT",
            "trade": False,
            "pressure_5m": 0.2,
            "pressure_day": 0.0,
            "r2": 0.4,
            "top": [],
            "text": "Mild buying.",
        },
    ],
    "alerts": [
        {
            "id": "v_20261125_1139_MARKET",
            "ts": "2026-11-25T11:39:00+05:30",
            "scope": "MARKET",
            "key": "MARKET",
            "rvol": 3.2,
            "z": 3.4,
            "flow_share": 0.31,
            "direction": "SELLING",
            "leaders": ["HDFCBANK", "ICICIBANK"],
            "indices": ["BANKNIFTY", "FINNIFTY"],
            "title": "Volume watch: 3.2 times normal, mostly selling",
            "text": "Led by HDFC Bank and ICICI Bank. Likely to move: NIFTY BANK, FINNIFTY.",
            "evidence": "After such spikes the next 15 minutes were 1.7 times as large as usual; direction was a coin flip.",
        }
    ],
    "book": {
        "as_of": "2026-11-25T11:41:30+05:30",
        "bid_share": 0.54,
        "stocks": [
            {
                "key": "HDFCBANK",
                "bid_qty": 182000,
                "ask_qty": 240000,
                "bid_share": 0.43,
                "spread_bps": 1.2,
            }
        ],
        "note": "Resting orders in the best five price steps. Recorded since 25 Nov 2026: not yet studied.",
    },
    "settings": {
        "z_alert": 3.0,
        "stock_z_alert": 4.0,
        "cooldown_min": 15,
        "alerts_on": True,
        "notify": True,
    },
    "notes": ["Stock bars end at 15:14 since the closing auction began."],
}
PROFILE = {
    "as_of": "2026-11-24",
    "sessions": 60,
    "minutes": [["09:15", 1210000000.0, 980000000.0, 1500000000.0]],
    "half_hours": [{"from": "09:15", "to": "09:44", "share": 0.19, "rank": 1}],
    "busiest": [{"from": "09:15", "to": "09:44", "share": 0.19}],
    "quietest": [{"from": "12:15", "to": "12:44", "share": 0.04}],
    "weekdays": [{"day": "Mon", "rvol": 0.97}],
    "expiry": {"expiry_day_rvol": 1.12, "other_days_rvol": 0.98, "note": "NIFTY expiry days"},
    "text": "The first half hour carries about a fifth of the day's volume.",
}
STUDY = {
    "as_of": "2026-11-24",
    "note": "docs/research/VOLUME_STUDY_2026-11-24.md",
    "facts": [{"id": "x", "text": "After a market-wide spike it was 1.7 times as large.", "value": 1.7}],
    "alerts_per_day": {"market": 1.8, "stock": 4.5},
}


def lifted() -> str:
    app, vol, parts = APP_JS.read_text("utf-8"), VOL_JS.read_text("utf-8"), []
    for name in APP_ONE_LINERS:
        m = re.search(rf"^const {name} = .*;$", app, re.M)
        assert m, f"const {name} not found as one line in app.js"
        parts.append(m.group(0))
    for name in VOL_ONE_LINERS:
        m = re.search(rf"^const {name} = .*$", vol, re.M)
        assert m, f"const {name} not found as one line in volume.js"
        parts.append(m.group(0))
    for name in VOL_BLOCKS:
        m = re.search(rf"^const {name} = [\[{{].*?^[\]}}];$", vol, re.M | re.S)
        assert m, f"const {name} block not found in volume.js"
        parts.append(m.group(0))
    for name in VOL_CONST_BLOCKS:
        m = re.search(rf"^const {name} = .*?^\}};$", vol, re.M | re.S)
        assert m, f"const {name} block not found in volume.js"
        parts.append(m.group(0))
    for name in VOL_WRAPPED:
        m = re.search(rf"^const {name} = .*?;$", vol, re.M | re.S)
        assert m, f"const {name} statement not found in volume.js"
        parts.append(m.group(0))
    for name in VOL_FUNCTIONS:
        m = re.search(rf"^function {name}\(.*?^\}}$", vol, re.M | re.S)
        assert m, f"function {name} not found in volume.js"
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
        f"const fake = require({json.dumps(str(VOL_FAKE))});\n"
        f"(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_node
class TestFormatters:
    def test_rupees_in_crore(self):
        out = run(
            "return [crore(34000000000), crore(412000000), crore(1500000), crore(5000), crore(-680000000), crore(null), crore('x'), crore(123456000000)];"
        )
        assert out == [
            "₹3,400 crore",
            "₹41.2 crore",
            "₹0.15 crore",
            "₹0.00 crore",
            "−₹68.0 crore",
            "–",
            "–",
            "₹12,346 crore",
        ]

    def test_times_normal_and_buyers_share(self):
        out = run(
            "return [times(1.31), timesNormal(1.31), timesNormal(null), buyersPct(0.58), buyersText(0.58), buyersText(null), buyersPct(0.5)];"
        )
        assert out == [
            "1.3 times",
            "1.3 times normal",
            "–",
            "58%",
            "Buyers 58%",
            "Buyers' share not known yet",
            "50%",
        ]

    def test_signed_and_day_words(self):
        out = run(
            "return [signed(-0.8), signed(0.4), signed(0), signed(null), dayWord('2026-11-23'), dayWord(null), clockOf('2026-11-25T11:39:00+05:30')];"
        )
        assert out == ["−0.80", "+0.40", "0.00", "–", "23 Nov", "–", "11:39"]

    def test_the_state_texts(self):
        out = run(
            "return [['quiet','normal','busy','spike','nonsense'].map(stateWord), STATE_HELP.quiet, STATE_HELP.busy, stateChip('spike'), stateChip('nonsense')];"
        )
        assert out[0] == ["Quiet", "Normal", "Busy", "Spike", "–"]
        assert "0.6" in out[1] and "1.5" in out[2]
        assert 'class="vol-chip spike"' in out[3] and ">Spike<" in out[3]
        assert 'class="vol-chip normal"' in out[4]  # an unknown state shows as normal, never as a broken chip

    def test_the_error_line(self):
        out = run(
            "return [errorText(new Error('the desk answered 503.'), true), errorText(new Error('boom'), false)];"
        )
        assert out == [
            "Could not refresh: the desk answered 503. Showing the last picture.",
            "The volume watch is not answering: boom.",
        ]


@needs_node
class TestRules:
    def test_closed_market_banner(self):
        out = run(
            "return [closedBanner({market_open:false}, ''), closedBanner({market_open:true}, ''), closedBanner({market_open:false}, '2026-11-20'), closedBanner(null, '')];"
        )
        assert out[0] == (
            "The market is closed. This is the last session. The watch starts again at 09:15."
        )
        assert out[1:] == ["", "", ""]  # open, a chosen past date, nothing at all: no banner

    def test_status_line(self):
        out = run(
            "return [statusLine({source:'live', session:'2026-11-25', now:{minute:'11:41'}}), statusLine({source:'stored', session:'2026-11-23', now:{minute:'15:29'}}), statusLine({source:'live', now:null}), statusLine(null)];"
        )
        assert out == [
            "Live now, last closed minute 11:41",
            "Stored session of 23 Nov, last closed minute 15:29",
            "Live now, no closed minute yet",
            "",
        ]

    def test_null_book_gives_one_plain_sentence(self):
        out = run(
            "return [bookIsEmpty(null), bookIsEmpty({stocks: []}), bookIsEmpty({stocks:[{}]}), bookHtml(null), bookHtml(PAYLOAD_BOOK)];".replace(
                "PAYLOAD_BOOK", json.dumps(EXAMPLE["book"])
            )
        )
        assert out[:3] == [True, True, False]
        assert "not shown now" in out[3] and "<table" not in out[3]
        assert "Buyers' share of resting orders: 54%" in out[4] and "<table" in out[4]
        assert "not yet studied" in out[4]

    def test_sort_by_a_column_both_ways_with_missing_last(self):
        stocks = [
            {"label": "b", "z": 1.0},
            {"label": "A", "z": None},
            {"label": "c", "z": 3.0},
            {"label": "d", "z": 2.0},
        ]
        out = run(
            f"const s = {json.dumps(stocks)}; return [sortStocks(s,'z','desc').map(x=>x.label), sortStocks(s,'z','asc').map(x=>x.label), sortStocks(s,'label','asc').map(x=>x.label), s.map(x=>x.label)];"
        )
        assert out[0] == ["c", "d", "b", "A"]
        assert out[1] == ["b", "d", "c", "A"]  # a missing value goes last whichever way
        assert out[2] == ["A", "b", "c", "d"]
        assert out[3] == ["b", "A", "c", "d"]  # the input is not changed

    def test_alerts_newest_first(self):
        out = run(
            "return newestFirst([{ts:'2026-11-25T10:00:00+05:30',id:1},{ts:'2026-11-25T11:39:00+05:30',id:2},{ts:'2026-11-24T14:00:00+05:30',id:3}]).map(a=>a.id);"
        )
        assert out == [2, 1, 3]

    def test_pressure_words_and_bars(self):
        out = run(
            "return [pressureWord(-0.42), pressureWord(0.3), pressureWord(0.01), pressureWord(null), clampHalf(-0.42), clampHalf(3), clampHalf(-9), clampHalf(null), pressBar(-0.42, 0.1)];"
        )
        assert out[:8] == ["Selling pressure", "Buying pressure", "Balanced", "–", -21, 50, -50, 0]
        assert 'right:50%;width:21%' in out[8] and 'left:50%;width:5%' in out[8]


@needs_node
class TestChartPoints:
    def test_minute_rows_become_points_in_indian_time(self):
        out = run(
            f"const p = chartPoints({json.dumps(EXAMPLE['minutes'])}, {json.dumps(EXAMPLE['index']['minutes'])}); return p;"
        )
        t0 = DAY + 19800
        assert out["turnover"][0] == {"time": t0, "value": 412000000.0, "side": "buy"}
        assert out["turnover"][1]["side"] == "sell"  # 100m buying against 200m selling
        assert out["expected"][0] == {"time": t0, "value": 300000000.0}
        assert out["net"][1] == {"time": t0 + 60, "value": 580000000.0}
        assert out["index"][1] == {"time": t0 + 60, "value": 25010.0}

    def test_unordered_rows_are_sorted_and_broken_rows_skipped(self):
        rows = [[DAY + 60, 2, 1, 1, 1, 0, 1, 0], [DAY, 1, None, 1, 0, 5, 1, 0], [None, 9], "x"]
        out = run(f"return chartPoints({json.dumps(rows)}, [[{DAY}, null]]);")
        assert [p["time"] for p in out["turnover"]] == [DAY + 19800, DAY + 19860]
        assert [p["time"] for p in out["expected"]] == [DAY + 19860]  # a null expected is left out
        assert out["index"] == []

    def test_nothing_gives_empty_series(self):
        assert run("return chartPoints(null, null);") == {
            "turnover": [],
            "expected": [],
            "net": [],
            "index": [],
        }

    def test_an_alert_snaps_to_the_last_drawn_minute_at_or_before_it(self):
        t = DAY + 19800
        out = run(
            f"const ts=[{t},{t + 60},{t + 120}]; return [alertTime('{ist(DAY + 60)}', ts), alertTime('{ist(DAY + 100)}', ts), alertTime('{ist(DAY - 600)}', ts), alertTime('nonsense', ts)];"
        )
        assert out == [t + 60, t + 60, None, None]

    def test_the_hover_readout(self):
        row = EXAMPLE["minutes"][0]
        out = run(f"return [minuteReadout({json.dumps(row)}, 25016.2), minuteReadout(null, 1)];")
        assert out[0] == (
            "10:25: turnover ₹41.2 crore (1.4 times normal), Buyers 58%, net flow so far ₹68.0 crore, index 25,016.2"
        )
        assert out[1] == ""


@needs_node
class TestRender:
    def test_the_contract_example_renders_every_section(self):
        out = run(
            f"const d = {json.dumps(EXAMPLE)}; const h = sectionsHtml(d, {{key:'turnover_day', dir:'desc'}}, ''); return {{h, tiles: nowTilesHtml(d.now), head: headlineHtml(d), ix: indicesHtml(d), pf: profileHtml({json.dumps(PROFILE)}), st: studyHtml({json.dumps(STUDY)}), legend: LEGEND.map(x=>x[0])}};"
        )
        h = out["h"]
        assert h["banner"] == "" and h["status"] == "Live now, last closed minute 11:41"
        assert "Volume is 1.3 times normal for this time of day. Buyers 58%." in h["headline"]
        assert 'class="vol-chip normal"' in h["headline"]
        for text in ("1.4 times", "1.3 times", "1.1 times", "Buyers 58%", "Buyers 51%", "₹3,400 crore", "11", "6"):
            assert text in out["tiles"] or text in ("11", "6"), text
        assert "1 unchanged" in out["tiles"] and "normal by now: ₹3,150 crore" in out["tiles"]
        assert "HDFC Bank" in h["stocks"] and "₹410 crore" in h["stocks"]
        assert "State Bank &lt;of&gt; India" in h["stocks"]  # names are escaped
        assert "Not in the picture (no bars yet): TMPV." in h["stocks"]
        assert "The basket explains 93% of its minute moves." in h["indices"]
        assert "Selling pressure, led by HDFC Bank and ICICI Bank." in h["indices"]
        assert "HDFC Bank 31%, ICICI Bank 27%, State Bank of India 10%" not in h["indices"]
        assert out["ix"].count("Training only") == 1  # only the index with trade: false
        assert "Volume watch: 3.2 times normal, mostly selling" in h["alerts"]
        assert "coin flip" in h["alerts"] and "11:39" in h["alerts"]
        assert "Stock bars end at 15:14" in h["notes"]
        assert "busiest" not in h  # sections are exactly these
        assert "09:15 to 09:44 (19% of a normal day)" in out["pf"] and "12:15 to 12:44" in out["pf"]
        assert "NIFTY expiry days: 1.1 times the usual volume" in out["pf"]
        assert "about 1.8 market-wide alerts and 4.5 single-stock alerts" in out["st"]
        assert "Turnover" in out["legend"] and "Order book" in out["legend"]

    def test_the_closed_answer_shows_the_banner_and_no_book(self):
        d = dict(EXAMPLE, market_open=False, source="stored", book=None)
        out = run(f"return sectionsHtml({json.dumps(d)}, {{key:'label', dir:'asc'}}, '');")
        assert "The market is closed." in out["banner"] and 'class="card vol-closed"' in out["banner"]
        assert out["status"].startswith("Stored session of 25 Nov")
        assert "not shown now" in out["book"]

    def test_empty_answers_do_not_break(self):
        d = {
            "source": "live",
            "market_open": True,
            "now": None,
            "minutes": [],
            "stocks": [],
            "indices": [],
            "alerts": [],
            "book": None,
        }
        out = run(f"return sectionsHtml({json.dumps(d)}, {{key:'z', dir:'desc'}}, '');")
        assert "No closed minute yet" in out["headline"] and "No closed minute yet" in out["tiles"]
        assert "No stock has bars yet" in out["stocks"] and "No index pressure yet" in out["indices"]
        assert "No watch alerts in this session." in out["alerts"]

    def test_the_table_headings_are_keyboard_buttons_with_a_sort_state(self):
        out = run(
            f"return stocksTableHtml({json.dumps(EXAMPLE['stocks'])}, {{key:'z', dir:'asc'}});"
        )
        assert out.count('tabindex="0" role="button"') == 10
        assert 'data-sort="z" tabindex="0" role="button" aria-sort="ascending"' in out
        assert out.count('aria-sort="none"') == 9

    def test_a_missing_value_is_a_dash_not_nan(self):
        out = run(f"return stocksTableHtml({json.dumps(EXAMPLE['stocks'])}, {{key:'z', dir:'desc'}});")
        assert "NaN" not in out and "undefined" not in out and "null" not in out

    def test_earlier_alerts(self):
        out = run(
            "return [earlierHtml(null), earlierHtml({alerts: []}), earlierHtml({alerts:[{session:'2026-11-24', ts:'2026-11-24T10:12:00+05:30', title:'T', text:'x', direction:'BUYING'}]})];"
        )
        assert out[0] == ""
        assert "No earlier alerts are stored yet." in out[1]
        assert "24 Nov 10:12" in out[2] and "vol-alert buy" in out[2]

    def test_the_index_picker_lists_nifty_the_indices_and_the_chosen_one(self):
        out = run(
            "return indexOptions({indices:[{key:'BANKNIFTY',label:'NIFTY BANK'}], index:{key:'SENSEX',label:'SENSEX'}}, 'BANKNIFTY');"
        )
        assert out.count("<option") == 3 and 'value="BANKNIFTY" selected' in out
        assert 'value="NIFTY"' in out and 'value="SENSEX"' in out

    def test_the_legend_and_the_watch_sentence(self):
        js = VOL_JS.read_text("utf-8")
        assert (
            "A watch alert is not a trade alert: it tells you where unusual volume is, not what to buy."
            in js
        )
        assert "not a count of buyers" in js


SMOKE = r"""
const BODY = %BODY%, PROFILE = %PROFILE%, STUDY = %STUDY%;
const els = {};
const mk = (id) => ({ id, innerHTML: "", textContent: "", innerText: "", value: "", max: "", disabled: false, hidden: false, dataset: {}, style: {},
  addEventListener() {}, querySelector() { return null; }, appendChild() {}, closest() { return null; }, focus() {}, scrollIntoView() {},
  classList: { add() {}, remove() {}, toggle() {}, contains() { return true; } }, children: [] });
const byId = (id) => (els[id] = els[id] || mk(id));
globalThis.window = globalThis; globalThis.addEventListener = () => {};
globalThis.location = { search: "" };
globalThis.document = { getElementById: byId, hidden: false, documentElement: { dataset: {} }, createElement: () => mk("x"), head: { appendChild() {} }, addEventListener() {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.MutationObserver = class { observe() {} };
globalThis.setInterval = () => 0;
const asked = [];
globalThis.fetch = async (url) => { asked.push(url); const body = url.startsWith("/api/volume/profile") ? PROFILE : url.startsWith("/api/volume/study") ? STUDY : url.startsWith("/api/volume/alerts") ? { alerts: [] } : BODY; return { ok: true, status: 200, json: async () => body }; };
const store = { get: (k, d) => d, set() {} };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
const loaders = {};
window.OneLot = { $: byId, esc, fmt: (v) => String(v), store, tabShown: () => true, tabLoaders: loaders };
%SCRIPT%
(async () => {
  await new Promise((r) => setTimeout(r, 50));
  console.log(JSON.stringify({ asked, loader: typeof loaders.volume, status: els["vol-status"].innerHTML, head: els["vol-headline"].innerHTML, stocks: els["vol-stocks"].innerHTML,
    profile: els["vol-profile"].innerHTML, study: els["vol-study"].innerHTML, err: els["vol-err"].textContent }));
})();
"""


@needs_node
def test_the_whole_script_renders_the_contract_example_in_a_stubbed_page():
    script = (
        SMOKE.replace("%BODY%", json.dumps(EXAMPLE))
        .replace("%PROFILE%", json.dumps(PROFILE))
        .replace("%STUDY%", json.dumps(STUDY))
        .replace("%SCRIPT%", VOL_JS.read_text("utf-8"))
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["loader"] == "function"
    assert any(a.startswith("/api/volume?index=NIFTY") for a in out["asked"])
    assert "/api/volume/profile" in out["asked"] and "/api/volume/study" in out["asked"]
    assert out["status"] == "Live now, last closed minute 11:41"
    assert "Volume is 1.3 times normal" in out["head"]
    assert "HDFC Bank" in out["stocks"]
    assert "first half hour" in out["profile"] and "1.7 times" in out["study"]
    assert out["err"] == ""


@needs_node
class TestFake:
    def test_the_live_answer_has_the_contract_keys_and_a_spike_with_an_alert(self):
        d = run_fake("return await fake.handle('/api/volume?index=BANKNIFTY');")
        for key in (
            "as_of",
            "session",
            "market_open",
            "source",
            "basket",
            "now",
            "minutes",
            "index",
            "stocks",
            "indices",
            "alerts",
            "book",
            "settings",
            "notes",
        ):
            assert key in d, key
        assert d["source"] == "live" and d["market_open"] is True
        assert d["index"]["key"] == "BANKNIFTY" and len(d["index"]["minutes"]) == len(d["minutes"])
        assert d["now"]["state"] == "spike" and any(a["scope"] == "MARKET" for a in d["alerts"])
        assert all(len(r) == 8 for r in d["minutes"])
        times = [r[0] for r in d["minutes"]]
        assert times == sorted(times) and len(set(times)) == len(times)
        assert d["book"] and d["book"]["stocks"]
        assert [s["turnover_day"] for s in d["stocks"]] == sorted(
            (s["turnover_day"] for s in d["stocks"]), reverse=True
        )

    def test_a_past_date_is_stored_without_book_and_a_bad_one_is_a_400_with_a_sentence(self):
        out = run_fake(
            "const d = await fake.handle('/api/volume?date=2026-11-20'); let bad = null; try { await fake.handle('/api/volume?date=2020-01-01'); } catch (e) { bad = [e.status, e.message]; } return [d.source, d.book, d.session, d.market_open, bad];"
        )
        assert out[:4] == ["stored", None, "2026-11-20", False]
        assert out[4][0] == 400 and "2026-09-01" in out[4][1]

    def test_profile_study_and_earlier_alerts(self):
        out = run_fake(
            "const p = await fake.handle('/api/volume/profile'); const s = await fake.handle('/api/volume/study'); const a = await fake.handle('/api/volume/alerts?limit=2'); return [Object.keys(p), p.minutes.length, p.busiest.length, s.facts.length, a.alerts.length, a.alerts[0].session];"
        )
        assert {"minutes", "half_hours", "busiest", "quietest", "weekdays", "expiry", "text"} <= set(
            out[0]
        )
        assert out[1] == 375 and out[2] == 2 and out[3] == 2 and out[4] == 2 and out[5]

    def test_the_demo_switches(self):
        script = (
            "globalThis.location = { search: '?volfake=1&volbook=0&volclosed=1' };"
            f"const fake = require({json.dumps(str(VOL_FAKE))});"
            "(async () => { const d = await fake.handle('/api/volume'); console.log(JSON.stringify([d.market_open, d.source, d.book])); })();"
        )
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout.strip().splitlines()[-1]) == [False, "stored", None]
        script = script.replace("&volbook=0&volclosed=1", "&volerr=1").replace(
            "const d = await fake.handle('/api/volume'); console.log(JSON.stringify([d.market_open, d.source, d.book]));",
            "try { await fake.handle('/api/volume'); console.log('ok'); } catch (e) { console.log(JSON.stringify(e.status)); }",
        )
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert done.stdout.strip() == "503"


class TestStatic:
    def test_volume_css_has_no_colour_literal_and_no_small_type(self):
        css = re.sub(r"/\*.*?\*/", "", VOL_CSS.read_text("utf-8"), flags=re.S)
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(", css)
        values = re.sub(r"var\([^)]*\)", "", " ".join(re.findall(r":\s*([^;{}]+)", css)))
        named = r"\b(?:white|black|red|green|blue|yellow|orange|purple|pink|gray|grey|cyan|magenta|teal|navy|maroon|gold|silver|lime)\b"
        assert not re.findall(named, values, re.I)
        sizes = [float(x) for x in re.findall(r"font-size\s*:\s*([\d.]+)px", css)]
        assert sizes and min(sizes) >= 14

    def test_volume_js_and_the_fake_hard_code_no_colour(self):
        for path in (VOL_JS, VOL_FAKE):
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text("utf-8"), flags=re.S)
            assert not re.findall(
                r"(?<![\w&-])#[0-9a-fA-F]{3,8}\b(?![\w-])|\b(?:rgba?|hsla?)\s*\(", code
            ), path.name

    def test_the_fake_is_loaded_only_with_volfake_in_the_url(self):
        js = VOL_JS.read_text("utf-8")
        assert js.count("/static/volume_fake.js") == 1 and "volfake=1" in js
        assert "volume_fake" not in INDEX.read_text("utf-8")

    def test_the_markup_has_the_root_the_files_and_a_nav_entry_after_live_desk(self):
        html = INDEX.read_text("utf-8")
        assert html.count('<div id="volume-root">') == 1
        assert '<script src="/static/volume.js" defer></script>' in html
        assert '<link rel="stylesheet" href="/static/volume.css">' in html
        assert '<section data-tab="volume" class="tab">' in html
        links = re.findall(r'<a href="#(\w+)" data-tab="\w+">', html)
        assert links[:4] == ["home", "live", "volume", "sim"] and len(links) == 9
        assert '<a href="#volume" data-tab="volume">Volume</a>' in html

    def test_every_id_the_script_uses_is_built_by_the_script(self):
        js = VOL_JS.read_text("utf-8")
        used = set(re.findall(r'\$\("(vol-[\w-]+)"\)', js))
        assert used
        for i in used:
            assert f'id="{i}"' in js, i

    def test_the_number_keys_reach_the_ninth_tab(self):
        app = APP_JS.read_text("utf-8")
        assert "Number keys 1 to 9" in app and "/^[1-9]$/" in app
        assert "keys 1 to 9" in INDEX.read_text("utf-8")

    def test_the_header_keeps_nine_tabs_on_one_row_by_spacing_not_type(self):
        css = (UI / "styles.css").read_text("utf-8")
        assert "US-090" in css and "min-width:900px) and (max-width:1299px" in css
        block = css.split("US-090", 1)[1].split("\n", 2)[1]
        assert "font-size" not in block

    def test_the_page_polls_every_20_seconds_one_request_at_a_time_and_remembers_the_index(self):
        js = VOL_JS.read_text("utf-8")
        assert "POLL_MS = 20000" in js and "if (S.busy) return;" in js
        assert "document.hidden" in js and "!S.date" in js
        assert 'store.set("volume.index"' in js
