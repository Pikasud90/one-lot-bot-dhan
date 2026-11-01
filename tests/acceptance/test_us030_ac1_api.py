"""US-030 AC1 (and the data side of AC3): GET /api/paper.

The story: the owner wants to see every paper trade and its outcome without reading the
database. The contract (Manager's Notes, 2026-11-01):

    GET /api/paper -> {enabled, status: {opened, filled, no_fill, closed, hook_errors,
                                         desk_hook_errors},
                       trades: [... newest first, at most 200],
                       totals_today: {closed, wins, losses, gross_rupees, net_rupees}}
    trade: alert_id, instrument, side, direction, kind, strategy, timeframe, grade, status,
           close_reason, qty, entry_ts, entry, exit, gross, net, created_at, closed_at
    money: rupee STRINGS from integer paise with Decimal, never float ('1000.30', '-12.05');
           net is null when not yet computed (US-023); read-only; same auth as other GETs.

Driven through the real app factory over a stub engine that carries a real Desk and a real
PaperBook on a throw-away SQLite file. Trades in every status are inserted through the schema,
so these tests do not need US-002 (closing trades): CLOSED rows have exit and gross set and
net NULL. Expected values are literals, never derived with the code under test.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
import us001_helpers as base
import us030_helpers as H

from onelot.config import AppConfig

MIN = dt.timedelta(minutes=1)
SEC = dt.timedelta(seconds=1)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    base.block_network(monkeypatch)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """One fixed 'now' (a Monday, 11:00 IST) for the tests AND for GET /api/paper's own clock,
    so "today" rows can never straddle IST midnight mid-test."""
    H.freeze_clock(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = base.new_db(tmp_path, "us030.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


@pytest.fixture
def app(app_cfg, db):
    return H.make_app(app_cfg, db)


def seed(app, rows):
    H.insert_trades(app.db.engine, rows)


def ids(body) -> list[str]:
    return [t["alert_id"] for t in body["trades"]]


def numbered(n: int) -> list[dict]:
    """`n` trades whose created_at rises by a minute per row: N000 is the oldest."""
    return [
        H.trade_values(f"N{i:03d}", status="OPEN", created=H.ORDER_BASE + i * MIN) for i in range(n)
    ]


# ══ the shape of the response ═════════════════════════════════════════════════════════
class TestShape:
    def test_serves_json_with_the_documented_top_level_keys(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        resp = H.get_paper(app)
        assert resp.status_code == 200, resp.text[:300]
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert {"enabled", "status", "trades", "totals_today"} <= set(body), sorted(body)
        assert body["enabled"] is True
        assert isinstance(body["trades"], list) and len(body["trades"]) == 4

    def test_status_block_names_exactly_the_documented_counters(self, app):
        status = H.served(app)["status"]
        assert set(status) == H.STATUS_KEYS, sorted(status)
        assert all(type(v) is int for v in status.values()), status

    def test_totals_today_names_exactly_the_documented_fields(self, app):
        totals = H.served(app)["totals_today"]
        assert set(totals) == H.TOTALS_KEYS, sorted(totals)

    def test_every_trade_carries_every_documented_field(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        for trade in H.served(app)["trades"]:
            missing = H.TRADE_KEYS - set(trade)
            assert not missing, f"{trade.get('alert_id')}: missing {sorted(missing)}"


# ══ one trade in every status, values exactly as stored ═══════════════════════════════
class TestEveryStatus:
    @staticmethod
    def trades(app) -> dict[str, dict]:
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        return H.trades_by_alert(H.served(app))

    def test_all_four_statuses_are_served_with_the_books_own_status_names(self, app):
        trades = self.trades(app)
        assert {k: t["status"] for k, t in trades.items()} == {
            "A-PENDING": "PENDING_FILL",
            "A-OPEN": "OPEN",
            "A-NOFILL": "NO_FILL",
            "A-CLOSED": "CLOSED",
        }

    def test_a_trade_waiting_for_its_fill_has_no_prices_and_no_close(self, app):
        trades = self.trades(app)
        t = trades["A-PENDING"]
        assert (t["entry"], t["exit"], t["gross"], t["net"]) == (None, None, None, None)
        assert (t["close_reason"], t["entry_ts"], t["closed_at"]) == (None, None, None)
        assert (t["instrument"], t["side"], t["direction"], t["kind"]) == (
            "SBIN",
            "BUY",
            "long",
            "STOCK",
        )
        assert (t["strategy"], t["timeframe"], t["grade"], t["qty"]) == (
            "ema_cross",
            "10m",
            "delayed",
            1,
        )

    def test_an_open_trade_shows_its_entry_in_rupees_and_nothing_after_it(self, app):
        trades = self.trades(app)
        t = trades["A-OPEN"]
        assert t["entry"] == "1000.30"
        assert (t["exit"], t["gross"], t["net"], t["closed_at"]) == (None, None, None, None)
        assert t["grade"] == "live"
        assert H.parse_ts(t["entry_ts"]) == H.ORDER_BASE + MIN

    def test_the_instrument_is_served_verbatim_not_html_escaped(self, app):
        trades = self.trades(app)
        assert trades["A-OPEN"]["instrument"] == "M&M"

    def test_a_no_fill_trade_has_no_prices_and_zero_money(self, app):
        trades = self.trades(app)
        t = trades["A-NOFILL"]
        assert (t["entry"], t["exit"]) == (None, None)
        assert t["close_reason"] == "NO_FILL"
        assert (t["gross"], t["net"]) == ("0.00", "0.00"), "stored zero money is served as 0.00"

    def test_a_closed_trade_shows_exit_gross_and_a_null_net_until_it_is_computed(self, app):
        trades = self.trades(app)
        t = trades["A-CLOSED"]
        assert (t["entry"], t["exit"], t["gross"]) == ("1000.30", "988.25", "-12.05")
        assert t["net"] is None, "net is null until US-023 computes it"
        assert t["close_reason"] == "STOP" and t["instrument"] == "RELIANCE"
        assert H.parse_ts(t["closed_at"]) == H.ORDER_BASE + 30 * MIN
        assert H.parse_ts(t["entry_ts"]) == H.ORDER_BASE + 10 * MIN

    def test_created_at_is_the_alerts_own_time(self, app):
        trades = self.trades(app)
        assert H.parse_ts(trades["A-PENDING"]["created_at"]) == H.ORDER_BASE
        assert H.parse_ts(trades["A-OPEN"]["created_at"]) == H.ORDER_BASE + SEC

    def test_an_index_option_trade_is_named_and_sized_by_its_lot(self, app):
        seed(
            app,
            [
                H.trade_values(
                    "A-IDX",
                    status="OPEN",
                    kind="INDEX",
                    instrument="BANKNIFTY_OPT",
                    series="BANKNIFTY",
                    direction="short",
                    entry=45000,
                    entry_ts=H.ORDER_BASE + MIN,
                )
            ],
        )
        t = H.trades_by_alert(H.served(app))["A-IDX"]
        assert "BANKNIFTY" in t["instrument"].upper()
        assert (t["kind"], t["side"], t["direction"], t["qty"]) == ("INDEX", "BUY_PE", "short", 30)
        assert t["entry"] == "450.00"


# ══ rupee strings are exact ═══════════════════════════════════════════════════════════
MONEY = [
    (100030, "1000.30"),
    (-1205, "-12.05"),
    (0, "0.00"),
    (1, "0.01"),
    (-1, "-0.01"),
    (5, "0.05"),
    (-5, "-0.05"),
    (99, "0.99"),
    (100, "1.00"),
    (-100, "-1.00"),
    (101, "1.01"),
    (123456789, "1234567.89"),
    (9007199254740993, "90071992547409.93"),  # 2**53 + 1 paise: a float cannot hold it
    (-9007199254740993, "-90071992547409.93"),
    (9223372036854775807, "92233720368547758.07"),  # the largest BIGINT
]


class TestRupeeStrings:
    def test_the_test_oracle_agrees_with_the_literals(self):
        assert [(p, H.rupees(p)) for p, _ in MONEY] == MONEY

    @pytest.mark.parametrize("paise, text", MONEY, ids=[str(p) for p, _ in MONEY])
    def test_every_money_field_is_the_exact_decimal_of_the_stored_paise(self, app, paise, text):
        seed(
            app,
            [
                H.trade_values(
                    "A-MONEY",
                    status="CLOSED",
                    close_reason="TARGET",
                    entry=paise,
                    exit=paise,
                    gross=paise,
                    net=paise,
                    entry_ts=H.ORDER_BASE,
                    exit_ts=H.ORDER_BASE + MIN,
                )
            ],
        )
        t = H.trades_by_alert(H.served(app))["A-MONEY"]
        assert (t["entry"], t["exit"], t["gross"], t["net"]) == (text, text, text, text)

    def test_money_is_a_string_or_null_never_a_json_number(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        for t in H.served(app)["trades"]:
            for field in ("entry", "exit", "gross", "net"):
                assert t[field] is None or isinstance(t[field], str), (t["alert_id"], field)
                if isinstance(t[field], str):
                    assert len(t[field].rsplit(".", 1)[-1]) == 2, f"{field}={t[field]!r}"


# ══ newest first, at most 200 ═════════════════════════════════════════════════════════
class TestOrderAndCap:
    def test_newest_first_by_created_at_not_by_row_id(self, app):
        offsets = [3, 1, 4, 0, 2]  # row ids 1..5 carry these created_at minutes
        seed(
            app,
            [
                H.trade_values(f"T{i}", status="OPEN", created=H.ORDER_BASE + off * MIN)
                for i, off in enumerate(offsets)
            ],
        )
        assert ids(H.served(app)) == ["T2", "T0", "T4", "T1", "T3"]

    def test_trades_created_in_the_same_instant_list_the_later_row_first(self, app):
        seed(
            app,
            [
                H.trade_values("TIE-A", status="OPEN", created=H.ORDER_BASE + MIN),
                H.trade_values("TIE-B", status="OPEN", created=H.ORDER_BASE + MIN),
                H.trade_values("OLDER", status="OPEN", created=H.ORDER_BASE),
            ],
        )
        assert ids(H.served(app)) == ["TIE-B", "TIE-A", "OLDER"]

    def test_more_than_200_trades_serve_the_newest_200(self, app):
        seed(app, numbered(205))
        got = ids(H.served(app))
        assert len(got) == H.MAX_TRADES
        assert got[0] == "N204" and got[-1] == "N005"
        assert not {"N000", "N001", "N002", "N003", "N004"} & set(got)
        assert got == [f"N{i:03d}" for i in range(204, 4, -1)]

    @pytest.mark.parametrize("n", [0, 1, 199, 200, 201])
    def test_the_list_is_the_whole_book_up_to_the_cap(self, app, n):
        if n:
            seed(app, numbered(n))
        assert len(H.served(app)["trades"]) == min(n, H.MAX_TRADES)

    def test_the_cap_is_applied_after_ordering_not_before(self, app):
        rows = numbered(201)
        rows.reverse()  # the NEWEST rows get the LOWEST row ids; the oldest row is last
        seed(app, rows)
        got = ids(H.served(app))
        assert len(got) == 200 and "N000" not in got and got[0] == "N200"


# ══ the status block ══════════════════════════════════════════════════════════════════
class TestStatusBlock:
    @staticmethod
    def book_three_alerts(app):
        """On SBIN, a series the Desk tracks: one alert fills (OPEN), one expired before any bar
        arrived (NO_FILL), one is still waiting for the next bar (PENDING_FILL)."""
        for alert in (
            base.make_alert("S-FILL"),  # 10:05:03: the 10:06 bar fills it
            base.make_alert("S-NOFILL", created=base.at(9, 30, 3)),  # 09:40:03 is long past
            base.make_alert("S-WAIT", created=base.at(10, 7, 30)),  # no bar after 10:07:30 yet
        ):
            base.open_trade(app.book, alert)
        app.book.step(base.at(10, 30), base.Feed(SBIN=base.standard_bars()))
        assert app.book.status() == {
            "opened": 3,
            "filled": 1,
            "no_fill": 1,
            "closed": 0,
            "hook_errors": 0,
        }, "harness: the book did not reach the intended states"

    def test_counts_are_the_books_and_the_desks_hook_errors_are_kept_apart(self, app):
        self.book_three_alerts(app)
        app.desk.hook_errors = 2
        status = H.served(app)["status"]
        assert status == {
            "opened": 3,
            "filled": 1,
            "no_fill": 1,
            "closed": 0,
            "hook_errors": 0,
            "desk_hook_errors": 2,
        }

    def test_the_trade_list_agrees_with_the_counts(self, app):
        self.book_three_alerts(app)
        got = {t["alert_id"]: t["status"] for t in H.served(app)["trades"]}
        assert got == {"S-FILL": "OPEN", "S-NOFILL": "NO_FILL", "S-WAIT": "PENDING_FILL"}

    def test_counts_are_read_live_not_from_the_last_desk_snapshot(self, app):
        assert H.served(app)["status"]["opened"] == 0
        base.open_trade(app.book, base.make_alert("LIVE-1"))
        assert H.served(app)["status"]["opened"] == 1  # no desk poll happened in between
        base.open_trade(app.book, base.make_alert("LIVE-2"))
        assert H.served(app)["status"]["opened"] == 2


# ══ totals_today ══════════════════════════════════════════════════════════════════════
class TestTotalsToday:
    @staticmethod
    def day_rows() -> list[dict]:
        m = H.midnight()
        yesterday_evening = m - dt.timedelta(hours=2)
        return [
            H.closed_row("T1", m, 100030),  # the very first instant of today: counts
            H.closed_row("T2", m + SEC, -1205),
            H.closed_row("T3", m + 2 * SEC, 0),  # breakeven: neither a win nor a loss
            H.closed_row("T4", m + 3 * SEC, 5, created=yesterday_evening),  # opened yesterday
            H.closed_row("Y1", m - SEC, 99999),  # 23:59:59 yesterday: not today
            H.trade_values(
                "NF", status="NO_FILL", close_reason="NO_FILL", gross=0, net=0, created=m + SEC
            ),
            H.trade_values("OP", status="OPEN", entry=100030, entry_ts=m + 5 * SEC, created=m),
        ]

    def test_counts_only_trades_closed_today_in_ist(self, app):
        seed(app, self.day_rows())
        totals = H.served(app)["totals_today"]
        assert totals["closed"] == 4, totals
        assert totals["wins"] == 2 and totals["losses"] == 1, totals

    def test_gross_is_the_exact_sum_of_todays_closed_trades(self, app):
        seed(app, self.day_rows())
        totals = H.served(app)["totals_today"]
        assert totals["gross_rupees"] == "988.30"  # 1000.30 - 12.05 + 0.00 + 0.05
        assert isinstance(totals["gross_rupees"], str)

    def test_net_is_null_while_any_closed_trade_has_no_net_yet(self, app):
        seed(app, self.day_rows())
        assert H.served(app)["totals_today"]["net_rupees"] is None

    def test_nothing_closed_today_means_zero_counts_zero_gross_and_no_net(self, app):
        seed(app, [self.day_rows()[4]])  # only yesterday's close
        assert H.served(app)["totals_today"] == {
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "gross_rupees": "0.00",
            "net_rupees": None,
        }

    def test_net_is_the_exact_sum_once_every_closed_trade_has_a_net(self, app):
        m = H.midnight()
        seed(
            app,
            [
                H.closed_row("W", m + SEC, 1505, net=1200),
                H.closed_row("L", m + 2 * SEC, -1205, net=-1300),
            ],
        )
        assert H.served(app)["totals_today"] == {
            "closed": 2,
            "wins": 1,
            "losses": 1,
            "gross_rupees": "3.00",
            "net_rupees": "-1.00",
        }

    def test_a_hundred_ten_paise_trades_sum_to_exactly_ten_rupees(self, app):
        """0.10 x 100 is 9.99999999999998 in binary floating point."""
        m = H.midnight()
        seed(app, [H.closed_row(f"P{i:03d}", m + i * SEC, 10) for i in range(100)])
        totals = H.served(app)["totals_today"]
        assert totals["closed"] == 100 and totals["wins"] == 100 and totals["losses"] == 0
        assert totals["gross_rupees"] == "10.00"


# ══ AC3, the data side: no trades, paper off, no book ═════════════════════════════════
class TestNothingToShow:
    def test_an_empty_book_serves_an_empty_list_and_zero_counts(self, app):
        body = H.served(app)
        assert body["enabled"] is True and body["trades"] == []
        assert set(body["status"]) == H.STATUS_KEYS
        assert body["status"] == dict.fromkeys(H.STATUS_KEYS, 0)

    def test_paper_off_says_enabled_false_with_no_trades(self, app_cfg, db):
        app = H.make_app(app_cfg, db, enabled=False)
        body = H.served(app)
        assert body["enabled"] is False
        assert body["trades"] == []

    def test_paper_off_serves_no_trades_even_if_older_rows_exist(self, app_cfg, db):
        """Contract: `enabled false -> {enabled: false, trades: []}`. (See the report: the
        config says open trades keep being managed when paper is turned off.)"""
        app = H.make_app(app_cfg, db, enabled=False)
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        body = H.served(app)
        assert body["enabled"] is False
        assert body["trades"] == []

    @pytest.mark.parametrize(
        "kw", [{"book": False}, {"desk": False}], ids=["desk-without-a-book", "no-desk"]
    )
    def test_without_a_book_the_endpoint_answers_instead_of_crashing(self, app_cfg, db, kw):
        app = H.make_app(app_cfg, db, **kw)
        resp = H.get_paper(app)
        assert resp.status_code in (200, 503), f"{resp.status_code}: {resp.text[:200]}"
        if resp.status_code == 200:
            body = resp.json()
            assert body["enabled"] is False and body["trades"] == []


# ══ read-only, same auth as the other GET routes ══════════════════════════════════════
class TestReadOnlyAndAuth:
    def test_a_get_changes_nothing_in_the_database(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        before = base.snapshot_tables(app.db.engine)
        for _ in range(3):
            H.served(app)
        assert base.snapshot_tables(app.db.engine) == before

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_no_other_method_is_served_on_the_route(self, app, method):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        before = base.snapshot_tables(app.db.engine)
        resp = getattr(app.client, method)("/api/paper", headers={"x-onelot-token": H.TOKEN})
        assert resp.status_code == 405, f"{method.upper()} /api/paper -> {resp.status_code}"
        assert base.snapshot_tables(app.db.engine) == before

    @pytest.mark.parametrize(
        "headers, params",
        [
            ({}, {}),
            ({"x-onelot-token": "not-the-token"}, {}),
            ({"x-onelot-token": H.TOKEN}, {}),
            ({}, {"token": "not-the-token"}),
            ({}, {"token": H.TOKEN}),
        ],
        ids=["no-token", "wrong-header", "right-header", "wrong-query", "right-query"],
    )
    def test_the_route_answers_exactly_like_the_other_desk_reads(self, app, headers, params):
        paper = app.client.get("/api/paper", headers=headers, params=params)
        desk = app.client.get("/api/desk", headers=headers, params=params)
        assert paper.status_code == desk.status_code, (
            f"/api/paper -> {paper.status_code} but /api/desk -> {desk.status_code}"
        )


# ══ reading never waits for the Desk or the database ══════════════════════════════════
class TestReadsDoNotWait:
    @staticmethod
    def request_within(app, seconds: float):
        """GET /api/paper on a worker thread; the response, or None if it is still waiting."""
        box: dict[str, object] = {}
        worker = threading.Thread(target=lambda: box.update(resp=H.get_paper(app)), daemon=True)
        worker.start()
        worker.join(seconds)
        return None if worker.is_alive() else box["resp"]

    def test_it_answers_while_another_thread_holds_the_desk_lock(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        held, release = threading.Event(), threading.Event()

        def hold():
            with app.desk._lock:  # what a slow poll holds
                held.set()
                release.wait(15)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            resp = self.request_within(app, 5.0)
        finally:
            release.set()
            holder.join(5)
        assert resp is not None, "GET /api/paper waited for the Desk lock"
        assert resp.status_code == 200 and len(resp.json()["trades"]) == 4

    def test_it_answers_while_another_connection_holds_the_database_write_lock(self, app, tmp_path):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        with base.write_lock(tmp_path / "us030.db"):
            resp = self.request_within(app, 5.0)
        assert resp is not None, "a read waited behind a held SQLite write lock"
        assert resp.status_code == 200 and len(resp.json()["trades"]) == 4
