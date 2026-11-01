"""US-001 AC4 and AC5: the paper book is evidence, not permission, and it is always one unit.

AC4  A risk-blocked, low-confidence or already-expired alert still opens a paper trade, and
     `governor.snapshot().as_dict()` is identical before and after: the paper book never
     touches the risk governor (ADR-001 option A was rejected for exactly that).
     (The clause "including after a paper loss larger than daily_loss_limit_inr closes" moved to
     US-002 by Manager decision: it needs a trade that closes with a loss, which is US-002's
     management.)

AC5  An index alert is exactly one lot of the alerted option and a stock alert exactly one
     share, even when risk.lots_per_trade is 5 or the capital is huge, and even when the alert
     itself claims a larger quantity: the lot comes from economics.option.lot.
"""

from __future__ import annotations

import copy

import pytest
import us001_helpers as H

from onelot.clock import now_ist
from onelot.config import AppConfig


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


def forbid_governor_mutation(monkeypatch) -> None:
    """Any call that changes the RiskGovernor now fails the test that makes it."""
    from onelot.risk.governor import RiskGovernor

    def refuse(name):
        def boom(self, *_a, **_k):
            raise AssertionError(f"the paper book must not call RiskGovernor.{name}")

        return boom

    for name in (
        "record_open",
        "record_close",
        "halt",
        "manual_panic_halt",
        "reset_halt",
        "sync_open_positions",
    ):
        monkeypatch.setattr(RiskGovernor, name, refuse(name))


def warmed_desk(app_cfg, desk_db, book):
    d, notifier = H.new_desk(app_cfg, desk_db, paper=book)
    d.poll()
    assert not d.signals, "harness: a stray signal fired during the first poll"
    return d, notifier


class TestStillOpened:
    @pytest.mark.parametrize(
        "kw",
        [
            {"risk_allowed": False},
            {"score": 3.0, "label": "low"},
            {"score": 0.0, "label": "low"},
            {"status": "EXPIRED"},
            {"risk_allowed": False, "score": 3.0, "label": "low", "status": "EXPIRED"},
        ],
        ids=["risk-blocked", "low-confidence", "zero-confidence", "expired", "all-three"],
    )
    @pytest.mark.parametrize("kind", ["STOCK", "INDEX"])
    def test_the_alert_still_opens_a_paper_trade(self, db, kw, kind):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1", kind=kind, **kw)
        assert trade is not None
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "PENDING_FILL"
        assert row["confidence"] == pytest.approx(kw.get("score", 61.5))

    def test_an_alert_that_is_already_expired_when_on_alert_runs_still_opens(self, db):
        """expires_at 10:15:03 is long gone at 10:40 (a late hook, a startup reconcile)."""
        book = H.make_book(db.engine)
        alert = H.make_alert("A1", status="EXPIRED")
        trade = book.on_alert(copy.deepcopy(alert), H.at(10, 40))
        assert trade is not None and H.val(trade.status) == "PENDING_FILL"
        assert H.same_instant(H.trade_row(db.engine, "A1")["expires_at"], H.EXPIRES)

    def test_a_blocked_alert_fills_like_any_other(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", risk_allowed=False)
        book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 100030


class TestTheGovernorIsNeverTouched:
    def test_no_governor_mutator_is_reached_by_the_book(self, db, monkeypatch):
        forbid_governor_mutation(monkeypatch)
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="F1", risk_allowed=False)
        H.open_trade(book, alert_id="G1", stop=1005.0, instrument_key="RELIANCE")
        H.open_trade(book, alert_id="O1", kind="INDEX", direction="short")
        book.step(
            H.at(10, 8),
            H.Feed(
                SBIN=H.standard_bars(),
                RELIANCE=H.standard_bars(),
                BANKNIFTY=H.minute_bars(H.at(10, 5), [54000.0, 53950.0, 53940.0]),
            ),
        )
        statuses = {r["alert_id"]: H.val(r["status"]) for r in H.trade_rows(db.engine)}
        assert statuses == {"F1": "OPEN", "G1": "NO_FILL", "O1": "OPEN"}

    @pytest.mark.parametrize("mode", ["allowed", "halted", "daily-loss-limit"])
    def test_the_governor_snapshot_is_identical_before_and_after_paper_activity(
        self, app_cfg, desk_db, monkeypatch, mode
    ):
        book = H.desk_book(app_cfg, desk_db.engine)
        d, notifier = warmed_desk(app_cfg, desk_db, book)
        gov = d.governor
        if mode == "halted":
            gov.manual_panic_halt()
        elif mode == "daily-loss-limit":
            gov.realized_pnl = -(app_cfg.risk.daily_loss_limit_inr + 1)
            assert gov.may_open(now_ist())[0] is False  # the desk's own check trips the halt first
        before = copy.deepcopy(gov.snapshot().as_dict())
        forbid_governor_mutation(monkeypatch)

        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        assert alert["risk_allowed"] is (mode == "allowed")
        assert [H.val(r["status"]) for r in H.trade_rows(desk_db.engine)] == ["PENDING_FILL"]
        d.poll()  # the paper book fills (or closes) on the next poll
        d.poll()

        (row,) = H.trade_rows(desk_db.engine)
        assert H.val(row["status"]) in ("OPEN", "NO_FILL"), "the paper book did not act"
        assert gov.snapshot().as_dict() == before
        assert d.snapshot()["risk"] == before
        assert d.positions == {} and H.count_rows(desk_db.engine, "desk_positions") == 0

    def test_a_manually_taken_position_and_a_paper_trade_do_not_share_counters(
        self, app_cfg, desk_db
    ):
        """Taking the alert by hand books the owner's position and the governor's counters; the
        paper trade is not a second position and stays one row."""
        book = H.desk_book(app_cfg, desk_db.engine)
        d, _ = warmed_desk(app_cfg, desk_db, book)
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        assert d.acknowledge(alert["id"], "TAKE")["accepted"]
        assert d.governor.snapshot().open_positions == 1 and d.governor.trades_taken == 1
        assert len(d.positions) == 1 and len(H.trade_rows(desk_db.engine)) == 1


class TestOneUnitAlways:
    def test_a_stock_is_one_share_even_when_the_alert_claims_more(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1", eco_qty=500)
        assert trade.qty == 1
        assert H.trade_row(db.engine, "A1")["qty"] == 1
        assert H.typeof(db.engine, "qty", "A1") == "integer"

    @pytest.mark.parametrize("eco_qty", [None, 5 * H.LOT, 1, 7])
    def test_an_index_alert_is_one_lot_of_the_alerted_option(self, db, eco_qty):
        """economics.qty may say five lots (150) or a stray number: the unit is option.lot."""
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1", kind="INDEX", eco_qty=eco_qty)
        assert trade.qty == H.LOT
        assert H.trade_row(db.engine, "A1")["qty"] == H.LOT
        assert H.typeof(db.engine, "qty", "A1") == "integer"

    def test_the_lot_is_that_of_the_alerted_option_not_a_hardcoded_banknifty_lot(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="N1", kind="INDEX", instrument_key="NIFTY_OPT")
        assert trade.qty == H.NIFTY_LOT and trade.instrument_key == "NIFTY_OPT"

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_the_quantity_survives_the_fill_unchanged(self, db, direction):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", eco_qty=500)
        H.open_trade(book, alert_id="O1", kind="INDEX", direction=direction, eco_qty=5 * H.LOT)
        book.step(
            H.at(10, 8),
            H.Feed(
                SBIN=H.standard_bars(),
                BANKNIFTY=H.minute_bars(H.at(10, 5), [54000.0, 54000.0, 54001.0]),
            ),
        )
        assert H.trade_row(db.engine, "A1")["qty"] == 1
        assert H.trade_row(db.engine, "O1")["qty"] == H.LOT

    def test_lots_per_trade_five_and_a_huge_capital_change_nothing_through_the_desk(
        self, app_cfg, desk_db
    ):
        cfg5 = app_cfg.model_copy(
            update={
                "risk": app_cfg.risk.model_copy(update={"lots_per_trade": 5}),
                "account": app_cfg.account.model_copy(update={"capital_inr": 10_000_000_000.0}),
            }
        )
        assert cfg5.risk.lots_per_trade == 5 and cfg5.account.capital_inr > 1e9
        book = H.desk_book(cfg5, desk_db.engine)
        d, _ = warmed_desk(cfg5, desk_db, book)
        stock = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        index = H.raise_alert(d, "BANKNIFTY", now=H.mid_session(d, "BANKNIFTY"))
        lot = d.instruments["BANKNIFTY_OPT"].lot_size
        assert stock["economics"]["qty"] == 1 and index["economics"]["qty"] == lot
        rows = {r["alert_id"]: r for r in H.trade_rows(desk_db.engine)}
        assert rows[stock["id"]]["qty"] == 1
        assert rows[index["id"]]["qty"] == lot
