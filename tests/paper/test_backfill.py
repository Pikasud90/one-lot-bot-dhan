"""US-023 NFR (developer tests): the startup backfill of costs and net.

    CLOSED rows written by US-002 with costs_paise/net_paise NULL get them computed ONCE when a
    PaperBook starts (idempotent), with the very charges a close books today
    (`charges.round_trip_paise` on the real sides), so the Paper panel shows a net for today.

The Tester's acceptance tests (tests/acceptance/test_us023_nfr_contract.py::TestStartupBackfill,
test_us023_pinned_proposals.py A-11/A-12, P5) cover the oracle values, rows that must stay as
they are, a disabled book and 300 rows. These pin the edges at the boundary: a row another book
costed meanwhile is never overwritten (the UPDATE is guarded), money that is not whole paise is
never backfilled, a row the cost model cannot price is left NULL and counted, a second start
issues no UPDATE at all, and the first today_totals after a restart shows the net.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from sqlalchemy import event, text

from onelot.config import CostConfig
from onelot.costs import CostModel
from onelot.paper.charges import round_trip_paise
from onelot.store.schema import paper_trades

from .conftest import SPECS, THU, ist, table_rows

ENTRY, EXIT = 100_030, 102_000  # RELIANCE long, 1 share: gross 1970


def legacy(engine, alert_id: str, *, exit_ts: dt.datetime | None = None, **over: Any) -> None:
    """A CLOSED stock long as US-002 wrote it: priced, gross booked, costs and net NULL."""
    ts = exit_ts or ist(THU, 11, 0)
    row: dict[str, Any] = {
        "alert_id": alert_id,
        "status": "CLOSED",
        "close_reason": "TARGET",
        "grade": "live",
        "data_source": "dhan",
        "instrument_key": "RELIANCE",
        "series_key": "RELIANCE",
        "kind": "STOCK",
        "side": "BUY",
        "direction": "long",
        "strategy": "ema_cross",
        "timeframe": "5m",
        "confidence": 62.5,
        "product": "equity_intraday",
        "qty": 1,
        "entry_ts": ist(THU, 10, 6),
        "fill_bar_ts": ist(THU, 10, 6),
        "exit_ts": ts,
        "last_bar_ts": ts,
        "resolution_min": 1,
        "entry_paise": ENTRY,
        "exit_paise": EXIT,
        "gross_paise": EXIT - ENTRY,
        "costs_paise": None,
        "net_paise": None,
        "risk_paise": 530,
        "created_at": ist(THU, 10, 5),
        "expires_at": ist(THU, 10, 15),
        "plan": {"alert": {}, "levels": {}},
        "updated_at": ts,
    } | over
    with engine.begin() as conn:
        conn.execute(paper_trades.insert().values(**row))


def by_alert(engine) -> dict[str, dict[str, Any]]:
    return {r["alert_id"]: r for r in table_rows(engine, paper_trades)}


def oracle_costs(cost_cfg: CostConfig) -> int:
    model = CostModel(cost_cfg.product(SPECS["RELIANCE"].product))
    return round_trip_paise(model, "STOCK", "long", ENTRY, EXIT, 1)


def test_a_row_another_book_costed_meanwhile_is_never_overwritten(make_book, engine, cost_cfg):
    """Two books may start on one database. The backfill writes a row only while its costs
    and net are still NULL (a guarded UPDATE), so a value another book wrote first stays."""
    legacy(engine, "RACED")
    legacy(engine, "PLAIN")
    model = CostModel(cost_cfg.product(SPECS["RELIANCE"].product))
    raced = {"done": False}

    def cost_for(key: str) -> CostModel:
        if not raced["done"]:  # the other book wins the race for RACED right now
            raced["done"] = True
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE paper_trades SET costs_paise = 1, net_paise = gross_paise - 1 "
                        "WHERE alert_id = 'RACED'"
                    )
                )
        return model

    make_book(cost_for=cost_for)
    rows = by_alert(engine)
    assert (rows["RACED"]["costs_paise"], rows["RACED"]["net_paise"]) == (1, 1969)
    costs = oracle_costs(cost_cfg)
    assert (rows["PLAIN"]["costs_paise"], rows["PLAIN"]["net_paise"]) == (costs, 1970 - costs)


def test_money_that_is_not_whole_paise_is_never_backfilled_or_truncated(
    make_book, engine, cost_cfg, caplog
):
    """A REAL gross (a corrupt write) is UNKNOWN: no net is derived from it, it is warned
    about, and the book still starts and backfills the other rows."""
    legacy(engine, "BAD")
    legacy(engine, "GOOD")
    with engine.begin() as conn:
        conn.execute(text("UPDATE paper_trades SET gross_paise = 1970.5 WHERE alert_id = 'BAD'"))
    with caplog.at_level(logging.WARNING, logger="onelot.paper"):
        make_book()
    with engine.connect() as conn:
        kinds = dict(
            conn.execute(text("SELECT alert_id, typeof(gross_paise) FROM paper_trades")).all()
        )
    rows = by_alert(engine)
    assert (rows["BAD"]["costs_paise"], rows["BAD"]["net_paise"]) == (None, None)
    assert kinds["BAD"] == "real", "the corrupt cell is left as it is, never rewritten"
    assert rows["GOOD"]["costs_paise"] == oracle_costs(cost_cfg)
    assert any("gross_paise" in r.getMessage() for r in caplog.records), caplog.text


def test_a_row_the_cost_model_cannot_price_stays_null_and_is_counted(make_book, engine, cost_cfg):
    """PROPOSED (Dev Alpha, for the Manager): like a close whose cost model fails (T1), a
    backfill that cannot price a row leaves it NULL (a later start retries it) and is counted
    in hook_errors, so the panel's status shows it; it never stops the other rows."""
    legacy(engine, "GONE", instrument_key="GONE")
    legacy(engine, "OK")
    model = CostModel(cost_cfg.product(SPECS["RELIANCE"].product))

    def cost_for(key: str) -> CostModel:
        if key == "GONE":
            raise KeyError(f"paper: no instrument spec for {key!r}")
        return model

    book = make_book(cost_for=cost_for)
    rows = by_alert(engine)
    assert (rows["GONE"]["costs_paise"], rows["GONE"]["net_paise"]) == (None, None)
    assert rows["OK"]["costs_paise"] == oracle_costs(cost_cfg)
    assert book.status()["hook_errors"] == 1


def test_a_second_start_issues_no_update_at_all(make_book, engine, cost_cfg):
    """Idempotent at the SQL level: once every row that can be costed is, a later start
    selects nothing to write (an unpriced close, exit NULL, is never a candidate)."""
    legacy(engine, "A")
    legacy(engine, "UNPRICED", exit_paise=None, gross_paise=None)
    make_book()
    assert by_alert(engine)["A"]["costs_paise"] == oracle_costs(cost_cfg)
    updates: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        make_book()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert updates == []


@pytest.mark.parametrize("enabled", [True, False])
def test_the_first_today_totals_after_a_restart_shows_the_net(make_book, engine, cost_cfg, enabled):
    """The point of the backfill: today's closes made before the upgrade show a net on the
    Paper panel as soon as the engine is back, not a null."""
    legacy(engine, "TODAY", exit_ts=ist(THU, 11, 0))
    book = make_book(enabled=enabled)
    totals = book.today_totals(ist(THU, 15, 0))
    costs = oracle_costs(cost_cfg)
    assert (totals["closed"], totals["gross_paise"], totals["net_paise"]) == (
        1,
        1970,
        1970 - costs,
    )


# ── T3 (Dev Beta): the boundaries the implementation added ────────────────────────────
@pytest.mark.parametrize(
    "over",
    [
        {"gross_paise": 1971},  # one paisa off sign x (exit - entry) x qty
        {"gross_paise": -1970},  # the sign of a short on a long row
        {"direction": "flat"},  # no cost sides: never guessed
        {"qty": 0},
    ],
    ids=["gross-off-by-one", "gross-wrong-sign", "unknown-direction", "qty-zero"],
)
def test_a_row_whose_money_does_not_add_up_is_left_null_warned_and_counted(
    make_book, engine, cost_cfg, caplog, over
):
    """A net derived from a gross that disagrees with its own legs would be a guess."""
    legacy(engine, "ODD", **over)
    legacy(engine, "GOOD")
    with caplog.at_level(logging.WARNING, logger="onelot.paper"):
        book = make_book()
    rows = by_alert(engine)
    assert (rows["ODD"]["costs_paise"], rows["ODD"]["net_paise"]) == (None, None)
    assert rows["GOOD"]["costs_paise"] == oracle_costs(cost_cfg)
    assert book.status()["hook_errors"] == 1
    assert any("paper_trades.id=1" in r.getMessage() for r in caplog.records), caplog.text


def test_a_short_stock_is_backfilled_sell_then_buy(make_book, engine, cost_cfg):
    """The backfill uses the real sides, like the close: STT on a short's ENTRY leg."""
    entry, exit_ = 9_997_000, 9_800_000  # sold at 99,970.00, bought back at 98,000.00
    legacy(
        engine,
        "SHORT",
        side="SELL",
        direction="short",
        entry_paise=entry,
        exit_paise=exit_,
        gross_paise=entry - exit_,
    )
    make_book()
    row = by_alert(engine)["SHORT"]
    assert (row["costs_paise"], row["net_paise"]) == (8254, 197_000 - 8254)


def test_a_locked_database_defers_the_backfill_and_never_stops_the_book(tmp_path, cost_cfg):
    """SQLite's write lock held by another process: the book starts (reads still work), the
    backfill writes nothing, says so once and counts it; the next start completes it."""
    from onelot.config import PaperCfg
    from onelot.paper.book import PaperBook
    from onelot.store import init_database

    from .conftest import BT, FakePricer, short_engine, write_lock

    path = tmp_path / "locked.db"
    init_database(f"sqlite:///{path}").dispose()
    eng = short_engine(path)
    try:
        legacy(eng, "A")
        models = {k: CostModel(cost_cfg.product(s.product)) for k, s in SPECS.items()}

        def start() -> PaperBook:
            return PaperBook(
                eng,
                PaperCfg(),
                BT,
                SPECS,
                models.__getitem__,
                FakePricer(),
                clock=lambda: ist(THU, 15, 0),
            )

        with write_lock(path):
            book = start()
            assert by_alert(eng)["A"]["costs_paise"] is None
        assert book.status()["hook_errors"] == 1
        start()
        assert by_alert(eng)["A"]["costs_paise"] == oracle_costs(cost_cfg)
    finally:
        eng.dispose()
