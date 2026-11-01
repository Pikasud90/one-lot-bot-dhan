"""US-003 AC6 (second half): format_exit(event, now) and format_missed(alert, now).

format_exit takes the Desk EXIT payload ``{"advice": str, "position": {...}}`` exactly as
``Desk._manage_positions`` puts it into ``Notification.payload`` and renders at most four
lines: EXIT label, reason, estimated P&L, time. Golden template:

    <b>EXIT: {SELL|BUY} {symbol}[ {DD MMM expiry} {strike:g} {CE|PE}] . {unit}[ . cover short]</b>
    Reason: {ADVICE with underscores as spaces}
    Est. P&amp;L {unrealized:+,.0f}     (the desk's own rounding; '&' is escaped like any other)
    Time {HH:MM:SS} IST              (the time of THIS message, i.e. `now`)

(Manager decisions: format_exit and format_missed name the instrument exactly as the alert
header does, symbol (trading_symbol, else series_key) plus contract. Line 1 also names the
order that CLOSES the position: SELL for a long stock or any bought option, BUY ... cover
short for a short stock; an unknown side names no action, so a guess can never send the owner
the wrong way.)

format_missed(alert, now) is the one-line note the outbox sends instead of an alert whose TTL
ran out during an outage. AC6 fixes only "one line"; the tests pin what a phone user needs to
recognise it: the word Missed, the symbol (plus contract for an option) and the six-character
alert id. No test file contains a unicode escape: special characters come from chr() constants.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from us003_helpers import (
    DOT,
    IST,
    NOW,
    NOW_PE,
    assert_budget,
    assert_well_formed,
    deep_copy,
    exit_event,
    index_ce_live,
    index_pe_position,
    index_pe_synthetic,
    index_position,
    install_network_guard,
    stock_buy,
    stock_position,
    stock_sell,
    telegram,
    visible,
)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the formatter must not touch the network: {attempts}"


def fmt_exit(event: Any, now: dt.datetime) -> str:
    return telegram().format_exit(event, now)


def fmt_missed(alert: Any, now: dt.datetime = NOW) -> str:
    return telegram().format_missed(alert, now)


T_STOP = dt.datetime(2026, 11, 2, 10, 41, 7, tzinfo=IST)
T_TARGET = dt.datetime(2026, 11, 2, 11, 2, 30, tzinfo=IST)
T_TRAIL = dt.datetime(2026, 11, 2, 14, 5, 9, tzinfo=IST)

EXIT_STOCK_LINE1 = f"<b>EXIT: SELL SBIN {DOT} 1 share</b>"  # closes a long
EXIT_STOCK_SHORT_LINE1 = f"<b>EXIT: BUY SBIN {DOT} 1 share {DOT} cover short</b>"
EXIT_STOCK_UNKNOWN_SIDE_LINE1 = f"<b>EXIT SBIN {DOT} 1 share</b>"  # no action is guessed
EXIT_INDEX_CE_LINE1 = f"<b>EXIT: SELL BANKNIFTY 26 NOV 52000 CE {DOT} 1 lot (30)</b>"
EXIT_INDEX_PE_LINE1 = f"<b>EXIT: SELL NIFTY 03 DEC 24500 PE {DOT} 1 lot (65)</b>"  # sells the put

EXIT_STOCK_STOP_REST = ["Reason: STOP", "Est. P&amp;L -7", "Time 10:41:07 IST"]
EXIT_INDEX_TARGET_REST = ["Reason: TARGET", "Est. P&amp;L +1,235", "Time 11:02:30 IST"]
EXIT_INDEX_TRAIL_REST = ["Reason: TRAIL STOP", "Est. P&amp;L +310", "Time 14:05:09 IST"]


# ==========================================================================================
# format_exit: goldens
# ==========================================================================================
def test_golden_exit_for_a_stock_stopped_out_at_a_loss():
    lines = fmt_exit(exit_event(stock_position(), "STOP"), T_STOP).split("\n")
    assert lines[0] == EXIT_STOCK_LINE1
    assert lines[1:] == EXIT_STOCK_STOP_REST


def test_golden_exit_for_a_short_stock_says_buy_to_cover():
    p = stock_position(side="SELL", direction="short", unrealized=12.6)
    lines = fmt_exit(exit_event(p, "TARGET"), T_STOP).split("\n")
    assert lines[0] == EXIT_STOCK_SHORT_LINE1
    assert lines[1:] == ["Reason: TARGET", "Est. P&amp;L +13", "Time 10:41:07 IST"]


def test_golden_exit_for_an_index_option_that_hit_its_target():
    lines = fmt_exit(exit_event(index_position(), "TARGET"), T_TARGET).split("\n")
    assert lines[0] == EXIT_INDEX_CE_LINE1
    assert lines[1:] == EXIT_INDEX_TARGET_REST


def test_golden_exit_for_a_trailing_stop_on_an_index_put():
    lines = fmt_exit(exit_event(index_pe_position(), "TRAIL_STOP"), T_TRAIL).split("\n")
    assert lines[0] == EXIT_INDEX_PE_LINE1
    assert lines[1:] == EXIT_INDEX_TRAIL_REST


def test_exit_names_the_instrument_by_symbol_and_contract_not_by_label():
    text = fmt_exit(exit_event(index_position(), "TARGET"), T_TARGET)
    assert "NIFTY BANK" not in text  # the position's human label is not on the EXIT line
    assert "BANKNIFTY_OPT" not in text  # nor the tracked option's instrument key
    assert visible(text.split("\n")[0]).startswith("EXIT: SELL BANKNIFTY 26 NOV 52000 CE")


@pytest.mark.parametrize(
    ("builder", "side", "line1_start"),
    [
        (stock_position, "BUY", "EXIT: SELL SBIN"),
        (stock_position, "SELL", "EXIT: BUY SBIN"),
        (index_position, "BUY_CE", "EXIT: SELL BANKNIFTY"),
        (index_pe_position, "BUY_PE", "EXIT: SELL NIFTY"),
    ],
    ids=["long-stock", "short-stock", "bought-call", "bought-put"],
)
@pytest.mark.parametrize(
    "advice",
    ["STOP", "TRAIL_STOP", "TARGET", "SIGNAL_EXIT", "OPPOSITE_SIGNAL", "SQUARE_OFF", "TIME_STOP"],
)
def test_the_closing_action_depends_on_the_side_and_never_on_the_advice(
    builder, side, line1_start, advice
):
    p = builder(side=side)
    text = fmt_exit(exit_event(p, advice), T_STOP)
    assert visible(text.split("\n")[0]).startswith(line1_start)
    assert text.split("\n")[1] == f"Reason: {advice.replace('_', ' ')}"


@pytest.mark.parametrize("side", ["HOLD", "", "BUY_XX", "<b>BUY</b>", None, 42, "SELL_PE", "LONG"])
def test_an_unknown_side_names_no_closing_action(side):
    text = fmt_exit(exit_event(stock_position(side=side), "STOP"), T_STOP)
    line1 = text.split("\n")[0]
    assert line1 == EXIT_STOCK_UNKNOWN_SIDE_LINE1
    assert "SELL" not in line1 and "BUY" not in line1 and "cover" not in line1
    assert_well_formed(text)


def test_a_missing_side_names_no_closing_action():
    p = stock_position()
    del p["side"]
    assert fmt_exit(exit_event(p, "STOP"), T_STOP).split("\n")[0] == EXIT_STOCK_UNKNOWN_SIDE_LINE1


def test_a_short_stock_exit_is_never_worded_like_a_long_one():
    long_text = fmt_exit(exit_event(stock_position(), "STOP"), T_STOP)
    short_text = fmt_exit(
        exit_event(stock_position(side="SELL", direction="short"), "STOP"), T_STOP
    )
    assert "cover short" in short_text and "cover short" not in long_text
    assert "EXIT: SELL" in long_text and "EXIT: SELL" not in short_text


def test_exit_uses_trading_symbol_when_the_position_carries_one():
    p = stock_position(series_key="MM", trading_symbol="M&M")
    assert fmt_exit(exit_event(p, "STOP"), T_STOP).split("\n")[0] == (
        f"<b>EXIT: SELL M&amp;M {DOT} 1 share</b>"
    )
    blank = stock_position(series_key="MM", trading_symbol="   ")
    assert "EXIT: SELL MM " in fmt_exit(exit_event(blank, "STOP"), T_STOP)


@pytest.mark.parametrize(
    "advice",
    ["STOP", "TRAIL_STOP", "TARGET", "SIGNAL_EXIT", "OPPOSITE_SIGNAL", "SQUARE_OFF", "TIME_STOP"],
)
def test_every_desk_advice_code_becomes_a_reason_line_with_spaces(advice):
    """These seven codes are all the Desk can put in the EXIT payload (desk._manage_positions
    and tracker.exit_advice)."""
    text = fmt_exit(exit_event(stock_position(), advice), T_STOP)
    assert text.split("\n")[1] == f"Reason: {advice.replace('_', ' ')}"


def test_exit_is_exactly_four_lines_in_the_normal_case():
    for event, now in [
        (exit_event(stock_position(), "STOP"), T_STOP),
        (exit_event(index_position(), "TARGET"), T_TARGET),
        (exit_event(index_pe_position(), "TRAIL_STOP"), T_TRAIL),
    ]:
        text = fmt_exit(event, now)
        assert len(text.split("\n")) == 4
        assert_budget(text, max_lines=4)
        assert_well_formed(text)


def test_exit_pnl_ampersand_is_an_entity_not_a_raw_ampersand():
    text = fmt_exit(exit_event(stock_position(), "STOP"), T_STOP)
    assert "P&amp;L" in text
    assert "P&L" not in text
    assert_well_formed(text)


@pytest.mark.parametrize(
    ("unrealized", "shown"),
    [(-7.4, "-7"), (1234.56, "+1,235"), (310.2, "+310"), (-15432.1, "-15,432"), (2.6, "+3")],
)
def test_exit_estimated_pnl_has_a_sign_thousands_separators_and_no_decimals(unrealized, shown):
    event = exit_event(stock_position(unrealized=unrealized), "STOP")
    assert fmt_exit(event, T_STOP).split("\n")[2] == f"Est. P&amp;L {shown}"


def test_exit_time_is_the_time_of_the_message_in_ist_even_when_now_is_utc():
    now_utc = dt.datetime(2026, 11, 2, 5, 11, 7, tzinfo=dt.timezone.utc)  # 10:41:07 IST
    lines = fmt_exit(exit_event(stock_position(), "STOP"), now_utc).split("\n")
    assert lines[1:] == EXIT_STOCK_STOP_REST


def test_exit_ignores_every_position_field_it_does_not_show():
    p = stock_position()
    for key in ("balance", "holdings", "ledger", "token", "account"):
        p[key] = f"SENTINEL-{key.upper()}-999"
    p["notes"] = ["SENTINEL-NOTE"]
    p["hold_check"] = "SENTINEL-HOLD-CHECK"
    p["source"] = "SENTINEL-SOURCE"
    p["label"] = "SENTINEL-LABEL"
    text = fmt_exit(exit_event(p, "STOP"), T_STOP)
    assert "SENTINEL" not in text
    clean = fmt_exit(exit_event(stock_position(), "STOP"), T_STOP)
    assert text == clean


def test_exit_does_not_mutate_the_event():
    event = exit_event(index_position(), "TARGET")
    before = deep_copy(event)
    fmt_exit(event, T_TARGET)
    assert event == before


def test_exit_escapes_the_symbol_and_the_reason():
    p = stock_position(series_key="M&M <b>X</b>\nNSE")
    text = fmt_exit(exit_event(p, "A<B&C"), T_STOP)
    lines = text.split("\n")
    assert lines[0].startswith("<b>EXIT: SELL M&amp;M &lt;b&gt;X&lt;/b&gt; NSE")
    assert lines[1] == "Reason: A&lt;B&amp;C"
    assert len(lines) == 4
    assert_well_formed(text)


@pytest.mark.parametrize("expiry", [None, "", "not-a-date", 20261126])
def test_exit_with_an_unreadable_expiry_still_names_symbol_strike_and_type(expiry):
    p = index_position()
    p["option"]["expiry"] = expiry
    text = fmt_exit(exit_event(p, "TARGET"), T_TARGET)
    assert len(text.split("\n")) == 4
    assert_well_formed(text)
    assert visible(text.split("\n")[0]).startswith("EXIT: SELL BANKNIFTY ")
    assert " 52000 CE" in visible(text.split("\n")[0])
    assert "None" not in text


@given(
    series_key=st.text(max_size=2000),
    advice=st.text(max_size=300),
    unrealized=st.floats(min_value=-1e9, max_value=1e9, allow_nan=False),
    index=st.booleans(),
)
@settings(max_examples=150, deadline=None, derandomize=True)
def test_exit_for_hostile_text_stays_within_four_lines_600_chars_and_well_formed(
    series_key, advice, unrealized, index
):
    p = index_position() if index else stock_position()
    p.update(series_key=series_key, unrealized=unrealized)
    text = fmt_exit(exit_event(p, advice), T_STOP)
    assert_budget(text, max_lines=4)
    assert_well_formed(text)


@given(series_key=st.text(max_size=2000))
@settings(max_examples=100, deadline=None, derandomize=True)
def test_exit_always_keeps_the_exit_word_the_reason_the_pnl_and_the_time(series_key):
    p = stock_position(series_key=series_key)
    lines = fmt_exit(exit_event(p, "STOP"), T_STOP).split("\n")
    assert lines[0].startswith("<b>EXIT: SELL ") and lines[0].endswith("</b>")
    assert lines[1:] == EXIT_STOCK_STOP_REST


# ==========================================================================================
# format_missed
# ==========================================================================================
@pytest.mark.parametrize(
    ("builder", "named", "short_id"),
    [
        (stock_buy, "SBIN", "a1b2c3"),
        (stock_sell, "RELIANCE", "0f9e8d"),
        (index_ce_live, "BANKNIFTY 26 NOV 52000 CE", "c0ffee"),
        (index_pe_synthetic, "NIFTY 03 DEC 24500 PE", "5e1f00"),
    ],
    ids=["stock-buy", "stock-sell", "index-ce", "index-pe"],
)
def test_missed_is_one_line_naming_the_symbol_and_contract_and_the_short_id(
    builder, named, short_id
):
    text = fmt_missed(builder(), NOW_PE)
    assert "\n" not in text
    assert "missed" in text.lower()
    shown = visible(text)
    assert named in shown
    assert short_id in shown
    assert_budget(text, max_lines=1)
    assert_well_formed(text)


def test_missed_ignores_the_account_fields():
    a = stock_buy()
    a["balance"] = "SENTINEL-BALANCE-1"
    a["economics"]["holdings"] = "SENTINEL-HOLDINGS-1"
    a["reasons"] = ["SENTINEL-REASON"]
    assert "SENTINEL" not in fmt_missed(a)


def test_missed_escapes_and_flattens_a_hostile_symbol():
    a = stock_buy()
    a["series_key"] = "M&M\n<b>x</b>"
    text = fmt_missed(a)
    assert "\n" not in text
    assert "M&amp;M &lt;b&gt;x&lt;/b&gt;" in text
    assert_well_formed(text)


def test_missed_does_not_mutate_the_alert():
    a = index_ce_live()
    before = deep_copy(a)
    fmt_missed(a)
    assert a == before


@given(
    series_key=st.text(max_size=2000),
    label=st.text(max_size=2000),
    alert_id=st.text(min_size=6, max_size=80),
    index=st.booleans(),
)
@settings(max_examples=150, deadline=None, derandomize=True)
def test_missed_for_hostile_text_is_one_line_within_budget_and_well_formed(
    series_key, label, alert_id, index
):
    a = index_ce_live() if index else stock_buy()
    a.update(series_key=series_key, label=label, id=alert_id)
    text = fmt_missed(a)
    assert "\n" not in text
    assert_budget(text, max_lines=1)
    assert_well_formed(text)
