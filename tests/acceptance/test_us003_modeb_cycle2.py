"""US-003 mode B, cycle 2: the Manager's review-round and mode-B decisions, verified from the
outside, plus regression tests for the four cycle-1 bugs.

Decisions covered here (the older acceptance tests were updated to match them):

* stock headers carry the Dhan product: BUY + intraday -> intraday, BUY + swing -> delivery,
  SELL -> intraday short; an unknown horizon or side adds nothing; options carry none;
* the symbol is trading_symbol when present, else series_key, and the label line is dropped
  only when it repeats the SHOWN symbol;
* the data line says live only for a data.delayed that is exactly False, else n/a;
* one confidence number everywhere: notify.base.shown_confidence floors the score and clamps
  it to 0..100, and the label thresholds apply to the stored one-decimal score;
* lone surrogates, U+061C and U+FEFF are stripped (a surrogate made an alert unsendable);
* send_message never raises and a closed client is unavailable;
* masked() says None, set or invalid for the token, and telegram_token_forms and
  telegram_redaction_literals are the one registration rule for Secrets and the client.

No test file contains a unicode escape: every special character comes from chr().
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from us003_helpers import (
    CHAT,
    DOT,
    NOW,
    TOKEN,
    Recorder,
    assert_budget,
    assert_well_formed,
    capture_logs,
    header_of,
    index_ce_live,
    install_network_guard,
    leaked_forms,
    make_client,
    make_http,
    ok_response,
    record_blob,
    stock_buy,
    stock_position,
    telegram,
    visible,
)

from onelot.config import (
    Secrets,
    telegram_redaction_literals,
    telegram_token_forms,
)
from onelot.desk.audit import confidence
from onelot.logging_setup import get_redactor
from onelot.notify.base import shown_confidence

BLOCKED = "[BLOCKED by risk] "


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"unexpected network attempt: {attempts}"


def fmt(alert: Any, now: Any = NOW) -> str:
    return telegram().format_alert(alert, now)


def head(alert: Any) -> str:
    return visible(header_of(fmt(alert)))


# ==========================================================================================
# the stock product on line 1
# ==========================================================================================
@pytest.mark.parametrize(
    ("side", "horizon", "product"),
    [
        ("BUY", "intraday", "intraday"),
        ("BUY", "swing", "delivery"),
        ("SELL", "intraday", "intraday short"),
        ("SELL", "swing", "intraday short"),  # a stock short is intraday only (ADR-001 am. 8)
        ("SELL", None, "intraday short"),
        ("SELL", "positional", "intraday short"),
    ],
)
def test_a_stock_header_names_the_dhan_product(side, horizon, product):
    a = stock_buy()
    a["side"], a["horizon"] = side, horizon
    assert head(a) == f"{side} SBIN {DOT} 1 share {DOT} {product}"
    assert_well_formed(fmt(a))


@pytest.mark.parametrize("horizon", ["positional", "", "  ", None, 5, ["swing"], {"h": "swing"}])
def test_a_buy_with_an_unknown_horizon_gets_no_product_rather_than_a_guess(horizon):
    a = stock_buy()
    a["horizon"] = horizon
    assert head(a) == f"BUY SBIN {DOT} 1 share"


def test_a_buy_with_no_horizon_key_gets_no_product():
    a = stock_buy()
    del a["horizon"]
    assert head(a) == f"BUY SBIN {DOT} 1 share"


@pytest.mark.parametrize("side", ["HOLD", "", "BUY_XX", "LONG", None, 7])
@pytest.mark.parametrize("horizon", ["intraday", "swing"])
def test_an_unknown_side_adds_no_product(side, horizon):
    a = stock_buy()
    a["side"], a["horizon"] = side, horizon
    text = fmt(a)
    assert "intraday" not in text and "delivery" not in text
    assert_budget(text)
    assert_well_formed(text)


@pytest.mark.parametrize("horizon", ["intraday", "swing", None, "positional"])
def test_an_option_header_never_carries_a_product(horizon):
    a = index_ce_live()
    a["horizon"] = horizon
    assert head(a) == f"BUY BANKNIFTY 26 NOV 52000 CE {DOT} 1 lot (30)"


def test_a_hostile_horizon_is_never_echoed():
    a = stock_buy()
    a["horizon"] = "<script>alert(1)</script> & intraday"
    text = fmt(a)
    assert "script" not in text
    assert head(a) == f"BUY SBIN {DOT} 1 share"
    assert_well_formed(text)


def test_the_blocked_prefix_comes_before_the_action_and_the_product_stays_last():
    a = stock_buy()
    a["horizon"], a["risk_allowed"] = "swing", False
    assert head(a) == f"{BLOCKED}BUY SBIN {DOT} 1 share {DOT} delivery"


# ==========================================================================================
# trading_symbol
# ==========================================================================================
def _mahindra(**over: Any) -> dict[str, Any]:
    a = stock_buy()
    a.update(series_key="MM", trading_symbol="M&M", label="Mahindra & Mahindra")
    a.update(over)
    return a


def test_the_header_shows_trading_symbol_and_the_label_follows_on_line_two():
    lines = fmt(_mahindra()).split("\n")
    assert lines[0] == f"<b>BUY M&amp;M {DOT} 1 share {DOT} intraday</b>"
    assert lines[1] == "<i>Mahindra &amp; Mahindra</i>"


def test_the_label_line_is_dropped_when_the_label_repeats_the_shown_symbol():
    text = fmt(_mahindra(label="M&M"))
    assert "<i>" not in text
    assert text.split("\n")[0] == f"<b>BUY M&amp;M {DOT} 1 share {DOT} intraday</b>"


def test_a_label_that_repeats_only_the_series_key_is_still_shown():
    """The comparison is with the symbol actually shown (trading_symbol), not series_key."""
    lines = fmt(_mahindra(label="MM")).split("\n")
    assert lines[1] == "<i>MM</i>"


@pytest.mark.parametrize("trading_symbol", [None, "", "   ", "\n\t", [], {}, ["M&M"]])
def test_an_unusable_trading_symbol_falls_back_to_the_series_key(trading_symbol):
    a = stock_buy()
    a["trading_symbol"] = trading_symbol
    assert head(a) == f"BUY SBIN {DOT} 1 share {DOT} intraday"


def test_an_option_header_uses_trading_symbol_too():
    a = index_ce_live()
    a["trading_symbol"] = "NIFTYBANK"
    assert head(a) == f"BUY NIFTYBANK 26 NOV 52000 CE {DOT} 1 lot (30)"


def test_a_hostile_trading_symbol_is_escaped_collapsed_and_capped():
    a = stock_buy()
    a["trading_symbol"] = "<b>X</b>\n&" + "Y" * 500
    text = fmt(a)
    first = text.split("\n")[0]
    assert "<b>X</b>" not in first.removeprefix("<b>")
    assert "&lt;b&gt;X&lt;/b&gt; &amp;YYY" in first
    assert len(text.split("\n")) == 9
    assert_budget(text)
    assert_well_formed(text)


def test_the_missed_note_uses_trading_symbol_and_the_exit_line_does_too():
    tg = telegram()
    note = visible(tg.format_missed(_mahindra(), NOW))
    assert "BUY M&M" in note and "BUY MM" not in note
    exit_text = tg.format_exit(
        {"advice": "STOP", "position": stock_position(**_mahindra_pos())}, NOW
    )
    assert exit_text.split("\n")[0] == f"<b>EXIT: SELL M&amp;M {DOT} 1 share</b>"


def _mahindra_pos() -> dict[str, Any]:
    return {"series_key": "MM", "trading_symbol": "M&M", "label": "Mahindra & Mahindra"}


# ==========================================================================================
# the data line is fail-safe
# ==========================================================================================
@pytest.mark.parametrize(
    "delayed", [None, 0, 1, 0.0, 1.0, "true", "false", "", [], {}, "missing"], ids=repr
)
def test_the_data_line_is_n_a_unless_delayed_is_a_real_boolean(delayed):
    a = stock_buy()
    if delayed == "missing":
        del a["data"]["delayed"]
    else:
        a["data"]["delayed"] = delayed
    text = fmt(a)
    assert "Data n/a" in text.split("\n")
    assert "Data live" not in text and "delayed ~15m" not in text
    assert len(text.split("\n")) == 9
    assert_well_formed(text)


@pytest.mark.parametrize("data", [None, [], "dhan", 5, {}])
def test_a_missing_or_malformed_data_block_reads_n_a(data):
    a = stock_buy()
    a["data"] = data
    assert "Data n/a" in fmt(a).split("\n")


def test_only_the_two_booleans_say_anything_about_freshness():
    for delayed, line in ((True, "Data <b>delayed ~15m</b>"), (False, "Data live")):
        a = stock_buy()
        a["data"]["delayed"] = delayed
        a["data"]["source"] = "yahoo" if not delayed else "dhan"  # the source is never read
        assert line in fmt(a).split("\n")


def test_a_missing_flag_is_not_rescued_by_a_live_looking_source():
    a = stock_buy()
    del a["data"]["delayed"]
    a["data"]["source"] = "dhan"
    assert "Data n/a" in fmt(a).split("\n")


# ==========================================================================================
# characters that must never reach the phone
# ==========================================================================================
SURROGATES = [chr(0xD800), chr(0xDBFF), chr(0xDC00), chr(0xDFFF)]
INVISIBLE = [chr(0x061C), chr(0xFEFF), chr(0x200E), chr(0x200F), chr(0x202E), chr(0x2066)]
TEXT_FIELDS = [
    "label",
    "series_key",
    "trading_symbol",
    "strategy_label",
    "timeframe",
    "id",
    "confidence.label",
]


def _set(alert: dict[str, Any], path: str, value: Any) -> None:
    node = alert
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[key]
    node[leaf] = value


@pytest.mark.parametrize("path", TEXT_FIELDS)
@pytest.mark.parametrize("char", SURROGATES + INVISIBLE, ids=lambda c: f"U+{ord(c):04X}")
def test_surrogates_and_invisible_marks_in_a_field_are_removed(path, char):
    a = stock_buy()
    _set(a, path, f"ab{char}cd")
    text = fmt(a)
    assert char not in text
    text.encode("utf-8")  # a lone surrogate would raise here
    assert_budget(text)
    assert_well_formed(text)
    assert "abcd" in text  # the visible letters on both sides of the mark are joined


@pytest.mark.parametrize("char", SURROGATES)
def test_an_alert_with_a_surrogate_in_its_label_is_still_sendable(char):
    a = stock_buy()
    a["label"] = f"State{char} Bank"
    client, rec = make_client()
    result = client.send_message(fmt(a))
    assert (result.ok, result.kind) == (True, "ok")
    assert rec.calls == 1


@pytest.mark.parametrize("char", SURROGATES + INVISIBLE, ids=lambda c: f"U+{ord(c):04X}")
def test_exit_and_missed_strip_them_too(char):
    tg = telegram()
    p = stock_position(series_key=f"SB{char}IN")
    exit_text = tg.format_exit({"advice": f"ST{char}OP", "position": p}, NOW)
    a = stock_buy()
    a["series_key"] = f"SB{char}IN"
    missed = tg.format_missed(a, NOW)
    for text in (exit_text, missed):
        assert char not in text
        text.encode("utf-8")
    assert "EXIT: SELL SBIN" in exit_text and "Reason: STOP" in exit_text
    assert "SBIN" in visible(missed)


MARKS = st.lists(st.sampled_from(SURROGATES + INVISIBLE + list("ab &<>\n")), max_size=300).map(
    "".join
)


@given(label=MARKS, symbol=MARKS, strategy=MARKS)
@settings(max_examples=200, deadline=None, derandomize=True)
def test_no_mix_of_marks_surrogates_and_markup_breaks_the_message(label, symbol, strategy):
    a = stock_buy()
    a.update(label=label, trading_symbol=symbol, strategy_label=strategy)
    text = fmt(a)
    text.encode("utf-8")
    assert not any(c in text for c in SURROGATES + INVISIBLE)
    assert_budget(text)
    assert_well_formed(text)


# ==========================================================================================
# junk that is not text: booleans and astronomically large integers
# ==========================================================================================
@pytest.mark.parametrize("path", TEXT_FIELDS)
@pytest.mark.parametrize("flag", [True, False])
def test_a_boolean_in_a_text_field_is_never_printed_as_a_word(path, flag):
    """The strike already shows '?' for a bool. The symbol is the word the owner types into
    Dhan, and a YAML-parsed ticker such as NO or ON arrives as False or True."""
    a = stock_buy()
    _set(a, path, flag)
    text = fmt(a)
    assert not re.search(r"\b(True|False)\b", visible(text)), text
    assert_budget(text)
    assert_well_formed(text)


@pytest.mark.parametrize("flag", [True, False])
def test_a_boolean_trading_symbol_falls_back_to_the_series_key(flag):
    a = stock_buy()
    a["trading_symbol"] = flag
    assert head(a) == f"BUY SBIN {DOT} 1 share {DOT} intraday"


@pytest.mark.parametrize("flag", [True, False])
def test_a_boolean_symbol_never_reaches_the_exit_line_or_the_missed_note(flag):
    tg = telegram()
    exit_text = tg.format_exit(
        {"advice": "STOP", "position": stock_position(trading_symbol=flag, series_key=flag)}, NOW
    )
    a = stock_buy()
    a["trading_symbol"], a["series_key"] = flag, flag
    for text in (exit_text, tg.format_missed(a, NOW)):
        assert not re.search(r"\b(True|False)\b", visible(text)), text


HUGE = 10**5000  # beyond the 4300 digits Python will turn into text
TEXT_AND_KEY_FIELDS = [*TEXT_FIELDS, "side", "kind", "horizon", "strategy"]


@pytest.mark.parametrize("path", TEXT_AND_KEY_FIELDS)
def test_an_astronomically_large_integer_in_a_text_field_never_raises(path):
    """The formatters promise that a malformed payload never raises. int -> str fails for
    integers of more than 4300 digits (ValueError), and so did every text field."""
    a = stock_buy()
    _set(a, path, HUGE)
    text = fmt(a)
    assert_budget(text)
    assert_well_formed(text)


def test_an_astronomically_large_integer_never_breaks_exit_or_missed():
    tg = telegram()
    exit_text = tg.format_exit(
        {"advice": HUGE, "position": stock_position(series_key=HUGE, unrealized=HUGE)}, NOW
    )
    a = stock_buy()
    a["label"], a["series_key"], a["id"] = HUGE, HUGE, HUGE
    missed = tg.format_missed(a, NOW)
    assert len(exit_text.split("\n")) == 4
    assert "\n" not in missed
    for text in (exit_text, missed):
        assert_well_formed(text)


@pytest.mark.parametrize(
    ("score", "shown"),
    [(10**400, 100), (-(10**400), 0), (10**5000, 100), (-(10**5000), 0)],
    ids=["1e400", "minus-1e400", "1e5000", "minus-1e5000"],
)
def test_a_score_beyond_float_range_is_clamped_not_an_error(score, shown):
    """float(10**400) raises OverflowError; a clamp to 0..100 is the documented rule."""
    assert shown_confidence(score) == shown
    a = stock_buy()
    a["confidence"]["score"] = score
    assert f"Conf {shown} high" in fmt(a).split("\n")


# ==========================================================================================
# one confidence number
# ==========================================================================================
@pytest.mark.parametrize(
    ("score", "shown"),
    [
        (71.6, 71),
        (0, 0),
        (0.0, 0),
        (0.99, 0),
        (64.9, 64),
        (65, 65),
        (99.99, 99),
        (100, 100),
        (100.0, 100),
        (100.5, 100),
        (1e9, 100),
        (-0.1, 0),
        (-50, 0),
    ],
)
def test_shown_confidence_floors_and_clamps(score, shown):
    assert shown_confidence(score) == shown
    assert isinstance(shown_confidence(score), int)


@pytest.mark.parametrize(
    "score",
    [None, float("nan"), float("inf"), float("-inf"), True, False, "71.6", [71], {}, object()],
    ids=repr,
)
def test_shown_confidence_is_none_for_anything_that_is_not_a_finite_number(score):
    assert shown_confidence(score) is None


@given(a=st.floats(0, 100), b=st.floats(0, 100))
@settings(max_examples=200, deadline=None, derandomize=True)
def test_shown_confidence_never_decreases_with_the_score_and_stays_in_range(a, b):
    low, high = sorted((a, b))
    assert 0 <= shown_confidence(low) <= shown_confidence(high) <= 100
    assert shown_confidence(low) <= low


STRENGTH = st.floats(min_value=0, max_value=100, allow_nan=False)
BACKTEST = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "trades": st.integers(0, 80),
            "expectancy_r": st.floats(-1, 1, allow_nan=False),
            "profit_factor": st.floats(0, 4, allow_nan=False),
        }
    ),
)
LIVE = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "n": st.integers(0, 40),
            "hit_rate": st.floats(0, 100, allow_nan=False),
            "avg_r": st.floats(-2, 2, allow_nan=False),
        }
    ),
)


@given(strength=STRENGTH, backtest=BACKTEST, live=LIVE, regime=st.sampled_from([True, False, None]))
@settings(max_examples=400, deadline=None, derandomize=True)
def test_the_floored_number_always_agrees_with_the_desks_label(strength, backtest, live, regime):
    """Label thresholds are high >= 65 and low < 45. The label follows the stored one-decimal
    score, so the number a surface shows (its floor) can never sit on the other side."""
    result = confidence(strength, backtest, live, regime)
    score, label = result["score"], result["label"]
    assert score == round(score, 1)  # stored with one decimal
    number = shown_confidence(score)
    assert number is not None
    assert (label == "high") == (number >= 65)
    assert (label == "low") == (number < 45)
    assert (label == "medium") == (45 <= number < 65)


def test_a_score_just_under_a_threshold_is_stored_labelled_and_shown_consistently():
    """64.96 raw is stored as 65.0 and is high; 64.94 raw is stored as 64.9, medium, shows 64."""
    for raw, label, number in ((64.96, "high", 65), (64.94, "medium", 64), (44.96, "medium", 45)):
        strength = (raw - 32.5) / 0.35  # the other parts are neutral 50s: raw = 0.35 s + 32.5
        result = confidence(strength, None, None, None)
        assert (result["label"], shown_confidence(result["score"])) == (label, number)


# ==========================================================================================
# close() and send_message never raising
# ==========================================================================================
def test_a_closed_client_is_unavailable_and_sends_no_request():
    rec = Recorder()
    client = telegram().TelegramClient(TOKEN, CHAT, http=make_http(rec))
    assert client.available is True
    client.close()
    assert client.available is False
    assert "closed" in str(client.unavailable_reason).lower()
    result = client.send_message("hello")
    assert (result.ok, result.kind, result.message_id) == (False, "unavailable", None)
    assert result.detail
    assert rec.calls == 0
    client.close()  # idempotent
    assert client.send_message("again").kind == "unavailable"
    assert rec.calls == 0


def test_closing_an_unavailable_client_keeps_its_original_reason():
    client = telegram().TelegramClient("bad", CHAT, http=make_http(Recorder()))
    reason = client.unavailable_reason
    client.close()
    assert client.unavailable_reason == reason and "token" in str(reason).lower()


def test_close_leaves_an_injected_http_client_open():
    http = make_http(Recorder())
    telegram().TelegramClient(TOKEN, CHAT, http=http).close()
    assert not http.is_closed


class _Weird(Exception):
    pass


UNEXPECTED = {
    "RuntimeError": RuntimeError,
    "OSError": OSError,
    "ValueError": ValueError,
    "KeyError": KeyError,
    "UnicodeError": UnicodeError,
    "TypeError": TypeError,
    "ZeroDivisionError": ZeroDivisionError,
    "_Weird": _Weird,
}


@pytest.mark.parametrize("name", sorted(UNEXPECTED))
def test_an_unexpected_exception_from_the_transport_becomes_a_transient_result(name):
    marker = "SENTINEL-EXC-TEXT-7c1e"

    def boom(request: httpx.Request) -> httpx.Response:
        raise UNEXPECTED[name](f"{marker} while calling {request.url}")

    client, rec = make_client(boom)
    with capture_logs() as logs:
        result = client.send_message("hello")
    assert (result.ok, result.kind, result.message_id, result.retry_after) == (
        False,
        "transient",
        None,
        None,
    )
    assert result.detail == f"unexpected error: {name}"  # the class name only
    assert marker not in repr(result) and not leaked_forms(repr(result))
    blob = record_blob(logs.records)
    assert marker not in blob and not leaked_forms(blob)  # not in the warning it logs either
    assert rec.calls == 1


@pytest.mark.parametrize("text", [b"bytes", object(), 5, None, ["a"]], ids=repr)
def test_a_text_of_the_wrong_type_still_returns_a_result(text):
    client, _ = make_client()
    result = client.send_message(text)  # type: ignore[arg-type]
    assert result.ok in (True, False)
    assert isinstance(result.detail, str)


def test_a_failure_on_one_send_does_not_poison_the_next():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call dies")
        return ok_response(9)

    client, _ = make_client(flaky)
    assert client.send_message("a").kind == "transient"
    assert client.send_message("b").message_id == 9


# ==========================================================================================
# masked() and the registration rule
# ==========================================================================================
TG_TOKEN = "987654321:AAH-" + "k3Jd9Qw2LmZ7pRt5Vx8Yb1Nc4Hs6Fg0Ta"


def _load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token: str | None,
    chat: str | None = None,
) -> Secrets:
    for name in (
        "DHAN_CLIENT_ID",
        "DHAN_ACCESS_TOKEN",
        "DHAN_DATA_ACCESS_TOKEN",
        "DHAN_PIN",
        "DHAN_TOTP_SECRET",
        "ONELOT_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return Secrets.load(project_root=tmp_path)


@pytest.mark.parametrize(
    ("token", "shown"),
    [
        (None, None),
        ("", None),
        (TG_TOKEN, "set"),
        (TG_TOKEN + "\n", "set"),  # Secrets.load strips the value before judging it
        ("123456:" + "A" * 30, "set"),
        ("123456789:" + "A" * 29, "invalid"),  # one character short
        ("12345:" + "A" * 35, "invalid"),
        ("-1007766554433", "invalid"),  # a chat id in the token field
        ("abc", "invalid"),
        ("bot" + TG_TOKEN, "invalid"),
    ],
    ids=repr,
)
def test_masked_reports_the_token_as_none_set_or_invalid_and_never_a_fragment(
    token, shown, monkeypatch, tmp_path
):
    masked = _load(monkeypatch, tmp_path, token).masked()
    assert masked["telegram_bot_token"] == shown
    blob = repr(masked)
    for piece in (TG_TOKEN, TG_TOKEN[-4:], TG_TOKEN[-8:], TG_TOKEN.partition(":")[2]):
        assert piece not in blob
    if token and len(token.strip()) >= 8:
        assert token.strip()[-4:] not in blob


@pytest.mark.parametrize(("chat", "shown"), [(None, None), ("", None), ("-1009876543210", "set")])
def test_masked_reports_the_chat_id_as_set_or_none(chat, shown, monkeypatch, tmp_path):
    masked = _load(monkeypatch, tmp_path, TG_TOKEN, chat).masked()
    assert masked["telegram_chat_id"] == shown
    assert "-1009876543210" not in repr(masked)


def test_telegram_token_forms_has_nothing_for_a_value_without_a_colon():
    for value in ("", "-1007766554433", "4455667788", "AAH-" + "x" * 40, "plain words"):
        assert telegram_token_forms(value) == []


def test_telegram_token_forms_lists_the_three_spellings_of_a_colon_bearing_token():
    bot_id, _, secret = TG_TOKEN.partition(":")
    assert telegram_token_forms(TG_TOKEN) == [TG_TOKEN, f"{bot_id}%3A{secret}", secret]
    assert telegram_token_forms("700000001:abc") == ["700000001:abc"]  # short secret: whole only


BARE_SECRET = "AAH-" + "Rt6Hy7Ju8Ik9Ol0P" * 2
REGISTRATION_CASES = {
    "valid-token": (TG_TOKEN, 3),
    "padded-token": ("  " + TG_TOKEN + "\n", 3),
    "wrong-shape-with-colon": ("70003:Qw3rtyUiop9AsdfGhjk1ZxcvBnm2", 3),
    "short-secret-after-colon": ("700000001:abc", 1),
    "chat-id": ("-1007766554433", 0),
    "digits": ("4455667788", 0),
    "sixteen-digits": ("1234567890123456", 0),
    "bare-secret": (BARE_SECRET, 1),
    "short-word": ("fifteen-chars-x", 0),
    "empty": ("", 0),
}


@pytest.mark.parametrize("case", sorted(REGISTRATION_CASES))
def test_the_registration_rule_is_one_function(case):
    value, count = REGISTRATION_CASES[case]
    assert len(telegram_redaction_literals(value)) == count


@pytest.mark.parametrize("case", sorted(REGISTRATION_CASES))
def test_secrets_and_the_client_register_exactly_the_same_literals(case, monkeypatch, tmp_path):
    value, _count = REGISTRATION_CASES[case]
    from_secrets = set(_load(monkeypatch, tmp_path, value.strip()).secret_values())
    baseline = set(_load(monkeypatch, tmp_path, None).secret_values())
    secrets_literals = from_secrets - baseline
    redactor = get_redactor()
    before = set(redactor._literals)
    telegram().TelegramClient(value, CHAT, http=make_http(Recorder()))
    client_literals = set(redactor._literals) - before
    # the process-wide redactor may already hold some of them from an earlier test
    expected = set(telegram_redaction_literals(value))
    assert secrets_literals == expected
    assert client_literals <= expected
    for literal in expected:
        assert literal in redactor._literals
    line = f"update {value.strip()} offset 5"
    if not expected:
        assert redactor.scrub(line) == line  # numbers and ordinary text are untouched
