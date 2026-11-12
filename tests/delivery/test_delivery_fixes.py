"""US-067, the lead's rulings after the review: no stall, isolation, the cost of a poll, late.

Drives `DeliveryDesk` with the tester's world (`tests/acceptance/test_us067_tester_desk.py`):
SBIN falls 10% on L3 (entry), is bought at L4's open and closes at its stop on L6; ITC falls
10% on L3, is bought at L4's open and closes above its SMA20 on L5.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import event

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "acceptance"))
sys.path.insert(0, str(HERE))

from delivery_helpers import session_bars  # noqa: E402
from test_us067_tester_desk import FULL, STORE, L, World, at  # noqa: E402

from onelot.clock import IST, TradingCalendar  # noqa: E402
from onelot.delivery.fills import Fill, decide  # noqa: E402


def cut(frame: pd.DataFrame, day: dt.date, after: str) -> pd.DataFrame:
    """`frame` without the bars of `day` after `after` (a day that stops early)."""
    h, m = map(int, after.split(":"))
    idx = frame.index
    return frame[~((idx.date == day) & (idx.time > dt.time(h, m)))]


# ── no stall (fix 1) ────────────────────────────────────────────────────────────────────────


def test_a_day_that_stops_early_is_read_for_exits_once_a_later_session_is_complete(tmp_path):
    """SBIN's L6 (its stop day) stops at 14:00, a weekday in a year the calendar does not
    cover (2025-10-21 style). At L6's close nothing is read; once L7 is complete, L6 is read as
    it is for the exit (the stop), with a warning on the card and on the trade, and L7 is read
    too: evaluation moves on."""
    full = dict(FULL, SBIN=cut(FULL["SBIN"], L[6], "14:00"))
    w = World(tmp_path, full)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    for i in (4, 5, 6):
        w.poll(at(L[i], "15:45"))
    sbin = w.trades()["SBIN"]
    assert sbin.status == "OPEN"  # L6 incomplete and nothing after it yet
    assert "incomplete (last bar 14:00)" in w.desk.notes["SBIN"]
    w.poll(at(L[7], "15:45"))
    sbin = w.trades()["SBIN"]
    assert sbin.exit_signal_date == L[6] and sbin.exit_reason == "STOP"
    assert f"{L[6].isoformat()} read from incomplete data" in (sbin.warning or "")
    assert w.desk.book.last_evaluated("SBIN") == L[7]
    evaluation = w.desk.book.evaluation("SBIN", L[6])
    assert evaluation["whole"] is False and "entry_blocked" in evaluation
    # the exit was given after L7's open: it sells at the first bar after it, late
    w.poll(at(L[8], "09:20"))
    sbin = w.trades()["SBIN"]
    assert sbin.status == "CLOSED" and sbin.exit_late and sbin.exit_ts == at(L[8], "09:15")


def test_a_short_day_raises_no_entry_and_the_sessions_after_it_are_read(tmp_path):
    """ITC's signal day L3 stops at 14:00: no entry from it, L4 is read normally."""
    full = dict(FULL, ITC=cut(FULL["ITC"], L[3], "14:00"))
    w = World(tmp_path, full)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.poll(at(L[4], "15:45"))
    # L3 read as it is (383 at 14:00, not 8% below the high): L4 is the first deep close
    assert w.trades()["ITC"].signal_date == L[4] and w.trades()["SBIN"].signal_date == L[3]
    assert w.desk.book.last_evaluated("ITC") == L[4]
    assert w.desk.book.evaluation("ITC", L[3])["entry_blocked"].endswith(
        "read from incomplete data"
    )


def special(frame: pd.DataFrame, day: dt.date, open_: float, close: float) -> pd.DataFrame:
    """`frame` with `day` as an afternoon special session, bars 13:45 to 14:44."""
    rest = frame[frame.index.date != day]
    return pd.concat(
        [rest, session_bars(day, open_, close, last="14:44", first="13:45")]
    ).sort_index()


def test_an_afternoon_special_session_on_a_calendar_holiday_is_whole_and_raises_its_entry(
    tmp_path,
):
    """L3 is a listed holiday with a Muhurat session 13:45 to 14:44 for every series: it is
    complete the next day, and its entry signal is raised then."""
    cal = TradingCalendar({L[3]: "Diwali"}, verified=True)
    full = {
        "SBIN": special(FULL["SBIN"], L[3], 215.0, 194.4),
        "ITC": special(FULL["ITC"], L[3], 415.0, 374.4),
        "NIFTY": special(FULL["NIFTY"], L[3], 25030.0, 25035.0),
    }
    w = World(tmp_path, full, cal=cal)
    w.start(at(L[3], "12:00"))
    w.poll(at(L[3], "15:45"))
    assert w.trades() == {}  # a special session is complete only the next day
    w.poll(at(L[4], "00:30"))
    sbin = w.trades()["SBIN"]
    assert sbin.signal_date == L[3] and sbin.status == "PENDING_ENTRY"
    assert len(w.notifier.sent) == 2


def test_a_missing_nifty_bar_means_no_entry_that_day_and_the_day_is_evaluated(tmp_path):
    nifty = FULL["NIFTY"]
    w = World(tmp_path, dict(FULL, NIFTY=nifty[nifty.index.date != L[3]]))
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.poll(at(L[4], "15:45"))
    assert w.trades() == {}
    reading = w.desk.book.evaluation("SBIN", L[3])
    assert reading["signal"] is False and reading["nifty_above_sma200"] is False
    assert w.desk.book.last_evaluated("SBIN") == L[4]


def test_a_bad_price_long_ago_is_left_out_with_a_note_and_the_rule_still_enters(tmp_path):
    st = dict(STORE)
    s = st["SBIN"].copy()
    s.iloc[10, s.columns.get_loc("close")] = np.nan
    s.iloc[11, s.columns.get_loc("open")] = -1.0
    st["SBIN"] = s
    w = World(tmp_path, store=st)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert w.trades()["SBIN"].status == "PENDING_ENTRY"
    assert "left out" in w.desk.notes["SBIN"]
    assert np.isfinite(w.desk.card("SBIN")["reading"]["sma200"])


# ── isolation (fix 2) ───────────────────────────────────────────────────────────────────────


def test_one_stock_failing_never_stops_another_stocks_fill_and_evaluation(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))  # both entries raised
    w.start(at(L[4], "09:00"))
    desk = w.desk
    real_fill, real_eval = desk._fill_one, desk._evaluate_session

    def fill(now, t, hist, bars):
        if t.series_key == "SBIN":
            raise RuntimeError("SBIN's bars are broken")
        return real_fill(now, t, hist, bars)

    def evaluate(now, stock, *a, **k):
        if stock.series_key == "SBIN":
            raise RuntimeError("SBIN's evaluation is broken")
        return real_eval(now, stock, *a, **k)

    monkeypatch.setattr(desk, "_fill_one", fill)
    monkeypatch.setattr(desk, "_evaluate_session", evaluate)
    for when in (at(L[4], "09:20"), at(L[4], "15:45"), at(L[5], "15:45")):
        w.poll(when)
    trades = w.trades()
    assert trades["SBIN"].status == "PENDING_ENTRY"  # never filled: its fills fail
    assert trades["ITC"].status == "EXIT_PENDING"  # filled at L4, exit signal on L5
    assert desk.errors >= 3


def test_a_failing_card_never_stops_the_other_cards(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    real = w.desk._rebuild_one

    def one(now, stock, *a, **k):
        if stock.series_key == "SBIN":
            raise ValueError("bad card")
        return real(now, stock, *a, **k)

    monkeypatch.setattr(w.desk, "_rebuild_one", one)
    w.poll(at(L[3], "15:45"))
    assert w.desk.card("SBIN")["note"] == "this card failed: see the log"
    assert w.desk.card("ITC")["reading"]["signal"] is True


# ── the cost of a poll (fix 4) ──────────────────────────────────────────────────────────────


def test_polls_with_unchanged_bars_read_nothing_and_rebuild_nothing(tmp_path):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.poll(at(L[3], "15:50"))  # the bars stopped at 15:14: nothing new
    statements: list[str] = []
    event.listen(w.engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
    joins, rebuilds = w.desk.calls["join_live"], w.desk.calls["rebuild"]
    for minute in range(51, 56):
        w.poll(at(L[3], f"15:{minute}"))
    assert statements == []
    assert w.desk.calls["join_live"] == joins and w.desk.calls["rebuild"] == rebuilds


def test_join_live_runs_at_most_once_per_series_while_the_bars_do_not_change(tmp_path):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    for minute in (45, 46, 47, 48):
        w.poll(at(L[3], f"15:{minute}"))
    assert w.desk.calls["join_live"] == 3  # SBIN, ITC, NIFTY once each


# ── late (fix 7) ────────────────────────────────────────────────────────────────────────────


def test_a_fill_bar_more_than_two_minutes_after_the_opening_minute_is_late():
    d0, d1 = L[1], L[2]
    bars = pd.concat(
        [session_bars(d0, 100.0, 101.0), session_bars(d1, 102.0, 103.0, first="09:20")]
    )
    opening = lambda day: dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)  # noqa: E731
    got = decide(bars, d0, at(d0, "08:00"), lambda d: False, may_miss=True, opening=opening)
    assert isinstance(got, Fill) and got.late and got.ts == at(d1, "09:20")
    on_time = pd.concat(
        [session_bars(d0, 100.0, 101.0), session_bars(d1, 102.0, 103.0, first="09:17")]
    )
    got = decide(on_time, d0, at(d0, "08:00"), lambda d: False, may_miss=True, opening=opening)
    assert isinstance(got, Fill) and not got.late


@pytest.mark.parametrize("depth", [0.06, 0.10])
def test_the_alert_text_takes_its_figures_from_the_settings(tmp_path, depth):
    from onelot.delivery.rule import PullbackParams

    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    t = w.trades()["SBIN"]
    stock = w.desk.stocks["SBIN"]
    w.desk.stocks["SBIN"] = type(stock)(**{**stock.__dict__, "params": PullbackParams(depth=depth)})
    assert f"is {depth * 100:g}% or more below" in w.desk.alert_text(t, "entry")[1]
