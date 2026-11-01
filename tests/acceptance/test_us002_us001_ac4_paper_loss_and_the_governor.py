"""US-001 AC4, last clause (moved to US-002 by Manager decision c of US-001).

    "... and `governor.snapshot().as_dict()` is identical before and after a paper LOSS larger
    than `risk.daily_loss_limit_inr` closes."

The paper book is evidence, not permission: a paper trade that loses more than the daily limit
must close, book its loss on its own row, and leave the RiskGovernor exactly as it was (the
reason ADR-001 rejected option A). Three ways to prove it:

* a stock whose one share is dear enough to lose more than the REAL limit (Rs 6,000);
* an index option lot whose stop is wide enough to lose more than the limit;
* the whole Desk wiring (`Desk.poll` steps the book), with the limit set small enough that an
  ordinary 0.5 % stop on a Rs 950 share is a "larger than the limit" loss.

Every `RiskGovernor` mutator is made to fail the test that reaches it.
"""

from __future__ import annotations

import copy

import pytest
import us001_helpers as H
import us002_helpers as U

from onelot.config import AppConfig, RiskCfg
from onelot.risk.governor import RiskGovernor
from onelot.store.repo import Repo


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


def governor(app_cfg, db):
    return RiskGovernor(
        app_cfg.risk, app_cfg.account.capital_inr, app_cfg.mode, H.DAY.isoformat(), Repo(db)
    )


class TestALossLargerThanTheDailyLimitLeavesTheGovernorAlone:
    def test_a_dear_share_stopped_out_loses_more_than_the_limit_and_the_governor_is_identical(
        self, app_cfg, db, monkeypatch
    ):
        limit = app_cfg.risk.daily_loss_limit_inr
        gov = governor(app_cfg, db)
        before = copy.deepcopy(gov.snapshot().as_dict())
        U.forbid_governor_mutation(monkeypatch)

        entry, drop = 100_000.0, limit + 1_000.0  # a Rs 1,00,000 share, stopped Rs 7,000 lower
        book = H.make_book(db.engine)
        alert = U.make_alert("BIG", entry=entry, stop=entry - drop, target=entry + 2 * drop)
        H.open_trade(book, alert)
        fill = (10_000_000, 10_001_000, 9_999_000, 10_000_000)
        stop_bar = (10_000_000, 10_001_000, int((entry - drop) * 100), 9_500_000)
        U.step_book(book, U.pbars(H.at(10, 5), [U.PRE_BAR, fill, stop_bar]))

        row = U.row_of(db.engine, "BIG")
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        assert row["gross_paise"] < -int(limit * 100), "the paper loss is larger than the limit"
        assert gov.snapshot().as_dict() == before
        assert gov.may_open(H.at(10, 30))[0] is True, "the paper loss did not trip the limit"

    def test_an_option_lot_stopped_out_for_more_than_the_limit_and_the_governor_is_identical(
        self, app_cfg, db, monkeypatch
    ):
        limit = app_cfg.risk.daily_loss_limit_inr
        gov = governor(app_cfg, db)
        before = copy.deepcopy(gov.snapshot().as_dict())
        U.forbid_governor_mutation(monkeypatch)

        book = H.make_book(db.engine)
        alert = U.make_alert("OPT", kind="INDEX", stop=53_600.0)  # a 400-point stop on a 30 lot
        H.open_trade(book, alert)
        pre = (5_399_000, 5_399_500, 5_398_500, 5_399_000)
        fill = (5_400_000, 5_401_000, 5_399_000, 5_400_500)
        stop_bar = (5_400_500, 5_400_600, 5_350_000, 5_360_000)
        U.step_book(book, U.pbars(H.at(10, 5), [pre, fill, stop_bar]), series="BANKNIFTY")

        row = U.row_of(db.engine, "OPT")
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        assert row["gross_paise"] < -int(limit * 100), row["gross_paise"]
        assert gov.snapshot().as_dict() == before

    @pytest.mark.parametrize("mode", ["allowed", "halted"])
    def test_a_desk_that_steps_the_book_books_the_loss_and_the_governor_does_not_move(
        self, app_cfg, desk_db, monkeypatch, mode
    ):
        tiny = RiskCfg(**{**app_cfg.risk.model_dump(), "daily_loss_limit_inr": 1.0})
        cfg = app_cfg.model_copy(update={"risk": tiny})
        frames = H.desk_frames()
        book = H.desk_book(cfg, desk_db.engine)
        desk, _ = H.new_desk(cfg, desk_db, paper=book, frames=frames)
        desk.poll()
        assert not desk.signals, "harness: a stray signal fired during the first poll"
        if mode == "halted":
            desk.governor.manual_panic_halt()
        before = copy.deepcopy(desk.governor.snapshot().as_dict())
        U.forbid_governor_mutation(monkeypatch)

        alert = H.raise_alert(desk, "SBIN", now=H.mid_session(desk, "SBIN"))
        # the fill bar is the next one and stays as it was; every bar after it crashes 15 %
        bars = frames["SBIN"]
        crashed = bars.copy()
        crashed.iloc[-39:] = crashed.iloc[-39:] * 0.85
        desk.source.frames["SBIN"] = crashed
        desk.poll()
        desk.poll()

        (row,) = [r for r in H.trade_rows(desk_db.engine) if r["alert_id"] == alert["id"]]
        assert H.val(row["status"]) == "CLOSED" and row["close_reason"] == "STOP"
        assert row["gross_paise"] < -100, "a paper loss larger than the Rs 1 daily limit"
        assert desk.governor.snapshot().as_dict() == before
        assert desk.snapshot()["risk"] == before
        assert desk.positions == {} and H.count_rows(desk_db.engine, "desk_positions") == 0
