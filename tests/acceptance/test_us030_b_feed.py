"""US-030 mode B: the Paper panel refreshes with the desk snapshot, at most every 5 s.

Contract (Manager's Notes): "the panel fetches /api/paper on each snapshot version change (not
more than every 5 s)". The mode-A tests could only check that wiring statically; here the REAL
`applyStream` of app.js (the function every SSE message goes through) and the real Paper code
run in node over a fake browser: a virtual clock and timers, a fake fetch that records when it
was asked, and a fake DOM. Every other screen is a no-op. Skipped without node.

What is pinned:
* a snapshot of a new version fetches GET /api/paper (a plain read), the same version never
  fetches twice, and a version after a quiet spell is fetched at once;
* however fast the versions arrive (random schedules included, with the endpoint failing at
  times), fetches start at least 5 s apart and the last one saw the last version;
* opening the Live Desk tab loads the panel at once; a failing endpoint is not hammered, says
  why in escaped text, and never leaves the panel on "Loading" for ever;
* the real endpoint's payload reaches the page through the real helpers, day totals included.
"""

from __future__ import annotations

import json
import random
import re

import pytest
import us001_helpers as base
import us030_helpers as H

from onelot.config import AppConfig

pytestmark = pytest.mark.skipif(
    H.NODE is None, reason="node is not installed; app.js helpers not exercised"
)

THROTTLE_MS = 5000
JUNK = re.compile(r"\b(undefined|null|NaN|None)\b")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    base.block_network(monkeypatch)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    H.freeze_clock(monkeypatch)


def starts(state) -> list[int]:
    return [f["at"] for f in state["fetches"]]


def gaps(state) -> list[int]:
    s = starts(state)
    return [b - a for a, b in zip(s, s[1:], strict=False)]


def answer_with(body: dict, status: int = 200) -> str:
    return f"() => ({{ status: {status}, body: {json.dumps(body)} }})"


# ══ when it fetches ═══════════════════════════════════════════════════════════════════
class TestWhenItFetches:
    def test_a_snapshot_of_a_new_version_fetches_the_endpoint_with_a_plain_get(self):
        got = H.run_stream("snapshot(); await advance(0);")
        assert starts(got) == [0]
        fetch = got["fetches"][0]
        assert fetch["url"].split("?")[0] == "/api/paper"
        opts = fetch["opts"] or {}
        assert str(opts.get("method", "GET")).upper() == "GET" and "body" not in opts

    def test_the_same_version_is_never_fetched_twice(self):
        got = H.run_stream(
            "snapshot(1); await advance(0);"
            "for (let i = 0; i < 6; i++) { await advance(10000); snapshot(1); }"
            "await advance(10000);"
        )
        assert starts(got) == [0]

    def test_a_version_after_a_quiet_spell_is_fetched_at_once(self):
        got = H.run_stream("snapshot(1); await advance(20000); snapshot(2); await advance(0);")
        assert starts(got) == [0, 20000]
        assert [f["version"] for f in got["fetches"]] == [1, 2]

    def test_a_burst_inside_5_s_costs_one_more_fetch_exactly_5_s_after_the_first(self):
        got = H.run_stream(
            "snapshot(1); await advance(0);"
            "for (let v = 2; v <= 4; v++) { await advance(1000); snapshot(v); }"
            "await advance(10000);"
        )
        assert starts(got) == [0, THROTTLE_MS]
        assert got["fetches"][1]["version"] == 4, "the second fetch must see the latest version"

    def test_a_snapshot_every_second_for_a_minute_stays_under_one_fetch_per_5_s(self):
        got = H.run_stream(
            "for (let i = 0; i < 60; i++) { snapshot(); await advance(1000); } await advance(10000);"
        )
        assert all(g >= THROTTLE_MS for g in gaps(got)), gaps(got)
        assert 10 <= len(starts(got)) <= 14, starts(got)
        assert got["fetches"][-1]["version"] == got["version"] == 60

    def test_opening_the_live_desk_tab_loads_the_panel_at_once(self):
        got = H.run_stream(
            "__tab.live = false; snapshot(); await advance(30000);"
            "__tab.live = true; await tabLoaders.live(); await advance(0);",
            extra=(H.statement_starting("tabLoaders.live ="),),
        )
        assert got["fetches"], "opening the tab never fetched"
        assert 30000 <= got["fetches"][-1]["at"] <= 31000, starts(got)
        assert "no paper trades" in H.paper_text(got)

    def test_reopening_the_tab_within_5_s_still_never_beats_the_throttle(self):
        got = H.run_stream(
            "snapshot(); await advance(0);"
            "for (let i = 0; i < 4; i++) { await tabLoaders.live(); await advance(500); }"
            "await advance(10000);",
            extra=(H.statement_starting("tabLoaders.live ="),),
        )
        assert all(g >= THROTTLE_MS for g in gaps(got)), starts(got)


# ══ the rate, whatever the schedule ═══════════════════════════════════════════════════
def random_schedule(seed: int) -> str:
    """A reproducible stream: 60 steps of waiting (0-4 s), new snapshots and endpoint outages."""
    rng = random.Random(seed)
    steps = []
    for _ in range(60):
        roll = rng.random()
        if roll < 0.55:
            steps.append("snapshot();")
        elif roll < 0.62:
            steps.append("__fail = true;")
        elif roll < 0.72:
            steps.append("__fail = false;")
        steps.append(f"await advance({rng.randrange(0, 4000)});")
    steps.append("__fail = false; snapshot(); await advance(40000);")
    return "\n".join(steps)


class TestTheRateWhateverTheSchedule:
    @pytest.mark.parametrize("seed", range(20))
    def test_fetches_start_5_s_apart_and_the_last_one_sees_the_last_version(self, seed):
        got = H.run_stream(random_schedule(seed))
        assert all(g >= THROTTLE_MS for g in gaps(got)), (seed, starts(got))
        assert got["fetches"][-1]["version"] == got["version"], (seed, got["fetches"][-3:])

    @pytest.mark.parametrize("seed", range(20))
    def test_with_a_slow_server_there_is_still_one_request_at_a_time(self, seed):
        rng = random.Random(1000 + seed)
        steps = []
        for _ in range(40):
            if rng.random() < 0.5:
                steps.append("snapshot();")
            steps.append(f"__delay = {rng.randrange(0, 7000)};")
            steps.append(f"await advance({rng.randrange(0, 3000)});")
        steps.append("__delay = 500; snapshot(); await advance(60000);")
        got = H.run_stream("\n".join(steps))
        assert got["maxInflight"] == 1, (seed, got["maxInflight"])
        assert all(g >= THROTTLE_MS for g in gaps(got)), (seed, starts(got))
        assert got["fetches"][-1]["version"] == got["version"], (seed, got["fetches"][-3:])

    def test_a_snapshot_during_a_slow_request_gets_one_follow_up_5_s_after_the_first_started(self):
        got = H.run_stream(
            "__delay = 2000; snapshot(1); await advance(1000); snapshot(2);"
            "await advance(1000); snapshot(3); await advance(30000);"
        )
        assert starts(got) == [0, THROTTLE_MS]
        assert got["maxInflight"] == 1
        assert got["fetches"][1]["version"] == 3

    def test_a_failing_endpoint_is_asked_no_more_than_once_per_5_s(self):
        got = H.run_stream("__fail = true; snapshot(); await advance(60000);")
        assert all(g >= THROTTLE_MS for g in gaps(got)), starts(got)
        assert len(starts(got)) <= 13, starts(got)


# ══ what the reader sees ══════════════════════════════════════════════════════════════
class TestWhatTheReaderSees:
    def test_a_first_failure_says_why_in_escaped_text_and_never_stays_on_loading(self):
        hostile = "<img src=x onerror=alert(1)> database is down"
        got = H.run_stream(
            "snapshot(); await advance(0);",
            answer=f"() => ({{ status: 503, body: {{ detail: {json.dumps(hostile)} }} }})",
        )
        html = H.paper_container(got)["innerHTML"]
        root = H.parse_html(html)
        assert not [n for n in root.walk() if n.tag in ("img", "script")]
        assert not [a for n in root.walk() for a in n.attrs if a.startswith("on")]
        text = H.paper_text(got)
        assert "database is down" in text and "unavailable" in text
        assert "loading paper trades" not in text

    def test_a_network_failure_is_explained_too(self):
        got = H.run_stream("__fail = true; snapshot(); await advance(0);")
        text = H.paper_text(got)
        assert "unavailable" in text and "loading paper trades" not in text

    def test_a_later_failure_keeps_the_table_and_warns_until_the_next_success(self):
        body = {"enabled": True, "status": {}, "totals_today": {}, "trades": [api_trade()]}
        serve = (
            f"__answer = () => __fail ? {{ throw: 'down' }} : "
            f"{{ status: 200, body: {json.dumps(body)} }};"
        )
        first = "snapshot(); await advance(0);"
        loaded = H.run_stream(serve + first)
        assert "sbin" in H.paper_text(loaded) and "unavailable" not in H.paper_text(loaded)

        failed = H.run_stream(serve + first + "__fail = true; snapshot(); await advance(6000);")
        assert "sbin" in H.paper_container(failed)["innerHTML"].lower(), "the table was dropped"
        meta = H.paper_element(failed, "meta")
        assert "warn" in meta["className"] and "unavailable" in meta["textContent"]

        recovered = H.run_stream(
            serve
            + first
            + "__fail = true; snapshot(); await advance(6000);"
            + "__fail = false; snapshot(); await advance(6000);"
        )
        meta = H.paper_element(recovered, "meta")
        assert "warn" not in meta["className"] and "unavailable" not in meta["textContent"]


def api_trade() -> dict:
    """One GET /api/paper trade: an OPEN SBIN stock trade."""
    return {
        "alert_id": "A1",
        "instrument": "SBIN",
        "side": "BUY",
        "direction": "long",
        "kind": "STOCK",
        "strategy": "ema_cross",
        "timeframe": "10m",
        "grade": "live",
        "status": "OPEN",
        "close_reason": None,
        "qty": 1,
        "entry_ts": H.midnight().isoformat(),
        "entry": "1000.30",
        "exit": None,
        "gross": None,
        "net": None,
        "created_at": H.midnight().isoformat(),
        "closed_at": None,
    }


# ══ the real endpoint on the real page code ═══════════════════════════════════════════
@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    d = base.new_db(tmp_path, "us030-feed.db", make_global=True)
    yield d
    base.dispose_db(d, was_global=True)


class TestTheRealEndpointOnTheRealPage:
    @pytest.fixture
    def payload(self, app_cfg, db):
        app = H.make_app(app_cfg, db)
        H.insert_trades(app.db.engine, H.realistic_rows())
        return H.served(app)

    def test_a_snapshot_tick_puts_todays_book_on_the_page(self, payload):
        got = H.run_stream("snapshot(); await advance(0);", answer=answer_with(payload))
        text = H.paper_text(got)
        for shown in ("m&m", "sbin", "reliance", "hdfcbank"):
            assert shown in text, f"{shown} missing from the page: {text}"
        assert "infy" not in text, "a trade closed yesterday must not be on today's panel"
        assert not JUNK.search(H.visible_text(H.paper_container(got)["innerHTML"]))

    def test_the_day_totals_reach_the_page_in_rupees_with_paise(self, payload):
        got = H.run_stream("snapshot(); await advance(0);", answer=answer_with(payload))
        shown = H.money_text(H.paper_element(got, "meta")["textContent"])
        assert "-12.05" in shown, f"today's gross is missing from the day line: {shown!r}"
        assert not JUNK.search(H.paper_element(got, "meta")["textContent"])

    def test_totals_with_a_net_show_both_amounts_exactly(self):
        body = {
            "enabled": True,
            "status": {"opened": 5, "filled": 3, "no_fill": 1, "closed": 2, "hook_errors": 0},
            "totals_today": {
                "closed": 2,
                "wins": 1,
                "losses": 1,
                "gross_rupees": "988.30",
                "net_rupees": "-1.00",
            },
            "trades": [],
        }
        got = H.run_stream("snapshot(); await advance(0);", answer=answer_with(body))
        shown = H.money_text(H.paper_element(got, "meta")["textContent"])
        assert "988.30" in shown and "-1.00" in shown, shown

    def test_a_net_not_computed_yet_is_not_shown_as_a_number(self):
        body = {
            "enabled": True,
            "status": {},
            "totals_today": {
                "closed": 1,
                "wins": 0,
                "losses": 1,
                "gross_rupees": "-12.05",
                "net_rupees": None,
            },
            "trades": [],
        }
        got = H.run_stream("snapshot(); await advance(0);", answer=answer_with(body))
        meta = H.paper_element(got, "meta")["textContent"]
        assert not JUNK.search(meta), meta
        assert H.money_text(meta).count("-12.05") == 1, "only the gross is an amount"

    def test_paper_off_through_the_stream_says_so_and_has_no_day_line(self):
        body = {"enabled": False, "status": {}, "trades": [], "totals_today": None}
        got = H.run_stream("snapshot(); await advance(0);", answer=answer_with(body))
        assert "paper trading is off" in H.paper_text(got)
        assert "closed" not in H.paper_element(got, "meta")["textContent"].lower()
