"""US-030 mode B: the edges of GET /api/paper that the mode-A tests left as risks.

* the IST day boundary on a FROZEN clock (just after and just before midnight, year end, a
  leap day, and the UTC-date trap), so nothing here depends on when the suite runs;
* managed (PENDING_FILL / OPEN) trades are listed first WITHIN the 200 cap: an old open trade
  is never pushed off the list by newer closed ones (Manager decision 2026-11-01);
* a corrupt row through the real endpoint: never a 500, the good rows are still served, the
  corrupt row is hidden (never truncated into a wrong number), and a corrupt amount makes the
  day's sum unknown (null), never a wrong total;
* timestamps are emitted in IST (Manager decision), and reads stay consistent while the book
  is being written.

Same harness as the mode-A tests: the real app factory over a stub engine with a real Desk and
a real PaperBook on a tmp SQLite file; rows go in through the schema.
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
    H.freeze_clock(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = base.new_db(tmp_path, "us030-b.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


@pytest.fixture
def app(app_cfg, db):
    return H.make_app(app_cfg, db)


def seed(app, rows):
    H.insert_trades(app.db.engine, rows)


def ids(body) -> list[str]:
    return [t["alert_id"] for t in body["trades"]]


def ist(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


# ══ the IST day on a frozen clock ═════════════════════════════════════════════════════
# (id, "now", [(alert, closed_at, gross paise)], alerts counted today, gross of those)
DAY_CASES = [
    pytest.param(
        "2026-11-02T00:00:00.500+05:30",
        [
            ("EARLIER", "2026-11-01T23:59:59+05:30", 100),
            ("FIRST", "2026-11-02T00:00:00+05:30", 200),
        ],
        ["FIRST"],
        "2.00",
        id="half-a-second-after-midnight",
    ),
    pytest.param(
        "2026-11-01T23:59:59.500+05:30",
        [
            ("FIRST", "2026-11-01T00:00:00+05:30", 100),
            ("PREVIOUS", "2026-10-31T23:59:59+05:30", 200),
            ("LAST", "2026-11-01T23:59:59+05:30", 300),
        ],
        ["FIRST", "LAST"],
        "4.00",
        id="half-a-second-before-midnight",
    ),
    pytest.param(
        "2027-01-01T00:00:30+05:30",
        [
            ("OLD-YEAR", "2026-12-31T23:59:59+05:30", 100),
            ("NEW-YEAR", "2027-01-01T00:00:00+05:30", 50),
        ],
        ["NEW-YEAR"],
        "0.50",
        id="new-year",
    ),
    pytest.param(
        "2028-02-29T10:00:00+05:30",
        [("FEB-28", "2028-02-28T23:59:59+05:30", 100), ("FEB-29", "2028-02-29T00:00:00+05:30", 70)],
        ["FEB-29"],
        "0.70",
        id="leap-day",
    ),
    pytest.param(
        "2026-11-02T03:00:00+05:30",  # 21:30 on the 1st in UTC
        [
            ("IST-TODAY", "2026-11-02T00:10:00+05:30", 100),  # 18:40Z on the 1st
            ("IST-YESTERDAY", "2026-11-01T23:50:00+05:30", 200),  # 18:20Z on the 1st
        ],
        ["IST-TODAY"],
        "1.00",
        id="utc-date-trap",
    ),
]


class TestTheIstDayOnAFrozenClock:
    @pytest.mark.parametrize("now, closes, counted, gross", DAY_CASES)
    def test_totals_today_count_only_trades_closed_on_the_ist_date_of_now(
        self, app, monkeypatch, now, closes, counted, gross
    ):
        H.freeze_clock(monkeypatch, ist(now))
        seed(app, [H.closed_row(alert, ist(when), paise) for alert, when, paise in closes])
        totals = H.served(app)["totals_today"]
        assert totals["closed"] == len(counted), (totals, counted)
        assert totals["gross_rupees"] == gross, totals

    def test_the_listing_does_not_depend_on_the_clock(self, app, monkeypatch):
        """Only totals_today looks at 'today': the trades list is the same either side of IST
        midnight."""
        seed(app, [H.closed_row("X", ist("2026-11-01T23:59:00+05:30"), 100)])
        before = H.served(app)["trades"]
        H.freeze_clock(monkeypatch, ist("2026-11-02T00:00:01+05:30"))
        assert H.served(app)["trades"] == before

    def test_a_trade_closed_exactly_at_midnight_belongs_to_the_new_day_only(self, app, monkeypatch):
        seed(app, [H.closed_row("AT-MIDNIGHT", ist("2026-11-02T00:00:00+05:30"), 100)])
        H.freeze_clock(monkeypatch, ist("2026-11-01T23:59:59.900+05:30"))
        assert H.served(app)["totals_today"]["closed"] == 0
        H.freeze_clock(monkeypatch, ist("2026-11-02T00:00:00.100+05:30"))
        assert H.served(app)["totals_today"]["closed"] == 1


class TestTimestampsAreEmittedInIst:
    def test_every_timestamp_carries_the_ist_offset(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        for trade in H.served(app)["trades"]:
            for field in ("created_at", "entry_ts", "closed_at"):
                if trade[field] is not None:
                    assert trade[field].endswith("+05:30"), (trade["alert_id"], field, trade[field])

    def test_a_no_fill_trade_is_closed_when_the_book_gave_up(self, app):
        gave_up = H.ORDER_BASE + 11 * MIN
        row = H.trade_values(
            "GAVE-UP",
            status="NO_FILL",
            close_reason="NO_FILL",
            gross=0,
            costs=0,
            net=0,
            created=H.ORDER_BASE,
        )
        row["updated_at"] = gave_up
        seed(app, [row])
        trade = H.trades_by_alert(H.served(app))["GAVE-UP"]
        assert H.parse_ts(trade["closed_at"]) == gave_up

    def test_waiting_and_open_trades_have_no_closed_at(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        trades = H.trades_by_alert(H.served(app))
        assert trades["A-PENDING"]["closed_at"] is None and trades["A-OPEN"]["closed_at"] is None


# ══ managed trades first, within the cap ══════════════════════════════════════════════
class TestManagedTradesAreNeverPushedOffTheList:
    @staticmethod
    def book(app, closed: int = 205):
        rows = [
            H.trade_values("OLD-OPEN", status="OPEN", created=H.ORDER_BASE - dt.timedelta(days=60)),
            H.trade_values(
                "OLD-PEND", status="PENDING_FILL", created=H.ORDER_BASE - dt.timedelta(days=40)
            ),
        ]
        rows += [
            H.closed_row(
                f"C{i:03d}", H.ORDER_BASE + i * MIN + 25 * MIN, 100, created=H.ORDER_BASE + i * MIN
            )
            for i in range(closed)
        ]
        seed(app, rows)

    def test_an_old_open_trade_is_listed_ahead_of_two_hundred_newer_closed_ones(self, app):
        self.book(app)
        got = ids(H.served(app))
        assert len(got) == H.MAX_TRADES
        assert "OLD-OPEN" in got and "OLD-PEND" in got

    def test_the_closed_ones_that_make_room_are_the_oldest_closed(self, app):
        self.book(app)
        got = ids(H.served(app))
        assert not {f"C{i:03d}" for i in range(7)} & set(got)
        assert {"C007", "C204"} <= set(got)

    def test_the_list_is_still_newest_first_with_the_old_open_trades_last(self, app):
        self.book(app)
        assert ids(H.served(app)) == [f"C{i:03d}" for i in range(204, 6, -1)] + [
            "OLD-PEND",
            "OLD-OPEN",
        ]

    def test_below_the_cap_nothing_is_dropped_and_the_order_is_by_age(self, app):
        self.book(app, closed=3)
        assert ids(H.served(app)) == ["C002", "C001", "C000", "OLD-PEND", "OLD-OPEN"]

    def test_more_than_200_managed_trades_still_list_at_most_200_the_newest_ones(self, app):
        """AC1's cap holds for managed trades too (Manager: 'managed first within the cap')."""
        seed(
            app,
            [
                H.trade_values(f"M{i:03d}", status="OPEN", created=H.ORDER_BASE + i * MIN)
                for i in range(205)
            ],
        )
        got = ids(H.served(app))
        assert got == [f"M{i:03d}" for i in range(204, 4, -1)]

    def test_the_status_counts_do_not_depend_on_which_trades_made_the_list(self, app):
        self.book(app)
        assert set(H.served(app)["status"]) == H.STATUS_KEYS


# ══ a corrupt row through the real endpoint ═══════════════════════════════════════════
CORRUPTIONS = [
    pytest.param("created_at = 'garbage'", "open", id="created_at"),
    pytest.param("entry_ts = 'garbage'", "open", id="entry_ts"),
    pytest.param("exit_ts = 'garbage'", "closed", id="exit_ts"),
    pytest.param("updated_at = 'garbage'", "open", id="updated_at"),
    pytest.param("entry_paise = 'abc'", "open", id="entry-text"),
    pytest.param("entry_paise = 100030.5", "open", id="entry-fraction-of-a-paisa"),
    pytest.param("gross_paise = 12.5", "closed", id="gross-fraction"),
    pytest.param("net_paise = 'x'", "closed", id="net-text"),
    pytest.param("plan = 'not json at all'", "open", id="plan"),
    pytest.param("status = 'WEIRD'", "open", id="status"),
    pytest.param("grade = 'sideways'", "open", id="grade"),
    pytest.param("qty = 'many'", "open", id="qty"),
]


def good_rows() -> list[dict]:
    m = H.midnight()
    return [
        H.trade_values("G1", status="OPEN", created=m + MIN, entry=100030, entry_ts=m + 2 * MIN),
        H.closed_row("G2", m + 3 * SEC, 500),
        H.trade_values("G3", status="PENDING_FILL", created=m + 5 * MIN),
    ]


def bad_row(kind: str) -> dict:
    m = H.midnight()
    if kind == "closed":
        return H.closed_row("BAD", m + 4 * SEC, 1000, created=m + 10 * MIN)
    return H.trade_values(
        "BAD", status="OPEN", created=m + 10 * MIN, entry=100030, entry_ts=m + 11 * MIN
    )


def corrupt(app, assignment: str) -> None:
    with app.db.engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE paper_trades SET {assignment} WHERE alert_id = 'BAD'")


class TestACorruptRow:
    @pytest.mark.parametrize("assignment, kind", CORRUPTIONS)
    def test_it_is_never_a_500_and_the_good_rows_are_still_served(self, app, assignment, kind):
        seed(app, [*good_rows(), bad_row(kind)])
        corrupt(app, assignment)
        resp = H.get_paper(app)
        assert resp.status_code == 200, f"{assignment}: {resp.status_code} {resp.text[:200]}"
        assert {"G1", "G2", "G3"} <= set(ids(resp.json())), ids(resp.json())

    @pytest.mark.parametrize("assignment, kind", CORRUPTIONS)
    def test_the_corrupt_row_is_hidden_not_shown_with_a_wrong_value(self, app, assignment, kind):
        seed(app, [*good_rows(), bad_row(kind)])
        corrupt(app, assignment)
        assert "BAD" not in ids(H.served(app)), f"{assignment}: the corrupt row is on the desk"

    def test_the_status_block_and_shape_survive_a_corrupt_row(self, app):
        seed(app, [*good_rows(), bad_row("open")])
        corrupt(app, "plan = 'not json at all'")
        body = H.served(app)
        assert body["enabled"] is True and set(body["status"]) == H.STATUS_KEYS
        assert set(body["totals_today"]) == H.TOTALS_KEYS

    def test_every_row_corrupt_is_an_empty_list_not_an_error(self, app):
        seed(app, [bad_row("open")])
        corrupt(app, "created_at = 'garbage'")
        resp = H.get_paper(app)
        assert resp.status_code == 200 and resp.json()["trades"] == []

    @pytest.mark.parametrize("assignment", ["gross_paise = 12.5", "gross_paise = 'abc'"])
    def test_a_corrupt_gross_makes_the_days_gross_unknown_never_a_wrong_sum(self, app, assignment):
        seed(app, [H.closed_row("G2", H.midnight() + 3 * SEC, 1000), bad_row("closed")])
        corrupt(app, assignment)
        totals = H.served(app)["totals_today"]
        assert totals["gross_rupees"] is None, f"a partial or truncated sum was served: {totals}"
        assert totals["closed"] == 2

    def test_a_corrupt_net_leaves_the_gross_total_exact(self, app):
        seed(app, [H.closed_row("G2", H.midnight() + 3 * SEC, 1000), bad_row("closed")])
        corrupt(app, "net_paise = 'x'")
        totals = H.served(app)["totals_today"]
        assert totals["gross_rupees"] == "20.00" and totals["net_rupees"] is None


# ══ reads while the book is being written ═════════════════════════════════════════════
class TestReadsWhileTheBookIsWritten:
    def test_every_read_is_a_consistent_listing_while_alerts_are_booked(self, app):
        booked = 30
        alerts = [
            base.make_alert(f"W{i:02d}", created=base.at(10, 0, 0) + i * MIN) for i in range(booked)
        ]
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for alert in alerts:
                    base.open_trade(app.book, alert)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        counts = []
        while thread.is_alive():
            body = H.served(app)
            listed = ids(body)
            assert len(listed) == len(set(listed)), "a trade was listed twice"
            assert listed == sorted(listed, reverse=True), "not newest first"
            counts.append(len(listed))
        thread.join(30)
        assert not errors, errors
        assert counts == sorted(counts), "the listing shrank while trades were only being added"
        final = H.served(app)
        assert len(final["trades"]) == booked and final["status"]["opened"] == booked
