"""US-032 AC3 (and the data side of AC1): GET /api/paper tells the panel the last session.

AC3: "/api/paper adds totals for the chosen day so the day line is exact (a 'last_session' block
with its date and totals computed from integer paise on the server); the existing totals_today
and fields are unchanged."

Contract under test (Manager's Notes, 2026-11-02): `last_session` is `{date: "YYYY-MM-DD" (IST),
totals: {closed, wins, losses, gross_rupees, net_rupees}}` for the most recent IST date strictly
BEFORE today on which a CLOSED or NO_FILL trade closed, else null. A trade closes at its exit
time, else (every NO_FILL) at its updated_at: the US-030 `closed_at` rule. The totals count the
CLOSED trades of that date only, exactly as `totals_today` does for today.

Driven through the real app factory over a stub engine with a real Desk and PaperBook, on a
clock frozen on the owner's Sunday (`us032_helpers.SUNDAY`); rows go in through the schema.
Expected values are literals or come from the independent oracle in `us032_helpers`.
"""

from __future__ import annotations

import datetime as dt
import random
import threading

import pytest
import us001_helpers as base
import us030_helpers as H
import us032_helpers as V

from onelot.config import AppConfig

TOTALS = H.TOTALS_KEYS


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    base.block_network(monkeypatch)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    H.freeze_clock(monkeypatch, V.SUNDAY)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = base.new_db(tmp_path, "us032.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


@pytest.fixture
def app(app_cfg, db):
    return H.make_app(app_cfg, db)


def seed(app, rows):
    H.insert_trades(app.db.engine, rows)


def session_of(app) -> dict | None:
    return H.served(app)["last_session"]


def thursday() -> dt.date:
    return V.FRIDAY - dt.timedelta(days=1)


# ══ the shape: one field added, nothing taken away ════════════════════════════════════
class TestTheShape:
    def test_last_session_is_added_and_every_us030_field_is_still_served(self, app):
        seed(app, H.one_row_of_every_status(H.ORDER_BASE))
        body = H.served(app)
        assert {"enabled", "status", "trades", "totals_today", "last_session"} <= set(body)
        assert set(body["status"]) == H.STATUS_KEYS
        assert set(body["totals_today"]) == TOTALS
        assert all(H.TRADE_KEYS <= set(t) for t in body["trades"])

    def test_a_session_has_exactly_a_date_and_totals_in_the_shape_of_totals_today(self, app):
        seed(app, V.session_rows(V.FRIDAY, [100, -50]))
        session = session_of(app)
        assert set(session) == V.LAST_SESSION_KEYS
        assert set(session["totals"]) == TOTALS
        assert session["date"] == "2026-10-30" and isinstance(session["date"], str)

    def test_an_empty_book_has_no_last_session(self, app):
        body = H.served(app)
        assert body["last_session"] is None and body["trades"] == []

    def test_totals_today_is_exactly_what_it_was_with_or_without_a_last_session(self, app):
        today_rows = [H.closed_row("T1", V.SUNDAY - V.HOUR, 1505), H.closed_row("T2", V.SUNDAY, -5)]
        seed(app, today_rows)
        alone = H.served(app)["totals_today"]
        seed(app, V.session_rows(V.FRIDAY, [100, 200, 300]))
        both = H.served(app)["totals_today"]
        assert (
            alone
            == both
            == {
                "closed": 2,
                "wins": 1,
                "losses": 1,
                "gross_rupees": "15.00",
                "net_rupees": None,
            }
        )

    def test_paper_off_has_no_last_session_and_no_trades(self, app_cfg, db):
        off = H.make_app(app_cfg, db, enabled=False)
        seed(off, V.session_rows(V.FRIDAY, [100]))
        body = H.served(off)
        assert body["enabled"] is False and body["trades"] == []
        assert body["last_session"] is None

    @pytest.mark.parametrize(
        "kw", [{"book": False}, {"desk": False}], ids=["desk-without-a-book", "no-desk"]
    )
    def test_without_a_book_the_endpoint_still_answers(self, app_cfg, db, kw):
        resp = H.get_paper(H.make_app(app_cfg, db, **kw))
        assert resp.status_code in (200, 503), resp.text[:200]
        if resp.status_code == 200:
            assert resp.json()["enabled"] is False and resp.json()["last_session"] is None


# ══ the owner's Sunday ════════════════════════════════════════════════════════════════
class TestTheOwnersSunday:
    @pytest.fixture
    def sunday(self, app):
        rows, grosses = V.owners_sunday()
        seed(app, rows)
        return H.served(app), grosses

    def test_nothing_closed_today_so_totals_today_is_empty(self, sunday):
        body, _ = sunday
        assert body["totals_today"] == {
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "gross_rupees": "0.00",
            "net_rupees": None,
        }

    def test_the_last_session_is_fridays_date(self, sunday):
        body, _ = sunday
        assert body["last_session"]["date"] == "2026-10-30"

    def test_its_totals_are_the_67_closes_exact_to_the_paisa(self, sunday):
        body, grosses = sunday
        assert body["last_session"]["totals"] == V.totals_of(grosses)
        assert body["last_session"]["totals"]["closed"] == 67

    def test_the_sunday_no_fill_is_a_trade_of_today_not_a_close_of_the_session(self, sunday):
        body, _ = sunday
        trades = H.trades_by_alert(body)
        assert H.parse_ts(trades["SUN-NOFILL"]["closed_at"]).date() == V.SUNDAY.date()
        assert body["last_session"]["totals"]["closed"] == 67, "a NO_FILL is never a close"

    def test_every_trade_is_still_listed_newest_first_for_the_panel(self, sunday):
        body, _ = sunday
        got = [t["alert_id"] for t in body["trades"]]
        assert len(got) == 69
        created = [H.parse_ts(t["created_at"]) for t in body["trades"]]
        assert created == sorted(created, reverse=True)


# ══ totals are summed from integer paise ══════════════════════════════════════════════
BIG = 9007199254740993  # 2**53 + 1 paise: a float cannot hold it
PAISE_CASES = [
    pytest.param([BIG, 1], None, "90071992547409.94", id="beyond-a-float"),
    pytest.param([-BIG, -1], None, "-90071992547409.94", id="beyond-a-float-negative"),
    pytest.param([10] * 100, None, "10.00", id="a-hundred-ten-paise-trades"),
    pytest.param([1, 1, 1], None, "0.03", id="three-paise"),
    pytest.param([-5, -5, 3], None, "-0.07", id="small-negative-sum"),
    pytest.param([100030, -1205], None, "988.25", id="the-us030-worked-example"),
    pytest.param([0, 0], None, "0.00", id="breakeven"),
    pytest.param([99, 1], None, "1.00", id="carries-into-a-rupee"),
]


class TestTotalsFromIntegerPaise:
    @pytest.mark.parametrize("grosses, nets, gross", PAISE_CASES)
    def test_the_gross_sum_is_exact(self, app, grosses, nets, gross):
        seed(app, V.session_rows(V.FRIDAY, grosses))
        totals = session_of(app)["totals"]
        assert totals["gross_rupees"] == gross
        assert isinstance(totals["gross_rupees"], str)
        assert totals == V.totals_of(grosses)

    def test_a_net_on_every_close_gives_the_exact_net_sum_and_decides_wins_and_losses(self, app):
        grosses, nets = [1505, -1205, 40], [-30, -1300, 25]
        seed(app, V.session_rows(V.FRIDAY, grosses, nets=nets))
        totals = session_of(app)["totals"]
        assert totals == {
            "closed": 3,
            "wins": 1,
            "losses": 2,
            "gross_rupees": "3.40",
            "net_rupees": "-13.05",
        }

    def test_one_close_without_a_net_makes_the_net_sum_unknown(self, app):
        seed(app, V.session_rows(V.FRIDAY, [100, 200], nets=[90, None]))
        assert session_of(app)["totals"]["net_rupees"] is None

    def test_breakeven_closes_are_neither_wins_nor_losses(self, app):
        seed(app, V.session_rows(V.FRIDAY, [0, 0, 7]))
        totals = session_of(app)["totals"]
        assert (totals["closed"], totals["wins"], totals["losses"]) == (3, 1, 0)

    def test_a_corrupt_gross_makes_the_sessions_gross_unknown_never_a_wrong_sum(self, app):
        seed(app, V.session_rows(V.FRIDAY, [1000, 1000]))
        with app.db.engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE paper_trades SET gross_paise = 12.5 WHERE alert_id = 'FRI001'"
            )
        totals = session_of(app)["totals"]
        assert totals["gross_rupees"] is None, f"a partial or truncated sum was served: {totals}"

    def test_the_totals_count_the_sessions_own_closes_beyond_the_200_trade_list(self, app):
        grosses = [(-1) ** i * (100 + i) for i in range(250)]
        seed(app, V.session_rows(V.FRIDAY, grosses, step=1))
        body = H.served(app)
        assert len(body["trades"]) == H.MAX_TRADES
        assert body["last_session"]["totals"] == V.totals_of(grosses), "totals use every close"


# ══ which date is the last session ════════════════════════════════════════════════════
class TestWhichSessionIsLast:
    def test_the_most_recent_earlier_date_with_a_close_wins(self, app):
        seed(
            app, [*V.session_rows(thursday(), [100], prefix="THU"), *V.session_rows(V.FRIDAY, [7])]
        )
        session = session_of(app)
        assert session["date"] == "2026-10-30" and session["totals"]["gross_rupees"] == "0.07"

    def test_todays_closes_never_make_today_the_last_session(self, app):
        seed(app, [H.closed_row("T1", V.SUNDAY - V.HOUR, 100), *V.session_rows(V.FRIDAY, [7])])
        assert session_of(app)["date"] == "2026-10-30"

    def test_a_book_that_closed_only_today_has_no_last_session(self, app):
        seed(app, [H.closed_row("T1", V.SUNDAY - V.HOUR, 100)])
        assert session_of(app) is None

    def test_a_date_with_only_no_fills_never_beats_an_earlier_date_with_a_close(self, app):
        seed(
            app,
            [
                *V.session_rows(thursday(), [100], prefix="THU"),
                V.no_fill_row("FRI-NOFILL", V.at_ist(V.FRIDAY, 10, 11)),
            ],
        )
        session = session_of(app)
        assert session["date"] == "2026-10-29" and session["totals"]["closed"] == 1

    def test_with_no_close_before_today_the_latest_no_fill_date_is_the_session(self, app):
        seed(
            app,
            [
                V.no_fill_row("THU-NOFILL", V.at_ist(thursday(), 10, 11)),
                V.no_fill_row("FRI-NOFILL", V.at_ist(V.FRIDAY, 10, 11)),
                V.no_fill_row("FRI-NOFILL2", V.at_ist(V.FRIDAY, 14, 2)),
            ],
        )
        assert session_of(app) == {
            "date": "2026-10-30",
            "totals": {
                "closed": 0,
                "wins": 0,
                "losses": 0,
                "gross_rupees": "0.00",
                "net_rupees": None,
            },
        }

    def test_a_close_only_today_leaves_an_earlier_no_fill_as_the_session(self, app):
        seed(
            app,
            [
                H.closed_row("T1", V.SUNDAY - V.HOUR, 100),
                V.no_fill_row("THU-NOFILL", V.at_ist(thursday(), 10, 11)),
            ],
        )
        assert session_of(app)["date"] == "2026-10-29"

    def test_open_and_waiting_trades_never_make_a_session(self, app):
        waiting = H.trade_values(
            "FRI-WAIT", status="PENDING_FILL", created=V.at_ist(V.FRIDAY, 15, 0)
        )
        seed(
            app, [*V.session_rows(thursday(), [100], prefix="THU"), V.friday_open_trade(), waiting]
        )
        assert session_of(app)["date"] == "2026-10-29"

    def test_the_day_a_trade_closed_decides_not_the_day_its_alert_was_raised(self, app):
        late_close = H.closed_row(
            "OVERNIGHT",
            V.at_ist(V.FRIDAY, 9, 20),
            300,
            created=V.at_ist(thursday(), 15, 20),  # raised Thursday, closed Friday
        )
        seed(app, [*V.session_rows(thursday(), [100], prefix="THU"), late_close])
        session = session_of(app)
        assert session["date"] == "2026-10-30"
        assert session["totals"]["closed"] == 1 and session["totals"]["gross_rupees"] == "3.00"

    def test_a_no_fill_is_dated_by_the_time_the_book_gave_up(self, app):
        no_fill = V.sunday_no_fill()  # raised Friday, booked on the Sunday
        seed(app, [*V.session_rows(thursday(), [100], prefix="THU"), no_fill])
        assert session_of(app)["date"] == "2026-10-29", "a Sunday NO_FILL is today's, not Friday's"

    def test_an_unreadable_close_time_does_not_hide_the_session(self, app):
        seed(app, V.session_rows(V.FRIDAY, [100, 200]))
        with app.db.engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE paper_trades SET exit_ts = 'garbage' WHERE alert_id = 'FRI001'"
            )
        resp = H.get_paper(app)
        assert resp.status_code == 200, resp.text[:200]
        assert resp.json()["last_session"]["date"] == "2026-10-30"


# ══ the owner's Monday: a NO_FILL stamped on the Sunday must not become the session ═══
class TestTheOwnersMonday:
    """Friday's closes are the evidence. A start-up reconcile on the Sunday booked a NO_FILL
    (closed_at = updated_at = Sunday): that must not turn Sunday into 'the last session'."""

    @pytest.fixture(autouse=True)
    def _monday(self, monkeypatch):
        H.freeze_clock(monkeypatch, V.MONDAY_0920)

    @pytest.fixture
    def monday(self, app):
        rows, friday, thursday = V.owners_monday()
        seed(app, rows)
        return H.served(app), friday, thursday

    def test_the_last_session_is_still_friday(self, monday):
        body, _, _ = monday
        assert body["last_session"]["date"] == "2026-10-30"

    def test_with_fridays_exact_totals_not_zero_and_not_thursdays(self, monday):
        body, friday, thursday = monday
        assert body["last_session"]["totals"] == V.totals_of(friday)
        assert body["last_session"]["totals"]["closed"] == 67
        assert body["last_session"]["totals"] != V.totals_of(thursday)

    def test_nothing_closed_today_and_the_sunday_no_fill_is_not_one_of_todays(self, monday):
        body, _, _ = monday
        assert (
            body["totals_today"]["closed"] == 0 and body["totals_today"]["gross_rupees"] == "0.00"
        )
        sunday = H.trades_by_alert(body)["SUN-NOFILL"]
        assert H.parse_ts(sunday["closed_at"]).date() == V.SUNDAY.date()

    def test_every_trade_is_still_in_the_list_for_the_all_view(self, monday):
        body, _, _ = monday
        assert len(body["trades"]) == 20 + 67 + 3

    @pytest.mark.parametrize(
        "booked",
        [
            "2026-10-31T00:00:00+05:30",
            "2026-10-31T23:59:59+05:30",
            "2026-11-01T12:00:00+05:30",
            "2026-11-01T23:59:59+05:30",
        ],
        ids=["saturday-first-second", "saturday-last-second", "sunday-noon", "sunday-last-second"],
    )
    def test_a_no_fill_booked_any_time_after_friday_and_before_today_changes_nothing(
        self, app, booked
    ):
        rows, friday, _ = V.owners_monday()
        seed(app, [r for r in rows if r["alert_id"] != "SUN-NOFILL"])
        seed(app, [V.no_fill_row("LATE-NOFILL", dt.datetime.fromisoformat(booked))])
        session = session_of(app)
        assert session["date"] == "2026-10-30"
        assert session["totals"] == V.totals_of(friday)

    def test_several_no_fills_over_the_weekend_change_nothing(self, app):
        rows, friday, _ = V.owners_monday()
        seed(app, rows)
        seed(
            app,
            [
                V.no_fill_row(f"WE{i}", V.at_ist(day, 8 + i, 5))
                for i, day in enumerate([dt.date(2026, 10, 31), dt.date(2026, 11, 1)] * 3)
            ],
        )
        assert session_of(app)["totals"] == V.totals_of(friday)

    def test_a_no_fill_booked_this_morning_is_todays_not_the_sessions(self, app):
        rows, friday, _ = V.owners_monday()
        seed(app, rows)
        seed(app, [V.no_fill_row("MON-NOFILL", V.MONDAY_0920 - 10 * V.MIN)])
        body = H.served(app)
        assert body["last_session"]["date"] == "2026-10-30"
        assert body["totals_today"]["closed"] == 0, "a NO_FILL is never a close"

    def test_a_no_fill_that_ended_on_fridays_own_date_belongs_to_fridays_session(self, monday):
        body, friday, _ = monday
        trade = H.trades_by_alert(body)["FRI-NOFILL"]
        assert H.parse_ts(trade["closed_at"]).date() == V.FRIDAY
        assert body["last_session"]["totals"]["closed"] == len(friday), "but it is not a close"


# ══ a model of the rule, against random books ═════════════════════════════════════════
TIMES_OF_DAY = [
    (0, 0, 0),
    (0, 0, 1),
    (9, 15, 0),
    (12, 0, 0),
    (15, 29, 59),
    (23, 59, 58),
    (23, 59, 59),
]


def random_book(seed_: int, today: dt.date):
    """(rows, expected last_session): CLOSED, NO_FILL and OPEN trades over the ten days up to
    and including `today`, at awkward times of day. The oracle is the Manager's rule: the latest
    IST date before today with a CLOSED close; failing that, the latest with a NO_FILL."""
    rng = random.Random(seed_)
    rows, closes, no_fills = [], [], []
    for i in range(rng.randrange(0, 30)):
        day = today - dt.timedelta(days=rng.randrange(0, 10))
        hh, mm, ss = rng.choice(TIMES_OF_DAY)
        when = V.at_ist(day, hh, mm, ss)
        kind = rng.choice(["closed", "closed", "no_fill", "open"])
        if kind == "closed":
            gross = rng.randrange(-5000, 5000)
            rows.append(H.closed_row(f"C{i:02d}", when, gross, instrument=f"C{i:02d}"))
            closes.append((day, gross))
        elif kind == "no_fill":
            rows.append(V.no_fill_row(f"N{i:02d}", when))
            no_fills.append(day)
        else:
            rows.append(H.trade_values(f"O{i:02d}", status="OPEN", created=when, entry=100030))
    before = [(d, g) for d, g in closes if d < today]
    if before:
        date = max(d for d, _ in before)
        grosses = [g for d, g in before if d == date]
    else:
        earlier = [d for d in no_fills if d < today]
        date, grosses = (max(earlier), []) if earlier else (None, [])
    expected = None if date is None else {"date": date.isoformat(), "totals": V.totals_of(grosses)}
    return rows, expected


class TestAgainstAModelOfTheRule:
    @pytest.mark.parametrize("seed_", range(25))
    def test_the_last_session_is_what_the_rule_says_for_a_random_book(
        self, app, monkeypatch, seed_
    ):
        today = dt.date(2026, 11, 2)
        rng = random.Random(1000 + seed_)
        now = V.at_ist(today, 0, 0, 30) + dt.timedelta(seconds=rng.randrange(0, 86000))
        H.freeze_clock(monkeypatch, now)
        rows, expected = random_book(seed_, today)
        if rows:
            seed(app, rows)
        got = session_of(app)
        assert got == expected, (seed_, now.isoformat(), got, expected)


# ══ the IST date edges, on a frozen clock ═════════════════════════════════════════════
# (now, [(alert, closed at, gross paise)], date of the last session or None, closes, gross)
EDGES = [
    pytest.param(
        "2026-11-02T00:00:30+05:30",  # the Monday, half a minute in
        [("FRI", "2026-10-30T15:00:00+05:30", 100)],
        "2026-10-30",
        1,
        "1.00",
        id="monday-before-the-first-close-looks-back-to-friday",
    ),
    pytest.param(
        "2026-11-02T00:00:30+05:30",
        [("FRI", "2026-10-30T15:00:00+05:30", 100), ("NOW", "2026-11-02T00:00:00+05:30", 900)],
        "2026-10-30",
        1,
        "1.00",
        id="a-close-at-exactly-midnight-is-today-not-the-last-session",
    ),
    pytest.param(
        "2026-11-01T23:59:59.500+05:30",
        [("YEST", "2026-10-31T23:59:59+05:30", 100), ("TODAY", "2026-11-01T00:00:00+05:30", 900)],
        "2026-10-31",
        1,
        "1.00",
        id="half-a-second-before-midnight",
    ),
    pytest.param(
        "2026-11-02T03:00:00+05:30",  # 21:30 on the 1st in UTC
        [("TODAY", "2026-11-02T00:10:00+05:30", 900), ("YEST", "2026-11-01T23:50:00+05:30", 100)],
        "2026-11-01",
        1,
        "1.00",
        id="utc-date-trap",
    ),
    pytest.param(
        "2027-01-01T00:00:30+05:30",
        [("OLD-YEAR", "2026-12-31T23:59:59+05:30", 100), ("NEW", "2027-01-01T00:00:00+05:30", 900)],
        "2026-12-31",
        1,
        "1.00",
        id="new-year",
    ),
    pytest.param(
        "2028-03-01T09:00:00+05:30",
        [("LEAP", "2028-02-29T15:00:00+05:30", 70), ("FEB28", "2028-02-28T15:00:00+05:30", 900)],
        "2028-02-29",
        1,
        "0.70",
        id="leap-day",
    ),
    pytest.param(
        "2026-11-01T12:00:00+05:30",
        [
            ("FRI-LATE", "2026-10-30T23:59:59+05:30", 100),
            ("SAT-FIRST", "2026-10-31T00:00:00+05:30", 200),
        ],
        "2026-10-31",
        1,
        "2.00",
        id="the-last-second-of-friday-and-the-first-of-saturday-are-different-sessions",
    ),
]


class TestIstDateEdges:
    @pytest.mark.parametrize("now, closes, date, closed, gross", EDGES)
    def test_the_last_session_is_the_ist_date_before_the_ist_date_of_now(
        self, app, monkeypatch, now, closes, date, closed, gross
    ):
        H.freeze_clock(monkeypatch, dt.datetime.fromisoformat(now))
        seed(
            app,
            [
                H.closed_row(alert, dt.datetime.fromisoformat(when), paise, instrument=alert)
                for alert, when, paise in closes
            ],
        )
        session = session_of(app)
        assert session is not None and session["date"] == date, session
        assert session["totals"]["closed"] == closed
        assert session["totals"]["gross_rupees"] == gross

    def test_nothing_before_today_means_null_whatever_the_hour(self, app, monkeypatch):
        seed(
            app, [H.closed_row("NOW", dt.datetime.fromisoformat("2026-11-02T00:00:00+05:30"), 900)]
        )
        for now in ("2026-11-02T00:00:01+05:30", "2026-11-02T23:59:59+05:30"):
            H.freeze_clock(monkeypatch, dt.datetime.fromisoformat(now))
            assert session_of(app) is None, now

    def test_the_session_moves_forward_when_the_clock_crosses_midnight(self, app, monkeypatch):
        seed(
            app, [H.closed_row("SUN", dt.datetime.fromisoformat("2026-11-01T10:00:00+05:30"), 100)]
        )
        H.freeze_clock(monkeypatch, dt.datetime.fromisoformat("2026-11-01T23:59:59+05:30"))
        assert session_of(app) is None
        H.freeze_clock(monkeypatch, dt.datetime.fromisoformat("2026-11-02T00:00:01+05:30"))
        assert session_of(app)["date"] == "2026-11-01"

    def test_the_listing_does_not_depend_on_the_clock(self, app, monkeypatch):
        seed(app, V.session_rows(V.FRIDAY, [100, 200]))
        before = H.served(app)["trades"]
        H.freeze_clock(monkeypatch, dt.datetime.fromisoformat("2026-11-03T00:00:01+05:30"))
        assert H.served(app)["trades"] == before


# ══ read-only, same auth, never waits ═════════════════════════════════════════════════


class TestStillAPlainRead:
    def test_a_get_changes_nothing_in_the_database(self, app):
        rows, _ = V.owners_sunday()
        seed(app, rows)
        before = base.snapshot_tables(app.db.engine)
        for _ in range(3):
            H.served(app)
        assert base.snapshot_tables(app.db.engine) == before

    @pytest.mark.parametrize(
        "headers, params",
        [({}, {}), ({"x-onelot-token": "wrong"}, {}), ({"x-onelot-token": H.TOKEN}, {})],
        ids=["no-token", "wrong-token", "right-token"],
    )
    def test_the_route_still_answers_like_the_other_desk_reads(self, app, headers, params):
        paper = app.client.get("/api/paper", headers=headers, params=params)
        desk = app.client.get("/api/desk", headers=headers, params=params)
        assert paper.status_code == desk.status_code

    @staticmethod
    def request_within(app, seconds: float):
        box: dict[str, object] = {}
        worker = threading.Thread(target=lambda: box.update(resp=H.get_paper(app)), daemon=True)
        worker.start()
        worker.join(seconds)
        return None if worker.is_alive() else box["resp"]

    def test_it_answers_while_another_thread_holds_the_desk_lock(self, app):
        rows, _ = V.owners_sunday()
        seed(app, rows)
        held, release = threading.Event(), threading.Event()

        def hold():
            with app.desk._lock:
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
        assert resp.status_code == 200 and resp.json()["last_session"]["date"] == "2026-10-30"

    def test_it_answers_while_another_connection_holds_the_database_write_lock(self, app, tmp_path):
        rows, _ = V.owners_sunday()
        seed(app, rows)
        with base.write_lock(tmp_path / "us032.db"):
            resp = self.request_within(app, 5.0)
        assert resp is not None, "a read waited behind a held SQLite write lock"
        assert resp.status_code == 200 and resp.json()["last_session"]["totals"]["closed"] == 67

    def test_a_book_of_a_few_thousand_trades_still_answers_promptly(self, app):
        rows = []
        for day in range(1, 26):  # 25 sessions of 200 closes
            rows += V.session_rows(
                dt.date(2026, 10, 1) + dt.timedelta(days=day - 1),
                [10] * 200,
                prefix=f"D{day:02d}",
                step=1,
            )
        seed(app, rows)
        started = dt.datetime.now()
        body = H.served(app)
        assert (dt.datetime.now() - started).total_seconds() < 5
        assert body["last_session"]["date"] == "2026-10-25"
        assert body["last_session"]["totals"]["closed"] == 200
        assert len(body["trades"]) == H.MAX_TRADES
