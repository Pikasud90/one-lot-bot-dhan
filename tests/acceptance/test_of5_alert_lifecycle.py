"""Owner feedback OF-5 (2026-11-07): the alert lifecycle, end to end through the REAL Desk.

The owner's words, one test class each:

* "One exit per entry irrespective whether the user takes the trade or not": the paper trade's
  close IS the alert's exit; it is told ONCE (an EXIT notification), never repeated by later
  polls, and recorded on the alert.
* "For all alerts by default a paper trade is open as soon as alert came and it is closed when
  corresponding alert of exit comes": a position the owner took closes at the exit alert's price.
* "The entry and exit prices should be as per the alerts only optionally asking user to enter
  price manually to see the difference": the alert's prices are the record; the owner's own
  prices sit beside them with the difference.
* "No need to notify same alerts again": one live alert per instrument, never the same bar twice.
* "No need to apply risk governor on alerts or paper trades": covered in the US-001/US-003/US-028
  suites (updated), and here for the governor's counters.

The rig is the US-002 one: a real Desk, a real PaperBook, the bars after each alert SCRIPTED.
"""

from __future__ import annotations

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


def exits(rig) -> list:
    return [n for n in rig.notifier.sent if getattr(n.level, "value", None) == "EXIT"]


def pending(rig, key: str = "SBIN", direction: str = "long") -> dict:
    """An alert left PENDING (Rig.alert resolves it at once), so the owner can take it."""
    return H.raise_alert(rig.desk, key, now=H.mid_session(rig.desk, key), direction=direction)


def through_the_stop(rig, alert: dict) -> int:
    """Script SBIN so the paper trade stops out two bars after its fill; the stop in paise."""
    stop, _ = levels(alert)
    o = rig.fill_open("SBIN")
    rig.script("SBIN", {2: (o, o, rupees(stop) - 0.5, rupees(stop) + 0.2)})
    return stop


def stored_position(rig, pid: str) -> dict:
    with rig.engine.connect() as conn:
        row = conn.execute(select(desk_positions).where(desk_positions.c.id == pid)).mappings()
        return dict(row.one())


class TestOneExitPerEntry:
    def test_the_paper_close_is_the_one_exit_told_once_and_kept_on_the_alert(self, rig):
        alert = rig.alert("SBIN")  # nobody takes it
        through_the_stop(rig, alert)

        for _ in range(4):  # the desk polls on: the exit is never told again
            rig.poll()

        row = rig.row(alert)
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        (told,) = exits(rig)
        price = rupees(row["exit_paise"])
        assert told.title.startswith(f"EXIT {alert['label']} — STOP")
        assert f"{price:,.2f}" in told.title
        assert told.payload["exit"]["price"] == price
        assert told.payload["exit"]["reason"] == "STOP"
        assert told.payload["position"]["trading_symbol"] == "SBIN"
        assert told.payload["position"]["unrealized"] == rupees(row["net_paise"])
        archived = next(r for r in rig.desk.archive if r["id"] == alert["id"])
        assert archived["exit"]["price"] == price and archived["exit"]["status"] == "CLOSED"
        with rig.engine.connect() as conn:
            stored = conn.execute(
                select(desk_signals.c.payload).where(desk_signals.c.id == alert["id"])
            ).scalar_one()
        assert stored["exit"]["reason"] == "STOP", "kept for a restart"

    def test_a_no_fill_is_the_alerts_one_exit_too(self, rig):
        alert = rig.alert("SBIN")
        stop, _ = levels(alert)
        gap = rupees(stop) - 3.0  # the fill bar opens beyond the stop: the trade never fills
        frame = rig.original["SBIN"].copy()
        frame.loc[rig.fill_bar("SBIN"), ["open", "high", "low", "close"]] = (gap, gap, gap, gap)
        rig.desk.source.frames["SBIN"] = frame

        rig.poll()
        rig.poll()

        assert rig.row(alert)["status"] == "NO_FILL"
        (told,) = exits(rig)
        assert told.title == f"EXIT {alert['label']} — NO FILL (GAP THROUGH STOP)"
        assert "never filled" in told.body


class TestATakenAlertClosesWithItsExit:
    def test_the_owners_position_closes_at_the_exit_alerts_price(self, rig):
        alert = pending(rig)
        took = rig.desk.acknowledge(alert["id"], "TAKE")
        pid = took["position_id"]
        through_the_stop(rig, alert)

        rig.poll()

        row = rig.row(alert)
        assert pid not in rig.desk.positions
        stored = stored_position(rig, pid)
        assert stored["status"] == "CLOSED" and stored["exit_reason"] == "STOP"
        assert stored["exit_price"] == pytest.approx(rupees(row["exit_paise"]))
        assert stored["payload"]["exit_source"] == "alert"
        assert stored["payload"]["entry_price"] == alert["economics"]["entry_price"]
        (told,) = exits(rig)
        assert "You took it" in told.body
        (journal,) = rig.desk.journal()
        assert journal["alert_exit_price"] == pytest.approx(rupees(row["exit_paise"]))
        assert journal["user_net_pnl"] is None and journal["diff_pnl"] is None

    def test_no_exit_advice_of_the_desks_own_while_the_paper_trade_runs(self, rig):
        """The desk's touch-based advice used to fire on every poll for a taken position; now
        the paper trade alone decides the exit, so a price through the desk's stop says nothing
        until the paper trade's bar closes there."""
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        rig.script("SBIN")  # quiet bars: the paper trade stays open... until the session ends
        tracker = rig.desk.trackers["SBIN"]
        position = rig.desk.positions[pid]
        tracker.live_quote = None
        tracker.bars.loc[tracker.bars.index[-1], "close"] = position["stop_underlying"] - 5
        rig.desk._manage_positions(H.mid_session(rig.desk, "SBIN"), H.OpenClock().current_window())
        assert pid in rig.desk.positions and rig.desk.positions[pid]["exit_advice"] is None
        assert "paper trade" in rig.desk.positions[pid]["hold_check"]
        assert exits(rig) == []

    def test_a_position_left_open_before_of5_closes_at_its_alerts_old_exit(self, rig, desk_db):
        """The 2 November HDFC case: the alert's paper trade closed long ago while the owner's
        position stayed open (and the desk re-advised it every poll). The next poll closes it
        at that exit's price, with no new EXIT for an exit already past."""
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        row = rig.row(alert)
        assert row["status"] == "CLOSED"
        told_before = len(exits(rig))
        # a position taken on that alert, as an older desk left it (opened, never closed)
        restarted, _ = H.new_desk(rig.cfg, desk_db, paper=rig.book, frames=rig.frames)
        restarted.signals[alert["id"]] = {**alert, "status": "PENDING"}
        pid = restarted.acknowledge(alert["id"], "TAKE")["position_id"]
        restarted.poll()
        assert pid not in restarted.positions
        stored = stored_position(rig, pid)
        assert stored["exit_price"] == pytest.approx(rupees(row["exit_paise"]))
        assert stored["exit_reason"] == "STOP"
        assert len(exits(rig)) == told_before


class TestTheOwnersPricesBesideTheAlerts:
    def test_a_reported_entry_and_exit_show_the_difference_and_never_replace_the_record(self, rig):
        alert = pending(rig)
        alert_entry = alert["economics"]["entry_price"]
        pid = rig.desk.acknowledge(alert["id"], "TAKE", fill_price=alert_entry + 1.0)["position_id"]
        assert rig.desk.positions[pid]["entry_price"] == alert_entry
        assert rig.desk.positions[pid]["user_entry_price"] == alert_entry + 1.0
        through_the_stop(rig, alert)
        rig.poll()
        alert_exit = rupees(rig.row(alert)["exit_paise"])

        out = rig.desk.report_prices(pid, exit_price=alert_exit - 2.0)

        assert out["ok"] and out["status"] == "CLOSED"
        assert out["alert_entry_price"] == alert_entry
        assert out["alert_exit_price"] == pytest.approx(alert_exit)
        # the owner bought 1.00 dearer and sold 2.00 cheaper: about 3.00 worse on one share
        assert out["diff_pnl"] == pytest.approx(-3.0, abs=0.1)
        assert out["net_pnl"] == out["alert_net_pnl"], "the alert's P&L stays the record"
        (journal,) = rig.desk.journal()
        assert journal["user_exit_price"] == alert_exit - 2.0
        assert journal["diff_pnl"] == out["diff_pnl"]

    def test_reporting_on_an_open_position_keeps_it_open(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        out = rig.desk.report_prices(pid, entry_price=123.45)
        assert out["ok"] and out["status"] == "OPEN"
        assert rig.desk.positions[pid]["user_entry_price"] == 123.45
        assert stored_position(rig, pid)["payload"]["user_entry_price"] == 123.45

    @pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), "abc"])
    def test_a_price_that_is_not_a_positive_number_is_refused(self, rig, bad):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        assert rig.desk.report_prices(pid, entry_price=bad)["ok"] is False

    def test_an_unknown_position_is_none(self, rig):
        assert rig.desk.report_prices("nope", entry_price=10.0) is None

    def test_an_early_close_gets_the_alerts_exit_beside_it_when_the_exit_comes(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        mine = alert["economics"]["entry_price"] + 4.0
        assert rig.desk.close_position(pid, mine)["closed"]
        stored = stored_position(rig, pid)
        assert stored["payload"]["exit_source"] == "user" and stored["exit_price"] == mine
        through_the_stop(rig, alert)

        rig.poll()

        stored = stored_position(rig, pid)
        alert_exit = rupees(rig.row(alert)["exit_paise"])
        assert stored["payload"]["alert_exit_price"] == pytest.approx(alert_exit)
        assert stored["payload"]["user_exit_price"] == mine
        assert stored["exit_price"] == pytest.approx(alert_exit), "the alert's exit is the record"
        assert stored["payload"]["diff_pnl"] == pytest.approx(mine - alert_exit, abs=0.1)


class TestNoRepeatedAlerts:
    def test_no_new_alert_on_an_instrument_while_its_paper_trade_runs(self, rig):
        alert = rig.alert("SBIN")
        assert rig.row(alert)["status"] == "PENDING_FILL"
        t = rig.desk.trackers["SBIN"]
        later = dict(alert)
        from onelot.desk.tracker import Verdict

        bar = t.bars.index[-2].to_pydatetime()
        close = float(t.bars["close"].iloc[-2])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        before = len(rig.desk.archive)
        rig.desk._raise_signal(t, t.slots[0], v, H.mid_session(rig.desk, "SBIN"))
        assert len(rig.desk.archive) == before, "suppressed while the paper trade is live"
        assert later["id"] == alert["id"]

    def test_a_new_alert_is_allowed_once_the_paper_trade_has_exited(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        assert rig.row(alert)["status"] == "CLOSED"
        t = rig.desk.trackers["SBIN"]
        from onelot.desk.tracker import Verdict

        bar = t.bars.index[-1].to_pydatetime()
        close = float(t.bars["close"].iloc[-1])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        rig.desk._raise_signal(t, t.slots[0], v, dt.datetime.now(tz=H.IST))
        assert len(rig.desk.signals) == 1

    def test_the_same_bar_is_never_alerted_twice(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        t = rig.desk.trackers["SBIN"]
        from onelot.desk.tracker import Verdict

        bar = dt.datetime.fromisoformat(alert["bar_ts"])
        close = float(t.bars["close"].iloc[-1])
        v = Verdict("long", "test", bar, close * 0.99, close, 60.0, [], True)
        rig.desk._raise_signal(t, t.slots[0], v, dt.datetime.now(tz=H.IST))
        assert rig.desk.signals == {}


class TestTheGovernorIsLeftAlone:
    def test_taking_and_closing_a_desk_position_moves_no_governor_counter(self, rig):
        before = rig.desk.governor.snapshot().as_dict()
        alert = pending(rig)
        rig.desk.acknowledge(alert["id"], "TAKE")
        through_the_stop(rig, alert)
        rig.poll()
        assert rig.desk.positions == {}
        assert rig.desk.governor.snapshot().as_dict() == before


def test_a_failing_exit_alert_never_stops_the_poll(rig, monkeypatch):
    alert = rig.alert("SBIN")
    through_the_stop(rig, alert)

    def boom(*args, **kwargs):
        raise RuntimeError("notifier down")

    monkeypatch.setattr(rig.desk, "_send_alert_exit", boom)
    errors = rig.desk.hook_errors
    rig.poll()
    assert rig.row(alert)["status"] == "CLOSED"
    assert rig.desk.hook_errors == errors + 1
    assert rig.desk.snapshot()["updated_at"] is not None


def test_an_old_position_row_without_of5_fields_reads_back_in_the_journal(rig):
    """A position closed before OF-5 has neither alert nor user prices in its payload: its
    exit price was the owner's own."""
    alert = pending(rig)
    pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
    rig.desk.close_position(pid, 101.0)
    with rig.engine.begin() as conn:
        payload = stored_position(rig, pid)["payload"]
        for k in ("alert_entry_price", "user_exit_price", "exit_source", "alert_exit_price"):
            payload.pop(k, None)
        conn.execute(
            update(desk_positions).where(desk_positions.c.id == pid).values(payload=payload)
        )
    (journal,) = rig.desk.journal()
    assert journal["user_exit_price"] == 101.0
    assert journal["alert_entry_price"] == alert["economics"]["entry_price"]


# ══ through the HTTP API ═══════════════════════════════════════════════════════════════
def api(desk):
    import warnings
    from unittest.mock import MagicMock

    from onelot.api.server import create_app

    with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    engine = MagicMock()
    engine.desk = desk
    return TestClient(create_app(engine, "test-token"))


TOKEN = {"x-onelot-token": "test-token"}


class TestTheApi:
    def test_report_your_prices_needs_the_token_and_answers_the_difference(self, rig):
        alert = pending(rig)
        pid = rig.desk.acknowledge(alert["id"], "TAKE")["position_id"]
        client = api(rig.desk)
        url = f"/api/desk/position/{pid}/report"
        assert client.post(url, json={"entry_price": 10.0}).status_code in (401, 403)
        res = client.post(url, json={"entry_price": 10.0}, headers=TOKEN)
        assert res.status_code == 200 and res.json()["user_entry_price"] == 10.0
        missing = client.post(
            "/api/desk/position/nope/report", json={"entry_price": 1.0}, headers=TOKEN
        )
        assert missing.status_code == 404
        bad = client.post(url, json={"entry_price": -5}, headers=TOKEN).json()
        assert bad["ok"] is False

    def test_signals_carry_the_exit_and_the_primary_paper_scoreboard(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        body = api(rig.desk).get("/api/desk/signals").json()
        assert body["primary_strategies"] == list(rig.desk.cfg.primary_strategies())
        row = next(s for s in body["signals"] if s["id"] == alert["id"])
        assert row["exit"]["reason"] == "STOP"
        board = {r["strategy"]: r for r in body["paper_scoreboard"]}
        mine = board[alert["strategy"]]
        assert mine["trades"] == 1 and mine["losses"] + mine["wins"] <= 1
        assert mine["net"] == f"{rupees(rig.row(alert)['net_paise']):.2f}"


def test_a_restart_shows_the_exit_of_an_alert_that_exited_before_it_was_recorded(rig, desk_db):
    """Alerts from before OF-5 have no exit on their row: the desk reads it from the paper
    trade when it starts (memory only), and tells nobody again."""
    alert = rig.alert("SBIN")
    through_the_stop(rig, alert)
    rig.poll()
    with rig.engine.begin() as conn:  # the row as an older desk stored it: no exit
        payload = conn.execute(
            select(desk_signals.c.payload).where(desk_signals.c.id == alert["id"])
        ).scalar_one()
        payload.pop("exit", None)
        conn.execute(
            update(desk_signals).where(desk_signals.c.id == alert["id"]).values(payload=payload)
        )
    restarted, notifier = H.new_desk(rig.cfg, desk_db, paper=rig.book, frames=rig.frames)
    row = next(r for r in restarted.archive if r["id"] == alert["id"])
    assert row["exit"]["reason"] == "STOP"
    assert [n for n in notifier.sent if getattr(n.level, "value", None) == "EXIT"] == []
