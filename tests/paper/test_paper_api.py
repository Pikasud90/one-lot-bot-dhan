"""GET /api/paper (US-030): the paper book for the desk's Paper panel. Read-only.

    {"enabled": bool,
     "status": {opened, filled, no_fill, closed, hook_errors, desk_hook_errors},
     "trades": PaperBook.recent(200)            (newest first, rupee strings),
     "totals_today": {closed, wins, losses, gross_rupees, net_rupees}}

paper.enabled false, no paper book, or no desk at all -> enabled false, trades [], totals_today
null (status still served). Same auth as every other GET: none. Over the real app factory,
with a stub engine whose desk carries a real PaperBook on a throw-away database.
"""

from __future__ import annotations

import datetime as dt
import warnings
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from onelot.api.server import create_app
from onelot.clock import now_ist
from onelot.store.schema import paper_trades

from .conftest import THU, ist, make_alert

with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

STATUS_KEYS = {"opened", "filled", "no_fill", "closed", "hook_errors", "desk_hook_errors"}
TOKEN = "test-token"


def client_for(desk: Any) -> TestClient:
    engine = MagicMock()
    engine.desk = desk
    return TestClient(create_app(engine, TOKEN), raise_server_exceptions=False)


def stub_desk(book: Any, hook_errors: Any = 2) -> SimpleNamespace:
    """What /api/paper needs of the Desk: its paper book and its own hook_errors count."""
    return SimpleNamespace(paper=book, hook_errors=hook_errors)


def book_today(book, engine, *, close: bool = False) -> None:
    """One trade raised a minute ago (today, IST); with `close`, closed as US-002 does."""
    now = now_ist().replace(microsecond=0)
    alert = make_alert(alert_id="today1", created=now - dt.timedelta(minutes=1))
    book.on_alert(alert, now)
    if close:
        with engine.begin() as conn:
            conn.execute(
                paper_trades.update()
                .where(paper_trades.c.alert_id == "today1")
                .values(
                    status="CLOSED",
                    close_reason="STOP",
                    entry_paise=100030,
                    exit_paise=98825,
                    gross_paise=-1205,
                    exit_ts=now,
                    updated_at=now,
                )
            )


class TestEnabled:
    def test_serves_status_trades_and_todays_totals(self, make_book, engine):
        book = make_book()
        book_today(book, engine, close=True)

        resp = client_for(stub_desk(book)).get("/api/paper")

        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"enabled", "status", "trades", "totals_today"}
        assert body["enabled"] is True
        assert set(body["status"]) == STATUS_KEYS
        assert body["status"]["opened"] == 1 and body["status"]["desk_hook_errors"] == 2
        (trade,) = body["trades"]
        assert (trade["alert_id"], trade["status"]) == ("today1", "CLOSED")
        assert (trade["entry"], trade["exit"], trade["gross"], trade["net"]) == (
            "1000.30",
            "988.25",
            "-12.05",
            None,
        )
        assert body["totals_today"] == {
            "closed": 1,
            "wins": 0,
            "losses": 1,
            "gross_rupees": "-12.05",
            "net_rupees": None,
        }

    def test_an_empty_book_serves_zero_totals_and_no_net(self, make_book):
        body = client_for(stub_desk(make_book(), hook_errors=0)).get("/api/paper").json()
        assert body["enabled"] is True and body["trades"] == []
        assert body["totals_today"] == {
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "gross_rupees": "0.00",
            "net_rupees": None,
        }
        assert body["status"] == dict.fromkeys(STATUS_KEYS, 0)

    def test_at_most_200_trades_newest_first(self, make_book):
        book = make_book()
        start = ist(THU, 9, 15)
        for i in range(201):
            created = start + dt.timedelta(seconds=i)
            book.on_alert(make_alert(alert_id=f"n{i:03d}", created=created), created)

        trades = client_for(stub_desk(book)).get("/api/paper").json()["trades"]

        assert len(trades) == 200
        assert (trades[0]["alert_id"], trades[-1]["alert_id"]) == ("n200", "n001")

    def test_the_desk_count_cannot_be_overwritten_by_the_book(self, make_book):
        book = make_book()
        book.status = lambda: {"opened": 4, "desk_hook_errors": 99}  # type: ignore[method-assign]
        status = client_for(stub_desk(book, hook_errors=3)).get("/api/paper").json()["status"]
        assert status["opened"] == 4 and status["desk_hook_errors"] == 3
        assert status["filled"] == 0  # a key the book did not report is still served

    @pytest.mark.parametrize("odd", [None, "7", True, MagicMock()])
    def test_a_desk_without_an_integer_hook_count_reports_zero(self, make_book, odd):
        status = client_for(stub_desk(make_book(), odd)).get("/api/paper").json()["status"]
        assert status["desk_hook_errors"] == 0

    def test_needs_no_token_like_every_other_read(self, make_book):
        client = client_for(stub_desk(make_book()))
        assert client.get("/api/paper").status_code == 200
        assert client.get("/api/paper", headers={"x-onelot-token": "wrong"}).status_code == 200

    @pytest.mark.parametrize("method", ["post", "put", "delete"])
    def test_is_read_only(self, make_book, method):
        resp = getattr(client_for(stub_desk(make_book())), method)("/api/paper")
        assert resp.status_code == 405

    def test_an_unreadable_database_is_a_503_not_a_crash(self, make_book):
        book = make_book()

        def locked(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("SELECT", {}, Exception("database is locked"))

        book.recent = locked  # type: ignore[method-assign]
        resp = client_for(stub_desk(book)).get("/api/paper")
        assert resp.status_code == 503
        assert "paper" in resp.json()["detail"]

    def test_the_503_detail_is_one_short_line_without_the_sql(self, make_book):
        # The panel shows it beside its title: the driver's message, not a statement dump.
        book = make_book()

        def locked(*_a: Any, **_k: Any) -> Any:
            raise OperationalError("SELECT secret FROM paper_trades", {"p": 1}, Exception("locked"))

        book.recent = locked  # type: ignore[method-assign]
        detail = client_for(stub_desk(book)).get("/api/paper").json()["detail"]
        assert detail == "paper book unreadable: locked"

    def test_an_error_without_a_message_is_still_a_503(self, make_book):
        book = make_book()

        def disk(*_a: Any, **_k: Any) -> Any:
            raise OSError()

        book.recent = disk  # type: ignore[method-assign]
        resp = client_for(stub_desk(book)).get("/api/paper")
        assert (resp.status_code, resp.json()["detail"]) == (503, "paper book unreadable: OSError")

    @pytest.mark.parametrize(
        "corrupt",
        [
            "created_at = 'garbage'",
            "exit_ts = 'garbage', status = 'CLOSED'",
            "gross_paise = 'abc', status = 'CLOSED', exit_ts = updated_at",
        ],
    )
    def test_a_corrupt_row_is_left_out_never_a_500(self, make_book, engine, corrupt):
        book = make_book()
        now = now_ist().replace(microsecond=0)
        for alert_id in ("good", "bad"):
            book.on_alert(make_alert(alert_id=alert_id, created=now - dt.timedelta(minutes=1)), now)
        with engine.begin() as conn:
            conn.exec_driver_sql(f"UPDATE paper_trades SET {corrupt} WHERE alert_id = 'bad'")

        resp = client_for(stub_desk(book)).get("/api/paper")

        assert resp.status_code == 200, resp.text[:300]
        assert "good" in [t["alert_id"] for t in resp.json()["trades"]]


class TestOff:
    OFF = {"enabled": False, "trades": [], "totals_today": None}

    def test_paper_disabled_serves_no_trades_but_the_counts(self, make_book, engine):
        book_today(make_book(), engine)  # a trade exists in the database
        book = make_book(enabled=False)

        body = client_for(stub_desk(book, hook_errors=1)).get("/api/paper").json()

        assert {k: body[k] for k in self.OFF} == self.OFF
        assert set(body["status"]) == STATUS_KEYS and body["status"]["desk_hook_errors"] == 1

    def test_a_desk_without_a_paper_book_is_served_as_off(self):
        body = client_for(stub_desk(None, hook_errors=5)).get("/api/paper").json()
        assert {k: body[k] for k in self.OFF} == self.OFF
        assert body["status"] == {**dict.fromkeys(STATUS_KEYS, 0), "desk_hook_errors": 5}

    def test_no_desk_at_all_is_served_as_off_not_a_503(self):
        resp = client_for(None).get("/api/paper")
        assert resp.status_code == 200
        body = resp.json()
        assert {k: body[k] for k in self.OFF} == self.OFF
        assert body["status"] == dict.fromkeys(STATUS_KEYS, 0)
