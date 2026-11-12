"""US-067, the third review (round 3): exits never wait for NIFTY, one warning per failed
record, whole-warning comparison on a trade."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "acceptance"))

from test_us067_tester_desk import COSTS, FULL, L, World, at  # noqa: E402

from onelot.delivery.book import DeliveryBook, NewEntry  # noqa: E402
from onelot.delivery.money import delivery_model, depository_paise  # noqa: E402

# ── 1. exits never wait for NIFTY ───────────────────────────────────────────────────────────


def test_an_exit_is_raised_while_the_entry_step_waits_for_nifty(tmp_path, monkeypatch):
    """SBIN closes at its stop on L6; NIFTY's L6 bar is missing at 15:45. The exit alert goes
    out at 15:45; the entry decision of L6 waits (not recorded) and is made an hour after the
    close. The exit test runs once for (SBIN, L6)."""
    nifty = FULL["NIFTY"]
    w = World(tmp_path, dict(FULL, NIFTY=nifty[nifty.index.date != L[6]]))
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    for when in (at(L[4], "09:20"), at(L[4], "15:45"), at(L[5], "15:45")):
        w.poll(when)
    import onelot.delivery.runner as runner

    calls: list[tuple[str, int]] = []
    real = runner.exit_reading

    def counting(c, entry_idx, fill, u, p, *a, **k):
        calls.append((round(float(c[-1]), 2), u))
        return real(c, entry_idx, fill, u, p, *a, **k)

    monkeypatch.setattr(runner, "exit_reading", counting)
    w.poll(at(L[6], "15:45"))
    sbin = w.trades()["SBIN"]
    assert sbin.status == "EXIT_PENDING" and sbin.exit_reason == "STOP"
    assert sbin.exit_signal_date == L[6]
    assert any(n.title.startswith("SELL") and "SBIN" in n.title for n in w.notifier.sent)
    assert w.desk.book.last_evaluated("SBIN") == L[5]  # the entry step waits
    assert w.desk.notes["SBIN"].startswith("waiting for NIFTY")
    w.poll(at(L[6], "16:00"))
    w.poll(at(L[6], "16:31"))  # an hour after the close: the entry step runs, as it is
    assert w.desk.book.last_evaluated("SBIN") == L[6]
    record = w.desk.book.evaluation("SBIN", L[6])
    assert [e["reasons"] for e in record["exits"]] == [["STOP"]]
    sbin_calls = [x for x in calls if x[0] == 160.0]
    assert len(sbin_calls) == 1  # the exit test of (SBIN, L6) ran once


# ── 2. a failed record is warned about once ─────────────────────────────────────────────────


def test_a_record_that_cannot_be_written_is_warned_about_once_per_session(
    tmp_path, monkeypatch, caplog
):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    monkeypatch.setattr(w.desk.book, "record_evaluation", lambda *a, **k: None)
    caplog.set_level(logging.WARNING, logger="onelot.delivery.runner")
    for minute in (45, 46, 47, 48):
        w.poll(at(L[3], f"15:{minute}"))
    said = [r for r in caplog.records if "could not be recorded" in r.getMessage()]
    assert sorted(r.getMessage().split()[1] for r in said) == ["ITC", "SBIN"]


# ── 3. warnings compared whole; a newer session count replaces the older one ───────────────


def book_with_trade(tmp_path):
    book = DeliveryBook(
        create_engine(f"sqlite:///{tmp_path / 'b.db'}"),
        delivery_model(COSTS),
        depository_paise(COSTS),
    )
    t = book.record_evaluation(
        "SBIN", L[3], {}, at(L[3], "15:45"), NewEntry("SBIN", 100.0, 100, {})
    )
    return book, t


def test_a_newer_session_count_replaces_the_older_one(tmp_path):
    book, t = book_with_trade(tmp_path)
    now = at(L[3], "15:45")
    assert book.warn(t.id, "session count short by 1", now)
    assert book.warn(t.id, "possible corporate action on 2026-11-30: check the figures", now)
    assert book.warn(t.id, "session count short by 10", now)  # not hidden by "short by 1"
    assert book.get(t.id).warning == (
        "possible corporate action on 2026-11-30: check the figures; session count short by 10"
    )
    assert book.warn(t.id, "session count short by 2", now)
    assert book.get(t.id).warning.endswith("; session count short by 2")
    assert book.get(t.id).warning.count("short by") == 1
    assert not book.warn(t.id, "session count short by 2", now)


def test_other_warnings_are_compared_whole(tmp_path):
    book, t = book_with_trade(tmp_path)
    now = at(L[3], "15:45")
    assert book.warn(t.id, f"{L[5]} read from incomplete data", now)
    longer = f"{L[5]} read from incomplete data, again"
    assert book.warn(t.id, longer, now)  # contains the first, but is another warning
    assert book.get(t.id).warning == f"{L[5]} read from incomplete data; {longer}"


# ── 4 and 5: what the page's row chip and unit line read from the card ─────────────────────


def test_the_card_carries_the_unit_the_session_limit_and_the_trades_count(tmp_path):
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    w.poll(at(L[5], "12:00"))
    card = w.desk.card("SBIN")
    assert card["unit_inr"] == 10000 and card["max_sessions"] == 30
    assert card["trade"]["status"] == "OPEN" and card["trade"]["sessions"] == 2
    assert w.desk.view()["unit_inr"] == 10000
