"""Manager decisions after T3 (US-001, 2026-11-01): (f) the reconciliation horizon,
(g) the REGRADED audit event, (h) an IV implied once at the alert, never a constant.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from pydantic import ValidationError

from onelot.config import AppConfig, PaperCfg
from onelot.paper.book import reconcile_since
from onelot.paper.fill import option_entry_paise
from onelot.paper.money import to_paise
from onelot.paper.pricing import IV_CEILING, black76_premium, implied_iv
from onelot.store.schema import paper_events, paper_trades

from .conftest import BT, THU, ist, make_alert, make_bars, store_desk_signal, table_rows

MON = dt.date(2026, 11, 2)
FRI = dt.date(2026, 10, 30)


def series(**frames):
    return lambda key: frames.get(key)


def b76(option, underlying, ts):
    return black76_premium(option, underlying, ts, BT.risk_free_rate)


def alert_at(alert_id: str, created: dt.datetime, **kw) -> dict:
    return make_alert(alert_id=alert_id, created=created, **kw)


# ── (f) reconciliation is bounded to reconcile_days calendar days ────────────────


def test_reconcile_days_defaults_to_three_and_rejects_a_negative():
    assert PaperCfg().reconcile_days == 3
    assert AppConfig.load().paper.reconcile_days == 3  # the shipped YAML needs no key
    with pytest.raises(ValidationError):
        PaperCfg(reconcile_days=-1)


@pytest.mark.parametrize(
    ("now", "days", "since"),
    [
        (ist(MON, 10, 8), 3, ist(FRI, 0, 0)),  # a Monday start still reaches Friday's alerts
        (ist(MON, 0, 0), 3, ist(FRI, 0, 0)),
        (ist(THU, 23, 59), 0, ist(THU, 0, 0)),  # 0: today's alerts only
        (
            dt.datetime(2026, 10, 29, 4, 0, tzinfo=dt.timezone.utc),
            1,
            ist(THU - dt.timedelta(1), 0, 0),
        ),
    ],
)
def test_the_horizon_is_midnight_ist_that_many_calendar_days_back(now, days, since):
    assert reconcile_since(now, days) == since


@pytest.mark.parametrize(
    ("days", "booked"),
    [(3, {"mon", "thu"}), (0, {"thu"}), (4, {"sun", "mon", "thu"})],
)
def test_only_desk_signals_inside_the_horizon_are_reconciled(make_book, engine, days, booked):
    store_desk_signal(engine, alert_at("sun", ist(THU - dt.timedelta(4), 23, 59, 59)))
    store_desk_signal(engine, alert_at("mon", ist(THU - dt.timedelta(3), 0, 0, 0)))  # boundary
    store_desk_signal(engine, alert_at("thu", ist(THU, 10, 5, 3)))

    make_book(reconcile_days=days)  # clock: THU 10:08

    assert {r["alert_id"] for r in table_rows(engine, paper_trades)} == booked


def test_a_monday_start_books_fridays_last_alert(make_book, engine):
    store_desk_signal(engine, alert_at("fri", ist(FRI, 15, 29, 3)))
    book = make_book(clock=lambda: ist(MON, 9, 0))
    assert [t.alert_id for t in book.open_trades()] == ["fri"]


# ── (g) a regrade is booked as a REGRADED event ──────────────────────────────────


def mixed() -> pd.DataFrame:
    one = make_bars(ist(THU, 10, 5), [1000.0] * 4)  # 10:05 .. 10:08 at 1m
    two = make_bars(ist(THU, 10, 10), [1000.0] * 3, res_min=2)  # 10:10, 10:12, 10:14
    return pd.concat([one, two])


def test_a_regrade_books_a_regraded_event_with_the_gap_bars(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(source="dhan"), ist(THU, 10, 5, 4))

    events = book.step(ist(THU, 10, 20), series(RELIANCE=mixed()))

    assert [e.kind for e in events] == ["REGRADED", "FILLED"]
    regraded = events[0]
    assert regraded.ts == ist(THU, 10, 14)  # data time: the bar that completed the evidence
    assert regraded.detail == {
        "from": "live",
        "to": "delayed",
        "reason": "mixed_spacing",
        "base_min": 1,
        "gap_bars": [ist(THU, 10, m).isoformat() for m in (8, 10, 12, 14)],
    }
    stored = [e for e in table_rows(engine, paper_events) if e["kind"] == "REGRADED"]
    assert len(stored) == 1 and stored[0]["detail"] == regraded.detail
    assert events[1].detail["grade"] == "delayed"  # the fill is booked on the new grade


def test_a_trade_is_regraded_once_and_a_delayed_one_never(make_book, engine):
    book = make_book()
    book.on_alert(make_alert(alert_id="live1", source="dhan"), ist(THU, 10, 5, 4))
    book.on_alert(make_alert(alert_id="yahoo1", source="yahoo"), ist(THU, 10, 5, 4))
    before_alert = pd.concat(  # 9:50..9:54 at 1m, then 9:56, 9:58, 10:00 at 2m: mixed
        [make_bars(ist(THU, 9, 50), [1000.0] * 5), make_bars(ist(THU, 9, 56), [1e3] * 3, res_min=2)]
    )
    for _ in range(2):
        book.step(ist(THU, 10, 5, 30), series(RELIANCE=before_alert))  # nothing can fill yet

    rows = {r["alert_id"]: r for r in table_rows(engine, paper_trades)}
    kinds = [(e["trade_id"], e["kind"]) for e in table_rows(engine, paper_events)]
    assert kinds.count((rows["live1"]["id"], "REGRADED")) == 1
    assert (rows["yahoo1"]["id"], "REGRADED") not in kinds
    assert {k: (r["grade"], r["status"]) for k, r in rows.items()} == {
        "live1": ("delayed", "PENDING_FILL"),
        "yahoo1": ("delayed", "PENDING_FILL"),
    }


# ── (h) an index alert without an IV ─────────────────────────────────────────────


def index_alert(alert_id: str = "o1", iv: float | None = None, **eco) -> dict:
    alert = make_alert("INDEX", "long", alert_id=alert_id)  # chain premium 312.34 at 45210.35
    alert["economics"]["option"]["iv"] = iv
    alert["economics"].update(eco)
    return alert


def test_a_missing_iv_is_implied_from_the_chain_premium_and_frozen(make_book):
    trade = make_book(premium_at=b76).on_alert(index_alert(), ist(THU, 10, 5, 4))
    assert trade is not None
    option = trade.plan["alert"]["economics"]["option"]
    assert trade.plan["pricing"]["iv_source"] == "implied"
    assert 0.01 < option["iv"] < IV_CEILING
    repriced = b76(option, 45210.35, ist(THU, 10, 5, 3))  # the alert's inputs, the frozen IV
    assert repriced == pytest.approx(312.34, abs=1e-4)


def test_the_implied_iv_prices_the_fill(make_book, engine):
    book = make_book(premium_at=b76)
    trade = book.on_alert(index_alert(), ist(THU, 10, 5, 4))
    assert trade is not None
    book.step(ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65])))
    (row,) = table_rows(engine, paper_trades)
    fair = b76(trade.plan["alert"]["economics"]["option"], 45224.65, ist(THU, 10, 6))
    assert fair is not None
    want = option_entry_paise(fair, BT.option_spread_pct, BT.option_slippage_ticks, 0.05)
    assert (row["status"], row["entry_paise"]) == ("OPEN", want)


@pytest.mark.parametrize(
    "premium",
    [None, 200.0, 40_000.0],
    ids=["no-chain-premium", "below-intrinsic-210", "beyond-300pct-vol"],
)
def test_an_iv_that_cannot_be_implied_stays_missing_and_the_fill_is_unpriced(
    make_book, engine, premium
):
    alert = index_alert(entry_price=premium)
    alert["economics"]["option"]["premium"] = premium
    book = make_book(premium_at=b76)
    trade = book.on_alert(alert, ist(THU, 10, 5, 4))
    assert trade is not None
    assert trade.plan["pricing"]["iv_source"] == "missing"
    assert trade.plan["alert"]["economics"]["option"]["iv"] is None  # never a constant

    events = book.step(
        ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65]))
    )
    assert [e.kind for e in events] == ["PRICING_UNAVAILABLE"]
    assert table_rows(engine, paper_trades)[0]["entry_paise"] is None


def test_an_alert_iv_is_kept_as_it_is(make_book):
    trade = make_book(premium_at=b76).on_alert(index_alert(iv=0.1532), ist(THU, 10, 5, 4))
    assert trade is not None
    assert trade.plan["pricing"] == {"iv_source": "alert"}  # synthetic: no basis
    assert trade.plan["alert"]["economics"]["option"]["iv"] == 0.1532


def test_a_live_chain_alert_records_its_basis_without_applying_it(make_book):
    """basis = chain premium - model premium at the alert (ADR-001 am. 6), recorded only."""
    book = make_book(premium_at=b76)
    with_iv = book.on_alert(index_alert("c1", iv=0.1532, synthetic=False), ist(THU, 10, 5, 4))
    implied = book.on_alert(index_alert("c2", iv=None, synthetic=False), ist(THU, 10, 5, 4))
    assert with_iv is not None and implied is not None
    option = with_iv.plan["alert"]["economics"]["option"]
    model = b76(option, 45210.35, ist(THU, 10, 5, 3))
    assert model is not None
    want = 31234 - to_paise(model)
    assert with_iv.plan["pricing"] == {"iv_source": "alert", "basis_paise": want}
    assert implied.plan["pricing"]["iv_source"] == "implied"
    assert abs(implied.plan["pricing"]["basis_paise"]) <= 1  # implied from that very premium


@pytest.mark.parametrize(
    "option",
    [
        {"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24"},
        {"strike": 45000.0, "opt_type": "PE", "expiry": "2026-11-24"},
    ],
)
def test_implied_iv_round_trips_black76(option):
    ts = ist(THU, 10, 5, 3)
    for iv in (0.08, 0.15, 0.45, 1.2):
        premium = black76_premium(option | {"iv": iv}, 45210.35, ts, BT.risk_free_rate)
        solved = implied_iv(option, premium, 45210.35, ts, BT.risk_free_rate)
        assert solved == pytest.approx(iv, abs=1e-5)


@pytest.mark.parametrize(
    ("option", "premium", "underlying"),
    [
        ({"strike": 45000.0, "opt_type": "CE", "expiry": "bad"}, 312.34, 45210.35),
        ({"strike": 45000.0, "opt_type": "XX", "expiry": "2026-11-24"}, 312.34, 45210.35),
        ({"strike": None, "opt_type": "CE", "expiry": "2026-11-24"}, 312.34, 45210.35),
        ({"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24"}, float("nan"), 45210.35),
        ({"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24"}, 312.34, 0.0),
        ({"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24"}, True, 45210.35),
    ],
)
def test_implied_iv_refuses_unusable_inputs(option, premium, underlying):
    assert implied_iv(option, premium, underlying, ist(THU, 10, 5, 3), BT.risk_free_rate) is None


def test_implied_iv_refuses_a_naive_time():
    option = {"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24"}
    assert implied_iv(option, 312.34, 45210.35, dt.datetime(2026, 10, 29, 10, 5), 0.065) is None


def test_a_premium_below_the_one_percent_vol_price_is_refused_not_clamped():
    """Review round 1, MINOR-5. Between the discounted intrinsic and the 1 %-vol price the
    solver returns its lower bound EXACTLY; that is a clamp, not an IV, so it is refused."""
    import math

    from onelot.analytics.blackscholes import implied_vol
    from onelot.analytics.blackscholes import price as b76_price
    from onelot.lab.execution import expiry_datetime, year_frac
    from onelot.paper.pricing import IV_FLOOR

    option = {"strike": 45000.0, "opt_type": "CE", "expiry": "2026-11-24"}
    ts, spot, rate = ist(THU, 10, 5, 3), 45210.35, BT.risk_free_rate
    t = year_frac(ts, expiry_datetime(dt.date(2026, 11, 24)))
    intrinsic = (spot - 45000.0) * math.exp(-rate * t)
    at_floor = b76_price(spot, 45000.0, t, IV_FLOOR, rate, "CE")
    premium = (intrinsic + at_floor) / 2
    assert intrinsic < premium < at_floor  # the band exists
    assert (
        implied_vol(premium, spot, 45000.0, t, rate, "CE", lo=IV_FLOOR, hi=IV_CEILING) == IV_FLOOR
    )
    assert implied_iv(option, premium, spot, ts, rate) is None
    just_above = black76_premium(option | {"iv": 0.0105}, spot, ts, rate)
    assert implied_iv(option, just_above, spot, ts, rate) == pytest.approx(0.0105, abs=1e-6)
