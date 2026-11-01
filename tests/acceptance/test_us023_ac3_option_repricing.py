"""US-023 AC3: an index option trade is repriced from the underlying at every step.

    open option trades: each step marks the premium from the UNDERLYING CLOSE of the bars managed
    so far with the IV frozen at the alert, stored as plan["position"]["mark_paise"] =
    max(to_paise(premium_at(option, last close, last close time)) + basis, one tick); a failed
    mark is null, never 0, with ONE PRICING_UNAVAILABLE (leg "mark") per failure streak; the close
    fill is round_to_tick DOWN of premium x (1 - spread) - slippage ticks x tick, floored at one
    tick; expiry day stays finite; a NaN (or non-positive) VIX falls back; a differential test
    against `lab.execution.execute_trade` agrees to zero paise with a fake pricer.

    Manager decision 1 (2026-11-02): a LIVE-CHAIN alert's premium is model + basis EVERYWHERE, in
    the entry fill, the exit fill and the marks, so the trade shows no profit at birth; a synthetic
    alert has basis 0 and is unchanged. The Lab knows no basis, so a chain trade is checked against
    `execute_trade` run on a pricer shifted by the basis (`W.shifted`).

The fake pricer (`H.make_pricer`) is linear in the underlying and independent of strike, expiry and
time, so every expected number is exact: CE premium = 450 + 0.65 x (underlying - 54000), PE the
mirror, floored at 0.05. Tests that need a real Black-76 premium use the book the Engine builds
(`H.real_pricing_book`) and an independent Lab pricer.

Today no step marks anything (plan["position"] has no mark_paise) and the unpriced-mark event does
not exist, so the marking tests fail on that until US-023 stores it.
"""

from __future__ import annotations

import datetime as dt
import math
from fractions import Fraction

import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W

from onelot.config import AppConfig, BacktestCfg
from onelot.desk.economics import index_option_economics
from onelot.lab.execution import OptionPricer

NAN, INF = float("nan"), float("inf")

# Closes 54005, 54050, 54080, 54000 (CE and PE alike: none touches 53850/54300 or 54150/53700)
CE_ROWS = [
    W.CE_FILL,
    (5400500, 5405500, 5400000, 5405000),
    (5405000, 5409000, 5403000, 5408000),
    (5408000, 5410000, 5399500, 5400000),
]
PE_ROWS = [
    W.PE_FILL,
    (5399800, 5400200, 5398000, 5398500),
    (5398500, 5399000, 5396000, 5396500),
    (5396500, 5401000, 5395000, 5400000),
]
NOWS = [H.at(10, 7), H.at(10, 8), H.at(10, 9), H.at(10, 10)]  # each closes one more bar


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def closes(direction: str) -> list[int]:
    rows = CE_ROWS if direction == "long" else PE_ROWS
    return [r[3] for r in rows]


def step_through(engine, book, alert, rows, nows=NOWS):
    """Open `alert`, then step the book once per `now`, yielding the stored row after each."""
    H.open_trade(book, alert)
    frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *rows])
    for now in nows:
        U.step_book(book, frame, now, series="BANKNIFTY")
        yield U.row_of(engine, alert["id"])


class TestEachStepMarksTheOpenOptionFromTheUnderlyingClose:
    @pytest.mark.parametrize(
        "direction, chain",
        [("long", False), ("short", False), ("long", True), ("short", True)],
        ids=["ce-synthetic", "pe-synthetic", "ce-live-chain", "pe-live-chain"],
    )
    def test_the_mark_follows_the_last_closed_underlying_with_the_basis(self, db, direction, chain):
        if chain:
            alert = W.chain_alert(direction=direction)
        else:
            alert = U.make_alert("O1", kind="INDEX", direction=direction)
        basis = 525 if chain else 0
        pricer = H.make_pricer()
        book = W.make_book(db.engine, premium_at=pricer)
        rows = CE_ROWS if direction == "long" else PE_ROWS
        seen = []
        stored = step_through(db.engine, book, alert, rows)
        for row, close in zip(stored, closes(direction), strict=True):
            assert row["status"] == "OPEN"
            expected = W.fake_mark(alert, close, basis)
            assert W.mark_of(db.engine) == expected, (close, expected)
            assert type(W.mark_of(db.engine)) is int
            seen.append(expected)
        assert len(set(seen)) == 4, "four different closes, four different marks"

    def test_the_underlying_priced_is_the_close_of_the_last_closed_bar(self, db):
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        pricer = H.make_pricer()
        book = W.make_book(db.engine, premium_at=pricer)
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
        for now, close in zip(NOWS, closes("long"), strict=True):
            before = len(pricer.calls)
            U.step_book(book, frame, now, series="BANKNIFTY")
            asked = {c[1] for c in pricer.calls[before:]}
            assert close / 100 in asked, f"step {now:%H:%M} must price the close {close / 100}"

    def test_a_mark_is_never_below_one_tick_whatever_a_negative_basis_says(self, db):
        """Chain premium 1.00 against a model of 450.00: the basis is -44,900 paise. After a
        slump the model premium is 0.05, the sum is hugely negative, and the mark is exactly
        5 paise (one tick): never 0, never negative (Manager decision 4)."""
        alert = W.chain_alert(chain=1.0, stop=52000.0)  # a wide stop: the slump is managed, not hit
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        slump = [
            W.CE_FILL,
            (5400500, 5400600, 5329000, 5330000),
        ]  # closes 53300: model at the floor
        for _ in step_through(db.engine, book, alert, slump, [H.at(10, 8)]):
            pass
        assert U.row_of(db.engine, "O1")["plan"]["pricing"]["basis_paise"] == 100 - 45000
        assert W.mark_of(db.engine) == 5

    def test_a_premium_that_rounds_to_zero_paise_marks_one_tick_not_zero(self, db):
        """A model premium of 0.001 is a real (positive, finite) price, not a failure; it is 0
        paise, so the mark is floored at one tick and no PRICING_UNAVAILABLE is raised."""
        pricer = W.SwitchablePricer(0.001)
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        book = W.make_book(db.engine, premium_at=pricer)
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
        U.step_book(book, frame, NOWS[0], series="BANKNIFTY")
        pricer.failing = True  # from now on every price is 0.001
        U.step_book(book, frame, NOWS[1], series="BANKNIFTY")
        assert W.mark_of(db.engine) == 5
        assert W.pricing_events(db.engine, "O1", leg="mark") == []

    def test_the_mark_is_stored_in_the_position_record_with_the_state_it_belongs_to(self, db):
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        (row,) = step_through(db.engine, book, alert, CE_ROWS, [NOWS[1]])
        position = row["plan"]["position"]
        assert position["last_close_paise"] == 5405000 and "mark_paise" in position
        assert position["mark_paise"] == W.fake_mark(alert, 5405000)

    def test_a_step_with_no_newly_closed_bar_leaves_the_mark_and_the_events_alone(self, db):
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
        U.step_book(book, frame, H.at(10, 8), series="BANKNIFTY")
        before = (W.mark_of(db.engine), H.event_rows(db.engine, "O1"))
        # 10:08:59: the 10:08 bar has not closed; the mark must not move
        assert U.step_book(book, frame, H.at(10, 8, 59), series="BANKNIFTY") == []
        assert (W.mark_of(db.engine), H.event_rows(db.engine, "O1")) == before
        assert W.mark_of(db.engine) == W.fake_mark(alert, 5405000)


class TestTheIvIsFrozenAtTheAlert:
    def test_every_repricing_is_asked_with_the_alerts_iv(self, db):
        alert = U.make_alert("O1", kind="INDEX", direction="long", iv=0.16)
        pricer = H.make_pricer()
        book = W.make_book(db.engine, premium_at=pricer)
        for _ in step_through(db.engine, book, alert, CE_ROWS):
            pass
        assert len(pricer.calls) >= 6, "entry, risk and a mark per step"
        assert {c[0]["iv"] for c in pricer.calls} == {0.16}

    def test_an_iv_implied_at_the_alert_is_the_one_every_later_mark_uses(self, db):
        alert = W.chain_alert()
        del alert["economics"]["option"]["iv"]
        pricer = H.make_pricer()
        book = W.make_book(db.engine, premium_at=pricer)
        for _ in step_through(db.engine, book, alert, CE_ROWS):
            pass
        ivs = {c[0].get("iv") for c in pricer.calls}
        assert len(ivs) == 1, f"one frozen IV for the whole life of the trade, got {ivs}"
        (iv,) = ivs
        assert isinstance(iv, float) and math.isfinite(iv) and 0.01 < iv < 3.0


class TestALiveChainBasisIsAppliedEverywhere:
    """Manager decision 1: premium = model + basis in the entry fill, the exit fill and the marks.

    The chain alert quotes 455.25 where the fake model says 450.00 (basis +525 paise). The fill
    is `ceil_tick((model + basis) x (1 + spread) + ticks x tick)`: 455.25 x 1.006 + 0.10 = 458.08
    -> 458.10, not the synthetic 452.80."""

    def test_the_entry_fill_is_priced_on_model_plus_basis(self, db):
        alert = W.chain_alert()
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        (row,) = step_through(db.engine, book, alert, CE_ROWS, [NOWS[0]])
        assert row["status"] == "OPEN"
        expected = W.option_entry_oracle(Fraction("450.00") + Fraction(525, 100), 0.006, 2)
        assert row["entry_paise"] == expected == 45810
        synthetic = U.make_alert("S1", kind="INDEX", direction="long")
        _, twin, _ = W.run_option(db.engine, [W.CE_FILL], alert=synthetic)
        assert twin["entry_paise"] == 45280, "a synthetic alert (basis 0) is unchanged"

    def test_a_chain_trade_shows_no_profit_at_birth(self, db):
        """At the alert's own underlying the mark is the chain premium, and the fill is above
        it by the execution friction only (half spread + slippage): the trade starts at or
        below zero, never in profit. (Basis only in the mark would start it +245 paise up.)"""
        alert = W.chain_alert()
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        flat = [W.CE_FILL, (5400000, 5400400, 5399600, 5400000)]  # closes back at 54000.00
        for _ in step_through(db.engine, book, alert, flat, [H.at(10, 8)]):
            pass
        row = U.row_of(db.engine, "O1")
        assert row["plan"]["pricing"]["basis_paise"] == 525
        mark = W.mark_of(db.engine)
        assert mark == 45525 == H.to_paise(455.25)
        friction = round(mark * 0.006) + 2 * 5 + 5  # half spread + 2 ticks + one rounding tick
        assert 0 <= row["entry_paise"] - mark <= friction, (row["entry_paise"], mark)

    @pytest.mark.parametrize("name", ["ce_target", "ce_stop", "pe_target", "pe_stop"])
    def test_the_exit_fill_is_priced_on_model_plus_basis(self, db, name):
        direction, rows, _, _ = W.OPTION_SCENARIOS[name]
        alert = W.chain_alert(direction=direction)
        _, row, events = W.run_option(db.engine, rows, alert=alert)
        close = U.close_of(events)
        fair = H.make_pricer()(
            {"opt_type": "CE" if direction == "long" else "PE"}, close.price / 100, None
        )
        expected = W.option_exit_oracle(Fraction(str(fair)) + Fraction(525, 100), 0.006, 2)
        assert row["status"] == "CLOSED" and row["exit_paise"] == expected, (name, expected)

    @pytest.mark.parametrize("basis_paise", [525, 150, 3000])
    @pytest.mark.parametrize("name", list(W.OPTION_SCENARIOS))
    def test_a_chain_trade_equals_the_labs_row_run_on_a_pricer_shifted_by_the_basis(
        self, db, name, basis_paise
    ):
        direction, rows, _, _ = W.OPTION_SCENARIOS[name]
        chain = 450.0 + basis_paise / 100
        alert = W.chain_alert(direction=direction, chain=chain)
        _, row, events = W.run_option(db.engine, rows, alert=alert)
        assert row["plan"]["pricing"]["basis_paise"] == basis_paise
        close = U.close_of(events)
        lab = W.lab_option_row(
            bt=H.BT,
            spec=W.SPECS["BANKNIFTY_OPT"],
            opt_type="CE" if direction == "long" else "PE",
            underlying_entry=54000.0,
            underlying_exit=close.price / 100,
            entry_ts=H.at(10, 6),
            exit_ts=close.at,
            pricer=W.shifted(H.make_pricer(), basis_paise / 100),
            stop=alert["economics"]["underlying_stop"],
        )
        assert row["entry_paise"] == H.to_paise(lab["detail"]["premium_entry"])
        assert row["exit_paise"] == H.to_paise(lab["detail"]["premium_exit"])
        assert row["gross_paise"] == H.to_paise(lab["gross_pnl"])
        assert row["costs_paise"] is not None
        assert abs(row["costs_paise"] - H.to_paise(lab["costs"])) <= 1
        assert row["net_paise"] == row["gross_paise"] - row["costs_paise"]


class TestMarksWithTheRealBlack76PricerAreFiniteAndOnTheLabsNumbers:
    """The book the Engine builds, over real specs, real cost tables and the Black-76 pricer."""

    def test_the_mark_sits_between_the_labs_premium_at_the_bars_open_and_at_its_close(self, db):
        rate = AppConfig.load().backtest.risk_free_rate
        alert = U.make_alert(
            "O1", kind="INDEX", direction="long"
        )  # strike 54000, expiry 2026-11-24
        book = H.real_pricing_book(db.engine)
        bar_opens = [H.at(10, 6), H.at(10, 7), H.at(10, 8), H.at(10, 9)]
        stored = step_through(db.engine, book, alert, CE_ROWS)
        for _row, close, bar_open in zip(stored, closes("long"), bar_opens, strict=True):
            low, high = sorted(
                H.lab_fair_premium(
                    strike=54000.0,
                    opt_type="CE",
                    expiry="2026-11-24",
                    underlying=close / 100,
                    ts=ts,
                    iv=0.16,
                    rate=rate,
                )
                for ts in (bar_open, bar_open + dt.timedelta(minutes=1))
            )
            mark = W.mark_of(db.engine)
            assert isinstance(mark, int) and mark > 0, mark
            assert H.to_paise(low) - 1 <= mark <= H.to_paise(high) + 1, (mark, low, high)

    @pytest.mark.parametrize("strike, direction", [(53800.0, "long"), (54200.0, "short")])
    def test_expiry_day_marks_stay_finite_from_the_open_to_the_last_minute(
        self, db, strike, direction
    ):
        """Expiry 2026-10-29 is the trade's own day (15:30 IST). An in-the-money option is worth
        about its intrinsic value at the end; nothing is NaN, infinite, zero or a float."""
        alert = U.make_alert(
            "O1", kind="INDEX", direction=direction, expiry="2026-10-29", strike=strike
        )
        book = H.real_pricing_book(db.engine)
        H.open_trade(book, alert)
        stamps = pd.date_range(H.at(10, 5), H.at(15, 20), freq="min")
        wiggle = [(-1) ** i * (i % 7) * 5 for i in range(len(stamps))]  # +-30 paise-ish noise
        rows = [(5400000 + w, 5400100 + w, 5399900 + w, 5400000 + w) for w in wiggle]
        frame = U.pbars_at(list(stamps), rows)
        seen = []
        for now in (H.at(10, 7), H.at(12, 0), H.at(14, 59), H.at(15, 14)):
            U.step_book(book, frame, now, series="BANKNIFTY")
            row = U.row_of(db.engine, "O1")
            assert row["status"] == "OPEN", (now, row["status"], row["close_reason"])
            mark = W.mark_of(db.engine)
            assert isinstance(mark, int) and mark > 0, (now, mark)
            seen.append(mark)
        intrinsic = 20000  # 200.00 rupees in paise: 54000 against a 53800 call / 54200 put
        assert abs(seen[-1] - intrinsic) < 300, f"15:14 on expiry day ~ intrinsic, got {seen[-1]}"
        assert book.status()["hook_errors"] == 0

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_the_expiry_day_close_is_priced_and_costed_to_whole_paise(self, db, direction):
        alert = U.make_alert("O1", kind="INDEX", direction=direction, expiry="2026-10-29")
        book = H.real_pricing_book(db.engine)
        H.open_trade(book, alert)
        stamps = pd.date_range(H.at(10, 5), H.at(15, 20), freq="min")
        rows = [(5400000, 5400100, 5399900, 5400000)] * len(stamps)
        frame = U.pbars_at(list(stamps), rows)
        U.step_book(book, frame, H.at(15, 16), series="BANKNIFTY")
        row = U.row_of(db.engine, "O1")
        assert (row["status"], row["close_reason"]) == ("CLOSED", "SESSION_END")
        for column in ("entry_paise", "exit_paise", "gross_paise", "costs_paise", "net_paise"):
            assert type(row[column]) is int, (
                f"{column} must be a finite integer, got {row[column]!r}"
            )
        assert row["exit_paise"] >= 5, "never below one tick"
        assert row["net_paise"] == row["gross_paise"] - row["costs_paise"]

    def test_a_trade_whose_premium_is_worthless_at_expiry_is_never_nan_or_negative(self, db):
        """A deep out-of-the-money call on expiry day: the model gives ~0. The mark is then
        unavailable (null) or at least one tick (5 paise), never 0 or NaN, and the book never
        raises."""
        alert = U.make_alert(
            "O1", kind="INDEX", direction="long", expiry="2026-10-29", strike=56000.0
        )
        book = H.real_pricing_book(db.engine)
        H.open_trade(book, alert)
        stamps = pd.date_range(H.at(10, 5), H.at(15, 20), freq="min")
        frame = U.pbars_at(list(stamps), [(5400000, 5400100, 5399900, 5400000)] * len(stamps))
        U.step_book(book, frame, H.at(15, 14), series="BANKNIFTY")
        mark = W.mark_of(db.engine) if U.row_of(db.engine, "O1")["status"] == "OPEN" else None
        assert mark is None or (isinstance(mark, int) and mark >= 5)
        assert book.status()["hook_errors"] == 0


BAD_MARKS = [
    pytest.param(None, id="none"),
    pytest.param(RuntimeError("pricer down"), id="raises"),
    pytest.param(NAN, id="nan"),
    pytest.param(INF, id="inf"),
    pytest.param(0.0, id="zero"),
    pytest.param(-5.0, id="negative"),
    pytest.param("n/a", id="string"),
    pytest.param(True, id="bool"),
]


class TestARepricingThatFailsIsNullAndSaidSoNeverAZero:
    def fill_then(self, db, bad):
        pricer = W.SwitchablePricer(bad)
        book = W.make_book(db.engine, premium_at=pricer)
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
        U.step_book(book, frame, NOWS[0], series="BANKNIFTY")
        assert W.mark_of(db.engine) == W.fake_mark(alert, 5400500), "a good mark to start from"
        return book, pricer, alert, frame

    @pytest.mark.parametrize("bad", BAD_MARKS)
    def test_the_mark_is_null_and_the_event_names_the_mark_leg(self, db, bad):
        book, pricer, _, frame = self.fill_then(db, bad)
        pricer.failing = True
        U.step_book(book, frame, NOWS[1], series="BANKNIFTY")
        assert U.row_of(db.engine, "O1")["status"] == "OPEN", "a failed mark never closes a trade"
        position = W.position_of(db.engine)
        assert position.get("mark_paise") is None, "null: not 0, and not the previous mark either"
        events = W.pricing_events(db.engine, "O1", leg="mark")
        assert len(events) == 1, "one PRICING_UNAVAILABLE for the mark that failed"
        assert book.status()["hook_errors"] == 0
        assert [t.alert_id for t in book.open_trades()] == ["O1"]

    @pytest.mark.parametrize("bad", [pytest.param(None, id="none"), pytest.param(NAN, id="nan")])
    def test_stepping_the_same_bars_again_says_nothing_new(self, db, bad):
        book, pricer, _, frame = self.fill_then(db, bad)
        pricer.failing = True
        U.step_book(book, frame, NOWS[1], series="BANKNIFTY")
        before = H.event_rows(db.engine, "O1")
        assert U.step_book(book, frame, H.at(10, 8, 30), series="BANKNIFTY") == []
        assert H.event_rows(db.engine, "O1") == before
        assert W.position_of(db.engine).get("mark_paise") is None

    def test_a_pricer_that_recovers_marks_again_and_a_new_failure_is_a_new_event(self, db):
        book, pricer, alert, frame = self.fill_then(db, None)
        pricer.failing = True
        U.step_book(book, frame, NOWS[1], series="BANKNIFTY")
        assert len(W.pricing_events(db.engine, "O1", leg="mark")) == 1
        pricer.failing = False
        U.step_book(book, frame, NOWS[2], series="BANKNIFTY")
        assert W.mark_of(db.engine) == W.fake_mark(alert, 5408000)
        assert len(W.pricing_events(db.engine, "O1", leg="mark")) == 1, "recovery adds nothing"
        pricer.failing = True
        U.step_book(book, frame, NOWS[3], series="BANKNIFTY")
        assert W.position_of(db.engine).get("mark_paise") is None
        assert len(W.pricing_events(db.engine, "O1", leg="mark")) == 2

    def test_a_streak_of_failing_steps_is_one_event_not_one_per_step(self, db):
        """Manager decision 4: one PRICING_UNAVAILABLE (leg 'mark') per failure STREAK. Three
        steps with new bars that all fail say it once; a recovery ends the streak and a later
        failure starts a new one."""
        book, pricer, alert, frame = self.fill_then(db, None)
        pricer.failing = True
        for now in NOWS[1:]:
            U.step_book(book, frame, now, series="BANKNIFTY")
            assert W.position_of(db.engine).get("mark_paise") is None
        assert len(W.pricing_events(db.engine, "O1", leg="mark")) == 1
        pricer.failing = False
        longer = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS, (5400000, 5404000, 5399000, 5403000)])
        U.step_book(book, longer, H.at(10, 11), series="BANKNIFTY")
        assert W.mark_of(db.engine) == W.fake_mark(alert, 5403000), "the streak is over"
        pricer.failing = True
        longest = U.pbars(
            H.at(10, 5),
            [
                W.OPT_PRE,
                *CE_ROWS,
                (5400000, 5404000, 5399000, 5403000),
                (5403000, 5404000, 5402000, 5403500),
            ],
        )
        U.step_book(book, longest, H.at(10, 12), series="BANKNIFTY")
        assert len(W.pricing_events(db.engine, "O1", leg="mark")) == 2, "a new streak, a new event"

    def test_the_unpriced_mark_does_not_lose_the_trades_management(self, db):
        """The mark is a read-out: the stop, the target and the trail keep working without it."""
        book, pricer, _, _ = self.fill_then(db, None)
        pricer.failing = True
        target_frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *W.OPTION_SCENARIOS["ce_target"][1]])
        U.step_book(book, target_frame, H.at(10, 9), series="BANKNIFTY")
        row = U.row_of(db.engine, "O1")
        assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET")
        assert row["exit_paise"] is None, "the exit could not be priced either: NULL, not 0"


class TestTheCloseFillIsRoundedDownAndNeverBelowOneTick:
    """exit = round_to_tick DOWN of premium x (1 - spread) - ticks x tick, at least one tick."""

    @staticmethod
    def exit_priced_at(db, fair, spread, ticks):
        def pricer(option, underlying, ts):
            return 450.0 if ts <= H.at(10, 6) else fair

        bt = BacktestCfg(slippage_pct=0.03, option_slippage_ticks=ticks, option_spread_pct=spread)
        _, rows, _, _ = W.OPTION_SCENARIOS["ce_target"]
        _, row, _ = W.run_option(db.engine, rows, pricer=pricer, bt=bt)
        return row

    @pytest.mark.parametrize(
        "fair",
        [0.05, 0.06, 0.10, 0.11, 1.0, 3.33, 100.0, 100.01, 100.04, 100.05, 452.78, 999.99, 5000.55],
    )
    @pytest.mark.parametrize("spread, ticks", [(0.006, 2), (0.0, 0), (0.02, 3), (0.0, 2)])
    def test_the_exit_is_the_floored_tick_of_the_spread_and_slippage_adjusted_premium(
        self, db, fair, spread, ticks
    ):
        row = self.exit_priced_at(db, fair, spread, ticks)
        assert row["close_reason"] == "TARGET" and row["exit_paise"] is not None
        expected = W.option_exit_oracle(fair, spread, ticks)
        assert row["exit_paise"] == expected, (fair, spread, ticks)
        assert row["exit_paise"] % 5 == 0 and row["exit_paise"] >= 5
        assert row["gross_paise"] == (expected - row["entry_paise"]) * row["qty"]

    def test_a_premium_that_slippage_would_take_below_zero_exits_at_exactly_one_tick(self, db):
        row = self.exit_priced_at(db, 0.06, 0.006, 2)
        assert row["exit_paise"] == 5, "0.06 x 0.994 - 0.10 < 0: floored at one tick (0.05)"
        assert row["net_paise"] == row["gross_paise"] - row["costs_paise"]

    def test_a_premium_exactly_on_a_tick_after_adjustment_is_not_floored_a_tick_lower(self, db):
        row = self.exit_priced_at(db, 100.0, 0.0, 2)
        assert row["exit_paise"] == 9990, "100.00 - 2 x 0.05 = 99.90 is on the grid: it stays"


class TestDifferentialAgainstTheLabsExecuteTrade:
    """The Lab books the same round trip through `execute_trade`; with the same fake pricer the
    paper book agrees to ZERO paise on entry, exit, gross and risk, to 1 paisa on costs (the Lab
    rounds the round trip once, the book each leg once) and net follows."""

    @pytest.mark.parametrize("name", list(W.OPTION_SCENARIOS))
    @pytest.mark.parametrize("instrument", ["BANKNIFTY_OPT", "NIFTY_OPT"])
    def test_the_paper_trade_equals_the_labs_row(self, db, name, instrument):
        direction, rows, _, _ = W.OPTION_SCENARIOS[name]
        alert = U.make_alert("O1", kind="INDEX", direction=direction, instrument_key=instrument)
        _, row, events = W.run_option(db.engine, rows, alert=alert)
        close = U.close_of(events)
        assert row["status"] == "CLOSED" and close is not None
        opt = "CE" if direction == "long" else "PE"
        stop = alert["economics"]["underlying_stop"]
        lab = W.lab_option_row(
            bt=H.BT,
            spec=W.SPECS[instrument],
            opt_type=opt,
            underlying_entry=54000.0,
            underlying_exit=close.price / 100,
            entry_ts=H.at(10, 6),
            exit_ts=close.at,
            pricer=H.make_pricer(),
            stop=stop,
        )
        assert row["entry_paise"] == H.to_paise(lab["detail"]["premium_entry"])
        assert row["exit_paise"] == H.to_paise(lab["detail"]["premium_exit"])
        assert row["gross_paise"] == H.to_paise(lab["gross_pnl"])
        assert row["risk_paise"] == H.to_paise(lab["risk_inr"])
        assert row["costs_paise"] is not None and row["net_paise"] is not None
        lab_costs = int(H.to_paise(lab["costs"]))
        assert abs(row["costs_paise"] - lab_costs) <= 1, (row["costs_paise"], lab_costs)
        assert abs(row["net_paise"] - H.to_paise(lab["net_pnl"])) <= 1
        r_paper = row["net_paise"] / row["risk_paise"]
        assert abs(r_paper - lab["r_multiple"]) < 1e-3, (r_paper, lab["r_multiple"])


class TestANanVixFallsBack:
    """India VIX feeds the synthetic alert's IV. A NaN last close (a bad Yahoo bar) is truthy in
    Python, so `if vix` lets it through and every figure of the alert becomes NaN; the paper book
    then freezes a missing IV. It must fall back to the constant IV, like no VIX at all."""

    BT = BacktestCfg()
    NOW = H.at(10, 5, 3)

    def economics(self, vix):
        spec = H.SPECS["BANKNIFTY_OPT"]
        return index_option_economics(spec, "long", 54000.0, 53850.0, 2.0, self.NOW, vix, self.BT)

    @pytest.mark.parametrize(
        "vix",
        [NAN, INF, -INF, -3.0, -0.01, 0.0],
        ids=["nan", "inf", "-inf", "-3", "-0.01", "zero"],
    )
    def test_the_alert_economics_equal_the_no_vix_fallback_and_are_finite(self, vix):
        fallback = self.economics(None)
        got = self.economics(vix)
        figures = [got.option["iv"], got.option["premium"], got.costs_inr, got.risk_inr]
        figures += [got.reward_inr, got.rr_net, got.entry_price, got.outlay]
        assert all(math.isfinite(x) for x in figures), figures
        assert got.option["iv"] == fallback.option["iv"] == self.BT.option_iv_constant
        assert got.as_dict() == fallback.as_dict()

    def test_a_good_vix_is_still_used(self):
        """Guard against the cure that ignores VIX altogether."""
        ts = pd.Timestamp(self.NOW)
        pricer = OptionPricer(self.BT, pd.Series([14.0], index=[ts.normalize()]))
        assert pricer.iv_for(ts) == pytest.approx(0.14 * self.BT.option_iv_mult)
        assert self.economics(14.0).option["iv"] == pytest.approx(0.161, abs=1e-4)
