"""US-099 review findings: the evening round's guards on prices and on the Data API.

- A history the broker re-based (a split) while a trade is open or pending: the trade is put on
  the new basis (no false STOP, no fill on another basis than its signal).
- A split or bonus the broker left UNADJUSTED (a raw fall beyond `JUMP_LIMIT`): no false
  pullback signal, and an open trade's exit is held for the owner rather than alerted.
- The Data API off at the evening: no round is closed on the stored bars; it runs when the API
  comes back the same evening.
- A young stock is not fetched again whole every evening; a wait for NIFTY is not a try.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from onelot.clock import IST
from onelot.data.store import save_bars
from onelot.delivery.money import rupees_to_paise
from onelot.delivery.wide import FULL_FROM, ROUND_TRIES

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
    nifty_of,
    stock,
    weekdays,
    wide_desk,
)

N = BASE + 60
DATES = weekdays(START, N)
SIG = BASE


def halved(f: pd.DataFrame) -> pd.DataFrame:
    """The broker's whole history after a 1:2 split: every price halved."""
    g = f.copy()
    g[["open", "high", "low", "close"]] = g[["open", "high", "low", "close"]] / 2
    return g


def desk(frames, rec=None, **kw):
    broker = Broker(frames)
    w = wide_desk(
        [stock(k) for k in frames], DATES, fetch=broker.fetch, data_api_active=lambda: True,
        notifier=rec, sleep=lambda s: None, **kw,
    )  # fmt: skip
    return w, broker


def go(w, broker, i, rec=None):
    broker.published = DATES[i]
    r = w.run_round(at(DATES[i]), DATES[i])
    w.tell(at(DATES[i], "18:05"))
    return r


def test_an_open_trade_across_a_rebased_history_is_rescaled_and_gets_no_false_stop():
    raw = frame(DATES, drop_then(SIG, 0.10, 0.001, N))
    rec = Recorder()
    w, broker = desk({"AAA": raw}, rec)
    for i in range(SIG - 2, SIG + 2):
        go(w, broker, i)
    (t,) = w.book.all()
    assert t.status == "OPEN" and t.entry_session == DATES[SIG + 1]
    booked, qty = t.entry_paise, t.qty
    broker.frames["AAA"] = halved(raw)  # the split: the whole history re-based
    r = go(w, broker, SIG + 2)
    (t,) = w.book.all()
    assert r.summary["refetched"] == ["AAA"] and r.summary["rebased"] == ["AAA"]
    assert (t.status, t.exit_reason) == ("OPEN", None)  # no false STOP
    assert t.entry_paise == round(booked * 0.5) and t.qty == 2 * qty
    assert abs(t.signal_close - raw["close"].iloc[SIG] / 2) < 1e-9
    assert "re-based" in (t.note or "")
    assert not any("SELL" in x for x in rec.titles())
    # the next rounds read it on the new basis; nothing is rescaled twice
    go(w, broker, SIG + 3)
    (t2,) = w.book.all()
    assert (t2.entry_paise, t2.qty, t2.status) == (t.entry_paise, t.qty, "OPEN")


def test_a_pending_entry_across_a_rebased_history_fills_on_the_basis_of_its_signal():
    raw = frame(DATES, drop_then(SIG, 0.10, 0.001, N))
    w, broker = desk({"AAA": raw})
    for i in range(SIG - 2, SIG + 1):
        go(w, broker, i)
    (t,) = w.book.all()
    assert t.status == "PENDING_ENTRY"
    qty = t.qty
    broker.frames["AAA"] = halved(raw)  # re-based between the signal and the fill
    go(w, broker, SIG + 1)
    (t,) = w.book.all()
    o = raw["open"].to_numpy(float)[SIG + 1] / 2
    assert t.status == "OPEN" and t.entry_paise == rupees_to_paise(o * 1.0003)
    assert abs(t.signal_close - raw["close"].iloc[SIG] / 2) < 1e-9  # the same basis
    assert t.qty == qty  # the alert's count: the owner bought that many at the new price


def test_an_unadjusted_bonus_raises_no_false_pullback_signal():
    c = closes([], N, 0.006)
    d = BASE + 10
    c[d:] *= 0.8  # an unadjusted 1:4 bonus: the raw price 20% lower overnight
    rec = Recorder()
    w, broker = desk({"BBB": frame(DATES, c)}, rec)
    for i in range(d - 2, d + 3):
        r = go(w, broker, i)
        if i == d:
            assert r.summary["jumps"] == ["BBB"]
    assert w.book.all() == [] and rec.sent == []
    reading = w.book.states()["BBB"]["reading"]
    assert "split or bonus" in reading["entry_blocked"]


def test_an_unadjusted_split_holds_an_open_trades_exit_for_review():
    c = drop_then(SIG, 0.10, 0.001, N)
    c[SIG + 5 :] *= 0.5  # a 1:1 split left unadjusted, five sessions after the signal
    rec = Recorder()
    w, broker = desk({"AAA": frame(DATES, c)}, rec)
    for i in range(SIG - 2, SIG + 8):
        r = go(w, broker, i)
        if i >= SIG + 5:
            assert r.summary["held"] == ["AAA"] and r.summary["exits"] == 0
    (t,) = w.book.all()
    assert t.status == "OPEN" and "exit held for review" in (t.note or "")
    assert [x for x in rec.titles() if "SELL" in x] == []


def test_a_real_rise_is_not_a_jump():
    c = drop_then(SIG, 0.10, 0.001, N)
    c[SIG - 10 :] *= 1.3  # a 30% rise ten sessions before the drop: a pullback all the same
    w, broker = desk({"AAA": frame(DATES, c)})
    for i in range(SIG - 2, SIG + 1):
        go(w, broker, i)
    assert [t.signal_date for t in w.book.all()] == [DATES[SIG]]


def test_with_the_data_api_off_the_round_waits_and_runs_when_it_returns():
    raw = frame(DATES, drop_then(SIG, 0.10, 0.001, N))
    api = {"on": False}
    rec = Recorder()
    broker = Broker({"AAA": raw})
    w = wide_desk(
        [stock("AAA")], DATES, fetch=broker.fetch, data_api_active=lambda: api["on"],
        notifier=rec, sleep=lambda s: None,
    )  # fmt: skip
    d = pd.DatetimeIndex(raw.index).tz_convert(IST).date
    save_bars(w.engine, "AAA", "1d", raw[d <= DATES[SIG - 1]], "dhan")  # as after the import
    w.maybe_round(at(DATES[SIG], "16:00"))
    assert w.book.round(DATES[SIG]) is None and broker.calls == []
    assert w.note and "Data API is off" in w.note
    assert w.run_round(at(DATES[SIG], "16:10"), DATES[SIG]).finished is False
    assert w.book.round(DATES[SIG]) is None
    api["on"], broker.published = True, DATES[SIG]
    w.maybe_round(at(DATES[SIG], "16:20"))  # the same evening, no 30-minute wait owed
    w.tell(at(DATES[SIG], "16:25"))
    (t,) = w.book.all()
    assert (t.signal_date, t.tier, t.late) == (DATES[SIG], "cap", False)
    assert w.book.round(DATES[SIG])["finished_at"] is not None and len(rec.sent) == 1


def test_a_young_stock_is_fetched_whole_once_then_only_its_last_bars():
    young = weekdays(DATES[N - 40], 40)
    f = frame(young, closes([], 40, 0.003))
    broker = Broker({"YYY": f})
    w = wide_desk(
        [stock("YYY")], DATES, fetch=broker.fetch, data_api_active=lambda: True,
        sleep=lambda s: None,
    )  # fmt: skip
    for k in (N - 3, N - 2, N - 1):
        broker.published = DATES[k]
        r = w.run_round(at(DATES[k]), DATES[k])
        assert r.summary["refetched"] == []
    starts = [c[1] for c in broker.calls]
    assert starts[0] == FULL_FROM and FULL_FROM not in starts[1:] and len(starts) == 3


def test_a_wait_for_nifty_is_not_a_try():
    raw = frame(DATES, drop_then(SIG, 0.10, 0.001, N))
    nifty = {"on": False}
    real = nifty_of(DATES)
    broker = Broker({"AAA": raw})
    broker.published = DATES[SIG]
    w = wide_desk(
        [stock("AAA")], DATES, fetch=broker.fetch, data_api_active=lambda: True,
        sleep=lambda s: None, nifty=lambda now: real(now) if nifty["on"] else None,
    )  # fmt: skip
    for k in range(ROUND_TRIES + 2):
        w.maybe_round(at(DATES[SIG], f"{16 + k // 2}:{30 * (k % 2):02d}"))
    row = w.book.round(DATES[SIG])
    assert row["finished_at"] is None and row["tries"] == 0
    assert "NIFTY" in (w.summary()["note"] or "")  # the page sees the note, not a stale cache
    nifty["on"] = True
    w.maybe_round(at(DATES[SIG], "20:00"))
    (t,) = w.book.all()
    assert (t.tier, t.late) == ("cap", False)
    assert w.book.round(DATES[SIG])["finished_at"] is not None
    assert w.summary()["note"] is None
