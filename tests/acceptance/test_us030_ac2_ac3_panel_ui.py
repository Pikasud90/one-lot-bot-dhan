"""US-030 AC2 and AC3 in the browser code: the Paper panel's render helpers (src/onelot/ui/app.js).

AC2: the Paper panel lists open paper trades and today's closed ones, with status chips
(pending, open, no fill, closed), entry / exit / net in rupees, the grade (live or delayed) and
the close reason; net is coloured with the existing pos / neg classes.
AC3: with no paper trades the panel says so; with paper.enabled false it says paper trading is
off.

No browser and no DOM. The helpers are lifted out of app.js (top-level definitions and
whatever they call, see us030_helpers.run_js) and run in node with `Date` frozen and the zone
set to Asia/Kolkata; what they return is HTML, which these tests PARSE instead of matching
strings, so a different but equivalent markup still passes. Skipped, with a reason, on a
machine without node.

JS names ASSUMED (the Manager's contract names none; change them in us030_helpers.HELPER_NAMES):
    paperChip(status)  paperRow(trade)  paperPanel(payload)      -- all return an HTML string

The last class feeds the REAL output of GET /api/paper into the real panel helper.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
import us001_helpers as base
import us030_helpers as H

from onelot.config import AppConfig

pytestmark = pytest.mark.skipif(
    H.NODE is None, reason="node is not installed; app.js helpers not exercised"
)

HOUR = dt.timedelta(hours=1)
SEC = dt.timedelta(seconds=1)
MIN = dt.timedelta(minutes=1)
JUNK = re.compile(r"\b(undefined|null|NaN|None)\b")  # a missing value leaking into the page
HOSTILE = "<img src=x onerror=alert(1)></td><script>alert(1)</script>"


def iso(value: dt.datetime) -> str:
    return value.isoformat()


def trade(**kw) -> dict:
    """A GET /api/paper trade: a CLOSED win today unless overridden."""
    m = H.midnight()
    values = {
        "alert_id": "A1",
        "instrument": "SBIN",
        "side": "BUY",
        "direction": "long",
        "kind": "STOCK",
        "strategy": "ema_cross",
        "timeframe": "10m",
        "grade": "live",
        "status": "CLOSED",
        "close_reason": "TARGET",
        "qty": 1,
        "created_at": iso(m + 10 * HOUR),
        "entry_ts": iso(m + 10 * HOUR + 60 * SEC),
        "entry": "1000.30",
        "exit": "1015.35",
        "gross": "15.05",
        "net": "12.05",
        "closed_at": iso(m + 11 * HOUR),
    }
    return {**values, **kw}


def waiting(**kw) -> dict:
    """A trade still waiting for its fill: no prices, no close."""
    return trade(
        status="PENDING_FILL",
        close_reason=None,
        entry_ts=None,
        entry=None,
        exit=None,
        gross=None,
        net=None,
        closed_at=None,
        **kw,
    )


def row_html(**kw) -> str:
    return H.html_of("row", trade(**kw))


def text_of(html: str) -> str:
    return H.visible_text(html).lower()


def spoken(value: str) -> str:
    """Lower case with underscores read as spaces: 'GAP_THROUGH_STOP' -> 'gap through stop'."""
    return value.lower().replace("_", " ")


def panel_text(payload) -> str:
    return text_of(H.html_of("panel", payload))


# ══ the status chip ═══════════════════════════════════════════════════════════════════
class TestStatusChip:
    @pytest.mark.parametrize(
        "status, text",
        [
            ("PENDING_FILL", "pending"),
            ("OPEN", "open"),
            ("NO_FILL", "no fill"),
            ("CLOSED", "closed"),
        ],
    )
    def test_each_status_reads_as_its_plain_words(self, status, text):
        assert H.visible_text(H.html_of("chip", status)).lower() == text

    def test_the_four_chips_are_four_different_things(self):
        shown = {H.visible_text(H.html_of("chip", s)) for s in H.ALL_STATUSES}
        assert len(shown) == 4

    @pytest.mark.parametrize("odd", ["WEIRD", "<b>X</b>", "constructor", "__proto__"])
    def test_an_unknown_status_never_becomes_markup_and_never_throws(self, odd):
        root = H.parse_html(H.html_of("chip", odd))
        assert not [n for n in root.walk() if n.tag in ("b", "script", "img")]


# ══ one row ═══════════════════════════════════════════════════════════════════════════
class TestRow:
    def test_entry_exit_and_net_are_shown_in_rupees_with_paise(self):
        html = row_html()
        for amount in ("1000.30", "1015.35", "12.05"):
            assert H.cells_showing(html, amount), f"{amount} is not shown: {text_of(html)}"

    @pytest.mark.parametrize(
        "net, gross, colour",
        [
            ("12.05", "15.05", "pos"),
            ("-13.30", "-12.05", "neg"),
            ("0.00", "1.00", None),
        ],
        ids=["profit", "loss", "zero"],
    )
    def test_net_is_coloured_with_the_existing_pos_and_neg_classes(self, net, gross, colour):
        cells = H.cells_showing(row_html(net=net, gross=gross), net)
        assert cells, f"net {net} is not shown"
        classes = set().union(*(H.classes_on_or_above(c) for c in cells))
        if colour is None:
            assert not classes & {"pos", "neg"}, f"a zero net must not be coloured: {classes}"
        else:
            other = {"pos", "neg"} - {colour}
            assert colour in classes and not classes & other, f"net {net} classes: {classes}"

    def test_entry_and_exit_prices_are_not_coloured_as_profit_or_loss(self):
        html = row_html()
        for price in ("1000.30", "1015.35"):
            for cell in H.cells_showing(html, price):
                assert not H.classes_on_or_above(cell) & {"pos", "neg"}, price

    def test_a_closed_trade_whose_net_is_not_computed_yet_shows_a_placeholder(self):
        html = row_html(net=None)
        assert not JUNK.search(H.visible_text(html)), H.visible_text(html)
        assert H.cells_showing(html, "1000.30") and H.cells_showing(html, "1015.35")

    def test_a_trade_waiting_for_its_fill_shows_no_prices_and_no_junk(self):
        html = H.html_of("row", waiting())
        text = H.visible_text(html)
        assert not JUNK.search(text), text
        assert "pending" in text.lower()
        assert not H.cells_showing(html, "0.00")

    @pytest.mark.parametrize(
        "status, reason, chip",
        [
            ("PENDING_FILL", None, "pending"),
            ("OPEN", None, "open"),
            ("NO_FILL", "GAP_THROUGH_STOP", "no fill"),
            ("CLOSED", "STOP", "closed"),
        ],
    )
    def test_the_row_carries_its_status_chip(self, status, reason, chip):
        values = {} if status == "CLOSED" else {"exit": None, "gross": None, "net": None}
        html = H.html_of("row", trade(status=status, close_reason=reason, closed_at=None, **values))
        assert chip in text_of(html)

    @pytest.mark.parametrize("grade, other", [("live", "delayed"), ("delayed", "live")])
    def test_the_grade_is_spelled_live_or_delayed(self, grade, other):
        words = set(re.findall(r"[a-z]+", text_of(row_html(grade=grade))))
        assert grade in words and other not in words

    @pytest.mark.parametrize("reason", ["TARGET", "STOP", "GAP_THROUGH_STOP", "SQUARE_OFF"])
    def test_the_close_reason_is_shown_in_words(self, reason):
        assert spoken(reason) in spoken(H.visible_text(row_html(close_reason=reason)))

    def test_the_instrument_is_named(self):
        assert "sbin" in text_of(row_html())

    def test_an_ampersand_in_the_instrument_is_shown_once_not_twice_escaped(self):
        text = H.visible_text(row_html(instrument="M&M"))
        assert "M&M" in text and "&amp;" not in text and "&lt;" not in text

    @pytest.mark.parametrize("field", sorted(H.TRADE_KEYS - {"alert_id"}))
    def test_a_hostile_value_in_any_field_never_becomes_markup(self, field):
        root = H.parse_html(row_html(**{field: HOSTILE}))
        tags = [n for n in root.walk() if n.tag in ("img", "script", "svg", "iframe")]
        handlers = [(n.tag, a) for n in root.walk() for a in n.attrs if a.startswith("on")]
        assert not tags and not handlers, (field, tags, handlers)


# ══ the whole panel ═══════════════════════════════════════════════════════════════════
class TestPanelMessages:
    @pytest.mark.parametrize(
        "payload",
        [
            {"enabled": False, "trades": []},
            {"enabled": False, "status": {}, "trades": [], "totals_today": None},
        ],
        ids=["bare", "with-blocks"],
    )
    def test_paper_off_says_paper_trading_is_off(self, payload):
        assert "paper trading is off" in panel_text(payload)

    def test_no_trades_says_so_and_does_not_claim_paper_is_off(self):
        text = panel_text({"enabled": True, "trades": []})
        assert "no paper trades" in text and "paper trading is off" not in text

    def test_a_payload_without_a_trades_list_does_not_crash_or_leak_junk(self):
        text = H.visible_text(H.html_of("panel", {"enabled": True}))
        assert not JUNK.search(text), text

    def test_paper_on_with_trades_does_not_say_it_is_off_or_empty(self):
        text = panel_text({"enabled": True, "trades": [trade(), waiting(alert_id="A2")]})
        assert "paper trading is off" not in text and "no paper trades" not in text


class TestPanelListing:
    @staticmethod
    def book() -> list[dict]:
        m = H.midnight()
        return [
            waiting(alert_id="b", instrument="BBB", created_at=iso(m - 20 * HOUR)),  # yesterday
            trade(alert_id="c", instrument="CCC"),  # closed today
            trade(alert_id="e", instrument="EEE", status="NO_FILL", close_reason="NO_FILL")
            | {"exit": None, "gross": "0.00", "net": "0.00", "entry": None, "entry_ts": None},
            trade(
                alert_id="a",
                instrument="AAA",
                status="OPEN",
                close_reason=None,
                created_at=iso(m - 72 * HOUR),  # opened three days ago and still open
                closed_at=None,
                exit=None,
                gross=None,
                net=None,
            ),
            trade(
                alert_id="d",
                instrument="DDD",
                closed_at=iso(m - SEC),  # 23:59:59 yesterday
                created_at=iso(m - 3 * HOUR),
            ),
        ]

    def test_open_trades_of_any_day_and_todays_closed_ones_are_listed(self):
        text = panel_text({"enabled": True, "trades": self.book()})
        for shown in ("aaa", "bbb", "ccc", "eee"):
            assert shown in text, f"{shown.upper()} should be on the panel: {text}"

    def test_a_trade_closed_before_today_is_not_listed(self):
        assert "ddd" not in panel_text({"enabled": True, "trades": self.book()})

    def test_a_trade_closed_in_the_first_second_of_today_is_listed(self):
        first = trade(instrument="FIRST", closed_at=iso(H.midnight()))
        assert "first" in panel_text({"enabled": True, "trades": [first]})

    def test_the_apis_newest_first_order_is_kept(self):
        trades = [
            trade(alert_id="n", instrument="NEWEST"),
            trade(alert_id="m", instrument="MIDDLE"),
            trade(alert_id="o", instrument="OLDEST"),
        ]
        text = panel_text({"enabled": True, "trades": trades})
        assert text.index("newest") < text.index("middle") < text.index("oldest")

    def test_open_trades_keep_the_apis_order_too(self):
        trades = [
            waiting(alert_id="n", instrument="NEWEST"),
            waiting(alert_id="m", instrument="MIDDLE"),
            trade(alert_id="o", instrument="OLDEST", status="OPEN", closed_at=None),
        ]
        text = panel_text({"enabled": True, "trades": trades})
        assert text.index("newest") < text.index("middle") < text.index("oldest")

    def test_every_listed_trade_has_its_chip(self):
        text = panel_text({"enabled": True, "trades": self.book()})
        for chip in ("pending", "open", "no fill", "closed"):
            assert chip in text, chip

    def test_a_hostile_instrument_name_never_becomes_markup_in_the_panel(self):
        html = H.html_of("panel", {"enabled": True, "trades": [trade(instrument=HOSTILE)]})
        root = H.parse_html(html)
        assert not [n for n in root.walk() if n.tag in ("img", "script")]
        assert not [a for n in root.walk() for a in n.attrs if a.startswith("on")]

    @pytest.mark.parametrize(
        "now, tz",
        [
            ("2026-11-02T00:30:00+05:30", "UTC"),  # the 1st in UTC and in Los Angeles
            ("2026-11-02T00:30:00+05:30", "America/Los_Angeles"),
            ("2026-11-01T22:00:00+05:30", "Pacific/Kiritimati"),  # already the 2nd there
        ],
        ids=["early-ist-utc-browser", "early-ist-la-browser", "late-ist-kiritimati-browser"],
    )
    def test_the_panel_follows_the_ist_calendar_day_whatever_zone_the_browser_is_in(self, now, tz):
        """'Today' is the IST date of the desk, not the date where the browser happens to be."""
        at = dt.datetime.fromisoformat(now)
        ist_day = at.astimezone(H.IST).replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {
            "enabled": True,
            "trades": [
                trade(alert_id="t", instrument="TODAYCLOSE", closed_at=iso(ist_day + 10 * MIN)),
                trade(alert_id="y", instrument="YESTERDAYCLOSE", closed_at=iso(ist_day - 10 * MIN)),
            ],
        }
        text = text_of(H.html_of("panel", payload, at=at, tz=tz))
        assert "todayclose" in text, "a trade closed on the IST day is missing"
        assert "yesterdayclose" not in text, "a trade closed on the previous IST day is listed"


# ══ the real API output on the real panel ═════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    base.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = base.new_db(tmp_path, "us030-ui.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


class TestRealApiOnTheRealPanel:
    @staticmethod
    def real_book(app) -> None:
        m = H.midnight()
        no_fill = H.trade_values(
            "R-NOFILL",
            status="NO_FILL",
            close_reason="NO_FILL",
            instrument="HDFCBANK",
            gross=0,
            costs=0,
            net=0,
            created=m + 2 * SEC,  # the book leaves exit_ts NULL on a NO_FILL, as here
        )
        H.insert_trades(
            app.db.engine,
            [
                H.trade_values(
                    "R-OPEN",
                    status="OPEN",
                    instrument="M&M",
                    created=m - 48 * HOUR,
                    entry=100030,
                    entry_ts=m - 48 * HOUR + 60 * SEC,
                ),
                H.trade_values("R-WAIT", status="PENDING_FILL", created=m - 5 * HOUR),
                H.closed_row("R-TODAY", m + SEC, -1205, instrument="RELIANCE"),
                H.closed_row("R-YESTERDAY", m - SEC, 500, instrument="INFY"),
                no_fill,
            ],
        )

    def test_the_panel_shows_what_the_endpoint_serves_for_a_realistic_book(self, app_cfg, db):
        app = H.make_app(app_cfg, db)
        self.real_book(app)
        html = H.html_of("panel", H.served(app))
        text = text_of(html)
        for shown in ("m&m", "sbin", "reliance"):
            assert shown in text, f"{shown} is missing from the panel: {text}"
        assert "infy" not in text, "a trade closed yesterday must not be on today's panel"
        assert not JUNK.search(H.visible_text(html)), H.visible_text(html)

    def test_a_no_fill_trade_from_today_is_on_the_panel(self, app_cfg, db):
        """The book leaves a NO_FILL trade's exit time empty, so the API and the panel
        together must still place it on today (AC2 names the 'no fill' chip)."""
        app = H.make_app(app_cfg, db)
        self.real_book(app)
        text = panel_text(H.served(app))
        assert "hdfcbank" in text and "no fill" in text

    def test_prices_arrive_on_the_panel_exactly_as_the_endpoint_served_them(self, app_cfg, db):
        app = H.make_app(app_cfg, db)
        self.real_book(app)
        html = H.html_of("panel", H.served(app))
        assert H.cells_showing(html, "1000.30"), "entry"
        assert H.cells_showing(html, "988.25"), "exit of the trade closed today"

    def test_paper_off_through_the_real_endpoint(self, app_cfg, db):
        app = H.make_app(app_cfg, db, enabled=False)
        assert "paper trading is off" in panel_text(H.served(app))

    def test_an_empty_book_through_the_real_endpoint(self, app_cfg, db):
        app = H.make_app(app_cfg, db)
        assert "no paper trades" in panel_text(H.served(app))
