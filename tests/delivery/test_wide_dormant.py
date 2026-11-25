"""US-103: a stock that stops trading, or stays stale, never holds a place for good.

The evening round of the wide list through the product's own path (a fake broker and a store
on a throw-away database, or the bars in memory as the replay reads them):

- a stock with no new daily bar for `dormant_after` market sessions is dormant: its open trade
  is closed at the last stored close as NO_DATA, a pending entry is cancelled and never filled,
  the place within the cap goes to the next new signal, and the round stops waiting for it;
- a market session is a trading day of the app's calendar on which at least half of the other
  stocks did get a bar: a holiday the calendar does not list, a special session off the
  calendar and an evening on which the bars of a few stocks only came are not counted;
- two evening rounds must each have found the stock with that many missing sessions, and the
  round of the next session releases it if it still has no bar, on any try: with
  `dormant_after: 3` on the fifth evening. An evening before the count is reached confirms
  nothing, and neither does a try that never asked the broker for the stock;
- a stale stock is released the same way; an exit held for review is released as REVIEW on
  the same count, and no money is booked for it;
- the dormant stock is still asked for every evening and read again when its bars return;
- the score counts the released trades apart; `dormant_after: 0` is the desk as before.

No test here reads the real clock: every round is given its time.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from onelot.clock import IST, TradingCalendar
from onelot.config import SessionCfg
from onelot.data.store import save_bars
from onelot.delivery.money import delivery_model, depository_paise, rupees_to_paise, trade_result
from onelot.delivery.runner import DeliveryDesk
from onelot.delivery.wide import (
    CANCELLED,
    CONFIRM_ROUNDS,
    DORMANT_AFTER,
    FETCH_BACK,
    FULL_FROM,
    MARKET_SHARE,
    NO_DATA,
    RELEASES,
    REVIEW,
    ROUND_TRIES,
)
from onelot.desk.config import WideCfg, load_desk_config
from onelot.store.schema import METADATA, wide_state, wide_trades

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wide_ui import VIEW, client_for, js, needs_node  # noqa: E402 - the page's harness
from wide_helpers import (  # noqa: E402
    BASE,
    COSTS,
    START,
    Broker,
    Recorder,
    at,
    closes,
    drop_then,
    frame,
    source_of,
    stock,
    weekdays,
    wide_desk,
)

N = BASE + 90
DATES = weekdays(START, N)
SIG = BASE  # a Monday: the drop session of `slow(SIG)`, the signal on its close
PLAIN = frame(DATES, closes([], N))  # a steady rise: trades every session, never a signal


def slow(day: int) -> pd.DataFrame:
    """Down 10% on session `day` (the signal), then 0.1% up a session: in at the next open and
    open for weeks (under its 20-session average, far above its stop)."""
    return frame(DATES, drop_then(day, 0.10, 0.001, N))


def cut(f: pd.DataFrame, last: int) -> pd.DataFrame:
    """A stock that stops trading: no daily bar after session `last`."""
    return f.iloc[: last + 1]


def without(f: pd.DataFrame, first: int, last: int) -> pd.DataFrame:
    """No daily bar on the sessions `first` to `last`; before and after as they were."""
    return pd.concat([f.iloc[:first], f.iloc[last + 1 :]])


def halved(f: pd.DataFrame) -> pd.DataFrame:
    """The broker's whole history after a 1:2 split: every price halved."""
    g = f.copy()
    g[["open", "high", "low", "close"]] = g[["open", "high", "low", "close"]] / 2
    return g


def unadjusted(f: pd.DataFrame, i: int, factor: float) -> pd.DataFrame:
    """A split the broker left unadjusted: every bar from session `i` on times `factor`."""
    g = f.copy()
    g.iloc[i:, :4] = g.iloc[i:, :4].to_numpy() * factor
    return g


class Lagging(Broker):
    """A broker whose bars of the stocks in `late` come one day late: an evening's answer
    for them ends at the session before."""

    late: frozenset[str] = frozenset()

    def fetch(self, stock, start, end):  # noqa: ANN001, ANN201
        f = super().fetch(stock, start, end)
        if stock.key in self.late and self.published is not None and len(f):
            f = f[pd.DatetimeIndex(f.index).tz_convert(IST).date < self.published]
        return f


def desk(frames, rec=None, broker_type=Broker, **kw):
    broker = broker_type(dict(frames))
    w = wide_desk(
        [stock(k) for k in frames], DATES, fetch=broker.fetch, data_api_active=lambda: True,
        notifier=rec, sleep=lambda s: None, **kw,
    )  # fmt: skip
    return w, broker


def try_at(w, i: int, k: int = 0, stop=None):
    """Try `k` (from 0) of the round of session `i`: 18:00, then every 30 minutes."""
    return w.run_round(at(DATES[i], f"{18 + k // 2}:{30 * (k % 2):02d}"), DATES[i], stop)


def go(w, broker, i: int):
    """The evening of session `i` as the desk runs it: the broker has published the session
    and the round is tried every 30 minutes until it is finished. Answers the last try."""
    broker.published = DATES[i]
    for k in range(ROUND_TRIES):
        r = try_at(w, i, k)
        if r.finished:
            break
    w.tell(at(DATES[i], "20:05"))
    return r


def tries(w, i: int) -> int:
    return int(w.book.round(DATES[i])["tries"])


def by_key(w) -> dict:
    return {t.series_key: t for t in w.book.all()}


def reading(w, key: str = "AAA") -> dict:
    return w.book.states()[key]["reading"]


def quiet(w, key: str = "AAA") -> tuple[int, int]:
    """(missing sessions, rounds counted) of a stock that is not dormant; (0, 0) with none."""
    q = reading(w, key).get("quiet") or {}
    return q.get("sessions", 0), q.get("rounds", 0)


def stored_days(w, key: str = "AAA") -> list[dt.date]:
    return [d.date() for d in w._stored(key).index.tz_convert(IST)]


def rows(engine, table) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(select(table).order_by(*table.primary_key)).mappings()
        ]


def ids(rec: Recorder) -> list[str]:
    return [n.payload["id"] for n in rec.sent]


def iso(i: int) -> str:
    return DATES[i].isoformat()


# With `dormant_after: 3` and a last bar on session L, the rounds find: L+1 one missing session,
# L+2 two, L+3 three (the first round that counts), L+4 four (the second); the round of L+5
# releases.


# ── 1. a stock that stops trading with an open trade within the cap ──────────
def test_a_stock_that_stops_trading_gives_its_place_to_the_next_new_signal():
    last = SIG + 3
    frames = {
        "AAA": cut(slow(SIG), last),  # in at SIG + 1, then no bar after SIG + 3
        "BBB": PLAIN,  # trades on: the round sees that the market was open
        "CCC": slow(SIG + 2),  # the one place is AAA's: recorded only
        "DDD": slow(SIG + 8),  # the evening AAA is released: takes the place
        "EEE": slow(SIG + 9),  # the place is DDD's by then: recorded only
    }
    rec = Recorder()
    w, broker = desk(frames, rec, cap_open=1, dormant_after=3)
    for i in range(SIG - 2, SIG + 8):
        r = go(w, broker, i)
        assert r.finished and r.summary["dormant"] == [] and r.summary["released"] == []
        # until it is dormant its missing bar is waited for: all four tries
        assert tries(w, i) == (1 if i <= last else ROUND_TRIES)
        assert r.summary["waiting"] == ([] if i <= last else ["AAA"])
        if i > last:  # the two counts, beside what the rule last read of its open trade
            k = i - last  # missing sessions; a round counts from the third on
            assert reading(w)["quiet"] == {
                "last_bar": iso(last), "sessions": k, "rounds": max(0, k - 2),
                "counted": iso(i) if k >= 3 else None,
            }  # fmt: skip
            assert reading(w)["busy"] and reading(w)["session"] == iso(last)
        else:
            assert "quiet" not in reading(w)
    open_ = by_key(w)["AAA"]  # four sessions without a bar, two rounds counted
    assert (open_.status, open_.tier, open_.entry_session) == ("OPEN", "cap", DATES[SIG + 1])
    assert by_key(w)["CCC"].tier == "test" and w.book.cap_open() == 1

    r = go(w, broker, SIG + 8)  # the next evening, and still no bar of AAA
    assert r.summary["released"] == ["AAA (no data)"] and r.summary["dormant"] == ["AAA"]
    assert tries(w, SIG + 8) == 1 and r.summary["waiting"] == []  # no longer waited for
    t = by_key(w)
    a = t["AAA"]
    c = frames["AAA"]["close"].to_numpy(float)
    assert (a.status, a.exit_reason, a.tier) == ("CLOSED", NO_DATA, "cap")
    assert (a.exit_signal_date, a.exit_session) == (DATES[SIG + 8], DATES[SIG + 8])
    assert a.exit_paise == rupees_to_paise(c[last]) and a.sessions_held == 3  # the last close
    booked = trade_result(
        delivery_model(COSTS), a.entry_paise, a.exit_paise, a.qty, depository_paise(COSTS)
    )
    assert (a.gross_paise, a.charges_paise, a.dp_paise, a.net_paise) == (
        booked.gross_paise,
        booked.charges_paise,
        booked.dp_paise,
        booked.net_paise,
    )
    assert a.charges_paise and a.dp_paise  # booked as a sale is
    assert a.note == (
        f"released on {DATES[SIG + 8]:%d %b %Y}: no daily price for 5 trading days after "
        f"{DATES[last]:%d %b %Y} (a suspension, a merger or a delisting?); closed at its last "
        f"known close, Rs {c[last]:,.2f}, not by the rule"
    )
    # the place: that evening's new signal has it; an old recorded trade never moves up
    assert (t["DDD"].tier, t["DDD"].signal_date) == ("cap", DATES[SIG + 8])
    assert t["CCC"].tier == "test" and w.book.cap_open() == 1
    r = go(w, broker, SIG + 9)
    assert tries(w, SIG + 9) == 1
    assert r.summary["dormant"] == ["AAA"] and r.summary["released"] == []
    assert by_key(w)["EEE"].tier == "test"
    # the owner was told to buy AAA and DDD; nothing ever said SELL for the released trade
    assert ids(rec) == [f"pbw-AAA-{iso(SIG)}-entry", f"pbw-DDD-{iso(SIG + 8)}-entry"]

    # the mark on the stock, as the page reads it
    mark = {"since": iso(SIG + 8), "last_bar": iso(last), "sessions": 5, "rounds": 2}
    assert reading(w) == {"session": iso(last), "dormant": mark}
    view = w.view()
    assert view["dormant"] == [{"series_key": "AAA", "name": "AAA Ltd", **mark}]
    assert view["dormant_after"] == 3 and view["cap_used"] == 1
    assert [x["series_key"] for x in view["closed"]] == ["AAA"]
    assert "AAA" not in [x["series_key"] for x in view["closest"]]
    assert view["last_round"]["dormant"] == ["AAA"]

    # the score keeps it apart: not one of the rule's own exits
    score = w.book.score()
    apart = {
        "trades": 1, "booked": 1, "winners": 0, "net": a.net_paise / 100, "no_data": 1,
        "review": 0,
    }  # fmt: skip
    assert a.net_paise < 0  # three flat sessions: the charges make it a loss
    for tier in ("all", "cap"):
        assert score[tier]["trades"] == 0 and score[tier]["net"] == 0  # no rule exit yet
        assert score[tier]["released"] == apart and score[tier]["cancelled"] == 0


def test_without_a_fetch_the_release_comes_before_the_signals_of_its_round():
    # the replay's path: the bars in memory, one try a round
    last = SIG + 3
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, "DDD": slow(SIG + 8)}
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), cap_open=1, dormant_after=3
    )
    for i in range(SIG - 1, SIG + 9):
        r = w.run_round(at(DATES[i]), DATES[i])
    t = by_key(w)
    assert r.summary["released"] == ["AAA (no data)"]
    assert (t["AAA"].exit_reason, t["AAA"].exit_session) == (NO_DATA, DATES[SIG + 8])
    assert (t["DDD"].tier, t["DDD"].signal_date) == ("cap", DATES[SIG + 8])


def test_a_release_on_the_first_try_frees_the_place_with_few_bars_in():
    # the release evening: on the first try only DDD's bar of the session is in (one of six
    # others: not a market session yet). The counts were met on earlier evenings, so the dead
    # stock is released on that try all the same, and DDD's signal of that try has the place
    last = SIG + 3
    others = {f"B{i}": PLAIN.copy() for i in range(1, 6)}
    frames = {"DDD": slow(SIG + 8), "AAA": cut(slow(SIG), last), **others}
    rec = Recorder()
    w, broker = desk(frames, rec, broker_type=Lagging, cap_open=1, dormant_after=3)
    for i in range(SIG - 2, SIG + 8):
        go(w, broker, i)
    assert by_key(w)["AAA"].status == "OPEN" and quiet(w) == (4, 2)
    broker.published, broker.late = DATES[SIG + 8], frozenset(others)
    r = try_at(w, SIG + 8, 0)
    assert not r.finished and r.summary["waiting"] == sorted(others)
    assert r.summary["released"] == ["AAA (no data)"] and r.summary["dormant"] == ["AAA"]
    t = by_key(w)
    assert (t["AAA"].status, t["AAA"].exit_reason) == ("CLOSED", NO_DATA)
    assert (t["DDD"].tier, t["DDD"].signal_date) == ("cap", DATES[SIG + 8])
    w.tell(at(DATES[SIG + 8], "18:05"))
    assert ids(rec)[-1] == f"pbw-DDD-{iso(SIG + 8)}-entry"  # and it is alerted


def test_the_default_is_ten_market_sessions():
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN}
    w = wide_desk([stock(k) for k in frames], DATES, source=source_of(frames))
    assert w.dormant_after == DORMANT_AFTER == 10 and CONFIRM_ROUNDS == 2
    seen = {}
    for i in range(SIG - 1, last + 12):
        r = w.run_round(at(DATES[i]), DATES[i])
        assert r.summary["dormant"] == [] and r.summary["released"] == []
        seen[i - last] = quiet(w)
    # nine sessions confirm nothing; the rounds of the tenth and the eleventh count
    assert [seen[k] for k in (8, 9, 10, 11)] == [(8, 0), (9, 0), (10, 1), (11, 2)]
    assert by_key(w)["AAA"].status == "OPEN"
    r = w.run_round(at(DATES[last + 12]), DATES[last + 12])  # the round of the twelfth
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", NO_DATA, DATES[last + 12])
    assert r.summary["released"] == ["AAA (no data)"]
    assert "no daily price for 12 trading days" in a.note


def test_a_release_with_a_gain_is_a_winner_among_the_released():
    # up 3% a session after its entry, then no bar: the last known close is above the entry
    last = SIG + 2
    frames = {"AAA": cut(frame(DATES, drop_then(SIG, 0.10, 0.03, N)), last), "BBB": PLAIN}
    w = wide_desk([stock(k) for k in frames], DATES, source=source_of(frames), dormant_after=3)
    for i in range(SIG - 1, last + 6):
        w.run_round(at(DATES[i]), DATES[i])
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_reason) == ("CLOSED", NO_DATA) and a.net_paise > 0
    for tier in ("all", "cap"):
        released = w.book.score()[tier]["released"]
        assert (released["trades"], released["booked"], released["winners"]) == (1, 1, 1)
        assert released["net"] == a.net_paise / 100 > 0


# ── 2. a pending entry ───────────────────────────────────────────────────────
def test_a_pending_entry_of_a_stock_that_stops_trading_is_cancelled_and_never_filled():
    back = SIG + 21
    # its last bar is the signal's own; the bars return three weeks later
    frames = {"AAA": without(slow(SIG), SIG + 1, back - 1), "BBB": PLAIN}
    rec = Recorder()
    w, broker = desk(frames, rec, dormant_after=3)
    for i in range(SIG - 2, SIG + 5):
        r = go(w, broker, i)
    a = by_key(w)["AAA"]
    assert (a.status, a.tier, a.signal_date) == ("PENDING_ENTRY", "cap", DATES[SIG])
    assert w.summary()["cap_used"] == 1 and len(w.summary()["pending"]) == 1
    r = go(w, broker, SIG + 5)
    a = by_key(w)["AAA"]
    assert r.summary["released"] == ["AAA (no data, entry cancelled)"]
    assert (a.status, a.exit_reason, a.exit_session) == (CANCELLED, NO_DATA, DATES[SIG + 5])
    assert (a.entry_session, a.entry_paise, a.exit_paise, a.net_paise) == (None,) * 4
    assert a.note == (
        f"entry cancelled on {DATES[SIG + 5]:%d %b %Y}: no daily price for 5 trading days after "
        f"{DATES[SIG]:%d %b %Y} (a suspension, a merger or a delisting?); never filled"
    )
    assert w.summary()["cap_used"] == 0 and w.summary()["pending"] == []
    score = w.book.score()
    for tier in ("all", "cap"):  # never a trade: not in the score, not among the released
        assert (score[tier]["trades"], score[tier]["cancelled"]) == (0, 1)
        assert score[tier]["released"]["trades"] == 0
    # the cancelled entry is among the trades that are over, for the page
    assert [(t.series_key, t.status) for t in w.book.closed()] == [("AAA", CANCELLED)]
    (row,) = w.view()["closed"]
    assert (row["status"], row["entry"], row["net"], row["exit_session"]) == (
        CANCELLED,
        None,
        None,
        iso(SIG + 5),
    )
    # the bars return: the stock is read again, the cancelled entry is never filled
    for i in range(SIG + 6, back + 2):
        r = go(w, broker, i)
        assert r.summary["returned"] == (["AAA"] if i == back else [])
        assert tries(w, i) == 1
    assert r.summary["dormant"] == []
    (a,) = w.book.all()
    assert (a.status, a.entry_session, a.entry_paise) == (CANCELLED, None, None)
    assert w.book.states()["AAA"]["last_session"] == iso(back + 1)
    assert ids(rec) == [f"pbw-AAA-{iso(SIG)}-entry"]  # told once, to enter


def test_a_filled_trade_is_never_cancelled_as_an_entry():
    frames = {"AAA": slow(SIG), "BBB": PLAIN}
    w = wide_desk([stock(k) for k in frames], DATES, source=source_of(frames))
    for i in range(SIG - 1, SIG + 2):
        w.run_round(at(DATES[i]), DATES[i])
    (t,) = w.book.all()
    assert t.status == "OPEN"
    assert w.book.cancel_entry(t, DATES[SIG + 2], NO_DATA, "no", at(DATES[SIG + 2])) is False
    (again,) = w.book.all()
    assert (again.status, again.exit_reason, again.note) == ("OPEN", None, t.note)


# ── 3. what is not a missing session ─────────────────────────────────────────
def holiday_desk(dormant_after: int):
    """AAA's last bar is a Tuesday (its entry session). The others have no bar on the Thursday
    (a holiday the app's calendar does not list), a bar on the Friday (a holiday on the
    calendar with a special session, as a Muhurat is) and one on the Saturday (a special
    session at a weekend). Answers the desk, AAA's last session and the days to run a round
    of: the app itself runs none of a day off its calendar, the replay runs one for every
    date of its file, so here too, the Friday and the Saturday as well."""
    last = SIG + 1
    nobody, listed = DATES[last + 2], DATES[last + 3]
    saturday = listed + dt.timedelta(days=1)
    assert (nobody.weekday(), listed.weekday(), saturday.weekday()) == (3, 4, 5)
    days = sorted([d for d in DATES if d != nobody] + [saturday])
    others = frame(days, closes([], len(days)))
    frames = {"AAA": cut(slow(SIG), last), "BBB": others, "CCC": others.copy()}
    cal = TradingCalendar({listed: "a holiday with a special session"}, verified=True)
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), calendar=cal,
        dormant_after=dormant_after,
    )  # fmt: skip
    assert w.session_for(at(listed)) == nobody and w.session_for(at(saturday)) == nobody
    return w, last, sorted([*DATES[SIG - 1 : last + 8], saturday])


def test_a_holiday_or_a_special_session_is_not_counted_as_a_missing_session():
    w, last, rounds = holiday_desk(dormant_after=3)
    counts = {}
    for day in rounds:
        r = w.run_round(at(day), day)
        assert r.summary["dormant"] == (["AAA"] if day == DATES[last + 7] else []), day
        if DATES[last] < day < DATES[last + 7]:
            assert r.summary["released"] == [] and by_key(w)["AAA"].status == "OPEN"
            counts[day] = quiet(w)
    # Wednesday 1, Thursday no (nobody traded), Friday no (not a trading day of the calendar),
    # Saturday no (a weekend), Monday 2, Tuesday 3 (the first round that counts), Wednesday 4;
    # released on the Thursday after
    assert list(counts.values()) == [(1, 0), (1, 0), (1, 0), (1, 0), (2, 0), (3, 1), (4, 2)]
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", NO_DATA, DATES[last + 7])
    assert a.sessions_held == 1
    assert reading(w)["dormant"]["since"] == iso(last + 7)
    # the others missed the Thursday too, and nobody else is dormant
    assert [x["series_key"] for x in w.view()["dormant"]] == ["AAA"]


def test_the_round_of_a_day_that_is_no_market_session_confirms_nothing():
    # one missing session is enough here, so the Wednesday's round is the first that counts.
    # The rounds of the Thursday (nobody traded), the Friday and the Saturday (off the
    # calendar) are not the second one; the Monday's is, and the Tuesday's round releases
    w, last, rounds = holiday_desk(dormant_after=1)
    counts = {}
    for day in rounds:
        r = w.run_round(at(day), day)
        assert r.summary["dormant"] == (["AAA"] if day >= DATES[last + 5] else []), day
        if DATES[last] < day < DATES[last + 5]:
            counts[day] = quiet(w)
    assert list(counts.values()) == [(1, 1), (1, 1), (1, 1), (1, 1), (2, 2)]
    assert by_key(w)["AAA"].exit_session == DATES[last + 5]  # the Tuesday


def test_an_evening_on_which_few_stocks_got_their_bar_is_not_counted():
    last, thin = SIG + 1, SIG + 3
    late = without(PLAIN, thin, thin)  # the bar of the thin session never comes for these
    frames = {
        "AAA": cut(slow(SIG), last), "B1": PLAIN, "B2": late, "B3": late.copy(), "B4": late.copy(),
    }  # fmt: skip
    w = wide_desk([stock(k) for k in frames], DATES, source=source_of(frames), dormant_after=3)
    for i in range(SIG - 1, last + 7):
        r = w.run_round(at(DATES[i]), DATES[i])
        # one of the four others has the thin session's bar: under half, not a market session
        assert r.summary["dormant"] == (["AAA"] if i == last + 6 else []), DATES[i]
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_session) == ("CLOSED", DATES[last + 6])  # not DATES[last + 5]
    assert "no daily price for 5 trading days" in a.note  # of six days without one


@pytest.mark.parametrize(
    ("have", "others", "sessions"),
    [(2, 4, 4), (1, 3, 3), (2, 5, 3)],  # exactly half counts; a third and two fifths do not
)
def test_a_market_session_is_a_day_on_which_half_of_the_others_have_a_bar(have, others, sessions):
    assert MARKET_SHARE == 0.5
    last, thin = SIG + 1, SIG + 3
    late = without(PLAIN, thin, thin)
    frames = {"AAA": cut(slow(SIG), last)}
    frames |= {f"H{i}": PLAIN.copy() for i in range(have)}
    frames |= {f"L{i}": late.copy() for i in range(others - have)}
    w = wide_desk([stock(k) for k in frames], DATES, source=source_of(frames))
    for i in range(SIG - 1, SIG + 6):
        w.run_round(at(DATES[i]), DATES[i])
    assert quiet(w)[0] == sessions  # four days without a bar by SIG + 5; the thin one in doubt


def test_a_stock_that_is_not_listed_yet_does_not_lower_the_share():
    last = SIG + 1
    young = PLAIN.iloc[SIG + 6 :]  # their first bar comes later: not of the list before it
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN}
    frames |= {f"Y{i}": young.copy() for i in range(3)}
    w = wide_desk([stock(k) for k in frames], DATES, source=source_of(frames))
    for i in range(SIG - 1, SIG + 5):
        w.run_round(at(DATES[i]), DATES[i])
    assert quiet(w)[0] == 3  # the one other stock that is listed has each of the three bars


def test_a_stock_that_trades_now_and_then_is_never_dormant():
    # a bar every third session only (as a stock that trades once a week has): two rounds
    # without one, each over the setting of one session, and then the bar comes
    keep = [i for i in range(N) if i <= SIG + 1 or (i - SIG - 1) % 3 == 0]
    sparse = slow(SIG).iloc[keep]
    frames = {"AAA": sparse, "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, dormant_after=1)
    counts = []
    for i in range(SIG - 2, SIG + 20):
        r = go(w, broker, i)
        assert r.summary["dormant"] == [] and r.summary["released"] == []
        counts.append(quiet(w))
    # from SIG + 2 on: one, two (both rounds counted), and the third evening has the bar
    assert counts[4:] == [(1, 1), (2, 2), (0, 0)] * 6
    assert by_key(w)["AAA"].status == "OPEN"  # and the rule goes on reading its trade


def test_a_stock_whose_bar_always_comes_a_day_late_is_never_dormant():
    frames = {"AAA": slow(SIG), "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, broker_type=Lagging, dormant_after=1)
    for i in range(SIG - 2, SIG + 1):
        go(w, broker, i)
    broker.late = frozenset({"AAA"})
    for i in range(SIG + 1, SIG + 12):
        r = go(w, broker, i)
        # every evening it lacks the session's bar and gets the one before: each new bar
        # starts the counts again, so they never pass one
        assert r.summary["dormant"] == [] and r.summary["released"] == []
        assert tries(w, i) == ROUND_TRIES  # and it is waited for, every evening
        assert w.book.states()["AAA"]["last_session"] == iso(i - 1)
        assert reading(w)["quiet"] == {
            "last_bar": iso(i - 1), "sessions": 1, "rounds": 1, "counted": iso(i),
        }  # fmt: skip
    a = by_key(w)["AAA"]  # the rule reads it a day late and manages its trade
    assert (a.status, a.entry_session, a.exit_reason) == ("OPEN", DATES[SIG + 1], None)


def test_a_bar_that_is_merely_late_on_an_evening_that_counts_releases_nothing():
    # two sessions without a bar; on the third evening the stock has traded again, but its bar
    # is published after the round's first two tries
    frames = {"AAA": without(slow(SIG), SIG + 2, SIG + 3), "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, broker_type=Lagging, dormant_after=3)
    for i in range(SIG - 2, SIG + 4):
        go(w, broker, i)
    assert quiet(w) == (2, 0) and by_key(w)["AAA"].status == "OPEN"
    broker.published, broker.late = DATES[SIG + 4], frozenset({"AAA"})
    r = try_at(w, SIG + 4, 0)
    assert not r.finished and r.summary["waiting"] == ["AAA"]
    assert r.summary["dormant"] == [] and r.summary["released"] == []
    assert reading(w)["quiet"] == {
        "last_bar": iso(SIG + 1), "sessions": 3, "rounds": 1, "counted": iso(SIG + 4),
    }  # fmt: skip
    r = try_at(w, SIG + 4, 1)  # a second try the same evening counts nothing more
    assert not r.finished and quiet(w) == (3, 1)
    broker.late = frozenset()  # the bar is there for the third try
    r = try_at(w, SIG + 4, 2)
    assert r.finished and r.summary["dormant"] == [] and r.summary["released"] == []
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_reason) == ("OPEN", None) and "quiet" not in reading(w)
    assert w.book.states()["AAA"]["last_session"] == iso(SIG + 4)


def test_a_bar_that_comes_late_on_the_release_evening_does_not_undo_the_release():
    # four sessions without a bar, two rounds counted; on the fifth evening the stock trades
    # again, but its bar comes after the round's first try: the release stands
    frames = {"AAA": without(slow(SIG), SIG + 2, SIG + 5), "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, broker_type=Lagging, dormant_after=3)
    for i in range(SIG - 2, SIG + 6):
        go(w, broker, i)
    assert quiet(w) == (4, 2) and by_key(w)["AAA"].status == "OPEN"
    broker.published, broker.late = DATES[SIG + 6], frozenset({"AAA"})
    r = try_at(w, SIG + 6, 0)
    assert r.finished and r.summary["released"] == ["AAA (no data)"]  # no longer waited for
    broker.late = frozenset()
    r = go(w, broker, SIG + 7)  # its bars are read from the next evening on
    assert r.summary["returned"] == ["AAA"] and r.summary["dormant"] == []
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", NO_DATA, DATES[SIG + 6])
    quiet_days = set(DATES[SIG + 2 : SIG + 6])
    assert stored_days(w) == [d for d in DATES[: SIG + 8] if d not in quiet_days]


def test_after_a_long_time_off_two_bad_evenings_release_nothing():
    frames = {"AAA": slow(SIG), "BBB": PLAIN, "CCC": PLAIN.copy()}
    rec = Recorder()
    w, broker = desk(frames, rec, dormant_after=3)
    for i in range(SIG - 2, SIG + 3):
        go(w, broker, i)
    assert by_key(w)["AAA"].status == "OPEN"
    # the last evening before the desk is off, every request for AAA fails: one missing
    # session, under the three asked, so that evening confirms nothing
    broker.fail = lambda key, start: key == "AAA"
    r = go(w, broker, SIG + 3)
    assert quiet(w) == (1, 0) and reading(w)["quiet"]["counted"] is None
    # the desk is off for six sessions. On its first two evenings back every request for AAA
    # fails again: the others now have all those sessions' bars, AAA's last stored bar is old
    for k, i in enumerate((SIG + 10, SIG + 11), start=1):
        r = go(w, broker, i)
        assert tries(w, i) == ROUND_TRIES and r.summary["waiting"] == ["AAA"]
        assert r.summary["dormant"] == [] and r.summary["released"] == []
        assert quiet(w) == (i - SIG - 2, k) and by_key(w)["AAA"].status == "OPEN"
    # the third evening the request works: every bar since is stored and read, the counts go
    broker.fail = lambda key, start: False
    r = go(w, broker, SIG + 12)
    assert tries(w, SIG + 12) == 1 and r.summary["dormant"] == [] and r.summary["released"] == []
    assert stored_days(w) == DATES[: SIG + 13]
    st = w.book.states()["AAA"]
    assert st["last_session"] == iso(SIG + 12) and "quiet" not in st["reading"]
    a = by_key(w)["AAA"]
    assert a.status == "OPEN" and a.exit_reason is None  # the rule's own exits decide
    assert ids(rec) == [f"pbw-AAA-{iso(SIG)}-entry"]


def test_a_desk_that_runs_now_and_then_still_releases_a_dead_stock():
    # the sessions are read from the stored bars, so evenings the desk was off count as
    # sessions; two evenings that each find five or more confirm them, the next releases
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, dormant_after=5)
    for i in range(SIG - 1, last + 1):
        go(w, broker, i)
    seen = []
    for i in (last + 3, last + 6, last + 9, last + 12):  # one evening in three
        r = go(w, broker, i)
        q = reading(w).get("quiet") or reading(w)["dormant"]
        seen.append((q["sessions"], q["rounds"], r.summary["dormant"]))
    # three missing sessions confirm nothing; six and nine are the two rounds; then released
    assert seen == [(3, 0, []), (6, 1, []), (9, 2, []), (12, 2, ["AAA"])]
    a = by_key(w)["AAA"]
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", NO_DATA, DATES[last + 12])
    assert "no daily price for 12 trading days" in a.note


def test_a_stock_the_try_never_asked_for_is_neither_counted_nor_released():
    # a round stopped at 09:00 before it reached AAA says nothing of AAA
    last = SIG + 1
    frames = {"BBB": PLAIN, "CCC": PLAIN.copy(), "AAA": cut(slow(SIG), last)}
    w, broker = desk(frames, dormant_after=3)

    def stopped(i: int):
        """One try of the evening, stopped after the two stocks before AAA were asked."""
        broker.published = DATES[i]
        n = len(broker.calls)
        r = try_at(w, i, 0, stop=lambda: len(broker.calls) - n >= 2)
        assert [c[0] for c in broker.calls[n:]] == ["BBB", "CCC"]
        assert any("stopped at 09:00" in e for e in r.summary["errors"])
        return r

    for i in range(SIG - 1, last + 4):
        go(w, broker, i)
    assert quiet(w) == (3, 1)  # three missing sessions: the first round that counts
    r = stopped(last + 4)
    assert reading(w)["quiet"] == {  # the sessions are in the bars; the evening is not counted
        "last_bar": iso(last), "sessions": 4, "rounds": 1, "counted": iso(last + 3),
    }  # fmt: skip
    assert r.summary["dormant"] == [] and not r.finished
    go(w, broker, last + 5)
    assert quiet(w) == (5, 2)  # asked again: the second round that counts
    r = stopped(last + 6)  # both counts met, a later session, but AAA was not asked: held back
    assert r.summary["released"] == [] and r.summary["dormant"] == []
    assert by_key(w)["AAA"].status == "OPEN" and quiet(w) == (6, 2)
    r = go(w, broker, last + 7)  # asked, and still no bar: released
    assert r.summary["released"] == ["AAA (no data)"]
    assert by_key(w)["AAA"].exit_session == DATES[last + 7]


# ── 4. a stale stock ─────────────────────────────────────────────────────────
def test_a_stale_stock_with_an_open_trade_is_released_and_read_again_once_refetched():
    raw = slow(SIG)
    w, broker = desk({"AAA": raw, "BBB": PLAIN, "CCC": PLAIN.copy()}, dormant_after=3)
    for i in range(SIG - 2, SIG + 3):
        go(w, broker, i)
    booked = by_key(w)["AAA"]
    assert (booked.status, booked.entry_session) == ("OPEN", DATES[SIG + 1])
    # the broker re-bases the history (a split) and its whole refetch fails every evening:
    # stale, not read, and no bar of it is stored after SIG + 2
    broker.frames["AAA"] = halved(raw)
    broker.fail = lambda key, start: key == "AAA" and start == FULL_FROM
    for i in range(SIG + 3, SIG + 7):
        r = go(w, broker, i)
        assert r.summary["stale"] == ["AAA"] and r.summary["dormant"] == []
        assert tries(w, i) == 1  # a stale stock is not waited for
        assert by_key(w)["AAA"].status == "OPEN"
    assert quiet(w) == (4, 2)
    r = go(w, broker, SIG + 7)  # four sessions with no new stored bar, and none this evening
    assert r.summary["released"] == ["AAA (no data)"]
    assert r.summary["dormant"] == ["AAA"] and r.summary["stale"] == ["AAA"]
    a = by_key(w)["AAA"]
    c = raw["close"].to_numpy(float)
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", NO_DATA, DATES[SIG + 7])
    # at the last STORED close, on the basis its entry was booked on: never the halved prices
    assert a.exit_paise == rupees_to_paise(c[SIG + 2])
    assert (a.entry_paise, a.qty) == (booked.entry_paise, booked.qty)
    st = w.book.states()["AAA"]
    assert st["stale"] and st["reading"]["dormant"]["last_bar"] == iso(SIG + 2)
    # the refetch works at last: the history is replaced and the stock is read again
    broker.fail = lambda key, start: False
    r = go(w, broker, SIG + 8)
    assert r.summary["refetched"] == ["AAA"] and r.summary["returned"] == ["AAA"]
    assert r.summary["dormant"] == [] and r.summary["stale"] == [] and r.finished
    st = w.book.states()["AAA"]
    assert not st["stale"] and st["last_session"] == iso(SIG + 8)
    assert "dormant" not in st["reading"] and w.view()["dormant"] == []
    (again,) = w.book.all()  # a released trade stays released, on the basis it was booked on
    assert (again.status, again.exit_reason) == ("CLOSED", NO_DATA)
    assert (again.entry_paise, again.exit_paise, again.qty) == (a.entry_paise, a.exit_paise, a.qty)


def test_a_trade_is_released_on_the_basis_of_the_bars_it_is_closed_on():
    raw = slow(SIG)
    last = SIG + 3
    # AAA is asked after stocks that have the session's bar (the round's whole refetch of a
    # history that disagrees is for a session that is there)
    w, broker = desk({"BBB": PLAIN, "CCC": PLAIN.copy(), "AAA": raw}, dormant_after=3)
    for i in range(SIG - 2, last + 1):
        go(w, broker, i)
    booked = by_key(w)["AAA"]
    assert booked.status == "OPEN"
    # a split, and the stock stops trading that very day: the broker serves the whole history
    # halved and no new bar, so the stored bars change basis and no session is read after it
    broker.frames["AAA"] = cut(halved(raw), last)
    for i in range(last + 1, last + 5):
        go(w, broker, i)
        t = by_key(w)["AAA"]
        assert (t.status, t.entry_paise, t.qty) == ("OPEN", booked.entry_paise, booked.qty)
    stored = w._stored("AAA")["close"].to_numpy(float)
    assert abs(stored[-1] - raw["close"].iloc[last] / 2) < 1e-9  # the stored bars: halved
    r = go(w, broker, last + 5)
    a = by_key(w)["AAA"]
    assert r.summary["released"] == ["AAA (no data)"] and r.summary["rebased"] == ["AAA"]
    assert (a.status, a.exit_reason) == ("CLOSED", NO_DATA)
    # entry and exit on the same (new) basis: the result of the three sessions, not a -50%
    assert a.entry_paise == round(booked.entry_paise * 0.5) and a.qty == 2 * booked.qty
    assert a.exit_paise == rupees_to_paise(raw["close"].iloc[last] / 2)
    assert -1.0 < a.net_pct < 1.0
    assert "re-based" in a.note and "no daily price for 5 trading days" in a.note


# ── 5. the round's tries, and bars that return ───────────────────────────────
def test_a_dormant_stock_no_longer_costs_the_round_its_extra_tries():
    last = SIG + 1
    frames = {"AAA": cut(PLAIN, last), "BBB": PLAIN.copy(), "CCC": PLAIN.copy()}
    w, broker = desk(frames, dormant_after=3)

    def evening(i: int) -> dict:
        """The desk's polls of an evening: a try of the round is due every 30 minutes."""
        broker.published = DATES[i]
        for hhmm in ("16:00", "16:30", "17:00", "17:30", "18:00", "18:30"):
            w.maybe_round(at(DATES[i], hhmm))
        return w.book.round(DATES[i])

    for i in range(SIG - 1, last + 1):
        assert evening(i)["tries"] == 1
    for i in range(last + 1, last + 5):  # not dormant yet: all four tries wait for its bar
        n = len(broker.calls)
        row = evening(i)
        assert row["tries"] == ROUND_TRIES and row["finished_at"] is not None
        assert row["summary"]["dormant"] == [] and row["summary"]["waiting"] == ["AAA"]
        assert sorted(c[0] for c in broker.calls[n:]) == ["AAA"] * ROUND_TRIES + ["BBB", "CCC"]
    for i in (last + 5, last + 6):  # dormant: the round is done at its first try
        n = len(broker.calls)
        row = evening(i)
        assert row["tries"] == 1 and row["finished_at"] is not None
        assert row["summary"]["dormant"] == ["AAA"] and row["summary"]["waiting"] == []
        # every stock was asked once that evening, the dormant one too: its bars may return
        assert sorted(c[0] for c in broker.calls[n:]) == ["AAA", "BBB", "CCC"]
    assert w.book.all() == []


def test_a_desk_that_is_shut_after_one_try_each_evening_still_releases():
    # no evening's round is ever finished (the stock is waited for and the desk is shut):
    # the release needs no particular try, so the fifth evening's first try releases
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, dormant_after=3)
    for i in range(SIG - 1, last + 5):
        broker.published = DATES[i]
        r = try_at(w, i, 0)
        assert r.finished == (i <= last) and r.summary["dormant"] == []
    broker.published = DATES[last + 5]
    r = try_at(w, last + 5, 0)
    assert r.finished and r.summary["released"] == ["AAA (no data)"]
    assert by_key(w)["AAA"].exit_session == DATES[last + 5]


def test_bars_that_return_are_fetched_from_the_last_stored_bar_and_the_stock_is_read_again():
    gone, back = SIG + 2, SIG + 62  # 60 sessions: longer than the 45 days a round fetches
    path = [(SIG, -0.10), (SIG + 1, 0.001), (back + 3, -0.085), (back + 4, 0.001)]
    frames = {"AAA": frame(DATES, closes(path, N)), "BBB": PLAIN, "CCC": PLAIN.copy()}
    rec = Recorder()
    w, broker = desk(frames, rec, dormant_after=3)
    for i in range(SIG - 2, gone + 1):
        go(w, broker, i)
    assert by_key(w)["AAA"].status == "OPEN"
    # AAA trades all along, but every request for it fails (as for a stock the broker no longer
    # knows under that id): no new bar is stored
    broker.fail = lambda key, start: key == "AAA"
    for i in range(gone + 1, back):
        r = go(w, broker, i)
        assert any(e.startswith("AAA: ConnectionError") for e in r.summary["errors"])
        assert r.summary["dormant"] == (["AAA"] if i >= gone + 5 else [])
        assert tries(w, i) == (ROUND_TRIES if i <= gone + 4 else 1)
        assert r.summary["released"] == (["AAA (no data)"] if i == gone + 5 else [])
    released = by_key(w)["AAA"]
    c = frames["AAA"]["close"].to_numpy(float)
    assert (released.status, released.exit_reason) == ("CLOSED", NO_DATA)
    assert released.exit_paise == rupees_to_paise(c[gone])
    assert stored_days(w)[-1] == DATES[gone]

    broker.fail = lambda key, start: False
    n = len(broker.calls)
    r = go(w, broker, back)
    (call,) = [x for x in broker.calls[n:] if x[0] == "AAA"]
    # from ten days before its last stored bar, not from 45 days before the session
    assert FETCH_BACK == dt.timedelta(days=10)
    assert call[1] <= DATES[gone] - dt.timedelta(days=10) < DATES[back] - dt.timedelta(days=45)
    assert stored_days(w) == DATES[: back + 1]  # no hole is left in its history
    assert r.summary["returned"] == ["AAA"] and r.summary["dormant"] == [] and r.finished
    st = w.book.states()["AAA"]
    assert st["last_session"] == iso(back) and "dormant" not in st["reading"]
    # the sessions in between were read late and gave no signal; the released trade stays so
    (still,) = w.book.all()
    assert (still.status, still.exit_reason, still.exit_paise) == (
        "CLOSED",
        NO_DATA,
        released.exit_paise,
    )
    # and the rule reads the stock as any other: its next signal is a new trade, on time
    for i in range(back + 1, back + 5):
        r = go(w, broker, i)
        assert r.summary["dormant"] == [] and tries(w, i) == 1
    new = [t for t in w.book.all() if t.signal_date == DATES[back + 3]]
    assert [(t.series_key, t.tier, t.late, t.status) for t in new] == [
        ("AAA", "cap", False, "OPEN")
    ]
    assert ids(rec) == [f"pbw-AAA-{iso(SIG)}-entry", f"pbw-AAA-{iso(back + 3)}-entry"]


def test_a_dormant_stock_whose_bars_return_a_day_late_wakes_all_the_same():
    last, back = SIG + 1, SIG + 10
    frames = {"AAA": without(PLAIN, last + 1, back - 2), "BBB": PLAIN.copy(), "CCC": PLAIN.copy()}
    w, broker = desk(frames, broker_type=Lagging, dormant_after=3)
    for i in range(SIG - 1, back - 1):
        r = go(w, broker, i)
    assert r.summary["dormant"] == ["AAA"] and tries(w, back - 2) == 1
    broker.late = frozenset({"AAA"})  # it trades again from `back - 1`, its bars a day late
    r = go(w, broker, back - 1)  # that evening the answer still ends at its old last bar
    assert r.summary["dormant"] == ["AAA"] and r.summary["returned"] == []
    assert tries(w, back - 1) == 1
    for i in range(back, back + 5):
        r = go(w, broker, i)
        assert r.summary["returned"] == (["AAA"] if i == back else [])
        assert r.summary["dormant"] == [] and tries(w, i) == ROUND_TRIES  # waited for again
        st = w.book.states()["AAA"]
        assert "dormant" not in st["reading"] and quiet(w) == (1, 0)
        assert st["last_session"] == iso(i - 1)
    assert w.view()["dormant"] == []


def test_an_exit_signalled_but_never_filled_is_released_with_its_signal_kept():
    full = frame(DATES, drop_then(SIG, 0.10, 0.03, N))  # a rebound: out above its SMA20
    rec = Recorder()
    w, broker = desk({"AAA": full, "BBB": PLAIN}, rec, dormant_after=3)
    x = SIG - 3
    while "AAA" not in by_key(w) or by_key(w)["AAA"].status != "EXIT_PENDING":
        x += 1
        go(w, broker, x)
    pending = by_key(w)["AAA"]
    assert (pending.exit_signal_date, pending.exit_reason) == (DATES[x], "SMA20")
    broker.frames["AAA"] = cut(full, x)  # the stock stops trading before the sale's session
    for i in range(x + 1, x + 5):
        go(w, broker, i)
        assert by_key(w)["AAA"].status == "EXIT_PENDING"
    r = go(w, broker, x + 5)
    a = by_key(w)["AAA"]
    assert r.summary["released"] == ["AAA (no data)"]
    assert (a.status, a.exit_reason) == ("CLOSED", NO_DATA)
    assert (a.exit_signal_date, a.exit_session) == (DATES[x], DATES[x + 5])  # its signal kept
    assert a.sessions_held == pending.sessions_held
    assert a.exit_paise == rupees_to_paise(float(full["close"].iloc[x]))
    assert f"its exit signal of {DATES[x]:%d %b %Y} (SMA20) was never filled" in a.note
    # told to buy and, on the rule's own signal, to sell; nothing after the release
    assert [n.payload["delivery"] for n in rec.sent] == ["entry", "exit"]
    assert w.book.score()["all"]["released"]["no_data"] == 1


def old_release_among_newer_closes(tmp_path):
    """AAA's signal is the oldest and its release the newest end; twenty-one later trades
    close by the rule before it."""
    last = SIG + 1
    quick = {f"Q{i:02d}": frame(DATES, drop_then(SIG + 1, 0.10, 0.03, N)) for i in range(21)}
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, **quick}
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), path=tmp_path / "w.db",
        dormant_after=5,
    )  # fmt: skip
    for i in range(SIG - 1, last + 8):
        w.run_round(at(DATES[i]), DATES[i])
    return w


def test_the_trades_that_are_over_are_listed_by_the_session_they_ended_on(tmp_path):
    # by its id (the order of the signals) AAA would come last and fall off a page of twenty
    w = old_release_among_newer_closes(tmp_path)
    done = {t.series_key: t for t in w.book.all() if t.status == "CLOSED"}
    assert len(done) == 22 and done["AAA"].exit_reason == NO_DATA
    assert done["AAA"].id == min(t.id for t in done.values())  # the oldest signal
    assert all(t.exit_session < done["AAA"].exit_session for k, t in done.items() if k != "AAA")
    ended = w.book.closed(20)
    assert ended[0].series_key == "AAA" and len(ended) == 20
    assert [t.exit_session for t in ended] == sorted((t.exit_session for t in ended), reverse=True)
    assert w.view()["closed"][0]["series_key"] == "AAA"


@needs_node
def test_a_release_after_twenty_later_closes_is_on_the_page(tmp_path):
    w = old_release_among_newer_closes(tmp_path)
    html = js("wideBlock(P, false)", w.view())
    table = html.split("Closed trades")[1].split("Closest to a signal")[0]
    assert "<b>AAA Ltd</b>" in table and "no data (not the rule's exit)" in table
    assert table.count("<tr><td><b>Q") == 19  # the page shows twenty rows: AAA and nineteen


# ── 6. an exit held for review ───────────────────────────────────────────────
def test_an_exit_held_for_review_is_released_as_review_and_books_no_money(tmp_path):
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)  # a 1:1 split the broker left unadjusted
    rec = Recorder()
    w, broker = desk(
        {"AAA": raw, "BBB": slow(SIG + 9)}, rec, cap_open=1, dormant_after=3,
        path=tmp_path / "w.db",
    )  # fmt: skip
    for i in range(SIG - 2, SIG + 9):
        r = go(w, broker, i)
        assert r.summary["released"] == []
        assert r.summary["held"] == (["AAA"] if i >= SIG + 5 else [])
        if i >= SIG + 5:  # an evening counts from the third session it is held on
            assert reading(w)["trade"]["held_rounds"] == max(0, i - SIG - 6)
    held = by_key(w)["AAA"]
    assert held.status == "OPEN" and "exit held for review" in held.note
    r = go(w, broker, SIG + 9)  # held four sessions, two rounds counted: the next one releases
    assert r.summary["released"] == ["AAA (held for review)"] and r.summary["held"] == []
    assert r.summary["dormant"] == []  # the stock trades: it is not dormant
    t = by_key(w)
    a = t["AAA"]
    assert (a.status, a.exit_reason) == ("CLOSED", REVIEW)
    assert (a.exit_signal_date, a.exit_session, a.sessions_held) == (
        DATES[SIG + 9],
        DATES[SIG + 9],
        9,
    )
    # no figure is written: the half-price close against the old entry would be a wrong one
    assert (a.exit_paise, a.gross_paise, a.charges_paise, a.dp_paise, a.net_paise, a.net_pct) == (
        (None,) * 6
    )
    row = next(x for x in rows(w.engine, wide_trades) if x["series_key"] == "AAA")
    assert all(
        row[k] is None
        for k in ("exit_paise", "gross_paise", "charges_paise", "dp_paise", "net_paise", "net_pct")
    )
    assert a.note == (
        f"released on {DATES[SIG + 9]:%d %b %Y}: its exit was held for review for 5 sessions. "
        f"Its result is not booked: the price basis is in doubt. Check by hand; {held.note}"
    )
    # the place is free for that evening's new signal; no SELL was ever told
    assert (t["BBB"].tier, t["BBB"].signal_date) == ("cap", DATES[SIG + 9])
    assert [n.payload["delivery"] for n in rec.sent] == ["entry", "entry"]
    # the score counts it, with no money; what the route answers is plain JSON
    for tier in ("all", "cap"):
        score = w.book.score()[tier]
        assert score["trades"] == 0 and score["released"] == {
            "trades": 1, "booked": 0, "winners": 0, "net": 0.0, "no_data": 0, "review": 1,
        }  # fmt: skip
    view = w.view()
    assert json.loads(json.dumps(view, allow_nan=False)) == view
    (shown,) = view["closed"]
    assert (shown["exit_reason"], shown["exit"], shown["net"], shown["net_pct"]) == (
        REVIEW,
        None,
        None,
        None,
    )
    # no entry is read on the close it was released on, nor across the fall after it
    r = go(w, broker, SIG + 10)
    assert r.summary["jumps"] == ["AAA"] and [x.series_key for x in w.book.all()] == ["AAA", "BBB"]
    assert RELEASES == (NO_DATA, REVIEW)


@needs_node
def test_the_page_says_result_not_booked_for_a_review_release(tmp_path):
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)
    frames = {"AAA": raw, "BBB": PLAIN}
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), path=tmp_path / "w.db",
        dormant_after=3,
    )  # fmt: skip
    for i in range(SIG - 1, SIG + 10):
        w.run_round(at(DATES[i]), DATES[i])
    assert by_key(w)["AAA"].exit_reason == REVIEW
    html = js("wideBlock(P, false)", w.view())
    for text in (
        "released without the rule's exit: AAA (held for review)",
        "1 trade released (0 for no data, at the last known close; 1 held for review, result "
        "not booked; 1 within the cap).",
        "<td>held for review (not the rule's exit)</td>",
        '<td class="n ">result not booked</td>',
        "Its result is not booked: the price basis is in doubt. Check by hand",
    ):
        assert text in html, text
    for bad in ("null", "NaN", "undefined", ", net "):
        assert bad not in html, bad


def test_an_exit_is_held_for_as_many_sessions_as_the_setting_says():
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)
    w, broker = desk({"AAA": raw, "BBB": PLAIN}, dormant_after=5)
    for i in range(SIG - 2, SIG + 11):
        r = go(w, broker, i)
        assert r.summary["released"] == []  # held six sessions, the fifth and sixth counted
    r = go(w, broker, SIG + 11)  # the round after
    a = by_key(w)["AAA"]
    assert r.summary["released"] == ["AAA (held for review)"]
    assert (a.exit_reason, a.exit_session) == (REVIEW, DATES[SIG + 11])
    assert "held for review for 7 sessions" in a.note


def test_an_exit_held_for_review_is_not_released_in_a_hurry_after_time_off():
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)
    w, broker = desk({"AAA": raw, "BBB": PLAIN}, dormant_after=3)
    for i in range(SIG - 2, SIG + 4):
        go(w, broker, i)
    # the desk is off while the split happens; its first round back reads eleven sessions,
    # ten of them with the exit held: far over three sessions, but one evening
    r = go(w, broker, SIG + 14)
    assert set(r.summary["held"]) == {"AAA"} and r.summary["released"] == []
    assert by_key(w)["AAA"].status == "OPEN" and reading(w)["trade"]["held_rounds"] == 1
    r = go(w, broker, SIG + 15)
    assert r.summary["held"] == ["AAA"] and r.summary["released"] == []
    assert reading(w)["trade"]["held_rounds"] == 2
    # the next evening is missed, and the round after reads two sessions: the third evening.
    # The trade is released on the newest of them, not while catching up
    r = go(w, broker, SIG + 17)
    a = by_key(w)["AAA"]
    assert r.summary["released"] == ["AAA (held for review)"]
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", REVIEW, DATES[SIG + 17])
    assert a.sessions_held == 17 and "held for review for 13 sessions" in a.note


def test_an_exit_held_for_review_is_released_for_a_stock_read_a_day_late():
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)
    w, broker = desk({"AAA": raw, "BBB": PLAIN}, broker_type=Lagging, dormant_after=3)
    for i in range(SIG - 2, SIG + 2):
        go(w, broker, i)
    broker.late = frozenset({"AAA"})  # from here on each round reads AAA's session before
    for i in range(SIG + 2, SIG + 10):
        r = go(w, broker, i)
        assert r.summary["released"] == []
    assert by_key(w)["AAA"].status == "OPEN" and reading(w)["trade"]["held_rounds"] == 2
    r = go(w, broker, SIG + 10)  # the session of SIG + 9: the fifth held, the third evening
    a = by_key(w)["AAA"]
    assert r.summary["released"] == ["AAA (held for review)"]
    assert (a.status, a.exit_reason, a.exit_session) == ("CLOSED", REVIEW, DATES[SIG + 9])


# ── 7. twice, a restart, the switch ──────────────────────────────────────────
def test_a_round_run_again_or_a_restart_changes_nothing_and_keeps_the_mark(tmp_path):
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, "CCC": PLAIN.copy()}
    eng = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    METADATA.create_all(eng)
    broker = Broker(dict(frames))

    def start():  # a fresh desk on the same store: a restart
        return wide_desk(
            [stock(k) for k in frames], DATES, engine=eng, fetch=broker.fetch,
            data_api_active=lambda: True, sleep=lambda s: None, dormant_after=3,
        )  # fmt: skip

    w = start()
    for i in range(SIG - 1, last + 4):
        go(w, broker, i)
    assert quiet(w) == (3, 1)
    w = start()  # a restart in the middle of the count: both counts are in the store
    go(w, broker, last + 4)
    assert quiet(w) == (4, 2) and by_key(w)["AAA"].status == "OPEN"
    w = start()  # and one between the second round that counts and the release
    go(w, broker, last + 5)
    assert by_key(w)["AAA"].exit_reason == NO_DATA
    before = rows(eng, wide_trades), rows(eng, wide_state), len(broker.calls)
    again = w.run_round(at(DATES[last + 5], "21:30"), DATES[last + 5])  # the same evening
    assert again.finished and again.summary["released"] == ["AAA (no data)"]
    assert (rows(eng, wide_trades), rows(eng, wide_state), len(broker.calls)) == before
    w = start()
    assert [x["series_key"] for x in w.view()["dormant"]] == ["AAA"]  # the mark is in the store
    trades = rows(eng, wide_trades)
    r = go(w, broker, last + 6)
    assert r.summary["released"] == [] and r.summary["dormant"] == ["AAA"]
    assert tries(w, last + 6) == 1 and rows(eng, wide_trades) == trades  # released once


def test_with_dormant_after_0_or_no_other_stock_nothing_is_ever_dormant():
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN}
    w, broker = desk(frames, dormant_after=0)  # the switch off: the desk as before US-103
    for i in range(SIG - 1, last + 16):
        r = go(w, broker, i)
        assert r.summary["dormant"] == [] and r.summary["released"] == []
        # its missing bar is waited for every evening
        assert tries(w, i) == (1 if i <= last else ROUND_TRIES)
    assert by_key(w)["AAA"].status == "OPEN" and w.view()["dormant"] == []
    assert "quiet" not in reading(w)
    # one stock alone: a dead stock cannot be told from a dead feed, so nothing is released
    alone = {"AAA": frames["AAA"]}
    w = wide_desk([stock("AAA")], DATES, source=source_of(alone))
    for i in range(SIG - 1, last + 16):
        assert w.run_round(at(DATES[i]), DATES[i]).summary["dormant"] == []
    assert by_key(w)["AAA"].status == "OPEN"
    # switched off after a stock was marked: the mark goes, the stock is waited for again
    w, broker = desk(frames, dormant_after=3)
    for i in range(SIG - 1, last + 6):
        r = go(w, broker, i)
    assert [x["series_key"] for x in w.view()["dormant"]] == ["AAA"]
    w.dormant_after = 0
    r = go(w, broker, last + 6)
    assert r.summary["dormant"] == [] and r.summary["returned"] == []
    assert tries(w, last + 6) == ROUND_TRIES and r.summary["waiting"] == ["AAA"]
    assert w.view()["dormant"] == []
    assert by_key(w)["AAA"].exit_reason == NO_DATA  # what was released stays released
    # an exit held for review stays held with the switch off
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)
    w, broker = desk({"AAA": raw}, dormant_after=0)
    for i in range(SIG - 2, SIG + 25):
        r = go(w, broker, i)
    assert by_key(w)["AAA"].status == "OPEN" and r.summary["held"] == ["AAA"]


def test_a_dormant_stock_whose_bars_cannot_be_read_keeps_its_mark():
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, "CCC": PLAIN.copy()}
    w, broker = desk(frames, dormant_after=3)
    for i in range(SIG - 1, last + 6):
        go(w, broker, i)
    mark = dict(reading(w)["dormant"])
    load = w._load
    w._load = lambda keys: {k: v for k, v in load(keys).items() if k != "AAA"}  # a failed read
    r = go(w, broker, last + 6)
    assert r.summary["returned"] == [] and reading(w)["dormant"] == mark
    w._load = load
    r = go(w, broker, last + 7)
    assert r.summary["dormant"] == ["AAA"] and tries(w, last + 7) == 1
    assert reading(w)["dormant"] == mark


# ── 8. the setting ───────────────────────────────────────────────────────────
def test_the_slot_key_dormant_after_is_read_strictly_and_reaches_the_wide_desk(tmp_path):
    def read(body: str) -> WideCfg:
        p = tmp_path / "desk.yaml"
        p.write_text(body)
        return load_desk_config(p).wide

    head = "defaults:\n  stock_strategies:\n"
    slot = "      - {name: pullback, timeframe: 1d, product: delivery, %s}\n"
    assert read(head + slot % "wide: true").dormant_after == 10  # the default
    assert read(head + slot % "wide: true, dormant_after: 4").dormant_after == 4
    assert read(head + slot % "dormant_after: 0") == WideCfg(dormant_after=0)  # never
    for bad in (
        "dormant_after: -1",
        "dormant_after: 2.5",
        "dormant_after: true",
        'dormant_after: "3"',
    ):
        with pytest.raises(ValueError, match="dormant_after"):
            read(head + slot % bad)
    cfg = load_desk_config()
    assert cfg.wide.dormant_after == 10  # as shipped
    four = dataclasses.replace(cfg, wide=dataclasses.replace(cfg.wide, dormant_after=4))
    for c, want in ((cfg, 10), (four, 4)):
        d = DeliveryDesk(
            create_engine("sqlite://"), c, COSTS, SessionCfg(), TradingCalendar({}, verified=True),
            None, dt.datetime(2026, 1, 1, tzinfo=IST), background=False,
        )  # fmt: skip
        assert d.wide.dormant_after == want


def test_a_setting_under_five_is_said_once_at_the_start(caplog):
    frames = {"AAA": slow(SIG), "BBB": PLAIN}
    said = {}
    for n in (0, 1, 4, 5, 10):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="onelot.delivery.wide"):
            wide_desk([stock(k) for k in frames], DATES, source=source_of(frames), dormant_after=n)
        said[n] = [r.getMessage() for r in caplog.records if "dormant_after" in r.getMessage()]
    assert said[0] == [] and said[5] == [] and said[10] == []  # off, or five and more: silent
    for n in (1, 4):
        (line,) = said[n]
        assert f"dormant_after is {n}" in line
        assert "under 5 would release stocks that still trade" in line
        assert "the study's longest hole is 5 sessions" in line


# ── 9. the page and the route ────────────────────────────────────────────────
def closed_row(key: str, **kw) -> dict:
    row = {
        "series_key": key, "name": key, "qty": 7, "tier": "cap", "status": "CLOSED",
        "entry_session": "2026-11-20", "exit_session": "2026-12-10", "exit_reason": "STOP",
        "net_pct": -1.2, "net": -120.0, "note": None,
    }  # fmt: skip
    return {**row, **kw}


@needs_node
def test_the_block_names_the_dormant_stocks_and_keeps_the_released_trades_apart():
    plain = js("wideBlock(P, false)", VIEW)  # a view with nothing released: no new line
    assert "No daily price" not in plain and "Not in the score" not in plain
    assert "released without" not in plain and "bars are back" not in plain
    released = {"trades": 3, "booked": 2, "winners": 1, "net": -310.75, "no_data": 2, "review": 1}
    mark = {"since": "2026-12-10", "last_bar": "2026-11-26", "sessions": 12, "rounds": 2}
    view = {
        **VIEW,
        "dormant_after": 10,
        "dormant": [{"series_key": "XYZ", "name": "XYZ <i>Ltd</i>", **mark}],
        "score": {
            "all": {**VIEW["score"]["all"], "released": released, "cancelled": 1},
            "cap": {**VIEW["score"]["cap"], "released": {**released, "trades": 2}, "cancelled": 0},
        },
        "last_round": {
            **VIEW["last_round"],
            "dormant": ["XYZ"],
            "released": ["XYZ (no data)", "PQR (held for review)"],
            "returned": ["LMN"],
        },
        "closed": [
            closed_row("XYZ", exit_reason="NO_DATA", note="released on 10 Dec 2026: no daily price"),
            closed_row(
                "PQR", exit_reason="REVIEW", net_pct=None, net=None,
                note="released on 10 Dec 2026: its exit was held for review",
            ),
            closed_row(
                "CAN", status="CANCELLED", entry_session=None, exit_reason="NO_DATA", net_pct=None,
                net=None, note="entry cancelled on 10 Dec 2026: no daily price",
            ),
            closed_row("OWN", note="the note of a rule exit"),  # a rule exit: as before
        ],
    }  # fmt: skip
    html = js("wideBlock(P, false)", view)
    for text in (
        "No daily price for 10 trading days or more (a suspension, a merger or a delisting?)",
        "Not waited for; read again when bars return: XYZ &lt;i&gt;Ltd&lt;/i&gt; (last bar 2026-11-26)",
        "released without the rule's exit: XYZ (no data), PQR (held for review)",
        "dormant, not waited for: XYZ",
        "daily bars are back: LMN",
        "Not in the score (not the rule's own exits): 3 trades released (2 for no data, at the "
        "last known close; 1 held for review, result not booked; 2 within the cap), net "
        "−₹311 on the 2 booked; 1 entry cancelled before the fill (no data).",
        "<td>no data (not the rule's exit)</td>",
        "<td>held for review (not the rule's exit)</td>",
        '<td class="n ">result not booked</td>',
        "<td>entry cancelled: no data</td>",
        "<td>never filled; cancelled 2026-12-10</td>",
        "released on 10 Dec 2026: no daily price",
        "released on 10 Dec 2026: its exit was held for review",
        "entry cancelled on 10 Dec 2026: no daily price",
        "<td>STOP</td>",
    ):
        assert text in html, text
    assert "<i>Ltd</i>" not in html and "the note of a rule exit" not in html
    assert "Dormant" not in html and "null" not in html and "NaN" not in html
    # the Live Desk's compact block keeps the two lines and leaves out the long tables
    compact = js("wideBlock(P, true)", view)
    assert "No daily price for 10 trading days or more" in compact
    assert "Not in the score" in compact and "Closed trades" not in compact
    # one released trade and no cancelled entry: singular, and no second clause
    one = {**released, "trades": 1, "booked": 1, "no_data": 1, "review": 0}
    view["score"] = {"all": {"released": one, "cancelled": 0}, "cap": {"released": one}}
    html = js("wideBlock(P, true)", view)
    assert "1 trade released (1 for no data, at the last known close; 0 held for review" in html
    assert "on the 1 booked." in html and "cancelled before" not in html


@needs_node
def test_the_dormant_list_shows_ten_names_and_counts_the_rest():
    names = [
        {"series_key": f"S{i:02d}", "name": f"S{i:02d} Ltd", "last_bar": "2026-11-26"}
        for i in range(13)
    ]
    html = js("wideBlock(P, true)", {**VIEW, "dormant_after": 10, "dormant": names})
    assert "S09 Ltd (last bar 2026-11-26) and 3 more</div>" in html
    assert "S10 Ltd" not in html and html.count("(last bar 2026-11-26)") == 10
    ten = js("wideBlock(P, true)", {**VIEW, "dormant_after": 10, "dormant": names[:10]})
    assert "S09 Ltd (last bar 2026-11-26)</div>" in ten and " more</div>" not in ten


def test_the_route_carries_the_dormant_stocks_and_the_released_trades(tmp_path):
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN}
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), path=tmp_path / "w.db",
        dormant_after=3,
    )  # fmt: skip
    for i in range(SIG - 1, last + 6):
        w.run_round(at(DATES[i]), DATES[i])
    version = w.book.version
    got = client_for(SimpleNamespace(delivery=SimpleNamespace(wide=w))).get("/api/delivery/wide")
    body = got.json()
    assert got.status_code == 200 and w.book.version == version  # a read: nothing written
    assert body["dormant_after"] == 3 and body["open"] == []
    assert body["dormant"] == [
        {
            "series_key": "AAA", "name": "AAA Ltd", "since": iso(last + 5),
            "last_bar": iso(last), "sessions": 5, "rounds": 2,
        }
    ]  # fmt: skip
    (row,) = body["closed"]
    assert (row["series_key"], row["status"], row["exit_reason"]) == ("AAA", "CLOSED", "NO_DATA")
    assert body["score"]["all"]["released"]["no_data"] == 1 and body["score"]["all"]["trades"] == 0
    assert body["last_round"]["dormant"] == ["AAA"]
    assert body["last_round"]["released"] == ["AAA (no data)"]
    assert body["last_round"]["returned"] == []


@needs_node
def test_the_page_shows_what_the_product_answers_for_a_released_trade(tmp_path):
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN}
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), path=tmp_path / "w.db",
        dormant_after=3,
    )  # fmt: skip
    for i in range(SIG - 1, last + 6):
        w.run_round(at(DATES[i]), DATES[i])
    html = js("wideBlock(P, false)", w.view())
    for text in (
        "No daily price for 3 trading days or more",
        f"AAA Ltd (last bar {iso(last)})",
        "released without the rule's exit: AAA (no data)",
        "dormant, not waited for: AAA",
        "1 trade released (1 for no data, at the last known close; 0 held for review, result not "
        "booked; 1 within the cap), net ",
        "on the 1 booked.",
        "<td>no data (not the rule's exit)</td>",
        f"no daily price for 5 trading days after {DATES[last]:%d %b %Y}",
        "closed at its last known close",
        "No open wide-list trade.",
    ):
        assert text in html, text
    assert "evenings in a row" not in html and "a mark" not in html


# ── 9. three changes a reviewer made to the code that no test noticed ────────
def test_a_round_with_no_broker_and_no_replay_source_counts_and_releases_nothing(tmp_path):
    # stored bars alone are nobody's answer: AAA stopped after SIG + 1 holding an open trade,
    # BBB and CCC trade every session, and no evening round may count or release AAA
    last = SIG + 1
    frames = {"AAA": cut(slow(SIG), last), "BBB": PLAIN, "CCC": PLAIN.copy()}
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    first, broker = desk(frames, engine=eng, dormant_after=3)
    for i in range(SIG - 1, last + 1):  # the trade is opened through the broker
        go(first, broker, i)
    assert by_key(first)["AAA"].status == "OPEN"
    for k in ("BBB", "CCC"):  # the bars of the sessions after, as the store comes to hold them
        save_bars(eng, k, "1d", frames[k], "dhan")
    w = wide_desk([stock(k) for k in frames], DATES, engine=eng, dormant_after=3)
    for i in range(last + 1, SIG + 12):
        r = w.run_round(at(DATES[i]), DATES[i])
        assert r.summary["note"] == "no Dhan data: the round read the stored daily bars only"
        assert r.summary["released"] == [] and r.summary["dormant"] == [], i
        assert by_key(w)["AAA"].status == "OPEN", i
    assert quiet(w)[1] == 0 and "dormant" not in reading(w)  # no evening was ever counted


def test_switching_dormant_after_to_0_keeps_a_held_trade_held():
    raw = unadjusted(slow(SIG), SIG + 5, 0.5)  # an unadjusted split: the exit is held
    w, broker = desk({"AAA": raw, "BBB": PLAIN}, dormant_after=3)
    for i in range(SIG - 2, SIG + 9):
        r = go(w, broker, i)
        assert r.summary["released"] == []
    assert reading(w)["trade"]["held_rounds"] == 2  # two held evenings are counted
    w.dormant_after = 0  # the switch: the desk as before, nothing is ever released
    for i in (SIG + 9, SIG + 10):
        r = go(w, broker, i)
        assert r.summary["released"] == [] and r.summary["held"] == ["AAA"]
        a = by_key(w)["AAA"]
        assert a.status == "OPEN" and a.exit_reason is None and "exit held for review" in a.note
        assert reading(w)["trade"]["held_rounds"] == 0 and "released" not in reading(w)["trade"]


def test_two_trades_that_end_on_the_same_session_are_listed_with_the_higher_id_first():
    last = SIG + 1
    frames = {
        "AAA": cut(slow(SIG), last),
        "CCC": cut(slow(SIG), last),
        "BBB": PLAIN,
    }
    w, broker = desk(frames, dormant_after=3)
    for i in range(SIG - 1, SIG + 7):
        r = go(w, broker, i)
    assert sorted(r.summary["released"]) == ["AAA (no data)", "CCC (no data)"]  # one evening
    t = by_key(w)
    assert t["AAA"].exit_session == t["CCC"].exit_session == DATES[SIG + 6]
    assert t["AAA"].id < t["CCC"].id
    assert [x.id for x in w.book.closed()] == [t["CCC"].id, t["AAA"].id]
    assert [x["series_key"] for x in w.view()["closed"]] == ["CCC", "AAA"]
