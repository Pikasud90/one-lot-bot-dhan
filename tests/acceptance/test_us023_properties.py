"""US-023 properties (hypothesis): the money rules hold over random trades, not just worked ones.

  P1  a random closed STOCK trade (long or short, any price) books exactly the oracle's costs and
      net, every column is an SQL integer, and a long is within 1 paisa of round_trip_cost
  P2  a random closed OPTION trade (random fake-pricer shape, both lots, all five exits, synthetic
      or live-chain with a random basis) equals lab.execution.execute_trade (run on a pricer
      shifted by the basis) to zero paise on entry, exit and gross and to 1 paisa on costs
  P3  a random book of closed trades: PaperLedger.totals equals a Decimal oracle EXACTLY (sums
      beyond 2**53, every grade and window) and is the same in every booking order
  P4  no look-ahead: the stored mark after a step at time t does not change when the bars after
      t are removed or altered; a delayed (Yahoo) trade marks on DATA time (now - feed delay)
  P5  the startup backfill books what the oracle says for any legacy rows, and a second start
      changes nothing

All run on the real PaperBook over a throw-away SQLite file per example; seeded
(derandomize=True), offline and fast.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W
from hypothesis import HealthCheck, Phase, assume, given, settings
from hypothesis import strategies as st

from onelot.clock import IST

PROP = settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    # no shrink phase: a red run reports the first falsifying example at once instead of
    # spending a minute minimising it (re-run with phases=tuple(Phase) to shrink)
    phases=(Phase.explicit, Phase.generate),
    suppress_health_check=list(HealthCheck),
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


# ── P1: stocks ────────────────────────────────────────────────────────────────────────
@PROP
@given(
    direction=st.sampled_from(["long", "short"]),
    price_ticks=st.integers(min_value=400, max_value=1_200_000),  # Rs 20.00 .. Rs 60,000.00
    hit=st.sampled_from(["target", "stop"]),
)
def test_P1_a_closed_stock_trade_books_the_oracles_costs_and_net(direction, price_ticks, hit):
    entry = price_ticks * 5 / 100
    with W.temp_db() as engine:
        _, row, _ = W.run_stock(engine, direction=direction, entry=entry, hit=hit)
        assert row["status"] == "CLOSED", row
        model = W.model_for("SBIN")
        e, x = row["entry_paise"], row["exit_paise"]
        costs = W.reject_ties(W.expected_costs, model, "STOCK", direction, e, x, 1)
        assert row["costs_paise"] == costs
        assert row["gross_paise"] == W.expected_gross("STOCK", direction, e, x, 1)
        assert row["net_paise"] == row["gross_paise"] - costs
        for column in ("entry_paise", "exit_paise", "gross_paise", "costs_paise", "net_paise"):
            assert W.typeofs(engine, column) == ["integer"], column
        if direction == "long":
            exact = W.round_trip_gap(model, e, x, 1)
            assert abs(Decimal(costs) - exact) <= 1
        else:
            first, second = W.sides("STOCK", "short")
            assert (first, second) == ("sell", "buy")


# ── P2: options, against the Lab ──────────────────────────────────────────────────────
@PROP
@given(
    name=st.sampled_from(sorted(W.OPTION_SCENARIOS)),
    instrument=st.sampled_from(["BANKNIFTY_OPT", "NIFTY_OPT"]),
    p0_ticks=st.integers(min_value=1000, max_value=18000),  # premium Rs 50.00 .. Rs 900.00
    delta=st.sampled_from([0.25, 0.4, 0.5, 0.65, 0.8]),
    basis_paise=st.sampled_from([0, 150, 525, 3000]),  # 0: a synthetic alert; else a chain alert
)
def test_P2_a_closed_option_trade_equals_the_labs_execute_trade(
    name, instrument, p0_ticks, delta, basis_paise
):
    direction, rows, _, _ = W.OPTION_SCENARIOS[name]
    p0 = p0_ticks * 5 / 100
    pricer = H.make_pricer(p0=p0, delta=delta)
    alert = U.make_alert(
        "O1",
        kind="INDEX",
        direction=direction,
        instrument_key=instrument,
        premium=p0 + basis_paise / 100,
    )
    alert["economics"]["synthetic"] = basis_paise == 0  # a live-chain alert records the basis
    with W.temp_db() as engine:
        _, row, events = W.run_option(engine, rows, alert=alert, pricer=pricer)
        assert row["status"] == "CLOSED"
        assert (row["plan"].get("pricing") or {}).get("basis_paise", 0) == basis_paise
        close = U.close_of(events)
        lab = W.lab_option_row(
            bt=H.BT,
            spec=W.SPECS[instrument],
            opt_type="CE" if direction == "long" else "PE",
            underlying_entry=54000.0,
            underlying_exit=close.price / 100,
            entry_ts=H.at(10, 6),
            exit_ts=close.at,
            pricer=W.shifted(H.make_pricer(p0=p0, delta=delta), basis_paise / 100),
            stop=alert["economics"]["underlying_stop"],
        )
        assert row["entry_paise"] == H.to_paise(lab["detail"]["premium_entry"])
        assert row["exit_paise"] == H.to_paise(lab["detail"]["premium_exit"])
        assert row["gross_paise"] == H.to_paise(lab["gross_pnl"])
        qty = W.SPECS[instrument].lot_size
        costs = W.reject_ties(
            W.expected_costs,
            W.model_for(instrument),
            "INDEX",
            direction,
            row["entry_paise"],
            row["exit_paise"],
            qty,
        )
        assert row["costs_paise"] == costs
        assert row["net_paise"] == row["gross_paise"] - costs
        assert abs(costs - H.to_paise(lab["costs"])) <= 1


# ── P3: the ledger ────────────────────────────────────────────────────────────────────
def at(day: int, hour: int = 10) -> dt.datetime:
    return dt.datetime.combine(W.IST_DAY + dt.timedelta(days=day), dt.time(hour, 0), tzinfo=IST)


@st.composite
def books(draw):
    items = []
    for i in range(draw(st.integers(min_value=0, max_value=14))):
        gross = draw(st.integers(min_value=-(10**17), max_value=10**17))
        costs = draw(st.integers(min_value=0, max_value=10**12))
        if gross - costs == 0:  # a zero net is a separate rule (test_us023_pinned_proposals)
            gross += 1
        kind = draw(st.sampled_from(["CLOSED"] * 6 + ["OPEN", "NO_FILL", "UNPRICED"]))
        grade = draw(st.sampled_from(["live", "delayed"]))
        ts = at(draw(st.integers(min_value=0, max_value=4)))
        alert_id = f"P{i:02d}"
        if kind == "CLOSED":
            items.append(W.booked(alert_id, gross, costs, ts=ts, grade=grade))
        elif kind == "OPEN":
            items.append(W.Booked(alert_id, "OPEN", None, None, None, grade, ts))
        elif kind == "NO_FILL":
            items.append(W.Booked(alert_id, "NO_FILL", 0, 0, 0, grade, ts))
        else:
            items.append(W.Booked(alert_id, "CLOSED", None, None, None, grade, ts))
    return items


@PROP
@given(
    items=books(),
    data=st.data(),
    grade=st.sampled_from([None, "live", "delayed"]),
    since_day=st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
    until_day=st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
)
def test_P3_ledger_totals_equal_the_decimal_oracle_in_every_booking_order(
    items, data, grade, since_day, until_day
):
    if since_day is not None and until_day is not None and since_day > until_day:
        since_day, until_day = until_day, since_day  # an inverted window is refused (AC2 file)
    since = None if since_day is None else at(since_day, 0)
    until = None if until_day is None else at(until_day, 0)
    shuffled = data.draw(st.permutations(items))
    with W.temp_db() as first, W.temp_db() as second:
        W.insert_booked(first, items)
        W.insert_booked(second, shuffled)
        want = W.oracle_totals(items, grade=grade, since=since, until=until)
        got_a = W.make_ledger(first).totals(grade=grade, since=since, until=until)
        got_b = W.make_ledger(second).totals(grade=grade, since=since, until=until)
        assert W.as_dict(got_a) == want
        assert got_a == got_b, "the same trades in another order: the same totals"
        assert all(type(getattr(got_a, f)) is int for f in W.LEDGER_FIELDS)
        assert got_a.net_paise == got_a.gross_paise - got_a.costs_paise
        assert got_a.wins + got_a.losses == got_a.trades


@PROP
@given(items=books())
def test_P3_the_grades_partition_the_whole(items):
    with W.temp_db() as engine:
        W.insert_booked(engine, items)
        ledger = W.make_ledger(engine)
        live, delayed, whole = (
            ledger.totals(grade="live"),
            ledger.totals(grade="delayed"),
            ledger.totals(),
        )
        for field in W.LEDGER_FIELDS:
            assert getattr(live, field) + getattr(delayed, field) == getattr(whole, field), field


# ── P4: no look-ahead in the mark ─────────────────────────────────────────────────────
@st.composite
def option_paths(draw):
    """Closes 54000 +- 120 on the 5-paise grid: far from 53850 / 54150 / 54300 / 53700."""
    n = draw(st.integers(min_value=3, max_value=10))
    steps = draw(st.lists(st.integers(-2400, 2400), min_size=n, max_size=n))
    rows, prev = [], 5400000
    for step in steps:
        close = 5400000 + step * 5
        rows.append((prev, max(prev, close) + 10, min(prev, close) - 10, close))
        prev = close
    return rows


@PROP
@given(
    rows=option_paths(),
    cut=st.integers(min_value=0, max_value=8),
    source=st.sampled_from(["dhan", "yahoo"]),
    alter=st.integers(min_value=-2000, max_value=2000),
    direction=st.sampled_from(["long", "short"]),
    chain=st.booleans(),
)
def test_P4_the_mark_never_looks_past_the_data_time(rows, cut, source, alter, direction, chain):
    k = min(cut, len(rows) - 1)
    if chain:  # a live-chain alert: the basis (+525 paise) rides on every mark
        alert = W.chain_alert(direction=direction, source=source)
    else:
        alert = U.make_alert("O1", kind="INDEX", direction=direction, source=source)
    # bar i opens 10:06 + i and closes a minute later; step when bar k has just closed, on DATA
    # time: a delayed trade's data lags the wall clock by feed_delay (15 minutes)
    now = H.at(10, 7) + dt.timedelta(minutes=k)
    if source == "yahoo":
        now += dt.timedelta(minutes=15)
    truncated = rows[: k + 1]
    last = rows[k][3]
    changed = last + alter * 5 if alter else last + 5  # a different close, always
    future = [(last, max(last, changed) + 10, min(last, changed) - 10, changed)] * (
        len(rows) - k - 1
    )
    altered = rows[: k + 1] + future
    outcomes = []
    for frame_rows in (truncated, rows, altered):
        with W.temp_db() as engine:
            book = W.make_book(engine, premium_at=H.make_pricer())
            H.open_trade(book, alert)
            frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *frame_rows])
            U.step_book(book, frame, now, series="BANKNIFTY")
            row = U.row_of(engine, "O1")
            assume(row["status"] == "OPEN")
            outcomes.append(
                (
                    W.mark_of(engine),
                    W.position_of(engine)["last_close_paise"],
                    len(H.event_rows(engine)),
                )
            )
    assert outcomes[0] == outcomes[1] == outcomes[2], outcomes
    mark, last_close, _ = outcomes[0]
    assert last_close == rows[k][3]
    assert mark == W.fake_mark(alert, rows[k][3], 525 if chain else 0)


# ── P5: the backfill ──────────────────────────────────────────────────────────────────
@PROP
@given(gross=st.lists(st.integers(min_value=-200_000, max_value=200_000), min_size=1, max_size=20))
def test_P5_the_backfill_books_the_oracles_costs_once(gross):
    legacy = [
        W.Booked(f"L{i:02d}", "CLOSED", g, None, None, "live", at(1)) for i, g in enumerate(gross)
    ]
    with W.temp_db() as engine:
        W.insert_booked(engine, legacy)
        W.make_book(engine)
        model = W.model_for("SBIN")
        rows = H.trade_rows(engine)
        for row in rows:
            costs = W.reject_ties(
                W.expected_costs, model, "STOCK", "long", row["entry_paise"], row["exit_paise"], 1
            )
            assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)
        once = W.rows_without(engine, "updated_at")
        W.make_book(engine)
        assert W.rows_without(engine, "updated_at") == once
