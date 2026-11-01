"""US-003 AC1, AC2, AC6 (formatters): the compact Telegram phone message.

Golden texts pin the exact template; the property tests pin the budget (<= 12 lines,
<= 600 chars), escaping and the b/i/code-only tag set for any text in any field.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.clock import IST
from onelot.notify.telegram import format_alert, format_exit, format_missed

from .payloads import assert_telegram_html, exit_event, index_alert, stock_alert

NOW = dt.datetime(2026, 11, 2, 10, 16, 30, tzinfo=IST)  # 3.5 min before the 10:20 deadline
SENTINEL = "SENTINEL-7f3a9c"


class TestGoldenAlerts:
    def test_stock_buy(self):
        assert format_alert(stock_alert(), NOW) == (
            "<b>BUY RELIANCE · 1 share</b>\n"
            "<i>Reliance Industries</i>\n"
            "Entry ₹2,950.50 mkt\n"
            "Stop ₹2,921.00 · Target ₹3,009.50\n"
            "orb 5m · conf 72 high\n"
            "Valid till 10:20:00 IST · 3 min left\n"
            "Data live · alert 10:15:00 IST · #a1b2c3"
        )

    def test_stock_sell_on_a_delayed_feed(self):
        alert = stock_alert(
            id="9f8e7d6c5b4a",
            series_key="TCS",
            label="Tata Consultancy",
            side="SELL",
            direction="short",
            strategy="vwap_revert",
            timeframe="15m",
            confidence={"score": 41.2, "label": "low"},
            data={"source": "yahoo", "delayed": True},
        )
        alert["economics"].update(
            side="SELL", entry_price=4100.0, stop_price=4141.0, target_price=4018.0
        )
        assert format_alert(alert, NOW) == (
            "<b>SELL TCS · 1 share</b>\n"
            "<i>Tata Consultancy</i>\n"
            "Entry ₹4,100.00 mkt\n"
            "Stop ₹4,141.00 · Target ₹4,018.00\n"
            "vwap_revert 15m · conf 41 low\n"
            "Valid till 10:20:00 IST · 3 min left\n"
            "Data <b>delayed ~15m</b> · alert 10:15:00 IST · #9f8e7d"
        )

    def test_index_ce_with_a_live_chain_premium(self):
        assert format_alert(index_alert("CE"), NOW) == (
            "<b>BUY BANKNIFTY 45000 CE · 1 lot (30)</b>\n"
            "<i>NIFTY BANK</i>\n"
            "Entry ₹312.40 mkt (ask)\n"
            "Stop ₹250.10 · Target ₹380.00\n"
            "Underlying stop 44,980.00 · target 45,400.00\n"
            "orb 5m · conf 66 high\n"
            "Valid till 10:20:00 IST · 3 min left\n"
            "Data live · alert 10:15:00 IST · #c3d4e5"
        )

    def test_index_pe_with_a_synthetic_premium_after_expiry(self):
        alert = index_alert(
            "PE",
            synthetic=True,
            id="d4e5f6a7b8c9",
            confidence={"score": 55.0, "label": "medium"},
            data={"source": "yahoo", "delayed": True},
        )
        alert["economics"].update(
            entry_price=298.75,
            stop_price=240.0,
            target_price=390.25,
            underlying_stop=45260.0,
            underlying_target=44780.0,
        )
        alert["economics"]["option"]["strike"] = 45500.0
        later = dt.datetime(2026, 11, 2, 10, 21, 0, tzinfo=IST)
        assert format_alert(alert, later) == (
            "<b>BUY BANKNIFTY 45500 PE · 1 lot (30)</b>\n"
            "<i>NIFTY BANK</i>\n"
            "Entry ₹298.75 est.\n"
            "Stop ₹240.00 · Target ₹390.25\n"
            "Underlying stop 45,260.00 · target 44,780.00\n"
            "orb 5m · conf 55 medium\n"
            "Valid till 10:20:00 IST · expired\n"
            "Data <b>delayed ~15m</b> · alert 10:15:00 IST · #d4e5f6"
        )

    def test_no_target_says_trail(self):
        alert = stock_alert()
        alert["economics"].update(target_price=None, underlying_target=None)
        lines = format_alert(alert, NOW).split("\n")
        assert lines[3] == "Stop ₹2,921.00 · trail (no target)"

    def test_index_without_a_target_trails_both_levels(self):
        alert = index_alert("CE")
        alert["economics"].update(target_price=None, underlying_target=None)
        lines = format_alert(alert, NOW).split("\n")
        assert lines[3] == "Stop ₹250.10 · trail (no target)"
        assert lines[4] == "Underlying stop 44,980.00 · trail"

    @pytest.mark.parametrize(
        "alert",
        [stock_alert(), index_alert("CE"), index_alert("PE", synthetic=True)],
        ids=["stock", "index-live", "index-synthetic"],
    )
    def test_goldens_are_valid_telegram_html(self, alert):
        assert_telegram_html(format_alert(alert, NOW))


class TestFieldRules:
    @pytest.mark.parametrize(
        ("seconds_left", "expected"),
        [
            (300, "5 min left"),
            (299, "4 min left"),
            (60, "1 min left"),
            (59, "expired"),
            (0, "expired"),
            (-1, "expired"),
            (-3600, "expired"),
        ],
    )
    def test_ttl_floors_minutes_and_says_expired_at_zero(self, seconds_left, expected):
        deadline = dt.datetime(2026, 11, 2, 10, 20, 0, tzinfo=IST)
        now = deadline - dt.timedelta(seconds=seconds_left)
        ttl_line = format_alert(stock_alert(), now).split("\n")[5]
        assert ttl_line == f"Valid till 10:20:00 IST · {expected}"

    def test_times_are_rendered_in_ist_whatever_the_offset(self):
        alert = stock_alert(
            created_at="2026-11-02T04:45:07+00:00", expires_at="2026-11-02T04:50:07+00:00"
        )
        now_utc = dt.datetime(2026, 11, 2, 4, 46, 7, tzinfo=dt.timezone.utc)
        lines = format_alert(alert, now_utc).split("\n")
        assert lines[5] == "Valid till 10:20:07 IST · 4 min left"
        assert lines[6] == "Data live · alert 10:15:07 IST · #a1b2c3"

    def test_datetime_objects_and_unparseable_times(self):
        alert = stock_alert(
            created_at=dt.datetime(2026, 11, 2, 10, 15, 0, tzinfo=IST), expires_at="not a time"
        )
        lines = format_alert(alert, NOW).split("\n")
        assert lines[5] == "Valid till n/a"
        assert lines[6] == "Data live · alert 10:15:00 IST · #a1b2c3"
        alert = stock_alert(created_at=None, expires_at=12345)
        lines = format_alert(alert, NOW).split("\n")
        assert lines[5] == "Valid till n/a"
        assert lines[6] == "Data live · alert n/a · #a1b2c3"

    @pytest.mark.parametrize(
        ("source", "delayed", "tag"),
        [
            ("yahoo", False, "Data live"),
            ("dhan-ws", True, "Data <b>delayed ~15m</b>"),
            ("yahoo", None, "Data live"),
        ],
    )
    def test_delayed_tag_comes_from_data_delayed_only(self, source, delayed, tag):
        """US-028 single rule: data.delayed decides; data.source is never re-read."""
        alert = stock_alert(data={"source": source, "delayed": delayed})
        assert format_alert(alert, NOW).split("\n")[-1].startswith(tag + " · ")

    def test_label_line_is_omitted_when_it_repeats_the_symbol(self):
        msg = format_alert(stock_alert(label="RELIANCE"), NOW)
        assert "<i>" not in msg
        assert msg.split("\n")[1] == "Entry ₹2,950.50 mkt"

    def test_money_rounds_half_up_not_binary_float(self):
        alert = stock_alert()
        alert["economics"].update(entry_price=2.675, stop_price=1234567.005)
        lines = format_alert(alert, NOW).split("\n")
        assert lines[2] == "Entry ₹2.68 mkt"
        assert lines[3].startswith("Stop ₹1,234,567.01 · ")

    def test_missing_or_garbage_numbers_render_as_na(self):
        alert = stock_alert()
        alert["economics"].update(entry_price=None, stop_price=float("nan"), target_price="x")
        lines = format_alert(alert, NOW).split("\n")
        assert lines[2] == "Entry n/a mkt"
        assert lines[3] == "Stop n/a · Target n/a"

    def test_confidence_is_a_whole_number_rounded_half_up(self):
        msg = format_alert(stock_alert(confidence={"score": 64.5, "label": "medium"}), NOW)
        assert "orb 5m · conf 65 medium" in msg.split("\n")


class TestEscapingAndWhitelist:
    def test_dynamic_text_is_html_escaped(self):
        alert = stock_alert(
            series_key="M&M",
            label="<b>Mahindra</b> & Mahindra 🚜",
            strategy="a<b>c",
            confidence={"score": 70, "label": "hi<i>gh"},
        )
        msg = format_alert(alert, NOW)
        lines = msg.split("\n")
        assert lines[0] == "<b>BUY M&amp;M · 1 share</b>"
        assert lines[1] == "<i>&lt;b&gt;Mahindra&lt;/b&gt; &amp; Mahindra 🚜</i>"
        assert lines[4] == "a&lt;b&gt;c 5m · conf 70 hi&lt;i&gt;gh"
        assert_telegram_html(msg)

    def test_newlines_inside_a_field_are_collapsed(self):
        alert = stock_alert(
            series_key="M&M\nEVIL",
            label="Mahindra\r\n\r\nand Mahindra\tLtd",
            strategy="orb\nfast",
        )
        msg = format_alert(alert, NOW)
        lines = msg.split("\n")
        assert len(lines) == 7
        assert lines[0] == "<b>BUY M&amp;M EVIL · 1 share</b>"
        assert lines[1] == "<i>Mahindra and Mahindra Ltd</i>"
        assert lines[4].startswith("orb fast 5m · ")
        assert " " not in msg and "\r" not in msg

    def test_controls_and_bidi_overrides_are_dropped(self):
        """A right-to-left override in a field must not re-order what the phone shows."""
        alert = stock_alert(series_key="REL\u202eECNAI\x00", label="Reliance\u2066 Ind\x07")
        lines = format_alert(alert, NOW).split("\n")
        assert lines[0] == "<b>BUY RELECNAI · 1 share</b>"
        assert lines[1] == "<i>Reliance Ind</i>"

    def test_account_fields_never_reach_the_message(self):
        alert = stock_alert(
            balance=SENTINEL,
            holdings=[SENTINEL],
            ledger={"cash": SENTINEL},
            token=SENTINEL,
            text=SENTINEL,
            reasons=[SENTINEL],
            risk_reason=SENTINEL,
            strategy_label=SENTINEL,
            instrument_key=SENTINEL,
        )
        alert["data"].update(source=SENTINEL, delay_note=SENTINEL, token=SENTINEL)
        alert["economics"].update(notes=[SENTINEL], ledger=SENTINEL, balance=SENTINEL)
        alert["economics"]["option"] = {"security_id": SENTINEL, "token": SENTINEL}
        alert["confidence"]["parts"] = {"backtest_note": SENTINEL}
        alert["manage"] = {"token": SENTINEL}
        msg = format_alert(alert, NOW)
        assert SENTINEL not in msg
        assert "7f3a9c" not in msg

    def test_containers_in_text_fields_are_never_stringified(self):
        alert = stock_alert(label={"token": SENTINEL}, strategy=[SENTINEL], timeframe=(SENTINEL,))
        msg = format_alert(alert, NOW)
        assert SENTINEL not in msg
        assert_telegram_html(msg)


class TestBudget:
    def test_an_over_budget_label_is_cut_with_an_ellipsis(self):
        msg = format_alert(stock_alert(label="Reliance Industries " * 60), NOW)
        assert len(msg) <= 600
        label_line = msg.split("\n")[1]
        assert label_line.startswith("<i>Reliance Industries Reliance")
        assert label_line.endswith("…</i>")
        assert msg.split("\n")[0] == "<b>BUY RELIANCE · 1 share</b>"
        assert msg.endswith("#a1b2c3")
        assert_telegram_html(msg)

    @pytest.mark.parametrize("char", ["&", "<", ">", "é", "🚀"])
    def test_truncation_never_cuts_inside_an_entity(self, char):
        msg = format_alert(stock_alert(label=char * 700), NOW)
        assert_telegram_html(msg)
        assert msg.split("\n")[1].endswith("…</i>")

    def test_pathological_fields_everywhere_stay_in_budget(self):
        huge = "&<>" * 400
        alert = index_alert(
            "CE",
            id=huge,
            series_key=huge,
            label=huge,
            strategy=huge,
            timeframe=huge,
            confidence={"score": 1e300, "label": huge},
        )
        alert["economics"].update(unit_label=huge, entry_price=9.99e11, stop_price=-9.99e11)
        alert["economics"]["option"]["strike"] = huge
        msg = format_alert(alert, NOW)
        assert_telegram_html(msg)
        assert msg.startswith("<b>BUY ")

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        label=st.text(max_size=900),
        symbol=st.text(max_size=200),
        strategy=st.text(max_size=200),
        timeframe=st.text(max_size=50),
        conf_label=st.text(max_size=50),
        unit=st.text(max_size=50),
        alert_id=st.text(max_size=40),
        index=st.booleans(),
    )
    def test_any_text_in_any_field_is_safe_and_in_budget(
        self, label, symbol, strategy, timeframe, conf_label, unit, alert_id, index
    ):
        alert: dict[str, Any] = index_alert("PE") if index else stock_alert()
        alert.update(
            label=label,
            series_key=symbol,
            strategy=strategy,
            timeframe=timeframe,
            id=alert_id,
            confidence={"score": 50, "label": conf_label},
        )
        alert["economics"]["unit_label"] = unit
        msg = format_alert(alert, NOW)
        assert_telegram_html(msg)
        assert re.match(r"<b>BUY ", msg)


class TestExitAndMissed:
    def test_exit_golden_index_stop(self):
        later = dt.datetime(2026, 11, 2, 11, 42, 10, tzinfo=IST)
        assert format_exit(exit_event("STOP"), later) == (
            "<b>EXIT BANKNIFTY 45000 CE · 1 lot (30)</b>\n"
            "Reason: stop hit\n"
            "Est. P&amp;L -₹1,234.50 (-1.04R)\n"
            "11:42:10 IST · #f00dba"
        )

    def test_exit_golden_stock_short_at_target(self):
        event = exit_event(
            "TARGET",
            id="0badc0ffee00",
            series_key="TCS",
            label="Tata Consultancy",
            kind="STOCK",
            side="SELL",
            direction="short",
            qty=1,
            option={},
            unrealized=82.0,
            r_now=2.0,
        )
        later = dt.datetime(2026, 11, 2, 13, 5, 0, tzinfo=IST)
        assert format_exit(event, later) == (
            "<b>EXIT TCS · 1 share (short)</b>\n"
            "Reason: target hit\n"
            "Est. P&amp;L +₹82.00 (+2.00R)\n"
            "13:05:00 IST · #0badc0"
        )

    @pytest.mark.parametrize(
        ("advice", "reason"),
        [
            ("TRAIL_STOP", "trailing stop hit"),
            ("SIGNAL_EXIT", "strategy exit signal"),
            ("OPPOSITE_SIGNAL", "opposite signal"),
            ("SQUARE_OFF", "square-off time"),
            ("TIME_STOP", "time stop"),
            ("NEW_<RULE>", "new &lt;rule&gt;"),
        ],
    )
    def test_exit_reasons(self, advice, reason):
        msg = format_exit(exit_event(advice), NOW)
        assert msg.split("\n")[1] == f"Reason: {reason}"
        assert_telegram_html(msg)

    def test_exit_without_pnl_says_na_and_stays_four_lines(self):
        msg = format_exit(exit_event("STOP", unrealized=None, r_now=None), NOW)
        lines = msg.split("\n")
        assert lines[2] == "Est. P&amp;L n/a"
        assert len(lines) == 4

    def test_exit_reads_only_whitelisted_position_fields(self):
        event = exit_event(
            "STOP", hold_check=SENTINEL, notes=[SENTINEL], balance=SENTINEL, label=SENTINEL
        )
        event["token"] = SENTINEL
        event["position"]["manage"] = {"token": SENTINEL}
        assert SENTINEL not in format_exit(event, NOW)

    def test_exit_on_garbage_payload_does_not_raise(self):
        msg = format_exit({"advice": None, "position": "nope"}, NOW)
        assert msg.split("\n")[0].startswith("<b>EXIT")
        assert_telegram_html(msg)

    def test_missed_is_one_line_and_never_starts_with_the_action(self):
        later = dt.datetime(2026, 11, 2, 10, 45, 0, tzinfo=IST)
        assert format_missed(index_alert("CE"), later) == (
            "<b>Missed</b> · BUY BANKNIFTY 45000 CE · 1 lot (30) · expired 10:20:00 IST · #c3d4e5"
        )

    def test_missed_escapes_and_stays_in_budget(self):
        msg = format_missed(stock_alert(series_key="M&M\n" * 300, id="<x>"), NOW)
        assert "\n" not in msg
        assert_telegram_html(msg)
