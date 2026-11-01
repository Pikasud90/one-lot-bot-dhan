"""US-032 AC1 and AC2 in the browser code: what the Paper panel shows, in each of its views.

AC1: with no paper trade closed today (IST) but closed or no-fill trades on an earlier date, the
panel shows the open trades plus the closed/no-fill trades of the most recent session, under a
visible heading such as 'Last session · Fri 30 Oct', and the day line totals (closed, won,
lost, gross, net) are for that session.
AC2: 'Today' keeps today's behaviour (and falls back to the last session, with its heading,
when today has none); 'Last session' always shows the latest session with closes; 'All' shows
every trade the API returned, newest first, at most 200.

The owner's Sunday is reproduced through the REAL endpoint (67 Friday closes, one NO_FILL booked
on the Sunday, an open trade from Friday) and rendered by the REAL paperPanel / paperMeta of
app.js, run in node over a frozen clock (us030_helpers' lexer). The returned HTML is parsed, not
string-matched. Contract names (Manager): paperPanel(payload, view), paperMeta(payload, view),
views 'today' | 'last' | 'all'. Skipped, with a reason, on a machine without node.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
import us001_helpers as base
import us030_helpers as H
import us032_helpers as V

from onelot.config import AppConfig

pytestmark = pytest.mark.skipif(
    H.NODE is None, reason="node is not installed; app.js helpers not exercised"
)

JUNK = re.compile(r"\b(undefined|null|NaN|None)\b")
HOSTILE = "<img src=x onerror=alert(1)>"
US030_UI_COMMIT = "7cf5235"  # the Sprint 1 integration just before US-032: the US-030 panel


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
    d = base.new_db(tmp_path, "us032-ui.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


def served_book(app_cfg, db, rows) -> dict:
    app = H.make_app(app_cfg, db)
    H.insert_trades(app.db.engine, rows)
    return H.served(app)


@pytest.fixture
def sunday(app_cfg, db):
    """(the real /api/paper payload of the owner's Sunday, the 67 Friday grosses)."""
    rows, grosses = V.owners_sunday()
    return served_book(app_cfg, db, rows), grosses


def closes(html: str) -> int:
    return V.chip_words(html).count("closed")


def amount(text: str) -> str:
    return H.money_text(text)


# ══ AC1 / AC2 'Today': the fallback ═══════════════════════════════════════════════════
class TestTodayFallsBackToTheLastSession:
    def test_the_heading_names_the_last_session_and_its_date(self, sunday):
        html = V.panel(sunday[0], "today")
        assert V.heading_nodes(html, "last session", "fri 30 oct"), V.text_of(html)[:300]

    def test_the_sessions_67_closes_are_on_the_panel(self, sunday):
        assert closes(V.panel(sunday[0], "today")) == 67

    def test_fridays_rows_come_in_the_apis_newest_first_order(self, sunday):
        names = V.instruments(V.panel(sunday[0], "today"))
        assert names[2:] == [f"FRI{i:03d}" for i in range(66, -1, -1)]

    def test_todays_open_and_no_fill_rows_stay_above_the_session(self, sunday):
        html = V.panel(sunday[0], "today")
        names = V.instruments(html)
        assert sorted(names[:2]) == ["HDFCBANK", "TCS"], names[:4]
        words = V.chip_words(html)
        assert sorted(words[:2]) == ["no fill", "open"], words[:4]

    def test_the_sessions_group_label_sits_between_todays_rows_and_the_session(self, sunday):
        html = V.panel(sunday[0], "today")
        text = V.text_of(html)
        assert text.rindex("hdfcbank") < text.rindex("last session") < text.index("fri066")

    def test_the_day_line_is_the_sessions_totals_not_an_empty_today(self, sunday):
        body, grosses = sunday
        expected = V.totals_of(grosses)
        line = V.meta(body, "today")
        assert f"{expected['closed']} closed" in line
        assert f"{expected['wins']} won" in line and f"{expected['losses']} lost" in line
        assert expected["gross_rupees"] in amount(line), line
        assert "fri 30 oct" in line.lower()
        assert not JUNK.search(line), line

    def test_an_unknown_net_is_a_dash_never_a_number(self, sunday):
        line = V.meta(sunday[0], "today")
        assert re.search(r"net\s*[–-]", line), line

    def test_nothing_closed_today_is_not_called_a_loss_or_a_zero_day(self, sunday):
        line = V.meta(sunday[0], "today")
        assert "0 closed" not in line, line

    def test_a_session_of_only_no_fills_still_gets_its_heading_and_rows(self, app_cfg, db):
        no_fill = H.trade_values(
            "FRI-NOFILL",
            status="NO_FILL",
            close_reason="NO_FILL",
            instrument="INFY",
            gross=0,
            costs=0,
            net=0,
            created=V.at_ist(V.FRIDAY, 10, 0),
        )
        no_fill["updated_at"] = V.at_ist(V.FRIDAY, 10, 11)
        body = served_book(app_cfg, db, [no_fill])
        assert body["last_session"]["totals"]["closed"] == 0
        html = V.panel(body, "today")
        assert V.heading_nodes(html, "last session", "fri 30 oct")
        assert V.instruments(html) == ["INFY"]
        assert "0 closed" in V.meta(body, "today")


class TestTodayKeepsTodaysBehaviourWhenSomethingClosed:
    @pytest.fixture
    def with_a_close_today(self, app_cfg, db):
        rows, grosses = V.owners_sunday()
        today = H.closed_row("SUN-WIN", V.SUNDAY - V.HOUR, 1505, instrument="WIPRO")
        return served_book(app_cfg, db, [*rows, today]), grosses

    def test_no_session_heading_and_only_todays_rows_and_the_open_trade(self, with_a_close_today):
        html = V.panel(with_a_close_today[0], "today")
        assert "last session" not in V.text_of(html)
        assert sorted(V.instruments(html)) == ["HDFCBANK", "TCS", "WIPRO"]

    def test_the_day_line_is_todays_totals(self, with_a_close_today):
        line = V.meta(with_a_close_today[0], "today")
        assert line.lower().startswith("today"), line
        assert "1 closed" in line and "1 won" in line and amount(line).count("15.05") == 1
        assert "last session" not in line.lower()


# ══ AC2 'Last session' ════════════════════════════════════════════════════════════════
class TestTheLastSessionView:
    def test_the_heading_and_exactly_the_sessions_closes(self, sunday):
        html = V.panel(sunday[0], "last")
        assert V.heading_nodes(html, "last session", "fri 30 oct")
        assert V.instruments(html) == [f"FRI{i:03d}" for i in range(66, -1, -1)]

    def test_nothing_of_another_day_is_listed_under_the_sessions_heading(self, sunday):
        html = V.panel(sunday[0], "last")
        text = V.text_of(html)
        assert "hdfcbank" not in text, "a Sunday NO_FILL under Friday's heading"
        assert set(V.chip_words(html)) == {"closed"}

    def test_the_day_line_is_the_sessions_totals(self, sunday):
        body, grosses = sunday
        expected = V.totals_of(grosses)
        line = V.meta(body, "last")
        assert f"{expected['closed']} closed" in line and expected["gross_rupees"] in amount(line)
        assert f"{expected['wins']} won" in line and f"{expected['losses']} lost" in line

    def test_it_shows_the_last_session_even_when_today_has_closes(self, app_cfg, db):
        rows, _ = V.owners_sunday()
        today = H.closed_row("SUN-WIN", V.SUNDAY - V.HOUR, 1505, instrument="WIPRO")
        body = served_book(app_cfg, db, [*rows, today])
        html = V.panel(body, "last")
        assert V.heading_nodes(html, "last session", "fri 30 oct")
        assert "wipro" not in V.text_of(html) and closes(html) == 67

    def test_with_no_earlier_session_it_says_so_and_shows_no_table(self, app_cfg, db):
        body = served_book(app_cfg, db, [H.closed_row("T1", V.SUNDAY - V.HOUR, 100)])
        assert body["last_session"] is None
        html = V.panel(body, "last")
        assert "<table" not in html and "earlier session" in V.text_of(html)

    def test_the_view_is_one_session_even_when_the_book_holds_many(self, app_cfg, db):
        thursday = V.FRIDAY - dt.timedelta(days=1)
        rows = [*V.session_rows(thursday, [1, 2, 3], prefix="THU"), *V.session_rows(V.FRIDAY, [9])]
        html = V.panel(served_book(app_cfg, db, rows), "last")
        assert V.instruments(html) == ["FRI000"]


# ══ AC2 'All' ═════════════════════════════════════════════════════════════════════════
class TestTheAllView:
    def test_every_trade_the_api_returned_in_its_order(self, sunday):
        body, _ = sunday
        html = V.panel(body, "all")
        assert V.instruments(html) == [t["instrument"] for t in body["trades"]]
        assert len(V.instruments(html)) == 69

    def test_all_shows_no_session_heading_and_no_group_row(self, sunday):
        assert "last session" not in V.text_of(V.panel(sunday[0], "all"))

    def test_all_includes_what_today_and_last_leave_out(self, sunday):
        names = V.instruments(V.panel(sunday[0], "all"))
        assert {"HDFCBANK", "TCS", "FRI000", "FRI066"} <= set(names)

    def test_all_is_capped_at_what_the_api_returned_not_more(self, app_cfg, db):
        body = served_book(app_cfg, db, V.session_rows(V.FRIDAY, [10] * 230, step=1))
        assert len(body["trades"]) == H.MAX_TRADES
        assert len(V.instruments(V.panel(body, "all"))) == H.MAX_TRADES

    def test_the_day_line_in_all_is_present_and_clean(self, sunday):
        line = V.meta(sunday[0], "all")
        assert line and not JUNK.search(line), line

    def test_an_empty_book_is_no_paper_trades_yet_in_every_view(self, app_cfg, db):
        body = served_book(app_cfg, db, [H.trade_values("X", status="PENDING_FILL")])
        body["trades"] = []
        for view in ("today", "last", "all"):
            assert "no paper trades yet" in V.text_of(V.panel(body, view)), view


# ══ the owner's Monday: a NO_FILL stamped on the Sunday must not hide Friday ═══════════
class TestTheOwnersMonday:
    """Friday's closes are the evidence on Monday morning. The NO_FILL a start-up reconcile
    stamped on the Sunday is a trade of Sunday, not a reason to show an empty 'last session'."""

    @pytest.fixture(autouse=True)
    def _monday(self, monkeypatch):
        H.freeze_clock(monkeypatch, V.MONDAY_0920)

    @pytest.fixture
    def monday(self, app_cfg, db):
        rows, friday, thursday = V.owners_monday()
        return served_book(app_cfg, db, rows), friday, thursday

    @pytest.mark.parametrize("view", ["today", "last"])
    def test_the_heading_is_friday_never_sunday(self, monday, view):
        html = V.panel(monday[0], view)
        assert V.heading_nodes(html, "last session", "fri 30 oct"), V.text_of(html)[:300]
        assert not V.heading_nodes(html, "last session", "sun 1 nov")

    @pytest.mark.parametrize("view", ["today", "last"])
    def test_fridays_67_closes_and_its_own_no_fill_are_listed(self, monday, view):
        html = V.panel(monday[0], view)
        names = V.instruments(html)
        assert closes(html) == 67
        assert V.chip_words(html).count("no fill") == 1 and "INFY" in names
        assert not any(n.startswith("THU") for n in names), "Thursday is not the last session"

    @pytest.mark.parametrize("view", ["today", "last"])
    def test_the_sunday_stamped_no_fill_is_not_shown_under_fridays_heading(self, monday, view):
        names = V.instruments(V.panel(monday[0], view))
        assert "HDFCBANK" not in names

    @pytest.mark.parametrize("view", ["today", "last"])
    def test_the_day_line_is_fridays_exact_totals_not_an_empty_sunday(self, monday, view):
        body, friday, _ = monday
        expected = V.totals_of(friday)
        line = V.meta(body, view)
        assert f"{expected['closed']} closed" in line, line
        assert f"{expected['wins']} won" in line and f"{expected['losses']} lost" in line
        assert expected["gross_rupees"] in amount(line), line
        assert "fri 30 oct" in line.lower() and "sun 1 nov" not in line.lower()

    def test_all_still_shows_every_trade_including_the_sunday_one(self, monday):
        body, _, _ = monday
        names = V.instruments(V.panel(body, "all"))
        assert len(names) == len(body["trades"]) == 90 and "HDFCBANK" in names

    def test_todays_open_trade_stays_above_the_session_in_the_fallback(self, monday):
        names = V.instruments(V.panel(monday[0], "today"))
        assert names[0] == "TCS", names[:3]


# ══ a book of NO_FILLs only ═══════════════════════════════════════════════════════════
class TestANoFillOnlyHistory:
    """No CLOSED trade anywhere before today: the latest date with a NO_FILL is the session."""

    @pytest.fixture
    def no_fills(self, app_cfg, db):
        thursday = V.FRIDAY - dt.timedelta(days=1)
        rows = [
            V.no_fill_row("THU-A", V.at_ist(thursday, 10, 11), instrument="WIPRO"),
            V.no_fill_row("THU-B", V.at_ist(thursday, 11, 11), instrument="WIPRO"),
            V.no_fill_row("FRI-A", V.at_ist(V.FRIDAY, 10, 11), instrument="INFY"),
            V.no_fill_row("FRI-B", V.at_ist(V.FRIDAY, 11, 11), instrument="TCS"),
            V.no_fill_row("FRI-C", V.at_ist(V.FRIDAY, 12, 11), instrument="SBIN"),
        ]
        return served_book(app_cfg, db, rows)

    def test_the_endpoint_names_friday_with_no_closes(self, no_fills):
        assert no_fills["last_session"]["date"] == "2026-10-30"
        assert no_fills["last_session"]["totals"]["closed"] == 0

    @pytest.mark.parametrize("view", ["today", "last"])
    def test_the_panel_lists_fridays_no_fills_under_its_heading(self, no_fills, view):
        html = V.panel(no_fills, view)
        assert V.heading_nodes(html, "last session", "fri 30 oct")
        assert sorted(V.instruments(html)) == ["INFY", "SBIN", "TCS"]
        assert set(V.chip_words(html)) == {"no fill"}

    def test_the_day_line_says_no_closes_and_zero_money_without_junk(self, no_fills):
        for view in ("today", "last"):
            line = V.meta(no_fills, view)
            assert "0 closed" in line and "0.00" in amount(line), line
            assert not JUNK.search(line), line

    def test_all_lists_all_five(self, no_fills):
        assert len(V.instruments(V.panel(no_fills, "all"))) == 5

    def test_a_close_today_keeps_todays_view_and_the_no_fill_session_stays_behind_it(
        self, app_cfg, db
    ):
        thursday = V.FRIDAY - dt.timedelta(days=1)
        rows = [
            V.no_fill_row("THU-A", V.at_ist(thursday, 10, 11), instrument="WIPRO"),
            H.closed_row("SUN-WIN", V.SUNDAY - V.HOUR, 1505, instrument="TCS"),
        ]
        body = served_book(app_cfg, db, rows)
        today = V.panel(body, "today")
        assert "last session" not in V.text_of(today) and V.instruments(today) == ["TCS"]
        assert V.instruments(V.panel(body, "last")) == ["WIPRO"]


# ══ a session the 200-trade list does not hold ════════════════════════════════════════
class TestASessionTheListDoesNotHold:
    """Two hundred and five open trades fill the whole list (managed trades come first):
    Friday's 67 closes exist on the server but none of them is in the list."""

    @pytest.fixture
    def pushed_out(self, app_cfg, db):
        saturday = V.FRIDAY + dt.timedelta(days=1)
        opens = [
            H.trade_values(
                f"OP{i:03d}",
                status="OPEN",
                instrument=f"OP{i:03d}",
                created=V.at_ist(saturday, 10, 0) + i * V.MIN,
                entry=100030,
            )
            for i in range(205)
        ]
        return served_book(app_cfg, db, [*V.session_rows(V.FRIDAY, V.friday_grosses()), *opens])

    def test_the_server_still_counts_the_67_closes_the_list_lacks(self, pushed_out):
        assert pushed_out["last_session"]["totals"]["closed"] == 67
        assert len(pushed_out["trades"]) == H.MAX_TRADES
        assert all(t["status"] == "OPEN" for t in pushed_out["trades"])

    @pytest.mark.parametrize("view", ["today", "last"])
    def test_the_panel_says_none_of_the_sessions_closes_are_listed(self, pushed_out, view):
        text = V.text_of(V.panel(pushed_out, view))
        assert re.search(r"0 of 67 closes listed|none of its", text), text[:300]

    def test_with_nothing_else_to_show_the_fallback_is_a_heading_and_the_reason(self, app_cfg, db):
        saturday = V.FRIDAY + dt.timedelta(days=1)
        rows = V.session_rows(V.FRIDAY, V.friday_grosses()) + [
            V.no_fill_row(f"NF{i:03d}", V.at_ist(saturday, 10, 0) + i * V.MIN) for i in range(205)
        ]
        body = served_book(app_cfg, db, rows)
        assert body["last_session"]["date"] == "2026-10-30", "NO_FILLs must not outrank closes"
        html = V.panel(body, "today")
        text = V.text_of(html)
        assert V.heading_nodes(html, "last session", "fri 30 oct")
        assert "<table" not in html and re.search(r"0 of 67 closes listed|none of its", text), text

    def test_the_last_session_view_has_no_table_to_show_just_the_reason(self, pushed_out):
        html = V.panel(pushed_out, "last")
        assert "<table" not in html and V.heading_nodes(html, "last session", "fri 30 oct")

    @pytest.mark.parametrize(
        "scenario",
        ["sunday", "monday", "no-fills", "pushed-out", "cut", "pushed-out-by-no-fills"],
        ids=lambda s: s,
    )
    @pytest.mark.parametrize("view", ["today", "last", "all"])
    def test_a_table_is_never_empty(self, app_cfg, db, monkeypatch, scenario, view):
        if scenario == "sunday":
            rows = V.owners_sunday()[0]
        elif scenario == "monday":
            H.freeze_clock(monkeypatch, V.MONDAY_0920)
            rows = V.owners_monday()[0]
        elif scenario == "no-fills":
            rows = [V.no_fill_row("F", V.at_ist(V.FRIDAY, 10, 0))]
        elif scenario == "cut":
            rows = V.session_rows(V.FRIDAY, [10] * 250, step=1)
        elif scenario == "pushed-out-by-no-fills":
            # 205 NO_FILLs booked on the Saturday fill the list: Friday's closes are not in it,
            # nothing is open and nothing closed today
            saturday = V.FRIDAY + dt.timedelta(days=1)
            rows = V.session_rows(V.FRIDAY, V.friday_grosses()) + [
                V.no_fill_row(f"NF{i:03d}", V.at_ist(saturday, 10, 0) + i * V.MIN)
                for i in range(205)
            ]
        else:
            saturday = V.FRIDAY + dt.timedelta(days=1)
            rows = V.session_rows(V.FRIDAY, V.friday_grosses()) + [
                H.trade_values(
                    f"OP{i:03d}",
                    status="OPEN",
                    created=V.at_ist(saturday, 10, 0) + i * V.MIN,
                    entry=100030,
                )
                for i in range(205)
            ]
        html = V.panel(served_book(app_cfg, db, rows), view)
        assert "<table" not in html or V.chip_words(html), (
            f"{scenario}/{view}: a table of no trades"
        )


# ══ the 200 cap cutting a session ═════════════════════════════════════════════════════
class TestTheCapCutsASession:
    @pytest.fixture
    def cut(self, app_cfg, db):
        grosses = [(-1) ** i * (100 + 3 * i) for i in range(250)]
        return served_book(app_cfg, db, V.session_rows(V.FRIDAY, grosses, step=1)), grosses

    def test_the_server_counted_all_250_closes_while_the_list_holds_200(self, cut):
        body, _ = cut
        assert len(body["trades"]) == H.MAX_TRADES
        assert body["last_session"]["totals"]["closed"] == 250

    def test_the_last_session_view_says_how_many_closes_are_listed(self, cut):
        html = V.panel(cut[0], "last")
        assert re.search(r"200 of 250 closes listed", V.text_of(html)), V.text_of(html)[:400]
        assert len(V.instruments(html)) == 200

    def test_the_day_line_still_carries_all_250_closes_and_their_exact_gross(self, cut):
        body, grosses = cut
        expected = V.totals_of(grosses)
        for view in ("last", "today"):
            line = V.meta(body, view)
            assert "250 closed" in line, (view, line)
            assert expected["gross_rupees"] in amount(line), (view, line)

    def test_the_today_fallback_says_it_lists_only_part_of_the_session(self, cut):
        html = V.panel(cut[0], "today")
        assert re.search(r"200 of 250", V.text_of(html)), (
            "the Today fallback shows 200 of the session's 250 closes under totals for all 250 "
            f"without saying so: {V.text_of(html)[:300]}"
        )

    @pytest.mark.parametrize("n", [1, 67, 200])
    def test_a_session_that_fits_in_the_list_makes_no_such_claim(self, app_cfg, db, n):
        body = served_book(app_cfg, db, V.session_rows(V.FRIDAY, [10] * n, step=1))
        for view in ("last", "today"):
            assert "closes listed" not in V.text_of(V.panel(body, view)), (n, view)

    def test_one_close_over_the_cap_is_announced(self, app_cfg, db):
        body = served_book(app_cfg, db, V.session_rows(V.FRIDAY, [10] * 201, step=1))
        assert "200 of 201 closes listed" in V.text_of(V.panel(body, "last"))


# ══ a payload without last_session renders exactly as US-030 ══════════════════════════
class TestWithoutALastSessionItIsUs030:
    @staticmethod
    def payloads(app_cfg, db) -> dict[str, dict]:
        app = H.make_app(app_cfg, db)
        H.insert_trades(app.db.engine, H.realistic_rows())
        realistic = H.served(app)
        realistic.pop("last_session")
        return {
            "paper-off": {"enabled": False, "status": {}, "trades": [], "totals_today": None},
            "empty-book": {
                "enabled": True,
                "status": {"opened": 0},
                "trades": [],
                "totals_today": {"closed": 0, "wins": 0, "losses": 0, "gross_rupees": "0.00"},
            },
            "open-and-closed-today": realistic,
            "only-yesterdays-close": {
                "enabled": True,
                "status": {"opened": 2, "filled": 1, "closed": 1, "hook_errors": 1},
                "trades": [
                    {**t, "closed_at": "2026-10-31T11:00:00+05:30"}
                    for t in realistic["trades"]
                    if t["status"] == "CLOSED"
                ],
                "totals_today": {"closed": 0, "wins": 0, "losses": 0, "gross_rupees": "0.00"},
            },
        }

    @pytest.mark.parametrize("key", ["absent", "null"])
    def test_the_panel_and_day_line_are_byte_for_byte_the_us030_ones(self, app_cfg, db, key):
        old_src = V.old_app_js(US030_UI_COMMIT)
        if old_src is None:
            pytest.skip(f"git history of {US030_UI_COMMIT} is not available here")
        old = H.top_level_definitions(old_src)
        for name, payload in self.payloads(app_cfg, db).items():
            if key == "null":
                payload = {**payload, "last_session": None}
            was_panel = V.run_expr(old, ["paperPanel"], "paperPanel(P)", payload)
            was_meta = V.run_expr(old, ["paperMeta"], "paperMeta(P)", payload)
            for view in ("today", None):
                assert V.panel(payload, view) == was_panel, (name, view)
                assert V.meta(payload, view) == was_meta, (name, view)

    def test_no_session_text_appears_in_the_today_or_all_views(self, app_cfg, db):
        for name, payload in self.payloads(app_cfg, db).items():
            for view in ("today", "all"):
                text = V.text_of(V.panel(payload, view)) + V.meta(payload, view).lower()
                assert "last session" not in text, (name, view)

    def test_a_payload_without_totals_today_never_falls_back(self, sunday):
        body, _ = sunday
        body = {k: v for k, v in body.items() if k != "totals_today"}
        assert "last session" not in V.text_of(V.panel(body, "today"))

    def test_an_unknown_view_is_the_today_view(self, sunday):
        for view in ("bogus", "", "LAST", None):
            assert V.panel(sunday[0], view) == V.panel(sunday[0], "today"), view


# ══ whatever last_session holds, the panel stays safe ═════════════════════════════════
BAD_SESSIONS = [
    pytest.param({"date": HOSTILE, "totals": {"closed": 1}}, id="hostile-date"),
    pytest.param({"date": "2026-13-45", "totals": {"closed": 1}}, id="impossible-date"),
    pytest.param({"date": 20261030, "totals": {"closed": 1}}, id="date-is-a-number"),
    pytest.param({"date": "2026-10-30"}, id="no-totals"),
    pytest.param({"date": "2026-10-30", "totals": None}, id="null-totals"),
    pytest.param({}, id="empty"),
    pytest.param("2026-10-30", id="a-string"),
    pytest.param([1, 2], id="a-list"),
]


class TestAMalformedLastSessionNeverBreaksThePanel:
    @pytest.mark.parametrize("session", BAD_SESSIONS)
    @pytest.mark.parametrize("view", ["today", "last", "all"])
    def test_no_crash_no_markup_and_no_junk(self, sunday, session, view):
        body = {**sunday[0], "last_session": session}
        html = V.panel(body, view)
        root = H.parse_html(html)
        assert not [n for n in root.walk() if n.tag in ("img", "script", "iframe")]
        assert not [a for n in root.walk() for a in n.attrs if a.startswith("on")]
        assert not JUNK.search(V.meta(body, view))

    def test_a_session_instrument_with_an_ampersand_is_shown_once_escaped(self, app_cfg, db):
        row = H.closed_row("M1", V.at_ist(V.FRIDAY, 10, 0), 100, instrument="M&M")
        text = H.visible_text(V.panel(served_book(app_cfg, db, [row]), "last"))
        assert "M&M" in text and "&amp;" not in text


# ══ the heading date is read from the date, not from the browser ══════════════════════
class TestTheHeadingDate:
    @staticmethod
    def session_payload(day: str) -> dict:
        return {
            "enabled": True,
            "trades": [
                {
                    "alert_id": "a",
                    "instrument": "X",
                    "side": "BUY",
                    "direction": "long",
                    "kind": "STOCK",
                    "strategy": "s",
                    "timeframe": "5m",
                    "grade": "live",
                    "status": "CLOSED",
                    "close_reason": "TARGET",
                    "qty": 1,
                    "entry_ts": f"{day}T10:00:00+05:30",
                    "entry": "1.00",
                    "exit": "2.00",
                    "gross": "1.00",
                    "net": None,
                    "created_at": f"{day}T10:00:00+05:30",
                    "closed_at": f"{day}T11:00:00+05:30",
                }
            ],
            "totals_today": {"closed": 0},
            "last_session": {"date": day, "totals": {"closed": 1, "wins": 1, "losses": 0}},
        }

    @pytest.mark.parametrize(
        "tz", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati", "Asia/Kolkata"]
    )
    @pytest.mark.parametrize("day", ["2026-10-30", "2026-11-01", "2026-12-31", "2028-02-29"])
    def test_the_heading_reads_weekday_day_month_in_any_browser_zone(self, day, tz):
        date = dt.date.fromisoformat(day)
        html = V.panel(self.session_payload(day), "last", tz=tz)
        assert V.heading_nodes(html, "last session", V.heading_for(date).lower()), (
            day,
            tz,
            V.text_of(html)[:200],
        )

    def test_every_day_of_three_years_gets_its_weekday_and_month_right(self):
        days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(1096)]
        expression = (
            "P.map((d) => paperPanel({ enabled: true, trades: [{ alert_id: 'a', status: 'CLOSED',"
            " instrument: 'X', side: 'BUY', grade: 'live', strategy: 's', timeframe: '5m', qty: 1,"
            " created_at: d + 'T10:00:00+05:30', closed_at: d + 'T11:00:00+05:30' }],"
            " totals_today: { closed: 0 }, last_session: { date: d, totals: { closed: 1 } } }, 'last'))"
        )
        out = V.run_expr(
            H.js_definitions(), ["paperPanel"], expression, [d.isoformat() for d in days]
        )
        wrong = [
            (d.isoformat(), V.heading_for(d))
            for d, html in zip(days, out, strict=True)
            if not V.heading_nodes(html, "last session", V.heading_for(d).lower())
        ]
        assert not wrong, wrong[:5]
