"""US-001 mode B: a feed that changes resolution regrades a live trade, with an audit event.

Manager decisions 6 and g: a frame whose bars of the trade's session date (closed ones only)
show TWO OR MORE CONSECUTIVE gaps wider than the base spacing is a feed that switched
resolution (Dhan 1m <-> Yahoo 2m). The waiting trade is regraded live -> delayed BEFORE its fill
decision, persisted, with one REGRADED event (from, to, reason, base_min, the gap bar times).
One hole, or the overnight gap, is not mixed spacing. It never goes back to live, and a trade
that is already delayed is never regraded.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import us001_helpers as H
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROP = settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def mixed_frame():
    """1-minute bars 10:00..10:05, then 2-minute bars 10:07, 10:09, 10:11."""
    one = H.minute_bars(H.at(10, 0), [999.0, 999.0, 999.0, 999.0, 999.0, 999.0])
    two = H.minute_bars(H.at(10, 7), [1000.0, 1001.0, 1002.0], step=2)
    return pd.concat([one, two])


def frame_at(offsets, start=None, opens=None):
    start = start or H.at(10, 0)
    stamps = [start + dt.timedelta(minutes=m) for m in offsets]
    return H.bars_at(stamps, opens if opens is not None else [1000.0] * len(stamps))


def kinds(engine, alert_id):
    return [H.val(e["kind"]) for e in H.event_rows(engine, alert_id)]


class TestTheRegradeEvent:
    def test_a_live_trade_on_a_mixed_frame_is_regraded_once_with_an_audit_event(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")  # live (dhan), 1m, created 10:05:03
        events = book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame()))
        assert [H.val(e.kind) for e in events] == ["REGRADED", "FILLED"]
        assert kinds(db.engine, "A1") == ["OPENED", "REGRADED", "FILLED"]
        (regraded,) = [e for e in H.event_rows(db.engine, "A1") if H.val(e["kind"]) == "REGRADED"]
        detail = regraded["detail"]
        assert detail["from"] == "live" and detail["to"] == "delayed"
        assert detail["reason"] == "mixed_spacing" and detail["base_min"] == 1
        # the gap bars: the opens that bound the wide gaps of the run, on the session date
        assert detail["gap_bars"] == [
            H.at(10, 5).isoformat(),
            H.at(10, 7).isoformat(),
            H.at(10, 9).isoformat(),
            H.at(10, 11).isoformat(),
        ]
        assert regraded["ts"] is not None and regraded["ts"] != H.at(10, 20)  # data time
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "delayed"
        assert [H.val(t.grade) for t in book.open_trades()] == ["delayed"]

    def test_the_regrade_is_booked_before_the_fill_and_the_fill_records_the_new_grade(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame()))
        filled = [e for e in H.event_rows(db.engine, "A1") if H.val(e["kind"]) == "FILLED"]
        assert filled[0]["detail"]["grade"] == "delayed"
        ids = [e["id"] for e in H.event_rows(db.engine, "A1")]
        assert ids == sorted(ids)

    def test_stepping_the_same_frame_again_regrades_nothing_twice(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame()))
        assert book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame())) == []
        assert book.step(H.at(10, 25), H.Feed(SBIN=mixed_frame())) == []
        assert kinds(db.engine, "A1").count("REGRADED") == 1

    def test_the_regrade_extends_the_fill_window_to_the_delayed_deadline(self, db):
        """Expires 10:06:03 and the first bar after the alert is 10:07: a live trade is NO_FILL;
        regraded delayed (+15 min) it fills that bar."""
        alert = H.make_alert("A1", created=H.at(10, 5, 3), ttl_min=1)
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame()))
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "OPEN" and H.same_instant(row["fill_bar_ts"], H.at(10, 7))
        control = H.minute_bars(H.at(10, 7), [1000.0, 1001.0, 1002.0, 1003.0])  # uniform: not mixed
        book2 = H.make_book(db.engine)
        H.open_trade(book2, H.make_alert("A2", created=H.at(10, 5, 3), ttl_min=1))
        book2.step(H.at(10, 20), H.Feed(SBIN=control))
        assert H.val(H.trade_row(db.engine, "A2")["status"]) == "NO_FILL"
        assert "REGRADED" not in kinds(db.engine, "A2")

    def test_a_waiting_trade_stays_regraded_across_later_frames_and_a_restart(self, db):
        """Created after the mixed run: it waits (no bar after it yet), is regraded at once, and
        never returns to live whatever the next frames look like."""
        alert = H.make_alert("A1", created=H.at(10, 30, 3), ttl_min=120)
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        events = book.step(H.at(10, 32), H.Feed(SBIN=mixed_frame()))
        assert [H.val(e.kind) for e in events] == ["REGRADED"]
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "PENDING_FILL"
        uniform = H.minute_bars(H.at(10, 0), [1000.0] * 40)  # all uniform now, bars after created
        restarted = H.make_book(db.engine)
        assert [H.val(t.grade) for t in restarted.open_trades()] == ["delayed"]
        events = restarted.step(H.at(10, 45), H.Feed(SBIN=uniform))
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "delayed"
        assert kinds(db.engine, "A1") == ["OPENED", "REGRADED", "FILLED"]

    @pytest.mark.parametrize("source", ["yahoo", "yahoo-2m", "mock", "fake"])
    def test_a_delayed_trade_is_never_regraded(self, db, source):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", source=source)
        book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame()))
        assert "REGRADED" not in kinds(db.engine, "A1")
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "delayed"

    def test_regrading_one_trade_does_not_regrade_another_series(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        H.open_trade(book, alert_id="R1", instrument_key="RELIANCE")
        clean = H.minute_bars(H.at(10, 5), [1000.0] * 12)
        book.step(H.at(10, 20), H.Feed(SBIN=mixed_frame(), RELIANCE=clean))
        assert H.val(H.trade_row(db.engine, "S1")["grade"]) == "delayed"
        assert H.val(H.trade_row(db.engine, "R1")["grade"]) == "live"
        assert "REGRADED" not in kinds(db.engine, "R1")


class TestWhatIsAndIsNotMixedSpacing:
    @pytest.mark.parametrize(
        "offsets, mixed",
        [
            ([0, 1, 2, 3, 4, 5], False),  # uniform
            ([0, 1, 2, 4, 5, 6], False),  # one missing minute: an outage, not a resolution change
            ([0, 1, 2, 5, 6, 7], False),  # one 3-minute hole
            ([0, 1, 3, 4, 6, 7], False),  # two holes separated by a normal bar (2, 1, 2)
            ([0, 1, 2, 4, 6, 7], True),  # exactly two consecutive wide gaps (2, 2)
            ([0, 1, 3, 8, 9, 10], True),  # 2 then 5: both wider than the base
            ([0, 1, 2, 4, 6, 8, 10], True),  # a run of 2-minute bars
        ],
        ids=["uniform", "one-hole", "3-min-hole", "separated-holes", "two-wide", "2-then-5", "run"],
    )
    def test_two_or_more_consecutive_wide_gaps_make_it_mixed(self, db, offsets, mixed):
        alert = H.make_alert("A1", created=H.at(9, 59, 3), ttl_min=120)
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        book.step(H.at(11, 30), H.Feed(SBIN=frame_at(offsets)))
        assert ("REGRADED" in kinds(db.engine, "A1")) is mixed
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == ("delayed" if mixed else "live")

    def test_the_overnight_gap_between_two_sessions_is_not_mixed_spacing(self, db):
        yesterday = H.minute_bars(H.at(15, 20, day=H.DAY - dt.timedelta(days=1)), [999.0] * 10)
        today = H.minute_bars(H.at(10, 0), [999.0] * 6 + [1000.0, 1001.0, 1002.0])
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        book.step(H.at(10, 20), H.Feed(SBIN=pd.concat([yesterday, today])))
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "live"
        assert "REGRADED" not in kinds(db.engine, "A1")

    def test_a_mixed_run_on_an_earlier_session_does_not_regrade_todays_trade(self, db):
        yesterday = frame_at(
            [0, 1, 2, 4, 6, 8, 10], start=H.at(10, 0, day=H.DAY - dt.timedelta(days=1))
        )
        today = H.minute_bars(H.at(10, 0), [999.0] * 6 + [1000.0, 1001.0, 1002.0])
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        book.step(H.at(10, 20), H.Feed(SBIN=pd.concat([yesterday, today])))
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "live"

    def test_only_bars_that_have_closed_count_towards_the_run(self, db):
        """All bars are unusable (NaN opens) so the trade just waits; the spacing still shows. At
        10:09:30 only one wide gap has closed, at 10:20 the run is complete."""
        alert = H.make_alert("A1", created=H.at(10, 0, 3), ttl_min=240)
        frame = mixed_frame()
        frame["open"] = float("nan")
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        assert book.step(H.at(10, 9, 30), H.Feed(SBIN=frame)) == []
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "live"
        events = book.step(H.at(10, 20), H.Feed(SBIN=frame))
        assert [H.val(e.kind) for e in events] == ["REGRADED"]

    @given(
        gaps=st.lists(st.integers(min_value=1, max_value=6), min_size=2, max_size=14),
    )
    @PROP
    def test_property_regraded_iff_two_consecutive_gaps_exceed_the_base(self, gaps):
        offsets = [0]
        for g in gaps:
            offsets.append(offsets[-1] + g)
        expected = any(a > 1 and b > 1 for a, b in zip(gaps, gaps[1:], strict=False))
        alert = H.make_alert("P1", created=H.at(9, 59, 3), ttl_min=300)
        with H.temp_book() as (book, engine):
            H.open_trade(book, alert)
            book.step(H.at(13, 0), H.Feed(SBIN=frame_at(offsets)))
            got = "REGRADED" in kinds(engine, "P1")
            grade = H.val(H.trade_row(engine, "P1")["grade"])
        assert got is expected
        assert grade == ("delayed" if expected else "live")
