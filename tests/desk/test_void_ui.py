"""US-105 UI (developer tests): the tag on a paper trade row and the left-out line.

    voidTag(reason)    "not counted: the contract had no open interest" (title: the reason)
    leftOutText(lo)    "Left out of the score: 3 trades on contracts that were not tradeable
                       (no open interest)"; empty when nothing was left out
    paperRow / paperTotals / paperCut run with these

The one-line helpers are lifted out of app.js and run in node, as test_paper_panel_ui.py does.
"""

from __future__ import annotations

import re
import shutil

import pytest
from test_paper_panel_ui import closed, js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

REASON = "contract not tradeable: no open interest when the alert was raised"
TAG = "not counted: the contract had no open interest"
NOTE = "contracts that were not tradeable (no open interest)"
TOTALS = {"closed": 2, "wins": 1, "losses": 1, "gross_rupees": "10.00", "net_rupees": "6.00"}


def text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


class TestTheTag:
    def test_a_void_trade_row_shows_the_tag_and_keeps_its_recorded_money(self):
        row = js("paperRow(P)", closed(void_reason=REASON, net="-90.27", gross="-88.00"))
        assert TAG in text(row) and REASON in row
        assert "−₹90.27" in text(row) and "−₹88.00" in text(row)

    def test_a_sound_row_is_exactly_what_it_was(self):
        sound = js("paperRow(P)", closed(net="-90.27"))
        assert TAG not in sound and "void-tag" not in sound
        assert sound == js("paperRow(P)", closed(net="-90.27", void_reason=None))

    def test_the_reason_is_escaped(self):
        row = js("paperRow(P)", closed(void_reason='<b>"x"</b>'))
        assert "<b>" not in row.replace("<b>RELIANCE</b>", "")


class TestTheLeftOutLine:
    @pytest.mark.parametrize(
        ("trades", "words"),
        [
            (3, f"Left out of the score: 3 trades on {NOTE}"),
            (1, f"Left out of the score: 1 trade on {NOTE}"),
        ],
    )
    def test_the_words(self, trades, words):
        assert js("leftOutText(P)", {"trades": trades, "net": "-1.00"}) == words

    @pytest.mark.parametrize("lo", [None, {}, {"trades": 0}, {"trades": "0"}, {"trades": None}])
    def test_nothing_left_out_says_nothing(self, lo):
        assert js("leftOutText(P)", lo) == ""

    def test_the_day_line_gains_the_sentence_only_when_something_was_left_out(self):
        plain = js('paperTotals("today", P)', TOTALS)
        assert plain.startswith("today: 2 closed · 1 won · 1 lost · gross ")
        withheld = js('paperTotals("today", P)', {**TOTALS, "left_out": {"trades": 3, "net": "-1"}})
        assert withheld == f"{plain} · Left out of the score: 3 trades on {NOTE}"
        assert js('paperTotals("today", P)', {**TOTALS, "left_out": {"trades": 0}}) == plain

    def test_the_meta_line_carries_it_for_today_and_for_the_last_session(self):
        left = {"trades": 2, "net": "-1.00", "reason": NOTE}
        p = {
            "enabled": True,
            "status": {},
            "trades": [],
            "totals_today": {**TOTALS, "left_out": left},
            "last_session": {"date": "2026-10-28", "totals": {**TOTALS, "left_out": left}},
        }
        assert "Left out of the score: 2 trades" in js('paperMeta(P, "today")', p)
        assert "Left out of the score: 2 trades" in js('paperMeta(P, "last")', p)
        quiet = {
            **p,
            "totals_today": TOTALS,
            "last_session": {**p["last_session"], "totals": TOTALS},
        }
        assert "Left out" not in js('paperMeta(P, "today")', quiet)
        assert "Left out" not in js('paperMeta(P, "last")', quiet)


class TestTheCutMessage:
    def test_void_trades_in_the_list_do_not_hide_a_real_cut(self):
        # 2 counted closes in the totals, 1 sound + 1 void listed: one counted close is cut
        past = [closed(alert_id="a"), closed(alert_id="b", void_reason=REASON)]
        s = {"date": "2026-10-28", "totals": {"closed": 2}}
        got = js("paperCut(P.s, P.past)", {"s": s, "past": past})
        assert got == "1 of 2 closes listed (the list holds the 200 newest trades)"

    def test_everything_listed_says_nothing(self):
        past = [closed(alert_id="a"), closed(alert_id="b", void_reason=REASON)]
        s = {"date": "2026-10-28", "totals": {"closed": 1}}
        assert js("paperCut(P.s, P.past)", {"s": s, "past": past}) == ""
