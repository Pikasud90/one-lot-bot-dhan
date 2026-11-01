"""US-001 AC8: how the Desk drives the paper book.

    Desk(..., paper: PaperBook | None = None)

* on_alert(copy.deepcopy(sig), now) is called exactly once per alert, AFTER the desk_signals
  insert and BEFORE notifier.send (the paper trade is persisted before anyone is told);
* step(now, bars) is called on every poll, after _manage_positions, inside a try/except that
  counts hook_errors;
* a PaperBook exception never prevents the alert, its notification, or the rest of the poll.

Driven through the real Desk over an offline bar source, with a recording spy (call order,
what it receives) and with the real PaperBook (what the book does with it). The harness is
the one the US-028 acceptance tests use: no network, no broker, no wall-clock dependence.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import us001_helpers as H
from sqlalchemy import text

from onelot.clock import IST
from onelot.config import AppConfig


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


def spied(app_cfg, desk_db, **kw):
    log: list[str] = []
    notifier = H.FakeNotifier()
    spy = H.SpyPaper(desk_db.engine, notifier, log)
    d, _ = H.new_desk(app_cfg, desk_db, paper=spy, notifier=notifier, **kw)
    d.poll()
    assert not d.signals, "harness: a stray signal fired during the first poll"
    log.clear()
    spy.step_calls.clear()
    return d, notifier, spy, log


def hook_errors(desk) -> int | None:
    """hook_errors is specified as a counter but not WHERE it lives: on the desk
    (`hook_errors` or `desk_hook_errors`), in the book's status(), or in the desk snapshot's
    paper block (which shows status() and the desk's own count separately). The largest value
    reported anywhere is the count."""
    seen: list[int] = []
    for name in ("hook_errors", "desk_hook_errors"):
        n = getattr(desk, name, None)
        if isinstance(n, int) and not isinstance(n, bool):
            seen.append(n)
    try:
        seen.append(int(desk.paper.status()["hook_errors"]))
    except Exception:
        pass
    try:
        block = desk.snapshot()["paper"]
    except Exception:
        block = None
    if isinstance(block, dict):
        for key in ("hook_errors", "desk_hook_errors"):
            try:
                seen.append(int(block[key]))
            except Exception:
                pass
    return max(seen) if seen else None


def boom_book(app_cfg, desk_db, *, on_alert_fails=0, step_fails=0):
    """A real PaperBook whose first N on_alert / step calls raise."""
    base = H.paper_book_class()

    class Boom(base):  # type: ignore[valid-type, misc]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.alert_failures = on_alert_fails
            self.step_failures = step_fails

        def on_alert(self, alert, now):
            if self.alert_failures:
                self.alert_failures -= 1
                raise RuntimeError("paper on_alert exploded")
            return super().on_alert(alert, now)

        def step(self, now, bars, strategy_exit=None):
            if self.step_failures:
                self.step_failures -= 1
                raise RuntimeError("paper step exploded")
            return super().step(now, bars, strategy_exit)

    return H.desk_book(app_cfg, desk_db.engine, cls=Boom)


class TestTheDeskKeepsItsBook:
    def test_the_book_is_attached_through_the_paper_keyword(self, app_cfg, desk_db):
        spy = H.SpyPaper()
        d, _ = H.new_desk(app_cfg, desk_db, paper=spy)
        assert d.paper is spy

    def test_a_desk_built_without_a_book_behaves_as_before(self, app_cfg, desk_db):
        d, notifier = H.new_desk(app_cfg, desk_db)
        d.poll()
        assert d.paper is None
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        d.poll()  # no book: nothing to call, nothing to break
        assert notifier.sent[-1].signal_id == alert["id"]


class TestOnAlertWiring:
    def test_on_alert_runs_after_the_insert_and_before_the_notification(self, app_cfg, desk_db):
        d, notifier, spy, log = spied(app_cfg, desk_db)
        sent_before = len(notifier.sent)
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        (call,) = spy.alert_calls
        assert call["alert"]["id"] == alert["id"]
        assert call["desk_row_status"] == "PENDING", "on_alert ran before the desk_signals insert"
        assert call["notifications_sent"] == sent_before, "on_alert ran after notifier.send"
        assert len(notifier.sent) == sent_before + 1 and notifier.sent[-1].signal_id == alert["id"]

    def test_the_book_receives_a_deep_copy_and_the_alert_time(self, app_cfg, desk_db):
        d, _, spy, _ = spied(app_cfg, desk_db)
        now = H.mid_session(d, "SBIN")
        alert = H.raise_alert(d, "SBIN", now=now)
        (call,) = spy.alert_calls
        received = call["alert"]
        assert call["now"] == now and call["now"].tzinfo is not None
        assert received is not alert and received == alert
        assert received["economics"] is not alert["economics"]
        assert received["confidence"] is not alert["confidence"]
        assert received["data"] is not alert["data"]
        # the desk resolves its own alert later; the book's copy is a snapshot
        assert d.acknowledge(alert["id"], "SKIP")["accepted"]
        assert alert["status"] == "SKIPPED" and received["status"] == "PENDING"
        received["economics"]["qty"] = 999  # and the book may scribble on it
        assert alert["economics"]["qty"] == 1

    def test_one_call_per_alert_however_the_desk_proceeds_afterwards(self, app_cfg, desk_db):
        d, _, spy, _ = spied(app_cfg, desk_db)
        now = H.mid_session(d, "SBIN")
        first = H.raise_alert(d, "SBIN", now=now)
        assert len(spy.alert_calls) == 1
        # a second fire on an instrument that already has a pending alert is suppressed
        t = d.trackers["SBIN"]
        from onelot.desk.tracker import Verdict

        v = Verdict("long", "x", t.bars.index[-1].to_pydatetime(), 1.0, 2.0, 60.0, [], True)
        d._raise_signal(t, t.slots[0], v, now)
        assert len(d.signals) == 1 and len(spy.alert_calls) == 1
        d.poll()
        d.poll()
        assert len(spy.alert_calls) == 1
        second = H.raise_alert(d, "BANKNIFTY", now=H.mid_session(d, "BANKNIFTY"))
        assert [c["alert"]["id"] for c in spy.alert_calls] == [first["id"], second["id"]]
        assert d.acknowledge(second["id"], "TAKE")["accepted"]
        assert len(spy.alert_calls) == 2  # taking it by hand is not a new alert

    def test_an_alert_that_never_gets_raised_never_reaches_the_book(
        self, app_cfg, desk_db, monkeypatch
    ):
        d, _, spy, _ = spied(app_cfg, desk_db)

        def fail(*_a, **_k):
            raise ValueError("economics unavailable")

        monkeypatch.setattr(d, "_economics", fail)
        t = d.trackers["SBIN"]
        from onelot.desk.tracker import Verdict

        v = Verdict("long", "x", t.bars.index[-1].to_pydatetime(), 1.0, 2.0, 60.0, [], True)
        d._raise_signal(t, t.slots[0], v, H.mid_session(d, "SBIN"))
        assert d.signals == {} and spy.alert_calls == []

    def test_with_the_real_book_the_trade_is_stored_before_the_notification_goes_out(
        self, app_cfg, desk_db
    ):
        seen: list[int] = []

        class ProbingNotifier(H.FakeNotifier):
            def send(self, n, dedup_key=None):
                with desk_db.engine.connect() as conn:
                    seen.append(
                        conn.execute(
                            text("SELECT COUNT(*) FROM paper_trades WHERE alert_id = :a"),
                            {"a": n.signal_id},
                        ).scalar_one()
                    )
                return super().send(n, dedup_key)

        book = H.desk_book(app_cfg, desk_db.engine)
        d, notifier = H.new_desk(app_cfg, desk_db, paper=book, notifier=ProbingNotifier())
        d.poll()
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        assert seen == [1], "the SIGNAL notification went out before its paper trade existed"
        assert notifier.sent[-1].signal_id == alert["id"]
        assert H.val(H.trade_row(desk_db.engine, alert["id"])["status"]) == "PENDING_FILL"


class TestStepWiring:
    def test_step_runs_every_poll_after_manage_positions(self, app_cfg, desk_db, monkeypatch):
        d, _, spy, log = spied(app_cfg, desk_db)
        monkeypatch.setattr(d, "_manage_positions", lambda now, window: log.append("manage"))
        for _ in range(3):
            d.poll()
        assert log == ["manage", "step"] * 3

    def test_step_runs_even_when_the_market_is_shut_and_nothing_is_held(
        self, app_cfg, desk_db, monkeypatch
    ):
        """_manage_positions is skipped then, but a waiting trade must still be able to close
        NO_FILL at the session's end."""
        d, _, spy, log = spied(app_cfg, desk_db, session_clock=H.ClosedClock())
        monkeypatch.setattr(d, "_manage_positions", lambda now, window: log.append("manage"))
        d.poll()
        d.poll()
        assert log == ["step", "step"]

    def test_step_is_handed_an_aware_now_and_the_trackers_bars(self, app_cfg, desk_db):
        d, _, spy, _ = spied(app_cfg, desk_db)
        before = dt.datetime.now(tz=IST)
        d.poll()
        (call,) = spy.step_calls
        assert call.now.tzinfo is not None
        assert abs((call.now - before).total_seconds()) < 30
        frame = call.bars("SBIN")
        assert isinstance(frame, pd.DataFrame)
        assert frame.index.equals(d.trackers["SBIN"].bars.index)
        assert frame["open"].equals(d.trackers["SBIN"].bars["open"])
        assert call.bars("BANKNIFTY").index.equals(d.trackers["BANKNIFTY"].bars.index)

    def test_the_bars_callable_tolerates_an_unknown_series(self, app_cfg, desk_db):
        d, _, spy, _ = spied(app_cfg, desk_db)
        d.poll()
        unknown = spy.step_calls[-1].bars("NO_SUCH_SERIES")
        assert unknown is None or (isinstance(unknown, pd.DataFrame) and unknown.empty)

    def test_the_real_book_copes_with_the_empty_frames_a_source_switch_leaves(
        self, app_cfg, desk_db, monkeypatch
    ):
        book = H.desk_book(app_cfg, desk_db.engine)
        d, _ = H.new_desk(app_cfg, desk_db, paper=book)
        d.poll()
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        d._reset_bars()
        monkeypatch.setattr(d.source, "fetch", lambda specs, days, since=None: {})
        d.poll()
        assert hook_errors(d) in (0, None)
        assert H.val(H.trade_row(desk_db.engine, alert["id"])["status"]) == "PENDING_FILL"


class TestARaisingBookNeverBlocksTheDesk:
    def test_an_on_alert_failure_keeps_the_alert_and_its_notification(self, app_cfg, desk_db):
        book = boom_book(app_cfg, desk_db, on_alert_fails=1)
        d, notifier = H.new_desk(app_cfg, desk_db, paper=book)
        d.poll()
        version = d.version
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))  # must not raise
        assert alert["id"] in d.signals and d.archive[0]["id"] == alert["id"]
        assert H.count_rows(desk_db.engine, "desk_signals") == 1
        assert notifier.sent[-1].signal_id == alert["id"]
        assert notifier.sent[-1].level.value == "SIGNAL"
        assert d.version > version
        assert hook_errors(d) == 1, "the failed hook was not counted anywhere"

    def test_a_failed_on_alert_does_not_poison_the_next_alert(self, app_cfg, desk_db):
        book = boom_book(app_cfg, desk_db, on_alert_fails=1)
        d, _ = H.new_desk(app_cfg, desk_db, paper=book)
        d.poll()
        H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        ok = H.raise_alert(d, "BANKNIFTY", now=H.mid_session(d, "BANKNIFTY"))
        assert [r["alert_id"] for r in H.trade_rows(desk_db.engine)] == [ok["id"]]
        assert hook_errors(d) == 1

    def test_a_step_failure_is_counted_and_the_poll_carries_on(self, app_cfg, desk_db, monkeypatch):
        book = boom_book(app_cfg, desk_db, step_fails=3)
        d, _ = H.new_desk(app_cfg, desk_db, paper=book)
        ran: list[str] = []
        real_manage = d._manage_positions
        monkeypatch.setattr(
            d, "_manage_positions", lambda now, w: (ran.append("manage"), real_manage(now, w))
        )
        for i in range(3):
            version = d.version
            d.poll()  # must not raise
            assert d.polls == i + 1
            assert d.version > version, "the snapshot was not rebuilt after the failed step"
            assert d.snapshot()["updated_at"] is not None
        assert ran == ["manage"] * 3
        assert hook_errors(d) == 3

    def test_the_book_is_stepped_again_on_the_poll_after_a_failure(self, app_cfg, desk_db):
        book = boom_book(app_cfg, desk_db, step_fails=1)
        d, _ = H.new_desk(app_cfg, desk_db, paper=book)
        d.poll()  # step fails (counted)
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        d.poll()  # step works again
        assert hook_errors(d) == 1
        assert H.val(H.trade_row(desk_db.engine, alert["id"])["status"]) in ("OPEN", "NO_FILL")

    def test_a_raising_spy_is_survived_too(self, app_cfg, desk_db):
        class Exploding(H.SpyPaper):
            def on_alert(self, alert, now):
                raise KeyError("economics")

            def step(self, now, bars, strategy_exit=None):
                raise ValueError("bars")

        d, notifier = H.new_desk(app_cfg, desk_db, paper=Exploding())
        d.poll()
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        d.poll()
        assert notifier.sent[-1].signal_id == alert["id"]
        assert d.polls == 2


class TestEndToEndThroughTheDesk:
    def test_an_alert_fills_on_a_later_poll_at_the_next_bars_open(self, app_cfg, desk_db):
        book = H.desk_book(app_cfg, desk_db.engine)
        d, _ = H.new_desk(app_cfg, desk_db, paper=book)
        d.poll()
        now = H.mid_session(d, "SBIN")
        alert = H.raise_alert(d, "SBIN", now=now)
        d.poll()
        bars = d.trackers["SBIN"].bars
        fill_ts = bars.index[bars.index > now][0]
        row = H.trade_row(desk_db.engine, alert["id"])
        assert H.val(row["status"]) == "OPEN"
        assert H.same_instant(row["fill_bar_ts"], fill_ts.to_pydatetime())
        want = H.expected_stock_entry(
            float(bars["open"].loc[fill_ts]), "long", app_cfg.backtest.slippage_pct
        )
        assert row["entry_paise"] == want
        assert row["resolution_min"] == 2 and row["qty"] == 1
        assert book.status()["opened"] == 1 and book.status()["filled"] == 1
        assert hook_errors(d) in (0, None)

    @pytest.mark.parametrize("source, grade", [("dhan", "live"), ("yahoo", "delayed")])
    def test_the_grade_is_the_serving_source_of_that_series(self, app_cfg, desk_db, source, grade):
        book = H.desk_book(app_cfg, desk_db.engine)
        d, _ = H.new_desk(
            app_cfg, desk_db, paper=book, served={"BANKNIFTY": source, "SBIN": source}
        )
        d.poll()
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        row = H.trade_row(desk_db.engine, alert["id"])
        assert alert["data"]["source"] == source == row["data_source"]
        assert H.val(row["grade"]) == grade

    def test_index_and_stock_alerts_from_one_desk_open_two_independent_trades(
        self, app_cfg, desk_db
    ):
        book = H.desk_book(app_cfg, desk_db.engine)
        d, _ = H.new_desk(app_cfg, desk_db, paper=book)
        d.poll()
        stock = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        index = H.raise_alert(d, "BANKNIFTY", now=H.mid_session(d, "BANKNIFTY"), direction="short")
        d.poll()
        rows = {r["alert_id"]: r for r in H.trade_rows(desk_db.engine)}
        assert set(rows) == {stock["id"], index["id"]}
        assert rows[stock["id"]]["kind"] == "STOCK" and rows[index["id"]]["kind"] == "INDEX"
        assert rows[index["id"]]["side"] == "BUY_PE" and rows[index["id"]]["direction"] == "short"
        assert book.status()["opened"] == 2
