"""A waiting trade on a series the desk no longer tracks (US-001 review round 1, MINOR-4).

Such a series gets no bars, so it has no data time and decision 3 ("the wall clock alone
never closes a trade") would leave the trade PENDING_FILL forever. The Desk tells the book
which series it tracks (`PaperBook.retire_untracked(series_keys, now)`, once after it is
built); a PENDING_FILL trade on any other series closes NO_FILL with detail
"series_untracked" once the WALL clock passes its deadline (+ the feed delay when delayed),
then or at any later step. This is the one documented exception to decision 3.
"""

from __future__ import annotations

import pytest

from onelot.store.schema import paper_events, paper_trades

from .conftest import THU, ist, make_alert, make_bars, table_rows


def rows(engine) -> dict[str, dict]:
    return {r["alert_id"]: r for r in table_rows(engine, paper_trades)}


def no_bars(key):
    return None


def test_past_its_deadline_a_waiting_trade_on_an_untracked_series_closes_no_fill(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(alert_id="gone"), ist(THU, 10, 5, 4))  # RELIANCE, expires 10:15:03

    events = book.retire_untracked({"BANKNIFTY", "SBIN"}, ist(THU, 10, 16))

    row = rows(engine)["gone"]
    assert (row["status"], row["close_reason"], row["entry_paise"]) == ("NO_FILL", "NO_FILL", None)
    assert (row["gross_paise"], row["costs_paise"], row["net_paise"]) == (0, 0, 0)
    (event,) = events
    assert (event.kind, event.ts) == ("NO_FILL", ist(THU, 10, 16))  # wall time: no data time
    assert event.detail == {
        "reason": "series_untracked",
        "series_key": "RELIANCE",
        "deadline": ist(THU, 10, 15, 3).isoformat(),
        "clock": "wall",
    }
    assert book.open_trades() == [] and book.status()["no_fill"] == 1


def test_before_its_deadline_it_waits_and_a_later_step_closes_it(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(alert_id="gone"), ist(THU, 10, 5, 4))
    assert book.retire_untracked(["SBIN"], ist(THU, 10, 10)) == []
    assert rows(engine)["gone"]["status"] == "PENDING_FILL"

    assert book.step(ist(THU, 10, 15, 3), no_bars) == []  # AT the deadline: not past it
    events = book.step(ist(THU, 10, 15, 4), no_bars)
    assert [(e.kind, e.detail["reason"]) for e in events] == [("NO_FILL", "series_untracked")]


@pytest.mark.parametrize(("now", "status"), [((10, 30), "PENDING_FILL"), ((10, 31), "NO_FILL")])
def test_a_delayed_trade_gets_its_feed_delay_too(make_book, engine, now, status):
    book = make_book()  # feed_delay_minutes 15: deadline 10:15:03 + 15 min = 10:30:03
    book.on_alert(make_alert(alert_id="late", source="yahoo"), ist(THU, 10, 5, 4))
    book.retire_untracked({"SBIN"}, ist(THU, *now))
    assert rows(engine)["late"]["status"] == status


def test_a_tracked_series_is_never_closed_by_the_wall_clock(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(alert_id="kept"), ist(THU, 10, 5, 4))
    book.retire_untracked({"RELIANCE"}, ist(THU, 14, 0))
    book.step(ist(THU, 14, 0), no_bars)  # no data: decision 3 holds, it waits
    assert rows(engine)["kept"]["status"] == "PENDING_FILL"


def test_without_the_desks_series_list_nothing_is_retired(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(alert_id="kept"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 14, 0), no_bars)
    assert rows(engine)["kept"]["status"] == "PENDING_FILL"


def test_an_open_trade_is_not_touched(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(alert_id="open1"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), lambda key: make_bars(ist(THU, 10, 5), [1000.0] * 3))
    assert book.retire_untracked(set(), ist(THU, 14, 0)) == []
    assert rows(engine)["open1"]["status"] == "OPEN"
    assert [e["kind"] for e in table_rows(engine, paper_events)] == ["OPENED", "FILLED"]


def test_a_naive_now_is_rejected(make_book):
    import datetime as dt

    with pytest.raises(ValueError, match="timezone-aware"):
        make_book().retire_untracked({"SBIN"}, dt.datetime(2026, 10, 29, 10, 16))
