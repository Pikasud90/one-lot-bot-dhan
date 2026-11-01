"""US-032 UI: the Paper panel's three views, Today | Last session | All (developer tests).

    paperPanel(payload, view = "today")  the table (or message) for one view
    paperMeta(payload, view = "today")   the plain-text day line for that view
    paperDay("2026-10-30")               "Fri 30 Oct": the session heading's date
    paperSession(payload)                payload.last_session when it is usable, else null
    paperFallback(payload)               Today shows the last session: nothing closed today

Today: the open trades and today's closes, exactly as in US-030, unless nothing CLOSED today
and the API names a last session; then the open trades and today's no-fills come first and
the last session's closed / no-fill trades follow under 'Last session · Fri 30 Oct', and the
day line gives that session's totals (summed on the server from paise). Last session: that
session's trades only. All: every trade the API returned, in its order (newest first).

Found at the owner's first launch, a Sunday: 67 Friday/Thursday closes were hidden behind
"today: 0 closed". The helpers are lifted out of app.js and run in node with `Date` frozen at
Sunday 1 Nov 2026, 09:05 IST. Skipped without node.
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
    "paperList",
    "paperOpen",
    "paperClosedOn",
    "paperShown",
    "paperDay",
    "paperSession",
    "paperFallback",
    "paperEmpty",
    "paperGroup",
    "paperHead",
    "paperTable",
    "paperCut",
    "paperPanel",
    "paperTotals",
    "paperSince",
    "paperMeta",
]
SUNDAY = "2026-11-01T09:05:00+05:30"
FREEZE = (
    "const __D = Date, __T = new __D(process.argv[2]).getTime();"
    "globalThis.Date = class extends __D { constructor(...a) { if (a.length) super(...a);"
    " else super(__T); } static now() { return __T; } };"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def js(expression: str, payload: Any = None, *, now: str = SUNDAY) -> Any:
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
        ["node", "-e", "\n".join(lines), "--", json.dumps(payload), now],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def trade(alert_id: str, instrument: str, **kw: Any) -> dict[str, Any]:
    base = {
        "alert_id": alert_id,
        "instrument": instrument,
        "side": "BUY",
        "direction": "long",
        "kind": "STOCK",
        "strategy": "ema_cross",
        "timeframe": "5m",
        "grade": "delayed",
        "status": "CLOSED",
        "close_reason": "STOP",
        "qty": 1,
        "entry_ts": "2026-10-30T10:06:00+05:30",
        "entry": "1000.30",
        "exit": "995.30",
        "gross": "-5.00",
        "net": None,
        "created_at": "2026-10-30T10:05:00+05:30",
        "closed_at": "2026-10-30T11:00:00+05:30",
    }
    return {**base, **kw}


FRI_TOTALS = {"closed": 2, "wins": 1, "losses": 1, "gross_rupees": "7.00", "net_rupees": None}
TODAY_ZERO = {"closed": 0, "wins": 0, "losses": 0, "gross_rupees": "0.00", "net_rupees": None}
STATUS = {"opened": 68, "filled": 67, "no_fill": 1, "closed": 67, "hook_errors": 0}


def sunday() -> dict[str, Any]:
    """The owner's Sunday: one trade still open, one NO_FILL written this morning for a Friday
    alert, two Friday closes, one Thursday close; nothing closed today."""
    return {
        "enabled": True,
        "status": STATUS,
        "trades": [
            trade("o", "OPENTRADE", status="OPEN", close_reason=None, exit=None, gross=None)
            | {"closed_at": None, "created_at": "2026-10-30T15:00:00+05:30"},
            trade("nf", "NOFILLTODAY", status="NO_FILL", close_reason="NO_FILL", entry=None)
            | {"exit": None, "gross": "0.00", "net": "0.00", "entry_ts": None}
            | {"closed_at": "2026-11-01T09:01:00+05:30"},
            trade("f2", "FRIWIN", close_reason="TARGET", exit="1012.30", gross="12.00")
            | {"closed_at": "2026-10-30T15:20:00+05:30"},
            trade("f1", "FRILOSS", closed_at="2026-10-30T13:05:00+05:30"),
            trade("t1", "THURSDAY", closed_at="2026-10-29T14:00:00+05:30"),
        ],
        "totals_today": TODAY_ZERO,
        "last_session": {"date": "2026-10-30", "totals": FRI_TOTALS},
    }


def order(html: str, *names: str) -> list[int]:
    low = text(html).lower()
    return [low.index(n.lower()) for n in names]


class TestPaperDay:
    @pytest.mark.parametrize(
        ("date", "shown"),
        [
            ("2026-10-30", "Fri 30 Oct"),
            ("2026-11-01", "Sun 1 Nov"),
            ("2024-02-29", "Thu 29 Feb"),
            ("2026-12-31", "Thu 31 Dec"),
            ("2026-02-30", "2026-02-30"),  # not a date: shown as it came, never rolled over
            ("garbage", "garbage"),
            (None, "–"),
        ],
    )
    def test_a_session_date_reads_as_weekday_day_month(self, date, shown):
        assert js("paperDay(P)", date) == shown

    @pytest.mark.parametrize("now", ["2026-10-30T23:59:00-10:00", "2026-10-30T00:01:00+14:00"])
    def test_the_heading_date_never_depends_on_the_browser_zone(self, now):
        assert js("paperDay(P)", "2026-10-30", now=now) == "Fri 30 Oct"


class TestSessionAndFallback:
    @pytest.mark.parametrize(
        "last_session",
        [None, {}, {"date": "30 Oct"}, {"date": None}, "2026-10-30", {"totals": FRI_TOTALS}],
    )
    def test_an_unusable_last_session_is_none(self, last_session):
        payload = {**sunday(), "last_session": last_session}
        assert js("paperSession(P)", payload) is None
        assert js("paperFallback(P)", payload) is False

    def test_today_falls_back_only_when_nothing_closed_today(self):
        assert js("paperFallback(P)", sunday()) is True
        closed_today = {**sunday(), "totals_today": {**TODAY_ZERO, "closed": 1}}
        assert js("paperFallback(P)", closed_today) is False

    @pytest.mark.parametrize("totals_today", [None, {}, {"closed": "0"}])
    def test_without_todays_count_there_is_no_fallback(self, totals_today):
        assert js("paperFallback(P)", {**sunday(), "totals_today": totals_today}) is False


class TestTodayView:
    def test_the_owners_sunday_shows_fridays_session_under_its_heading(self):
        html = js("paperPanel(P)", sunday())
        assert html.startswith('<div class="paper-head"><b>Last session · Fri 30 Oct</b>')
        shown = text(html)
        for name in ("OPENTRADE", "NOFILLTODAY", "FRIWIN", "FRILOSS"):
            assert name in shown, name
        assert "THURSDAY" not in shown  # an older session stays in All
        # open and today's rows first, then the session under its own group row
        assert re.search(r'<tr class="pgroup"><td colspan="10">Last session · Fri 30 Oct', html)
        positions = order(html, "OPENTRADE", "NOFILLTODAY", "FRIWIN", "FRILOSS")
        assert positions == sorted(positions)
        low = text(html).lower()
        assert (
            low.index("nofilltoday") < low.rindex("last session · fri 30 oct") < low.index("friwin")
        )

    def test_with_nothing_else_to_show_the_heading_appears_once(self):
        payload = {**sunday(), "trades": [t for t in sunday()["trades"] if t["alert_id"][0] == "f"]}
        html = js("paperPanel(P)", payload)
        assert "pgroup" not in html and text(html).count("Last session · Fri 30 Oct") == 1

    def test_a_close_today_keeps_the_us030_view(self):
        payload = sunday()
        payload["trades"].append(trade("tt", "TODAYCLOSE", closed_at="2026-11-01T09:03:00+05:30"))
        payload["totals_today"] = {**TODAY_ZERO, "closed": 1}
        html = js("paperPanel(P)", payload)
        assert "Last session" not in html and "pgroup" not in html
        assert "TODAYCLOSE" in text(html) and "FRIWIN" not in text(html)
        assert html == js("paperPanel({...P, last_session: null})", payload)

    def test_without_a_last_session_the_panel_is_exactly_the_us030_one(self):
        payload = {**sunday(), "last_session": None}
        assert js("paperPanel(P)", payload) == js('paperPanel(P, "today")', payload)
        assert "Last session" not in js("paperPanel(P)", payload)
        assert "FRIWIN" not in text(js("paperPanel(P)", payload))

    def test_a_session_whose_trades_are_not_in_the_list_says_so(self):
        payload = {**sunday(), "trades": sunday()["trades"][:2]}
        html = js("paperPanel(P)", payload)
        assert "none of its trades is in the list" in text(html)

    def test_the_fallback_says_how_many_closes_are_listed(self):
        # review round 1 NIT: the same note as the Last session view
        payload = sunday()
        payload["last_session"]["totals"] = {**FRI_TOTALS, "closed": 230}
        assert "2 of 230 closes listed" in text(js("paperPanel(P)", payload))
        assert "closes listed" not in text(js("paperPanel(P)", sunday()))

    def test_a_fallback_with_nothing_to_list_is_an_empty_message_not_an_empty_table(self):
        payload = {**sunday(), "trades": [t for t in sunday()["trades"] if t["alert_id"] == "t1"]}
        html = js("paperPanel(P)", payload)
        assert "<table" not in html and 'class="empty-inline"' in html
        assert html.startswith('<div class="paper-head"><b>Last session · Fri 30 Oct</b>')
        assert "None of its trades is in the list" in text(html)
        assert "0 of 2 closes listed" in text(html)


class TestLastSessionView:
    def test_only_that_sessions_trades_under_its_heading(self):
        html = js('paperPanel(P, "last")', sunday())
        shown = text(html)
        assert html.startswith('<div class="paper-head"><b>Last session · Fri 30 Oct</b>')
        assert "FRIWIN" in shown and "FRILOSS" in shown
        for hidden in ("OPENTRADE", "NOFILLTODAY", "THURSDAY"):
            assert hidden not in shown, hidden

    def test_shown_even_when_today_has_closes(self):
        payload = {**sunday(), "totals_today": {**TODAY_ZERO, "closed": 3}}
        assert "FRIWIN" in text(js('paperPanel(P, "last")', payload))

    def test_no_earlier_session_says_so(self):
        html = js('paperPanel(P, "last")', {**sunday(), "last_session": None})
        assert "No earlier session" in text(html) and "<table" not in html

    def test_more_closes_than_listed_says_how_many_are_listed(self):
        payload = sunday()
        payload["last_session"]["totals"] = {**FRI_TOTALS, "closed": 230}
        assert "2 of 230 closes listed" in text(js('paperPanel(P, "last")', payload))


class TestAllView:
    def test_every_returned_trade_in_the_apis_order(self):
        html = js('paperPanel(P, "all")', sunday())
        assert html.startswith('<table class="paper">')
        assert html.count("<tr>") == 1 + 5
        positions = order(html, "OPENTRADE", "NOFILLTODAY", "FRIWIN", "FRILOSS", "THURSDAY")
        assert positions == sorted(positions)

    def test_an_empty_book_is_still_no_paper_trades_yet(self):
        payload = {**sunday(), "trades": [], "last_session": None}
        for view in ("today", "last", "all"):
            assert "No paper trades yet" in text(js(f"paperPanel(P, {json.dumps(view)})", payload))

    @pytest.mark.parametrize("view", ["today", "last", "all"])
    def test_paper_off_wins_in_every_view(self, view):
        payload = {"enabled": False, "status": {}, "trades": [], "totals_today": None}
        assert "Paper trading is off" in text(js(f"paperPanel(P, {json.dumps(view)})", payload))

    @pytest.mark.parametrize("view", ["ALL", "", "constructor", None])
    def test_an_unknown_view_is_today(self, view):
        assert js("paperPanel(P, P2)".replace("P2", json.dumps(view)), sunday()) == js(
            "paperPanel(P)", sunday()
        )


class TestEveryFieldStillEscaped:
    @pytest.mark.parametrize("view", ["today", "last", "all"])
    def test_a_hostile_session_date_and_instrument_never_become_markup(self, view):
        payload = sunday()
        payload["trades"][2]["instrument"] = '<img src=x onerror="alert(1)">'
        payload["last_session"]["date"] = '<img src=x onerror="alert(1)">'
        html = js(f"paperPanel(P, {json.dumps(view)})", payload)
        assert "<img" not in html


class TestDayLine:
    def test_today_is_unchanged_when_something_closed_today(self):
        payload = {**sunday(), "totals_today": {**FRI_TOTALS, "closed": 2}}
        assert js("paperMeta(P)", payload) == (
            "today: 2 closed · 1 won · 1 lost · gross ₹7.00 · net – (no costs yet) · "
            "since restart: 68 opened · 67 filled · 1 no fill"
        )

    def test_the_fallback_gives_the_sessions_totals(self):
        assert js("paperMeta(P)", sunday()) == (
            "last session Fri 30 Oct: 2 closed · 1 won · 1 lost · gross ₹7.00 · net – "
            "(no costs yet) · today: nothing closed yet · since restart: 68 opened · "
            "67 filled · 1 no fill"
        )

    def test_the_last_session_view_gives_its_totals(self):
        payload = {**sunday(), "totals_today": {**TODAY_ZERO, "closed": 4}}
        assert js('paperMeta(P, "last")', payload).startswith(
            "last session Fri 30 Oct: 2 closed · 1 won · 1 lost · gross ₹7.00"
        )
        none = js('paperMeta(P, "last")', {**sunday(), "last_session": None})
        assert none.startswith("last session: none before today · since restart")

    def test_all_counts_what_is_listed_and_sums_no_money(self):
        line = js('paperMeta(P, "all")', sunday())
        assert line.startswith("all: 5 trades listed, newest first (at most 200) · since")
        assert "₹" not in line  # the client never adds rupees up

    def test_paper_off_has_no_day_line(self):
        assert js('paperMeta(P, "last")', {"enabled": False}) == ""
