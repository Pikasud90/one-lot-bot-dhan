"""US-102: the wide list's evening round waits for the broker's daily bars of the session.

Measured on 25 Nov 2026: at 15:42 the broker's daily history held no bar of the day for any
stock. A check that finds no stock with the session's bar is not one of the `ROUND_TRIES`
tries, costs at most `PROBE` requests, and is made again every `ROUND_GAP`, through the night
and before 09:00 the next morning, until the bars are there; the session is then read on
time. Once one stock has the bar, tries are counted as before. No test reaches a broker.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from onelot.clock import IST
from onelot.data.store import save_bars
from onelot.delivery.wide import PROBE, ROUND_GAP, ROUND_TRIES
from onelot.store.schema import wide_rounds, wide_state, wide_trades

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wide_helpers import (  # noqa: E402
    BASE,
    START,
    Broker,
    Recorder,
    at,
    closes,
    drop_then,
    frame,
    stock,
    weekdays,
    wide_desk,
)

N = BASE + 60
DATES = weekdays(START, N)
SIG = BASE  # the session of the round: 2024-12-02, a Monday
DAY = DATES[SIG]
NEXT = DAY + dt.timedelta(days=1)  # the calendar day after (a trading day too)
KEYS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
NOT_YET = (
    "Dhan has not published the daily bars of 2024-12-02 yet (checked 15:42): "
    "the round looks again every 30 minutes"
)


def frames_of() -> dict[str, pd.DataFrame]:
    """AAA signals on the round's own session; the others rise steadily."""
    out = {k: frame(DATES, closes([], N, 0.003)) for k in KEYS}
    out["AAA"] = frame(DATES, drop_then(SIG, 0.10, 0.03, N))
    return out


def upto(f: pd.DataFrame, last: dt.date) -> pd.DataFrame:
    d = pd.DatetimeIndex(f.index).tz_convert(IST).date
    return f[d <= last]


def desk(frames, rec=None, stored_to: dt.date | None = DATES[SIG - 1]):
    """A desk on the wide list with each stock's history stored to `stored_to` (as after the
    import); the broker serves up to `published` (the session before: not published yet)."""
    broker = Broker(frames)
    broker.published = DATES[SIG - 1]
    w = wide_desk(
        [stock(k) for k in frames], DATES, fetch=broker.fetch, data_api_active=lambda: True,
        notifier=rec, sleep=lambda s: None,
    )  # fmt: skip
    if stored_to is not None:
        for k, f in frames.items():
            save_bars(w.engine, k, "1d", upto(f, stored_to), "dhan")
    return w, broker


def rows(w, table):
    with w.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings()]


def test_not_published_is_one_cheap_check_and_not_a_try():
    w, broker = desk(frames_of())
    before = rows(w, wide_state)
    w.maybe_round(at(DAY, "15:42"))
    row = w.book.round(DAY)
    assert len(broker.calls) <= PROBE and w.requests <= PROBE
    assert row["tries"] == 0 and row["finished_at"] is None
    assert rows(w, wide_state) == before and rows(w, wide_trades) == []
    assert row["summary"]["note"] == NOT_YET and w.note == NOT_YET
    assert w.summary()["note"] == NOT_YET
    assert w.summary()["last_round"]["note"] == NOT_YET
    assert set(row["summary"]["waiting"]) <= {c[0] for c in broker.calls}
    assert row["summary"]["errors"] == []
    r = w.run_round(at(DAY, "16:12"), DAY)
    assert r.finished is False


def test_the_long_wait_through_the_night_then_the_session_is_read_on_time():
    rec = Recorder()
    w, broker = desk(frames_of(), rec)
    times = [at(DAY, "15:42") + k * ROUND_GAP for k in range(ROUND_TRIES + 3)]
    times += [at(NEXT, "00:30"), at(NEXT, "08:00")]
    for now in times:
        n = len(broker.calls)
        w.maybe_round(now)
        w.tell(now)
        assert 0 < len(broker.calls) - n <= PROBE, now
        row = w.book.round(DAY)
        assert row["tries"] == 0 and row["finished_at"] is None, now
    assert w.book.all() == [] and rec.sent == []
    assert w.book.round(DAY)["started_at"].replace(tzinfo=dt.timezone.utc).astimezone(IST) == at(
        DAY, "15:42"
    )
    broker.published = DAY  # the bars of the day are there at last
    n = len(broker.calls)
    w.maybe_round(at(NEXT, "08:30"))
    w.tell(at(NEXT, "08:31"))
    assert sorted(c[0] for c in broker.calls[n:]) == sorted(KEYS)
    (t,) = w.book.all()
    assert (t.series_key, t.signal_date, t.tier, t.late) == ("AAA", DAY, "cap", False)
    assert [x.title for x in rec.sent] == [
        f"BUY {t.qty} shares of AAA Ltd at the next open · pullback 1d (delivery, wide list)"
    ]
    row = w.book.round(DAY)
    assert row["finished_at"] is not None and row["tries"] == 1
    assert row["summary"]["note"] is None and w.note is None and w.summary()["note"] is None


def test_a_second_check_within_the_gap_makes_no_request():
    w, broker = desk(frames_of())
    w.maybe_round(at(DAY, "15:42"))
    n = len(broker.calls)
    assert 0 < n <= PROBE
    w.maybe_round(at(DAY, "16:00"))
    w.maybe_round(at(DAY, "16:11"))
    assert len(broker.calls) == n and w.book.round(DAY)["tries"] == 0


def test_a_check_where_every_answer_fails_is_not_a_try_and_says_dhan_did_not_answer():
    w, broker = desk(frames_of())
    broker.fail = lambda key, start: True
    w.maybe_round(at(DAY, "15:42"))
    row = w.book.round(DAY)
    assert len(broker.calls) == PROBE
    assert row["tries"] == 0 and row["finished_at"] is None
    errors = row["summary"]["errors"]
    assert len(errors) == PROBE and all("ConnectionError" in e for e in errors)
    note = (
        "Dhan did not answer (5 requests failed, checked 15:42): "
        "the round tries again every 30 minutes"
    )
    assert row["summary"]["note"] == note and w.note == note
    assert rows(w, wide_trades) == []


def partly() -> dict[str, pd.DataFrame]:
    """CCC (third in the list) has the session's bar; the others end the session before."""
    out = frames_of()
    for k in out:
        if k != "CCC":
            out[k] = upto(out[k], DATES[SIG - 1])
    return out


def test_partly_there_every_stock_is_asked_and_the_tries_count():
    w, broker = desk(partly())
    broker.published = DAY
    w.maybe_round(at(DAY, "15:42"))
    assert sorted(c[0] for c in broker.calls) == sorted(KEYS)
    row = w.book.round(DAY)
    assert row["tries"] == 1 and row["finished_at"] is None
    assert row["summary"]["waiting"] == sorted(k for k in KEYS if k != "CCC")
    for k in range(1, ROUND_TRIES):
        w.maybe_round(at(DAY, "15:42") + k * ROUND_GAP)
    row = w.book.round(DAY)
    assert row["tries"] == ROUND_TRIES and row["finished_at"] is not None


def test_a_resumed_round_asks_only_the_missing_stocks_with_no_early_stop():
    w, broker = desk(partly())
    broker.published = DAY
    w.maybe_round(at(DAY, "15:42"))
    n = len(broker.calls)
    w.maybe_round(at(DAY, "16:12"))
    asked = [c[0] for c in broker.calls[n:]]
    assert asked == [k for k in KEYS if k != "CCC"] and len(asked) > PROBE
    assert w.book.round(DAY)["tries"] == 2


def test_a_first_round_while_not_published_records_nothing_then_reads_the_newest_session():
    frames = frames_of()
    frames["BBB"] = frame(DATES, drop_then(SIG - 1, 0.10, 0.03, N))  # a signal the day before
    rec = Recorder()
    w, broker = desk(frames, rec)
    w.maybe_round(at(DAY, "15:42"))
    w.tell(at(DAY, "15:43"))
    assert rows(w, wide_trades) == [] and rec.sent == []
    assert all(r.get("last_session") is None for r in rows(w, wide_state))
    broker.published = DAY
    w.maybe_round(at(DAY, "18:00"))
    w.tell(at(DAY, "18:01"))
    trades = w.book.all()
    assert [(t.series_key, t.signal_date, t.late) for t in trades] == [("AAA", DAY, False)]
    assert len(rec.sent) == 1


def test_when_the_bars_never_come_the_next_session_reads_the_old_one_late():
    rec = Recorder()
    frames = frames_of()
    w, broker = desk(frames, rec, stored_to=None)
    for i in (SIG - 2, SIG - 1):
        broker.published = DATES[i]
        w.maybe_round(at(DATES[i], "18:00"))
        w.tell(at(DATES[i], "18:05"))
    assert w.book.all() == []
    for hhmm in ("15:42", "18:00", "21:00"):
        w.maybe_round(at(DAY, hhmm))
    w.maybe_round(at(NEXT, "08:00"))
    assert w.book.all() == []
    broker.published = DATES[SIG + 1]
    w.maybe_round(at(DATES[SIG + 1], "18:00"))
    w.tell(at(DATES[SIG + 1], "18:05"))
    (t,) = w.book.all()
    assert (t.series_key, t.signal_date, t.tier, t.late) == ("AAA", DAY, "test", True)
    assert rec.sent == []
    old = w.book.round(DAY)
    assert old["tries"] == 0 and old["finished_at"] is None
    new = w.book.round(DATES[SIG + 1])
    assert new["finished_at"] is not None and new["tries"] == 1
    assert {r["session_date"] for r in rows(w, wide_rounds)} >= {
        DAY.isoformat(),
        DATES[SIG + 1].isoformat(),
    }
