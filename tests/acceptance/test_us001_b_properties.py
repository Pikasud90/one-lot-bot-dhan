"""US-001 mode B: properties, hostile alerts and the remaining boundaries.

* an independent oracle of the whole fill rule (written here from the story and ADR text, not
  from the code) is run against random hostile frames: holes, duplicates, unsorted rows,
  NaN / inf / zero / negative / sub-paise opens, mixed spacing, random `now`, live and delayed
  alerts, long and short; step must agree with it on every outcome, price and bar, never
  raise, never touch the caller's frame, and stay quiet when repeated;
* the 15:30 session-end boundary at 1m and 2m resolution;
* hostile ALERTS (missing confidence / data, an option-less index alert, odd ids);
* a lot that disagrees with the spec is kept and warned about; a trade's plan is a copy;
* failures inside step (a database error, a bars() that raises) are contained.
"""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd
import pytest
import us001_helpers as H
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.clock import IST

PROP = settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
NAN, INF = float("nan"), float("inf")
MIN = dt.timedelta(minutes=1)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


# ── the oracle ────────────────────────────────────────────────────────────────────────
def usable(open_price: float) -> bool:
    return (
        open_price == open_price
        and abs(open_price) != INF
        and open_price > 0
        and H.to_paise(open_price) > 0
    )


def oracle(
    rows,
    *,
    created,
    expires,
    source,
    feed_delay,
    res,
    now,
    direction,
    stop,
    target,
    exit_time=dt.time(15, 15),
):
    """What the story says a waiting trade does at `now` over `rows` [(open ts, open price)].

    ('fill', ts, open) | ('gap', ts, open) | ('gap_target', ts, open) | ('session_end', ts,
    open) | ('no_fill',) | ('wait',), plus the grade. The first eligible bar decides: opening at
    or after the strategy's `exit_time` on the alert's date is NO_FILL 'session_end' (US-002
    review 1 MINOR-2); its open strictly beyond the stop is GAP_THROUGH_STOP; its open at or
    beyond the TARGET, or the SLIPPED entry (open +- 0.03 %, snapped against the trader) at or
    beyond it, is GAP_THROUGH_TARGET (decision 3 and MAJOR-2). `stop` and `target` are paise,
    on the tick grid (stock targets are snapped against the trader, so these already are)."""
    last: dict[dt.datetime, float] = {}
    for ts, price in rows:  # a duplicated time: the LAST row wins
        last[ts] = price
    bars = sorted(last.items())
    step = dt.timedelta(minutes=res)
    closed = [(ts, p) for ts, p in bars if ts + step <= now]
    day = created.astimezone(IST).date()
    on_day = [ts for ts, _ in closed if ts.astimezone(IST).date() == day]
    wide = [(b - a) > step for a, b in zip(on_day, on_day[1:], strict=False)]
    mixed = any(x and y for x, y in zip(wide, wide[1:], strict=False))
    delayed = str(source).lower().startswith("yahoo") or mixed
    deadline = expires + (dt.timedelta(minutes=feed_delay) if delayed else dt.timedelta(0))
    close_at = dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST)
    midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=IST)
    session_end = close_at if created < close_at else midnight
    grade = "delayed" if delayed else "live"
    for ts, price in closed:
        if (
            created < ts <= deadline
            and ts < session_end  # a bar opening AT the close never fills (before-close alerts)
            and ts.astimezone(IST).date() == day
            and usable(price)
        ):
            paise = H.to_paise(price)
            sign = 1 if direction == "long" else -1
            if ts >= dt.datetime.combine(day, exit_time, tzinfo=IST):
                return ("session_end", ts, price), grade
            if (paise - stop) * sign < 0:
                return ("gap", ts, price), grade
            if (paise - target) * sign >= 0:
                return ("gap_target", ts, price), grade
            if (H.expected_stock_entry(price, direction) - target) * sign >= 0:
                return ("gap_target", ts, price), grade  # the slipped entry is past the target
            return ("fill", ts, price), grade
    if closed:
        last_ts = closed[-1][0]
        if last_ts >= min(deadline, midnight) or last_ts + step >= session_end:
            return ("no_fill",), grade
    return ("wait",), grade


@st.composite
def scenarios(draw):
    res = draw(st.sampled_from([1, 2]))
    # the grid runs to 15:39: bars opening at or after the 15:30 close are part of the picture
    grid = [H.at(9, 15) + dt.timedelta(minutes=res * i) for i in range((375 + 10) // res)]
    n = len(grid)
    lo = draw(st.integers(0, n - 1))
    hi = draw(st.integers(lo, min(n - 1, lo + 40)))
    holes = draw(st.sets(st.integers(lo, hi), max_size=6))
    # Hypothesis favours the ends of an integer range, so the levels that matter are listed:
    # a tick either side of (and exactly on) the stops 995 / 1005 and the targets 1005 / 995 /
    # 1020 / 980 the scenarios use, so every boundary rule is met, not just approached
    boundary = [994.95, 995.0, 995.05, 1004.95, 1005.0, 1005.05, 1019.95, 1020.0, 1020.05]
    boundary += [979.95, 980.0, 980.05, 1000.0]
    price = st.one_of(
        st.sampled_from(boundary),
        st.sampled_from(boundary),
        st.integers(99_000, 101_000).map(lambda p: p / 100),
        st.sampled_from([NAN, INF, -INF, 0.0, -1.0, 0.004]),
    )
    kept = [i for i in range(lo, hi + 1) if i not in holes]
    rows = [(grid[i], draw(price)) for i in kept]
    for j in draw(st.lists(st.integers(0, max(len(kept) - 1, 0)), max_size=3 if kept else 0)):
        rows.append((grid[kept[j]], draw(price)))  # duplicated timestamps
    created_idx = draw(st.integers(max(lo - 3, 0), hi))
    created = grid[created_idx] + dt.timedelta(seconds=draw(st.sampled_from([0, 3, 59])))
    now_idx = draw(st.integers(min(created_idx, n - 1), n - 1))
    now = grid[now_idx] + dt.timedelta(seconds=draw(st.sampled_from([0, 30])))
    if draw(st.booleans()):
        now = max(now, H.at(15, 30) + dt.timedelta(minutes=draw(st.integers(0, 300))))
    order = draw(st.permutations(range(len(rows))))
    return {
        "res": res,
        "rows": [rows[i] for i in order],
        "created": created,
        "ttl": draw(st.sampled_from([1, 5, 10, 30])),
        "source": draw(st.sampled_from(["dhan", "yahoo"])),
        "feed_delay": draw(st.sampled_from([0, 15])),
        "direction": draw(st.sampled_from(["long", "short"])),
        "near_target": draw(st.booleans()),  # inside the 990..1010 price range, or far away
        "now": now,
    }


class TestTheFillRuleAgainstAnIndependentOracle:
    @given(scenarios())
    @PROP
    def test_property_step_agrees_with_the_oracle_on_hostile_frames(self, sc):
        direction = sc["direction"]
        stop_price = 995.0 if direction == "long" else 1005.0
        long_ = direction == "long"
        target_price = (
            (1005.0 if long_ else 995.0) if sc["near_target"] else (1020.0 if long_ else 980.0)
        )
        alert = H.make_alert(
            "O1",
            direction=direction,
            created=sc["created"],
            ttl_min=sc["ttl"],
            source=sc["source"],
            resolution=f"{sc['res']}m",
            stop=stop_price,
            target=target_price,
        )
        rows = sc["rows"]
        frame = H.bars_at([ts for ts, _ in rows], [p for _, p in rows]) if rows else None
        frozen = None if frame is None else frame.copy(deep=True)
        expected, grade = oracle(
            rows,
            created=sc["created"],
            expires=sc["created"] + dt.timedelta(minutes=sc["ttl"]),
            source=sc["source"],
            feed_delay=sc["feed_delay"],
            res=sc["res"],
            now=sc["now"],
            direction=direction,
            stop=H.to_paise(stop_price),
            target=H.to_paise(target_price),
        )
        with H.temp_book(feed_delay=sc["feed_delay"]) as (book, engine):
            H.open_trade(book, alert)
            events = book.step(sc["now"], H.Feed(SBIN=frame))
            row = H.trade_row(engine, "O1")
            again = book.step(sc["now"], H.Feed(SBIN=frame))
            assert again == [], "a repeated step booked something new"
            assert H.trade_row(engine, "O1") == row
            kinds = [H.val(e["kind"]) for e in H.event_rows(engine, "O1")]
        if frame is not None:
            pd.testing.assert_frame_equal(frame, frozen)  # the caller's frame is never touched
        status, reason = H.val(row["status"]), row["close_reason"]
        assert H.val(row["grade"]) == grade, (sc, expected)
        kind = expected[0]
        if kind == "fill":
            _, ts, price = expected
            # filled; the same step may already have managed it on to a close (US-002), so the
            # fill is judged on the entry columns, and a close reason only if it closed
            H.assert_filled(row, at_ts=ts, entry=H.expected_stock_entry(price, direction))
            assert (status == "OPEN") is (reason is None), (sc, expected, row)
            assert kinds.count("FILLED") == 1 and "NO_FILL" not in kinds
        elif kind == "gap":
            assert (status, reason) == ("NO_FILL", "GAP_THROUGH_STOP"), (sc, expected, row)
            assert row["entry_paise"] is None and row["exit_paise"] is None
        elif kind == "gap_target":
            assert (status, reason) == ("NO_FILL", "GAP_THROUGH_TARGET"), (sc, expected, row)
            assert row["entry_paise"] is None and row["exit_paise"] is None
            assert row["costs_paise"] == 0 == row["net_paise"] and "FILLED" not in kinds
        elif kind == "session_end":
            assert (status, reason) == ("NO_FILL", "NO_FILL"), (sc, expected, row)
            assert row["entry_paise"] is None and row["exit_paise"] is None
            assert row["costs_paise"] == 0 == row["net_paise"] and "FILLED" not in kinds
            assert kinds.count("NO_FILL") == 1
        elif kind == "no_fill":
            assert (status, reason) == ("NO_FILL", "NO_FILL"), (sc, expected, row)
            assert row["entry_paise"] is None and row["costs_paise"] == 0 == row["net_paise"]
        else:
            assert status == "PENDING_FILL" and row["entry_paise"] is None, (sc, expected, row)
            assert not any(k in kinds for k in ("FILLED", "NO_FILL"))
        assert [H.val(e.kind) for e in events if H.val(e.kind) in ("FILLED", "NO_FILL")] == [
            k for k in kinds if k in ("FILLED", "NO_FILL")
        ]


class TestTheSessionEndBoundary:
    @pytest.mark.parametrize(
        "res, created, now, status",
        [
            # 1-minute bars: the last session bar opens 15:29 and closes at 15:30
            (1, H.at(15, 28, 30), H.at(15, 30), "FILLED"),  # the 15:29 bar fills it
            (1, H.at(15, 28, 30), H.at(15, 29, 59), "PENDING_FILL"),  # 15:29 not closed yet
            (1, H.at(15, 29, 1), H.at(15, 30), "NO_FILL"),  # no bar can follow: session over
            (1, H.at(15, 29, 1), H.at(15, 29, 59), "PENDING_FILL"),
            # 2-minute bars: the last one opens 15:28 and closes at 15:30
            (2, H.at(15, 27, 3), H.at(15, 30), "FILLED"),
            (2, H.at(15, 27, 3), H.at(15, 29, 59), "PENDING_FILL"),
            (2, H.at(15, 28, 3), H.at(15, 30), "NO_FILL"),
        ],
        ids=[
            "1m-fills-last",
            "1m-last-open",
            "1m-none-left",
            "1m-none-left-early",
            "2m-fills-last",
            "2m-last-open",
            "2m-none-left",
        ],
    )
    def test_the_last_bar_of_the_session_fills_and_nothing_fills_after_it(
        self, db, res, created, now, status
    ):
        last_open = H.at(15, 30) - dt.timedelta(minutes=res)
        first = H.at(15, 20)
        stamps = []
        t = first
        while t <= last_open:
            stamps.append(t)
            t += dt.timedelta(minutes=res)
        frame = H.bars_at(stamps, [1000.0] * len(stamps))
        book = H.make_book(db.engine)
        alert = H.make_alert("A1", created=created, resolution=f"{res}m")
        alert["manage"]["exit_time"] = "15:30"  # the 15:30 boundary is under test, not exit_time
        H.open_trade(book, alert)
        book.step(now, H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, "A1")
        if status == "FILLED":  # then SESSION_END closes it in the same step (US-002)
            H.assert_filled(row)
        else:
            assert H.val(row["status"]) == status


class TestHostileAlerts:
    def test_a_missing_confidence_opens_with_a_null_score(self, db):
        for i, conf in enumerate((None, {}, {"score": None}, {"score": NAN}, {"score": "n/a"})):
            alert = H.make_alert(f"C{i}")
            alert["confidence"] = conf
            assert H.open_trade(H.make_book(db.engine), alert) is not None
            assert H.trade_row(db.engine, f"C{i}")["confidence"] is None

    def test_an_alert_with_no_data_block_is_graded_delayed(self, db):
        alert = H.make_alert("A1")
        del alert["data"]
        trade = H.open_trade(H.make_book(db.engine), alert)
        row = H.trade_row(db.engine, "A1")
        assert H.val(trade.grade) == "delayed" and row["data_source"] == ""
        assert row["resolution_min"] is None  # step then infers it from the bars

    def test_a_trade_with_no_declared_resolution_infers_it_from_the_bars(self, db):
        alert = H.make_alert("A1")
        del alert["data"]["resolution"]
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = H.minute_bars(H.at(10, 4), [999.0, 1000.0, 1001.0, 1002.0], step=2)
        book.step(H.at(10, 8), H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "OPEN" and row["resolution_min"] == 2

    def test_an_index_alert_without_an_option_opens_with_the_spec_lot_and_never_fills(self, db):
        alert = H.make_alert("O1", kind="INDEX")
        alert["economics"]["option"] = {}
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert)
        assert trade.qty == H.LOT
        book.step(H.at(10, 8), H.Feed(BANKNIFTY=H.minute_bars(H.at(10, 5), [54000.0] * 3)))
        row = H.trade_row(db.engine, "O1")
        assert H.val(row["status"]) == "PENDING_FILL" and row["entry_paise"] is None
        assert "PRICING_UNAVAILABLE" in [H.val(e["kind"]) for e in H.event_rows(db.engine, "O1")]

    def test_numpy_scalars_inside_an_alert_are_stored_as_plain_integers_and_json(self, db):
        alert = H.make_alert("N1", kind="INDEX")
        eco = alert["economics"]
        eco["qty"] = np.int64(150)
        eco["option"]["lot"] = np.int64(H.LOT)
        eco["underlying_stop"] = np.float32(53850.0)
        eco["entry_price"] = np.float64(450.0)
        alert["risk_allowed"] = np.bool_(True)
        alert["confidence"]["score"] = np.float32(61.5)
        trade = H.open_trade(H.make_book(db.engine), alert)
        assert trade.qty == H.LOT and type(trade.qty) is int
        assert H.typeof(db.engine, "qty", "N1") == "integer"
        plan = H.trade_row(db.engine, "N1")["plan"]  # round-tripped through JSON
        assert plan["levels"]["underlying_stop_paise"] == 5_385_000
        assert plan["alert"]["risk_allowed"] is True

    @pytest.mark.parametrize(
        "alert_id",
        ["0123456789ab", 12345, "α-1", "x" * 64, " A1 ", "a b", "a'b\"c;--"],
        ids=["hex12", "int", "unicode", "64-chars", "padded", "space", "quotes"],
    )
    def test_odd_alert_ids_open_one_row_and_stay_idempotent(self, db, alert_id):
        book = H.make_book(db.engine)
        alert = H.make_alert("X")
        alert["id"] = alert_id
        first = book.on_alert(alert, H.CREATED)
        again = book.on_alert(dict(alert), H.CREATED)
        assert first is not None and again is not None and again.id == first.id
        assert len(H.trade_rows(db.engine)) == 1
        assert first.alert_id == str(alert_id).strip()

    @pytest.mark.parametrize("bad_id", [None, "", "   "])
    def test_an_alert_without_a_usable_id_is_refused_and_writes_nothing(self, db, bad_id):
        alert = H.make_alert("X")
        alert["id"] = bad_id
        with pytest.raises(ValueError):
            H.make_book(db.engine).on_alert(alert, H.CREATED)
        assert H.count_rows(db.engine, "paper_trades") == 0

    def test_an_alert_that_is_not_a_mapping_is_refused_not_crashed_on(self, db):
        for junk in (None, [], "alert", 42):
            with pytest.raises((ValueError, TypeError, AttributeError)):
                H.make_book(db.engine).on_alert(junk, H.CREATED)  # type: ignore[arg-type]
        assert H.count_rows(db.engine, "paper_trades") == 0


class TestLotsAndPlans:
    def test_an_option_lot_that_disagrees_with_the_spec_is_kept_and_warned_about(self, db, caplog):
        alert = H.make_alert("O1", kind="INDEX", lot=25)  # an exchange lot revision the spec missed
        assert H.SPECS["BANKNIFTY_OPT"].lot_size == H.LOT
        with caplog.at_level(logging.WARNING):
            trade = H.open_trade(H.make_book(db.engine), alert)
        assert trade.qty == 25
        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("O1" in m and "25" in m for m in warned), warned

    def test_a_matching_lot_is_not_warned_about(self, db, caplog):
        with caplog.at_level(logging.WARNING):
            H.open_trade(H.make_book(db.engine), alert_id="O1", kind="INDEX")
        assert not [r for r in caplog.records if "lot" in r.getMessage().lower()]

    def test_the_plan_is_the_alert_snapshot_plus_integer_paise_levels(self, db):
        alert = H.make_alert("A1", entry=1000.05, stop=990.01, target=1020.5)
        trade = H.open_trade(H.make_book(db.engine), alert)
        plan = trade.plan
        assert plan["alert"]["id"] == "A1" and plan["alert"]["strategy"] == "ema_cross"
        assert plan["levels"] == {
            "underlying_entry_paise": 100005,
            "underlying_stop_paise": 99001,
            "underlying_target_paise": 102050,
        }
        assert all(type(v) is int for v in plan["levels"].values())

    def test_changing_a_plan_a_trade_handed_out_changes_nothing_in_the_book(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1")
        plan = trade.plan
        plan["alert"]["economics"]["underlying_stop"] = 1.0
        plan["levels"]["underlying_stop_paise"] = 100
        again = book.open_trades()[0].plan
        assert again["levels"]["underlying_stop_paise"] == 99000
        assert again["alert"]["economics"]["underlying_stop"] == 990.0
        # and it still fills against the ORIGINAL stop
        book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars((995.0, 995.0, 996.0))))
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "OPEN"


class TestFailuresInsideStepAreContained:
    def test_a_database_error_in_step_is_counted_not_raised(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        with H.failing_sql(db.engine, "paper_trades") as injected:
            events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))  # must not raise
        assert injected["hits"] >= 1 and events == []
        assert book.status()["hook_errors"] == 1 and book.status()["filled"] == 0
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "PENDING_FILL"

    def test_one_failing_trade_does_not_stop_the_others(self, db):
        book = H.make_book(db.engine)
        bad = H.open_trade(book, alert_id="BAD", instrument_key="SBIN")
        good = H.open_trade(book, alert_id="OK", instrument_key="RELIANCE")
        feed = H.Feed(SBIN=H.standard_bars(), RELIANCE=H.standard_bars())

        def only_the_bad_trade(statement, parameters):
            # UPDATE ... WHERE id = ? AND status = ? AND grade = ?: the id is third from last
            return statement.startswith("update") and tuple(parameters)[-3] == bad.id

        with H.failing_sql(db.engine, "paper_trades", when=only_the_bad_trade) as injected:
            events = book.step(H.at(10, 8), feed)
        assert injected["hits"] == 1
        assert [(e.trade_id, H.val(e.kind)) for e in events] == [(good.id, "FILLED")]
        assert H.val(H.trade_row(db.engine, "OK")["status"]) == "OPEN"
        assert H.val(H.trade_row(db.engine, "BAD")["status"]) == "PENDING_FILL"
        assert book.status()["hook_errors"] == 1 and book.status()["filled"] == 1

    @pytest.mark.parametrize(
        "raiser",
        [
            pytest.param(
                lambda key: (_ for _ in ()).throw(RuntimeError("feed down")), id="runtime"
            ),
            pytest.param(lambda key: (_ for _ in ()).throw(KeyError(key)), id="key"),
            pytest.param(lambda key: [1, 2, 3], id="list"),
            pytest.param(lambda key: {"open": [1]}, id="dict"),
            pytest.param(lambda key: "bars", id="str"),
            pytest.param(lambda key: 42, id="int"),
            pytest.param(lambda key: pd.Series([1.0, 2.0]), id="series"),
        ],
    )
    def test_a_bars_callable_that_fails_or_returns_junk_never_escapes_step(self, db, raiser):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        H.open_trade(book, alert_id="R1", instrument_key="RELIANCE")

        def bars(key):
            return H.standard_bars() if key == "RELIANCE" else raiser(key)

        events = book.step(H.at(10, 8), bars)
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.val(H.trade_row(db.engine, "R1")["status"]) == "OPEN"
        assert H.val(H.trade_row(db.engine, "S1")["status"]) == "PENDING_FILL"

    def test_a_now_before_the_alert_fills_nothing_and_raises_nothing(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        assert book.step(H.at(9, 0), H.Feed(SBIN=H.standard_bars())) == []
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "PENDING_FILL"
