"""US-001 AC2: a PENDING_FILL trade fills at the OPEN of the first closed fine bar.

    step(now, bars) fills at the open of the first bar of the desk fine resolution with
        open  >  created_at                       (strictly later: no look-ahead)
        open + resolution  <=  now                (the bar has closed)
        open  <=  expires_at (+ feed_delay_minutes for delayed grade)   (data time, not wall time)

Worked example: created 10:05:03, bars opening 10:05 and 10:06, step at 10:08 fills at the
10:06 open. Stock long open 1000.00 with slippage_pct 0.03 PERCENT -> 1000.30, snapped UP
to the 0.05 tick, entry_paise 100030; a short gives 99970 snapped DOWN. Index options mirror
`lab.execution.execute_trade` (round_to_tick up of fair*(1+option_spread_pct) +
option_slippage_ticks*tick, IV frozen at the alert) to zero paise, here with a fake pricer.

Frames in this file have uniform spacing: where the first eligible bar must come late, the
frame simply starts late (a desk restart / source switch), never with a hole in the middle.
"""

from __future__ import annotations

import datetime as dt
import json

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


def fill(db, *, frame=None, now=None, alert=None, **book_kw):
    """Open one trade from `alert` (default: stock long A1), step once, return (book, row, events)."""
    now = now or H.at(10, 8)
    book = H.make_book(db.engine, **book_kw)
    alert = alert or H.make_alert("A1")
    H.open_trade(book, alert)
    frame = frame if frame is not None else H.standard_bars()
    events = book.step(now, H.Feed(**{alert["series_key"]: frame}))
    return book, H.trade_row(db.engine, alert["id"]), events


class TestWorkedExample:
    def test_a_long_fills_at_the_1006_open_plus_slippage_snapped_up(self, db):
        _, row, events = fill(db, frame=H.minute_bars(H.at(10, 5), [998.0, 1000.0]))
        assert H.val(row["status"]) == "OPEN"
        assert row["entry_paise"] == 100030  # 1000.00 * 1.0003 = 1000.30, on the 0.05 tick
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6))
        assert H.same_instant(row["entry_ts"], H.at(10, 6))
        assert row["exit_paise"] is None and row["close_reason"] is None
        assert [H.val(e.kind) for e in events] == ["FILLED"]

    def test_a_short_fills_at_the_same_open_minus_slippage_snapped_down(self, db):
        alert = H.make_alert("A1", direction="short")
        _, row, _ = fill(db, frame=H.minute_bars(H.at(10, 5), [998.0, 1000.0]), alert=alert)
        assert H.val(row["status"]) == "OPEN"
        assert row["entry_paise"] == 99970  # 1000.00 * 0.9997 = 999.70
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6))

    def test_a_later_bar_is_never_used_when_an_earlier_one_is_eligible(self, db):
        _, row, _ = fill(db, frame=H.standard_bars((998.0, 1000.0, 1100.0, 1200.0)))
        assert row["entry_paise"] == 100030 and H.same_instant(row["fill_bar_ts"], H.at(10, 6))

    def test_the_fill_row_records_the_resolution_and_the_quantity(self, db):
        _, row, _ = fill(db)
        assert row["resolution_min"] == 1 and row["qty"] == 1
        assert H.val(row["grade"]) == "live"
        assert H.same_instant(row["created_at"], H.CREATED)  # the fill does not touch created_at

    def test_the_filled_event_is_stamped_with_the_bar_open_and_keeps_the_prices_used(self, db):
        _, row, events = fill(db, frame=H.standard_bars((998.0, 1003.35, 1001.0)))
        (event,) = events
        assert event.trade_id == row["id"] and H.val(event.kind) == "FILLED"
        assert event.ts == H.at(10, 6) and event.ts != H.at(10, 8)  # bar time, never `now`
        text = H.blob(event.detail)
        assert "1003.35" in text or "100335" in text, f"the bar open is not in the detail: {text}"
        stored = [e for e in H.event_rows(db.engine, "A1") if H.val(e["kind"]) == "FILLED"]
        assert len(stored) == 1 and H.same_instant(stored[0]["ts"], H.at(10, 6))
        assert stored[0]["detail"] == json.loads(json.dumps(event.detail, default=str))

    def test_stepping_again_with_the_same_inputs_does_nothing(self, db):
        book, row, _ = fill(db)
        feed = H.Feed(SBIN=H.standard_bars())
        assert book.step(H.at(10, 8), feed) == []
        assert H.trade_row(db.engine, "A1") == row
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]
        assert book.status()["filled"] == 1

    def test_a_filled_trade_stays_listed_as_open(self, db):
        book, row, _ = fill(db)
        (listed,) = book.open_trades()
        assert listed.id == row["id"] and H.val(listed.status) == "OPEN"
        assert listed.entry_paise == 100030 and listed.qty == 1


class TestSlippageAndTickGolden:
    """Independent literals (hand-derived), including values where float noise would bump a tick."""

    @pytest.mark.parametrize(
        "open_price, long_paise, short_paise",
        [
            (
                1000.00,
                100030,
                99970,
            ),  # 1000*1.0003 = 1000.3000000000001 in float: must stay 1000.30
            (1000.01, 100035, 99970),
            (1000.04, 100035, 99970),
            (1000.05, 100040, 99970),  # 1000.350015 -> up to .40 ; 999.749985 -> down to .70
            (250.00, 25010, 24990),
            (100.00, 10005, 9995),
            (5000.00, 500150, 499850),
            (12345.60, 1234935, 1234185),
            (1.00, 105, 95),
        ],
    )
    def test_entry_is_snapped_against_the_trader(self, db, open_price, long_paise, short_paise):
        frame = H.standard_bars((open_price, open_price, open_price))
        for direction, want in (("long", long_paise), ("short", short_paise)):
            stop = open_price * (0.5 if direction == "long" else 2.0)
            # the target is moved far out: a slipped entry at or beyond the target is NO_FILL
            # (US-002 review 1 MAJOR-2), and at 1.00 the snap alone moves a tick
            target = open_price * (3.0 if direction == "long" else 0.1)
            alert = H.make_alert(
                f"G-{direction}", direction=direction, entry=open_price, stop=stop, target=target
            )
            _, row, _ = fill(db, frame=frame, alert=alert)
            assert row["entry_paise"] == want, (direction, open_price)

    def test_slippage_pct_is_a_percent_not_a_fraction(self, db):
        """0.10 means 0.10%: 1000.00 -> 1001.00 (long) and 999.00 (short), not +-10%."""
        bt = H.BT.model_copy(update={"slippage_pct": 0.10})
        _, long_row, _ = fill(db, bt=bt)
        assert long_row["entry_paise"] == 100100
        _, short_row, _ = fill(db, bt=bt, alert=H.make_alert("A2", direction="short"))
        assert short_row["entry_paise"] == 99900

    def test_zero_slippage_fills_at_the_open_exactly(self, db):
        bt = H.BT.model_copy(update={"slippage_pct": 0.0})
        _, row, _ = fill(db, bt=bt, frame=H.standard_bars((998.0, 1000.05, 1001.0)))
        assert row["entry_paise"] == 100005  # 1000.05 is on the tick already

    @given(
        open_paise=st.integers(min_value=100, max_value=2_000_000),
        direction=st.sampled_from(["long", "short"]),
    )
    @PROP
    def test_property_the_snap_is_against_the_trader_on_the_tick_grid(self, open_paise, direction):
        """For any open: entry is on the 5-paise grid, never better than open*(1 -+ 0.03%), and
        less than one tick worse; a long never pays below the open, a short never sells above it."""
        open_price = open_paise / 100
        far = open_price * (0.5 if direction == "long" else 2.0)
        far_target = open_price * (3.0 if direction == "long" else 0.1)  # out of the snap's reach
        alert = H.make_alert(
            "P1", direction=direction, entry=open_price, stop=far, target=far_target
        )
        with H.temp_book() as (book, engine):
            H.open_trade(book, alert)
            book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars((open_price,) * 3)))
            entry = H.trade_row(engine, "P1")["entry_paise"]
        assert entry == H.expected_stock_entry(open_price, direction)
        assert entry % 5 == 0
        if direction == "long":
            assert entry >= open_paise and entry * 10_000 >= open_paise * 10_003
            assert entry * 10_000 < open_paise * 10_003 + 5 * 10_000
        else:
            assert entry <= open_paise and entry * 10_000 <= open_paise * 9_997
            assert entry * 10_000 > open_paise * 9_997 - 5 * 10_000


class TestFirstEligibleBar:
    @pytest.mark.parametrize(
        "created, fill_at",
        [
            (H.at(10, 5, 3), H.at(10, 6)),
            (H.at(10, 5, 59, 999999), H.at(10, 6)),
            (H.at(10, 6), H.at(10, 7)),  # open must be LATER than created_at, not equal
            (H.at(10, 6, 0, 1), H.at(10, 7)),
            (H.at(10, 4, 59, 999999), H.at(10, 5)),
        ],
        ids=["worked-example", "1us-before-open", "created-on-open", "1us-after-open", "earlier"],
    )
    def test_the_open_must_be_strictly_later_than_created_at(self, db, created, fill_at):
        opens = [1000 + 2 * i for i in range(8)]  # 10:04 -> 1000, 10:05 -> 1002, ...
        frame = H.minute_bars(H.at(10, 4), opens)
        alert = H.make_alert("A1", created=created)
        _, row, _ = fill(db, frame=frame, alert=alert, now=H.at(10, 20))
        want_open = opens[int((fill_at - H.at(10, 4)).total_seconds() // 60)]
        assert H.same_instant(row["fill_bar_ts"], fill_at)
        assert row["entry_paise"] == H.expected_stock_entry(want_open, "long")

    @pytest.mark.parametrize(
        "now, filled",
        [
            (H.at(10, 6, 59), False),
            (H.at(10, 7), True),  # open + resolution <= now: the closing instant counts
            (H.at(10, 7, 0, 1), True),
            (H.at(10, 6), False),
        ],
        ids=["1s-before-close", "at-close", "1us-after-close", "at-open"],
    )
    def test_a_bar_is_used_only_once_it_has_closed(self, db, now, filled):
        _, row, events = fill(db, frame=H.standard_bars((998.0, 1000.0)), now=now)
        if filled:
            assert H.val(row["status"]) == "OPEN" and [H.val(e.kind) for e in events] == ["FILLED"]
        else:
            assert H.val(row["status"]) == "PENDING_FILL" and events == []
            assert row["entry_paise"] is None and row["fill_bar_ts"] is None

    def test_two_minute_bars_close_two_minutes_after_their_open(self, db):
        alert = H.make_alert("A1", resolution="2m")
        frame = H.minute_bars(H.at(10, 4), [999.0, 1000.0, 1002.0, 1004.0], step=2)
        _, early, _ = fill(db, frame=frame, alert=alert, now=H.at(10, 7, 59))
        assert H.val(early["status"]) == "PENDING_FILL"  # the 10:06 bar closes at 10:08
        _, row, _ = fill(
            db, frame=frame, alert=H.make_alert("A2", resolution="2m"), now=H.at(10, 8)
        )
        assert H.val(row["status"]) == "OPEN" and H.same_instant(row["fill_bar_ts"], H.at(10, 6))
        assert row["entry_paise"] == 100030 and row["resolution_min"] == 2

    def test_nothing_after_now_can_change_the_fill(self, db):
        """Bars that have not closed by `now` are poisoned; the fill must not move or break."""
        poisoned = H.minute_bars(H.at(10, 5), [998.0, 1000.0, 1.0, 1.0, 5000.0, 1.0])
        _, row, _ = fill(db, frame=poisoned, now=H.at(10, 7, 30))
        assert row["entry_paise"] == 100030 and H.same_instant(row["fill_bar_ts"], H.at(10, 6))

    @given(
        opens=st.lists(st.integers(99_000, 101_000), min_size=40, max_size=40),
        minutes_after=st.integers(0, 39),
        direction=st.sampled_from(["long", "short"]),
    )
    @PROP
    def test_property_the_outcome_at_now_ignores_bars_that_have_not_closed(
        self, opens, minutes_after, direction
    ):
        """No-look-ahead truncation: the result of step(now, full frame) equals step(now, the
        frame cut to bars closed by now), and equals it with the future bars poisoned."""
        now = H.CREATED + dt.timedelta(minutes=minutes_after, seconds=30)
        prices = [o / 100 for o in opens]
        full = H.minute_bars(H.at(10, 0), prices)
        closed = full[full.index + pd.Timedelta(minutes=1) <= now]
        poisoned = full.copy()
        future = poisoned.index + pd.Timedelta(minutes=1) > now
        poisoned.loc[future, ["open", "high", "low", "close"]] = 1.0
        far = 1000.0 * (0.01 if direction == "long" else 100.0)
        alert = H.make_alert("T1", direction=direction, stop=far)
        results = []
        for frame in (full, closed, poisoned):
            with H.temp_book() as (book, engine):
                H.open_trade(book, alert)
                book.step(now, H.Feed(SBIN=frame))
                row = H.trade_row(engine, "T1")
                results.append(
                    (
                        H.val(row["status"]),
                        row["entry_paise"],
                        row["fill_bar_ts"],
                        row["close_reason"],
                    )
                )
        assert results[0] == results[1] == results[2]


class TestDataTimeNotWallTime:
    def test_an_outage_then_catch_up_fills_at_the_first_bar_inside_the_ttl(self, db):
        """The feed is silent for 35 minutes, then the missing bars arrive at once. TTL is judged
        on bar times: the 10:06 bar opened inside the window, so it is the fill."""
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        stale = H.minute_bars(H.at(10, 0), [1000.0] * 6)  # bars 10:00 .. 10:05
        assert book.step(H.at(10, 40), H.Feed(SBIN=stale)) == []
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "PENDING_FILL"
        opens = [990.0] * 6 + [1000.0] + [1010.0] * 33
        caught_up = H.minute_bars(H.at(10, 0), opens)  # 10:00 .. 10:39
        events = book.step(H.at(10, 41), H.Feed(SBIN=caught_up))
        row = H.trade_row(db.engine, "A1")
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6))
        assert row["entry_paise"] == 100030

    def test_the_window_end_is_inclusive_for_a_live_alert(self, db):
        created = H.at(10, 5)
        alert = H.make_alert("A1", created=created)  # expires 10:15:00
        frame = H.minute_bars(
            H.at(10, 15), [1000.0, 1001.0, 1002.0]
        )  # first bar opens ON expires_at
        _, row, _ = fill(db, frame=frame, alert=alert, now=H.at(10, 30))
        assert H.val(row["status"]) == "OPEN" and H.same_instant(row["fill_bar_ts"], H.at(10, 15))
        assert row["entry_paise"] == 100030

    @pytest.mark.parametrize(
        "feed_delay, frame_start, filled",
        [
            (15, H.at(10, 30), True),  # expires 10:15:00 + 15 min = 10:30:00, inclusive
            (15, H.at(10, 31), False),
            (5, H.at(10, 20), True),
            (5, H.at(10, 21), False),
            (0, H.at(10, 15), True),
            (0, H.at(10, 16), False),
            (30, H.at(10, 45), True),
            (30, H.at(10, 46), False),
        ],
    )
    def test_feed_delay_minutes_extends_the_window_of_a_delayed_alert(
        self, db, feed_delay, frame_start, filled
    ):
        alert = H.make_alert("A1", created=H.at(10, 5), source="yahoo")
        frame = H.minute_bars(frame_start, [1000.0, 1001.0, 1002.0])
        _, row, _ = fill(db, frame=frame, alert=alert, now=H.at(11, 30), feed_delay=feed_delay)
        assert H.val(row["grade"]) == "delayed"
        want = "OPEN" if filled else "NO_FILL"
        assert H.val(row["status"]) == want, (feed_delay, frame_start)

    def test_the_same_late_bars_fill_a_delayed_alert_but_not_a_live_one(self, db):
        frame = H.minute_bars(H.at(10, 16), [1000.0, 1001.0, 1002.0])  # opens after 10:15:03
        _, live, _ = fill(db, frame=frame, now=H.at(10, 30), alert=H.make_alert("L1"))
        _, late, _ = fill(
            db, frame=frame, now=H.at(10, 30), alert=H.make_alert("D1", source="yahoo")
        )
        assert H.val(live["status"]) == "NO_FILL" and live["entry_paise"] is None
        assert H.val(late["status"]) == "OPEN" and H.same_instant(late["fill_bar_ts"], H.at(10, 16))
        assert late["entry_paise"] == 100030

    def test_the_feed_delay_is_not_applied_to_a_live_alert(self, db):
        frame = H.minute_bars(H.at(10, 20), [1000.0, 1001.0])  # inside +15 min, outside the TTL
        _, row, _ = fill(db, frame=frame, now=H.at(10, 30), feed_delay=15)
        assert H.val(row["status"]) == "NO_FILL"


def _option_alert(direction: str, **kw):
    return H.make_alert("O1", kind="INDEX", direction=direction, **kw)


def _option_frame(underlying_open: float):
    return H.minute_bars(H.at(10, 5), [OPT_PRE, underlying_open, underlying_open + 1.0])


OPT_PRE = H.OPT_U0 - 5.0


class TestIndexOptionFill:
    @pytest.mark.parametrize(
        "direction, underlying_open, entry_paise",
        [
            ("long", 54000.0, 45280),  # fair 450.00 -> 450*1.006 + 2*0.05 = 452.80
            ("long", 54012.5, 46100),  # fair 458.125 -> 460.97375 -> up to 461.00
            ("short", 53950.0, 48550),  # PE: fair 482.50 -> 485.495 -> up to 485.50
            ("short", 54000.0, 45280),
        ],
    )
    def test_the_fill_mirrors_execute_trade_to_the_paise(
        self, db, direction, underlying_open, entry_paise
    ):
        pricer = H.make_pricer()
        _, row, events = fill(
            db,
            alert=_option_alert(direction),
            frame=_option_frame(underlying_open),
            premium_at=pricer,
        )
        opt = "CE" if direction == "long" else "PE"
        oracle = H.oracle_option_entry_paise(
            bt=H.BT,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type=opt,
            underlying_open=underlying_open,
            fill_ts=H.at(10, 6),
            pricer=H.make_pricer(),
        )
        assert oracle == entry_paise, "harness: the literal and execute_trade disagree"
        assert H.val(row["status"]) == "OPEN"
        assert row["entry_paise"] == entry_paise
        assert row["qty"] == H.LOT
        assert [H.val(e.kind) for e in events] == ["FILLED"]

    def test_the_pricer_is_asked_for_the_alerted_option_at_the_fill_bar(self, db):
        pricer = H.make_pricer()
        alert = _option_alert("long", iv=0.1875)
        fill(db, alert=alert, frame=_option_frame(54012.5), premium_at=pricer)
        wanted = alert["economics"]["option"]
        at_fill = [c for c in pricer.calls if c[1] == 54012.5]
        assert at_fill, f"premium_at was never asked for the fill bar's underlying: {pricer.calls}"
        option, underlying, ts = at_fill[-1]
        assert option["strike"] == wanted["strike"] and option["opt_type"] == "CE"
        assert option["expiry"] == wanted["expiry"] and option["iv"] == 0.1875  # IV frozen
        assert pd.Timestamp(ts) == pd.Timestamp(H.at(10, 6))

    def test_spread_is_a_fraction_and_the_slippage_is_in_ticks(self, db):
        """option_spread_pct 0.01 is 1%, option_slippage_ticks 3 is 3 x 0.05: 450*1.01 + 0.15."""
        bt = H.BT.model_copy(update={"option_spread_pct": 0.01, "option_slippage_ticks": 3})
        _, row, _ = fill(db, alert=_option_alert("long"), frame=_option_frame(54000.0), bt=bt)
        assert row["entry_paise"] == 45465
        oracle = H.oracle_option_entry_paise(
            bt=bt,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type="CE",
            underlying_open=54000.0,
            fill_ts=H.at(10, 6),
            pricer=H.make_pricer(),
        )
        assert oracle == 45465

    def test_the_stock_percent_slippage_is_not_applied_to_an_option_premium(self, db):
        bt = H.BT.model_copy(update={"slippage_pct": 5.0})
        _, row, _ = fill(db, alert=_option_alert("long"), frame=_option_frame(54000.0), bt=bt)
        assert row["entry_paise"] == 45280

    @given(
        underlying_ticks=st.integers(min_value=1_070_000, max_value=1_090_000),
        stop_distance=st.integers(min_value=20, max_value=1_500),
        direction=st.sampled_from(["long", "short"]),
    )
    @PROP
    def test_property_the_option_entry_and_risk_equal_execute_trade(
        self, underlying_ticks, stop_distance, direction
    ):
        """For any underlying open and stop distance (some far enough to hit the one-tick floor),
        entry_paise and risk_paise equal what the lab's execute_trade books, to the paise."""
        underlying_open = round(underlying_ticks * 0.05, 2)  # an index level on the 0.05 grid
        underlying_stop = round(
            underlying_open - stop_distance
            if direction == "long"
            else underlying_open + stop_distance,
            2,
        )
        # the alert's target sits far beyond any open drawn here: a fill bar that opens at or
        # beyond the target is NO_FILL / GAP_THROUGH_TARGET (US-002 decision 3), not a fill
        far_target = underlying_open + 5_000 if direction == "long" else underlying_open - 5_000
        alert = _option_alert(direction, stop=underlying_stop, target=far_target)
        with H.temp_book(premium_at=H.make_pricer()) as (book, engine):
            H.open_trade(book, alert)
            book.step(H.at(10, 8), H.Feed(BANKNIFTY=_option_frame(underlying_open)))
            row = H.trade_row(engine, "O1")
        entry, risk = H.oracle_option_fill(
            bt=H.BT,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type="CE" if direction == "long" else "PE",
            underlying_open=underlying_open,
            underlying_stop=underlying_stop,
            fill_ts=H.at(10, 6),
            pricer=H.make_pricer(),
        )
        assert row["entry_paise"] == entry
        assert row["risk_paise"] == risk

    def test_a_pricer_that_returns_none_never_produces_a_zero_fill(self, db):
        book = H.make_book(db.engine, premium_at=lambda option, underlying, ts: None)
        H.open_trade(book, _option_alert("long"))
        try:
            book.step(H.at(10, 8), H.Feed(BANKNIFTY=_option_frame(54000.0)))
        except Exception as exc:
            pytest.fail(f"a failed repricing must not escape step: {type(exc).__name__}: {exc}")
        row = H.trade_row(db.engine, "O1")
        assert row["entry_paise"] is None or row["entry_paise"] > 0
        kinds = [H.val(e["kind"]) for e in H.event_rows(db.engine, "O1")]
        assert "PRICING_UNAVAILABLE" in kinds, kinds  # ADR-001 amendment 6: never a silent zero


class TestRiskIsSetAtTheFill:
    """Manager decision: risk_paise (excluding costs) is written when the trade fills.
    Stock: |entry - stop| x qty (0 is stored as NULL). Option, ALIGNED WITH THE LAB so paper R
    and Lab R are comparable: (entry premium - the stop exit fill execute_trade would book) x qty,
    i.e. stop_fair = premium_at(option, underlying_stop, fill ts), stop fill =
    round_to_tick(stop_fair*(1 - option_spread_pct) - option_slippage_ticks*tick, down) floored
    at one tick. When the stop cannot be repriced: NULL and a PRICING_UNAVAILABLE event."""

    def test_a_pending_trade_has_no_risk_yet(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        assert H.trade_row(db.engine, "A1")["risk_paise"] is None

    @pytest.mark.parametrize(
        "direction, stop, risk",
        [
            ("long", 990.0, 100030 - 99000),  # entry 1000.30 - stop 990.00 = 10.30 -> 1030 paise
            ("short", 1010.0, 101000 - 99970),  # stop 1010.00 - entry 999.70 = 10.30
            ("long", 999.95, 100030 - 99995),
        ],
    )
    def test_a_stock_risks_the_distance_from_the_filled_entry_to_the_stop(
        self, db, direction, stop, risk
    ):
        alert = H.make_alert("A1", direction=direction, stop=stop)
        _, row, _ = fill(db, alert=alert)
        assert row["risk_paise"] == risk, row
        assert H.typeof(db.engine, "risk_paise", "A1") == "integer"

    @pytest.mark.parametrize("direction, underlying_stop", [("long", 53850.0), ("short", 54150.0)])
    def test_an_option_risks_what_the_lab_would_book_at_the_stop_exit(
        self, db, direction, underlying_stop
    ):
        """Entry 452.80. The model premium at the stop is 450 -+ 0.65 x 150 = 352.50; the lab's
        stop exit is 352.50 x (1 - 0.006) - 2 x 0.05 = 350.285 -> down to the tick = 350.25."""
        pricer = H.make_pricer()
        alert = _option_alert(direction, stop=underlying_stop)
        _, row, _ = fill(db, alert=alert, frame=_option_frame(54000.0), premium_at=pricer)
        opt = "CE" if direction == "long" else "PE"
        assert pricer({"opt_type": opt}, underlying_stop, H.at(10, 6)) == pytest.approx(352.5)
        lab_entry, lab_risk = H.oracle_option_fill(
            bt=H.BT,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type=opt,
            underlying_open=54000.0,
            underlying_stop=underlying_stop,
            fill_ts=H.at(10, 6),
            pricer=H.make_pricer(),
        )
        assert (lab_entry, lab_risk) == (45280, 307650), "harness: the literal and the lab differ"
        assert row["entry_paise"] == 45280
        assert row["risk_paise"] == (45280 - 35025) * H.LOT == 307650
        assert H.typeof(db.engine, "risk_paise", "O1") == "integer"

    @pytest.mark.parametrize("direction, underlying_stop", [("long", 52000.0), ("short", 56000.0)])
    def test_the_option_stop_fill_is_floored_at_one_tick(self, db, direction, underlying_stop):
        """A stop so far away that the model premium is the pricer's 0.05 floor: the lab's stop
        fill would go below zero, so it is clamped at one tick (0.05) and risk is 452.75 x lot."""
        alert = _option_alert(direction, stop=underlying_stop)
        _, row, _ = fill(db, alert=alert, frame=_option_frame(54000.0), premium_at=H.make_pricer())
        _, lab_risk = H.oracle_option_fill(
            bt=H.BT,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type="CE" if direction == "long" else "PE",
            underlying_open=54000.0,
            underlying_stop=underlying_stop,
            fill_ts=H.at(10, 6),
            pricer=H.make_pricer(),
        )
        assert lab_risk == (45280 - 5) * H.LOT == 1358250, "harness"
        assert row["risk_paise"] == 1358250

    def test_the_options_risk_uses_the_configured_spread_and_ticks_like_the_lab(self, db):
        """option_spread_pct is a FRACTION (0.01 = 1%) and the slippage is in ticks (3): the stop
        fill is 352.50 x 0.99 - 0.15 = 348.825 -> 348.80 and the entry 454.65, so risk = 105.85 x lot."""
        bt = H.BT.model_copy(update={"option_spread_pct": 0.01, "option_slippage_ticks": 3})
        _, row, _ = fill(
            db, alert=_option_alert("long", stop=53850.0), frame=_option_frame(54000.0), bt=bt
        )
        assert row["entry_paise"] == 45465
        assert row["risk_paise"] == (45465 - 34880) * H.LOT == 317550

    def test_a_stock_with_no_distance_to_its_stop_stores_null_not_zero(self, db):
        """Zero slippage and a stop on the open: the stock fills (the open is not BEYOND the stop),
        entry == stop, and a risk of 0 would make R meaningless, so the column stays NULL."""
        bt = H.BT.model_copy(update={"slippage_pct": 0.0})
        for direction in ("long", "short"):
            alert = H.make_alert(f"Z-{direction}", direction=direction, stop=1000.0)
            _, row, events = fill(db, alert=alert, bt=bt)
            # the fill bar opens AT the stop, so US-002 may close the trade on that very bar:
            # the fill is judged on the FILLED event and the entry columns, never on NO_FILL
            assert len(H.fills(events)) == 1, direction
            H.assert_filled(row, entry=100000)
            assert "NO_FILL" not in H.kinds_of(db.engine, alert["id"]), direction
            assert row["risk_paise"] is None, direction
            assert H.typeof(db.engine, "risk_paise", alert["id"]) == "null"

    def test_a_stop_that_cannot_be_repriced_leaves_risk_null_and_says_so(self, db):
        base = H.make_pricer()

        def no_price_at_the_stop(option, underlying, ts):
            return None if underlying == 53850.0 else base(option, underlying, ts)

        _, row, _ = fill(
            db,
            alert=_option_alert("long", stop=53850.0),
            frame=_option_frame(54000.0),
            premium_at=no_price_at_the_stop,
        )
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 45280
        assert row["risk_paise"] is None
        kinds = [H.val(e["kind"]) for e in H.event_rows(db.engine, "O1")]
        assert "PRICING_UNAVAILABLE" in kinds, kinds


class TestLiveChainAlertsAreNotRebased:
    def test_the_fill_ignores_the_alerts_chain_premium_until_the_basis_story(self, db):
        """Manager: the live-chain basis is only recorded in plan at on_alert and applied in
        US-023. Here a chain-shaped alert (premium 480 against a model of 450) fills exactly like a
        synthetic one: premium_at at the fill bar, spread and ticks, no basis."""
        alert = _option_alert("long", premium=480.0)
        eco = alert["economics"]
        eco["synthetic"] = False
        eco["option"].update({"bid": 479.0, "ask": 480.0, "spread_pct": 0.21, "security_id": "1"})
        _, row, _ = fill(db, alert=alert, frame=_option_frame(54000.0))
        assert row["entry_paise"] == 45280


class TestTimezones:
    def test_now_in_utc_gives_the_same_fill_as_now_in_ist(self, db):
        _, ist_row, _ = fill(db, alert=H.make_alert("I1"))
        _, utc_row, _ = fill(
            db, alert=H.make_alert("U1"), now=H.at(10, 8).astimezone(dt.timezone.utc)
        )
        for col in ("entry_paise", "fill_bar_ts", "status", "entry_ts"):
            assert utc_row[col] == ist_row[col], col

    def test_bars_indexed_in_utc_give_the_same_fill_as_bars_indexed_in_ist(self, db):
        _, ist_row, _ = fill(db, alert=H.make_alert("I1"))
        utc_frame = H.standard_bars()
        utc_frame.index = utc_frame.index.tz_convert("UTC")
        _, utc_row, _ = fill(db, alert=H.make_alert("U1"), frame=utc_frame)
        for col in ("entry_paise", "fill_bar_ts", "status", "entry_ts"):
            assert utc_row[col] == ist_row[col], col


class TestSeriesAreIndependent:
    def test_a_series_without_bars_does_not_hold_up_another_series(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        H.open_trade(book, alert_id="R1", instrument_key="RELIANCE")
        events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars(), RELIANCE=None))
        assert [e.trade_id for e in events] == [H.trade_row(db.engine, "S1")["id"]]
        assert H.val(H.trade_row(db.engine, "R1")["status"]) == "PENDING_FILL"

    def test_each_trade_fills_from_its_own_series_bars(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="S1", instrument_key="SBIN", entry=1000.0)
        H.open_trade(book, alert_id="R1", instrument_key="RELIANCE", entry=2000.0)
        feed = H.Feed(
            SBIN=H.standard_bars((998.0, 1000.0, 1001.0)),
            RELIANCE=H.standard_bars((1998.0, 2000.0, 2001.0)),
        )
        book.step(H.at(10, 8), feed)
        assert H.trade_row(db.engine, "S1")["entry_paise"] == 100030
        assert H.trade_row(db.engine, "R1")["entry_paise"] == 200060  # 2000 * 1.0003 = 2000.60
