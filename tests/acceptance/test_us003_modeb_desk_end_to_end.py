"""US-003 mode B: the formatters on payloads raised by a REAL Desk.

Mode A built its alert dicts by hand from Desk._raise_signal. Here a real Desk (the offline
harness of the US-028 tests: real trackers, real economics, a fake notifier) raises the alert,
and the formatter reads the Notification.payload the desk really hands to the notifier. Covered:
a Dhan-served live stock alert, a Yahoo-served (delayed) stock alert, an index alert with the
synthetic premium, an index alert with a live-chain premium, risk-blocked alerts (also delayed),
and the EXIT payloads {advice, position} that _manage_positions produces for a stock and an
index option.

Expected texts are rebuilt here from the alert's own numbers with independent code (Decimal
half-up for prices, the floor for the confidence number, a month table), never by calling the
formatter again.
"""

from __future__ import annotations

import datetime as dt
import html
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import test_us028_desk_delay_label as harness  # the vetted offline Desk harness
from us003_helpers import (
    DOT,
    assert_budget,
    assert_well_formed,
    header_of,
    install_network_guard,
    telegram,
    visible,
)

from onelot.clock import now_ist
from onelot.config import AppConfig
from onelot.desk.desk import DELAYED_TITLE_MARKER
from onelot.notify.base import NotificationLevel

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
BLOCKED = "[BLOCKED by risk] "


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the desk harness must stay offline: {attempts}"


@pytest.fixture
def app_cfg() -> AppConfig:
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database
    from onelot.store import init_database

    database = init_database(f"sqlite:///{tmp_path / 'us003_desk.db'}")
    use_database(database)
    yield database
    database.dispose()
    get_database.__globals__["_db"] = None


# ---- independent expectations ------------------------------------------------------------
def money(value: float) -> str:
    q = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f}"


def product(alert: dict[str, Any]) -> str:
    """The stock product suffix of line 1 (Manager decision, review round 1)."""
    if alert["kind"] != "STOCK":
        return ""
    if alert["side"] == "SELL":
        return f" {DOT} intraday short"
    return {"intraday": f" {DOT} intraday", "swing": f" {DOT} delivery"}.get(alert["horizon"], "")


def shown_confidence(score: float) -> int:
    """The number both surfaces show: the score floored and clamped to 0..100."""
    return int(min(max(Decimal(str(score)), Decimal(0)), Decimal(100)))


def whole(value: float) -> str:
    q = Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f"{'+' if q > 0 else '-' if q < 0 else ''}{abs(q):,.0f}"


def dd_mmm(iso: str) -> str:
    day = dt.date.fromisoformat(iso[:10])
    return f"{day.day:02d} {MONTHS[day.month - 1]}"


def strike(value: float) -> str:
    text = f"{Decimal(str(value)).normalize():f}"
    return text


def levels(head: str, stop: float, target: float | None) -> str:
    shown = "trail" if target is None else money(target)
    return f"{head} {money(stop)} {DOT} Target {shown}"


def expected_lines(alert: dict[str, Any], now: dt.datetime) -> list[str]:
    """The lines below the label, rebuilt from the real alert dict."""
    eco = alert["economics"]
    created = dt.datetime.fromisoformat(alert["created_at"])
    expires = dt.datetime.fromisoformat(alert["expires_at"])
    left = int((expires - now).total_seconds() // 60)
    tag = "mkt" if alert["kind"] == "STOCK" else ("est." if eco["synthetic"] else "ask")
    out = [f"Entry {money(eco['entry_price'])} {tag}"]
    if alert["kind"] == "INDEX":
        out.append(levels("Underlying Stop", eco["underlying_stop"], eco["underlying_target"]))
    out.append(levels("Stop", eco["stop_price"], eco["target_price"]))
    out.append(html.escape(f"{alert['strategy_label']} {alert['timeframe']}", quote=False))
    shown = shown_confidence(alert["confidence"]["score"])
    out.append(f"Conf {shown} {html.escape(alert['confidence']['label'], quote=False)}")
    out.append(
        f"Valid till {expires:%H:%M} IST ({left}m left)"
        if left > 0
        else f"Valid till {expires:%H:%M} IST (expired)"
    )
    out.append("Data <b>delayed ~15m</b>" if alert["data"]["delayed"] else "Data live")
    out.append(f"Alert {created:%H:%M:%S} IST {DOT} id <code>{alert['id'][:6]}</code>")
    return out


def sent_alert(notifier: Any) -> Any:
    signals = [n for n in notifier.sent if n.level is NotificationLevel.SIGNAL]
    assert signals, "the desk sent no SIGNAL notification"
    return signals[-1]


def clock_for(alert: dict[str, Any]) -> dt.datetime:
    return dt.datetime.fromisoformat(alert["created_at"]) + dt.timedelta(seconds=45)


def check_alert_text(notification: Any, expect_header: str, expect_label: str | None) -> str:
    alert = notification.payload
    now = clock_for(alert)
    text = telegram().format_alert(alert, now)
    lines = text.split("\n")
    assert_budget(text)
    assert_well_formed(text)
    assert visible(header_of(text)).endswith(expect_header), lines[0]
    if expect_label is None:
        assert not lines[1].startswith("<i>")
    else:
        assert lines[1] == f"<i>{html.escape(expect_label, quote=False)}</i>"
    assert lines[-len(expected_lines(alert, now)) :] == expected_lines(alert, now)
    # nothing the desk wrote for the in-app view leaks into the phone message
    assert alert["text"].split("\n")[0] not in text
    for reason in alert["reasons"]:
        assert reason not in text
    return text


def make(app_cfg, db, monkeypatch, serves):
    return harness.make_desk(app_cfg, db, monkeypatch, serves=serves)


class FakeChain:
    """A live Dhan option chain with one usable row per side (what ChainCache holds)."""

    def __init__(self, expiry: str) -> None:
        self.expiry = expiry
        self.rows = [
            SimpleNamespace(
                opt_type=opt,
                strike=strike_value,
                ltp=ask - 0.5,
                bid=ask - 1.0,
                ask=ask,
                delta=0.5 if opt == "CE" else -0.5,
                iv=0.14,
                oi=125000,
                security_id="55012",
            )
            for opt, strike_value, ask in (("CE", 54000.0, 212.35), ("PE", 54000.0, 198.7))
        ]


# ==========================================================================================
# stock alerts
# ==========================================================================================
def test_a_dhan_served_stock_alert_reads_live(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    alert = harness.raise_alert(desk, "SBIN")
    notification = sent_alert(notifier)
    assert notification.payload is alert  # the formatter reads exactly what the desk sent
    assert alert["data"]["delayed"] is False
    assert alert["horizon"] == "swing"  # the harness desk holds stocks for swing: delivery
    header = f"BUY SBIN {DOT} 1 share{product(alert)}"
    assert header == f"BUY SBIN {DOT} 1 share {DOT} delivery"
    text = check_alert_text(notification, header, "State Bank of India")
    assert "Data live" in text.split("\n")
    assert "delayed" not in text


def test_a_yahoo_served_stock_alert_reads_delayed_on_the_desks_own_flag(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"BANKNIFTY"})  # Yahoo serves SBIN
    alert = harness.raise_alert(desk, "SBIN")
    notification = sent_alert(notifier)
    assert alert["data"]["source"] == "yahoo" and alert["data"]["delayed"] is True
    header = f"BUY SBIN {DOT} 1 share{product(alert)}"
    text = check_alert_text(notification, header, "State Bank of India")
    assert "Data <b>delayed ~15m</b>" in text.split("\n")
    # the in-app title and the phone message carry the same flag
    assert notification.title.startswith(DELAYED_TITLE_MARKER)


def test_the_phone_flag_follows_data_delayed_not_the_source_name_on_a_real_alert(
    app_cfg, db, monkeypatch
):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"BANKNIFTY"})
    alert = harness.raise_alert(desk, "SBIN")
    flipped = {**alert, "data": {**alert["data"], "delayed": False}}  # source still "yahoo"
    text = telegram().format_alert(flipped, clock_for(alert))
    assert "Data live" in text.split("\n")


# ==========================================================================================
# index alerts
# ==========================================================================================
def test_an_index_alert_with_the_synthetic_premium_reads_est(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    alert = harness.raise_alert(desk, "BANKNIFTY")
    option = alert["economics"]["option"]
    assert alert["economics"]["synthetic"] is True
    header = f"BUY BANKNIFTY {dd_mmm(option['expiry'])} {strike(option['strike'])} CE"
    text = check_alert_text(
        sent_alert(notifier), f"{header} {DOT} 1 lot ({option['lot']})", "NIFTY BANK"
    )
    assert any(ln.startswith("Entry ") and ln.endswith(" est.") for ln in text.split("\n"))
    assert "BANKNIFTY_OPT" not in text


def test_an_index_alert_with_a_live_chain_premium_reads_ask(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    monkeypatch.setattr(desk, "_live_chain", lambda tracker, now: FakeChain("2027-01-28"))
    alert = harness.raise_alert(desk, "BANKNIFTY")
    eco = alert["economics"]
    assert eco["synthetic"] is False and eco["option"]["ask"] == 212.35
    text = check_alert_text(
        sent_alert(notifier),
        f"BUY BANKNIFTY 28 JAN {strike(eco['option']['strike'])} CE {DOT} 1 lot (30)",
        "NIFTY BANK",
    )
    assert "Entry 212.35 ask" in text.split("\n")


# ==========================================================================================
# no risk-blocked alerts (owner feedback OF-5, 2026-11-07): a governor that says no blocks
# nothing on the desk, so the phone never reads "[BLOCKED by risk]" for a desk alert. The
# formatter's own handling of risk_allowed False stays covered in tests/notify.
# ==========================================================================================
def _block_risk(desk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desk.governor, "may_open", lambda now: (False, "daily loss limit reached"))


def test_a_governor_that_says_no_leaves_the_alert_and_its_phone_text_unmarked(
    app_cfg, db, monkeypatch
):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    _block_risk(desk, monkeypatch)
    alert = harness.raise_alert(desk, "SBIN")
    notification = sent_alert(notifier)
    assert alert["risk_allowed"] is True and alert["risk_reason"] is None
    assert not notification.title.startswith(BLOCKED)
    now = clock_for(alert)
    text = telegram().format_alert(alert, now)
    lines = text.split("\n")
    assert visible(lines[0]) == f"BUY SBIN {DOT} 1 share{product(alert)}"
    assert lines[2:] == expected_lines(alert, now)
    assert "BLOCKED" not in text and "daily loss limit" not in text
    assert_budget(text)
    assert_well_formed(text)


def test_a_delayed_alert_keeps_only_the_delay_marker_when_the_governor_says_no(
    app_cfg, db, monkeypatch
):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"BANKNIFTY"})
    _block_risk(desk, monkeypatch)
    alert = harness.raise_alert(desk, "SBIN")
    notification = sent_alert(notifier)
    assert notification.title.startswith(DELAYED_TITLE_MARKER)
    text = telegram().format_alert(alert, clock_for(alert))
    assert visible(header_of(text)).startswith("BUY SBIN")
    assert "Data <b>delayed ~15m</b>" in text.split("\n")
    assert_budget(text)
    assert_well_formed(text)


def test_an_index_alert_keeps_the_contract_in_the_header_when_the_governor_says_no(
    app_cfg, db, monkeypatch
):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    _block_risk(desk, monkeypatch)
    alert = harness.raise_alert(desk, "BANKNIFTY")
    option = alert["economics"]["option"]
    text = telegram().format_alert(alert, clock_for(alert))
    want = f"BUY BANKNIFTY {dd_mmm(option['expiry'])} {strike(option['strike'])} CE"
    assert visible(header_of(text)).startswith(want)
    assert_budget(text)
    assert_well_formed(text)


# ==========================================================================================
# one alert, two surfaces: the confidence number must agree
# ==========================================================================================
@pytest.mark.parametrize(
    "score", [0.5, 1.5, 2.5, 56.5, 57.5, 72.5, 71.6, 44.9, 64.9, 65.0, 99.9, 100.0, 0.0]
)
def test_the_phone_and_the_desk_title_show_the_same_floored_confidence_number(
    score, app_cfg, db, monkeypatch
):
    """One helper (notify.base.shown_confidence) feeds the desk SIGNAL title and the phone
    line: the score floored, so 56.5 reads 56 on both and 64.9 never reads 65."""
    import re

    monkeypatch.setattr(
        "onelot.desk.desk.confidence",
        lambda *args, **kwargs: {"score": score, "label": "medium", "parts": {}},
    )
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    alert = harness.raise_alert(desk, "SBIN")
    title_number = re.search(r"conf (\d+)", sent_alert(notifier).title).group(1)
    phone_line = next(
        ln
        for ln in telegram().format_alert(alert, clock_for(alert)).split("\n")
        if ln.startswith("Conf ")
    )
    assert phone_line.split()[1] == title_number == str(shown_confidence(score))


# ==========================================================================================
# the missed note for a real alert
# ==========================================================================================
def test_the_missed_note_for_a_real_alert_is_one_line_with_symbol_contract_and_id(
    app_cfg, db, monkeypatch
):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    stock = harness.raise_alert(desk, "SBIN")
    index = harness.raise_alert(desk, "BANKNIFTY")
    late = dt.datetime.fromisoformat(index["expires_at"]) + dt.timedelta(minutes=30)
    tg = telegram()
    note = tg.format_missed(stock, late)
    assert "\n" not in note and "missed" in note.lower()
    assert "SBIN" in visible(note) and stock["id"][:6] in visible(note)
    option = index["economics"]["option"]
    note = tg.format_missed(index, late)
    assert "\n" not in note
    shown = visible(note)
    assert f"BANKNIFTY {dd_mmm(option['expiry'])} {strike(option['strike'])} CE" in shown
    assert index["id"][:6] in shown
    assert_budget(note, max_lines=1)
    assert_well_formed(note)


# ==========================================================================================
# EXIT payloads from _manage_positions
# ==========================================================================================
def raise_short_alert(d: Any, key: str) -> dict[str, Any]:
    """The harness's raise_alert, for a SHORT verdict (stop above the close)."""
    from onelot.desk.tracker import Verdict

    t = d.trackers[key]
    close = float(t.bars["close"].iloc[-1])
    ts = t.bars.index[-1].to_pydatetime()
    verdict = Verdict("short", "test", ts, close * 1.005, close, 60.0, ["test"], True)
    d._raise_signal(t, t.slots[0], verdict, now_ist())
    alerts = [s for s in d.signals.values() if s["series_key"] == key]
    assert len(alerts) == 1, f"harness: no short alert was raised for {key} ({t.last_error})"
    return alerts[0]


def _take_and_trigger_stop(desk: Any, notifier: Any, key: str, short: bool = False) -> Any:
    alert = raise_short_alert(desk, key) if short else harness.raise_alert(desk, key)
    reply = desk.acknowledge(alert["id"], "TAKE")
    assert reply["accepted"], reply
    position = desk.positions[reply["position_id"]]
    # move the stop through the market so the next poll advises STOP: above the price for a
    # long, below it for a short
    entry = float(position["entry_underlying"])
    position["stop_underlying"] = entry * (0.1 if short else 10)
    now = now_ist()
    desk._manage_positions(now, harness.OpenClock().current_window(now))
    exits = [n for n in notifier.sent if n.level is NotificationLevel.EXIT]
    assert exits, "the desk raised no EXIT notification"
    return exits[-1], now


def test_the_exit_payload_of_a_stock_position_formats_to_four_lines(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    notification, now = _take_and_trigger_stop(desk, notifier, "SBIN")
    payload = notification.payload
    assert set(payload) == {"advice", "position"} and payload["advice"] == "STOP"
    text = telegram().format_exit(payload, now)
    lines = text.split("\n")
    assert len(lines) == 4
    assert lines[0] == f"<b>EXIT: SELL SBIN {DOT} 1 share</b>"  # a long stock is closed by a SELL
    assert lines[1] == "Reason: STOP"
    assert lines[2] == f"Est. P&amp;L {whole(payload['position']['unrealized'])}"
    assert lines[3] == f"Time {now:%H:%M:%S} IST"
    assert_budget(text, max_lines=4)
    assert_well_formed(text)


def test_the_exit_payload_of_an_index_option_names_the_contract(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    notification, now = _take_and_trigger_stop(desk, notifier, "BANKNIFTY")
    position = notification.payload["position"]
    option = position["option"]
    text = telegram().format_exit(notification.payload, now)
    lines = text.split("\n")
    assert len(lines) == 4
    want = f"EXIT: SELL BANKNIFTY {dd_mmm(option['expiry'])} {strike(option['strike'])} CE"
    assert visible(lines[0]) == f"{want} {DOT} 1 lot ({option['lot']})", lines[0]  # sell the call
    assert lines[1] == "Reason: STOP"
    assert lines[2] == f"Est. P&amp;L {whole(position['unrealized'])}"
    assert_budget(text, max_lines=4)
    assert_well_formed(text)
    assert "BANKNIFTY_OPT" not in text


def test_the_exit_payload_of_a_short_stock_says_buy_to_cover(app_cfg, db, monkeypatch):
    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    notification, now = _take_and_trigger_stop(desk, notifier, "SBIN", short=True)
    position = notification.payload["position"]
    assert position["side"] == "SELL" and position["direction"] == "short"
    assert notification.payload["advice"] == "STOP"
    lines = telegram().format_exit(notification.payload, now).split("\n")
    assert len(lines) == 4
    assert lines[0] == f"<b>EXIT: BUY SBIN {DOT} 1 share {DOT} cover short</b>"
    assert lines[2] == f"Est. P&amp;L {whole(position['unrealized'])}"


def test_the_exit_payload_is_not_changed_by_formatting_it(app_cfg, db, monkeypatch):
    import copy

    desk, notifier, _, _ = make(app_cfg, db, monkeypatch, {"SBIN", "BANKNIFTY"})
    notification, now = _take_and_trigger_stop(desk, notifier, "SBIN")
    before = copy.deepcopy(notification.payload)
    telegram().format_exit(notification.payload, now)
    assert notification.payload == before
