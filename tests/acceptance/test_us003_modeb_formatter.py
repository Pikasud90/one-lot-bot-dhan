"""US-003 mode B: edge, time, money and invariant attacks on the three formatters.

Mode A pinned the contract. These tests go after what a hostile payload, a clock edge or a
rounding rule could still break: junk in every numeric field, money rounding, midnight and
month/year/leap-day expiries, the IST reading of expiry dates, the same instant expressed in
other zones (including a DST change), the locale of the process, a monotonic countdown, and
the worst-case message size. No test file contains a unicode escape: special characters come
from chr() constants in us003_helpers.
"""

from __future__ import annotations

import datetime as dt
import locale
import re
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from us003_helpers import (
    CREATED,
    DOT,
    EXPIRES,
    IST,
    NOW,
    NOW_PE,
    assert_budget,
    assert_well_formed,
    deep_copy,
    exit_event,
    header_of,
    html_problems,
    index_ce_live,
    index_pe_synthetic,
    index_position,
    install_network_guard,
    stock_buy,
    stock_position,
    telegram,
    visible,
)

T_EXIT = dt.datetime(2026, 11, 2, 10, 41, 7, tzinfo=IST)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the formatter must not touch the network: {attempts}"


def fmt(alert: Any, now: dt.datetime = NOW) -> str:
    return telegram().format_alert(alert, now)


def line_with(text: str, prefix: str) -> str:
    return next(ln for ln in text.split("\n") if ln.startswith(prefix))


# ==========================================================================================
# money and numbers
# ==========================================================================================
@pytest.mark.parametrize(
    ("price", "shown"),
    [
        (812.405, "812.41"),  # an exact half at the third decimal goes up, not to even
        (812.404, "812.40"),
        (812.395, "812.40"),
        (2.675, "2.68"),  # the classic binary-float trap: f"{2.675:.2f}" says 2.67
        (0.005, "0.01"),
        (0.0, "0.00"),
        (7, "7.00"),
        (1234567.895, "1,234,567.90"),
        (999_999_999_999.99, "999,999,999,999.99"),
        (-12.5, "-12.50"),
    ],
)
def test_entry_price_is_rounded_half_up_with_separators(price, shown):
    a = stock_buy()
    a["economics"]["entry_price"] = price
    assert f"Entry {shown} mkt" in fmt(a).split("\n")


NOT_A_NUMBER = [
    float("nan"),
    float("inf"),
    float("-inf"),
    10**30,
    1e12,
    -1e12,
    True,
    False,
    None,
    "",
    "abc",
    "1e400",
    [1.5],
    {"price": 1.5},
]


@pytest.mark.parametrize("junk", NOT_A_NUMBER, ids=[repr(j)[:14] for j in NOT_A_NUMBER])
def test_a_price_that_is_not_a_sane_number_reads_n_a_instead_of_raising(junk):
    a = stock_buy()
    a["economics"]["entry_price"] = junk
    assert "Entry n/a mkt" in fmt(a).split("\n")


@pytest.mark.parametrize(
    ("pnl", "shown"),
    [
        (0.4, "0"),
        (-0.3, "0"),  # never a signed zero
        (0.0, "0"),
        (0.5, "+1"),  # half up, away from zero
        (1.5, "+2"),
        (2.5, "+3"),
        (-0.5, "-1"),
        (-2.5, "-3"),
        (1234.56, "+1,235"),
        (-999_999.4, "-999,999"),
    ],
)
def test_exit_pnl_is_whole_rupees_half_up_with_no_signed_zero(pnl, shown):
    event = exit_event(stock_position(unrealized=pnl), "STOP")
    assert telegram().format_exit(event, T_EXIT).split("\n")[2] == f"Est. P&amp;L {shown}"


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), None, "x", True, [1], {"a": 1}])
def test_exit_pnl_that_is_not_a_number_reads_n_a(junk):
    event = exit_event(stock_position(unrealized=junk), "STOP")
    text = telegram().format_exit(event, T_EXIT)
    assert text.split("\n")[2] == "Est. P&amp;L n/a"
    assert len(text.split("\n")) == 4


@pytest.mark.parametrize(
    ("strike", "shown"),
    [
        (52000, "52000"),
        (52000.0, "52000"),
        (52000.25, "52000.25"),
        (100.10, "100.1"),
        (5.0, "5"),
        (1_000_000, "1000000"),  # never scientific notation
        (24550.5, "24550.5"),
    ],
)
def test_strike_is_plain_digits_without_exponent_or_trailing_zeros(strike, shown):
    a = index_ce_live()
    a["economics"]["option"]["strike"] = strike
    assert f"26 NOV {shown} CE" in header_of(fmt(a))


@pytest.mark.parametrize("synthetic", [None, 0, 1, "false", "no", "missing"])
def test_an_index_premium_is_tagged_ask_only_when_synthetic_is_exactly_false(synthetic):
    """Fail-safe tagging: a missing or odd marker never claims a live-chain ask."""
    a = index_ce_live()
    if synthetic == "missing":
        del a["economics"]["synthetic"]
    else:
        a["economics"]["synthetic"] = synthetic
    assert "Entry 125.50 est." in fmt(a).split("\n")


BAD_STRIKES = [float("nan"), float("inf"), float("-inf"), True, False]


@pytest.mark.parametrize("strike", BAD_STRIKES, ids=repr)
def test_a_strike_that_is_not_a_number_is_never_printed_as_a_word(strike):
    """The strike is what the owner types into Dhan, so an unreadable one must show the
    formatter's own placeholder (as a missing strike does), never the words nan, inf, True
    or False."""
    a = index_ce_live()
    a["economics"]["option"]["strike"] = strike
    tg = telegram()
    p = index_position()
    p["option"]["strike"] = strike
    shown = {
        "alert": header_of(tg.format_alert(a, NOW)),
        "exit": header_of(tg.format_exit(exit_event(p, "STOP"), T_EXIT)),
        "missed": tg.format_missed(a, NOW),
    }
    for where, text in shown.items():
        assert not re.search(r"\b(nan|inf|true|false)\b", visible(text), re.IGNORECASE), (
            where,
            text,
        )
        assert " ? CE" in visible(text), (where, text)


def test_a_missing_strike_shows_the_placeholder():
    a = index_ce_live()
    del a["economics"]["option"]["strike"]
    assert " ? CE " in visible(header_of(fmt(a)))


NUMERIC_FIELDS = [
    "economics.entry_price",
    "economics.stop_price",
    "economics.target_price",
    "economics.underlying_stop",
    "economics.underlying_target",
    "economics.qty",
    "economics.option.lot",
    "confidence.score",
]

JUNK_NUMBER = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-(10**40), max_value=10**40),
    st.none(),
    st.booleans(),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=4), st.integers(), max_size=3),
)


def _set_path(alert: dict[str, Any], path: str, value: Any) -> None:
    node = alert
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[key]
    node[leaf] = value


@given(
    junk=st.lists(JUNK_NUMBER, min_size=len(NUMERIC_FIELDS), max_size=len(NUMERIC_FIELDS)),
    blocked=st.booleans(),
    delayed=st.booleans(),
)
@settings(max_examples=250, deadline=None, derandomize=True)
def test_junk_in_every_numeric_field_never_raises_leaks_a_repr_or_breaks_the_budget(
    junk, blocked, delayed
):
    a = index_ce_live()
    for path, value in zip(NUMERIC_FIELDS, junk, strict=True):
        _set_path(a, path, value)
    a["risk_allowed"] = not blocked
    a["data"]["delayed"] = delayed
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)
    shown = text.replace("[BLOCKED by risk] ", "")
    assert not re.search(r"\b(nan|inf|infinity|none|true|false)\b", shown, re.IGNORECASE), text
    assert not re.search(r"[{}\[\]']", shown), text  # a container is never stringified


# ==========================================================================================
# time edges
# ==========================================================================================
def _with_times(created: str, expires: str) -> dict[str, Any]:
    a = stock_buy()
    a["created_at"], a["expires_at"] = created, expires
    return a


def test_a_deadline_after_midnight_counts_minutes_across_the_date_change():
    a = _with_times("2026-11-02T23:58:00+05:30", "2026-11-03T00:08:00+05:30")
    now = dt.datetime(2026, 11, 2, 23, 59, 0, tzinfo=IST)
    text = fmt(a, now)
    assert "Valid till 00:08 IST (9m left)" in text.split("\n")
    assert "Alert 23:58:00 IST" in text


def test_an_alert_raised_at_the_market_open_boundary_shows_the_exact_times():
    a = _with_times("2026-11-02T09:15:00+05:30", "2026-11-02T09:25:00+05:30")
    now = dt.datetime(2026, 11, 2, 9, 15, 0, tzinfo=IST)
    text = fmt(a, now)
    assert "Valid till 09:25 IST (10m left)" in text.split("\n")
    assert "Alert 09:15:00 IST" in text


def test_an_alert_whose_deadline_is_the_closing_minute_is_not_rounded_up():
    a = _with_times("2026-11-02T15:20:00+05:30", "2026-11-02T15:29:59+05:30")
    now = dt.datetime(2026, 11, 2, 15, 20, 0, tzinfo=IST)
    # 9 min 59 s left: floored to 9, and the deadline minute is the one the clock shows
    assert "Valid till 15:29 IST (9m left)" in fmt(a, now).split("\n")


def test_a_deadline_equal_to_now_is_expired_and_one_second_before_it_also_reads_expired():
    a = stock_buy()
    deadline = dt.datetime.fromisoformat(EXPIRES)
    assert "Valid till 10:37 IST (expired)" in fmt(a, deadline).split("\n")
    assert "Valid till 10:37 IST (expired)" in fmt(a, deadline - dt.timedelta(seconds=1)).split(
        "\n"
    )


@pytest.mark.parametrize(
    ("expiry", "shown"),
    [
        ("2026-12-31", "31 DEC"),  # year end
        ("2027-01-01", "01 JAN"),  # the day after
        ("2026-03-31", "31 MAR"),  # quarter end
        ("2026-09-30", "30 SEP"),
        ("2028-02-29", "29 FEB"),  # a leap day
        ("2026-02-28", "28 FEB"),
        ("2026-11-02", "02 NOV"),  # expiry day is the alert day
    ],
)
def test_calendar_edge_expiries_are_shown_exactly(expiry, shown):
    a = index_ce_live()
    a["economics"]["option"]["expiry"] = expiry
    assert header_of(fmt(a)) == f"<b>BUY BANKNIFTY {shown} 52000 CE {DOT} 1 lot (30)</b>"


@pytest.mark.parametrize("expiry", ["2027-02-29", "2026-04-31", "2026-00-10", "2026-06-00"])
def test_an_impossible_calendar_date_is_left_out_rather_than_invented(expiry):
    a = index_ce_live()
    a["economics"]["option"]["expiry"] = expiry
    head = visible(header_of(fmt(a)))
    assert head == f"BUY BANKNIFTY 52000 CE {DOT} 1 lot (30)"


@pytest.mark.parametrize(
    ("expiry", "shown"),
    [
        ("2026-11-25T18:29:59+00:00", "25 NOV"),  # 23:59:59 IST on the 25th
        ("2026-11-25T18:30:00+00:00", "26 NOV"),  # exactly midnight IST: the 26th
        ("2026-11-26T00:00:00+00:00", "26 NOV"),
        ("2026-11-25T20:00:00-05:00", "26 NOV"),  # 06:30 IST on the 26th
        ("2026-11-26T03:00:00+09:00", "25 NOV"),  # 23:30 IST on the 25th
    ],
)
def test_an_expiry_timestamp_is_read_in_ist(expiry, shown):
    a = index_ce_live()
    a["economics"]["option"]["expiry"] = expiry
    assert f" {shown} 52000 CE " in header_of(fmt(a))


ZONES = [
    "UTC",
    "America/New_York",
    "Europe/London",
    "Asia/Tokyo",
    "Pacific/Auckland",
    "Australia/Lord_Howe",  # a 30-minute DST shift
    "Asia/Kolkata",
]


def _in_zone(value: str, zone: str) -> str:
    return dt.datetime.fromisoformat(value).astimezone(ZoneInfo(zone)).isoformat()


@given(
    seconds_before_deadline=st.integers(min_value=-4 * 3600, max_value=11 * 60),
    zone=st.sampled_from(ZONES),
    alert_zone=st.sampled_from(ZONES),
)
@settings(max_examples=150, deadline=None, derandomize=True)
def test_the_same_instants_in_any_zone_give_the_same_text(
    seconds_before_deadline, zone, alert_zone
):
    base = index_ce_live()
    deadline = dt.datetime.fromisoformat(EXPIRES)
    now_ist = deadline - dt.timedelta(seconds=seconds_before_deadline)
    moved = deep_copy(base)
    moved["created_at"] = _in_zone(CREATED, alert_zone)
    moved["expires_at"] = _in_zone(EXPIRES, alert_zone)
    now_moved = now_ist.astimezone(ZoneInfo(zone))
    assert fmt(moved, now_moved) == fmt(base, now_ist)


@pytest.mark.parametrize(
    "instant_utc",
    [
        dt.datetime(2026, 3, 8, 6, 59, 59, tzinfo=dt.timezone.utc),  # New York: 01:59:59 EST
        dt.datetime(2026, 3, 8, 7, 0, 1, tzinfo=dt.timezone.utc),  # 03:00:01 EDT, the hour skipped
        dt.datetime(2026, 11, 1, 5, 59, 59, tzinfo=dt.timezone.utc),  # 01:59:59 EDT
        dt.datetime(
            2026, 11, 1, 6, 0, 1, tzinfo=dt.timezone.utc
        ),  # 01:00:01 EST, the hour repeated
        dt.datetime(2026, 3, 29, 0, 59, 59, tzinfo=dt.timezone.utc),  # London: clocks go forward
        dt.datetime(2026, 10, 25, 0, 59, 59, tzinfo=dt.timezone.utc),  # London: clocks go back
    ],
    ids=[
        "ny-spring-before",
        "ny-spring-after",
        "ny-fall-before",
        "ny-fall-after",
        "ldn-fwd",
        "ldn-back",
    ],
)
def test_a_daylight_saving_change_in_the_callers_zone_does_not_shift_the_message(instant_utc):
    created = instant_utc.astimezone(IST) - dt.timedelta(seconds=40)
    expires = created + dt.timedelta(minutes=10)
    a = stock_buy()
    a["created_at"], a["expires_at"] = created.isoformat(), expires.isoformat()
    for zone in ("America/New_York", "Europe/London", "UTC"):
        assert fmt(a, instant_utc.astimezone(ZoneInfo(zone))) == fmt(a, instant_utc.astimezone(IST))
    assert "(9m left)" in fmt(a, instant_utc)  # 40 s after creation: 9 min 20 s left, floored


def test_naive_datetimes_are_read_as_ist_the_project_convention():
    """Characterisation, not a requirement of the story: onelot.clock.to_ist reads a naive
    value as IST, and the formatter follows it (US-001's 'naive input is rejected' is not
    applied here). Pinned so a change to it is a decision, not an accident."""
    aware = stock_buy()
    naive = stock_buy()
    naive["created_at"] = CREATED[:19]  # no offset
    naive["expires_at"] = EXPIRES[:19]
    now_naive = NOW.replace(tzinfo=None)
    assert fmt(naive, now_naive) == fmt(aware, NOW)


def test_a_timestamp_that_is_not_a_date_reads_n_a_instead_of_raising():
    a = stock_buy()
    a["created_at"], a["expires_at"] = "yesterday", None
    text = fmt(a)
    assert "n/a" in line_with(text, "Valid till")
    assert "n/a" in line_with(text, "Alert")
    assert_budget(text)
    assert_well_formed(text)


# ==========================================================================================
# invariants
# ==========================================================================================
def _minutes_left(text: str) -> int:
    ttl = line_with(text, "Valid till")
    m = re.search(r"\((\d+)m left\)", ttl)
    return int(m.group(1)) if m else 0  # 'expired' counts as zero


@given(
    t1=st.integers(min_value=-1200, max_value=1200),
    t2=st.integers(min_value=-1200, max_value=1200),
)
@settings(max_examples=200, deadline=None, derandomize=True)
def test_minutes_left_never_increases_as_time_passes_and_equals_the_floored_gap(t1, t2):
    a = stock_buy()
    deadline = dt.datetime.fromisoformat(EXPIRES)
    less, more = sorted((t1, t2))  # seconds still remaining: `less` is the later moment
    shown_later = _minutes_left(fmt(a, deadline - dt.timedelta(seconds=less)))
    shown_earlier = _minutes_left(fmt(a, deadline - dt.timedelta(seconds=more)))
    assert shown_later <= shown_earlier  # time passing can never show more minutes
    assert shown_later == (less // 60 if less >= 60 else 0)  # floored; under a minute is 0
    assert shown_earlier == (more // 60 if more >= 60 else 0)


def test_the_message_does_not_depend_on_the_process_locale():
    """The month comes from a table, not strftime('%b'), so a German or French desk machine
    still writes NOV and DEC."""
    a = index_pe_synthetic()
    before = fmt(a, NOW_PE)
    saved = locale.setlocale(locale.LC_TIME)
    changed = []
    try:
        for name in ("de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8", "ja_JP.UTF-8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
            except locale.Error:
                continue
            changed.append(name)
            assert fmt(a, NOW_PE) == before
            assert "03 DEC" in header_of(fmt(a, NOW_PE))
    finally:
        locale.setlocale(locale.LC_TIME, saved)
    assert "03 DEC" in before, changed


def test_the_worst_case_alert_fits_the_budget_and_keeps_every_fixed_line():
    """Every capped field full of '&' (five characters each once escaped), the largest
    numbers that print, blocked and delayed and an index: the label may be dropped but the
    nine fixed lines must survive within 12 lines and 600 characters."""
    a = index_ce_live()
    big = 999_999_999_999.99
    a.update(
        series_key="&" * 80,
        label="&" * 5000,
        strategy_label="&" * 200,
        timeframe="&" * 80,
        id="&" * 20,
        risk_allowed=False,
    )
    a["confidence"] = {"score": 999_999_999_999.4, "label": "&" * 80}
    a["data"]["delayed"] = True
    eco = a["economics"]
    eco.update(
        entry_price=big,
        stop_price=big,
        target_price=big,
        underlying_stop=big,
        underlying_target=big,
        qty=999_999_999_999,
    )
    eco["option"].update(strike=big, expiry="2026-11-26")
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)
    lines = text.split("\n")
    prefixes = ("Entry", "Underlying Stop", "Stop", "Conf", "Valid till", "Data", "Alert")
    for prefix in prefixes:
        assert any(ln.startswith(prefix) for ln in lines), (prefix, text)
    assert visible(lines[0]).startswith("[BLOCKED by risk] BUY ")


def test_a_realistic_maximum_alert_still_has_room_for_a_long_label():
    a = index_ce_live()
    a["label"] = "NIFTY MIDCAP SELECT INDEX FUTURES AND OPTIONS PRODUCT"
    a["risk_allowed"] = False
    a["data"]["delayed"] = True
    text = fmt(a)
    assert_budget(text)
    assert text.split("\n")[1] == f"<i>{a['label']}</i>"  # nothing real is truncated


# ==========================================================================================
# the other two formatters under arbitrary payloads
# ==========================================================================================
SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(max_size=40),
    st.dates(),
    st.datetimes(timezones=st.timezones()),
    st.binary(max_size=4),
)
JSON_LIKE = st.recursive(
    SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)
ALERT_KEYS = [
    "id",
    "created_at",
    "expires_at",
    "series_key",
    "label",
    "kind",
    "side",
    "strategy",
    "strategy_label",
    "timeframe",
    "risk_allowed",
    "confidence",
    "data",
    "economics",
]
EVENT_KEYS = ["advice", "position", "series_key", "unrealized"]
HEALTH = [HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.large_base_example]


@given(st.dictionaries(st.sampled_from(ALERT_KEYS), JSON_LIKE, max_size=len(ALERT_KEYS)))
@settings(max_examples=400, deadline=None, derandomize=True, suppress_health_check=HEALTH)
def test_format_alert_and_format_missed_survive_any_json_like_alert(alert):
    tg = telegram()
    text = tg.format_alert(alert, NOW)
    assert_budget(text)
    assert not html_problems(text), (html_problems(text), text)
    missed = tg.format_missed(alert, NOW)
    assert_budget(missed, max_lines=1)
    assert not html_problems(missed), (html_problems(missed), missed)


@given(st.dictionaries(st.sampled_from(EVENT_KEYS), JSON_LIKE, max_size=len(EVENT_KEYS)))
@settings(max_examples=300, deadline=None, derandomize=True, suppress_health_check=HEALTH)
def test_format_exit_survives_any_json_like_event(event):
    text = telegram().format_exit(event, NOW)
    assert_budget(text, max_lines=4)
    assert len(text.split("\n")) == 4
    assert not html_problems(text), (html_problems(text), text)


def test_exit_and_missed_for_empty_payloads_are_still_well_formed():
    tg = telegram()
    exit_text = tg.format_exit({}, NOW)
    assert len(exit_text.split("\n")) == 4
    assert_well_formed(exit_text)
    missed = tg.format_missed({}, NOW)
    assert "\n" not in missed
    assert_well_formed(missed)


def test_an_exit_for_a_position_with_no_option_still_names_the_symbol():
    p = index_position()
    p["option"] = {}
    text = telegram().format_exit(exit_event(p, "STOP"), T_EXIT)
    assert visible(text.split("\n")[0]).startswith("EXIT BANKNIFTY")
    assert len(text.split("\n")) == 4
    assert_well_formed(text)
