"""US-079 AC9: trades closed BREAKEVEN_STOP and LOCK_STOP (the stop and trail rule's two new
close reasons) are counted by the ledger and the scoreboard, listed by the book's view and
named in the exit alert, like every other close. Nothing in those places groups by reason,
so these tests pin that they take the new reasons without a crash and without a gap."""

from __future__ import annotations

from sqlalchemy import update

from onelot.paper.ledger import PaperLedger
from onelot.paper.manage import CLOSE_REASONS
from onelot.paper.view import scoreboard_view, slot_scoreboard_view
from onelot.store.schema import paper_trades

from .test_book_manage import at, bars, feed, opened
from .test_book_ratchet import WALK, closed_of, ratchet_alert
from .test_scoreboard import insert

NEW = ("BREAKEVEN_STOP", "LOCK_STOP")


def booked(engine) -> None:
    for k, (reason, net) in enumerate([("BREAKEVEN_STOP", 300), ("LOCK_STOP", 900),
                                       ("STOP", -500)]):  # fmt: skip
        insert(engine, f"a{k}", "microtrend", net, timeframe="3m", kind="INDEX")
        with engine.begin() as conn:
            conn.execute(
                update(paper_trades)
                .where(paper_trades.c.alert_id == f"a{k}")
                .values(close_reason=reason)
            )


def test_the_close_reasons_know_them() -> None:
    assert set(NEW) <= CLOSE_REASONS


def test_the_ledger_and_the_scoreboards_count_them(engine) -> None:
    booked(engine)
    ledger = PaperLedger(engine)
    totals = ledger.totals()
    assert (totals.trades, totals.wins, totals.losses) == (3, 2, 1)
    assert totals.net_paise == 300 + 900 - 500
    (row,) = scoreboard_view(ledger.by_strategy())
    assert (row["strategy"], row["trades"], row["wins"]) == ("microtrend", 3, 2)
    (slot,) = slot_scoreboard_view(ledger.by_slot())
    assert slot["trades"] == 3


def test_the_books_view_lists_a_trade_closed_by_the_rule(make_book, engine) -> None:
    rows = [WALK[0], WALK[1], (45250.0, 45260.0, 45220.0, 45230.0)]  # back to breakeven
    book = opened(make_book, ratchet_alert())
    events = book.step(at(12), feed(BANKNIFTY=bars(rows)))
    assert closed_of(events).detail["reason"] == "BREAKEVEN_STOP"
    (view,) = book.recent()
    assert (view["status"], view["close_reason"]) == ("CLOSED", "BREAKEVEN_STOP")
    assert view["levels"]["stage"] == "BREAKEVEN"
    assert view["levels"]["first_stop"] == "45110.00"
    assert PaperLedger(engine).totals().trades == 1
