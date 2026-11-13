"""US-068: the owner's options rule, `microtrend`.

Entry at a 3-minute close where Supertrend(14, 1.7) of 3m, 5m, 10m, 15m, 30m, 60m and 4h newly
all point the same way (a call up, a put down); exit when 3 of the 7 point against the trade;
the 4-hour bars run 09:15 to 13:15 and 13:15 to the close; a timeframe with too few bars has
no reading and nothing fires. End to end through the desk and the real paper book.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desk_harness import new_desk, small_cfg  # noqa: E402

from onelot.clock import IST  # noqa: E402
from onelot.data.resample import resample_bars  # noqa: E402
from onelot.data.store import save_bars  # noqa: E402
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg, load_desk_config  # noqa: E402
from onelot.indicators import mtf  # noqa: E402
from onelot.paper.factory import build_paper_book  # noqa: E402
from onelot.store import init_database  # noqa: E402
from onelot.store.schema import paper_trades  # noqa: E402
from onelot.strategies import mtf_support as ms  # noqa: E402
from onelot.strategies.base import Context  # noqa: E402
from onelot.strategies.microtrend import MICRO_TIMEFRAMES  # noqa: E402
from onelot.strategies.registry import get_strategy  # noqa: E402

TFS = MICRO_TIMEFRAMES


def strategy(**kw):
    return get_strategy("microtrend", {"timeframe": "3m", **kw})


def frame(rows: list[dict[str, int]]) -> pd.DataFrame:
    """Feature rows as the strategy reads them: one direction per timeframe and decision."""
    idx = pd.date_range("2026-11-02 09:18", periods=len(rows), freq="3min", tz=IST)
    f = pd.DataFrame(index=idx)
    for tf in TFS:
        f[f"mtf_{tf}"] = [r.get(tf, r.get("all", 0)) for r in rows]
    f["mtf_up"] = sum((f[f"mtf_{tf}"] == 1).astype(int) for tf in TFS)
    f["mtf_down"] = sum((f[f"mtf_{tf}"] == -1).astype(int) for tf in TFS)
    f["mtf_trail_line"] = 100.0
    f["mtf_trail_atr"] = 1.0
    return f


def test_a_call_only_on_a_new_alignment_and_a_put_the_other_way():
    rows = [
        {"all": 1, "240m": 0},  # the 4h trend has no reading: nothing fires
        {"all": 1},  # all seven up, newly: a call
        {"all": 1},  # still aligned: not again
        {"all": 1, "3m": -1},  # broken
        {"all": 1},  # aligned again, newly: a call
        {"all": -1},  # all down, newly: a put
    ]
    out = strategy().raw_signals(frame(rows))
    assert out["long_entry"].tolist() == [False, True, False, False, True, False]
    assert out["short_entry"].tolist() == [False, False, False, False, False, True]
    assert out["long_stop"].iloc[1] == pytest.approx(100.0 - 3.0)  # the backstop
    assert out["short_stop"].iloc[5] == pytest.approx(100.0 + 3.0)


def test_the_exit_is_at_3_of_7_against_and_not_at_2():
    two = {"all": 1, "3m": -1, "5m": -1}
    three = {"all": 1, "3m": -1, "5m": -1, "10m": -1}
    out = strategy().raw_signals(frame([{"all": 1}, two, three]))
    assert out["long_exit"].tolist() == [False, False, True]
    puts = strategy().raw_signals(frame([{"all": -1}, {"all": -1, "3m": 1, "5m": 1, "4h": 1}]))
    assert puts["short_exit"].tolist() == [False, False]  # "4h" is not a column: 240m is
    puts = strategy().raw_signals(frame([{"all": -1}, {"all": -1, "3m": 1, "5m": 1, "240m": 1}]))
    assert puts["short_exit"].tolist() == [False, True]
    assert strategy(exit_count=2).raw_signals(frame([two]))["long_exit"].tolist() == [True]


def minutes(days: list[dt.date], seed: int = 3, drift: float = 0.0001) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = [
        t
        for d in days
        for t in pd.date_range(dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST), periods=375,
                               freq="1min")
    ]  # fmt: skip
    c = 25000 * np.exp(np.cumsum(rng.normal(drift, 0.0008, len(idx))))
    o = np.r_[25000.0, c[:-1]]
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998,
         "close": c, "volume": 0.0},
        index=pd.DatetimeIndex(idx),
    )  # fmt: skip


def weekdays(end: dt.date, n: int) -> list[dt.date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def test_the_4h_bars_run_09_15_to_13_15_and_13_15_to_the_close():
    days = weekdays(dt.date(2026, 11, 6), 2)
    bars = ms.rung_frames(minutes(days), "1m", None, TFS)["240m"]
    assert [t.strftime("%m-%d %H:%M") for t in bars.index] == [
        f"{d:%m-%d} {hm}" for d in days for hm in ("09:15", "13:15")
    ]
    first = minutes(days)
    am = first[(first.index.date == days[0]) & (first.index.time < dt.time(13, 15))]
    assert (
        bars["high"].iloc[0] == am["high"].max() and bars["close"].iloc[0] == am["close"].iloc[-1]
    )
    # a bar counts once its nominal end has passed: the afternoon one (13:15 + 4h = 17:15) is
    # first read by the next session
    closes = mtf.nominal_close_ns(pd.DatetimeIndex(bars.index), "240m")
    assert pd.Timestamp(closes[1], tz="UTC").tz_convert(IST).strftime("%H:%M") == "17:15"


def test_without_enough_4h_bars_there_is_no_reading_and_nothing_fires():
    s = strategy()
    for n, ready in ((20, False), (40, True)):  # 70 bars of 4h = 35 sessions
        b = minutes(weekdays(dt.date(2026, 11, 6), n), drift=0.0002)
        df = resample_bars(b, "3m")
        ctx = Context(symbol="NIFTY", extras={"minute_bars": b, "minute_resolution": "1m"})
        f = s.generate(df, ctx)
        assert bool((f["mtf_240m"] != 0).any()) is ready
        if not ready:
            assert not f["long_entry"].any() and not f["short_entry"].any()


def test_the_shipped_configuration_runs_two_rules():
    cfg = load_desk_config()
    assert {i.key for i in cfg.indices} == {"BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY",
                                            "SENSEX"}  # fmt: skip
    assert all([s.name for s in i.strategies] == ["microtrend"] for i in cfg.indices)
    assert len(cfg.stocks) == 18
    assert all([s.name for s in i.strategies] == ["pullback"] for i in cfg.stocks)
    assert cfg.primary_strategies() == ("microtrend",)  # the Lab's list (pullback is delivery)
    assert [s.strategy for s in cfg.primary_slots()] == ["pullback", "microtrend"]
    evidence = cfg.indices[0].strategies[0].params["evidence"]
    assert evidence.startswith("Backtest of this exact rule: 3,824 trades")


# ── end to end: the desk, the real paper book, one call that exits on the reversal count ────

# A random walk (seed 30) whose last session has a call aligned at the 12:12 bar and 3 of 7
# reversed at the 14:42 bar; 45 sessions, so the 4h trend is warm from the store's history.
SEED, SESSIONS = 30, 45
DAY = dt.date.today() - dt.timedelta(days=3)
while DAY.weekday() >= 5:
    DAY -= dt.timedelta(days=1)


def walk() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    days = weekdays(DAY, SESSIONS)
    idx = [
        t
        for d in days
        for t in pd.date_range(dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST), periods=375,
                               freq="1min")
    ]  # fmt: skip
    m = len(idx)
    drift = np.where((np.arange(m) // 375) % 9 < 6, 0.00012, -0.00006)
    c = 25000 * np.exp(np.cumsum(rng.normal(drift, 0.0008, m)))
    o = np.r_[25000.0, c[:-1]]
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998,
         "close": c, "volume": 0.0},
        index=pd.DatetimeIndex(idx),
    )  # fmt: skip


BARS = walk()


class Clock:
    now = dt.datetime.combine(DAY, dt.time(9, 0), tzinfo=IST)

    def __call__(self) -> dt.datetime:
        return Clock.now


class Source:
    name, delayed, resolution, primary, last_error = "dhan", False, "1m", None, None

    def __init__(self) -> None:
        self.served_by: dict[str, str] = {}

    def fetch(self, specs, days, since=None):
        live = BARS[BARS.index.date >= weekdays(DAY, 3)[0]]
        return {k: live[live.index + pd.Timedelta(minutes=1) <= Clock.now] for k in specs}


@pytest.fixture
def rig(cfg, tmp_path, monkeypatch):
    db = init_database(f"sqlite:///{tmp_path / 'mt.db'}")
    save_bars(db.engine, "NIFTY", "1m", BARS[BARS.index.date < DAY], "test")  # the store
    clock = Clock()
    monkeypatch.setattr("onelot.desk.desk.now_ist", clock)
    book = build_paper_book(cfg, db.engine, clock=clock)
    shipped = load_desk_config().indices[0].strategies[0]
    slot = StrategySlotCfg("microtrend", "3m", dict(shipped.params))
    desk_cfg = small_cfg(indices=(DeskInstrumentCfg("NIFTY", "NIFTY_OPT", "INDEX", (slot,)),),
                         stocks=())  # fmt: skip
    desk, notifier, _ = new_desk(cfg, db, desk_cfg=desk_cfg, paper=book)
    desk.source = Source()
    desk.fine_minutes = 1
    for t in desk.trackers.values():
        t.fine_minutes = 1
    # before the open the stored minute bars load on this thread (in market hours they load
    # in the background, which a test would race; that path has its own test)
    desk._load_minute_history(dt.datetime.combine(DAY, dt.time(8, 30), tzinfo=IST))
    return desk, notifier, book, db


def poll_through(desk, until: str, every: int = 3) -> None:
    start = dt.datetime.combine(DAY, dt.time(9, 18), tzinfo=IST)
    end = dt.datetime.combine(DAY, dt.time(*map(int, until.split(":"))), tzinfo=IST)
    t = start
    while t <= end:
        Clock.now = t
        desk.poll()
        t += dt.timedelta(minutes=every)


def test_a_call_opens_on_the_alignment_and_closes_on_the_reversal_count(rig):
    desk, notifier, book, db = rig
    poll_through(desk, "15:30")
    with db.engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(paper_trades)).mappings()]
    assert rows, "no paper trade opened"
    assert all(r["strategy"] == "microtrend" and r["status"] == "CLOSED" for r in rows)
    calls = [r for r in rows if r["direction"] == "long" and r["close_reason"] == "SIGNAL_EXIT"]
    assert calls, [(r["direction"], r["close_reason"]) for r in rows]
    for r in calls:  # filled at a bar at or after a 3-minute close, never before it
        at = pd.Timestamp(r["exit_ts"]).tz_convert(IST)
        assert (at.hour * 60 + at.minute - (9 * 60 + 15)) % 3 == 0
    sent = [n for n in notifier.sent if "microtrend" in n.title]
    assert sent and all("Not an edge: this is the paper test." in n.body for n in sent)
    card = next(d for d in desk.snapshot()["indices"] if d["key"] == "NIFTY")
    assert [r["timeframe"] for r in card["trend"]["rows"]][0] == "3m"
    assert card["trend"]["rows"][-1]["timeframe"] == "4h"


def test_the_session_end_still_closes_a_trade_the_count_never_ends(rig, monkeypatch):
    desk, notifier, book, db = rig
    monkeypatch.setattr(desk, "_paper_strategy_exit", lambda trade, now: None)
    # the book asks the desk for the strategy's exit; here the count never answers
    poll_through(desk, "15:30")
    with db.engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(paper_trades)).mappings()]
    assert rows and {r["close_reason"] for r in rows} <= {"SESSION_END", "STOP"}
    assert any(r["close_reason"] == "SESSION_END" for r in rows)


def test_the_card_counts_the_reversal_while_the_trade_is_open(rig):
    desk, notifier, book, db = rig
    poll_through(desk, "13:00")
    open_ = book.open_trades()
    if not open_:
        pytest.skip("no micro-trend trade open at 13:00 on this path")
    card = next(d for d in desk.snapshot()["indices"] if d["key"] == "NIFTY")
    rev = card["trend"]["reversal"]
    assert rev["of"] == 7 and rev["exit_at"] == 3 and 0 <= rev["reversed"] < 3


def test_in_market_hours_the_minute_history_loads_in_the_background(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "onelot.desk.desk.minute_history", lambda e, k, now: calls.append(k) or BARS.iloc[:10]
    )
    db = init_database(f"sqlite:///{tmp_path / 'bg.db'}")
    slot = StrategySlotCfg("microtrend", "3m", {})
    desk_cfg = small_cfg(indices=(DeskInstrumentCfg("NIFTY", "NIFTY_OPT", "INDEX", (slot,)),),
                         stocks=())  # fmt: skip
    desk, _, _ = new_desk(cfg, db, desk_cfg=desk_cfg)
    desk._load_minute_history(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST))
    assert desk._minute_loader.name == "minute-history"
    desk._minute_loader.join(5)
    assert calls == ["NIFTY"] and len(desk.trackers["NIFTY"].minute_history) == 10
