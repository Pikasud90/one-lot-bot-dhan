"""US-001 mode B: the IV of an index option is frozen at the alert, implied once if missing.

Manager decision h: an index alert without an IV gets one IMPLIED once from its chain premium
(`economics.entry_price`) at the underlying entry and created_at, by bisection inside
1 %..300 %; a result on a bound (or no solution) is refused. The IV is frozen in the book's plan
(`plan.pricing.iv_source` = alert | implied | missing, and the option's iv). If it cannot be
implied the IV stays missing and every repricing is PRICING_UNAVAILABLE: a constant IV is never
used. A live-chain alert (`synthetic` false) also records its basis (chain premium minus the
model premium at the alert, paise) in the plan; it is RECORDED here, applied in US-023.

The book is the one the Engine builds (`build_paper_book`: the real Black-76 pricer, the real
specs and costs); the oracle for every premium is the Lab's own `OptionPricer`.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
import us001_helpers as H
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from onelot.config import AppConfig
from onelot.costs import round_to_tick

PROP = settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
RATE = AppConfig().backtest.risk_free_rate
CLOCK = lambda: H.at(10, 30)  # noqa: E731


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def real_book(engine):
    from onelot.paper.factory import build_paper_book

    return build_paper_book(AppConfig.load(), engine, clock=CLOCK)


def index_alert(
    alert_id="I1",
    *,
    sigma=0.2,
    direction="long",
    expiry="2026-11-24",
    created=None,
    underlying=H.OPT_U0,
    strike=None,
    iv=None,
    premium="auto",
    round_premium=True,
    synthetic=True,
    chain=None,
    **kw,
):
    """An index alert whose chain premium is the Lab's Black-76 price at `sigma` (rounded to 2 dp
    like the desk's), and whose option carries `iv` (absent when None)."""
    created = created or H.CREATED
    strike = underlying if strike is None else strike
    opt = "CE" if direction == "long" else "PE"
    if premium == "auto":
        premium = H.lab_fair_premium(
            strike=strike,
            opt_type=opt,
            expiry=expiry,
            underlying=underlying,
            ts=created,
            iv=sigma,
            rate=RATE,
        )
        premium = round(premium, 2) if round_premium else premium
    alert = H.make_alert(
        alert_id,
        kind="INDEX",
        direction=direction,
        created=created,
        entry=underlying,
        strike=strike,
        expiry=expiry,
        premium=premium,
        iv=iv,
        drop_iv=iv is None,
        **kw,
    )
    alert["economics"]["synthetic"] = synthetic
    if chain is not None:  # a premium make_alert cannot do arithmetic on
        alert["economics"]["entry_price"] = alert["economics"]["option"]["premium"] = chain
    return alert


def pricing(trade):
    return trade.plan["pricing"]


def stored_iv(trade):
    return trade.plan["alert"]["economics"]["option"].get("iv")


def index_frame(opens=(H.OPT_U0 - 5, H.OPT_U0, H.OPT_U0 + 1), start=None):
    return H.minute_bars(start or H.at(10, 5), list(opens))


class TestImpliedOnceAtTheAlert:
    @pytest.mark.parametrize("direction", ["long", "short"])
    @pytest.mark.parametrize("sigma", [0.0125, 0.05, 0.15, 0.5, 1.5, 2.9])
    def test_a_missing_iv_is_implied_from_the_chain_premium(self, db, direction, sigma):
        trade = H.open_trade(real_book(db.engine), index_alert(sigma=sigma, direction=direction))
        assert pricing(trade)["iv_source"] == "implied"
        assert stored_iv(trade) == pytest.approx(sigma, abs=1e-4)

    @pytest.mark.parametrize("iv_value", [None, 0.0, -0.2])
    def test_a_null_zero_or_negative_iv_counts_as_missing_and_is_implied(self, db, iv_value):
        alert = index_alert(sigma=0.2)
        alert["economics"]["option"]["iv"] = iv_value
        trade = H.open_trade(real_book(db.engine), alert)
        assert pricing(trade)["iv_source"] == "implied"
        assert stored_iv(trade) == pytest.approx(0.2, abs=1e-4)

    def test_an_iv_the_alert_carries_is_used_as_it_is_never_re_implied(self, db):
        alert = index_alert(sigma=0.2, iv=0.33)  # the premium says 20%, the alert says 33%
        trade = H.open_trade(real_book(db.engine), alert)
        assert pricing(trade)["iv_source"] == "alert"
        assert stored_iv(trade) == 0.33

    def test_the_iv_is_implied_at_the_alerts_own_time_not_when_the_hook_runs(self, db):
        """Expiry is three days away, so three hours matter: the hook runs late (a reconcile)."""
        alert = index_alert(sigma=0.2, expiry="2026-11-02")
        trade = H.open_trade(real_book(db.engine), alert, now=H.CREATED + dt.timedelta(hours=3))
        assert stored_iv(trade) == pytest.approx(0.2, abs=2e-4)

    def test_the_iv_is_implied_at_the_underlying_entry_not_at_a_later_price(self, db):
        alert = index_alert(sigma=0.2, underlying=54000.0)
        trade = H.open_trade(real_book(db.engine), alert)
        assert stored_iv(trade) == pytest.approx(0.2, abs=1e-4)
        assert trade.plan["levels"]["underlying_entry_paise"] == 5_400_000

    def test_property_implying_the_iv_of_a_model_premium_recovers_it(self, db):
        """Only for quotes that carry information: an option that is all intrinsic value, or so far
        out of the money that its premium is a few rupees, has no identifiable IV (vega ~ 0)."""
        book = real_book(db.engine)
        counter = itertools.count()

        @given(
            sigma=st.floats(min_value=0.02, max_value=2.5),
            moneyness=st.floats(min_value=-0.03, max_value=0.03),
            days=st.integers(min_value=3, max_value=60),
            direction=st.sampled_from(["long", "short"]),
        )
        @PROP
        def check(sigma, moneyness, days, direction):
            underlying = 54000.0
            strike = round(underlying * (1 + moneyness) / 100) * 100.0
            expiry = (H.DAY + dt.timedelta(days=days)).isoformat()
            alert = index_alert(
                f"P{next(counter)}",
                sigma=sigma,
                direction=direction,
                expiry=expiry,
                strike=strike,
                underlying=underlying,
                round_premium=False,
            )
            premium = alert["economics"]["entry_price"]
            intrinsic = (
                max(underlying - strike, 0.0)
                if direction == "long"
                else max(strike - underlying, 0.0)
            )
            assume(premium - intrinsic >= 40.0)  # enough time value for the IV to be identifiable
            trade = H.open_trade(book, alert)
            assert pricing(trade)["iv_source"] == "implied"
            assert stored_iv(trade) == pytest.approx(sigma, rel=2e-3, abs=2e-4)

        check()


class TestTheIvIsFrozen:
    def test_the_implied_iv_is_stored_in_the_row_and_reloaded_unchanged(self, db):
        book = real_book(db.engine)
        trade = H.open_trade(book, index_alert(sigma=0.2))
        iv = stored_iv(trade)
        (reloaded,) = real_book(db.engine).open_trades()
        assert stored_iv(reloaded) == iv and pricing(reloaded)["iv_source"] == "implied"
        row = H.trade_row(db.engine, "I1")
        assert row["plan"]["alert"]["economics"]["option"]["iv"] == iv
        assert row["plan"]["pricing"]["iv_source"] == "implied"

    def test_a_repeated_on_alert_with_a_different_premium_never_re_implies(self, db):
        book = real_book(db.engine)
        first = H.open_trade(book, index_alert(sigma=0.2))
        again = H.open_trade(book, index_alert(sigma=0.4))  # same id, a different quote
        assert again.id == first.id and stored_iv(again) == stored_iv(first)
        assert H.trade_row(db.engine, "I1")["plan"]["alert"]["economics"]["option"][
            "iv"
        ] == stored_iv(first)

    def test_the_fill_reprices_with_the_implied_iv_and_matches_the_lab_to_the_paise(self, db):
        book = real_book(db.engine)
        trade = H.open_trade(book, index_alert(sigma=0.2))
        iv = stored_iv(trade)
        assert book.step(H.at(10, 8), H.Feed(BANKNIFTY=index_frame())) != []
        row = H.trade_row(db.engine, "I1")
        fair = H.lab_fair_premium(
            strike=H.OPT_U0,
            opt_type="CE",
            expiry="2026-11-24",
            underlying=H.OPT_U0,
            ts=H.at(10, 6),
            iv=iv,
            rate=RATE,
        )
        bt = AppConfig().backtest
        want = H.to_paise(
            round_to_tick(
                fair * (1 + bt.option_spread_pct) + bt.option_slippage_ticks * 0.05, 0.05, "up"
            )
        )
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == want
        assert row["risk_paise"] is not None and row["risk_paise"] > 0

    def test_the_pricer_is_only_ever_asked_with_the_frozen_iv(self, db):
        book = real_book(db.engine)
        real = book.premium_at
        seen: list[float | None] = []
        book.premium_at = lambda o, u, ts: (seen.append(o.get("iv")), real(o, u, ts))[1]
        trade = H.open_trade(book, index_alert(sigma=0.2))
        book.step(H.at(10, 8), H.Feed(BANKNIFTY=index_frame()))
        assert seen and set(seen) == {stored_iv(trade)}


REFUSALS = {
    "below-1pct": lambda: index_alert(sigma=0.005),
    "exactly-1pct": lambda: index_alert(sigma=0.01, round_premium=False),
    "exactly-300pct": lambda: index_alert(sigma=3.0, round_premium=False),
    "above-300pct": lambda: index_alert(sigma=3.5),
    "premium-none": lambda: index_alert(premium=None),
    "premium-zero": lambda: index_alert(premium=0.0),
    "premium-negative": lambda: index_alert(premium=-5.0),
    "premium-text": lambda: index_alert(premium=None, chain="cheap"),
    "premium-above-the-underlying": lambda: index_alert(premium=60_000.0),
    "below-intrinsic": lambda: index_alert(premium=100.0, strike=50_000.0),
}


class TestRefusedAtTheBounds:
    @pytest.mark.parametrize("name", list(REFUSALS))
    def test_an_iv_that_cannot_be_implied_stays_missing_and_is_never_replaced_by_a_constant(
        self, db, name
    ):
        alert = REFUSALS[name]()
        book = real_book(db.engine)
        real = book.premium_at
        asked: list[tuple[object, object]] = []

        def spy(option, underlying, ts):
            out = real(option, underlying, ts)
            asked.append((option.get("iv"), out))
            return out

        book.premium_at = spy
        trade = H.open_trade(book, alert)
        assert pricing(trade)["iv_source"] == "missing", name
        assert stored_iv(trade) is None
        book.step(H.at(10, 8), H.Feed(BANKNIFTY=index_frame()))
        row = H.trade_row(db.engine, "I1")
        assert H.val(row["status"]) == "PENDING_FILL" and row["entry_paise"] is None
        kinds = [H.val(e["kind"]) for e in H.event_rows(db.engine, "I1")]
        assert kinds.count("PRICING_UNAVAILABLE") == 1, kinds
        assert all(iv is None and out is None for iv, out in asked), asked

    def test_pricing_unavailable_is_said_once_per_bar_then_the_window_closes_no_fill(self, db):
        book = real_book(db.engine)
        H.open_trade(book, index_alert(sigma=0.005))
        feed = H.Feed(BANKNIFTY=index_frame())
        first = book.step(H.at(10, 8), feed)
        assert [H.val(e.kind) for e in first] == ["PRICING_UNAVAILABLE"]
        assert book.step(H.at(10, 8), feed) == []  # said once for that bar
        assert [t.alert_id for t in book.open_trades()] == ["I1"]
        late = H.minute_bars(H.at(10, 16), [H.OPT_U0] * 3)  # the window (10:15:03) is over
        events = book.step(H.at(10, 30), H.Feed(BANKNIFTY=late))
        row = H.trade_row(db.engine, "I1")
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None
        assert row["costs_paise"] == 0 and row["net_paise"] == 0

    @pytest.mark.parametrize("sigma", [0.0125, 2.9])
    def test_values_just_inside_the_bounds_are_accepted(self, db, sigma):
        trade = H.open_trade(real_book(db.engine), index_alert(sigma=sigma))
        assert pricing(trade)["iv_source"] == "implied"
        assert stored_iv(trade) == pytest.approx(sigma, abs=1e-4)

    def test_a_stock_alert_has_no_option_to_price(self, db):
        trade = H.open_trade(real_book(db.engine), alert_id="S1")
        assert "iv" not in trade.plan["alert"]["economics"]["option"]
        assert "pricing" not in trade.plan or trade.plan["pricing"] in ({}, None)


class TestTheLiveChainBasisIsRecordedNotApplied:
    def test_the_basis_is_the_chain_premium_minus_the_model_premium_at_the_alert(self, db):
        alert = index_alert(sigma=0.2, iv=0.2, premium=480.0, synthetic=False)
        trade = H.open_trade(real_book(db.engine), alert)
        model = H.lab_fair_premium(
            strike=H.OPT_U0,
            opt_type="CE",
            expiry="2026-11-24",
            underlying=H.OPT_U0,
            ts=H.CREATED,
            iv=0.2,
            rate=RATE,
        )
        assert pricing(trade)["iv_source"] == "alert"
        assert pricing(trade)["basis_paise"] == H.to_paise(480.0) - H.to_paise(model)

    def test_a_synthetic_alert_records_no_basis(self, db):
        trade = H.open_trade(real_book(db.engine), index_alert(sigma=0.2, iv=0.2))
        assert "basis_paise" not in pricing(trade)

    def test_a_chain_alert_whose_iv_is_implied_has_a_basis_of_about_zero(self, db):
        trade = H.open_trade(real_book(db.engine), index_alert(sigma=0.2, synthetic=False))
        assert pricing(trade)["iv_source"] == "implied"
        assert abs(pricing(trade)["basis_paise"]) <= 5  # the IV reproduces the quote to a few paise

    def test_the_basis_is_not_applied_to_the_fill(self, db):
        """Same IV, same bars: a chain alert 30 rupees above the model fills exactly like a synthetic
        one (the basis is applied in US-023, not here)."""
        book = real_book(db.engine)
        cheap = index_alert("C1", sigma=0.2, iv=0.2, premium=450.0, synthetic=False)
        rich = index_alert("C2", sigma=0.2, iv=0.2, premium=480.0, synthetic=False)
        H.open_trade(book, cheap)
        H.open_trade(book, rich)
        book.step(H.at(10, 8), H.Feed(BANKNIFTY=index_frame()))
        assert (
            H.trade_row(db.engine, "C1")["entry_paise"]
            == H.trade_row(db.engine, "C2")["entry_paise"]
        )
        assert (
            H.trade_row(db.engine, "C1")["risk_paise"] == H.trade_row(db.engine, "C2")["risk_paise"]
        )
