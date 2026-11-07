"""Tester (QA) probes for owner feedback OF-5, through the REAL Desk and a REAL PaperBook.

Written against the owner's words, not against the implementation:

* "One exit per entry irrespective whether the user takes the trade or not"
* "For all alerts by default a paper trade is open as soon as alert came and it is closed when
  corresponding alert of exit comes"
* "The entry and exit prices should be as per the alerts only"
* "No need to notify same alerts again"

The rig is the one of tests/acceptance/test_of5_alert_lifecycle.py (a warmed Desk, a real book,
the bars after each alert scripted).
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
import us001_helpers as H
from sqlalchemy import select, update
from test_us002_b_desk_end_to_end import Rig, levels, rupees

from onelot.config import AppConfig
from onelot.data.schema import desk_positions, desk_signals


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


@pytest.fixture
def rig(app_cfg, desk_db):
    return Rig(app_cfg, desk_db)


def exits(notifier) -> list:
    return [n for n in notifier.sent if getattr(n.level, "value", None) == "EXIT"]


def pending(rig, key: str = "SBIN", direction: str = "long") -> dict:
    """An alert left PENDING so the owner can take it."""
    return H.raise_alert(rig.desk, key, now=H.mid_session(rig.desk, key), direction=direction)


def stored_position(rig, pid: str) -> dict:
    with rig.engine.connect() as conn:
        row = conn.execute(select(desk_positions).where(desk_positions.c.id == pid)).mappings()
        return dict(row.one())


def stored_signal(rig, sid: str) -> dict:
    with rig.engine.connect() as conn:
        row = conn.execute(select(desk_signals).where(desk_signals.c.id == sid)).mappings()
        return dict(row.one())


def restart(rig, desk_db, cfg=None):
    """A new Desk over the same database and the same paper book (the app restarted)."""
    if cfg is None:
        return H.new_desk(rig.cfg, desk_db, paper=rig.book, frames=rig.frames)
    from onelot.clock import now_ist
    from onelot.desk import Desk
    from onelot.risk.governor import RiskGovernor
    from onelot.store.repo import Repo

    repo = Repo(desk_db)
    gov = RiskGovernor(
        rig.cfg.risk, rig.cfg.account.capital_inr, rig.cfg.mode, now_ist().date().isoformat(), repo
    )
    notifier = H.FakeNotifier()
    desk = Desk(
        desk_db.engine,
        rig.cfg,
        notifier,
        gov,
        H.OpenClock(),
        repo=repo,
        desk_cfg=cfg,
        paper=rig.book,
    )
    desk.source = H.OfflineSource(rig.frames)
    desk.fine_minutes = H.BAR_MIN
    return desk, notifier


# ══ a paper trade that fills AND closes in the very poll the owner's position is watched ═══
class TestFillAndCloseInOnePoll:
    def test_a_taken_alert_closes_at_the_exit_alerts_price_with_exactly_one_exit(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.script(
            "SBIN"
        )  # quiet bars: the trade fills on the next bar and ends at the session end

        rig.poll()
        for _ in range(3):
            rig.poll()

        row = rig.row(alert)
        assert row["status"] == "CLOSED" and row["close_reason"] == "SESSION_END"
        assert pid not in rig.desk.positions
        stored = stored_position(rig, pid)
        assert stored["status"] == "CLOSED"
        assert stored["exit_price"] == pytest.approx(rupees(row["exit_paise"]))
        assert stored["payload"]["exit_source"] == "alert"
        (told,) = exits(rig.notifier)
        assert told.payload["alert_id"] == alert["id"]
        assert told.payload["exit"]["price"] == pytest.approx(rupees(row["exit_paise"]))

    def test_the_closed_time_of_the_owners_position_is_never_before_it_was_opened(self, rig):
        """The alert was taken at wall-clock time but its paper trade ended in the (older) data
        time of the bars: the record must not close before it opened."""
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.script("SBIN")
        rig.poll()
        stored = stored_position(rig, pid)
        opened, closed = stored["opened_at"], stored["closed_at"]
        assert closed is not None and opened is not None
        assert closed >= opened


# ══ two alerts closing in one poll ═══════════════════════════════════════════════════════
class TestTwoAlertsClosingInOnePoll:
    def test_each_alert_gets_its_own_single_exit_and_each_position_its_own_close(self, rig):
        stock = pending(rig, "SBIN")
        index = pending(rig, "BANKNIFTY")
        p_stock = rig.desk.acknowledge(stock["id"], "TAKE")["position_id"]
        p_index = rig.desk.acknowledge(index["id"], "TAKE")["position_id"]
        rig.script("SBIN")
        rig.script("BANKNIFTY")

        rig.poll()
        rig.poll()

        told = exits(rig.notifier)
        assert sorted(n.payload["alert_id"] for n in told) == sorted([stock["id"], index["id"]])
        assert len({n.position_id for n in told}) == 2, "one EXIT per alert, never merged"
        assert rig.desk.positions == {}
        for pid, alert in ((p_stock, stock), (p_index, index)):
            row = rig.row(alert)
            stored = stored_position(rig, pid)
            assert row["status"] == "CLOSED"
            assert stored["status"] == "CLOSED"
            assert stored["payload"]["exit_source"] == "alert"
            assert stored["exit_price"] == pytest.approx(rupees(row["exit_paise"]))

    def test_a_failing_exit_for_one_alert_never_costs_the_other_its_exit(self, rig, monkeypatch):
        first = rig.alert("SBIN")
        second = rig.alert("BANKNIFTY")
        rig.script("SBIN")
        rig.script("BANKNIFTY")
        real = rig.desk._send_alert_exit
        calls = []

        def flaky(trade, base, info, taken):
            calls.append(trade.alert_id)
            if trade.alert_id == first["id"]:
                raise RuntimeError("notifier down for the first one")
            return real(trade, base, info, taken)

        monkeypatch.setattr(rig.desk, "_send_alert_exit", flaky)
        rig.poll()
        assert set(calls) == {first["id"], second["id"]}, "the second exit was still attempted"
        (told,) = exits(rig.notifier)
        assert told.payload["alert_id"] == second["id"]


# ══ NO_FILL with a position the owner took ═══════════════════════════════════════════════
class TestNoFillOfATakenAlert:
    def _gap_through_the_stop(self, rig, alert):
        stop, _ = levels(alert)
        gap = rupees(stop) - 3.0
        frame = rig.original["SBIN"].copy()
        frame.loc[rig.fill_bar("SBIN"), ["open", "high", "low", "close"]] = (gap, gap, gap, gap)
        rig.desk.source.frames["SBIN"] = frame

    def test_the_position_closes_once_at_a_price_the_desk_knows_and_one_exit_is_told(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        self._gap_through_the_stop(rig, alert)

        for _ in range(3):
            rig.poll()

        assert rig.row(alert)["status"] == "NO_FILL"
        assert pid not in rig.desk.positions, "a no-fill is over: the position must not linger"
        (told,) = exits(rig.notifier)
        assert "NO FILL" in told.title
        stored = stored_position(rig, pid)
        assert stored["status"] == "CLOSED"
        last = rig.desk.trackers["SBIN"].last_price()
        assert stored["exit_price"] == pytest.approx(round(last, 2)), "the desk's own mark"

    def test_the_owner_is_not_told_the_desk_closes_at_an_exit_price_that_does_not_exist(self, rig):
        """A no-fill has no exit price. The EXIT body must not promise one."""
        alert = pending(rig)
        rig.desk.acknowledge(alert["id"], "TAKE")
        self._gap_through_the_stop(rig, alert)
        rig.poll()
        rig.poll()
        (told,) = exits(rig.notifier)
        assert "exit alert's price" not in told.body, told.body

    def test_a_position_closed_at_the_desks_own_mark_is_not_recorded_as_closed_by_the_alert(
        self, rig
    ):
        """A no-fill has no exit alert price: the position closes at the desk's last price, and
        its record (exit_source, the journal, the archive's trade brief) must say so."""
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        self._gap_through_the_stop(rig, alert)
        rig.poll()
        rig.poll()
        stored = stored_position(rig, pid)
        assert stored["payload"]["exit_source"] == "desk", stored["payload"]["exit_source"]


# ══ an option (INDEX) alert: premium per unit, rupees for the lot ═════════════════════════
class TestAnOptionAlertsExitUnits:
    def test_the_exit_price_is_premium_per_unit_and_the_pnl_is_that_times_the_lot(self, rig):
        alert = pending(rig, "BANKNIFTY")
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        lot = rig.specs["BANKNIFTY_OPT"].lot_size
        rig.script("BANKNIFTY")

        rig.poll()

        row = rig.row(alert)
        assert row["qty"] == lot
        (told,) = exits(rig.notifier)
        exit_per_unit = rupees(row["exit_paise"])
        assert told.payload["exit"]["price"] == pytest.approx(exit_per_unit)
        assert told.payload["exit"]["net"] == pytest.approx(rupees(row["net_paise"]))
        stored = stored_position(rig, pid)
        payload = stored["payload"]
        assert stored["qty"] == lot
        assert stored["exit_price"] == pytest.approx(exit_per_unit), "per unit, not per lot"
        entry = payload["alert_entry_price"]
        assert entry == alert["economics"]["entry_price"]
        assert payload["gross_pnl"] == pytest.approx((exit_per_unit - entry) * lot, abs=0.01)
        assert payload["net_pnl"] == pytest.approx(
            payload["gross_pnl"] - payload["costs"], abs=0.01
        )
        assert payload["alert_net_pnl"] == payload["net_pnl"]


# ══ the owner's own prices ═══════════════════════════════════════════════════════════════
class TestReportingPricesOnLegacyAndEdgeRows:
    def _legacy(self, rig, alert, price):
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.desk.close_position(pid, price)
        payload = stored_position(rig, pid)["payload"]
        for k in (
            "alert_entry_price",
            "user_entry_price",
            "user_exit_price",
            "alert_exit_price",
            "exit_source",
            "alert_net_pnl",
            "user_net_pnl",
            "diff_pnl",
            "alert_created_at",
        ):
            payload.pop(k, None)
        with rig.engine.begin() as conn:
            conn.execute(
                update(desk_positions).where(desk_positions.c.id == pid).values(payload=payload)
            )
        return pid

    def test_a_position_closed_before_of5_accepts_the_owners_prices_without_an_error(self, rig):
        alert = pending(rig)
        entry = alert["economics"]["entry_price"]
        pid = self._legacy(rig, alert, entry + 2.0)

        out = rig.desk.report_prices(pid, entry_price=entry + 1.0)

        assert out["ok"] is True and out["status"] == "CLOSED"
        assert out["user_entry_price"] == entry + 1.0
        assert out["alert_entry_price"] == entry
        stored = stored_position(rig, pid)
        # the row's own money is still consistent: net = gross - costs
        assert stored["net_pnl"] == pytest.approx(stored["gross_pnl"] - stored["costs"], abs=0.01)
        (journal,) = rig.desk.journal()
        assert journal["user_entry_price"] == entry + 1.0
        assert journal["user_exit_price"] == pytest.approx(entry + 2.0)

    def test_reporting_only_an_exit_on_a_legacy_row_keeps_the_original_entry(self, rig):
        alert = pending(rig)
        entry = alert["economics"]["entry_price"]
        pid = self._legacy(rig, alert, entry + 2.0)
        out = rig.desk.report_prices(pid, exit_price=entry + 3.0)
        assert out["ok"] and out["user_exit_price"] == entry + 3.0
        assert out["alert_entry_price"] == entry

    def test_the_journal_never_serves_a_non_finite_number(self, rig):
        """A typo such as 1e308 must not poison the journal for good (JSON cannot carry inf)."""
        import json

        alert = pending(rig, "BANKNIFTY")
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.desk.close_position(pid, 100.0)
        rig.desk.report_prices(pid, entry_price=1e308)
        journal = rig.desk.journal()
        json.dumps(journal, allow_nan=False)  # what FastAPI does when it answers


# ══ restarts ═════════════════════════════════════════════════════════════════════════════
class TestAcrossRestarts:
    def test_the_same_bar_is_not_alerted_again_after_a_restart(self, rig, desk_db):
        from onelot.desk.tracker import Verdict

        alert = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        assert rig.row(alert)["status"] == "CLOSED"

        desk, notifier = restart(rig, desk_db)
        for tracker in desk.trackers.values():  # strategies must not raise stray alerts here
            tracker.evaluate = lambda *args, **kwargs: []
        desk.poll()  # a restarted desk has no bars until its first poll
        t = desk.trackers["SBIN"]
        bar = dt.datetime.fromisoformat(alert["bar_ts"])
        close = float(t.bars["close"].iloc[-1])
        slot = next(s for s in t.slots if s.cfg.name == alert["strategy"])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        desk._raise_signal(t, slot, v, dt.datetime.now(tz=H.IST))
        assert desk.signals == {}
        assert [n for n in notifier.sent if n.level.value == "SIGNAL"] == []

    def test_no_exit_is_ever_told_twice_for_one_alert_across_polls_and_restarts(self, rig, desk_db):
        alert = rig.alert("SBIN")
        rig.script("SBIN")
        for _ in range(3):
            rig.poll()
        assert len(exits(rig.notifier)) == 1
        for _ in range(2):  # the app restarts twice and keeps polling the same bars
            desk, notifier = restart(rig, desk_db)
            for _ in range(3):
                desk.poll()
            assert exits(notifier) == [], "a restart must not tell an exit again"
        assert len(exits(rig.notifier)) == 1
        assert rig.row(alert)["status"] == "CLOSED"

    def test_a_trade_the_owner_took_before_a_restart_still_closes_once_at_the_exit_price(
        self, rig, desk_db
    ):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        desk, notifier = restart(rig, desk_db)
        assert pid in desk.positions
        rig.script("SBIN")
        desk.source.frames = rig.desk.source.frames
        for _ in range(3):
            desk.poll()
        stored = stored_position(rig, pid)
        row = rig.row(alert)
        assert stored["status"] == "CLOSED"
        assert stored["exit_price"] == pytest.approx(rupees(row["exit_paise"]))
        assert len(exits(notifier)) == 1


# ══ the paper book misbehaving never stops or doubles anything ══════════════════════════
class TestWhenThePaperBookFails:
    def test_open_trades_raising_never_blocks_an_alert_and_is_counted(self, rig, monkeypatch):
        from onelot.desk.tracker import Verdict

        def boom():
            raise RuntimeError("book unreadable")

        monkeypatch.setattr(rig.book, "open_trades", boom)
        t = rig.desk.trackers["SBIN"]
        bar = t.bars.index[-2].to_pydatetime()
        close = float(t.bars["close"].iloc[-2])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        before = rig.desk.hook_errors
        rig.desk._raise_signal(t, t.slots[0], v, H.mid_session(rig.desk, "SBIN"))
        assert len(rig.desk.signals) == 1, "the alert went out whatever the book said"
        assert rig.desk.hook_errors > before

    def test_a_book_that_cannot_find_the_trade_for_a_moment_never_makes_a_second_exit(
        self, rig, monkeypatch
    ):
        """One exit per entry: the desk's own advice is the exit ONLY for an alert that has no
        paper trade. A book read that fails for one poll must not turn into a second exit."""
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        position = rig.desk.positions[pid]
        tracker = rig.desk.trackers["SBIN"]
        tracker.live_quote = None
        tracker.bars.loc[tracker.bars.index[-1], "close"] = position["stop_underlying"] - 5
        real = rig.book.for_alert

        def flaky(alert_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(rig.book, "for_alert", flaky)
        rig.desk._manage_positions(H.mid_session(rig.desk, "SBIN"), H.OpenClock().current_window())
        monkeypatch.setattr(rig.book, "for_alert", real)
        # the book recovers and the paper trade ends on its own bars
        rig.desk.source.frames["SBIN"] = rig.original["SBIN"]
        rig.script("SBIN")
        for _ in range(3):
            rig.poll()
        assert rig.row(alert)["status"] == "CLOSED"
        assert len(exits(rig.notifier)) == 1, [n.title for n in exits(rig.notifier)]


# ══ a paper trade closed by the book outside of step() ═══════════════════════════════════
class TestRetiredUntrackedTrades:
    def test_a_waiting_paper_trade_retired_when_its_series_leaves_the_watchlist_is_told(
        self, rig, desk_db
    ):
        """The desk restarts with a watchlist that no longer tracks SBIN: the book closes that
        alert's waiting paper trade NO_FILL (series_untracked). It is the alert's one exit,
        so the owner must be told (or at least the alert must show it)."""
        alert = rig.alert("SBIN")
        assert rig.row(alert)["status"] == "PENDING_FILL"
        cfg = dataclasses.replace(H.small_desk_cfg(), stocks=())
        desk, notifier = restart(rig, desk_db, cfg=cfg)
        desk.poll()
        assert rig.row(alert)["status"] == "NO_FILL"
        row = next(r for r in desk.archive if r["id"] == alert["id"])
        told = [n for n in exits(notifier) if n.payload.get("alert_id") == alert["id"]]
        assert told or row.get("exit"), "the alert's one exit was never surfaced anywhere"


# ══ the owner's early close and the alert's exit arriving together ════════════════════════
class TestTheOwnersReportAndTheAlertsExitCrossing:
    def test_a_report_made_while_the_alerts_exit_arrives_cannot_wipe_that_exit(
        self, rig, monkeypatch
    ):
        """The owner closed early at their own price. Their `report_prices` (API thread) reads
        the closed row, the desk thread then annotates it with the alert's exit, and the report
        writes its stale copy back: the alert's exit price is lost for good."""
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.desk.close_position(pid, alert["economics"]["entry_price"] + 4.0)
        alert_exit = alert["economics"]["entry_price"] + 9.0
        real_store = rig.desk._store_settled
        state = {"crossed": False}

        def crossing(pid_, p, closed):
            if not state["crossed"]:
                state["crossed"] = True
                rig.desk._annotate_closed(alert["id"], alert_exit)  # the poll thread, in between
            return real_store(pid_, p, closed)

        monkeypatch.setattr(rig.desk, "_store_settled", crossing)
        rig.desk.report_prices(pid, entry_price=alert["economics"]["entry_price"] + 1.0)
        payload = stored_position(rig, pid)["payload"]
        assert payload.get("alert_exit_price") == pytest.approx(alert_exit), payload


# ══ an alert whose exit is already out is not actionable any more ═════════════════════════
class TestAPendingAlertThatHasAlreadyExited:
    def test_it_cannot_be_taken_as_a_live_trade_after_its_exit_alert_went_out(self, rig):
        """'One exit per entry': once the exit alert is out the entry is over. The pending card
        must not stay takeable (taking it books a trade closed at a past price)."""
        alert = pending(rig)
        # the card is still inside its 10 minutes when the paper trade ends (the rig's alerts
        # are raised in the past, so give this one a live expiry)
        later = (dt.datetime.now(tz=H.IST) + dt.timedelta(minutes=10)).isoformat()
        rig.desk.signals[alert["id"]]["expires_at"] = later
        rig.script("SBIN")
        rig.poll()
        assert rig.row(alert)["status"] == "CLOSED" and len(exits(rig.notifier)) == 1
        assert alert["id"] not in rig.desk.signals, "its pending card is still takeable"
        took = rig.desk.acknowledge(alert["id"], "TAKE")
        assert took["accepted"] is False, took


# ══ what the owner types ═════════════════════════════════════════════════════════════════
class TestPricesTheOwnerTypesAtTakeTime:
    @pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), "abc", True, None, ""])
    def test_a_fill_that_is_not_a_positive_number_is_ignored_and_the_alert_price_is_the_record(
        self, rig, bad
    ):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE", fill_price=bad)["position_id"]
        pos = rig.desk.positions[pid]
        assert pos["user_entry_price"] is None
        assert pos["entry_price"] == alert["economics"]["entry_price"]
        assert pos["alert_entry_price"] == alert["economics"]["entry_price"]

    def test_an_early_close_with_a_bad_price_changes_nothing(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        for bad in (0, -1, float("nan"), "x", None, 1e7):
            out = rig.desk.close_position(pid, bad)
            # QA cycle 2 (BUG-18): the refusal names the whole accepted range
            assert out == {
                "closed": False,
                "message": "type the price you exited at: above zero and below 10,000,000",
            }
        assert pid in rig.desk.positions
        assert rig.desk.close_position("nope", 10.0) is None


# ══ what the phone reads ═════════════════════════════════════════════════════════════════
class TestThePhoneTextOfAnExit:
    def _text(self, notification):
        from onelot.notify.telegram import format_exit

        return format_exit(notification.payload, dt.datetime.now(tz=H.IST))

    def test_a_stop_or_session_end_names_the_instrument_the_reason_and_the_price(self, rig):
        stock = pending(rig, "SBIN")
        rig.script("SBIN")
        rig.poll()
        (told,) = exits(rig.notifier)
        text = self._text(told)
        assert "SBIN" in text and "SESSION END" in text
        assert f"{rupees(rig.row(stock)['exit_paise']):,.2f}" in text
        assert "None" not in text and "nan" not in text.lower()

    def test_an_alert_that_never_filled_does_not_read_like_the_exit_of_a_live_trade(self, rig):
        """The dashboard says 'NO FILL'. The phone gets 'EXIT: SELL SBIN - Reason: GAP THROUGH
        STOP', which reads like a live trade to close."""
        alert = pending(rig, "SBIN")
        TestNoFillOfATakenAlert()._gap_through_the_stop(rig, alert)
        rig.poll()
        rig.poll()
        (told,) = exits(rig.notifier)
        assert "no fill" in self._text(told).lower().replace("_", " ")


# ══ a restart with the book rebuilt from the database ════════════════════════════════════
class TestARestartKeepsOneLiveIdeaPerInstrument:
    def test_an_active_paper_trade_still_blocks_a_new_alert_after_the_book_is_rebuilt(
        self, rig, desk_db
    ):
        from onelot.desk.tracker import Verdict

        alert = rig.alert("SBIN")  # its paper trade waits for a fill
        assert rig.row(alert)["status"] == "PENDING_FILL"
        book = H.desk_book(rig.cfg, desk_db.engine)  # the app restarted: a new book, same database
        assert [t.alert_id for t in book.open_trades()] == [alert["id"]], "the book reloaded it"
        book.step = lambda *args, **kwargs: []  # its bars have not arrived: the trade keeps waiting
        desk, notifier = H.new_desk(rig.cfg, desk_db, paper=book, frames=rig.frames)
        for tracker in desk.trackers.values():
            tracker.evaluate = lambda *args, **kwargs: []
        desk.poll()
        t = desk.trackers["SBIN"]
        bar = t.bars.index[-3].to_pydatetime()  # a bar that was never alerted
        close = float(t.bars["close"].iloc[-3])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        before = len(desk.archive)
        desk._raise_signal(t, t.slots[0], v, dt.datetime.now(tz=H.IST))
        assert len(desk.archive) == before and desk.signals == {}
        assert [n for n in notifier.sent if n.level.value == "SIGNAL"] == []


# ══ prices reported before the exit comes ════════════════════════════════════════════════
class TestPricesReportedBeforeTheExit:
    def test_they_survive_a_restart_and_the_difference_is_made_when_the_alert_exits(
        self, rig, desk_db
    ):
        alert = pending(rig)
        entry = alert["economics"]["entry_price"]
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        out = rig.desk.report_prices(pid, entry_price=entry + 1.0, exit_price=entry - 2.0)
        assert out["ok"] and out["status"] == "OPEN"
        desk, notifier = restart(rig, desk_db)
        assert desk.positions[pid]["user_entry_price"] == entry + 1.0
        rig.script("SBIN")
        desk.source.frames = rig.desk.source.frames
        desk.poll()
        desk.poll()
        payload = stored_position(rig, pid)["payload"]
        alert_exit = rupees(rig.row(alert)["exit_paise"])
        assert payload["alert_exit_price"] == pytest.approx(alert_exit)
        assert (payload["user_entry_price"], payload["user_exit_price"]) == (
            entry + 1.0,
            entry - 2.0,
        )
        # yours: bought 1.00 above the alert, sold 2.00 below its entry; the alert's own trade
        # entered at `entry` and left at `alert_exit`. One share, costs a rupee or so apart.
        assert payload["diff_pnl"] == pytest.approx(-3.0 - (alert_exit - entry), abs=0.5)
        assert payload["net_pnl"] == payload["alert_net_pnl"], "the alert's P&L stays the record"
        assert len(exits(notifier)) == 1


# ══ through the real Notifier and the Telegram outbox ════════════════════════════════════
class TestOnePhoneMessagePerExit:
    def test_each_alerts_exit_is_one_outbox_row_and_one_message_however_often_the_desk_polls(
        self, rig, tmp_path, monkeypatch
    ):
        """The real Notifier (20 s repeat window), the real Telegram channel and outbox on a
        throw-away database, a fake HTTP transport. The EXIT reminders of US-004 (1 message + 3
        reminders for an advice raised every poll) must never fire for an alert's exit: the desk
        raises it once."""
        from us004_helpers import FakeClock, build_notifier, rig_factory

        from onelot.clock import now_ist
        from onelot.notify.manager import Notifier

        with rig_factory(tmp_path) as make:
            phone = make(clock=FakeClock(now_ist()))
            real, _channel = build_notifier(phone)
            real = Notifier(real.cfg, dedup_seconds=20.0)  # production's repeat window
            for ch in build_notifier(phone)[0].channels[1:]:
                real.add_channel(ch)
            rig.desk.notifier = real
            stock = pending(rig, "SBIN")
            index = pending(rig, "BANKNIFTY")
            rig.desk.acknowledge(stock["id"], "TAKE")
            rig.script("SBIN")
            rig.script("BANKNIFTY")
            for _ in range(4):
                rig.poll()
                phone.clock.advance(400)  # past the reminder interval (5 minutes) every time
            phone.drain()
            rows = [r for r in phone.rows() if r["kind"] == "exit"]
            assert sorted(r["alert_id"] for r in rows) == sorted(
                [f"alert-{stock['id']}#1", f"alert-{index['id']}#1"]
            )
            texts = [t for t in phone.rec.texts() if t.startswith("<b>EXIT")]
            assert len(texts) == 2 and all("SESSION END" in t for t in texts), texts


# ══ no look-ahead: an alert's exit never depends on bars after it ═══════════════════════
class TestTheExitDoesNotLookAhead:
    def _run(self, app_cfg, tmp_path, name, shape):
        db = H.new_db(tmp_path, f"{name}.db", make_global=True)
        try:
            rig = Rig(app_cfg, db)
            alert = pending(rig)
            pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
            stop, _ = levels(alert)
            o = rig.fill_open("SBIN")
            bar = (o, o, rupees(stop) - 0.5, rupees(stop) + 0.2)
            wild = dict.fromkeys(range(3, 39), (o * 1.4, o * 1.5, o * 0.5, o * 0.6))
            frame = rig.script("SBIN", {2: bar, **(wild if shape == "wild" else {})})
            if shape == "truncated":
                rig.desk.source.frames["SBIN"] = frame.iloc[:-36]  # nothing after the stop bar
            rig.poll()
            rig.poll()
            row = rig.row(alert)
            stored = stored_position(rig, pid)
            (told,) = exits(rig.notifier)
            return (
                row["close_reason"],
                row["exit_paise"],
                row["exit_ts"],
                round(stored["exit_price"], 2),
                told.title.split(" @ ")[0].split("—")[-1].strip(),
            )
        finally:
            H.dispose_db(db, was_global=True)

    def test_the_same_exit_whether_the_later_bars_are_quiet_wild_or_missing(
        self, app_cfg, tmp_path
    ):
        quiet = self._run(app_cfg, tmp_path, "quiet", "quiet")
        wild = self._run(app_cfg, tmp_path, "wild", "wild")
        cut = self._run(app_cfg, tmp_path, "cut", "truncated")
        assert quiet[0] == "STOP"
        assert quiet == wild == cut, (quiet, wild, cut)


# ══ a stock SHORT (SELL): the sign of the money and the words of the exit ═══════════════
class TestAShortStockAlert:
    def test_a_taken_short_earns_when_the_exit_price_is_below_the_alerts_entry(self, rig):
        alert = pending(rig, "SBIN", "short")
        assert alert["side"] == "SELL"
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.script("SBIN")
        rig.poll()
        row = rig.row(alert)
        exit_price = rupees(row["exit_paise"])
        entry = alert["economics"]["entry_price"]
        payload = stored_position(rig, pid)["payload"]
        assert payload["exit_price"] == pytest.approx(exit_price)
        assert payload["gross_pnl"] == pytest.approx(entry - exit_price, abs=0.01)
        assert payload["net_pnl"] == pytest.approx(
            payload["gross_pnl"] - payload["costs"], abs=0.01
        )
        assert payload["costs"] > 0

    def test_the_phone_is_told_to_buy_back_not_to_sell_again(self, rig):
        from onelot.notify.telegram import format_exit

        alert = pending(rig, "SBIN", "short")
        rig.script("SBIN")
        rig.poll()
        (told,) = exits(rig.notifier)
        text = format_exit(told.payload, dt.datetime.now(tz=H.IST))
        assert text.startswith("<b>EXIT: BUY SBIN"), text
        assert "cover short" in text
        assert (
            told.payload["position"]["side"] == "SELL" and told.payload["alert_id"] == alert["id"]
        )
