"""US-003 AC1 and AC2: format_alert(alert, now) -> Telegram HTML.

The line template (AC1 order; Manager decisions of 2026-11-01 for lines 1 and 2, and the
review-round and mode-B decisions for the product, the symbol, the confidence number and the
data tag):

    <b>{BUY|SELL} {symbol}[ {DD MMM expiry} {strike:g} {CE|PE}] . {1 share | 1 lot (N)}[ . {product}]</b>
    <i>{label}</i>                                     only when it differs from the shown symbol
    Entry {price} {mkt | ask | est.}                   stock: mkt; index premium: ask (live) | est.
    Underlying Stop {x} . Target {y|trail}             INDEX ONLY, before the premium levels
    Stop {x} . Target {y|trail}                        stock: share prices; index: premium levels
    {strategy_label} {timeframe}
    Conf {NN} {label}                                  NN = the score FLOORED, clamped to 0..100
    Valid till {HH:MM} IST ({M}m left | expired)
    Data {live | <b>delayed ~15m</b> | n/a}            data.delayed ONLY: live only for exactly False
    Alert {HH:MM:SS} IST . id <code>{first six chars}</code>

(the dot is the middle dot U+00B7, built with chr(); DD MMM is the expiry day and the upper-case
English month, e.g. 26 NOV). The product is stock-only: BUY with horizon intraday -> 'intraday',
BUY with swing -> 'delivery', SELL -> 'intraday short'; an unknown horizon or side adds nothing
and an option header carries none. The symbol is trading_symbol when present, else series_key.
A risk-blocked alert (risk_allowed False) is still formatted and its line 1 starts with
'[BLOCKED by risk] '. Prices use `{:,.2f}`; the strike uses `{:g}`; NN is the score floored
(71.6 shows 71), so it never crosses a label threshold. Every free-text value is HTML-escaped
with whitespace runs collapsed to one space. Only these fields are read: id, created_at,
expires_at, trading_symbol (else series_key), label, kind, side, horizon, strategy_label (else
strategy), timeframe, risk_allowed, confidence.{score,label}, data.delayed, economics.{qty,
entry_price, stop_price, target_price, underlying_stop, underlying_target, synthetic,
option.{strike, opt_type, expiry, lot}}.

The module is imported lazily (us003_helpers.telegram) so each test fails on its own while
onelot.notify.telegram is incomplete. No test file contains a unicode escape or a non-ASCII
literal: every special character comes from a chr() constant in us003_helpers.
"""

from __future__ import annotations

import datetime as dt
from types import MappingProxyType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from us003_helpers import (
    CREATED,
    DOT,
    E_ACUTE,
    ELLIPSES,
    EMOJI_CHART,
    EMOJI_FAMILY,
    EMOJI_ROCKET,
    EXPIRES,
    IST,
    NOW,
    NOW_PE,
    NOW_SELL,
    assert_budget,
    assert_well_formed,
    deep_copy,
    header_of,
    index_ce_live,
    index_pe_synthetic,
    install_network_guard,
    label_shown,
    no_target_index,
    no_target_stock,
    squash,
    stock_buy,
    stock_sell,
    tags_in,
    telegram,
    visible,
)

BLOCKED = "[BLOCKED by risk] "


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the formatter must not touch the network: {attempts}"


def fmt(alert: Any, now: dt.datetime = NOW) -> str:
    return telegram().format_alert(alert, now)


# ==========================================================================================
# AC1: golden texts
# ==========================================================================================
GOLDEN_STOCK_BUY = "\n".join(
    [
        f"<b>BUY SBIN {DOT} 1 share {DOT} intraday</b>",
        "<i>State Bank of India</i>",
        "Entry 812.40 mkt",
        f"Stop 805.00 {DOT} Target 827.20",
        "Trend-filtered Mean Reversion 5m",
        "Conf 71 high",  # 71.6 floored, never rounded up to 72
        "Valid till 10:37 IST (9m left)",
        "Data live",
        f"Alert 10:27:03 IST {DOT} id <code>a1b2c3</code>",
    ]
)

GOLDEN_STOCK_SELL_DELAYED = "\n".join(
    [
        f"<b>SELL RELIANCE {DOT} 1 share {DOT} intraday short</b>",
        "<i>Reliance Industries</i>",
        "Entry 2,951.35 mkt",
        f"Stop 2,967.10 {DOT} Target 2,919.85",
        "EMA Cross (trend + momentum + volume) 15m",
        "Conf 58 medium",
        "Valid till 11:13 IST (3m left)",
        "Data <b>delayed ~15m</b>",
        f"Alert 11:03:41 IST {DOT} id <code>0f9e8d</code>",
    ]
)

GOLDEN_INDEX_CE_LIVE_CHAIN = "\n".join(
    [
        f"<b>BUY BANKNIFTY 26 NOV 52000 CE {DOT} 1 lot (30)</b>",
        "<i>NIFTY BANK</i>",
        "Entry 125.50 ask",
        f"Underlying Stop 51,850.00 {DOT} Target 52,300.00",
        f"Stop 98.00 {DOT} Target 160.00",
        "EMA Cross (trend + momentum + volume) 12m",
        "Conf 68 medium",
        "Valid till 10:37 IST (9m left)",
        "Data live",
        f"Alert 10:27:03 IST {DOT} id <code>c0ffee</code>",
    ]
)

GOLDEN_INDEX_PE_SYNTHETIC = "\n".join(
    [
        f"<b>BUY NIFTY 03 DEC 24500 PE {DOT} 1 lot (65)</b>",
        "<i>NIFTY 50</i>",
        "Entry 118.35 est.",
        f"Underlying Stop 24,610.00 {DOT} Target 24,320.50",
        f"Stop 92.10 {DOT} Target 150.45",
        "Supertrend (tuned) + volume + momentum 5m",
        "Conf 44 low",  # 44.9 floored: stays below the 45 threshold of its label
        "Valid till 13:22 IST (6m left)",
        "Data live",
        f"Alert 13:12:09 IST {DOT} id <code>5e1f00</code>",
    ]
)

GOLDEN_STOCK_NO_TARGET = GOLDEN_STOCK_BUY.replace(
    f"Stop 805.00 {DOT} Target 827.20", f"Stop 805.00 {DOT} Target trail"
)

GOLDEN_INDEX_NO_TARGET = GOLDEN_INDEX_CE_LIVE_CHAIN.replace(
    f"Underlying Stop 51,850.00 {DOT} Target 52,300.00",
    f"Underlying Stop 51,850.00 {DOT} Target trail",
).replace(f"Stop 98.00 {DOT} Target 160.00", f"Stop 98.00 {DOT} Target trail")

STOCK_HEADER = GOLDEN_STOCK_BUY.split("\n")[0]
STOCK_REST = GOLDEN_STOCK_BUY.split("\n")[2:]  # every line below the header and the label


def test_golden_stock_buy_with_live_feed():
    assert fmt(stock_buy()) == GOLDEN_STOCK_BUY


def test_golden_stock_sell_with_delayed_yahoo_feed():
    assert fmt(stock_sell(), NOW_SELL) == GOLDEN_STOCK_SELL_DELAYED


def test_golden_index_ce_with_live_chain_premium_tagged_ask():
    assert fmt(index_ce_live()) == GOLDEN_INDEX_CE_LIVE_CHAIN


def test_golden_index_pe_with_synthetic_premium_tagged_est():
    assert fmt(index_pe_synthetic(), NOW_PE) == GOLDEN_INDEX_PE_SYNTHETIC


def test_golden_stock_without_target_says_trail():
    assert fmt(no_target_stock()) == GOLDEN_STOCK_NO_TARGET


def test_golden_index_without_target_says_trail_on_both_levels():
    assert fmt(no_target_index()) == GOLDEN_INDEX_NO_TARGET


@pytest.mark.parametrize(
    ("alert", "now"),
    [
        (stock_buy(), NOW),
        (stock_sell(), NOW_SELL),
        (index_ce_live(), NOW),
        (index_pe_synthetic(), NOW_PE),
        (no_target_stock(), NOW),
        (no_target_index(), NOW),
    ],
    ids=["stock-buy", "stock-sell", "index-ce", "index-pe", "stock-no-target", "index-no-target"],
)
def test_every_golden_fixture_fits_the_budgets_and_is_well_formed(alert, now):
    text = fmt(alert, now)
    assert_budget(text)  # AC1: at most 12 lines and 600 characters
    assert_well_formed(text)  # balanced tags, only b/i/code, entities valid
    assert {name for _closing, name in tags_in(text)} <= {"b", "i", "code"}


# ==========================================================================================
# AC1: the header (line 1) and the label (line 2)
# ==========================================================================================
def test_header_is_the_action_the_symbol_the_unit_and_the_product_for_stocks():
    assert header_of(fmt(stock_buy())) == f"<b>BUY SBIN {DOT} 1 share {DOT} intraday</b>"
    sell = header_of(fmt(stock_sell(), NOW_SELL))
    assert sell == f"<b>SELL RELIANCE {DOT} 1 share {DOT} intraday short</b>"


def test_index_header_is_buy_with_expiry_strike_and_ce_or_pe_then_the_lot():
    ce = header_of(fmt(index_ce_live()))
    assert ce == f"<b>BUY BANKNIFTY 26 NOV 52000 CE {DOT} 1 lot (30)</b>"
    pe = header_of(fmt(index_pe_synthetic(), NOW_PE))
    assert pe == f"<b>BUY NIFTY 03 DEC 24500 PE {DOT} 1 lot (65)</b>"


def test_header_uses_the_series_key_never_the_option_instrument_key_or_the_label():
    text = fmt(index_ce_live())
    assert "BANKNIFTY_OPT" not in text  # instrument_key of the tracked option
    assert header_of(text).startswith("<b>BUY BANKNIFTY ")
    assert "NIFTY BANK" not in header_of(text)  # the human label lives on line 2


@pytest.mark.parametrize("strike", [52000, 52000.0], ids=["int-strike", "float-strike"])
def test_strike_has_no_trailing_zeros_whether_int_or_float(strike):
    a = index_ce_live()
    a["economics"]["option"]["strike"] = strike
    assert header_of(fmt(a)) == f"<b>BUY BANKNIFTY 26 NOV 52000 CE {DOT} 1 lot (30)</b>"


@pytest.mark.parametrize(
    ("expiry", "shown"),
    [
        ("2026-01-29", "29 JAN"),
        ("2026-02-26", "26 FEB"),
        ("2026-03-31", "31 MAR"),
        ("2026-04-28", "28 APR"),
        ("2026-05-26", "26 MAY"),
        ("2026-06-30", "30 JUN"),
        ("2026-07-28", "28 JUL"),
        ("2026-08-25", "25 AUG"),
        ("2026-09-29", "29 SEP"),
        ("2026-10-27", "27 OCT"),
        ("2026-11-24", "24 NOV"),
        ("2026-12-01", "01 DEC"),  # DD is two digits
        ("2026-11-26T15:30:00+05:30", "26 NOV"),  # a full ISO timestamp works too
    ],
)
def test_expiry_is_shown_as_day_and_upper_case_english_month(expiry, shown):
    a = index_ce_live()
    a["economics"]["option"]["expiry"] = expiry
    assert header_of(fmt(a)) == f"<b>BUY BANKNIFTY {shown} 52000 CE {DOT} 1 lot (30)</b>"


@pytest.mark.parametrize("expiry", [None, "", "not-a-date", "2026-13-45", 20261126, ["2026-11-26"]])
def test_a_missing_or_unreadable_expiry_never_breaks_the_message(expiry):
    a = index_ce_live()
    a["economics"]["option"]["expiry"] = expiry
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)
    head = visible(header_of(text))
    assert head.startswith("BUY BANKNIFTY ")
    assert " 52000 CE " in head
    assert "None" not in text
    assert text.split("\n")[2:] == GOLDEN_INDEX_CE_LIVE_CHAIN.split("\n")[2:]


def test_a_stock_header_has_no_expiry_strike_or_option_type():
    assert visible(header_of(fmt(stock_buy()))) == f"BUY SBIN {DOT} 1 share {DOT} intraday"


def test_the_label_follows_on_line_two_in_italics():
    assert fmt(stock_buy()).split("\n")[1] == "<i>State Bank of India</i>"
    assert fmt(index_ce_live()).split("\n")[1] == "<i>NIFTY BANK</i>"


def test_the_label_line_is_omitted_when_it_repeats_the_symbol():
    a = stock_buy()
    a["label"] = "SBIN"
    text = fmt(a)
    assert text.split("\n") == [STOCK_HEADER, *STOCK_REST]
    assert "<i>" not in text


@pytest.mark.parametrize("label", ["", "   ", "\n\t "])
def test_an_empty_label_leaves_no_empty_italic_tag(label):
    a = stock_buy()
    a["label"] = label
    text = fmt(a)
    assert "<i></i>" not in text
    assert "<i> </i>" not in text
    assert_well_formed(text)
    assert text.split("\n") == [STOCK_HEADER, *STOCK_REST]


def test_unit_is_one_share_for_stocks_and_one_lot_with_its_size_for_index():
    assert f"{DOT} 1 share {DOT} " in header_of(fmt(stock_buy()))  # the unit, then the product
    assert header_of(fmt(index_ce_live())).endswith(f"{DOT} 1 lot (30)</b>")
    assert header_of(fmt(index_pe_synthetic(), NOW_PE)).endswith(f"{DOT} 1 lot (65)</b>")


def test_unit_does_not_depend_on_the_unit_label_field_being_present():
    stock, index = stock_buy(), index_ce_live()
    del stock["economics"]["unit_label"]
    del index["economics"]["unit_label"]
    assert f"{DOT} 1 share {DOT} intraday</b>" in header_of(fmt(stock))
    assert header_of(fmt(index)).endswith(f"{DOT} 1 lot (30)</b>")


# ==========================================================================================
# AC1: the other lines, one item at a time
# ==========================================================================================
def test_entry_is_tagged_mkt_for_stock_ask_for_live_chain_est_for_synthetic():
    assert "Entry 812.40 mkt" in fmt(stock_buy()).split("\n")
    assert "Entry 125.50 ask" in fmt(index_ce_live()).split("\n")
    assert "Entry 118.35 est." in fmt(index_pe_synthetic(), NOW_PE).split("\n")


def test_stock_entry_with_thousands_keeps_separators_and_two_decimals():
    a = stock_buy()
    a["economics"]["entry_price"] = 12345.6
    assert "Entry 12,345.60 mkt" in fmt(a).split("\n")


def test_stock_has_no_underlying_line_but_index_does():
    assert not [ln for ln in fmt(stock_buy()).split("\n") if ln.startswith("Underlying")]
    assert f"Underlying Stop 51,850.00 {DOT} Target 52,300.00" in fmt(index_ce_live()).split("\n")


def test_index_alert_lists_the_underlying_levels_before_the_premium_levels():
    lines = fmt(index_ce_live()).split("\n")
    under = lines.index(f"Underlying Stop 51,850.00 {DOT} Target 52,300.00")
    premium = lines.index(f"Stop 98.00 {DOT} Target 160.00")
    assert lines.index("Entry 125.50 ask") < under < premium


def test_strategy_line_is_the_strategy_label_then_the_timeframe():
    assert "Trend-filtered Mean Reversion 5m" in fmt(stock_buy()).split("\n")


@pytest.mark.parametrize(
    ("score", "shown"),
    [
        (71.6, "71"),  # floored, never rounded up
        (72.4, "72"),
        (72.0, "72"),
        (0.0, "0"),
        (0.9, "0"),
        (100.0, "100"),
        (44.9, "44"),
        (9.2, "9"),
        (64.9, "64"),  # never shows the 65 that reads 'high' next to a 'medium' label
        (65.0, "65"),
        (99.9, "99"),
        (71, "71"),  # an int score works too
        (100.5, "100"),  # clamped to 0..100
        (250, "100"),
        (-3.0, "0"),
        (-0.4, "0"),
    ],
)
def test_confidence_is_the_floored_score_clamped_to_0_100_then_the_label(score, shown):
    a = stock_buy()
    a["confidence"] = {"score": score, "label": "high", "parts": {}}
    assert f"Conf {shown} high" in fmt(a).split("\n")


@pytest.mark.parametrize(
    "score", [None, float("nan"), float("inf"), float("-inf"), True, False, [71], {"x": 1}]
)
def test_a_confidence_score_that_is_not_a_number_reads_n_a(score):
    a = stock_buy()
    a["confidence"] = {"score": score, "label": "high", "parts": {}}
    assert "Conf n/a high" in fmt(a).split("\n")


@given(tenths=st.integers(min_value=0, max_value=1000))
@settings(max_examples=300, deadline=None, derandomize=True)
def test_the_shown_confidence_is_the_floor_of_every_one_decimal_score(tenths):
    a = stock_buy()
    a["confidence"] = {"score": tenths / 10, "label": "medium", "parts": {}}
    assert f"Conf {tenths // 10} medium" in fmt(a).split("\n")


@pytest.mark.parametrize(
    ("remaining_seconds", "tail"),
    [
        (600, "10m left"),
        (558, "9m left"),  # the golden: 9 min 18 s
        (599, "9m left"),  # floored, never rounded up
        (61, "1m left"),
        (60, "1m left"),
        (0, "expired"),
        (-1, "expired"),
        (-300, "expired"),
        (-86_400, "expired"),
    ],
)
def test_ttl_shows_floored_minutes_left_or_expired(remaining_seconds, tail):
    a = stock_buy()
    now = dt.datetime.fromisoformat(a["expires_at"]) - dt.timedelta(seconds=remaining_seconds)
    assert f"Valid till 10:37 IST ({tail})" in fmt(a, now).split("\n")


@pytest.mark.parametrize("remaining_seconds", [59, 30, 1])
def test_ttl_under_one_minute_reads_expired_because_the_floored_minutes_are_zero(
    remaining_seconds,
):
    """AC1 says 'expired when 0 or less' about the FLOORED minutes left, so the last 59 s
    show 'expired'. Safe-side by design: a stale alert is never offered as actionable.
    (If the Manager prefers '<1m left' for the last minute, change only this test.)"""
    a = stock_buy()
    now = dt.datetime.fromisoformat(a["expires_at"]) - dt.timedelta(seconds=remaining_seconds)
    assert "Valid till 10:37 IST (expired)" in fmt(a, now).split("\n")


def test_ttl_minutes_left_counts_down_as_now_advances():
    a = stock_buy()
    left = [
        next(ln for ln in fmt(a, NOW + dt.timedelta(minutes=k)).split("\n") if "Valid" in ln)
        for k in range(0, 11)
    ]
    assert left[0] == "Valid till 10:37 IST (9m left)"
    assert left[1] == "Valid till 10:37 IST (8m left)"
    assert left[9] == "Valid till 10:37 IST (expired)"  # 18 s left -> floor 0
    assert left[10] == "Valid till 10:37 IST (expired)"


def test_ttl_is_an_absolute_ist_deadline_even_when_inputs_are_in_utc():
    a = stock_buy()
    a["created_at"] = "2026-11-02T04:57:03+00:00"  # 10:27:03 IST
    a["expires_at"] = "2026-11-02T05:07:03+00:00"  # 10:37:03 IST
    now_utc = dt.datetime(2026, 11, 2, 4, 57, 45, tzinfo=dt.timezone.utc)  # 10:27:45 IST
    assert fmt(a, now_utc) == GOLDEN_STOCK_BUY


def test_alert_time_is_the_alert_creation_time_not_now():
    text = fmt(stock_buy(), NOW + dt.timedelta(minutes=3, seconds=9))
    assert f"Alert 10:27:03 IST {DOT} id <code>a1b2c3</code>" in text.split("\n")
    assert CREATED[11:19] == "10:27:03"  # the fixture really differs from NOW (10:27:45)


def test_alert_id_is_cut_to_its_first_six_characters():
    a = stock_buy()
    a["id"] = "ABCDEF0123456789"
    assert f"{DOT} id <code>ABCDEF</code>" in fmt(a)
    assert "ABCDEF0" not in fmt(a)


def test_delayed_tag_comes_from_data_delayed_true_even_when_the_source_looks_live():
    a = stock_buy()
    a["data"]["delayed"] = True  # source still says "dhan"
    text = fmt(a)
    assert "Data <b>delayed ~15m</b>" in text.split("\n")
    assert "Data live" not in text


def test_live_tag_comes_from_data_delayed_false_even_when_the_source_says_yahoo():
    """US-028's single rule: the formatter never re-derives the tag from data.source."""
    a = stock_buy()
    a["data"]["source"] = "yahoo"  # data.delayed stays False
    text = fmt(a)
    assert "Data live" in text.split("\n")
    assert "delayed" not in text


@pytest.mark.parametrize("source", ["dhan", "dhan-ws", "mock", "yahoo", "YAHOO-bars", "", None])
def test_delayed_tag_is_independent_of_the_source_for_every_source_name(source):
    for delayed in (True, False):
        a = stock_buy()
        a["data"]["source"] = source
        a["data"]["delayed"] = delayed
        assert ("delayed ~15m" in fmt(a)) is delayed


def test_formatting_does_not_mutate_the_alert():
    for alert, now in [(stock_buy(), NOW), (index_ce_live(), NOW), (index_pe_synthetic(), NOW_PE)]:
        before = deep_copy(alert)
        fmt(alert, now)
        assert alert == before


def test_a_read_only_mapping_is_accepted_and_gives_the_same_text():
    a = stock_buy()
    assert fmt(MappingProxyType(a)) == GOLDEN_STOCK_BUY


def test_formatting_is_deterministic_for_the_same_alert_and_now():
    assert fmt(index_ce_live()) == fmt(index_ce_live())


def test_the_deadline_strings_in_the_fixtures_are_what_the_goldens_assume():
    assert (EXPIRES[11:16], CREATED[11:16]) == ("10:37", "10:27")
    assert NOW.utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert str(IST) == "Asia/Kolkata"


# ==========================================================================================
# Manager decision 2: a risk-blocked alert is still formatted, with a prefix on line 1
# ==========================================================================================
def test_a_risk_blocked_stock_alert_is_prefixed_on_line_one_and_otherwise_identical():
    a = stock_buy()
    a["risk_allowed"] = False
    text = fmt(a)
    lines = text.split("\n")
    assert visible(lines[0]) == f"{BLOCKED}BUY SBIN {DOT} 1 share {DOT} intraday"
    assert lines[1:] == GOLDEN_STOCK_BUY.split("\n")[1:]
    assert len(lines) == len(GOLDEN_STOCK_BUY.split("\n"))  # a prefix, not an extra line
    assert_well_formed(text)


def test_a_risk_blocked_index_alert_is_prefixed_on_line_one_and_otherwise_identical():
    a = index_ce_live()
    a["risk_allowed"] = False
    text = fmt(a)
    lines = text.split("\n")
    assert visible(lines[0]) == f"{BLOCKED}BUY BANKNIFTY 26 NOV 52000 CE {DOT} 1 lot (30)"
    assert lines[1:] == GOLDEN_INDEX_CE_LIVE_CHAIN.split("\n")[1:]
    assert_well_formed(text)


def test_a_blocked_delayed_alert_keeps_both_markers():
    a = stock_sell()
    a["risk_allowed"] = False
    text = fmt(a, NOW_SELL)
    assert visible(header_of(text)).startswith(f"{BLOCKED}SELL RELIANCE")
    assert "Data <b>delayed ~15m</b>" in text.split("\n")
    assert_budget(text)
    assert_well_formed(text)


@pytest.mark.parametrize("flag", [True, None, "missing"], ids=["true", "none", "missing"])
def test_only_an_explicit_false_risk_allowed_adds_the_blocked_prefix(flag):
    a = stock_buy()
    if flag == "missing":
        del a["risk_allowed"]
    else:
        a["risk_allowed"] = flag
    assert fmt(a) == GOLDEN_STOCK_BUY
    assert "BLOCKED" not in fmt(a)


def test_the_blocked_prefix_does_not_break_the_budget_when_the_label_is_huge():
    a = index_ce_live()
    a["risk_allowed"] = False
    a["label"] = "NIFTY MIDCAP SELECT " * 40
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)
    assert visible(header_of(text)).startswith(BLOCKED)


# ==========================================================================================
# AC1: budgets and truncation of the label
# ==========================================================================================
def test_a_label_far_over_budget_is_truncated_with_an_ellipsis_and_stays_well_formed():
    a = stock_buy()
    a["label"] = "Mahindra " * 200  # 1,800 characters
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)
    lines = text.split("\n")
    label_line = lines[1]
    assert label_line.startswith("<i>Mahindra Mahindra") and label_line.endswith("</i>")
    assert visible(label_line).endswith(ELLIPSES), label_line
    # the symbol line and everything below the label are unaffected by the truncation
    assert lines[0] == STOCK_HEADER
    assert lines[2:] == STOCK_REST


def test_truncation_never_cuts_an_entity_even_when_the_cut_falls_inside_one():
    """Every candidate cut position is hit: labels made only of '&' (5 chars each once
    escaped) of every length from a bit under to a bit over the budget."""
    for n in range(40, 700, 3):
        a = stock_buy()
        a["label"] = "&" * n
        text = fmt(a)
        assert_budget(text)
        assert_well_formed(text)


def test_truncation_never_cuts_a_tag_that_was_in_the_label():
    for n in range(20, 300, 7):
        a = stock_buy()
        a["label"] = "<b>bold</b><i>x</i><code>y</code>" * n
        text = fmt(a)
        assert_budget(text)
        assert_well_formed(text)
        assert tags_in(text).count((False, "b")) == 1  # only the header's own tag is real
        assert tags_in(text).count((False, "i")) == 1  # and only the label line's own italics


def test_the_index_alert_with_the_longest_realistic_text_still_fits():
    a = index_ce_live()
    a["label"] = "NIFTY MIDCAP SELECT " * 20
    a["strategy_label"] = "Intraday momentum (first 30m -> last 30m)"
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)
    assert "-&gt;" in text


def test_a_short_label_is_never_truncated():
    a = stock_buy()
    a["label"] = "Tata Motors"
    assert fmt(a).split("\n")[1] == "<i>Tata Motors</i>"


PRINTABLE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Cf", "Zl", "Zp")), max_size=2000
)
DENSE_MARKUP = st.lists(
    st.sampled_from(["&", "<", ">", '"', "'", " ", "a", "\n", "&amp;", "<b>", "</b>", "&lt;"]),
    max_size=500,
).map("".join)


@given(label=st.text(max_size=2000))
@settings(max_examples=150, deadline=None, derandomize=True)
def test_any_label_keeps_the_12_line_600_char_budget_and_well_formed_html(label):
    a = stock_buy()
    a["label"] = label
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)


@given(label=st.one_of(PRINTABLE, DENSE_MARKUP))
@settings(max_examples=200, deadline=None, derandomize=True)
def test_a_label_is_shown_whole_or_as_a_long_prefix_with_an_ellipsis_and_nothing_else_moves(label):
    a = stock_buy()
    a["label"] = label
    text = fmt(a)
    complete, kept = label_shown(text, label)
    want = squash(label)
    assert complete or kept >= min(40, len(want)), (kept, len(want))
    lines = text.split("\n")
    assert lines[0] == STOCK_HEADER
    assert lines[-len(STOCK_REST) :] == STOCK_REST


@given(
    label=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")), min_size=1, max_size=30
    ).filter(lambda s: s != "SBIN")
)
@settings(max_examples=100, deadline=None, derandomize=True)
def test_a_short_plain_label_appears_exactly_on_line_two(label):
    a = stock_buy()
    a["label"] = label
    assert fmt(a).split("\n")[1] == f"<i>{label}</i>"


@given(
    label=st.text(max_size=400),
    series_key=st.text(max_size=400),
    strategy_label=st.text(max_size=400),
    confidence_label=st.text(max_size=200),
    timeframe=st.text(max_size=100),
    alert_id=st.text(min_size=6, max_size=60),
    expiry=st.text(max_size=100),
    kind=st.sampled_from(["stock", "index"]),
    delayed=st.booleans(),
    blocked=st.booleans(),
)
@settings(max_examples=200, deadline=None, derandomize=True)
def test_the_budget_holds_when_many_free_text_fields_are_hostile_at_once(
    label,
    series_key,
    strategy_label,
    confidence_label,
    timeframe,
    alert_id,
    expiry,
    kind,
    delayed,
    blocked,
):
    """AC1's '12 lines and 600 characters' is unconditional, so a long symbol, strategy label
    or confidence label must not break it either (only the label is named as truncatable;
    see the report's AC notes)."""
    a = stock_buy() if kind == "stock" else index_ce_live()
    a.update(
        label=label,
        series_key=series_key,
        strategy_label=strategy_label,
        timeframe=timeframe,
        id=alert_id,
    )
    a["confidence"]["label"] = confidence_label
    a["data"]["delayed"] = delayed
    a["risk_allowed"] = not blocked
    if kind == "index":
        a["economics"]["option"]["expiry"] = expiry
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)


# ==========================================================================================
# AC2: escaping, newline collapsing, emoji
# ==========================================================================================
def test_instrument_ampersand_is_escaped_in_the_header_and_the_label_line():
    a = stock_buy()
    a.update(series_key="M&M", instrument_key="M&M", label="Mahindra & Mahindra")
    text = fmt(a)
    lines = text.split("\n")
    assert lines[0] == f"<b>BUY M&amp;M {DOT} 1 share {DOT} intraday</b>"
    assert lines[1] == "<i>Mahindra &amp; Mahindra</i>"
    assert "M&M" not in text
    assert_well_formed(text)


def test_a_label_equal_to_an_ampersand_symbol_is_omitted():
    a = stock_buy()
    a.update(series_key="M&M", label="M&M")
    text = fmt(a)
    assert text.split("\n")[0] == f"<b>BUY M&amp;M {DOT} 1 share {DOT} intraday</b>"
    assert "<i>" not in text
    assert_well_formed(text)


def test_a_label_containing_a_tag_is_escaped_not_interpreted():
    a = stock_buy()
    a["label"] = "<b>X</b><script>alert(1)</script><i>"
    text = fmt(a)
    assert "<script>" not in text
    assert text.split("\n")[1] == (
        "<i>&lt;b&gt;X&lt;/b&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;i&gt;</i>"
    )
    assert_well_formed(text)


HOSTILE = "<script>alert(1)</script> & <b>bold</b>\n\"quoted\" 'x' </i></code>"


def _set_path(alert: dict[str, Any], path: str, value: Any) -> None:
    node = alert
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[key]
    node[leaf] = value


@pytest.mark.parametrize(
    "path",
    [
        "series_key",
        "label",
        "strategy_label",
        "timeframe",
        "confidence.label",
        "id",
        "economics.option.expiry",
        "economics.option.opt_type",
    ],
)
def test_hostile_text_in_any_dynamic_field_adds_no_real_tags_lines_or_raw_symbols(path):
    a = index_ce_live()
    _set_path(a, path, HOSTILE)
    text = fmt(a)
    assert "<script>" not in text
    assert_budget(text)
    assert_well_formed(text)
    tags = tags_in(text)
    assert tags.count((False, "b")) == 1  # the header's own tag, nothing from the hostile text
    assert tags.count((False, "code")) == 1  # the id's own tag
    assert len(tags) <= 6


def test_angle_brackets_and_ampersands_are_escaped_in_every_dynamic_field():
    a = stock_buy()
    a["strategy_label"] = "A<B & C>D"
    a["timeframe"] = "<5m>"
    a["confidence"]["label"] = "<high>&"
    text = fmt(a)
    assert "A&lt;B &amp; C&gt;D &lt;5m&gt;" in text
    assert "Conf 71 &lt;high&gt;&amp;" in text
    assert_well_formed(text)


def test_strategy_label_arrow_from_the_real_registry_is_escaped():
    a = stock_buy()
    a["strategy_label"] = "Intraday momentum (first 30m -> last 30m)"
    assert "Intraday momentum (first 30m -&gt; last 30m) 5m" in fmt(a).split("\n")


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r", "\t", " \n "], ids=repr)
def test_newlines_and_tabs_inside_a_field_are_collapsed_to_one_space(newline):
    a = stock_buy()
    a["label"] = f"M&M{newline}Ltd"
    text = fmt(a)
    assert text.split("\n")[1] == "<i>M&amp;M Ltd</i>"
    assert len(text.split("\n")) == len(GOLDEN_STOCK_BUY.split("\n"))
    assert "\r" not in text
    assert "\t" not in text


def test_newlines_in_other_fields_do_not_add_lines():
    a = stock_buy()
    a["series_key"] = "SB\nIN"
    a["strategy_label"] = "Trend\nfiltered\nMean Reversion"
    a["confidence"]["label"] = "very\nhigh"
    a["id"] = "ab\ncdef0123"
    text = fmt(a)
    assert len(text.split("\n")) == len(GOLDEN_STOCK_BUY.split("\n"))
    assert_well_formed(text)


def test_emoji_in_the_label_survive_unescaped_and_unmangled():
    a = stock_buy()
    a["label"] = f"Nifty {EMOJI_CHART} Bank {EMOJI_ROCKET}"
    text = fmt(a)
    assert text.split("\n")[1] == f"<i>Nifty {EMOJI_CHART} Bank {EMOJI_ROCKET}</i>"
    assert_well_formed(text)


def test_accented_letters_survive_unmangled():
    a = stock_buy()
    a["label"] = f"Caf{E_ACUTE} Coffee Day"
    assert fmt(a).split("\n")[1] == f"<i>Caf{E_ACUTE} Coffee Day</i>"


def test_a_truncated_emoji_label_still_encodes_as_utf8_and_stays_in_budget():
    a = stock_buy()
    a["label"] = f"{EMOJI_FAMILY} " * 300  # zero-width-joiner family emoji
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)


def test_an_entity_looking_label_is_shown_literally_not_decoded():
    a = stock_buy()
    a["label"] = "&amp; &lt;b&gt; &#65;"
    text = fmt(a)
    assert visible(text.split("\n")[1]) == "&amp; &lt;b&gt; &#65;"
    assert_well_formed(text)


# ==========================================================================================
# AC2: the field whitelist (series_key and option.expiry ARE whitelisted)
# ==========================================================================================
ACCOUNT_SENTINELS = {
    "balance": "SENTINEL-BALANCE-7654321.09",
    "holdings": "SENTINEL-HOLDINGS-RELIANCE-x500",
    "ledger": "SENTINEL-LEDGER-ROW-0042",
    "token": "SENTINEL-TOKEN-eyJhbGciOiJIUzI1NiJ9",
}


def _with_sentinels(alert: dict[str, Any]) -> dict[str, Any]:
    """Account-shaped data in every place a careless formatter might read it from."""
    a = deep_copy(alert)
    for key, value in ACCOUNT_SENTINELS.items():
        a[key] = value
        a["data"][key] = value
        a["economics"][key] = value
        a["economics"]["option"][key] = value
        a["confidence"][key] = value
        a["manage"][key] = value
    a["account"] = {"capital_inr": "SENTINEL-CAPITAL-5000000", "positions": ["SENTINEL-POS-1"]}
    a["economics"]["notes"] = ["SENTINEL-NOTE-chain-quote"]
    a["reasons"] = ["SENTINEL-REASON-rsi"]
    a["text"] = "SENTINEL-DESK-TEXT Why: SENTINEL-WHY"
    a["risk_reason"] = "SENTINEL-RISK-REASON"
    a["status"] = "SENTINEL-STATUS"
    a["outcome"] = {"pnl": "SENTINEL-OUTCOME"}
    return a


@pytest.mark.parametrize(
    ("builder", "now"),
    [(stock_buy, NOW), (index_ce_live, NOW), (index_pe_synthetic, NOW_PE)],
    ids=["stock", "index-live", "index-synthetic"],
)
def test_account_fields_and_every_non_whitelisted_field_never_reach_the_output(builder, now):
    dirty = _with_sentinels(builder())
    text = fmt(dirty, now)
    assert "SENTINEL" not in text
    assert "7654321" not in text
    assert "eyJhbGci" not in text
    # and the output is exactly what the clean alert gives: the whitelist ignored all of it
    assert text == fmt(builder(), now)


@given(
    junk=st.dictionaries(
        keys=st.text(min_size=1, max_size=12).map(lambda s: "zz_junk_" + s),
        values=st.one_of(st.text(max_size=30), st.integers(), st.floats(allow_nan=False)),
        max_size=6,
    ),
    where=st.sampled_from(["top", "data", "economics", "option", "confidence"]),
)
@settings(max_examples=100, deadline=None, derandomize=True)
def test_unknown_keys_anywhere_in_the_alert_do_not_change_the_message(junk, where):
    base = index_ce_live()
    dirty = deep_copy(base)
    target = {
        "top": dirty,
        "data": dirty["data"],
        "economics": dirty["economics"],
        "option": dirty["economics"]["option"],
        "confidence": dirty["confidence"],
    }[where]
    target.update(junk)
    assert fmt(dirty) == fmt(base)


@pytest.mark.parametrize("name", sorted(ACCOUNT_SENTINELS))
def test_a_top_level_account_field_alone_is_ignored(name):
    a = stock_buy()
    a[name] = f"SENTINEL-{name.upper()}-ONLY"
    assert fmt(a) == GOLDEN_STOCK_BUY
