"""The micro trend on the Live Desk cards: the pure render helpers in src/onelot/ui/app.js
and the card that uses them (developer tests, 2026-11-11).

    trendVerdict(trend)       [class, words]: "all 6 up" only when every timeframe agrees
    trendPips(trend)          the strip under a card's head: one pip per timeframe, then the count
    trendBlock(trend, price)  the table in an open card and in the focus panel
    trendSince(row, at)       when a timeframe's trend began, as the card words it
    trendGap(row, price)      how far the last price stands from a timeframe's line
    slotTrail(slot)           the Trail column: "60m line", "ATR × 1.5" or "–"
    slotTrailNote(slot, kind) the line under the table: where the line lets a stop stand now
    accRow(row, open)         the whole card, lifted with what it calls

`trend` is the `trend` entry of a desk row (desk/tracker.py `_read_trend`) and a slot's
`trail` the `trail` entry of `StrategySlot.as_dict`. An engine started before 2026-11-11
sends neither; the card must still draw.

No browser: the helpers are lifted out of app.js and run in node. Skipped without node.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"
#: single-line `const name = ...;` helpers, in the order they are declared
HELPERS = [
    "fmt",
    "cls",
    "esc",
    "kv",
    "spct",
    "tsf",
    "hhmm",
    "trendRows",
    "trendCls",
    "trendWord",
    "trendMark",
    "trendVerdict",
    "trendDay",
    "trendSince",
    "trendGap",
    "trendTitle",
    "trendPips",
    "trendBlock",
    "slotTrail",
    "slotTrailNote",
]
#: what accRow needs beside them, each matched whole
BLOCKS = [
    r"^const rup = .*\n  : .*;$",  # a two-line const
    r"^function table\(.*?^}$",
    r"^function hdot\(.*}$",
    r"^function accRow\(.*?^}$",
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def js(expression: str, payload: Any = None) -> Any:
    """Evaluate `expression` (P is the JSON payload) after the helpers and the card."""
    source = APP_JS.read_text(encoding="utf-8")
    lines = ["let latest = null;"]
    for name in HELPERS:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
        assert found, f"`const {name} = ...;` not found as a single line in {APP_JS}"
        lines.append(found.group(0))
    for pattern in BLOCKS:
        found = re.search(pattern, source, re.M | re.S if "?" in pattern else re.M)
        assert found, f"{pattern!r} not found in {APP_JS}"
        lines.append(found.group(0))
    lines += [
        "const P = JSON.parse(process.argv[1]);",
        f"console.log(JSON.stringify({expression}));",
    ]
    done = subprocess.run(
        ["node", "-e", "\n".join(lines), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def rung(tf: str, direction: int, line: float | None, since: str | None, exact: bool = True):
    return {
        "timeframe": tf,
        "direction": direction,
        "line": line,
        "since": since,
        "since_exact": exact,
    }


def ladder(*directions: int, note: str | None = None) -> dict[str, Any]:
    """A `trend` entry read at 11:07 on 2026-11-06 (NIFTY's real numbers), one direction per
    timeframe, fastest first; 0 = that timeframe has no reading."""
    frames = [
        ("1m", 24598.38, "2026-11-06T11:03:00+05:30"),
        ("5m", 24663.77, "2026-11-06T10:50:00+05:30"),
        ("15m", 24571.72, "2026-11-04T13:15:00+05:30"),
        ("30m", 24527.61, "2026-11-04T14:15:00+05:30"),
        ("60m", 24368.77, "2026-10-29T09:15:00+05:30"),
        ("1d", 23897.97, "2026-10-15T09:15:00+05:30"),
    ]
    rows = [
        rung(tf, d, line if d else None, since if d else None)
        for (tf, line, since), d in zip(frames, directions, strict=True)
    ]
    return {
        "period": 14,
        "mult": 1.7,
        "base": "1m",
        "rows": rows,
        "up": sum(d > 0 for d in directions),
        "down": sum(d < 0 for d in directions),
        "aligned": directions[0] if len(set(directions)) == 1 else 0,
        "unavailable": [tf for (tf, _, _), d in zip(frames, directions, strict=True) if not d],
        "at": "2026-11-06T11:07:00+05:30",
        "note": note,
    }


ALL_UP = ladder(1, 1, 1, 1, 1, 1)
ALL_DOWN = ladder(-1, -1, -1, -1, -1, -1)
MIXED = ladder(1, -1, 1, 1, 1, 1)
TWO_MINUTE_FEED = ladder(0, 0, 0, 1, 1, 1, note="1m, 5m and 15m cannot be built from 2-minute bars")

ATR_SLOT = {
    "name": "supertrend_vol",
    "label": "Supertrend (tuned) + volume + momentum",
    "timeframe": "30m",
    "params": {"allow_long": True, "allow_short": False, "trail_atr_mult": 1.5},
    "last_bar_ts": "2026-11-06T10:45:00+05:30",
    "verdict": "none",
    "reason": "no setup on the last bar (blocked: window)",
    "strength": None,
    "signals_today": 0,
    "error": None,
    "trail": None,
}
LINE_SLOT = {
    "name": "supertrend_mtf",
    "label": "Supertrend multi-timeframe (micro trend)",
    "timeframe": "5m",
    "params": {"allow_long": True, "allow_short": False, "trail_tf": "60m", "stop_buffer_atr": 2.0},
    "last_bar_ts": "2026-11-06T11:00:00+05:30",
    "verdict": "long",
    "reason": "entry on the last closed bar",
    "strength": 50.0,
    "signals_today": 0,
    "error": None,
    "trail": {"timeframe": "60m", "long": 24141.5893, "short": None},
}
ROW = {
    "key": "NIFTY",
    "label": "NIFTY 50",
    "kind": "INDEX",
    "note": "",
    "tracked": "NIFTY_OPT",
    "unit": "1 lot (65)",
    "state": "WATCH",
    "last": {
        "price": 24622.9,
        "change_pct": 0.3517,
        "prev_close": 24536.6,
        "bar_ts": "2026-11-06T11:06:00+05:30",
        "age_sec": 0.0,
        "live": None,
    },
    "strategies": [ATR_SLOT, LINE_SLOT],
    "regime": {"regime": "neutral", "adx": 30.1, "atr": 40.2, "trend": "up"},
    "trend": MIXED,
    "risk": {"trades_today": 0, "consecutive_losses": 0, "pnl_today": 0.0},
    "health": {"state": "OK", "note": "live", "bars": 6391, "source": "dhan", "resolution_min": 1},
}


# ── the verdict ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("trend", "expected"),
    [
        (ALL_UP, ["up", "all 6 up"]),
        (ALL_DOWN, ["down", "all 6 down"]),
        (MIXED, ["mixed", "5 up · 1 down"]),
        (ladder(1, 1, -1, -1, 1, -1), ["mixed", "3 up · 3 down"]),
    ],
)
def test_the_verdict_counts_the_timeframes(trend, expected):
    assert js("trendVerdict(P)", trend) == expected


def test_a_timeframe_without_a_reading_breaks_the_agreement():
    """Three up and three unreadable is not "all up": the strategy's gate stays shut too."""
    assert js("trendVerdict(P)", TWO_MINUTE_FEED) == ["mixed", "3 up · 0 down · 3 no reading"]
    assert js("trendVerdict(P)", ladder(0, -1, -1, -1, -1, -1)) == [
        "mixed",
        "0 up · 5 down · 1 no reading",
    ]


@pytest.mark.parametrize(
    "trend", [None, {}, {"rows": []}, {"rows": "junk"}, ladder(0, 0, 0, 0, 0, 0)]
)
def test_no_reading_at_all_is_said_so(trend):
    assert js("trendVerdict(P)", trend) == ["none", "no trend reading"]


# ── the strip under a card's head ─────────────────────────────────────────────
def test_the_strip_has_one_pip_per_timeframe_fastest_first():
    html = js("trendPips(P)", MIXED)
    pips = re.findall(r'<span class="tpip (\w+)">(.) ([^<]+)</span>', html)
    assert pips == [
        ("up", "▲", "1m"),
        ("down", "▼", "5m"),
        ("up", "▲", "15m"),
        ("up", "▲", "30m"),
        ("up", "▲", "60m"),
        ("up", "▲", "1d"),
    ]
    assert '<span class="tverdict mixed">5 up · 1 down</span>' in html
    assert 'title="Supertrend(14, 1.7): 1m up · 5m down · 15m up · 30m up · 60m up · 1d up"' in html


def test_the_strip_marks_agreement_and_missing_readings():
    assert '<span class="tverdict up">all 6 up</span>' in js("trendPips(P)", ALL_UP)
    assert '<span class="tverdict down">all 6 down</span>' in js("trendPips(P)", ALL_DOWN)
    html = js("trendPips(P)", TWO_MINUTE_FEED)
    assert html.count('<span class="tpip none">· ') == 3
    assert "3 up · 0 down · 3 no reading" in html


@pytest.mark.parametrize("trend", [None, {}, {"rows": []}])
def test_the_strip_of_a_row_without_a_trend_says_no_reading_yet(trend):
    """An engine started before the ladder existed sends no `trend`."""
    html = js("trendPips(P)", trend)
    assert html.startswith('<div class="trend-strip">')
    assert "no reading yet" in html
    assert "tpip" not in html


def test_the_strip_escapes_what_it_shows():
    trend = copy.deepcopy(ALL_UP)
    trend["rows"][0]["timeframe"] = '<img src=x onerror="x()">'
    html = js("trendPips(P)", trend)
    assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;x()&quot;&gt;" in html


# ── since when, and how far from the line ─────────────────────────────────────
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (rung("1m", 1, 1.0, "2026-11-06T11:03:00+05:30"), "11:03"),  # the day of the reading
        (rung("15m", 1, 1.0, "2026-11-04T13:15:00+05:30"), "4 Nov 13:15"),  # an earlier day
        (rung("1d", 1, 1.0, "2026-10-15T09:15:00+05:30"), "15 Oct"),  # the daily: the day only
        (rung("1d", 1, 1.0, "2026-11-06T09:15:00+05:30"), "6 Nov"),
        (rung("60m", -1, 1.0, "2026-10-12T09:15:00+05:30", exact=False), "12 Oct 09:15 or earlier"),
        (rung("1d", -1, 1.0, "2026-07-01T09:15:00+05:30", exact=False), "1 Jul or earlier"),
        (rung("5m", 0, None, None), "–"),
        (None, "–"),
    ],
)
def test_since_is_a_time_today_and_a_day_before(row, expected):
    assert js('trendSince(P, "2026-11-06T11:07:00+05:30")', row) == expected


@pytest.mark.parametrize(
    ("line", "price", "expected"),
    [
        (24368.77, 24622.9, "1.04% above"),
        (24663.77, 24622.9, "0.17% below"),
        (100.0, 100.0, "0.00% above"),
        (None, 24622.9, "–"),
        (24368.77, None, "–"),
        (0, 24622.9, "–"),
        ("junk", 24622.9, "–"),
    ],
)
def test_the_gap_is_the_room_between_the_price_and_the_line(line, price, expected):
    assert js("trendGap(P.row, P.price)", {"row": rung("60m", 1, line, None), "price": price}) == (
        expected
    )


# ── the table in an open card and in the focus panel ──────────────────────────
def test_the_table_has_a_row_per_timeframe():
    html = js("trendBlock(P, 24622.9)", MIXED)
    assert "<h4>Micro trend · Supertrend(14, 1.7)</h4>" in html
    body = html.split("<tbody>")[1].split("</tbody>")[0]
    assert body.count("<tr>") == 6
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td.*?</td>", body, re.S)]
    assert cells[:5] == ["1m", "▲ up", "24598.38", "0.10% above", "11:03"]
    assert cells[5:10] == ["5m", "▼ down", "24663.77", "0.17% below", "10:50"]
    assert cells[25:30] == ["1d", "▲ up", "23897.97", "3.03% above", "15 Oct"]
    assert '<td class="tdir down">▼ down</td>' in html
    assert '<b class="tverdict mixed">5 up · 1 down</b>' in html
    assert "a micro trend needs all 6 to agree · read at 11:07 from 1m bars" in html


def test_the_table_shows_missing_readings_and_the_note():
    html = js("trendBlock(P, 999.5)", TWO_MINUTE_FEED)
    assert html.count('<td class="tdir none">· no reading</td>') == 3
    assert '<div class="hint warn">1m, 5m and 15m cannot be built from 2-minute bars</div>' in html
    noted = js("trendBlock(P, 999.5)", ladder(1, 1, 1, 1, 1, 1, note="<b>stale</b> & old"))
    assert '<div class="hint warn">&lt;b&gt;stale&lt;/b&gt; &amp; old</div>' in noted


@pytest.mark.parametrize("trend", [None, {}, {"rows": []}])
def test_the_table_without_a_trend_is_a_plain_line(trend):
    html = js("trendBlock(P, 100)", trend)
    assert html == (
        '<h4>Micro trend</h4><div class="hint">No trend reading yet: it comes with the first bars.'
        "</div>"
    )


# ── how a slot trails its stop ────────────────────────────────────────────────
def test_the_trail_column_names_the_line_or_the_atr_multiple():
    assert js("slotTrail(P)", LINE_SLOT) == "60m line"
    assert js("slotTrail(P)", ATR_SLOT) == "ATR × 1.5"
    assert js("slotTrail(P)", {"params": {"trail_atr_mult": 0}, "trail": None}) == "–"
    assert js("slotTrail(P)", {"params": {}}) == "–"
    assert js("slotTrail(P)", None) == "–"


def test_a_slot_of_an_older_engine_has_no_trail_entry():
    old = {k: v for k, v in ATR_SLOT.items() if k != "trail"}
    assert js("slotTrail(P)", old) == "ATR × 1.5"
    assert js('slotTrailNote(P, "INDEX")', old) == ""


def test_the_note_gives_the_stop_level_for_the_sides_the_slot_trades():
    html = js('slotTrailNote(P, "INDEX")', LINE_SLOT)
    assert html == (
        '<div class="hint"><b>supertrend_mtf 5m</b> trails its stop along the 60m Supertrend line'
        " · CE stop level now <b>24141.59</b></div>"
    )  # long only: nothing about puts


def test_the_note_says_when_the_line_is_on_the_other_side():
    slot = copy.deepcopy(LINE_SLOT)
    slot["trail"] = {"timeframe": "60m", "long": None, "short": 24900.0}
    html = js('slotTrailNote(P, "INDEX")', slot)
    assert "no CE stop level now (the line is on the other side)" in html
    assert "PE" not in html


def test_the_note_of_a_two_sided_stock_slot_names_long_and_short():
    slot = copy.deepcopy(LINE_SLOT)
    slot["params"] = {"allow_long": True, "allow_short": True}
    slot["trail"] = {"timeframe": "30m", "long": None, "short": 1002.5}
    html = js('slotTrailNote(P, "STOCK")', slot)
    assert "along the 30m Supertrend line" in html
    assert "no long stop level now" in html
    assert "short stop level now <b>1002.50</b>" in html


def test_a_slot_that_trails_by_atr_has_no_note():
    assert js('slotTrailNote(P, "INDEX")', ATR_SLOT) == ""
    assert js('slotTrailNote(P, "INDEX")', None) == ""


def test_the_note_escapes_the_names():
    slot = copy.deepcopy(LINE_SLOT)
    slot["name"] = "<script>"
    slot["trail"]["timeframe"] = '"><i>'
    html = js('slotTrailNote(P, "INDEX")', slot)
    assert "<script>" not in html and "<i>" not in html
    assert "&lt;script&gt; 5m" in html


# ── the card ──────────────────────────────────────────────────────────────────
def test_the_card_carries_the_strip_in_its_click_target_and_the_table_in_its_body():
    html = js("accRow(P, true)", ROW)
    top, body = html.split('<div class="acc-body">', 1)
    assert '<div class="acc-row open" data-key="NIFTY">' in top
    assert '<div class="acc-top" data-toggle="NIFTY">' in top
    assert top.index('class="acc-head"') < top.index('class="trend-strip"')
    assert top.count("tpip") == 6
    assert "5 up · 1 down" in top
    assert '<table class="trend">' in body
    assert body.index("Micro trend") < body.index("<h4>Market</h4>")


def test_the_cards_strategies_table_has_the_trail_column_and_the_note():
    body = js("accRow(P, true)", ROW).split('<div class="acc-body">', 1)[1]
    table = body.split("<h4>Strategies running</h4>", 1)[1]
    assert ">Trail</th>" in table
    assert '<td class=" nw">ATR × 1.5</td>' in table
    assert '<td class=" nw">60m line</td>' in table
    assert "CE stop level now <b>24141.59</b>" in table


def test_a_row_of_an_older_engine_still_draws():
    """No `trend` on the row and no `trail` on its slots: the card says so and keeps working."""
    old = {k: v for k, v in ROW.items() if k != "trend"}
    old["strategies"] = [{k: v for k, v in ATR_SLOT.items() if k != "trail"}]
    html = js("accRow(P, false)", old)
    assert '<div class="acc-row " data-key="NIFTY">' in html
    assert "no reading yet" in html
    assert "No trend reading yet" in html
    assert "ATR × 1.5" in html
    assert "stop level now" not in html
