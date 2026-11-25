"""US-099 acceptance 5 and 6: the two tiers and the alerts of the wide list.

Every signal is a paper trade (tier "all"); at most `cap_open` are open within the cap, the desk
18's active trades counted; the same day's signals take the free places desk group first, then
deepest first; a trade keeps its tier for life. Only a within-the-cap trade reaches the notifier,
once to enter and once to exit; a recorded one never does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wide_helpers import (  # noqa: E402
    BASE,
    START,
    Recorder,
    at,
    closes,
    frame,
    source_of,
    stock,
    weekdays,
    wide_desk,
)

N = BASE + 50
DATES = weekdays(START, N)
SIG = BASE


def run(w, first: int, last: int, rec: Recorder | None = None) -> None:
    for i in range(first, last):
        w.run_round(at(DATES[i]), DATES[i])
        w.tell(at(DATES[i], "18:05"))


def test_the_cap_takes_the_desk_group_first_then_the_deepest_and_records_the_rest():
    depths = {"D00": 0.085, **{f"N{i:02d}": 0.085 + 0.005 * i for i in range(1, 11)}}
    frames = {
        k: frame(DATES, closes([(SIG, -d), (SIG + 1, 0.03)], N)) for k, d in depths.items()
    }
    stocks = [stock("D00", "desk")] + [stock(k) for k in depths if k != "D00"]
    rec = Recorder()
    w = wide_desk(stocks, DATES, source=source_of(frames), cap_open=10, notifier=rec)
    run(w, SIG - 2, SIG + 1)
    trades = {t.series_key: t for t in w.book.all()}
    assert len(trades) == 11  # every signal is a trade of tier "all"
    capped = {k for k, t in trades.items() if t.tier == "cap"}
    assert capped == {"D00", *(f"N{i:02d}" for i in range(2, 11))}  # the desk one, then depth
    left = trades["N01"]
    assert left.tier == "test" and left.note == (
        "recorded for the test, not alerted: the cap of 10 is full"
    )
    entries = [n for n in rec.sent if n.payload["delivery"] == "entry"]
    assert sorted(n.payload["id"] for n in entries) == sorted(
        f"pbw-{k}-{DATES[SIG].isoformat()}-entry" for k in capped
    )
    # the round and the alerts again: nothing twice
    w.run_round(at(DATES[SIG], "20:00"), DATES[SIG])
    w.tell(at(DATES[SIG], "20:05"))
    assert len(rec.sent) == 10
    # exits: each capped trade is told once to sell; the recorded one never
    run(w, SIG + 1, N)
    done = {t.series_key: t for t in w.book.all()}
    assert all(t.status == "CLOSED" for t in done.values())
    exits = [n for n in rec.sent if n.payload["delivery"] == "exit"]
    assert sorted(n.payload["id"].split("-")[1] for n in exits) == sorted(capped)
    assert not any("N01" in n.payload["id"] for n in rec.sent)
    assert all("wide list" in n.title for n in rec.sent)
    score = w.book.score()
    assert score["all"]["trades"] == 11 and score["cap"]["trades"] == 10


def test_a_freed_place_goes_to_the_next_new_signal_never_to_an_old_recorded_trade():
    paths = {
        "XXX": [(SIG, -0.10), (SIG + 1, 0.03)],  # in at SIG + 1, out within a few sessions
        "YYY": [(SIG + 1, -0.10), (SIG + 2, -0.002)],  # recorded (cap full), open 30 sessions
        "ZZZ": [(SIG + 12, -0.10), (SIG + 13, -0.002)],  # after XXX closed: takes the place
        "WWW": [(SIG + 14, -0.10), (SIG + 15, -0.002)],  # the place is ZZZ's: recorded
    }
    frames = {k: frame(DATES, closes(p, N)) for k, p in paths.items()}
    rec = Recorder()
    w = wide_desk(
        [stock(k) for k in frames], DATES, source=source_of(frames), cap_open=1, notifier=rec
    )
    run(w, SIG - 2, N)
    t = {x.series_key: x for x in w.book.all()}
    assert t["XXX"].tier == "cap" and t["XXX"].status == "CLOSED"
    assert t["XXX"].exit_session < DATES[SIG + 12]
    assert t["YYY"].tier == "test"  # and it stayed so while the place was free
    assert t["ZZZ"].tier == "cap" and t["WWW"].tier == "test"
    assert {n.payload["id"].split("-")[1] for n in rec.sent} == {"XXX", "ZZZ"}


def test_the_desk_18s_active_trades_count_toward_the_cap():
    frames = {"AAA": frame(DATES, closes([(SIG, -0.10), (SIG + 1, 0.03)], N))}
    rec = Recorder()
    w = wide_desk(
        [stock("AAA")], DATES, source=source_of(frames), cap_open=2, notifier=rec,
        desk_active=lambda: 2,
    )  # fmt: skip
    run(w, SIG - 1, SIG + 1)
    (t,) = w.book.all()
    assert t.tier == "test" and "the cap of 2 is full" in (t.note or "") and rec.sent == []
    assert w.summary()["cap_used"] == 2 and w.summary()["desk_active"] == 2


def test_a_desk_count_that_cannot_be_read_counts_the_cap_as_full():
    frames = {"AAA": frame(DATES, closes([(SIG, -0.10), (SIG + 1, 0.03)], N))}

    def broken() -> int:
        raise RuntimeError("no table")

    rec = Recorder()
    w = wide_desk(
        [stock("AAA")], DATES, source=source_of(frames), notifier=rec, desk_active=broken
    )
    run(w, SIG - 1, SIG + 1)
    (t,) = w.book.all()
    assert t.tier == "test" and rec.sent == []


def test_a_signal_read_late_is_recorded_and_never_alerted():
    frames = {"AAA": frame(DATES, closes([(SIG, -0.10), (SIG + 1, 0.03)], N))}
    rec = Recorder()
    w = wide_desk([stock("AAA")], DATES, source=source_of(frames), notifier=rec)
    run(w, SIG - 3, SIG - 2)  # the last round before the signal
    run(w, SIG + 2, SIG + 3)  # the rounds of SIG and SIG + 1 did not run: a catch-up
    (t,) = w.book.all()
    assert t.signal_date == DATES[SIG] and t.late and t.tier == "test"
    assert t.entry_session == DATES[SIG + 1]  # the fill is still that session's open
    assert rec.sent == []


def test_an_alert_no_channel_took_is_told_again_later_and_then_once():
    frames = {"AAA": frame(DATES, closes([(SIG, -0.10), (SIG + 1, 0.03)], N))}

    class Refusing(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.refuse = True

        def send(self, n, dedup_key=None):  # noqa: ANN001, ANN201
            self.sent.append(n)
            return {"telegram": not self.refuse}

    rec = Refusing()
    w = wide_desk([stock("AAA")], DATES, source=source_of(frames), notifier=rec)
    w.run_round(at(DATES[SIG]), DATES[SIG])
    w.tell(at(DATES[SIG], "18:05"))
    w.tell(at(DATES[SIG], "18:06"))  # within RETELL: not again
    assert len(rec.sent) == 1
    rec.refuse = False
    w.tell(at(DATES[SIG], "18:11"))
    w.tell(at(DATES[SIG], "18:20"))
    assert len(rec.sent) == 2
