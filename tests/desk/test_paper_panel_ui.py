"""US-030 UI: the Paper panel's pure render helpers in src/onelot/ui/app.js (developer tests).

    paperChip(status)        the status chip: pending / open / no fill / closed
    paperRow(trade, today?)  one GET /api/paper trade as a <tr>, every field through esc()
    paperShown(trades, today) open trades (any day) first, then those closed on `today`
    paperPanel(payload)      the table, or "No paper trades yet" / "Paper trading is off"
    paperMeta(payload)       the plain-text day line (set with textContent)
    inr(rupees)              a rupee STRING from the API ("-12.05") -> "−₹12.05", no float

No browser: the one-line `const name = ...;` helpers are lifted out of app.js and run in
node with `Date` frozen (the panel's "today" is the IST calendar date). Skipped without node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"
HELPERS = [
    "esc",
    "cls",
    "tsf",
    "hhmm",
    "inr",
    "todayIST",
    "paperChip",
    "paperWhen",
    "paperRow",
    "paperShown",
    "paperPanel",
    "paperMeta",
]
TODAY = "2026-10-29"
FROZEN = "2026-10-29T15:00:00+05:30"
FREEZE = (
    "const __D = Date, __T = new __D(process.argv[2]).getTime();"
    "globalThis.Date = class extends __D { constructor(...a) { if (a.length) super(...a);"
    " else super(__T); } static now() { return __T; } };"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def js(expression: str, payload: Any = None, *, now: str = FROZEN) -> Any:
    """Evaluate `expression` (P is the JSON payload) after the app.js helpers, Date frozen."""
    source = APP_JS.read_text(encoding="utf-8")
    lines = [FREEZE]
    for name in HELPERS:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
        assert found, f"`const {name} = ...;` not found as a single line in {APP_JS}"
        lines.append(found.group(0))
    lines += [
        "const P = JSON.parse(process.argv[1]);",
        f"console.log(JSON.stringify({expression}));",
    ]
    done = subprocess.run(
        ["node", "-e", "\n".join(lines), json.dumps(payload), now],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def trade(**kw: Any) -> dict[str, Any]:
    """A GET /api/paper trade (an OPEN stock trade unless overridden)."""
    base = {
        "alert_id": "a1",
        "instrument": "RELIANCE",
        "side": "BUY",
        "direction": "long",
        "kind": "STOCK",
        "strategy": "ema_cross",
        "timeframe": "5m",
        "grade": "live",
        "status": "OPEN",
        "close_reason": None,
        "qty": 1,
        "entry_ts": f"{TODAY}T10:06:00+05:30",
        "entry": "1000.30",
        "exit": None,
        "gross": None,
        "net": None,
        "created_at": f"{TODAY}T10:05:03+05:30",
        "closed_at": None,
    }
    return {**base, **kw}


def closed(**kw: Any) -> dict[str, Any]:
    values = {
        "status": "CLOSED",
        "close_reason": "STOP",
        "exit": "988.25",
        "gross": "-12.05",
        "closed_at": f"{TODAY}T11:30:00+05:30",
    }
    return trade(**{**values, **kw})


def cell_classes(row_html: str, text: str) -> set[str]:
    """The classes of the <td> whose content is exactly `text`."""
    for attrs, inner in re.findall(r"<td([^>]*)>(.*?)</td>", row_html):
        if inner == text:
            found = re.search(r'class="([^"]*)"', attrs)
            return set(found.group(1).split()) if found else set()
    raise AssertionError(f"no cell showing {text!r} in {row_html}")


class TestInr:
    @pytest.mark.parametrize(
        ("rupees", "shown"),
        [
            ("1000.30", "₹1,000.30"),
            ("-12.05", "−₹12.05"),
            ("-0.05", "−₹0.05"),
            ("0.00", "₹0.00"),
            ("100.00", "₹100.00"),
            ("1234567.80", "₹12,34,567.80"),  # Indian grouping, from the string itself
            ("92233720368547758.07", "₹92,23,37,20,36,85,47,758.07"),  # beyond 2**53: exact
            (None, "–"),
            ("", "–"),
        ],
    )
    def test_formats_the_api_string_without_a_float(self, rupees, shown):
        assert js("inr(P)", rupees) == shown


class TestChip:
    @pytest.mark.parametrize(
        ("status", "css", "text"),
        [
            ("PENDING_FILL", "pending", "pending"),
            ("OPEN", "open", "open"),
            ("NO_FILL", "nofill", "no fill"),
            ("CLOSED", "closed", "closed"),
        ],
    )
    def test_each_status_has_its_chip(self, status, css, text):
        assert js("paperChip(P)", status) == f'<span class="pchip {css}">{text}</span>'

    @pytest.mark.parametrize("odd", ["constructor", "__proto__", "<b>X</b>", None])
    def test_an_unknown_status_is_shown_escaped_never_as_markup(self, odd):
        html = js("paperChip(P)", odd)
        assert html.startswith('<span class="pchip ">') and "<b>" not in html


class TestRow:
    def test_an_open_trade(self):
        html = js("paperRow(P)", trade())
        assert html.startswith("<tr>") and html.endswith("</tr>")
        assert '<span class="pchip open">open</span>' in html
        assert "<b>RELIANCE</b>" in html and "ema_cross · 5m" in html
        assert cell_classes(html, "₹1,000.30") == {"n"}
        assert html.count("<td") == 10

    def test_cells_read_as_separate_words_when_the_tags_are_dropped(self):
        # Copy-paste, screen readers and text search see the row's text, not its layout.
        text = re.sub(r"<[^>]+>", "", js("paperRow(P)", trade(side="BUY", grade="delayed")))
        assert re.search(r"\bBUY delayed\b", text), text
        header = re.sub(r"<[^>]+>", "", js("paperPanel(P)", {"enabled": True, "trades": [trade()]}))
        assert header.startswith("Status Instrument Side Grade Entry / unit Exit / unit"), header

    def test_the_header_says_entry_and_exit_are_per_unit_and_gross_net_per_trade(self):
        # One lot of 30 shows a ₹312.45 entry but a gross 30 times larger: the header says so.
        html = js("paperPanel(P)", {"enabled": True, "trades": [trade()]})
        head = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", html)  # <th>, never <thead>
        assert head == [
            "Status",
            "Instrument",
            "Side",
            "Grade",
            "Entry / unit",
            "Exit / unit",
            "Gross (trade)",
            "Net (trade)",
            "Close reason",
            "Time",
        ]
        assert html.count(' title="') >= 4  # each money column explains itself on hover

    def test_a_closed_trade_shows_exit_gross_and_a_dash_for_the_missing_net(self):
        html = js("paperRow(P)", closed())
        assert '<span class="pchip closed">closed</span>' in html
        assert cell_classes(html, "₹988.25") == {"n"}
        assert cell_classes(html, "−₹12.05") == {"n", "neg"}
        assert ">STOP<" in html
        assert "10:05 → 11:30" in html
        net_cell = re.findall(r"<td[^>]*>(.*?)</td>", html)[7]
        assert net_cell == "–"

    @pytest.mark.parametrize(
        ("net", "shown", "colour"),
        [("9.70", "₹9.70", {"pos"}), ("-0.30", "−₹0.30", {"neg"}), ("0.00", "₹0.00", set())],
    )
    def test_net_is_coloured_with_the_existing_pos_neg_classes(self, net, shown, colour):
        html = js("paperRow(P)", closed(net=net, gross="1.00"))
        assert cell_classes(html, shown) == {"n", *colour}

    @pytest.mark.parametrize(("grade", "colour"), [("live", "pos"), ("delayed", "warn")])
    def test_the_grade_says_live_or_delayed(self, grade, colour):
        html = js("paperRow(P)", trade(grade=grade))
        assert cell_classes(html, grade) == {colour}

    def test_an_index_trade_spells_the_side_and_lot(self):
        html = js(
            "paperRow(P)",
            trade(kind="INDEX", side="BUY_CE", instrument="BANKNIFTY 45000 CE 2026-11-24", qty=30),
        )
        assert ">BUY CE<" in html and "qty 30" in html
        assert "<b>BANKNIFTY 45000 CE 2026-11-24</b>" in html

    def test_a_reason_is_spelled_with_spaces(self):
        html = js("paperRow(P)", trade(status="NO_FILL", close_reason="GAP_THROUGH_STOP"))
        assert ">GAP THROUGH STOP<" in html

    def test_a_trade_from_an_earlier_day_shows_its_date(self):
        html = js("paperRow(P)", trade(created_at="2026-10-28T14:55:00+05:30"))
        assert "2026-10-28 14:55" in html

    @pytest.mark.parametrize(
        "field",
        [
            "instrument",
            "side",
            "strategy",
            "timeframe",
            "grade",
            "status",
            "close_reason",
            "entry",
            "exit",
            "gross",
            "net",
            "created_at",
            "closed_at",
            "qty",
        ],
    )
    def test_every_field_is_escaped(self, field):
        hostile = '<img src=x onerror="alert(1)">'
        html = js("paperRow(P)", closed(**{field: hostile}))
        assert "<img" not in html
        assert 'onerror="' not in html


class TestShown:
    def test_open_trades_first_then_those_closed_today(self):
        trades = [
            closed(alert_id="c-today"),
            trade(alert_id="open-old", created_at="2026-10-27T10:00:00+05:30"),
            closed(alert_id="c-yesterday", closed_at="2026-10-28T11:30:00+05:30"),
            trade(alert_id="nofill-today", status="NO_FILL", closed_at=f"{TODAY}T10:20:00+05:30"),
            trade(alert_id="pending", status="PENDING_FILL", entry=None),
            closed(alert_id="c-no-time", closed_at=None),
        ]
        shown = js(f"paperShown(P, {json.dumps(TODAY)}).map(t => t.alert_id)", trades)
        assert shown == ["open-old", "pending", "c-today", "nofill-today"]

    @pytest.mark.parametrize("odd", [None, {}, "x"])
    def test_anything_but_a_list_shows_nothing(self, odd):
        assert js(f"paperShown(P, {json.dumps(TODAY)})", odd) == []


class TestPanel:
    @pytest.mark.parametrize(
        "payload",
        [
            {"enabled": False, "status": {}, "trades": [], "totals_today": None},
            {"enabled": False, "status": {}, "trades": [trade()], "totals_today": None},
            None,
        ],
        ids=["disabled", "disabled-with-trades", "no-payload"],
    )
    def test_paper_off(self, payload):
        html = js("paperPanel(P)", payload)
        assert "Paper trading is off" in html and "<table" not in html

    def test_no_trades_yet(self):
        payload = {"enabled": True, "status": {}, "trades": [], "totals_today": {}}
        html = js("paperPanel(P)", payload)
        assert "No paper trades yet" in html and "<table" not in html

    def test_nothing_open_and_nothing_closed_today(self):
        old = closed(closed_at="2026-10-28T11:30:00+05:30")
        html = js("paperPanel(P)", {"enabled": True, "trades": [old]})
        assert "none closed today" in html and "No paper trades yet" not in html

    def test_a_table_of_the_shown_trades(self):
        payload = {"enabled": True, "trades": [closed(alert_id="c"), trade(alert_id="o")]}
        html = js("paperPanel(P)", payload)
        assert html.startswith('<table class="paper">')
        assert html.count("<tr>") == 3  # the header and two trades
        assert html.index("pchip open") < html.index("pchip closed")

    def test_today_is_the_ist_date_not_the_utc_one(self):
        # 00:30 IST on the 29th is still the 28th in UTC: a trade closed at 00:10 IST counts.
        payload = {"enabled": True, "trades": [closed(closed_at=f"{TODAY}T00:10:00+05:30")]}
        html = js("paperPanel(P)", payload, now="2026-10-28T19:00:00Z")
        assert "pchip closed" in html


class TestMeta:
    def test_the_day_line_with_a_net_not_yet_computed(self):
        payload = {
            "enabled": True,
            "status": {"opened": 5, "filled": 3, "no_fill": 1, "closed": 1, "hook_errors": 0},
            "totals_today": {
                "closed": 2,
                "wins": 1,
                "losses": 1,
                "gross_rupees": "-2.35",
                "net_rupees": None,
            },
        }
        text = js("paperMeta(P)", payload)
        assert text.startswith("today: 2 closed · 1 won · 1 lost · gross −₹2.35 · net –")
        assert "5 opened · 3 filled · 1 no fill" in text and "error" not in text

    def test_errors_are_counted_book_and_desk_together(self):
        payload = {
            "enabled": True,
            "status": {"hook_errors": 2, "desk_hook_errors": 1},
            "totals_today": {"net_rupees": "1.00"},
        }
        assert "3 errors (see the log)" in js("paperMeta(P)", payload)

    @pytest.mark.parametrize("payload", [None, {"enabled": False}])
    def test_nothing_when_paper_is_off(self, payload):
        assert js("paperMeta(P)", payload) == ""
