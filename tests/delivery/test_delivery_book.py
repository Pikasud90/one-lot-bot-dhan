"""US-067 D3 to D6, D12 and D15: the delivery book and where its fills come from."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delivery_helpers import at, bars_of  # noqa: E402

from onelot.config import CostConfig  # noqa: E402
from onelot.delivery.book import (  # noqa: E402
    CLOSED,
    EXIT_PENDING,
    MISSED,
    OPEN,
    PENDING_ENTRY,
    DeliveryBook,
    NewEntry,
)
from onelot.delivery.fills import Fill, Missed, decide, gap_warning  # noqa: E402
from onelot.delivery.money import delivery_model, depository_paise  # noqa: E402
from onelot.paper.ledger import Slot  # noqa: E402
from onelot.store.schema import delivery_trades  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
D0, D1, D2, D3 = (dt.date(2026, 11, 16), dt.date(2026, 11, 17), dt.date(2026, 11, 18),
                  dt.date(2026, 11, 19))  # fmt: skip
NOW = at(D0, "15:40")


@pytest.fixture
def costs() -> CostConfig:
    return CostConfig.load(ROOT / "config" / "costs.yaml")


@pytest.fixture
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'd.db'}")


@pytest.fixture
def book(engine, costs) -> DeliveryBook:
    return DeliveryBook(engine, delivery_model(costs), depository_paise(costs))


def entry(qty: int = 10, close: float = 1000.0) -> NewEntry:
    return NewEntry("SBIN", close, qty, {"evidence": "e", "unit_inr": 10000})


def never(_: dt.date) -> bool:
    return False


def always(_: dt.date) -> bool:
    return True


# ── where a fill comes from (D3, D5) ────────────────────────────────────────────────────────


BARS = bars_of({D0: (990.0, 1000.0), D1: (1005.0, 1010.0), D2: (1020.0, 1030.0)})


def test_d3_running_at_the_open_fills_at_the_first_bar_of_the_next_session():
    f = decide(BARS, D0, at(D0, "09:00"), never, may_miss=True)
    assert f == Fill(at(D1, "09:15"), D1, 1005.0, False)


def test_d3_a_start_during_the_next_session_fills_at_the_first_bar_after_the_start_late():
    f = decide(BARS, D0, at(D1, "10:00"), never, may_miss=True)
    assert isinstance(f, Fill) and f.late and f.ts == at(D1, "10:00") and f.session == D1
    assert f.open_ == pytest.approx(float(BARS.loc[at(D1, "10:00"), "open"]))


def test_d3_a_start_after_the_next_session_misses_the_entry():
    assert decide(BARS, D0, at(D2, "09:00"), never, may_miss=True) == Missed(D1)


def test_d3_started_after_the_last_bar_of_a_session_that_is_complete_misses():
    bars = bars_of({D0: (990.0, 1000.0), D1: (1005.0, 1010.0)})
    assert decide(bars, D0, at(D1, "15:20"), never, may_miss=True) is None  # maybe more bars
    assert decide(bars, D0, at(D1, "15:20"), always, may_miss=True) == Missed(D1)


def test_d3_no_bar_of_a_later_session_yet_waits():
    bars = bars_of({D0: (990.0, 1000.0)})
    assert decide(bars, D0, at(D0, "09:00"), always, may_miss=True) is None
    assert decide(None, D0, at(D0, "09:00"), always, may_miss=True) is None


def test_d3_the_next_session_is_read_from_the_bars_so_a_special_session_counts():
    sunday = dt.date(2026, 11, 8)  # the 2026 Muhurat session, not a calendar trading day
    bars = bars_of({dt.date(2026, 11, 6): (990.0, 1000.0), sunday: (1001.0, 1002.0)}, "19:14")
    f = decide(bars, dt.date(2026, 11, 6), at(sunday, "09:00"), never, may_miss=True)
    assert isinstance(f, Fill) and f.session == sunday


def test_d5_an_exit_is_never_missed_it_sells_at_the_first_bar_after_the_start_late():
    f = decide(BARS, D0, at(D2, "11:00"), never, may_miss=False)
    assert isinstance(f, Fill) and f.late and f.session == D2 and f.ts == at(D2, "11:00")


def test_d15_a_first_bar_more_than_20pct_from_the_previous_close_is_flagged():
    bars = bars_of({D0: (990.0, 1000.0), D1: (790.0, 800.0), D2: (800.0, 801.0)})
    assert gap_warning(bars, D0) == "possible corporate action on 2026-11-17: check the figures"
    assert gap_warning(bars, D1) is None
    assert gap_warning(BARS, D0) is None
    assert gap_warning(bars_of({D0: (990.0, 1000.0), D1: (801.0, 800.0)}), D0) is None  # 19.9%


# ── the book (D2 to D6, D12) ────────────────────────────────────────────────────────────────


def test_d2_an_entry_signal_is_one_pending_row_written_with_its_evaluation(book):
    t = book.record_evaluation("SBIN", D0, {"signal": True}, NOW, entry())
    assert t is not None and t.status == PENDING_ENTRY and t.qty == 10
    assert book.last_evaluated("SBIN") == D0 and book.evaluation("SBIN", D0) == {"signal": True}
    assert book.untold() == [(t, "entry")]
    book.mark_told(t.id, "entry", NOW)
    assert book.untold() == []


def test_d12_the_same_session_is_never_recorded_twice(book, engine):
    assert book.record_evaluation("SBIN", D0, {}, NOW, entry()) is not None
    assert book.record_evaluation("SBIN", D0, {}, NOW, entry()) is None
    assert book.record_evaluation("SBIN", D0, {}, NOW, None) is None
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(delivery_trades)).scalar() == 1


def test_d12_a_second_book_on_the_same_database_sees_the_same_state(book, engine, costs):
    t = book.record_evaluation("SBIN", D0, {}, NOW, entry())
    other = DeliveryBook(engine, delivery_model(costs), depository_paise(costs))
    assert [x.id for x in other.active()] == [t.id]
    assert other.fill_entry(t.id, at(D1, "09:15"), D1, 1005.3, False, NOW)
    assert not book.fill_entry(t.id, at(D1, "09:16"), D1, 1006.0, True, NOW)  # already OPEN
    assert book.get(t.id).entry_paise == 100530


def test_d3_missed_entry_is_closed_with_its_reason_and_never_opens(book):
    t = book.record_evaluation("SBIN", D0, {}, NOW, entry())
    assert book.miss_entry(t.id, "missed: the desk was not running", NOW)
    assert not book.fill_entry(t.id, at(D1, "09:15"), D1, 1000.0, False, NOW)
    got = book.get(t.id)
    assert got.status == MISSED and got.note == "missed: the desk was not running"
    assert book.active() == []


def test_d4_d5_d6_a_trade_is_carried_then_closed_with_every_figure(book, costs):
    t = book.record_evaluation("SBIN", D0, {}, NOW, entry(qty=10))
    assert book.fill_entry(t.id, at(D1, "09:15"), D1, 1000.30, False, NOW)
    assert book.get(t.id).status == OPEN  # carried: nothing closes it at a session end
    assert book.signal_exit(t.id, D2, ["SMA20", "TIME"], 2, NOW)
    assert not book.signal_exit(t.id, D2, ["SMA20"], 2, NOW)  # one exit alert, once
    got = book.get(t.id)
    assert (got.status, got.exit_reason, got.sessions_held) == (EXIT_PENDING, "SMA20,TIME", 2)
    assert book.untold() == [(got, "entry"), (got, "exit")] or book.untold()[-1][1] == "exit"
    assert book.fill_exit(t.id, at(D3, "09:20"), 1099.67, True, NOW)
    got = book.get(t.id)
    model = delivery_model(costs)
    buy = round(model.leg(1000.30, 10, "buy").total * 100)
    sell = round(model.leg(1099.67, 10, "sell").total * 100)
    assert got.status == CLOSED and got.exit_late and not got.entry_late
    assert (got.entry_paise, got.exit_paise, got.gross_paise) == (100030, 109967, 99370)
    assert got.charges_paise == buy + sell and got.dp_paise == 1475
    assert got.net_paise == 99370 - buy - sell - 1475
    assert got.net_pct == pytest.approx(got.net_paise / (100030 * 10) * 100)
    d = got.as_dict()
    assert d["net"] == got.net_paise / 100 and d["exit_ts"].startswith("2026-11-19T09:20")
    assert not book.fill_exit(t.id, at(D3, "09:21"), 1.0, True, NOW)


def test_d15_the_first_warning_stays_and_only_active_trades_take_one(book):
    t = book.record_evaluation("SBIN", D0, {}, NOW, entry())
    assert book.warn(t.id, "possible corporate action on X", NOW)
    assert not book.warn(t.id, "another", NOW)
    assert book.get(t.id).warning == "possible corporate action on X"


def test_the_scoreboard_row_counts_closed_trades_and_missed_entries(book):
    assert book.by_slot() == {}
    won = book.record_evaluation("SBIN", D0, {}, NOW, entry())
    book.fill_entry(won.id, at(D1, "09:15"), D1, 1000.0, False, NOW)
    book.signal_exit(won.id, D1, ["SMA20"], 1, NOW)
    book.fill_exit(won.id, at(D2, "09:15"), 1100.0, False, NOW)
    lost = book.record_evaluation("ITC", D0, {}, NOW, entry())
    book.miss_entry(lost.id, "missed", NOW)
    ((slot, row),) = book.by_slot().items()
    closed = book.get(won.id)
    assert slot == Slot("pullback", "1d", "STOCK")
    assert (row.trades, row.wins, row.losses, row.no_fill) == (1, 1, 0, 1)
    assert row.net_paise == closed.net_paise
    assert row.costs_paise == closed.charges_paise + closed.dp_paise
