"""US-067, the second review (round 2): the real top-up, no fill before its alert, the told
rule, the evaluation record, bounded memory, stray bars, NIFTY, short session counts, notes.

The desk-side tests drive `DeliveryDesk` with the tester's world (SBIN falls 10% on L3, is
bought at L4's open, stops out on L6; ITC falls on L3, closes above its SMA20 on L5)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "acceptance"))
sys.path.insert(0, str(HERE.parent / "desk"))
sys.path.insert(0, str(HERE))

from desk_harness import new_desk  # noqa: E402
from test_us067_tester_desk import FULL, STORE, L, World, at  # noqa: E402

from onelot.clock import TradingCalendar  # noqa: E402
from onelot.desk.config import load_desk_config  # noqa: E402
from onelot.notify.base import Notification, NotificationLevel  # noqa: E402
from onelot.notify.manager import Notifier  # noqa: E402
from onelot.store import init_database  # noqa: E402

# ── 1. the real top-up ──────────────────────────────────────────────────────────────────────


class Backfill:
    """Stands in for `onelot.data.backfill.backfill`: records the call, answers errors."""

    def __init__(self, errors: list[str]) -> None:
        self.calls: list[dict] = []
        self.errors = errors

    def __call__(self, engine, provider, series_keys=None, resolutions=None, **kw):
        self.calls.append({"keys": list(series_keys), "res": list(resolutions), **kw})
        return SimpleNamespace(errors=self.errors)


@pytest.fixture
def desk(cfg, tmp_path, monkeypatch):
    db = init_database(f"sqlite:///{tmp_path / 'top.db'}")
    fake = Backfill(["SBIN 1m: timeout"])
    monkeypatch.setattr("onelot.data.backfill.backfill", fake)
    monkeypatch.setattr("onelot.data.backfill.choose_provider", lambda mode, adapter: "dhan")
    d, _, _ = new_desk(cfg, db, desk_cfg=load_desk_config())  # the shipped 18 stocks
    d._adapter_getter = lambda: object()
    d._data_api_active = lambda: True
    d.delivery._background = False
    return d, fake


def test_the_top_up_goes_through_the_real_desk_method_with_the_19_series_and_a_stop_flag(desk):
    d, fake = desk
    now = at(L[3], "16:00")
    d.delivery.load_history(now)
    d.delivery.maybe_top_up(now)
    (call,) = fake.calls
    assert len(call["keys"]) == 19 and "NIFTY" in call["keys"] and call["res"] == ["1m"]
    assert callable(call["stop_flag"]) and call["stop_flag"]() in (True, False)
    assert "1 error(s): SBIN 1m: timeout" in d.delivery.top_up_note
    assert "TypeError" not in d.delivery.top_up_note


@pytest.mark.parametrize("hhmm", ["09:00", "12:30", "15:35"])
def test_the_real_top_up_never_runs_in_market_hours(desk, hhmm):
    d, fake = desk
    d.delivery.maybe_top_up(at(L[3], hhmm))
    assert fake.calls == []


# ── 2. never a fill on a bar earlier than its alert ─────────────────────────────────────────


def test_an_entry_read_after_the_open_fills_after_its_alert_and_late(tmp_path):
    """The desk ran since L3 15:00 but read L3 only at L4 09:20 (the history arrived late):
    the alert is created at 09:20, so not the 09:15 open."""
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[4], "09:20"))
    assert w.trades()["SBIN"].status == "PENDING_ENTRY"
    w.poll(at(L[4], "09:21"))
    sbin = w.trades()["SBIN"]
    assert sbin.status == "OPEN" and sbin.entry_late and sbin.entry_ts == at(L[4], "09:20")


def test_the_normal_entry_still_fills_at_the_next_opening_bar_on_time(tmp_path):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.poll(at(L[4], "09:16"))
    sbin = w.trades()["SBIN"]
    assert sbin.entry_ts == at(L[4], "09:15") and not sbin.entry_late


def test_an_exit_read_an_hour_after_a_restart_fills_after_its_alert(tmp_path):
    """Restart at L6 10:00, the exit of ITC (SMA20 on L5) read at 11:00: not the 10:00 bar."""
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    w.poll(at(L[4], "15:45"))
    w.start(at(L[6], "10:00"))
    w.poll(at(L[6], "11:00"))
    w.poll(at(L[6], "11:01"))
    itc = w.trades()["ITC"]
    assert itc.exit_signal_date == L[5] and itc.status == "CLOSED"
    assert itc.exit_late and itc.exit_ts == at(L[6], "11:00")


# ── 3. told only when a channel took it, or the repeat window answered for one that did ────


class Refuser:
    name, deferred = "refuser", False

    def accepts(self, n):
        return False

    def send(self, n):
        raise AssertionError("not asked")


def notifier(channel) -> Notifier:
    cfg = SimpleNamespace(notify=SimpleNamespace(os_toast=False, webhook=False))
    n = Notifier(cfg)
    n.channels = [channel]
    return n


def test_an_empty_result_from_nobody_taking_it_is_not_told(tmp_path):
    w = World(tmp_path)
    w.notifier = notifier(Refuser())
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert all(t.entry_told_at is None for t in w.trades().values())


def test_the_repeat_window_counts_as_told_only_after_a_real_acceptance(tmp_path):
    from onelot.notify.dashboard import DashboardChannel

    w = World(tmp_path)
    w.notifier = notifier(DashboardChannel())
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert all(t.entry_told_at is not None for t in w.trades().values())
    desk = w.desk
    t = w.trades()["SBIN"]
    key = f"delivery:{desk.notification(t, 'entry', at(L[3], '15:45')).signal_id}"
    assert w.notifier.suppresses(key)  # a repeat within the window now gives {}
    assert (t.id, "entry") in desk._accepted
    other = notifier(DashboardChannel())
    other._dedup.should_send(key, at(L[3], "15:45"))  # a repeat before any acceptance here
    assert (
        other.suppresses(key)
        and other.send(
            Notification(title="x", body="y", level=NotificationLevel.SIGNAL), dedup_key=key
        )
        == {}
    )


# ── 4. the evaluation is remembered only when it was written ────────────────────────────────


def test_a_session_whose_record_failed_is_read_again(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    monkeypatch.setattr(w.desk.book, "record_evaluation", lambda *a, **k: None)
    w.poll(at(L[3], "15:45"))
    assert w.desk._last_eval.get("SBIN") is None
    monkeypatch.undo()
    w.poll(at(L[3], "15:50"))
    assert w.desk._last_eval["SBIN"] == L[3] and "SBIN" in w.trades()


# ── 5. bounded memory ───────────────────────────────────────────────────────────────────────


def test_the_gap_check_memory_does_not_grow_with_each_bar(tmp_path):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    for minute in range(16, 60):
        w.poll(at(L[4], f"09:{minute}"))
    assert len(w.desk._gap_seen) <= 2  # one per active trade


# ── 6. a stray bar off the calendar is not a session ────────────────────────────────────────


def test_a_stray_bar_on_a_holiday_is_ignored_with_a_note_and_a_full_special_session_counts(
    tmp_path,
):
    """L5 is a listed holiday. SBIN has one stray bar that day: not a session, noted. ITC has a
    whole special session (360 bars) that day: a session. (A weekend date that is not a known
    special session is already dropped by `data/hygiene` as a mock session.)"""
    cal = TradingCalendar({L[5]: "holiday"}, verified=True)
    sbin = FULL["SBIN"]
    one = sbin[(sbin.index.date != L[5]) | (sbin.index.time == dt.time(10, 0))]
    w = World(tmp_path, dict(FULL, SBIN=one), cal=cal)
    w.start(at(L[5], "15:00"))
    w.poll(at(L[6], "15:45"))
    h = w.desk._hist["SBIN"][1]
    assert L[5] not in set(h.days.index) and "not a session" in h.bad[L[5]]
    assert L[5] in set(w.desk._hist["ITC"][1].days.index)


# ── 8. a session count short on the trade ───────────────────────────────────────────────────


def test_a_calendar_session_missing_from_the_bars_makes_the_count_short(tmp_path):
    cal = TradingCalendar({dt.date(2026, 1, 26): "Republic Day"}, verified=True)  # covers 2026
    sbin = FULL["SBIN"]
    w = World(tmp_path, dict(FULL, SBIN=sbin[sbin.index.date != L[5]]), cal=cal)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    for when in (at(L[4], "09:20"), at(L[4], "15:45"), at(L[6], "15:45")):
        w.poll(when)
    assert "session count short by 1" in (w.trades()["SBIN"].warning or "")


# ── 9. a note stays during the session until a reading replaces it ──────────────────────────


def test_a_bad_bar_note_stays_on_the_card_through_the_next_session(tmp_path):
    st = dict(STORE)
    s = st["SBIN"].copy()
    s.iloc[10, s.columns.get_loc("close")] = float("nan")
    st["SBIN"] = s
    w = World(tmp_path, store=st)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    note = w.desk.notes["SBIN"]
    assert "left out" in note
    for hhmm in ("09:30", "11:00", "14:00"):
        w.poll(at(L[4], hhmm))
        assert w.desk.notes.get("SBIN") == note
        assert w.desk.card("SBIN")["note"] == note
