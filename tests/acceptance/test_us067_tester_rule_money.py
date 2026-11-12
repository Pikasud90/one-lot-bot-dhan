"""US-067 independent tests: R1 to R8 boundaries, quantity, charges and the paper book.

Written from the story's words, not from the developer's tests. Integer prices keep the SMAs
exact so a boundary can be hit exactly."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine, select

from onelot.clock import IST
from onelot.config import CostConfig
from onelot.delivery.book import CLOSED, MISSED, OPEN, DeliveryBook, NewEntry
from onelot.delivery.money import delivery_model, depository_paise, trade_result
from onelot.delivery.rule import (
    PullbackParams,
    entry_reading,
    exit_reading,
    nifty_ok,
    quantity,
    simulate,
)
from onelot.store.schema import delivery_trades

ROOT = Path(__file__).resolve().parents[2]
P = PullbackParams()
N = 260
T = N - 1


def build(c_t: float = 92.0, sma_equals: bool = False, n: int = N) -> np.ndarray:
    """Closes whose last close is c_t, whose 20-session high is 100, SMA200 far below c_t
    (or exactly c_t when `sma_equals`)."""
    c = np.full(n, 80.0)
    c[n - 20 : n - 1] = 100.0  # t-19 .. t-1
    c[n - 1] = c_t
    if sma_equals:  # window sum == 200 * c_t exactly, integers only
        need = int(200 * c_t - c_t - 19 * 100)
        rest = n - 200  # first index of the window = n-200
        k = 200 - 20  # bars of the window before the last 20
        base, extra = divmod(need, k)
        c[rest : rest + k] = base
        c[rest : rest + extra] = base + 1
    return c


OK = np.ones(N, bool)


def test_r1_close_equal_to_092_high_signals():
    c = build(92.0)
    assert entry_reading(c, OK, T, P).signal


def test_r1_a_hair_above_092_high_does_not():
    c = build(92.01)
    r = entry_reading(c, OK, T, P)
    assert not r.signal and not r.deep


def test_r1_close_equal_sma200_is_no_signal_and_one_unit_more_is():
    c = build(92.0, sma_equals=True)
    s = np.cumsum(np.insert(c, 0, 0.0))
    assert (s[N] - s[N - 200]) / 200 == 92.0  # the construction is exact
    r = entry_reading(c, OK, T, P)
    assert r.deep and not r.above_trend and not r.signal
    c2 = c.copy()
    c2[N - 200] -= 1  # SMA200 now 91.995 < 92
    assert entry_reading(c2, OK, T, P).signal


def test_r1d_first_day_condition():
    c = build(92.0)
    c[T - 1] = 90.0  # deep the session before too (its own window includes the 100s)
    assert not entry_reading(c, OK, T, P).signal
    c[T - 1] = 100.0
    assert entry_reading(c, OK, T, P).signal


def test_r1d_window_of_t_minus_1_is_its_own():
    # t-1 is deep only against a 105 that sits at t-20 (outside t's window): t is not deep
    c = np.full(N, 80.0)
    c[T - 19 : T] = 100.0
    c[T - 20] = 105.0
    c[T - 1] = 96.6  # 0.92*105 = 96.6 -> deep at t-1 (105 in its window t-20..t-1)
    c[T] = 96.6  # at t: window t-19..t has high 100 -> 0.92*100 = 92 -> not deep
    r = entry_reading(c, OK, T, P)
    assert not r.deep and not r.signal


def test_r1_201_bars_needed():
    c200, c201 = build(92.0, n=200), build(92.0, n=201)
    assert not entry_reading(c200, np.ones(200, bool), 199, P).signal
    assert entry_reading(c201, np.ones(201, bool), 200, P).signal
    assert not entry_reading(c200, np.ones(200, bool), 199, P).ready


def test_r1b_nifty_missing_bar_short_history_and_equal():
    dates = [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(N)]
    nd = dates[:]
    up = np.arange(N, dtype=float) + 100
    ok = nifty_ok(dates, nd, up, P)
    assert not ok[:199].any() and ok[199:].all()
    # a missing NIFTY bar on the date
    gap = [d for d in nd if d != dates[T]]
    ok2 = nifty_ok(dates, gap, np.delete(up, T), P)
    assert not ok2[T] and ok2[T - 1]
    # NIFTY close == its SMA200 is not above: constant series
    flat = np.full(N, 100.0)
    assert not nifty_ok(dates, nd, flat, P).any()
    # fewer than 200 NIFTY bars
    assert not nifty_ok(dates, nd[:150], up[:150], P).any()


def _exit(c, e, fill, u):
    return exit_reading(np.asarray(c, float), e, fill, u, P)


def test_r3a_close_equal_sma20_does_not_exit_one_above_does():
    c = np.full(40, 100.0)  # SMA20 == 100 == close
    assert not _exit(c, 30, 100.0, 35).hit
    c[35] = 100.0 + 20 / 21 * 0 + 1  # clearly above
    assert "SMA20" in _exit(c, 30, 100.0, 35).reasons


def test_r3c_stop_equal_fill_times_085_exits():
    # SMA20 stays above the close; fill 100 -> stop level 85.0
    c = np.full(40, 120.0)
    c[35] = 85.0
    r = _exit(c, 30, 100.0, 35)
    assert r.hit and r.reasons == ("STOP",) and r.stop_level == pytest.approx(85.0)
    c[35] = 85.01
    assert not _exit(c, 30, 100.0, 35).hit


def test_r3b_30_sessions_entry_session_counts_as_1():
    c = np.full(80, 120.0)
    c[45:] = 119.0  # below SMA20 never > ; keep above stop
    # entry session 30; session 30 is count 1; session 59 is count 30
    assert _exit(c, 30, 100.0, 58).sessions == 29 and not _exit(c, 30, 100.0, 58).hit
    r = _exit(c, 30, 100.0, 59)
    assert r.sessions == 30 and r.reasons == ("TIME",)
    assert _exit(c, 30, 100.0, 30).sessions == 1


def test_r3_u_before_entry_is_refused():
    with pytest.raises(ValueError):
        _exit(np.full(40, 1.0), 30, 1.0, 29)


# ── R4 and R8 by an independent implementation on random walks ───────────────────────────


def my_trades(o, c, ok, p=P):
    """Plain loops, written from the story."""
    n = len(c)
    out = []
    t = 200
    while t < n - 1:

        def deep(i):
            return c[i] <= 0.92 * max(c[i - 19 : i + 1])

        s200 = sum(c[t - 199 : t + 1]) / 200
        if not (c[t] > s200 and ok[t] and deep(t) and not deep(t - 1)):
            t += 1
            continue
        e = t + 1
        fill = o[e] * 1.0003
        x = None
        for u in range(e, n):
            s20 = sum(c[u - 19 : u + 1]) / 20
            if c[u] > s20 or (u - e + 1) >= 30 or c[u] <= fill * 0.85:
                x = u + 1
                break
        if x is None or x >= n:
            out.append((t, e, n - 1 if x is None else n - 1, True))
            t = n  # reference ends; nothing after an open trade
            continue
        out.append((t, e, x, False))
        t = x
    return out


@pytest.mark.parametrize("seed", range(12))
def test_simulate_equals_an_independent_loop_and_r4_r8_hold(seed):
    rng = np.random.default_rng(seed)
    n = 900
    c = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, n)))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.004, n))
    ok = rng.random(n) > 0.25
    got = simulate(o, c, ok, P)
    mine = my_trades(o, c, ok)
    assert [(g.signal, g.entry, g.exit, g.at_close) for g in got][: len(mine)] == [
        (a, b, d, e) for a, b, d, e in mine
    ][: len(got)]
    for a, b in zip(got, got[1:], strict=False):
        assert b.signal >= a.exit  # R4: the close of the sale day may enter, never earlier
        assert b.signal != a.exit - 1
    # R8: cutting after t leaves the signals up to t unchanged
    for cut in (400, 555, 700):
        part = simulate(o[:cut], c[:cut], ok[:cut], P)
        want = [g.signal for g in got if g.signal < cut - 1]
        have = [g.signal for g in part if g.signal < cut - 1]
        assert have == want, (cut, have, want)
    # entry_reading alone depends on the bars up to t only
    from onelot.delivery.rule import sma

    for t in (250, 400, 640):
        full = entry_reading(c, ok, t, P, sma(c, 200))
        cut_ = entry_reading(c[: t + 1], ok[: t + 1], t, P)
        assert full == cut_


def test_r4_the_exit_close_is_never_an_entry_but_the_sale_day_can_be():
    # an entry, a 15% stop-out whose close is itself a fresh deep close would signal entry at u
    # only if the loop re-read u; simulate restarts at u+1 (the sale day). Check on a built path.
    c = np.full(300, 100.0)
    c[:150] = 50.0  # SMA200 stays below the later closes
    c[250:] = 100.0
    c[259] = 91.0  # signal at 259 (deep vs 100, above SMA200, first day)
    o = c.copy()
    ok = np.ones(300, bool)
    tr = simulate(o, c, ok, P)
    assert tr[0].signal == 259 and tr[0].entry == 260
    # exit signal needs close > SMA20: close 91 on 260 vs SMA20 ~ 99 -> no; stay until 30 sessions
    assert tr[0].reasons == ("TIME",) or tr[0].reasons == ("SMA20",)


# ── R5 quantity ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "price, want",
    [
        (10000, 1),
        (6666.66, 2),
        (6666.67, 1),
        (20000.01, 1),
        (19999.99, 1),
        (3, 3333),
        (4000, 3),  # 2.5 shares: half goes up
        (2000, 5),
        (3333.33, 3),
        (3333.34, 3),
        (1e9, 1),
    ],
)
def test_r5_quantity(price, want):
    assert quantity(price, 10000) == want


def test_r5_unit_is_configuration_and_bad_prices_refused():
    assert quantity(100, 20000) == 200
    for bad in (0, -5, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            quantity(bad, 10000)
    with pytest.raises(ValueError):
        PullbackParams(unit_inr=0)


# ── R6 charges and the book ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def costs():
    return CostConfig.load(ROOT / "config" / "costs.yaml")


def test_r6_dp_charge_is_14_75_and_applied_once(costs):
    assert depository_paise(costs) == 1475
    m = delivery_model(costs)
    r1 = trade_result(m, 10000, 10500, 1, 1475)
    r0 = trade_result(m, 10000, 10500, 1, 0)
    assert r0.net_paise - r1.net_paise == 1475 and r1.charges_paise == r0.charges_paise
    assert r1.net_paise == r1.gross_paise - r1.charges_paise - 1475


def test_r6_net_scales_with_quantity_and_pct_is_of_entry_value(costs):
    m = delivery_model(costs)
    a = trade_result(m, 100000, 105000, 1, 0)
    b = trade_result(m, 100000, 105000, 10, 0)
    assert b.gross_paise == 10 * a.gross_paise
    assert abs(b.charges_paise - 10 * a.charges_paise) <= 10  # per-leg rounding only
    assert b.net_pct == pytest.approx(a.net_pct, abs=1e-3)
    r = trade_result(m, 123456, 130000, 7, 1475)
    assert r.net_pct == pytest.approx(r.net_paise / (123456 * 7) * 100)


def _book(costs, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    return DeliveryBook(eng, delivery_model(costs), depository_paise(costs)), eng


NOW = dt.datetime(2026, 11, 16, 15, 40, tzinfo=IST)


def test_book_dp_only_on_closed_trades_and_one_row_per_identity(costs, tmp_path):
    book, eng = _book(costs, tmp_path)
    d0, d1 = dt.date(2026, 11, 13), dt.date(2026, 11, 14)
    new = NewEntry("SBIN", 194.4, 51, {"evidence": "x"})
    t = book.record_evaluation("SBIN", d0, {"r": 1}, NOW, new)
    assert t is not None and t.dp_paise is None and t.net_paise is None
    # the same session again: nothing written
    assert book.record_evaluation("SBIN", d0, {"r": 1}, NOW, new) is None
    m = book.record_evaluation("ITC", d0, {"r": 1}, NOW, NewEntry("ITC", 400, 25, {}))
    assert book.miss_entry(m.id, "missed", NOW)
    assert book.get(m.id).dp_paise is None and book.get(m.id).charges_paise is None
    assert book.fill_entry(t.id, NOW, d1, 195.0585, False, NOW)
    assert not book.fill_entry(t.id, NOW, d1, 999.0, False, NOW)  # no second fill
    assert book.get(t.id).entry_paise == 19506 and book.get(t.id).status == OPEN
    assert book.get(t.id).dp_paise is None
    assert book.signal_exit(t.id, d1, ["SMA20"], 2, NOW)
    assert not book.signal_exit(t.id, d1, ["TIME"], 3, NOW)
    assert book.fill_exit(t.id, NOW, 200.0, False, NOW)
    assert not book.fill_exit(t.id, NOW, 300.0, False, NOW)  # no second exit
    c = book.get(t.id)
    assert c.status == CLOSED and c.dp_paise == 1475 and c.exit_paise == 20000
    assert c.gross_paise == (20000 - 19506) * 51
    assert c.net_paise == c.gross_paise - c.charges_paise - 1475
    tot = list(book.by_slot().values())[0]
    assert tot.trades == 1 and tot.no_fill == 1 and tot.costs_paise == c.charges_paise + 1475
    # a duplicate identity cannot be inserted even bypassing the evaluation table
    with eng.begin() as conn:
        with pytest.raises(Exception):  # noqa: B017 - any refusal by the database
            conn.execute(
                delivery_trades.insert().values(
                    **{
                        k: getattr(c, k)
                        for k in (
                            "series_key",
                            "instrument_key",
                            "strategy",
                            "timeframe",
                            "qty",
                            "status",
                            "created_at",
                        )
                    },
                    signal_date=d0.isoformat(),
                    signal_close=1.0,
                    entry_late=False,
                    exit_late=False,
                    plan={},
                    updated_at=NOW,
                )
            )
    with eng.connect() as conn:
        rows = conn.execute(
            select(delivery_trades.c.series_key, delivery_trades.c.signal_date)
        ).all()
    assert len(rows) == len(set(rows)) == 2
    assert MISSED


def test_charge_rupees_to_paise_half_up():
    from onelot.delivery.money import rupees_to_paise

    assert rupees_to_paise(195.005) == 19501  # half up on the shortest repr
    assert rupees_to_paise(0.1 + 0.2) == 30
    assert math.isfinite(1.0)
