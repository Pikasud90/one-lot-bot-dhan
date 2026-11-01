"""Isolation, one unit, restart (US-001 AC4, AC5, AC6) at the PaperBook level.

AC4  every alert opens a trade whatever its risk verdict, confidence or status, and the
     RiskGovernor is never touched (the book cannot even see one).
AC5  exactly one share or one lot of the ALERTED option, never lots_per_trade lots.
AC6  restart: non-closed trades reload; desk_signals rows without a paper row are
     reconciled when the book is constructed, at the ALERT's created_at (Manager decision 1,
     2026-11-01); a disabled book reconciles nothing; one bad row never stops startup.

The Desk-driven halves (governor snapshot through Desk.poll, lots_per_trade through the
desk) need the AC8 wiring and live in the acceptance suite.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine

from onelot.config import PaperCfg, RiskCfg
from onelot.paper.book import PaperBook
from onelot.risk.governor import RiskGovernor
from onelot.store.schema import desk_signals, paper_events, paper_trades

from .conftest import BT, SPECS, THU, FakePricer, ist, make_alert, make_bars, table_rows


def series(**frames):
    return lambda key: frames.get(key)


def store_desk_signal(engine: Engine, alert: dict[str, Any], payload: Any = ...) -> None:
    """A desk_signals row as Desk._raise_signal writes it (payload = the alert dict)."""
    with engine.begin() as conn:
        conn.execute(
            desk_signals.insert().values(
                id=alert["id"],
                created_at=dt.datetime.fromisoformat(alert["created_at"]),
                expires_at=dt.datetime.fromisoformat(alert["expires_at"]),
                instrument_key=alert["instrument_key"],
                series_key=alert["series_key"],
                kind=alert["kind"],
                side=alert["side"],
                strategy=alert["strategy"],
                timeframe=alert["timeframe"],
                bar_ts=dt.datetime.fromisoformat(alert["bar_ts"]),
                status=alert["status"],
                payload=alert if payload is ... else payload,
            )
        )


def rows_by_alert(engine: Engine) -> dict[str, dict[str, Any]]:
    return {r["alert_id"]: r for r in table_rows(engine, paper_trades)}


def event_kinds(engine: Engine, trade_id: int) -> list[str]:
    return [e["kind"] for e in table_rows(engine, paper_events) if e["trade_id"] == trade_id]


# ── AC4: evidence, not permission ────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["active", "manual-halt", "daily-loss-limit"])
def test_the_governor_snapshot_is_identical_after_every_kind_of_paper_activity(make_book, mode):
    gov = RiskGovernor(RiskCfg(), 50_000.0, "paper", THU.isoformat())
    if mode == "manual-halt":
        gov.manual_panic_halt(ist(THU, 9, 30))
    elif mode == "daily-loss-limit":
        gov.realized_pnl = -(RiskCfg().daily_loss_limit_inr + 1.0)
        assert gov.may_open(ist(THU, 9, 30))[0] is False
    before = copy.deepcopy(gov.snapshot().as_dict())

    book = make_book(premium_at=lambda option, underlying, ts: None)  # options never price
    book.on_alert(make_alert(alert_id="fill", risk_allowed=False), ist(THU, 10, 5, 4))
    book.on_alert(make_alert(alert_id="gap", stop=1001.0), ist(THU, 10, 5, 4))
    book.on_alert(make_alert("INDEX", alert_id="unpriced", status="EXPIRED"), ist(THU, 10, 40))
    book.step(
        ist(THU, 10, 20),
        series(
            RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 3),
            BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0] * 14),
        ),
    )

    assert {k: r["status"] for k, r in rows_by_alert(book.engine).items()} == {
        "fill": "OPEN",
        "gap": "NO_FILL",
        "unpriced": "NO_FILL",
    }
    assert gov.snapshot().as_dict() == before


@pytest.mark.parametrize(
    "kw",
    [
        {"risk_allowed": False},
        {"status": "EXPIRED"},
        {"status": "SKIPPED", "risk_allowed": False},
    ],
    ids=["risk-blocked", "expired", "skipped-and-blocked"],
)
def test_startup_reconciles_blocked_and_expired_alerts_too(make_book, engine, kw):
    """RED for T3: reconciliation is evidence too; the desk_signals status does not filter it."""
    alert = make_alert(alert_id="r1", **kw)
    alert["confidence"] = {"score": 3.0, "label": "low", "parts": {}}
    store_desk_signal(engine, alert)

    make_book()

    row = rows_by_alert(engine)["r1"]
    assert (row["status"], row["confidence"]) == ("PENDING_FILL", 3.0)


# ── AC5: one unit, always ────────────────────────────────────────────────────────


def test_an_index_risk_is_one_lot_even_when_the_alert_claims_five(make_book, engine):
    alert = make_alert("INDEX", "long")
    alert["economics"]["qty"] = 5 * 30
    book = make_book(premium_at=FakePricer(fair=312.34))
    book.on_alert(alert, ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), series(BANKNIFTY=make_bars(ist(THU, 10, 5), [45200.0, 45224.65])))
    row = rows_by_alert(engine)[alert["id"]]
    assert (row["qty"], row["risk_paise"]) == (30, (31435 - 31234) * 30)


def test_a_lot_that_disagrees_with_the_spec_is_kept_and_warned(make_book, caplog):
    """RED for T3 (Manager decision 12): the alerted option's lot is the unit; a mismatch
    with spec.lot_size (an exchange lot revision the specs missed) is logged, never silent."""
    alert = make_alert("INDEX", "long", alert_id="lot25")
    alert["economics"]["option"]["lot"] = 25  # SPECS["BANKNIFTY_OPT"].lot_size is 30
    with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
        trade = make_book().on_alert(alert, ist(THU, 10, 5, 4))
    assert trade is not None and trade.qty == 25
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("lot25" in m and "25" in m and "30" in m for m in warned), warned


def test_a_matching_lot_is_not_warned(make_book, caplog):
    with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
        make_book().on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_reconciled_trades_are_one_unit_whatever_economics_qty_says(make_book, engine):
    """RED for T3: the reconciliation path sizes exactly like on_alert."""
    stock, index = make_alert(alert_id="s1"), make_alert("INDEX", alert_id="i1")
    stock["economics"]["qty"], index["economics"]["qty"] = 500, 150
    store_desk_signal(engine, stock)
    store_desk_signal(engine, index)

    make_book()

    rows = rows_by_alert(engine)
    assert (rows["s1"]["qty"], rows["i1"]["qty"]) == (1, 30)


# ── AC6: restart and startup reconciliation ──────────────────────────────────────


def test_constructing_a_book_reconciles_desk_signals_without_a_paper_row(make_book, engine):
    """RED for T3 (Manager decision 1): created_at is the ALERT's, never the startup time."""
    alert = make_alert(alert_id="late1")  # created 10:05:03; the book starts at 10:08
    store_desk_signal(engine, alert)

    book = make_book()

    row = rows_by_alert(engine)["late1"]
    assert row["status"] == "PENDING_FILL"
    assert row["created_at"] == ist(THU, 10, 5, 3) and row["expires_at"] == ist(THU, 10, 15, 3)
    assert event_kinds(engine, row["id"]) == ["OPENED"]
    assert [t.alert_id for t in book.open_trades()] == ["late1"]


def test_a_reconciled_trade_fills_at_the_first_bar_after_the_alert(make_book, engine):
    """RED for T3: the hook never ran; the fill is still the 10:06 open, not a later bar."""
    store_desk_signal(engine, make_alert(alert_id="late1"))
    book = make_book()
    frame = make_bars(ist(THU, 10, 0), [990.0] * 6 + [1000.0] + [1010.0] * 20)

    events = book.step(ist(THU, 10, 27), series(RELIANCE=frame))

    assert [e.kind for e in events] == ["FILLED"]
    row = rows_by_alert(engine)["late1"]
    assert (row["fill_bar_ts"], row["entry_paise"]) == (ist(THU, 10, 6), 100030)


def test_reconciliation_adds_only_missing_rows_and_never_rewrites_existing_ones(make_book, engine):
    """RED for T3: an OPEN and a NO_FILL trade survive two restarts byte for byte."""
    first = make_book()
    first.on_alert(make_alert(alert_id="open1"), ist(THU, 10, 5, 4))
    first.on_alert(make_alert(alert_id="gap1", stop=1001.0), ist(THU, 10, 5, 4))
    first.step(ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2)))
    for alert_id in ("open1", "gap1", "new1"):
        store_desk_signal(engine, make_alert(alert_id=alert_id))
    kept = rows_by_alert(engine)

    for _ in range(2):
        make_book()

    rows = rows_by_alert(engine)
    assert sorted(rows) == ["gap1", "new1", "open1"]
    assert rows["open1"] == kept["open1"] and rows["gap1"] == kept["gap1"]
    assert len(table_rows(engine, paper_events)) == 3 + 2  # 3 OPENED + FILLED + NO_FILL


def test_a_disabled_book_reconciles_nothing(make_book, engine):
    """Guard (green now, must stay green): paper.enabled=false writes nothing at startup."""
    store_desk_signal(engine, make_alert(alert_id="r1"))
    book = make_book(enabled=False)
    assert table_rows(engine, paper_trades) == [] and book.open_trades() == []


def test_one_unreadable_desk_signal_is_skipped_and_logged_never_fatal(make_book, engine, caplog):
    """RED for T3: a bad row (no payload, no economics, a naive time) cannot stop startup or
    the reconciliation of the good rows around it."""
    store_desk_signal(engine, make_alert(alert_id="good1"))
    store_desk_signal(engine, make_alert(alert_id="nopayload"), payload=None)
    broken = make_alert(alert_id="noeco")
    del broken["economics"]
    store_desk_signal(engine, broken)
    naive = make_alert(alert_id="naive1")
    naive["created_at"] = "2026-10-29T10:05:03"  # only in the payload: the column stays aware
    store_desk_signal(engine, make_alert(alert_id="naive1"), payload=naive)
    store_desk_signal(engine, make_alert(alert_id="good2"))

    with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
        book = make_book()

    assert sorted(rows_by_alert(engine)) == ["good1", "good2"]
    assert {t.alert_id for t in book.open_trades()} == {"good1", "good2"}
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    for bad in ("nopayload", "noeco", "naive1"):
        assert bad in warned, warned


def test_a_payload_without_an_id_is_reconciled_under_the_desk_signals_id(make_book, engine):
    """RED for T3: desk_signals.id is the key; the payload is only the alert body."""
    alert = make_alert(alert_id="row-id")
    payload = {k: v for k, v in alert.items() if k != "id"}
    store_desk_signal(engine, alert, payload=payload)

    make_book()

    assert sorted(rows_by_alert(engine)) == ["row-id"]


def test_a_database_without_desk_tables_still_builds_a_book(tmp_path):
    """Guard (green now, must stay green): a paper-only database has nothing to reconcile."""
    engine = create_engine(f"sqlite:///{tmp_path / 'paper_only.db'}")
    try:
        book = PaperBook(
            engine,
            PaperCfg(),
            BT,
            SPECS,
            lambda key: pytest.fail("no costs needed"),
            FakePricer(),
            clock=lambda: ist(THU, 10, 8),
        )
        assert book.open_trades() == []
    finally:
        engine.dispose()
