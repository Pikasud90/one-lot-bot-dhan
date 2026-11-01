"""US-003 AC1, AC2, AC6 (formatters): the compact Telegram phone message.

Golden texts pin the exact template; the property tests pin the budget (<= 12 lines,
<= 600 chars), escaping and the b/i/code-only tag set for any text in any field.
Invisible characters are built with chr() so this file holds none literally.
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
LINE_SEPARATOR = chr(0x2028)
RTL_OVERRIDE = chr(0x202E)
LTR_ISOLATE = chr(0x2066)


class TestGoldenAlerts:
    def test_stock_buy(self):
        assert format_alert(stock_alert(), NOW) == (
            "<b>BUY RELIANCE · 1 share</b>\n"
            "<i>Reliance Industries</i>\n"
            "Entry 2,950.50 mkt\n"
            "Stop 2,921.00 · Target 3,009.50\n"
            "Opening Range Breakout 5m\n"
            "Conf 72 high\n"
            "Valid till 10:20 IST (3m left)\n"
            "Data live\n"
            "Alert 10:15:00 IST · id <code>a1b2c3</code>"
        )

    def test_stock_sell_on_a_delayed_feed(self):
        alert = stock_alert(
            id="9f8e7d6c5b4a",
            series_key="TCS",
            label="Tata Consultancy",
            side="SELL",
            direction="short",
            strategy="vwap_revert",
            strategy_label="VWAP Reversion",
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
            "Entry 4,100.00 mkt\n"
            "Stop 4,141.00 · Target 4,018.00\n"
            "VWAP Reversion 15m\n"
            "Conf 41 low\n"
            "Valid till 10:20 IST (3m left)\n"
            "Data <b>delayed ~15m</b>\n"
            "Alert 10:15:00 IST · id <code>9f8e7d</code>"
        )

    def test_index_ce_with_a_live_chain_premium(self):
        assert format_alert(index_alert("CE"), NOW) == (
            "<b>BUY BANKNIFTY 45000 CE · 1 lot (30)</b>\n"
            "<i>NIFTY BANK</i>\n"
            "Entry 312.40 ask\n"
            "Underlying Stop 44,980.00 · Target 45,400.00\n"
            "Stop 250.10 · Target 380.00\n"
            "Opening Range Breakout 5m\n"
            "Conf 66 high\n"
            "Valid till 10:20 IST (3m left)\n"
            "Data live\n"
            "Alert 10:15:00 IST · id <code>c3d4e5</code>"
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
            "Entry 298.75 est.\n"
            "Underlying Stop 45,260.00 · Target 44,780.00\n"
            "Stop 240.00 · Target 390.25\n"
            "Opening Range Breakout 5m\n"
            "Conf 55 medium\n"
            "Valid till 10:20 IST (expired)\n"
            "Data <b>delayed ~15m</b>\n"
            "Alert 10:15:00 IST · id <code>d4e5f6</code>"
        )

    def test_no_target_says_trail(self):
        alert = stock_alert()
        alert["economics"].update(target_price=None, underlying_target=None)
        assert format_alert(alert, NOW).split("\n")[3] == "Stop 2,921.00 · Target trail"

    def test_index_without_a_target_trails_both_levels(self):
        alert = index_alert("CE")
        alert["economics"].update(target_price=None, underlying_target=None)
        lines = format_alert(alert, NOW).split("\n")
        assert lines[3] == "Underlying Stop 44,980.00 · Target trail"
        assert lines[4] == "Stop 250.10 · Target trail"

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
            (300, "5m left"),
            (299, "4m left"),
            (60, "1m left"),
            (59, "expired"),
            (0, "expired"),
            (-1, "expired"),
            (-3600, "expired"),
        ],
    )
    def test_ttl_floors_minutes_and_says_expired_at_zero(self, seconds_left, expected):
        deadline = dt.datetime(2026, 11, 2, 10, 20, 0, tzinfo=IST)
        now = deadline - dt.timedelta(seconds=seconds_left)
        assert (
            format_alert(stock_alert(), now).split("\n")[6] == f"Valid till 10:20 IST ({expected})"
        )

    def test_times_are_rendered_in_ist_whatever_the_offset(self):
        alert = stock_alert(
            created_at="2026-11-02T04:45:07+00:00", expires_at="2026-11-02T04:50:07+00:00"
        )
        now_utc = dt.datetime(2026, 11, 2, 4, 46, 7, tzinfo=dt.timezone.utc)
        lines = format_alert(alert, now_utc).split("\n")
        assert lines[6] == "Valid till 10:20 IST (4m left)"
        assert lines[8] == "Alert 10:15:07 IST · id <code>a1b2c3</code>"

    def test_datetime_objects_and_unparseable_times(self):
        alert = stock_alert(
            created_at=dt.datetime(2026, 11, 2, 10, 15, 0, tzinfo=IST), expires_at="not a time"
        )
        lines = format_alert(alert, NOW).split("\n")
        assert lines[6] == "Valid till n/a"
        assert lines[8] == "Alert 10:15:00 IST · id <code>a1b2c3</code>"
        lines = format_alert(stock_alert(created_at=None, expires_at=12345), NOW).split("\n")
        assert lines[6] == "Valid till n/a"
        assert lines[8] == "Alert n/a · id <code>a1b2c3</code>"

    @pytest.mark.parametrize(
        ("source", "delayed", "line"),
        [
            ("yahoo", False, "Data live"),
            ("dhan-ws", True, "Data <b>delayed ~15m</b>"),
            ("yahoo", None, "Data live"),
            ("yahoo", "true", "Data live"),
        ],
    )
    def test_delayed_tag_comes_from_data_delayed_only(self, source, delayed, line):
        """US-028 single rule: data.delayed (exactly True) decides; data.source is never read."""
        alert = stock_alert(data={"source": source, "delayed": delayed})
        assert format_alert(alert, NOW).split("\n")[7] == line

    def test_the_unit_comes_from_the_kind_and_lot_size_not_the_unit_label(self):
        stock, index = stock_alert(), index_alert("CE")
        stock["economics"]["unit_label"] = SENTINEL
        del index["economics"]["unit_label"]
        index["economics"]["qty"] = None  # falls back to option.lot
        assert format_alert(stock, NOW).split("\n")[0] == "<b>BUY RELIANCE · 1 share</b>"
        header = format_alert(index, NOW).split("\n")[0]
        assert header == "<b>BUY BANKNIFTY 45000 CE · 1 lot (30)</b>"

    def test_the_strategy_name_is_the_fallback_for_a_missing_label(self):
        alert = stock_alert()
        del alert["strategy_label"]
        assert format_alert(alert, NOW).split("\n")[4] == "orb 5m"

    def test_numeric_text_fields_are_rendered_as_numbers(self):
        lines = format_alert(stock_alert(series_key=500325, timeframe=5), NOW).split("\n")
        assert lines[0] == "<b>BUY 500325 · 1 share</b>"
        assert lines[4] == "Opening Range Breakout 5"

    def test_label_line_is_omitted_when_it_repeats_the_symbol(self):
        msg = format_alert(stock_alert(label="RELIANCE"), NOW)
        assert "<i>" not in msg
        assert msg.split("\n")[1] == "Entry 2,950.50 mkt"

    def test_money_rounds_half_up_not_binary_float(self):
        alert = stock_alert()
        alert["economics"].update(entry_price=2.675, stop_price=1234567.005)
        lines = format_alert(alert, NOW).split("\n")
        assert lines[2] == "Entry 2.68 mkt"
        assert lines[3].startswith("Stop 1,234,567.01 · ")

    def test_missing_or_garbage_numbers_render_as_na(self):
        alert = stock_alert()
        alert["economics"].update(entry_price=None, stop_price=float("nan"), target_price="x")
        lines = format_alert(alert, NOW).split("\n")
        assert lines[2] == "Entry n/a mkt"
        assert lines[3] == "Stop n/a · Target n/a"

    @pytest.mark.parametrize(("score", "shown"), [(64.5, "65"), (0.0, "0"), (100, "100")])
    def test_confidence_is_a_whole_number_rounded_half_up(self, score, shown):
        msg = format_alert(stock_alert(confidence={"score": score, "label": "medium"}), NOW)
        assert msg.split("\n")[5] == f"Conf {shown} medium"


class TestEscapingAndWhitelist:
    def test_dynamic_text_is_html_escaped(self):
        alert = stock_alert(
            series_key="M&M",
            label="<b>Mahindra</b> & Mahindra 🚜",
            strategy_label="a<b>c",
            timeframe="<5m>",
            confidence={"score": 70, "label": "hi<i>gh"},
        )
        msg = format_alert(alert, NOW)
        lines = msg.split("\n")
        assert lines[0] == "<b>BUY M&amp;M · 1 share</b>"
        assert lines[1] == "<i>&lt;b&gt;Mahindra&lt;/b&gt; &amp; Mahindra 🚜</i>"
        assert lines[4] == "a&lt;b&gt;c &lt;5m&gt;"
        assert lines[5] == "Conf 70 hi&lt;i&gt;gh"
        assert_telegram_html(msg)

    def test_newlines_inside_a_field_are_collapsed(self):
        alert = stock_alert(
            series_key="M&M\nEVIL",
            label=f"Mahindra\r\n\r\nand{LINE_SEPARATOR}Mahindra\tLtd",
            strategy_label="orb\nfast",
        )
        msg = format_alert(alert, NOW)
        lines = msg.split("\n")
        assert len(lines) == 9
        assert lines[0] == "<b>BUY M&amp;M EVIL · 1 share</b>"
        assert lines[1] == "<i>Mahindra and Mahindra Ltd</i>"
        assert lines[4] == "orb fast 5m"
        assert LINE_SEPARATOR not in msg and "\r" not in msg

    def test_controls_and_bidi_overrides_are_dropped(self):
        """A right-to-left override in a field must not re-order what the phone shows."""
        alert = stock_alert(
            series_key=f"REL{RTL_OVERRIDE}ECNAI\x00", label=f"Reliance{LTR_ISOLATE} Ind\x07"
        )
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
            strategy=SENTINEL,  # only a fallback for a missing strategy_label
            instrument_key=SENTINEL,
        )
        alert["data"].update(source=SENTINEL, delay_note=SENTINEL, token=SENTINEL)
        alert["economics"].update(
            notes=[SENTINEL], ledger=SENTINEL, balance=SENTINEL, unit_label=SENTINEL
        )
        alert["economics"]["option"] = {"security_id": SENTINEL, "token": SENTINEL}
        alert["confidence"]["parts"] = {"backtest_note": SENTINEL}
        alert["manage"] = {"token": SENTINEL}
        msg = format_alert(alert, NOW)
        assert SENTINEL not in msg
        assert "7f3a9c" not in msg

    def test_containers_in_text_fields_are_never_stringified(self):
        alert = stock_alert(
            label={"token": SENTINEL}, strategy_label=[SENTINEL], timeframe=(SENTINEL,)
        )
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
        assert msg.endswith("id <code>a1b2c3</code>")
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
            label="<&>" * 400,
            strategy_label=huge,
            timeframe=huge,
            confidence={"score": 1e300, "label": huge},
            expires_at="9999-12-31T23:59:59+05:30",
            data={"delayed": True},
        )
        alert["economics"].update(
            entry_price=-9.99e11,
            stop_price=-9.99e11,
            target_price=-9.99e11,
            underlying_stop=-9.99e11,
            underlying_target=-9.99e11,
            qty=10**11,
        )
        alert["economics"]["option"]["strike"] = huge
        msg = format_alert(alert, NOW)
        assert_telegram_html(msg)
        assert msg.startswith("<b>BUY ")
        assert "<i>" in msg  # the label still gets some room

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        label=st.text(max_size=900),
        symbol=st.text(max_size=200),
        strategy=st.text(max_size=200),
        timeframe=st.text(max_size=50),
        conf_label=st.text(max_size=50),
        qty=st.one_of(st.none(), st.integers(), st.floats(), st.text(max_size=20)),
        alert_id=st.text(max_size=40),
        index=st.booleans(),
    )
    def test_any_text_in_any_field_is_safe_and_in_budget(
        self, label, symbol, strategy, timeframe, conf_label, qty, alert_id, index
    ):
        alert: dict[str, Any] = index_alert("PE") if index else stock_alert()
        alert.update(
            label=label,
            series_key=symbol,
            strategy_label=strategy,
            timeframe=timeframe,
            id=alert_id,
            confidence={"score": 50, "label": conf_label},
        )
        alert["economics"]["qty"] = qty
        msg = format_alert(alert, NOW)
        assert_telegram_html(msg)
        assert re.match(r"<b>BUY ", msg)


class TestExitAndMissed:
    def test_exit_golden_index_stop(self):
        later = dt.datetime(2026, 11, 2, 11, 42, 10, tzinfo=IST)
        assert format_exit(exit_event("STOP"), later) == (
            "<b>EXIT BANKNIFTY 45000 CE · 1 lot (30)</b>\n"
            "Reason: STOP\n"
            "Est. P&amp;L -1,235\n"  # -1234.5 rounds half-up, away from zero
            "Time 11:42:10 IST"
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
            "<b>EXIT TCS · 1 share</b>\nReason: TARGET\nEst. P&amp;L +82\nTime 13:05:00 IST"
        )

    @pytest.mark.parametrize(
        ("advice", "reason"),
        [
            ("TRAIL_STOP", "TRAIL STOP"),
            ("SIGNAL_EXIT", "SIGNAL EXIT"),
            ("OPPOSITE_SIGNAL", "OPPOSITE SIGNAL"),
            ("SQUARE_OFF", "SQUARE OFF"),
            ("TIME_STOP", "TIME STOP"),
            ("NEW_<RULE>&", "NEW &lt;RULE&gt;&amp;"),
            ("", "n/a"),
        ],
    )
    def test_exit_reasons(self, advice, reason):
        msg = format_exit(exit_event(advice), NOW)
        assert msg.split("\n")[1] == f"Reason: {reason}"
        assert_telegram_html(msg)

    @pytest.mark.parametrize(("unrealized", "shown"), [(None, "n/a"), (0.4, "0"), (-0.4, "0")])
    def test_exit_pnl_edge_values(self, unrealized, shown):
        msg = format_exit(exit_event("STOP", unrealized=unrealized), NOW)
        lines = msg.split("\n")
        assert lines[2] == f"Est. P&amp;L {shown}"
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
        assert msg.split("\n")[0] == "<b>EXIT ?</b>"
        assert_telegram_html(msg)

    def test_missed_is_one_line_and_never_starts_with_the_action(self):
        later = dt.datetime(2026, 11, 2, 10, 45, 0, tzinfo=IST)
        assert format_missed(index_alert("CE"), later) == (
            "<b>Missed</b> · BUY BANKNIFTY 45000 CE · 1 lot (30) · expired 10:20 IST"
            " · id <code>c3d4e5</code>"
        )

    def test_missed_before_the_deadline_says_valid_till(self):
        msg = format_missed(stock_alert(), NOW)
        assert " · valid till 10:20 IST · " in msg

    def test_missed_escapes_and_stays_in_budget(self):
        msg = format_missed(stock_alert(series_key="M&M\n" * 300, id="<x>"), NOW)
        assert "\n" not in msg
        assert "id <code>&lt;x&gt;</code>" in msg
        assert_telegram_html(msg)
