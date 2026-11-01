"""US-023 mode B: edges the acceptance tests could not reach before the code existed.

* calendar edges for the ledger window: month end, year end, a leap day and midnight IST, with
  the window written in IST, in UTC and in fixed offsets that are one instant;
* sums past int64 (three nets of 4 x 10**18): the ledger sums in Python integers, exactly, and
  returns plain ints;
* the ledger read while a writer books trades into the same database: every snapshot is
  self-consistent (net = gross - costs, trades never go down) and nobody raises;
* an OPEN option trade written before US-023 has no mark in its position: the panel shows null
  (not 0, not an error) until the next bar closes, then a real mark;
* a stateful property over random bar paths, random pricer failures and random restarts:
  after every step the mark is None exactly when that step's pricing failed, else the oracle's
  mark; the PRICING_UNAVAILABLE (mark) events are exactly one per failure streak, restarts
  included; the trade stays OPEN and nothing is counted as a hook error.
"""

from __future__ import annotations

import datetime as dt
import json
import threading

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from onelot.clock import IST


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def instant(y, mo, d, h=0, mi=0, tz=IST):
    return dt.datetime(y, mo, d, h, mi, tzinfo=tz)


BOUNDARIES = {
    "month_end": (instant(2026, 10, 31, 23, 59), instant(2026, 11, 1, 0, 0)),
    "year_end": (instant(2026, 12, 31, 23, 59), instant(2027, 1, 1, 0, 0)),
    "leap_day": (instant(2028, 2, 29, 23, 59), instant(2028, 3, 1, 0, 0)),
    "midnight_ist": (instant(2026, 10, 29, 23, 59, tz=IST), instant(2026, 10, 30, 0, 0, tz=IST)),
}
ZONES = {
    "ist": IST,
    "utc": dt.timezone.utc,
    "minus5": dt.timezone(dt.timedelta(hours=-5)),
    "plus13": dt.timezone(dt.timedelta(hours=13)),
}


class TestWindowsAcrossCalendarEdges:
    @pytest.mark.parametrize("zone", list(ZONES))
    @pytest.mark.parametrize("edge", list(BOUNDARIES))
    def test_a_window_opening_at_the_edge_keeps_the_trade_after_it_and_not_the_one_before(
        self, db, edge, zone
    ):
        before, after = BOUNDARIES[edge]
        W.insert_booked(
            db.engine,
            [W.booked("BEFORE", 900, 100, ts=before), W.booked("AFTER", 1100, 100, ts=after)],
        )
        ledger = W.make_ledger(db.engine)
        since = after.astimezone(ZONES[zone])  # the same instant, written in another zone
        assert since == after
        kept = ledger.totals(since=since)
        assert (kept.trades, kept.net_paise) == (1, 1000), (edge, zone)
        cut = ledger.totals(until=since)
        assert (cut.trades, cut.net_paise) == (1, 800), (edge, zone)
        assert ledger.totals(since=since, until=since).trades == 0, "[t, t) holds nothing"
        assert ledger.totals().trades == 2

    def test_a_one_microsecond_window_around_a_trade_holds_exactly_that_trade(self, db):
        t = instant(2026, 10, 29, 10, 0)
        W.insert_booked(db.engine, [W.booked("T", 700, 100, ts=t)])
        ledger = W.make_ledger(db.engine)
        tick = dt.timedelta(microseconds=1)
        assert ledger.totals(since=t, until=t + tick).trades == 1
        assert ledger.totals(since=t - tick, until=t).trades == 0
        assert ledger.totals(since=t + tick).trades == 0


class TestSumsPastInt64:
    def test_three_nets_of_four_quintillion_sum_exactly(self, db):
        big = 4 * 10**18  # below 2**63 each, above it in total
        W.insert_booked(
            db.engine,
            [W.booked(f"B{i}", big + 1, 1, ts=instant(2026, 10, 29, 10, i)) for i in range(3)],
        )
        totals = W.make_ledger(db.engine).totals()
        assert totals.trades == 3
        assert totals.gross_paise == 3 * (big + 1) and totals.costs_paise == 3
        assert totals.net_paise == 3 * big == 12 * 10**18
        assert all(type(getattr(totals, f)) is int for f in W.LEDGER_FIELDS)

    def test_a_mixed_book_of_huge_wins_and_losses_cancels_to_the_unit(self, db):
        big = 4 * 10**18
        W.insert_booked(
            db.engine,
            [
                W.booked("W1", big, 0, ts=instant(2026, 10, 29, 10, 0)),
                W.booked("W2", big, 0, ts=instant(2026, 10, 29, 10, 1)),
                W.booked("L1", -big, 0, ts=instant(2026, 10, 29, 10, 2)),
                W.booked("L2", -big, 0, ts=instant(2026, 10, 29, 10, 3)),
                W.booked("W3", 7, 0, ts=instant(2026, 10, 29, 10, 4)),
            ],
        )
        totals = W.make_ledger(db.engine).totals()
        assert (totals.trades, totals.wins, totals.losses) == (5, 3, 2)
        assert totals.net_paise == 7


class TestTheLedgerReadWhileABookWrites:
    def test_every_snapshot_is_self_consistent_and_trades_never_go_down(self, db):
        total = 120
        stop = threading.Event()
        problems: list[str] = []
        writer_errors: list[BaseException] = []

        def writer():
            try:
                for i in range(total):
                    W.insert_booked(
                        db.engine,
                        [W.booked(f"S{i:04d}", 1000 + i, 37, ts=instant(2026, 10, 29, 10, 0))],
                    )
            except BaseException as exc:  # noqa: BLE001 - reported below
                writer_errors.append(exc)
            finally:
                stop.set()

        ledger = W.make_ledger(db.engine)
        thread = threading.Thread(target=writer)
        thread.start()
        last, reads = 0, 0
        while not stop.is_set() or reads < 3:
            totals = ledger.totals()
            reads += 1
            if totals.net_paise != totals.gross_paise - totals.costs_paise:
                problems.append(f"torn snapshot {totals}")
            if totals.trades < last:
                problems.append(f"trades went down {last} -> {totals.trades}")
            if totals.wins + totals.losses != totals.trades:
                problems.append(f"wins+losses != trades {totals}")
            if totals.costs_paise != 37 * totals.trades:
                problems.append(f"costs do not match the trades {totals}")
            last = totals.trades
        thread.join(timeout=60)
        assert writer_errors == [] and problems == [], problems[:3]
        assert ledger.totals().trades == total and reads >= 3


CE_ROWS = [
    W.CE_FILL,
    (5400500, 5405500, 5400000, 5405000),
    (5405000, 5409000, 5403000, 5408000),
    (5408000, 5410000, 5399500, 5400000),
]


class TestAnOpenOptionWrittenBeforeUs023:
    def legacy_open_option(self, db, chain):
        """Fill and step an option trade, then delete the mark from its stored position: what a
        US-002 era row looks like."""
        alert = W.chain_alert() if chain else U.make_alert("O1", kind="INDEX", direction="long")
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
        U.step_book(book, frame, H.at(10, 8), series="BANKNIFTY")
        with db.engine.begin() as conn:
            plan = H.trade_row(db.engine, "O1")["plan"]
            for key in ("mark_paise", "mark_failed"):
                plan["position"].pop(key, None)
            conn.execute(
                text("UPDATE paper_trades SET plan = :p WHERE alert_id = 'O1'"),
                {"p": json.dumps(plan)},
            )
        assert "mark_paise" not in W.position_of(db.engine)
        return alert, frame

    @pytest.mark.parametrize("chain", [False, True], ids=["synthetic", "live-chain"])
    def test_the_panel_shows_null_not_zero_and_the_next_bar_marks_it(self, db, chain):
        alert, frame = self.legacy_open_option(db, chain)
        book = W.make_book(db.engine, premium_at=H.make_pricer())  # the upgraded start
        assert book.status()["hook_errors"] == 0
        trade = W.api_trade(book, "O1")
        assert trade["status"] == "OPEN"
        assert trade.get("mark") is None and trade.get("unrealised") is None
        assert W.pricing_events(db.engine, "O1", leg="mark") == [], "nothing failed: not marked yet"
        U.step_book(book, frame, H.at(10, 9), series="BANKNIFTY")  # the 10:08 bar closes
        basis = 525 if chain else 0
        assert W.mark_of(db.engine) == W.fake_mark(alert, 5408000, basis)
        assert W.api_trade(book, "O1")["mark"] == W.rupees(W.fake_mark(alert, 5408000, basis))

    def test_stepping_again_before_a_new_bar_does_not_invent_a_mark_or_an_event(self, db):
        _, frame = self.legacy_open_option(db, False)
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        before = (H.trade_rows(db.engine), H.event_rows(db.engine))
        assert U.step_book(book, frame, H.at(10, 8, 30), series="BANKNIFTY") == []
        assert (H.trade_rows(db.engine), H.event_rows(db.engine)) == before


@st.composite
def runs(draw):
    n = draw(st.integers(min_value=3, max_value=8))
    steps = draw(st.lists(st.integers(-2400, 2400), min_size=n, max_size=n))
    fails = [False, *draw(st.lists(st.booleans(), min_size=n - 1, max_size=n - 1))]
    restarts = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    return steps, fails, restarts


@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    phases=(Phase.explicit, Phase.generate),
    suppress_health_check=list(HealthCheck),
)
@given(run=runs(), direction=st.sampled_from(["long", "short"]), chain=st.booleans())
def test_P6_marks_and_failure_streaks_hold_over_random_failures_and_restarts(run, direction, chain):
    steps, fails, restarts = run
    rows, prev = [], 5400000
    for step in steps:
        close = 5400000 + step * 5
        rows.append((prev, max(prev, close) + 10, min(prev, close) - 10, close))
        prev = close
    alert = (
        W.chain_alert(direction=direction)
        if chain
        else U.make_alert("O1", kind="INDEX", direction=direction)
    )
    basis = 525 if chain else 0
    pricer = W.SwitchablePricer(None)
    frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *rows])
    with W.temp_db() as engine:
        book = W.make_book(engine, premium_at=pricer)
        H.open_trade(book, alert)
        expected_streaks = 0
        for k, (failing, restart) in enumerate(zip(fails, restarts, strict=True)):
            if restart and k > 0:
                book = W.make_book(engine, premium_at=pricer)  # the process restarted
            pricer.failing = failing
            U.step_book(book, frame, H.at(10, 7) + dt.timedelta(minutes=k), series="BANKNIFTY")
            row = U.row_of(engine, "O1")
            assert row["status"] == "OPEN", (k, row["status"], row["close_reason"])
            mark = W.mark_of(engine)
            if failing:
                assert mark is None, (k, mark)
                if k == 0 or not fails[k - 1]:
                    expected_streaks += 1
            else:
                assert mark == W.fake_mark(alert, rows[k][3], basis), (k, mark)
            assert len(W.pricing_events(engine, "O1", leg="mark")) == expected_streaks, (k, fails)
        assert book.status()["hook_errors"] == 0
