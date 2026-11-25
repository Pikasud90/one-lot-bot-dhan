"""US-099 acceptance 3, 4 and 9: the evening round of the wide list on daily bars.

Through the product's own path (`WideDesk.maybe_round` with a fake broker and a store on a
throw-away database): a signal becomes a pending trade, fills at the next session's open in the
next round, and exits by each of the three exits; a round run twice the same evening changes
nothing; a restart between rounds continues. A history the broker re-based is fetched again
whole and raises no false signal; a failed refetch marks the stock stale and it is not read.
The round never runs between 09:00 and 15:35, and no test reaches a broker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, func, select

from onelot.delivery.money import rupees_to_paise
from onelot.delivery.rule import PullbackParams, entry_reading, quantity
from onelot.delivery.wide import FULL_FROM, REQUEST_GAP_S, StockDays
from onelot.store.schema import METADATA, wide_rounds, wide_state, wide_trades

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wide_helpers import (  # noqa: E402
    BASE,
    START,
    Broker,
    FakeClock,
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
SIG = BASE  # the drop session: the signal on its close


def store(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    METADATA.create_all(eng)
    return eng


def three_exits() -> dict[str, pd.DataFrame]:
    return {
        "AAA": frame(DATES, drop_then(SIG, 0.10, 0.03, N)),  # a rebound: above SMA20
        "BBB": frame(DATES, drop_then(SIG, 0.10, -0.002, N)),  # a slow slide: 30 sessions
        "CCC": frame(DATES, drop_then(SIG, 0.10, -0.03, N)),  # a fall: the 15% stop
    }


def rows(engine, table):
    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(select(table).order_by(*table.primary_key)).mappings()
        ]


def test_a_signal_fills_next_session_and_exits_by_each_of_the_three_exits(tmp_path):
    frames = three_exits()
    eng = store(tmp_path)
    broker, rec, clock = Broker(frames), Recorder(), FakeClock()
    broker.clock = clock

    def desk():  # a fresh desk on the same store: a restart
        return wide_desk(
            [stock(k) for k in frames], DATES, engine=eng, fetch=broker.fetch,
            data_api_active=lambda: True, notifier=rec, sleep=clock.sleep,
            monotonic=clock.monotonic,
        )  # fmt: skip

    w = desk()
    for i in range(SIG - 5, SIG + 1):
        broker.published = DATES[i]
        w.maybe_round(at(DATES[i]))
        w.tell(at(DATES[i], "18:05"))
    trades = {t.series_key: t for t in w.book.all()}
    assert set(trades) == {"AAA", "BBB", "CCC"}
    for k, t in trades.items():
        c = frames[k]["close"].to_numpy(float)
        assert (t.status, t.tier, t.late, t.signal_date) == (
            "PENDING_ENTRY",
            "cap",
            False,
            DATES[SIG],
        )
        assert t.qty == quantity(float(c[SIG]), 10000)
    assert sorted(rec.titles()) == sorted(
        f"BUY {trades[k].qty} shares of {k} Ltd at the next open · pullback 1d (delivery, wide list)"
        for k in frames
    )

    # the same evening again: nothing changes, nothing is fetched or told twice
    before = (rows(eng, wide_trades), rows(eng, wide_state), len(broker.calls), len(rec.sent))
    w.maybe_round(at(DATES[SIG], "19:00"))
    w.run_round(at(DATES[SIG], "19:30"), DATES[SIG])
    w.tell(at(DATES[SIG], "19:40"))
    assert (
        rows(eng, wide_trades),
        rows(eng, wide_state),
        len(broker.calls),
        len(rec.sent),
    ) == before

    # pacing: never two requests within half a second
    gaps = np.diff(broker.times)
    assert len(broker.times) == w.requests and (gaps >= REQUEST_GAP_S - 1e-9).all()

    # a restart, then the next session's round fills at that session's open
    w = desk()
    broker.published = DATES[SIG + 1]
    w.maybe_round(at(DATES[SIG + 1]))
    for k, t in {t.series_key: t for t in w.book.all()}.items():
        o = frames[k]["open"].to_numpy(float)
        assert (t.status, t.entry_session) == ("OPEN", DATES[SIG + 1])
        assert t.entry_paise == rupees_to_paise(o[SIG + 1] * 1.0003)

    for i in range(SIG + 2, N):
        if i == SIG + 15:
            w = desk()  # another restart between rounds
        broker.published = DATES[i]
        w.maybe_round(at(DATES[i]))
        w.tell(at(DATES[i], "18:05"))
    done = {t.series_key: t for t in w.book.all()}
    assert {k: (t.status, t.exit_reason) for k, t in done.items()} == {
        "AAA": ("CLOSED", "SMA20"),
        "BBB": ("CLOSED", "TIME"),
        "CCC": ("CLOSED", "STOP"),
    }
    assert done["BBB"].sessions_held == 30 and done["BBB"].exit_signal_date == DATES[SIG + 30]
    for k, t in done.items():
        o = frames[k]["open"].to_numpy(float)
        x = DATES.index(t.exit_signal_date) + 1  # sold at the next session's open
        assert t.exit_session == DATES[x]
        assert t.exit_paise == rupees_to_paise(o[x] * 0.9997)
        assert t.net_paise is not None and t.charges_paise and t.dp_paise
    # each trade alerted once to enter and once to exit, nothing more
    assert len(rec.sent) == 6 and len({n.signal_id for n in rec.sent}) == 6
    assert sum("SELL" in n.title for n in rec.sent) == 3
    # one round row per session, each finished
    rounds = rows(eng, wide_rounds)
    assert len(rounds) == N - (SIG - 5) and all(r["finished_at"] is not None for r in rounds)


def rebased(f: pd.DataFrame, day_i: int) -> pd.DataFrame:
    """The broker's history after a 1:2 split on session `day_i`: every earlier price halved,
    and from that session on the half-scale continuation."""
    g = f.copy()
    g.iloc[:, :4] = g.iloc[:, :4].to_numpy() / 2
    return g


def test_a_rebased_history_is_fetched_again_whole_and_raises_no_false_signal(tmp_path):
    x = SIG  # the split session
    plain = frame(DATES, closes([], N, 0.01))  # steep enough that half of it stays above SMA200
    frames = {"AAA": frame(DATES, drop_then(x, 0.10, 0.03, N)), "EEE": plain, "FFF": plain.copy()}
    eng = store(tmp_path)
    broker, clock = Broker(dict(frames)), FakeClock()
    w = wide_desk(
        [stock(k) for k in frames], DATES, engine=eng, fetch=broker.fetch,
        data_api_active=lambda: True, sleep=clock.sleep, monotonic=clock.monotonic,
    )  # fmt: skip
    for i in range(x - 5, x):
        broker.published = DATES[i]
        w.maybe_round(at(DATES[i]))
    # the split: the broker now serves both histories re-based; FFF's whole refetch fails
    broker.frames["EEE"] = rebased(plain, x)
    broker.frames["FFF"] = rebased(plain, x)
    broker.fail = lambda key, start: key == "FFF" and start == FULL_FROM
    # without the check, the stored closes and the new half-price bar read as a 50% fall
    joined = np.r_[plain["close"].to_numpy(float)[:x], broker.frames["EEE"]["close"].iloc[x]]
    ok = np.ones(len(joined), dtype=bool)
    assert entry_reading(joined, ok, x, PullbackParams()).signal
    broker.published = DATES[x]
    r = w.run_round(at(DATES[x]), DATES[x])
    assert r.summary["refetched"] == ["EEE"] and r.summary["stale"] == ["FFF"]
    assert {t.series_key for t in w.book.all()} == {"AAA"}  # the others went on
    st = w.book.states()
    assert st["FFF"]["stale"] and st["FFF"]["last_session"] == DATES[x - 1].isoformat()
    assert not st["EEE"]["stale"] and st["EEE"]["last_session"] == DATES[x].isoformat()
    stored = w._stored("EEE")["close"].to_numpy(float)
    assert np.allclose(stored, broker.frames["EEE"]["close"].to_numpy(float)[: x + 1])
    # FFF stays stale and unread until a refetch works; then it is read, still no signal
    broker.fail = lambda key, start: False
    broker.published = DATES[x + 1]
    r = w.run_round(at(DATES[x + 1]), DATES[x + 1])
    st = w.book.states()
    assert r.summary["refetched"] == ["FFF"] and not st["FFF"]["stale"]
    assert st["FFF"]["last_session"] == DATES[x + 1].isoformat()
    assert {t.series_key for t in w.book.all()} == {"AAA"}


def test_the_round_never_runs_between_0900_and_1535_and_never_without_bars(tmp_path):
    frames = three_exits()
    eng = store(tmp_path)
    broker = Broker(frames)
    broker.published = DATES[SIG]
    w = wide_desk(
        [stock(k) for k in frames], DATES, engine=eng, fetch=broker.fetch,
        data_api_active=lambda: True, sleep=lambda s: None,
    )  # fmt: skip
    for hhmm in ("09:00", "10:00", "12:30", "15:30", "15:35"):
        w.maybe_round(at(DATES[SIG], hhmm))
    assert broker.calls == [] and rows(eng, wide_rounds) == []
    w.maybe_round(at(DATES[SIG], "15:36"))
    assert broker.calls and len(rows(eng, wide_rounds)) == 1
    # no broker (Data API off) and no stored daily bar of the list: no round at all
    eng2 = create_engine("sqlite://")
    METADATA.create_all(eng2)
    w2 = wide_desk([stock("AAA")], DATES, engine=eng2, fetch=broker.fetch)
    w2.maybe_round(at(DATES[SIG]))
    with eng2.connect() as conn:
        assert conn.execute(select(func.count()).select_from(wide_rounds)).scalar() == 0
    assert w2.note and "no Dhan data" in w2.note


def test_an_error_on_one_stock_is_noted_and_the_others_go_on(tmp_path):
    frames = three_exits()
    eng = store(tmp_path)
    broker = Broker(frames)
    w = wide_desk(
        [stock(k) for k in frames], DATES, engine=eng, fetch=broker.fetch,
        data_api_active=lambda: True, sleep=lambda s: None,
    )  # fmt: skip
    for i in range(SIG - 3, SIG):
        broker.published = DATES[i]
        w.run_round(at(DATES[i]), DATES[i])
    broker.fail = lambda key, start: key == "BBB"
    broker.published = DATES[SIG]
    r = w.run_round(at(DATES[SIG]), DATES[SIG])
    assert any(e.startswith("BBB: ConnectionError") for e in r.summary["errors"])
    assert {t.series_key for t in w.book.all()} == {"AAA", "CCC"}
    assert not r.finished and "BBB" in r.summary["waiting"]
    # the next try that evening resumes: only BBB is fetched again, and its signal is read
    broker.fail = lambda key, start: False
    n = len(broker.calls)
    r = w.run_round(at(DATES[SIG], "19:00"), DATES[SIG])
    assert [c[0] for c in broker.calls[n:]] == ["BBB"] and r.finished
    assert {t.series_key for t in w.book.all()} == {"AAA", "BBB", "CCC"}


def test_stock_days_reads_the_store_frame_as_the_desk_18_do():
    f = frame(DATES[:5], np.array([10.0, 11.0, -1.0, 12.0, 13.0]))
    sd = StockDays.of("ZZZ", f)
    # a bad price is left out (session 3's close, and session 4's open, which is that close)
    assert sd.dates == [DATES[0], DATES[1], DATES[4]] and set(sd.hist.bad) == {DATES[2], DATES[3]}
    assert sd.index_of(DATES[2]) is None and sd.last_at_or_before(DATES[3]) == 1


def test_a_resumed_round_keeps_what_its_earlier_try_found(tmp_path):
    frames = three_exits()
    eng = store(tmp_path)
    broker = Broker(frames)
    w = wide_desk(
        [stock(k) for k in frames], DATES, engine=eng, fetch=broker.fetch,
        data_api_active=lambda: True, sleep=lambda s: None,
    )  # fmt: skip
    broker.published = DATES[SIG - 1]
    w.run_round(at(DATES[SIG - 1]), DATES[SIG - 1])
    broker.fail = lambda key, start: key == "CCC"
    broker.published = DATES[SIG]
    first = w.run_round(at(DATES[SIG]), DATES[SIG]).summary
    assert (first["scanned"], first["signals"]) == (2, 2)
    broker.fail = lambda key, start: False
    again = w.run_round(at(DATES[SIG], "19:00"), DATES[SIG]).summary
    assert (again["scanned"], again["signals"], again["fetched"]) == (3, 3, 3)
    assert any(e.startswith("CCC") for e in again["errors"])
