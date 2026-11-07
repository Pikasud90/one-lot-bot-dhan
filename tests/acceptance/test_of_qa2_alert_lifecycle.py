"""Tester (QA) cycle 2, Sprint 02a: adversarial probes of the cycle-1 fixes in the Desk.

Each class attacks one fix for what the fix itself could break, through the REAL Desk and a
REAL PaperBook (the rig of tests/acceptance/test_of5_alert_lifecycle.py):

* BUG-2  a pending alert is expired when its exit comes first: not a taken one, not another one;
* BUG-3  UNREADABLE book + `exit_told`: no second exit, and never a first exit that is blocked;
* BUG-4  a retired waiting trade tells its exit, once, also across restarts and reloads;
* BUG-5  `_store_settled` under the desk lock: real threads, no deadlock, no failed write;
* BUG-6  no-fill wording and `exit_source`;
* BUG-11 the price ceiling and a journal that never carries an inf.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
import us001_helpers as H
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from test_of_qa_alert_lifecycle import exits, pending, restart, stored_position, stored_signal
from test_us002_b_desk_end_to_end import Rig, levels, rupees

from onelot.config import AppConfig
from onelot.data.schema import desk_positions


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


def live(rig, alert: dict) -> None:
    """Give a pending card a live expiry (the rig raises its alerts in the past)."""
    later = (dt.datetime.now(tz=H.IST) + dt.timedelta(minutes=10)).isoformat()
    rig.desk.signals[alert["id"]]["expires_at"] = later


def archived(rig, alert: dict) -> dict:
    return next(r for r in rig.desk.archive if r["id"] == alert["id"])


def below_the_stop(rig, pid: str) -> None:
    """The desk's own touch-based advice would fire: the last price is through the stop."""
    position = rig.desk.positions[pid]
    tracker = rig.desk.trackers["SBIN"]
    tracker.live_quote = None
    tracker.bars.loc[tracker.bars.index[-1], "close"] = position["stop_underlying"] - 5


def manage(rig) -> None:
    rig.desk._manage_positions(H.mid_session(rig.desk, "SBIN"), H.OpenClock().current_window())


# ══ BUG-2: a pending alert whose exit came first ═══════════════════════════════════════
class TestAPendingAlertWhoseExitCameFirst:
    def test_it_is_expired_in_memory_and_in_the_database_with_one_exit_and_no_other_notice(
        self, rig
    ):
        alert = pending(rig)
        live(rig, alert)
        before = len(rig.notifier.sent)
        rig.script("SBIN")
        rig.poll()
        rig.poll()
        row = archived(rig, alert)
        assert row["status"] == "EXPIRED" and row["resolved_at"]
        assert alert["id"] not in rig.desk.signals
        assert stored_signal(rig, alert["id"])["status"] == "EXPIRED"
        assert len(exits(rig.notifier)) == 1
        titles = [n.title for n in rig.notifier.sent[before:]]
        assert not [t for t in titles if t.startswith(("Monitoring", "Closed"))], titles

    def test_a_taken_alert_keeps_its_status_when_its_exit_comes(self, rig):
        alert = pending(rig)
        live(rig, alert)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.script("SBIN")
        rig.poll()
        assert archived(rig, alert)["status"] == "CLOSED"
        assert stored_signal(rig, alert["id"])["status"] == "TAKEN"
        assert stored_position(rig, pid)["status"] == "CLOSED"

    def test_only_the_alert_that_exited_is_expired(self, rig):
        stock = pending(rig, "SBIN")
        live(rig, stock)
        index = H.raise_alert(rig.desk, "BANKNIFTY")  # raised on the last bar: nothing to fill on
        rig.script("SBIN")
        rig.poll()
        assert archived(rig, stock)["status"] == "EXPIRED"
        assert index["id"] in rig.desk.signals and archived(rig, index)["status"] == "PENDING"

    def test_a_new_alert_on_the_instrument_is_allowed_right_after_the_exit(self, rig):
        from onelot.desk.tracker import Verdict

        alert = pending(rig)
        live(rig, alert)
        rig.script("SBIN")
        rig.poll()
        t = rig.desk.trackers["SBIN"]
        bar = t.bars.index[-3].to_pydatetime()
        close = float(t.bars["close"].iloc[-3])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        rig.desk._raise_signal(t, t.slots[0], v, dt.datetime.now(tz=H.IST))
        assert len(rig.desk.signals) == 1, "the expired card no longer holds the instrument"

    def test_a_failed_write_while_expiring_the_card_never_costs_the_alert_its_exit(
        self, rig, monkeypatch
    ):
        """The exit is the owner's one message about this alert; expiring its card is housekeeping.
        A locked database at that write must not swallow the EXIT."""
        alert = pending(rig)
        live(rig, alert)
        real = rig.desk._resolve_signal_db
        state = {"n": 0}

        def locked_once(sid, status):
            state["n"] += 1
            if state["n"] == 1:
                raise OperationalError("UPDATE desk_signals", {}, Exception("database is locked"))
            return real(sid, status)

        monkeypatch.setattr(rig.desk, "_resolve_signal_db", locked_once)
        rig.script("SBIN")
        rig.poll()
        rig.poll()
        assert len(exits(rig.notifier)) == 1, "the exit was swallowed by the failed housekeeping"

    def test_a_failed_write_of_the_alerts_exit_record_never_costs_the_owner_the_exit_message(
        self, rig
    ):
        """The same question for the write that records the exit on the alert (code of the
        first delivery): the message is the owner's one signal, the record is bookkeeping."""
        alert = rig.alert("SBIN")  # nobody takes it, no pending card
        rig.script("SBIN")
        with H.failing_sql(rig.engine, "desk_signals") as hits:
            rig.poll()
        assert hits["hits"] >= 1, "the harness never failed the write"
        assert len(exits(rig.notifier)) == 1, "the exit was swallowed by the failed bookkeeping"
        assert alert["id"] not in rig.desk.signals

    def test_taking_the_expired_card_is_refused_with_a_reason_and_opens_nothing(self, rig):
        alert = pending(rig)
        live(rig, alert)
        rig.script("SBIN")
        rig.poll()
        out = rig.desk.acknowledge(alert["id"], "TAKE")
        assert out["accepted"] is False and "not pending" in out["message"]
        assert rig.desk.positions == {}


# ══ BUG-3: UNREADABLE and exit_told ═══════════════════════════════════════════════════
class TestAnUnreadableBookAndExitTold:
    def test_three_polls_with_an_unreadable_book_leave_the_position_open_and_say_so(
        self, rig, monkeypatch
    ):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        below_the_stop(rig, pid)
        real = rig.book.for_alert

        def boom(alert_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(rig.book, "for_alert", boom)
        errors = rig.desk.hook_errors
        for _ in range(3):
            manage(rig)
        assert pid in rig.desk.positions and exits(rig.notifier) == []
        position = rig.desk.positions[pid]
        assert position["exit_advice"] is None and "could not be read" in position["hold_check"]
        assert rig.desk.hook_errors >= errors + 3
        monkeypatch.setattr(rig.book, "for_alert", real)
        rig.desk.source.frames["SBIN"] = rig.original["SBIN"]
        rig.script("SBIN")
        rig.poll()
        rig.poll()
        assert pid not in rig.desk.positions
        assert len(exits(rig.notifier)) == 1

    def test_a_poll_that_cannot_read_the_book_does_not_close_a_position_whose_trade_ended(
        self, rig, monkeypatch
    ):
        """The trade ends while the lookup fails: the exit is told (the events carry it), the
        position waits for the next poll that can read the book and then closes at its price."""
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.script("SBIN")
        real = rig.book.for_alert
        monkeypatch.setattr(rig.book, "for_alert", lambda a: (_ for _ in ()).throw(OSError("io")))
        rig.poll()
        assert pid in rig.desk.positions and len(exits(rig.notifier)) == 1
        monkeypatch.setattr(rig.book, "for_alert", real)
        rig.poll()
        stored = stored_position(rig, pid)
        assert pid not in rig.desk.positions and stored["status"] == "CLOSED"
        assert stored["exit_price"] == pytest.approx(rupees(rig.row(alert)["exit_paise"]))
        assert len(exits(rig.notifier)) == 1

    def test_exit_told_is_stored_and_a_close_delivered_again_after_a_restart_tells_nothing(
        self, rig, desk_db
    ):
        alert = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        assert len(exits(rig.notifier)) == 1
        assert stored_signal(rig, alert["id"])["payload"].get("exit_told")
        desk, notifier = restart(rig, desk_db)
        desk._alert_exit(rig.book.for_alert(alert["id"]).id)
        assert exits(notifier) == []

    def test_a_trade_that_appears_after_the_desks_own_exit_tells_nothing_more(
        self, rig, monkeypatch
    ):
        """on_alert hit a locked database: the alert went out without a paper trade, the owner
        took it, the desk's own advice was its exit; then the book's retry opens the trade and
        it closes. One exit per alert, whichever path spoke first."""
        real_open = rig.book._open
        calls = {"n": 0}

        def flaky(alert_id, alert, now, opened_by):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError(
                    "INSERT INTO paper_trades", {}, Exception("database is locked")
                )
            return real_open(alert_id, alert, now, opened_by)

        monkeypatch.setattr(rig.book, "_open", flaky)
        alert = pending(rig)
        assert H.trade_rows(rig.engine) == [], "the book could not open its trade yet"
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        below_the_stop(rig, pid)
        manage(rig)
        assert pid not in rig.desk.positions and len(exits(rig.notifier)) == 1
        rig.desk.source.frames["SBIN"] = rig.original["SBIN"]
        rig.script("SBIN")
        for _ in range(3):
            rig.poll()
        assert rig.row(alert)["status"] == "CLOSED", "the retry did open and close the trade"
        assert len(exits(rig.notifier)) == 1, [n.title for n in exits(rig.notifier)]

    def test_exit_told_never_blocks_the_first_exit_of_the_next_alert_on_the_same_instrument(
        self, rig
    ):
        from onelot.desk.tracker import Verdict

        first = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        assert len(exits(rig.notifier)) == 1
        t = rig.desk.trackers["SBIN"]
        bar = t.bars.index[-4].to_pydatetime()
        close = float(t.bars["close"].iloc[-4])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        rig.desk._raise_signal(t, t.slots[0], v, bar + dt.timedelta(seconds=3))
        (second,) = list(rig.desk.signals.values())
        rig.poll()
        rig.poll()
        told = [n.payload["alert_id"] for n in exits(rig.notifier)]
        assert told == [first["id"], second["id"]], told


# ══ BUG-4: a retired waiting trade ═══════════════════════════════════════════════════════
def without_stocks():
    return dataclasses.replace(H.small_desk_cfg(), stocks=())


class TestARetiredWaitingTrade:
    def test_a_reload_that_drops_the_series_tells_the_exit_of_its_waiting_trade(
        self, rig, monkeypatch
    ):
        import onelot.desk.desk as D

        alert = rig.alert("SBIN")
        assert rig.row(alert)["status"] == "PENDING_FILL"
        monkeypatch.setattr(D, "load_desk_config", lambda *a, **k: without_stocks())
        rig.desk.reload()
        assert rig.row(alert)["status"] == "NO_FILL"
        told = [n for n in exits(rig.notifier) if n.payload.get("alert_id") == alert["id"]]
        assert len(told) == 1 and "NO FILL" in told[0].title
        assert archived(rig, alert)["exit"]["status"] == "NO_FILL"

    def test_a_second_restart_after_the_retirement_tells_nothing_again(self, rig, desk_db):
        alert = rig.alert("SBIN")
        first, n1 = restart(rig, desk_db, cfg=without_stocks())
        assert len([n for n in exits(n1) if n.payload.get("alert_id") == alert["id"]]) == 1
        second, n2 = restart(rig, desk_db, cfg=without_stocks())
        assert exits(n2) == []
        assert next(r for r in second.archive if r["id"] == alert["id"])["exit"]["status"] == (
            "NO_FILL"
        )

    @pytest.mark.parametrize("answer", [None, (), "nothing", 7])
    def test_a_book_that_answers_oddly_never_stops_a_reload(self, rig, monkeypatch, answer):
        monkeypatch.setattr(rig.book, "retire_untracked", lambda *a, **k: answer)
        assert rig.desk.reload()["instruments"] >= 1

    def test_a_book_that_raises_is_counted_and_never_stops_a_reload(self, rig, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("book down")

        monkeypatch.setattr(rig.book, "retire_untracked", boom)
        errors = rig.desk.hook_errors
        assert rig.desk.reload()["instruments"] >= 1
        assert rig.desk.hook_errors == errors + 1

    def test_a_pending_card_of_a_retired_trade_is_expired_too(self, rig, desk_db):
        """A card still inside its ten minutes whose paper trade was retired at start."""
        alert = pending(rig)
        live(rig, alert)
        # the restarted desk reads the pending card back from the database
        desk, notifier = restart(rig, desk_db, cfg=without_stocks())
        assert [n for n in exits(notifier) if n.payload.get("alert_id") == alert["id"]]
        assert alert["id"] not in desk.signals


# ══ BUG-5: the stored position under the desk lock ════════════════════════════════════
def early_close(rig, alert: dict):
    pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
    entry = alert["economics"]["entry_price"]
    assert rig.desk.close_position(pid, entry + 4.0)["closed"]
    return pid, entry


class TestTheStoredPositionUnderTheLock:
    def test_a_report_held_back_between_its_read_and_its_write_keeps_the_alerts_exit(
        self, rig, monkeypatch
    ):
        """Two real threads: the report has read the row when the poll thread annotates it."""
        alert = pending(rig)
        pid, entry = early_close(rig, alert)
        alert_exit = entry + 9.0
        real_store = rig.desk._store_settled
        read_done, go = threading.Event(), threading.Event()

        def gated(pid_, p, closed):
            read_done.set()
            assert go.wait(10), "the annotation never finished"
            return real_store(pid_, p, closed)

        monkeypatch.setattr(rig.desk, "_store_settled", gated)
        outcome: dict = {}
        api = threading.Thread(
            target=lambda: outcome.update(rig.desk.report_prices(pid, entry_price=entry + 1.0))
        )
        api.start()
        assert read_done.wait(10)
        monkeypatch.setattr(rig.desk, "_store_settled", real_store)
        with rig.desk._lock:  # the poll thread, annotating while the report waits
            rig.desk._annotate_closed(alert["id"], alert_exit)
        go.set()
        api.join(10)
        assert not api.is_alive() and outcome["ok"]
        payload = stored_position(rig, pid)["payload"]
        assert payload["alert_exit_price"] == pytest.approx(alert_exit)
        assert payload["user_entry_price"] == entry + 1.0
        assert outcome["alert_exit_price"] == pytest.approx(alert_exit), "the answer shows it"

    def test_a_commit_by_another_connection_between_the_read_and_the_write_cannot_fail_it(
        self, rig, desk_db, monkeypatch
    ):
        alert = pending(rig)
        pid, entry = early_close(rig, alert)
        path = str(desk_db.engine.url.database)
        real_settle = rig.desk._settle
        state = {"n": 0}

        def with_a_commit_in_between(p):
            if state["n"] == 0:
                state["n"] += 1
                conn = sqlite3.connect(path, timeout=5)
                conn.execute("CREATE TABLE IF NOT EXISTS qa_probe (x)")
                conn.execute("INSERT INTO qa_probe VALUES (1)")
                conn.commit()
                conn.close()
            return real_settle(p)

        monkeypatch.setattr(rig.desk, "_settle", with_a_commit_in_between)
        out = rig.desk.report_prices(pid, entry_price=entry + 1.0)
        assert out["ok"] and state["n"] == 1
        assert stored_position(rig, pid)["payload"]["user_entry_price"] == entry + 1.0

    def test_reports_and_polls_from_two_threads_neither_deadlock_nor_lose_the_alerts_exit(
        self, rig
    ):
        alert = pending(rig)
        pid, entry = early_close(rig, alert)
        rig.script("SBIN")
        failures: list[BaseException] = []

        def reports():
            try:
                for i in range(30):
                    out = rig.desk.report_prices(pid, entry_price=entry + (i % 3) + 0.5)
                    assert out["ok"]
            except BaseException as exc:  # noqa: BLE001 - the test reports whatever it was
                failures.append(exc)

        def polls():
            try:
                for _ in range(4):
                    rig.poll()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=reports), threading.Thread(target=polls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), (
            "a deadlock between the lock and the database"
        )
        assert failures == []
        payload = stored_position(rig, pid)["payload"]
        assert payload["alert_exit_price"] == pytest.approx(rupees(rig.row(alert)["exit_paise"]))
        assert payload["user_entry_price"] is not None


# ══ BUG-6: no-fill wording and exit_source ═══════════════════════════════════════════════
def stub_trade(**over):
    base = {
        "id": 7,
        "alert_id": "abc123",
        "series_key": "SBIN",
        "side": "BUY",
        "kind": "STOCK",
        "qty": 1,
    }
    return SimpleNamespace(**{**base, **over})


def stub_info(**over):
    base = {
        "trade_id": 7,
        "status": "NO_FILL",
        "reason": "NO_FILL",
        "at": "2026-11-05T10:00:00+05:30",
        "price": None,
        "net": 0.0,
        "grade": "live",
    }
    return {**base, **over}


class TestNoFillWording:
    def base(self):
        return {"label": "State Bank of India", "side": "BUY", "created_at": "2026-11-05T09:45:00"}

    def test_a_plain_no_fill_says_no_fill_once_on_the_phone_and_in_the_title(self, rig):
        rig.desk._send_alert_exit(stub_trade(), self.base(), stub_info(), taken=False)
        (told,) = exits(rig.notifier)
        assert told.payload["advice"] == "NO FILL"
        assert told.title == "EXIT State Bank of India — NO FILL"

    def test_a_no_fill_with_a_cause_says_both(self, rig):
        info = stub_info(reason="GAP_THROUGH_STOP")
        rig.desk._send_alert_exit(stub_trade(), self.base(), info, taken=True)
        (told,) = exits(rig.notifier)
        assert told.payload["advice"] == "NO FILL (GAP THROUGH STOP)"
        assert "closes your position at its last price" in told.body
        assert "exit alert's price" not in told.body

    def test_a_close_without_a_priced_exit_does_not_promise_the_exit_alerts_price(self, rig):
        info = stub_info(status="CLOSED", reason="SESSION_END", price=None, net=None)
        rig.desk._send_alert_exit(stub_trade(), self.base(), info, taken=True)
        (told,) = exits(rig.notifier)
        assert "its last price" in told.body and "exit alert's price" not in told.body
        assert told.payload["advice"] == "SESSION END"

    def test_a_priced_close_still_promises_the_exit_alerts_price_and_prints_it(self, rig):
        info = stub_info(status="CLOSED", reason="STOP", price=986.4, net=-1.65)
        rig.desk._send_alert_exit(stub_trade(), self.base(), info, taken=True)
        (told,) = exits(rig.notifier)
        assert told.payload["advice"] == "STOP @ ₹986.40"
        assert "closes your position at the exit alert's price" in told.body

    def test_the_closed_notice_of_a_position_closed_at_the_desks_mark_does_not_credit_the_alert(
        self, rig
    ):
        alert = pending(rig)
        rig.desk.acknowledge(alert["id"], "TAKE")
        stop, _ = levels(alert)
        gap = rupees(stop) - 3.0
        frame = rig.original["SBIN"].copy()
        frame.loc[rig.fill_bar("SBIN"), ["open", "high", "low", "close"]] = (gap, gap, gap, gap)
        rig.desk.source.frames["SBIN"] = frame
        rig.poll()
        rig.poll()
        closed = [n for n in rig.notifier.sent if n.title.startswith("Closed ")]
        assert len(closed) == 1
        assert "the exit alert's price" not in closed[0].body, closed[0].body

    def test_the_no_fill_position_is_recorded_as_a_desk_close_in_the_archive_too(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        stop, _ = levels(alert)
        gap = rupees(stop) - 3.0
        frame = rig.original["SBIN"].copy()
        frame.loc[rig.fill_bar("SBIN"), ["open", "high", "low", "close"]] = (gap, gap, gap, gap)
        rig.desk.source.frames["SBIN"] = frame
        rig.poll()
        rig.poll()
        assert stored_position(rig, pid)["payload"]["exit_source"] == "desk"
        assert archived(rig, alert)["trade"]["source"] == "desk"
        (journal,) = rig.desk.journal()
        assert journal["exit_source"] == "desk"


# ══ BUG-11: the price ceiling, the messages and the journal ═════════════════════════════
class TestThePriceCeiling:
    @pytest.mark.parametrize("price", [0.05, 1, 986.7, 99_999.99, 9_999_999.99, "250.5"])
    def test_every_real_price_is_accepted(self, rig, price):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE", fill_price=price)["position_id"]
        assert rig.desk.positions[pid]["user_entry_price"] == float(price)

    @pytest.mark.parametrize(
        "price", [1e7, 10_000_000.01, 1e9, 1e30, 1e308, float("inf"), float("nan"), 0, -3.0]
    )
    def test_an_impossible_price_is_refused_everywhere_it_can_be_typed(self, rig, price):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE", fill_price=price)["position_id"]
        assert rig.desk.positions[pid]["user_entry_price"] is None
        assert rig.desk.report_prices(pid, entry_price=price)["ok"] is False
        assert rig.desk.report_prices(pid, exit_price=price)["ok"] is False
        assert rig.desk.close_position(pid, price)["closed"] is False
        assert pid in rig.desk.positions

    def test_the_refusal_names_the_ceiling_not_only_zero(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        message = rig.desk.report_prices(pid, entry_price=1e9)["message"]
        assert "below" in message or "at most" in message or "under" in message, message

    def test_the_largest_accepted_price_makes_a_journal_that_json_can_carry(self, rig):
        alert = pending(rig, "BANKNIFTY")
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.desk.close_position(pid, 9_999_999.99)
        rig.desk.report_prices(pid, entry_price=9_999_999.99)
        json.dumps(rig.desk.journal(), allow_nan=False)

    def test_a_non_finite_figure_already_in_a_stored_row_never_reaches_the_journal(self, rig):
        """Rows closed before the ceiling may hold an inf (the 1e308 of cycle 1)."""
        alert = pending(rig, "BANKNIFTY")
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.desk.close_position(pid, 100.0)
        with rig.engine.begin() as conn:
            conn.execute(
                update(desk_positions)
                .where(desk_positions.c.id == pid)
                .values(exit_price=float("inf"), net_pnl=float("-inf"))
            )
        json.dumps(rig.desk.journal(), allow_nan=False)


# ══ design note 2: stock_horizon intraday ══════════════════════════════════════════════
class TestStockHorizonIntraday:
    def test_the_shipped_config_is_intraday_and_a_stock_alert_says_square_off_not_swing(self, rig):
        from onelot.desk.config import load_desk_config

        assert load_desk_config().stock_horizon == "intraday"
        rig.desk.cfg = dataclasses.replace(rig.desk.cfg, stock_horizon="intraday")
        alert = pending(rig)
        assert alert["kind"] == "STOCK" and alert["horizon"] == "intraday"
        assert "swing" not in alert["text"].lower()
        assert "Square off by 15:15" in alert["text"]

    def test_a_long_stock_alert_reaches_the_phone_as_intraday(self, rig):
        from onelot.notify.telegram import format_alert

        rig.desk.cfg = dataclasses.replace(rig.desk.cfg, stock_horizon="intraday")
        alert = pending(rig)
        text = format_alert(alert, dt.datetime.now(tz=H.IST))
        assert "swing" not in text.lower() and "NRML" not in text, text

    def test_a_position_left_from_an_earlier_session_is_noted_once_not_advised_every_poll(
        self, rig
    ):
        """With intraday stocks the session-over rule applies to a stock position too: the
        paper trade is gone (no link) and the desk neither advises nor repeats itself."""
        rig.desk.cfg = dataclasses.replace(rig.desk.cfg, stock_horizon="intraday")
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.desk.positions[pid]["opened_at"] = (
            dt.datetime.now(tz=H.IST) - dt.timedelta(days=1)
        ).isoformat()
        rig.desk.paper = None  # an alert from before the paper book: the desk's own rules
        before = len(rig.notifier.sent)
        today = dt.datetime.now(tz=H.IST)
        for _ in range(3):
            rig.desk._manage_positions(today, H.OpenClock().current_window(today))
        position = rig.desk.positions[pid]
        assert position["session_over"] is True and position["exit_advice"] is None
        assert len(exits(rig.notifier)) == 0
        assert len(rig.notifier.sent) - before == 1, "one INFO about the session being over"
