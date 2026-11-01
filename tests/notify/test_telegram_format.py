"""US-003 AC1, AC2, AC6 (formatters): the compact Telegram phone message.

Golden texts pin the exact template; the property tests pin the budget (<= 12 lines,
<= 600 chars), escaping and the b/i/code-only tag set for any text in any field.
Invisible characters are built with chr() so this file holds none literally.
"""

from __future__ import annotations

import datetime as dt
import locale
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
LONE_SURROGATES = (chr(0xD800), chr(0xDBFF), chr(0xDC00), chr(0xDFFF))
ARABIC_LETTER_MARK = chr(0x061C)
BYTE_ORDER_MARK = chr(0xFEFF)


class TestGoldenAlerts:
    def test_stock_buy(self):
        assert format_alert(stock_alert(), NOW) == (
            "<b>BUY RELIANCE · 1 share · intraday</b>\n"
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
            "<b>SELL TCS · 1 share · intraday short</b>\n"
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
            "<b>BUY BANKNIFTY 25 NOV 45000 CE · 1 lot (30)</b>\n"
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
            "<b>BUY BANKNIFTY 25 NOV 45500 PE · 1 lot (30)</b>\n"
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


class TestHeader:
    """Manager decision 2026-11-01: the tradable symbol, the exact contract and the unit on
    line 1; the human label on line 2; a risk-blocked alert is prefixed, not dropped."""

    @pytest.mark.parametrize(
        ("expiry", "shown"),
        [
            ("2026-12-03", "03 DEC"),  # DD keeps its leading zero
            (dt.date(2027, 1, 28), "28 JAN"),
            (dt.datetime(2026, 11, 25, 15, 30, tzinfo=IST), "25 NOV"),
            ("2026-11-25T15:30:00+05:30", "25 NOV"),
            # 20:00 UTC on the 24th is already the 25th in India: the IST date is shown
            (dt.datetime(2026, 11, 24, 20, 0, tzinfo=dt.timezone.utc), "25 NOV"),
            ("2026-11-24T20:00:00+00:00", "25 NOV"),
            (" 2026-11-25 ", "25 NOV"),
        ],
    )
    def test_expiry_is_day_and_upper_case_english_month(self, expiry, shown):
        alert = index_alert("CE")
        alert["economics"]["option"]["expiry"] = expiry
        header = format_alert(alert, NOW).split("\n")[0]
        assert header == f"<b>BUY BANKNIFTY {shown} 45000 CE · 1 lot (30)</b>"

    @pytest.mark.parametrize(
        "expiry", [None, "", "soon", "2026-02-30", 20261125, 2.5, ["2026-11-25"], True]
    )
    def test_an_unreadable_expiry_is_left_out_without_a_double_space(self, expiry):
        alert = index_alert("CE")
        alert["economics"]["option"]["expiry"] = expiry
        header = format_alert(alert, NOW).split("\n")[0]
        assert header == "<b>BUY BANKNIFTY 45000 CE · 1 lot (30)</b>"

    def test_month_names_do_not_follow_the_process_locale(self):
        """strftime('%b') is locale-dependent; the header must read DEC on any Mac."""
        saved = locale.setlocale(locale.LC_TIME)
        for name in ("de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
            except locale.Error:
                continue
            try:
                alert = index_alert("CE")
                alert["economics"]["option"]["expiry"] = "2026-12-03"
                assert " 03 DEC 45000 CE " in format_alert(alert, NOW).split("\n")[0]
            finally:
                locale.setlocale(locale.LC_TIME, saved)
            return
        pytest.skip("no non-English locale installed")

    def test_the_label_never_replaces_the_symbol_on_line_one(self):
        alert = index_alert("CE", label="BANK NIFTY INDEX", instrument_key="BANKNIFTY_OPT")
        lines = format_alert(alert, NOW).split("\n")
        assert lines[0].startswith("<b>BUY BANKNIFTY 25 NOV 45000 CE")
        assert lines[1] == "<i>BANK NIFTY INDEX</i>"
        assert "BANKNIFTY_OPT" not in "\n".join(lines)

    def test_a_risk_blocked_alert_is_prefixed_inside_the_bold_and_otherwise_unchanged(self):
        plain = format_alert(stock_alert(), NOW).split("\n")
        blocked = format_alert(stock_alert(risk_allowed=False), NOW).split("\n")
        assert blocked[0] == "<b>[BLOCKED by risk] BUY RELIANCE · 1 share · intraday</b>"
        assert blocked[1:] == plain[1:]

    @pytest.mark.parametrize("flag", [True, None, 0, "", "false", "no"])
    def test_only_an_explicit_false_blocks(self, flag):
        """Same strictness as data.delayed: the Desk always writes a bool."""
        assert "BLOCKED" not in format_alert(stock_alert(risk_allowed=flag), NOW)

    def test_a_blocked_index_alert_keeps_the_contract(self):
        header = format_alert(index_alert("PE", risk_allowed=False), NOW).split("\n")[0]
        assert header == "<b>[BLOCKED by risk] BUY BANKNIFTY 25 NOV 45000 PE · 1 lot (30)</b>"

    @pytest.mark.parametrize("strike", ["abc", "52k", None, [52000], float("nan"), True], ids=repr)
    def test_an_unreadable_strike_shows_a_question_mark_never_a_word(self, strike):
        """The strike is typed into Dhan: only a number is shown, anything else is '?'."""
        alert = index_alert("CE")
        alert["economics"]["option"]["strike"] = strike
        event = exit_event("STOP")
        event["position"]["option"]["strike"] = strike
        for text in (
            format_alert(alert, NOW).split("\n")[0],
            format_missed(alert, NOW),
            format_exit(event, NOW).split("\n")[0],
        ):
            assert " ? CE" in text, text

    def test_a_numeric_string_strike_is_still_a_number(self):
        alert = index_alert("CE")
        alert["economics"]["option"]["strike"] = "52000.0"
        assert " 52000 CE" in format_alert(alert, NOW).split("\n")[0]

    @pytest.mark.parametrize(
        ("side", "horizon", "product"),
        [
            ("BUY", "intraday", " · intraday"),
            ("BUY", "swing", " · delivery"),
            ("SELL", "intraday", " · intraday short"),
            ("SELL", "swing", " · intraday short"),  # stock shorts are intraday only
            ("BUY", None, ""),  # unknown: the product is left out, never guessed
            ("BUY", "weekly", ""),
            ("HOLD", "intraday", ""),  # an unknown side gets no product either
        ],
    )
    def test_a_stock_header_carries_the_product(self, side, horizon, product):
        """Decision 1: the product decides the order type in Dhan (intraday vs delivery)."""
        alert = stock_alert(side=side, horizon=horizon)
        expected = f"<b>{side} RELIANCE · 1 share{product}</b>"
        assert format_alert(alert, NOW).split("\n")[0] == expected
        assert f" · {side} RELIANCE · 1 share{product} · " in format_missed(alert, NOW)

    @pytest.mark.parametrize("horizon", ["intraday", "swing", None])
    def test_an_option_header_never_carries_a_product(self, horizon):
        header = format_alert(index_alert("CE", horizon=horizon), NOW).split("\n")[0]
        assert header.endswith(" CE · 1 lot (30)</b>")

    def test_the_trading_symbol_is_preferred_over_the_series_key(self):
        """US-004 adds trading_symbol (the NSE symbol, e.g. M&M) to the Desk payload; the
        series key ('MM') is only the fallback."""
        with_symbol = stock_alert(series_key="MM", trading_symbol="M&M", label="Mahindra")
        without = stock_alert(series_key="MM", label="Mahindra")
        assert format_alert(with_symbol, NOW).startswith("<b>BUY M&amp;M · ")
        assert format_alert(without, NOW).startswith("<b>BUY MM · ")
        assert " M&amp;M · " in format_missed(with_symbol, NOW)
        event = exit_event(
            "STOP", series_key="MM", trading_symbol="M&M", kind="STOCK", side="BUY", option={}
        )
        assert format_exit(event, NOW).split("\n")[0] == "<b>EXIT: SELL M&amp;M · 1 share</b>"

    @pytest.mark.parametrize("unusable", [None, "", "   ", ["M&M"], {"s": 1}], ids=repr)
    def test_an_unusable_trading_symbol_falls_back_to_the_series_key(self, unusable):
        alert = stock_alert(series_key="RELIANCE", trading_symbol=unusable)
        assert format_alert(alert, NOW).startswith("<b>BUY RELIANCE · ")

    def test_the_label_line_is_omitted_when_it_repeats_the_trading_symbol(self):
        alert = stock_alert(series_key="MM", trading_symbol="M&M", label="M&M")
        assert "<i>" not in format_alert(alert, NOW)

    def test_missed_and_exit_name_the_contract_like_the_header(self):
        later = dt.datetime(2026, 11, 2, 10, 45, 0, tzinfo=IST)
        missed = format_missed(index_alert("PE", risk_allowed=False), later)
        assert missed.startswith("<b>Missed</b> · BUY BANKNIFTY 25 NOV 45000 PE · 1 lot (30) · ")
        event = exit_event("STOP")
        event["position"]["option"]["expiry"] = None
        header = format_exit(event, NOW).split("\n")[0]
        assert header == "<b>EXIT: SELL BANKNIFTY 45000 CE · 1 lot (30)</b>"


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
            ("dhan-ws", None, "Data n/a"),
            ("dhan-ws", "false", "Data n/a"),
            ("dhan-ws", 0, "Data n/a"),
            ("yahoo", "true", "Data n/a"),
            ("yahoo", 1, "Data n/a"),
        ],
    )
    def test_delayed_tag_comes_from_data_delayed_only(self, source, delayed, line):
        """US-028 single rule: data.delayed decides and data.source is never read. Fail-safe:
        'live' only for exactly False, so an alert of unknown freshness never reads live."""
        alert = stock_alert(data={"source": source, "delayed": delayed})
        assert format_alert(alert, NOW).split("\n")[7] == line

    @pytest.mark.parametrize("data", [{"source": "dhan-ws"}, None, "live", []], ids=repr)
    def test_a_missing_delayed_flag_or_data_block_reads_n_a(self, data):
        alert = stock_alert()
        alert["data"] = data
        assert format_alert(alert, NOW).split("\n")[7] == "Data n/a"

    def test_the_unit_comes_from_the_kind_and_lot_size_not_the_unit_label(self):
        stock, index = stock_alert(), index_alert("CE")
        stock["economics"]["unit_label"] = SENTINEL
        del index["economics"]["unit_label"]
        index["economics"]["qty"] = None  # falls back to option.lot
        assert format_alert(stock, NOW).split("\n")[0] == "<b>BUY RELIANCE · 1 share · intraday</b>"
        header = format_alert(index, NOW).split("\n")[0]
        assert header == "<b>BUY BANKNIFTY 25 NOV 45000 CE · 1 lot (30)</b>"

    def test_the_strategy_name_is_the_fallback_for_a_missing_label(self):
        alert = stock_alert()
        del alert["strategy_label"]
        assert format_alert(alert, NOW).split("\n")[4] == "orb 5m"

    def test_numeric_text_fields_are_rendered_as_numbers(self):
        lines = format_alert(stock_alert(series_key=500325, timeframe=5), NOW).split("\n")
        assert lines[0] == "<b>BUY 500325 · 1 share · intraday</b>"
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

    @pytest.mark.parametrize(
        ("score", "shown"),
        [(64.5, "64"), (64.99, "64"), (44.9, "44"), (0.0, "0"), (100, "100"), (None, "n/a")],
    )
    def test_confidence_is_the_floored_score_like_the_desk_title(self, score, shown):
        """BUG-4: one rule on every surface, floor, so 64.9 never shows as a 'medium' 65."""
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
        assert lines[0] == "<b>BUY M&amp;M · 1 share · intraday</b>"
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
        assert lines[0] == "<b>BUY M&amp;M EVIL · 1 share · intraday</b>"
        assert lines[1] == "<i>Mahindra and Mahindra Ltd</i>"
        assert lines[4] == "orb fast 5m"
        assert LINE_SEPARATOR not in msg and "\r" not in msg

    def test_controls_and_bidi_overrides_are_dropped(self):
        """A right-to-left override in a field must not re-order what the phone shows."""
        alert = stock_alert(
            series_key=f"REL{RTL_OVERRIDE}ECNAI\x00", label=f"Reliance{LTR_ISOLATE} Ind\x07"
        )
        lines = format_alert(alert, NOW).split("\n")
        assert lines[0] == "<b>BUY RELECNAI · 1 share · intraday</b>"
        assert lines[1] == "<i>Reliance Ind</i>"

    @pytest.mark.parametrize(
        "junk",
        [*LONE_SURROGATES, ARABIC_LETTER_MARK, BYTE_ORDER_MARK],
        ids=["d800", "dbff", "dc00", "dfff", "061c", "feff"],
    )
    def test_lone_surrogates_and_invisible_marks_are_dropped_so_the_text_encodes(self, junk):
        """A lone surrogate cannot be encoded as UTF-8, so one stray character in a label
        would make the whole alert undeliverable; U+061C and U+FEFF are invisible."""
        alert = index_alert("CE", series_key=f"BANK{junk}NIFTY", label=f"NIFTY{junk} BANK")
        alert["strategy_label"] = f"Opening{junk} Range Breakout"
        for text in (
            format_alert(alert, NOW),
            format_missed(alert, NOW),
            format_exit(exit_event("STOP", series_key=f"BANK{junk}NIFTY"), NOW),
        ):
            assert junk not in text
            text.encode("utf-8")  # raises UnicodeEncodeError on a lone surrogate
            assert "BANKNIFTY" in text
        lines = format_alert(alert, NOW).split("\n")
        assert lines[1] == "<i>NIFTY BANK</i>"

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


class TestJunkScalars:
    """US-003 BUG-5/6: a bool or an astronomically large int where text belongs."""

    HUGE = 10**5000  # str() refuses ints beyond 4300 digits (ValueError)

    @pytest.mark.parametrize("flag", [True, False])
    def test_a_bool_in_a_text_field_renders_empty_never_as_a_word(self, flag):
        alert = index_alert(
            "CE", label=flag, series_key=flag, id=flag, timeframe=flag, strategy_label=flag
        )
        alert["confidence"]["label"] = flag
        msg = format_alert(alert, NOW)
        assert not re.search(r"\b(True|False)\b", msg), msg
        lines = msg.split("\n")
        assert lines[0].startswith("<b>BUY ? 25 NOV 45000 CE · ")  # the symbol reads '?'
        assert lines[4] == "orb"  # a bool strategy label falls back to the strategy name
        assert lines[5] == "Conf 66"
        assert lines[-1].endswith("id <code></code>")
        assert_telegram_html(msg)

    @pytest.mark.parametrize("flag", [True, False])
    def test_a_bool_trading_symbol_falls_back_to_the_series_key(self, flag):
        alert = stock_alert(trading_symbol=flag)
        assert format_alert(alert, NOW).startswith("<b>BUY RELIANCE · 1 share")
        event = exit_event("STOP", trading_symbol=flag, series_key=flag)
        assert format_exit(event, NOW).split("\n")[0].startswith("<b>EXIT: SELL ? ")

    @pytest.mark.parametrize(
        "path",
        [
            "label",
            "series_key",
            "trading_symbol",
            "strategy_label",
            "timeframe",
            "id",
            "side",
            "kind",
            "horizon",
        ],
    )
    def test_a_huge_int_in_a_text_field_never_raises(self, path):
        alert = stock_alert()
        alert[path] = self.HUGE
        msg = format_alert(alert, NOW)
        assert_telegram_html(msg)
        assert "\n" not in format_missed(alert, NOW)

    def test_a_huge_int_in_exit_fields_never_raises(self):
        event = exit_event("STOP", series_key=self.HUGE, unrealized=self.HUGE, kind=self.HUGE)
        event["advice"] = self.HUGE
        lines = format_exit(event, NOW).split("\n")
        assert lines[1:3] == ["Reason: n/a", "Est. P&amp;L n/a"]

    def test_a_huge_score_is_clamped(self):
        alert = stock_alert(confidence={"score": self.HUGE, "label": "high"})
        assert format_alert(alert, NOW).split("\n")[5] == "Conf 100 high"


class TestBudget:
    def test_an_over_budget_label_is_cut_with_an_ellipsis(self):
        msg = format_alert(stock_alert(label="Reliance Industries " * 60), NOW)
        assert len(msg) <= 600
        label_line = msg.split("\n")[1]
        assert label_line.startswith("<i>Reliance Industries Reliance")
        assert label_line.endswith("…</i>")
        assert msg.split("\n")[0] == "<b>BUY RELIANCE · 1 share · intraday</b>"
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
            risk_allowed=False,  # the longest header: blocked prefix + expiry + strike
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
        assert msg.startswith("<b>[BLOCKED by risk] BUY ")
        assert "<i>" in msg  # the label still gets some room

    @pytest.mark.parametrize("char", ["&", "W"], ids=["escaped", "plain"])
    def test_the_fixed_part_leaves_the_label_room(self, char):
        """The longest template (a blocked index alert with an expiry) with every capped
        field at its cap, the widest numbers (a 15-character strike, a 12-digit lot, 10 digits
        of minutes) and '&' (5 characters once escaped): the fixed part stays within 504 HTML
        characters, so the label always keeps at least 88 and the 600 budget holds."""
        wide = char * 400

        def alert(label: str) -> dict[str, Any]:
            a = index_alert(
                "CE",
                id=wide,
                series_key=wide,
                trading_symbol=wide,
                label=label,
                strategy_label=wide,
                timeframe=wide,
                confidence={"score": 100, "label": wide},
                expires_at="9999-12-31T23:59:59+05:30",
                created_at="2026-11-02T10:15:00+05:30",
                data={"delayed": True},
                risk_allowed=False,
            )
            biggest = -999999999999.99
            a["economics"].update(
                entry_price=biggest,
                stop_price=biggest,
                target_price=biggest,
                underlying_stop=biggest,
                underlying_target=biggest,
                qty=999999999999,
            )
            a["economics"]["option"].update(strike=999999999999.99, expiry="2026-11-26")
            return a

        fixed = format_alert(alert(""), NOW)
        assert_telegram_html(fixed)
        assert len(fixed) <= 504
        msg = format_alert(alert("L" * 2000), NOW)
        assert_telegram_html(msg)
        assert len(msg) >= 590  # the label really stretched to fill the budget
        label_line = msg.split("\n")[1]
        assert label_line.startswith("<i>LLLL") and label_line.endswith("…</i>")
        assert len(label_line) - len("<i>…</i>") >= 88

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
            "<b>EXIT: SELL BANKNIFTY 25 NOV 45000 CE · 1 lot (30)</b>\n"
            "Reason: STOP\n"
            "Est. P&amp;L -1,235\n"  # -1234.5 rounds half-up, away from zero
            "Time 11:42:10 IST"
        )

    @pytest.mark.parametrize(
        ("kind", "side", "line_one"),
        [
            ("STOCK", "BUY", "<b>EXIT: SELL SBIN · 1 share</b>"),
            ("STOCK", "SELL", "<b>EXIT: BUY SBIN · 1 share · cover short</b>"),
            ("INDEX", "BUY_CE", "<b>EXIT: SELL SBIN 25 NOV 45000 CE · 1 lot (30)</b>"),
            ("INDEX", "BUY_PE", "<b>EXIT: SELL SBIN 25 NOV 45000 PE · 1 lot (30)</b>"),
            ("STOCK", "HOLD", "<b>EXIT SBIN · 1 share</b>"),  # unknown side: no action named
            ("STOCK", None, "<b>EXIT SBIN · 1 share</b>"),
        ],
    )
    def test_exit_line_one_names_the_closing_action(self, kind, side, line_one):
        """Decision 7: SELL closes a long stock or any bought option; BUY covers a short."""
        event = exit_event("STOP", series_key="SBIN", kind=kind, side=side)
        if kind == "STOCK":
            event["position"].update(qty=1, option={})
        assert format_exit(event, NOW).split("\n")[0] == line_one

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
            "<b>EXIT: BUY TCS · 1 share · cover short</b>\n"
            "Reason: TARGET\nEst. P&amp;L +82\nTime 13:05:00 IST"
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
            "<b>Missed</b> · BUY BANKNIFTY 25 NOV 45000 CE · 1 lot (30) · expired 10:20 IST"
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
