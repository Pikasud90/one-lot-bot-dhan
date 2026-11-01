"""PaperBook.step edges (US-001 AC2/AC3, ADR-001 amendments 3, 5, 6): boundaries and failures.

The pure rule lives in `onelot.paper.fill`; these tests pin its money arithmetic exactly,
the at-the-stop boundary, data time vs the session close, a pricer that fails, and that a
fill or NO_FILL commits with its event or not at all.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, event

from onelot.paper.fill import (
    Bars,
    FillDecision,
    decide_fill,
    normalise_bars,
    stock_entry_paise,
    to_ns,
)
from onelot.store.schema import paper_events, paper_trades

from .conftest import THU, FakePricer, ist, make_alert, make_bars, table_rows

FRI = dt.date(2026, 10, 30)


def series(**frames: pd.DataFrame | None) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


def the_row(engine: Engine) -> dict:
    (row,) = table_rows(engine, paper_trades)
    return row


def kinds(engine: Engine) -> list[str]:
    return [e["kind"] for e in table_rows(engine, paper_events)]


@contextlib.contextmanager
def failing_writes(engine: Engine, table: str) -> Iterator[None]:
    """Every INSERT/UPDATE on `table` raises while the block runs."""

    def boom(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        words = statement.lstrip().lower()
        if words.split(None, 1)[0] in ("insert", "update") and table in words:
            raise RuntimeError(f"injected failure on {table}")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", boom)


# ── exact money arithmetic (amendment 5: paise once, snap against the trader) ────


@pytest.mark.parametrize(
    ("open_paise", "direction", "pct", "tick", "want"),
    [
        (100000, "long", 0.03, 5, 100030),  # 1000 x 1.0003 is 1000.3000000000001 in float
        (100000, "short", 0.03, 5, 99970),
        (100010, "long", 0.03, 5, 100045),  # 1000.40003 -> UP
        (100010, "short", 0.03, 5, 99975),  # 999.79997 -> DOWN
        (100000, "long", 0.1, 5, 100100),  # 0.1 is inexact in binary; the percent is exact
        (12345, "long", 0.03, 1, 12349),  # a 1-paise tick: 12348.7035 -> UP
        (12345, "short", 0.03, 1, 12341),  # 12341.2965 -> DOWN
        (100003, "long", 0.0, 5, 100005),  # an off-tick open snaps even with no slippage
        (100003, "short", 0.0, 5, 100000),
        (100005, "long", 0.0, 5, 100005),  # on the tick: unchanged
    ],
)
def test_stock_entry_is_exact_and_against_the_trader(open_paise, direction, pct, tick, want):
    got = stock_entry_paise(open_paise, direction, pct, tick)
    assert got == want and type(got) is int


# ── the stop boundary (Manager decision 4: only STRICTLY beyond the stop is a gap) ─


@pytest.mark.parametrize(
    ("direction", "stop", "status", "entry", "risk"),
    [
        ("long", 1000.00, "OPEN", 100030, 30),  # AT the stop: fills, risk is the slippage
        ("short", 1000.00, "OPEN", 99970, 30),
        ("long", 1000.05, "NO_FILL", None, None),  # one tick beyond: gap
        ("short", 999.95, "NO_FILL", None, None),
        ("long", 999.95, "OPEN", 100030, 35),  # one tick inside
        ("short", 1000.05, "OPEN", 99970, 35),
    ],
)
def test_the_stop_boundary_on_the_fill_bar(make_book, engine, direction, stop, status, entry, risk):
    book = make_book()
    book.on_alert(make_alert("STOCK", direction, stop=stop), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2)))
    row = the_row(engine)
    assert (row["status"], row["entry_paise"], row["risk_paise"]) == (status, entry, risk)
    if status == "NO_FILL":
        assert row["close_reason"] == "GAP_THROUGH_STOP"


def test_an_open_at_the_stop_with_zero_slippage_fills_with_risk_null_not_zero(make_book, engine):
    """R = net / risk: a zero risk would divide by zero downstream, so it is stored NULL."""
    from onelot.config import BacktestCfg

    book = make_book(bt=BacktestCfg(slippage_pct=0.0, option_spread_pct=0.006))
    book.on_alert(make_alert("STOCK", "long", stop=1000.00), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2)))
    row = the_row(engine)
    assert (row["status"], row["entry_paise"], row["risk_paise"]) == ("OPEN", 100000, None)


# ── data time and the session (amendment 3) ─────────────────────────────────────


def bars_of(frame: pd.DataFrame) -> Bars:
    view = normalise_bars(frame)
    assert view is not None
    return view


def decide(frame: pd.DataFrame, created: dt.datetime, now: dt.datetime, ttl_min: int = 10, **kw):
    args = {"resolution_min": 1, "direction": "long", "stop_paise": 1}
    return decide_fill(
        bars_of(frame),
        created_at=created,
        deadline=created + dt.timedelta(minutes=ttl_min),
        now=now,
        **(args | kw),
    )


def test_data_jumping_to_the_next_session_says_expired_when_the_ttl_ended_first():
    nxt = THU + dt.timedelta(days=1)
    frame = pd.concat(
        [make_bars(ist(THU, 10, 0), [1000.0] * 5), make_bars(ist(nxt, 9, 15), [1000.0] * 3)]
    )
    d = decide(frame, ist(THU, 10, 5, 3), ist(nxt, 9, 20))
    assert (d.outcome, d.reason, d.data_through) == ("no_fill", "expired", ist(nxt, 9, 18))


@pytest.mark.parametrize(
    ("last_open", "outcome", "reason"),
    [((15, 24), "wait", None), ((15, 29), "no_fill", "session_closed")],
    ids=["data-at-1525-waits", "data-at-1530-closes"],
)
def test_a_ttl_running_past_the_close_ends_when_the_data_reaches_1530(last_open, outcome, reason):
    created = ist(FRI, 15, 29, 3)  # TTL 60 min: only the session can end it
    n = last_open[1] - 20 + 1
    frame = make_bars(ist(FRI, 15, 20), [1000.0] * n)
    d = decide(frame, created, ist(FRI, 15, 45), ttl_min=60)  # the wall clock is past 15:30
    assert (d.outcome, d.reason) == (outcome, reason)


def test_an_alert_raised_after_the_close_is_not_closed_by_the_clock_alone():
    """A special evening session: bars after 15:30 still fill an alert raised then."""
    created = ist(THU, 18, 0, 3)
    d = decide(make_bars(ist(THU, 18, 0), [1000.0] * 4), created, ist(THU, 18, 3))
    assert (d.outcome, d.bar_ts) == ("fill", ist(THU, 18, 1))


@pytest.mark.parametrize("ttl_min", [10, 3 * 24 * 60], ids=["ttl-10", "ttl-to-monday"])
def test_a_bar_opening_at_the_1530_close_never_fills(ttl_min):
    """Review round 1, MINOR-1: a feed that prints a 15:30 bar (a closing-auction print)
    must not fill an alert raised before the close; the window ends AT 15:30."""
    created = ist(FRI, 15, 29, 10)
    frame = make_bars(ist(FRI, 15, 25), [1000.0] * 6)  # 15:25 .. 15:30
    d = decide(frame, created, ist(FRI, 15, 31, 5), ttl_min=ttl_min)
    assert (d.outcome, d.reason, d.bar_ts) == ("no_fill", "session_closed", None)


def test_the_reviewers_probe_closes_no_fill_session_closed_not_filled_at_1530(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(created=ist(FRI, 15, 29, 10)), ist(FRI, 15, 29, 11))
    events = book.step(ist(FRI, 15, 31, 5), series(RELIANCE=make_bars(ist(FRI, 15, 25), [1e3] * 6)))
    row = the_row(engine)
    assert (row["status"], row["close_reason"], row["fill_bar_ts"]) == ("NO_FILL", "NO_FILL", None)
    assert [(e.kind, e.detail["reason"]) for e in events] == [("NO_FILL", "session_closed")]


def test_a_bar_just_before_the_close_still_fills():
    created = ist(FRI, 15, 28, 10)
    frame = make_bars(ist(FRI, 15, 25), [1000.0] * 6)  # 15:29 is the last eligible bar
    d = decide(frame, created, ist(FRI, 15, 31, 5))
    assert (d.outcome, d.bar_ts) == ("fill", ist(FRI, 15, 29))


def test_with_no_closed_bar_yet_the_trade_waits_and_knows_no_data_time():
    """fill.decide_fill: nothing has closed by `now` (review round 1, MINOR-5)."""
    created = ist(THU, 10, 5, 3)
    frame = make_bars(ist(THU, 10, 6), [1000.0] * 3)  # 10:06 closes at 10:07
    d = decide(frame, created, ist(THU, 10, 6, 59))
    assert d == FillDecision("wait")
    assert (d.window_closed, d.data_through, d.bar_ts) == (None, None, None)


def test_an_unusable_bar_on_the_deadline_closes_the_window_without_a_price():
    created = ist(THU, 10, 5)  # deadline 10:15:00 exactly: that bar is the last chance
    d = decide(make_bars(ist(THU, 10, 15), [float("nan")]), created, ist(THU, 10, 30))
    assert (d.outcome, d.reason, d.bar_open_paise) == ("no_fill", "expired", None)


def test_resolution_is_inferred_from_the_bars_when_the_alert_does_not_say(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(resolution="unknown"), ist(THU, 10, 5, 4))
    bars = series(RELIANCE=make_bars(ist(THU, 10, 4), [1000.0] * 3, res_min=2))  # 10:04, :06, :08
    assert book.step(ist(THU, 10, 7, 59), bars) == []  # the 10:06 2m bar closes at 10:08
    assert [e.kind for e in book.step(ist(THU, 10, 8), bars)] == ["FILLED"]
    assert (the_row(engine)["fill_bar_ts"], the_row(engine)["resolution_min"]) == (
        ist(THU, 10, 6),
        2,
    )


# ── normalising the frame ────────────────────────────────────────────────────────


def test_normalise_sorts_keeps_the_last_duplicate_and_blanks_unusable_opens():
    stamps = pd.DatetimeIndex([ist(THU, 10, 7), ist(THU, 10, 5), ist(THU, 10, 6), ist(THU, 10, 6)])
    frame = pd.DataFrame({"open": [0.0, 998.0, 1000.0, 1005.0]}, index=stamps)
    view = bars_of(frame)
    assert list(view.ns) == [to_ns(ist(THU, 10, m)) for m in (5, 6, 7)]
    assert view.opens[1] == 1005.0 and np.isnan(view.opens[2])  # last revision; 0 is no price


@pytest.mark.parametrize(
    "frame",
    [
        None,
        pd.DataFrame(),
        make_bars(ist(THU, 10, 5), [1000.0]).drop(columns=["open"]),
        make_bars(ist(THU, 10, 5), [1000.0]).tz_localize(None),
        "not a frame",
    ],
    ids=["none", "empty", "no-open", "naive", "garbage"],
)
def test_normalise_refuses_what_it_cannot_read(frame):
    assert normalise_bars(frame) is None


def test_bars_indexed_in_utc_compare_as_the_same_instants():
    frame = make_bars(ist(THU, 10, 5), [1000.0, 1001.0])
    utc = frame.tz_convert(dt.timezone.utc)
    assert list(bars_of(utc).ns) == list(bars_of(frame).ns)
    assert bars_of(frame).ns.dtype == np.int64


# ── a pricer that fails (amendment 6: never a silent zero) ──────────────────────


@pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf"), True, "312.34"])
def test_an_unusable_premium_is_reported_once_and_the_trade_waits(make_book, engine, bad):
    book = make_book(premium_at=lambda option, underlying, ts: bad)
    book.on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
    bars = series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65]))

    first = book.step(ist(THU, 10, 8), bars)
    again = book.step(ist(THU, 10, 9), bars)

    assert [(e.kind, e.detail["leg"]) for e in first] == [("PRICING_UNAVAILABLE", "entry")]
    assert again == []  # said once per bar, not once per poll
    row = the_row(engine)
    assert (row["status"], row["entry_paise"], row["risk_paise"]) == ("PENDING_FILL", None, None)
    assert kinds(engine) == ["OPENED", "PRICING_UNAVAILABLE"]


def test_a_pricer_that_recovers_fills_at_the_first_eligible_bar_not_a_later_one(make_book, engine):
    calls = {"n": 0}

    def flaky(option, underlying, ts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("IV source down")
        return 312.34

    book = make_book(premium_at=flaky)
    book.on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
    early = series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65]))
    assert [e.kind for e in book.step(ist(THU, 10, 8), early)] == ["PRICING_UNAVAILABLE"]
    later = series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65, 45300.0, 45400.0]))
    assert [e.kind for e in book.step(ist(THU, 10, 10), later)] == ["FILLED"]
    row = the_row(engine)
    assert (row["fill_bar_ts"], row["entry_paise"]) == (ist(THU, 10, 6), 31435)


def test_a_fill_bar_that_never_prices_is_no_fill_expired_at_the_deadline(make_book, engine):
    """Manager decision 11: PRICING_UNAVAILABLE while waiting, then NO_FILL 'expired'."""
    book = make_book(premium_at=lambda option, underlying, ts: None)
    book.on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))  # expires 10:15:03
    bars = make_bars(ist(THU, 10, 5), [45200.0] * 12)  # 10:05 .. 10:16

    waiting = book.step(ist(THU, 10, 8), series(BANKNIFTY=bars.iloc[:3]))
    closed = book.step(ist(THU, 10, 20), series(BANKNIFTY=bars))

    assert [e.kind for e in waiting] == ["PRICING_UNAVAILABLE"]
    assert [(e.kind, e.detail["reason"], e.detail["pricing"]) for e in closed] == [
        ("NO_FILL", "expired", "unavailable")
    ]
    assert closed[0].ts == ist(THU, 10, 17)  # data time: the 10:16 bar's close, not `now`
    row = the_row(engine)
    assert (row["status"], row["close_reason"], row["entry_paise"]) == ("NO_FILL", "NO_FILL", None)
    assert (row["gross_paise"], row["costs_paise"], row["net_paise"]) == (0, 0, 0)
    assert kinds(engine) == ["OPENED", "PRICING_UNAVAILABLE", "NO_FILL"]


def test_option_risk_uses_the_premium_at_the_underlying_stop_with_the_frozen_option(
    make_book, engine
):
    pricer = FakePricer(fair=312.34)  # flat in the underlying: risk is the spread and ticks
    book = make_book(premium_at=pricer)
    alert = make_alert("INDEX", "long")  # underlying stop 45110.35
    book.on_alert(alert, ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65])))
    # Manager decision (a): risk = entry - the stop EXIT fill the Lab would book,
    # floor_tick(312.34 x (1 - 0.006) - 2 x 0.05) = floor_tick(310.36596) = 310.35
    assert the_row(engine)["risk_paise"] == (31435 - 31035) * 30
    option, underlying, ts = pricer.calls[1]
    assert (option, underlying, ts) == (alert["economics"]["option"], 45110.35, ist(THU, 10, 6))


def _linear_premium(option, underlying, ts):
    """Delta 0.65 around 312.34 at 45224.65: a CE gains with the underlying, a PE loses."""
    sign = 1.0 if option["opt_type"] == "CE" else -1.0
    return max(0.05, 312.34 + sign * 0.65 * (float(underlying) - 45224.65))


def _lab_risk_paise(direction: str, underlying_open: float, underlying_stop: float) -> int:
    """The differential oracle: the risk `lab.execution.execute_trade` books, in paise."""
    from onelot.config import CostConfig
    from onelot.costs import CostModel
    from onelot.lab.execution import execute_trade
    from onelot.lab.simulator import Leg, SimTrade
    from onelot.paper.money import to_paise

    from .conftest import BT, SPECS

    ROOT = Path(__file__).resolve().parents[2]

    class Pricer:
        def iv_for(self, ts):
            return 0.1532

        def premium(self, spot, k, ts, expiry, opt):
            return _linear_premium({"opt_type": opt}, spot, ts)

        def delta(self, spot, k, ts, expiry, opt):
            return 0.65

    ts = pd.Timestamp(ist(THU, 10, 6))
    trade = SimTrade(
        symbol="BANKNIFTY",
        strategy="ema_cross",
        side=1 if direction == "long" else -1,
        signal_ts=ts,
        entry_ts=ts,
        entry_price=underlying_open,
        stop_initial=underlying_stop,
        risk_ps=abs(underlying_open - underlying_stop),
        target=None,
        strength=60.0,
        atr=1.0,
        legs=[Leg(ts + pd.Timedelta(minutes=5), underlying_open, 1.0, "test")],
    )
    costs = CostModel(CostConfig.load(ROOT / "config" / "costs.yaml").product("index_options"))
    row = execute_trade(trade, SPECS["BANKNIFTY_OPT"], costs, BT, BT.slippage_pct, Pricer())
    return to_paise(row["risk_inr"])


@pytest.mark.parametrize("direction", ["long", "short"])
def test_option_risk_equals_the_risk_lab_execute_trade_books(make_book, engine, direction):
    """Manager decision (a): paper R and Lab R are comparable for the promotion gate."""
    book = make_book(premium_at=_linear_premium)
    alert = make_alert("INDEX", direction)  # underlying stop 45110.35 (long) / 45310.35 (short)
    stop = alert["economics"]["underlying_stop"]
    book.on_alert(alert, ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65])))
    row = the_row(engine)
    assert row["entry_paise"] == 31435
    assert row["risk_paise"] == _lab_risk_paise(direction, 45224.65, stop)
    # long : stop fair 312.34 - 0.65 x 114.30 = 238.045 -> floor_tick(236.51673) = 236.50
    # short: stop fair 312.34 - 0.65 x  85.70 = 256.635 -> floor_tick(254.99519) = 254.95
    want = {"long": (31435 - 23650) * 30, "short": (31435 - 25495) * 30}
    assert row["risk_paise"] == want[direction]


def test_option_stop_exit_fill_is_floored_at_one_tick(make_book, engine):
    def collapses_at_the_stop(option, underlying, ts):
        return 312.34 if underlying == 45224.65 else 0.01

    book = make_book(premium_at=collapses_at_the_stop)
    book.on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65])))
    # floor_tick(0.01 x 0.994 - 0.10) is negative: the Lab floors the exit at one tick (5 paise)
    assert the_row(engine)["risk_paise"] == (31435 - 5) * 30


def test_a_stop_premium_not_below_the_entry_leaves_risk_null_and_says_so(make_book, engine):
    def odd(option, underlying, ts):
        return 312.34 if underlying == 45224.65 else 400.0

    book = make_book(premium_at=odd)
    book.on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
    events = book.step(
        ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65]))
    )
    row = the_row(engine)
    assert (row["status"], row["entry_paise"], row["risk_paise"]) == ("OPEN", 31435, None)
    assert [(e.kind, e.detail.get("leg")) for e in events] == [
        ("FILLED", None),
        ("PRICING_UNAVAILABLE", "risk"),
    ]


# ── atomic commits and failure isolation ─────────────────────────────────────────


@pytest.mark.parametrize("table", ["paper_events", "paper_trades"])
def test_a_failed_fill_leaves_no_half_state_is_counted_and_retries(make_book, engine, table):
    book = make_book()
    book.on_alert(make_alert(), ist(THU, 10, 5, 4))
    before = (table_rows(engine, paper_trades), table_rows(engine, paper_events))
    bars = series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2))

    with failing_writes(engine, table):
        assert book.step(ist(THU, 10, 8), bars) == []  # nothing escapes

    assert (table_rows(engine, paper_trades), table_rows(engine, paper_events)) == before
    (held,) = book.open_trades()
    assert (held.status, held.entry_paise) == ("PENDING_FILL", None)
    assert book.status()["filled"] == 0 and book.status()["hook_errors"] == 1
    assert [e.kind for e in book.step(ist(THU, 10, 9), bars)] == ["FILLED"]
    assert kinds(engine) == ["OPENED", "FILLED"]


def test_two_books_on_one_database_fill_a_trade_once(make_book, engine):
    first = make_book()
    first.on_alert(make_alert(), ist(THU, 10, 5, 4))
    second = make_book()  # reloads the PENDING_FILL trade
    bars = series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2))

    assert [e.kind for e in first.step(ist(THU, 10, 8), bars)] == ["FILLED"]
    assert second.step(ist(THU, 10, 8), bars) == []  # the guarded update lost: adopt the row

    assert kinds(engine) == ["OPENED", "FILLED"]
    (held,) = second.open_trades()
    assert (held.status, held.entry_paise) == ("OPEN", 100030)
    assert second.status()["filled"] == 0


def test_a_bars_callable_that_raises_does_not_stop_other_series(make_book, engine):
    book = make_book()
    book.on_alert(make_alert("STOCK", alert_id="s1"), ist(THU, 10, 5, 4))
    book.on_alert(make_alert("INDEX", alert_id="i1"), ist(THU, 10, 5, 4))

    def bars(key: str) -> pd.DataFrame:
        if key == "RELIANCE":
            raise ConnectionError("feed down")
        return make_bars(ist(THU, 10, 5), [45200.0, 45224.65])

    events = book.step(ist(THU, 10, 8), bars)

    assert [e.kind for e in events] == ["FILLED"]
    status = {r["alert_id"]: r["status"] for r in table_rows(engine, paper_trades)}
    assert status == {"s1": "PENDING_FILL", "i1": "OPEN"}
    assert book.status()["hook_errors"] == 0  # a feed outage is waiting, not a book error


def test_a_naive_now_is_rejected_not_guessed(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(), ist(THU, 10, 5, 4))
    with pytest.raises(ValueError, match="timezone-aware"):
        book.step(dt.datetime(2026, 10, 29, 10, 8), series())
    assert the_row(engine)["status"] == "PENDING_FILL"
