"""US-023 AC3 (the screen): GET /api/paper exposes `mark` and `unrealised` for OPEN trades, and
the net of closed ones.

    /api/paper trades[i]  (US-030: money as exact rupee strings from integer paise, None = JSON null)
        mark        per-unit premium mark of an OPEN option, = plan["position"]["mark_paise"]
        unrealised  (mark - entry) x qty for an OPEN option, gross of costs
        both null   when the mark failed ("never 0"), and for PENDING_FILL / CLOSED / NO_FILL rows
        net         the closed trade's net, now that US-023 computes it
    totals_today.net_rupees   the sum of today's closed nets (null until costs exist)

PREREQUISITE: GET /api/paper is US-030 (branch story/US-030-paper-view). It is not on this branch
yet; until it is merged every test here fails in `W.paper_api` with that message. After the merge
they go green only when US-023 adds `mark`, `unrealised` and the booked net.
"""

from __future__ import annotations

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W

from onelot.clock import now_ist

NAN = float("nan")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


FRAME_ROWS = {
    "long": [W.CE_FILL, (5400500, 5405500, 5400000, 5405000)],  # last close 54050.00
    "short": [W.PE_FILL, (5399800, 5400200, 5398000, 5398500)],  # last close 53985.00
}
LAST_CLOSE = {"long": 5405000, "short": 5398500}


def open_option(db, direction, *, chain=False, pricer=None):
    alert = (
        W.chain_alert(direction=direction)
        if chain
        else U.make_alert("O1", kind="INDEX", direction=direction)
    )
    book = W.make_book(db.engine, premium_at=pricer or H.make_pricer())
    H.open_trade(book, alert)
    frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *FRAME_ROWS[direction]])
    U.step_book(book, frame, H.at(10, 8), series="BANKNIFTY")
    return book, alert, frame


class TestTheEndpointIsThere:
    def test_the_paper_endpoint_serves_the_book(self, db):
        book = W.make_book(db.engine)
        body = W.paper_api(book)
        assert set(body) == {"enabled", "status", "trades", "totals_today"}


class TestAnOpenOptionShowsItsMarkAndUnrealised:
    @pytest.mark.parametrize("direction", ["long", "short"])
    @pytest.mark.parametrize("chain", [False, True], ids=["synthetic", "live-chain"])
    def test_mark_and_unrealised_are_exact_rupee_strings_of_the_stored_mark(
        self, db, direction, chain
    ):
        book, alert, _ = open_option(db, direction, chain=chain)
        row = U.row_of(db.engine, "O1")
        assert row["status"] == "OPEN"
        mark = W.fake_mark(alert, LAST_CLOSE[direction], 525 if chain else 0)
        assert W.mark_of(db.engine) == mark
        unrealised = (mark - row["entry_paise"]) * row["qty"]
        trade = W.api_trade(book, "O1")
        assert trade["status"] == "OPEN"
        assert trade["mark"] == W.rupees(mark)
        assert trade["unrealised"] == W.rupees(unrealised)

    def test_the_worked_numbers(self, db):
        """BUY CE on a LIVE-CHAIN alert (premium 455.25, model 450.00, basis 5.25): the fill is
        priced on model + basis, ceil_tick(455.25 x 1.006 + 0.10) = 458.10; the underlying
        closes 54050 -> fair 482.50 + basis 5.25 = mark 487.75; unrealised (487.75 - 458.10) x 30
        = 889.50. (A synthetic alert fills at 452.80 and has no basis.)"""
        book, _, _ = open_option(db, "long", chain=True)
        trade = W.api_trade(book, "O1")
        assert (trade["entry"], trade["mark"], trade["unrealised"]) == (
            "458.10",
            "487.75",
            "889.50",
        )

    def test_the_synthetic_worked_numbers(self, db):
        """BUY CE on a synthetic alert: fill 452.80 (no basis); fair 482.50 at 54050; unrealised
        (482.50 - 452.80) x 30 = 891.00."""
        book, _, _ = open_option(db, "long", chain=False)
        trade = W.api_trade(book, "O1")
        assert (trade["entry"], trade["mark"], trade["unrealised"]) == (
            "452.80",
            "482.50",
            "891.00",
        )

    def test_a_losing_open_trade_shows_a_negative_unrealised(self, db):
        """BUY CE filled at 452.80; the underlying closes 53985 (down 15): fair 450 - 9.75 =
        440.25, so unrealised is (440.25 - 452.80) x 30 = -376.50."""
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        book = W.make_book(db.engine, premium_at=H.make_pricer())
        H.open_trade(book, alert)
        down = [W.CE_FILL, (5400500, 5400600, 5398000, 5398500)]  # close 53985.00
        U.step_book(book, U.pbars(H.at(10, 5), [W.OPT_PRE, *down]), H.at(10, 8), series="BANKNIFTY")
        trade = W.api_trade(book, "O1")
        assert (trade["mark"], trade["unrealised"]) == ("440.25", "-376.50")

    def test_the_mark_is_not_the_entry_and_not_the_exit_it_is_a_read_out(self, db):
        book, _, _ = open_option(db, "long")
        trade = W.api_trade(book, "O1")
        assert trade["exit"] is None and trade["gross"] is None and trade["net"] is None
        assert trade["mark"] is not None and trade["mark"] != trade["entry"]


class TestAFailedMarkIsNullNeverZero:
    @pytest.mark.parametrize("bad", [None, NAN, RuntimeError("pricer down"), 0.0])
    def test_mark_and_unrealised_are_json_null_when_repricing_failed(self, db, bad):
        pricer = W.SwitchablePricer(bad)
        book, alert, frame = open_option(db, "long", pricer=pricer)
        assert W.api_trade(book, "O1")["mark"] is not None, "a priced mark first"
        pricer.failing = True
        bars = U.pbars(
            H.at(10, 5),
            [W.OPT_PRE, *FRAME_ROWS["long"], (5405000, 5409000, 5403000, 5408000)],
        )
        U.step_book(book, bars, H.at(10, 9), series="BANKNIFTY")
        trade = W.api_trade(book, "O1")
        assert trade["status"] == "OPEN"
        assert trade["mark"] is None and trade["unrealised"] is None
        assert trade["mark"] != "0.00" and trade["unrealised"] != "0.00"
        assert trade["entry"] == "452.80", "the rest of the row is untouched"


class TestOnlyOpenTradesShowAMark:
    def test_pending_closed_and_no_fill_trades_carry_no_mark(self, db):
        book = W.make_book(db.engine)
        pending = W.stock_alert("P1", key="SBIN")
        closed = W.stock_alert("C1", key="RELIANCE")
        gapped = W.stock_alert("G1", key="HDFCBANK")
        for alert in (pending, closed, gapped):
            H.open_trade(book, alert)
        gap_bars = H.minute_bars(H.at(10, 5), [1000.0, 980.0, 980.0])  # opens beyond the stop
        feed = H.Feed(
            RELIANCE=U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(closed, "target")]),
            HDFCBANK=gap_bars,
        )
        book.step(H.at(10, 9), feed)
        statuses = {t["alert_id"]: t for t in W.paper_api(book)["trades"]}
        assert {k: v["status"] for k, v in statuses.items()} == {
            "P1": "PENDING_FILL",
            "C1": "CLOSED",
            "G1": "NO_FILL",
        }
        for alert_id, trade in statuses.items():
            assert trade.get("mark") is None, alert_id
            assert trade.get("unrealised") is None, alert_id


class TestTheClosedTradeShowsItsNet:
    @staticmethod
    def today_book(db):
        """Two stock trades raised and closed on TODAY's IST date (the endpoint's 'today' is the
        wall clock's): a winner (net 1863) and a loser (net -1165)."""
        today = now_ist().date()
        book = W.make_book(db.engine)
        feed = {}
        for alert_id, key, hit in (("T1", "SBIN", "target"), ("T2", "RELIANCE", "stop")):
            alert = W.stock_alert(alert_id, key=key, created=H.at(10, 5, 3, day=today))
            H.open_trade(book, alert, now=H.at(10, 5, 3, day=today))
            rows = [U.PRE_BAR, *W.stock_rows(alert, hit)]
            feed[key] = U.pbars(H.at(10, 5, day=today), rows)
        book.step(H.at(10, 9, day=today), H.Feed(**feed))
        return book

    def test_a_closed_trade_shows_its_gross_and_net_in_rupees(self, db):
        book = self.today_book(db)
        winner, loser = W.api_trade(book, "T1"), W.api_trade(book, "T2")
        assert (winner["gross"], winner["net"]) == ("19.70", "18.63")
        assert (loser["gross"], loser["net"]) == ("-10.60", "-11.65")
        assert winner.get("mark") is None and winner.get("unrealised") is None

    def test_todays_totals_show_the_net_once_costs_exist(self, db):
        book = self.today_book(db)
        totals = W.paper_api(book)["totals_today"]
        assert totals == {
            "closed": 2,
            "wins": 1,
            "losses": 1,
            "gross_rupees": "9.10",
            "net_rupees": "6.98",
        }

    def test_a_loss_after_costs_counts_as_a_loss_in_the_panel(self, db):
        today = now_ist().date()
        book = W.make_book(db.engine)
        alert = W.stock_alert("T3", created=H.at(10, 5, 3, day=today), target=1000.9)
        H.open_trade(book, alert, now=H.at(10, 5, 3, day=today))
        frame = U.pbars(H.at(10, 5, day=today), [U.PRE_BAR, *W.stock_rows(alert, "target")])
        book.step(H.at(10, 9, day=today), H.Feed(SBIN=frame))
        trade = W.api_trade(book, "T3")
        assert trade["gross"] == "0.60" and trade["net"].startswith("-")
        totals = W.paper_api(book)["totals_today"]
        assert (totals["wins"], totals["losses"]) == (0, 1), "gross +0.60 but net below zero"
