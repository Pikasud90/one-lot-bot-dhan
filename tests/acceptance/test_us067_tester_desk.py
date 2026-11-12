"""US-067 independent tests of the delivery desk: restart, late and missed, completeness, D15,
bad data. Drives `DeliveryDesk` (the product path) session by session on an in-memory or file
database with fake one-minute bars; no network, no clock but the one passed in."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "delivery"))
sys.path.insert(0, str(HERE.parent / "desk"))

from delivery_helpers import at, session_bars, weekdays  # noqa: E402
from desk_harness import small_cfg  # noqa: E402

from onelot.clock import IST, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.delivery.runner import DeliveryDesk  # noqa: E402
from onelot.desk.config import DeliveryCfg, DeskInstrumentCfg, StrategySlotCfg  # noqa: E402
from onelot.store.schema import delivery_evaluations, delivery_trades  # noqa: E402

ROOT = HERE.parents[1]
EVID = "Backtest: test line. Not proven: this is the paper test."
SLOT = StrategySlotCfg("pullback", "1d", {"unit_inr": 10000, "evidence": EVID}, True, "delivery")
CFG = small_cfg(
    indices=(),
    stocks=(
        DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (SLOT,)),
        DeskInstrumentCfg("ITC", "ITC", "STOCK", (SLOT,)),
    ),
    delivery=DeliveryCfg(top_up=False),
)
START = dt.date(2026, 1, 5)
ALLDAYS = weekdays(START, 600)
STORED = ALLDAYS[:230]
LIVE = ALLDAYS[230:246]
L = {i + 1: d for i, d in enumerate(LIVE)}
COSTS = CostConfig.load(ROOT / "config" / "costs.yaml")
CAL = TradingCalendar({}, verified=True)

# (open, close) of the live sessions; SBIN falls 10% on L3 (entry), stops out on L6
SBIN = {1: (215.2, 215.5), 2: (215.5, 216.0), 3: (215.0, 194.4), 4: (195.0, 190.0),
        5: (189.0, 185.0), 6: (180.0, 160.0), 7: (158.0, 160.0)}  # fmt: skip
# ITC falls 10% on L3, closes above SMA20 on L5
ITC = {1: (415.2, 415.5), 2: (415.5, 416.0), 3: (415.0, 374.4), 4: (375.0, 380.0),
       5: (381.0, 430.0), 6: (431.0, 431.0)}  # fmt: skip


def stored(base: float) -> pd.DataFrame:
    close = base + 0.5 * np.arange(len(STORED), dtype=float)
    idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in STORED])
    return pd.DataFrame(
        {"open": close - 0.2, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=idx,
    )


def live(path, last):
    flat = path[max(path)][1]
    return pd.concat([session_bars(d, *path.get(i, (flat, flat)), last=last) for i, d in L.items()])


FULL = {
    "SBIN": live(SBIN, "15:14"),
    "ITC": live(ITC, "15:14"),
    "NIFTY": live({i: (25000.0 + 10 * i, 25005.0 + 10 * i) for i in L}, "15:29"),
}
STORE = {"SBIN": stored(100.0), "ITC": stored(300.0), "NIFTY": stored(20000.0)}


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {}


class World:
    def __init__(self, tmp_path, full=None, store=None, cal=CAL):
        self.cal = cal
        self.engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
        self.notifier = Notifier()
        self.full = dict(full or FULL)
        self.store = dict(store or STORE)
        self.desk = None

    def start(self, when):
        self.desk = DeliveryDesk(
            self.engine, CFG, COSTS, SessionCfg(), self.cal, self.notifier, when, background=False
        )
        for k, v in self.store.items():
            self.desk._stored[k] = (v, {})
        self.desk._history_day = when.date()
        return self.desk

    def poll(self, when):
        def bars(k):
            f = self.full.get(k)
            return None if f is None else f[f.index + pd.Timedelta(minutes=1) <= when]

        self.desk.on_poll(when, bars, lambda k: 100.0)
        return self.desk

    def trades(self):
        return {t.series_key: t for t in self.desk.book._select()}

    def counts(self):
        with self.engine.connect() as c:
            t = c.execute(select(func.count()).select_from(delivery_trades)).scalar()
            ids = c.execute(
                select(delivery_trades.c.series_key, delivery_trades.c.signal_date)
            ).all()
            e = c.execute(select(func.count()).select_from(delivery_evaluations)).scalar()
        assert len(ids) == len(set(ids))
        return t, e, len(self.notifier.sent)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def jsonable(desk):
    json.dumps(desk.view(), allow_nan=False)
    json.dumps({k: desk.card(k) for k in desk.stocks}, allow_nan=False)


def test_poll_twice_in_the_same_minute_and_rebuild_in_every_state(w):
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.poll(at(L[3], "15:45"))
    w.poll(at(L[3], "15:45"))
    assert w.counts()[0] == 2 and len(w.notifier.sent) == 2
    snap = w.counts()
    assert {t.status for t in w.trades().values()} == {"PENDING_ENTRY"}
    jsonable(w.desk)
    # state 1: alert pending: rebuild twice, poll the same minute
    for _ in range(2):
        w.start(at(L[3], "15:50"))
        w.poll(at(L[3], "15:50"))
        w.poll(at(L[3], "15:50"))
    assert w.counts() == snap
    # state 2: trade open (L4)
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    w.poll(at(L[4], "09:20"))
    assert {t.status for t in w.trades().values()} == {"OPEN"}
    entry = {k: t.entry_paise for k, t in w.trades().items()}
    w.poll(at(L[4], "15:45"))
    snap = w.counts()
    jsonable(w.desk)
    for _ in range(2):
        w.start(at(L[5], "09:00"))
        w.poll(at(L[5], "09:10"))
        w.poll(at(L[5], "09:10"))
    assert w.counts()[:2] == (2, snap[1] + 0) or w.counts()[0] == 2
    assert {k: t.entry_paise for k, t in w.trades().items()} == entry
    # state 3: exit pending (ITC after L5 close)
    w.poll(at(L[5], "15:45"))
    assert w.trades()["ITC"].status == "EXIT_PENDING"
    n = w.counts()
    for _ in range(2):
        w.start(at(L[5], "16:00"))
        w.poll(at(L[5], "16:00"))
        w.poll(at(L[5], "16:00"))
    assert w.counts() == n
    jsonable(w.desk)
    # state 4: closed
    w.start(at(L[6], "09:00"))
    w.poll(at(L[6], "09:30"))
    w.poll(at(L[6], "09:30"))
    itc = w.trades()["ITC"]
    assert itc.status == "CLOSED" and itc.dp_paise == 1475
    exit_paise = itc.exit_paise
    for _ in range(2):
        w.start(at(L[6], "10:00"))
        w.poll(at(L[6], "10:00"))
    assert w.trades()["ITC"].exit_paise == exit_paise and w.trades()["ITC"].dp_paise == 1475
    titles = [x.title.split()[0] for x in w.notifier.sent]
    assert titles.count("BUY") == 2 and titles.count("SELL") == 1
    jsonable(w.desk)


def test_entry_alert_is_pending_not_ten_minutes(w):
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert all(n.ttl_seconds > 12 * 3600 for n in w.notifier.sent)
    assert all(EVID in n.body for n in w.notifier.sent)


def test_late_fill_when_the_desk_starts_at_10_00_on_the_entry_session(w):
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "10:00"))
    w.poll(at(L[4], "10:01"))
    t = w.trades()["SBIN"]
    assert t.status == "OPEN" and t.entry_late and t.entry_ts == at(L[4], "10:00")
    bar = FULL["SBIN"].loc[at(L[4], "10:00")]
    assert t.entry_paise == round(bar["open"] * 1.0003 * 100)


def test_whole_entry_session_missed_has_no_trade_and_no_charge(w):
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[5], "09:00"))
    w.poll(at(L[5], "09:30"))
    for t in w.trades().values():
        assert t.status == "MISSED" and t.entry_paise is None
        assert t.charges_paise is None and t.dp_paise is None and t.net_paise is None
        assert "not running" in t.note
    assert w.desk.book.open_count() == 0
    w.poll(at(L[5], "09:31"))
    assert {t.status for t in w.trades().values()} == {"MISSED"}


def test_late_exit_when_down_at_the_exit_open(w):
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    w.poll(at(L[4], "15:45"))
    w.poll(at(L[5], "15:45"))  # ITC exit signal
    w.start(at(L[6], "11:00"))
    w.poll(at(L[6], "11:05"))
    t = w.trades()["ITC"]
    assert t.status == "CLOSED" and t.exit_late and t.exit_ts == at(L[6], "11:00")
    assert t.exit_paise == round(FULL["ITC"].loc[at(L[6], "11:00")]["open"] * 0.9997 * 100)


def test_desk_down_over_the_exit_signal_and_several_sessions(w):
    """Trade open; desk down from L5 to L8 inclusive; the exit condition holds on the close of
    L5 (the first missed session). At restart: one exit alert for L5, a late sale."""
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    w.poll(at(L[4], "15:45"))
    assert w.trades()["ITC"].status == "OPEN"
    w.start(at(L[9], "10:00"))
    w.poll(at(L[9], "10:05"))
    w.poll(at(L[9], "10:05"))
    itc = w.trades()["ITC"]
    assert itc.exit_signal_date == L[5] and itc.exit_reason == "SMA20"
    assert itc.status == "CLOSED" and itc.exit_late
    assert [n.title.split()[0] for n in w.notifier.sent].count("SELL") >= 1
    sells = [n for n in w.notifier.sent if n.title.startswith("SELL") and "ITC" in n.title]
    assert len(sells) == 1
    sbin = w.trades()["SBIN"]
    assert sbin.exit_signal_date == L[6] and sbin.exit_reason == "STOP"
    t, e, s = w.counts()
    w.start(at(L[9], "10:10"))
    w.poll(at(L[9], "10:10"))
    assert w.counts() == (t, e, s)


def test_completeness_at_15_40_and_not_before(w):
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:39"))
    assert w.counts()[0] == 0
    w.poll(at(L[3], "15:40"))  # 10 minutes after 15:30: complete, stock's last bar is 15:14
    assert w.counts()[0] == 2


def test_a_day_that_stops_at_14_00_raises_nothing_and_is_not_an_error(tmp_path):
    full = dict(FULL)
    f = full["SBIN"]
    full["SBIN"] = f[~((f.index.date == L[3]) & (f.index.time > dt.time(14, 0)))]
    w = World(tmp_path, full)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert "SBIN" not in w.trades() and "ITC" in w.trades()
    assert w.desk.errors == 0 and "incomplete" in w.desk.notes["SBIN"]
    jsonable(w.desk)
    w.poll(at(L[4], "09:30"))  # SBIN's L4 does not get read past the incomplete day
    assert "SBIN" not in w.trades()


def test_weekend_and_holiday_raise_nothing(w):
    fri = next(i for i in range(3, 13) if L[i].weekday() == 4)
    w.start(at(L[1], "08:00"))
    for i in range(1, fri + 1):
        w.poll(at(L[i], "09:20"))
        w.poll(at(L[i], "15:45"))
    before = w.counts()
    for day in (L[fri] + dt.timedelta(days=1), L[fri] + dt.timedelta(days=2)):
        for hhmm in ("00:30", "10:00", "15:45", "23:00"):
            w.poll(at(day, hhmm))
    assert w.counts() == before and w.desk.errors == 0
    jsonable(w.desk)


def test_special_weekend_session_is_complete_the_next_day_and_fills_on_monday(tmp_path):
    """Budget Sunday 2026-02-01 (a real special session in the hygiene list): a 10% fall
    on it signals only once the day after has begun; the fill is Monday's open."""
    hol = dt.date(2026, 1, 26)
    pre = [d for d in weekdays(dt.date(2025, 1, 1), 700) if d <= dt.date(2026, 1, 28) and d != hol][
        -230:
    ]
    sun, thu, fri, mon = (
        dt.date(2026, 2, 1),
        dt.date(2026, 1, 29),
        dt.date(2026, 1, 30),
        dt.date(2026, 2, 2),
    )

    def st(base):
        close = base + 0.5 * np.arange(len(pre), dtype=float)
        idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in pre])
        return pd.DataFrame(
            {
                "open": close - 0.2,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1e6,
            },
            index=idx,
        )

    full = {}
    for k, last, base in (
        ("SBIN", "15:14", 216.0),
        ("ITC", "15:14", 416.0),
        ("NIFTY", "15:29", 25010.0),
    ):
        drop = 0.9 if k != "NIFTY" else 1.0
        full[k] = pd.concat(
            [
                session_bars(thu, base, base, last),
                session_bars(fri, base, base, last),
                session_bars(sun, base, base * drop, last),
                session_bars(mon, base * drop, base * drop, last),
            ]
        )
    store = {"SBIN": st(100.0), "ITC": st(300.0), "NIFTY": st(20000.0)}
    w = World(tmp_path, full, store, cal=TradingCalendar({hol: "Republic Day"}, verified=True))
    w.start(at(thu, "08:00"))
    w.poll(at(fri, "15:45"))
    base_n = w.counts()
    w.poll(at(sun, "15:45"))
    assert w.counts() == base_n  # a special session is not complete on its own day
    w.poll(at(mon, "08:00"))
    assert w.counts()[0] == 2 and len(w.notifier.sent) == 2, w.desk.notes
    w.poll(at(mon, "09:20"))
    assert {t.status for t in w.trades().values()} == {"OPEN"}
    assert all(t.entry_session == mon for t in w.trades().values())


def test_d15_gap_flags_pending_and_open_and_nothing_else(tmp_path):
    full = dict(FULL)
    f = full["SBIN"].copy()
    d4 = f.index.date == L[4]
    f.loc[d4, ["open", "high", "low", "close"]] *= 0.7  # 30% below L3's close at L4 open
    full["SBIN"] = f
    g = full["ITC"].copy()
    d6 = g.index.date == L[6]
    g.loc[d6, ["open", "high", "low", "close"]] *= 1.5
    full["ITC"] = g
    w = World(tmp_path, full)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    # pending (gap not yet seen): no warning
    assert all(t.warning is None for t in w.trades().values())
    w.poll(at(L[4], "09:20"))
    sbin = w.trades()["SBIN"]
    assert sbin.warning == f"possible corporate action on {L[4].isoformat()}: check the figures"
    assert sbin.status == "OPEN" and sbin.qty == 51 and sbin.exit_signal_date is None
    assert w.trades()["ITC"].warning is None
    w.poll(at(L[4], "15:45"))
    w.poll(at(L[5], "15:45"))  # ITC exit signal; exit open L6 is +50%
    w.poll(at(L[6], "09:20"))
    itc = w.trades()["ITC"]
    assert itc.status == "CLOSED"  # an exit still happens: nothing else changes
    assert itc.warning is None or "corporate action" in itc.warning


def test_d15_exactly_20_percent_is_not_flagged_and_21_is(tmp_path):
    for factor, want in ((0.80, False), (0.79, True), (1.20, False), (1.21, True)):
        full = dict(FULL)
        f = full["SBIN"].copy()
        prev_close = f[f.index.date == L[3]]["close"].iloc[-1]
        d4 = f.index.date == L[4]
        scale = prev_close * factor / f[d4]["open"].iloc[0]
        f.loc[d4, ["open", "high", "low", "close"]] *= scale
        full["SBIN"] = f
        (tmp_path / str(factor)).mkdir()
        w = World(tmp_path / str(factor), full)
        w.start(at(L[3], "15:00"))
        w.poll(at(L[3], "15:45"))
        w.poll(at(L[4], "09:20"))
        assert (w.trades()["SBIN"].warning is not None) == want, factor


def test_bad_data_never_raises(tmp_path):
    # no bars for a stock at all, NaN prices, unsorted and duplicated bars, history ten sessions old
    full = dict(FULL)
    full.pop("ITC")
    f = full["SBIN"].copy()
    f.iloc[100, f.columns.get_loc("close")] = np.nan
    f = pd.concat([f.iloc[200:], f.iloc[:200], f.iloc[300:320]])  # unsorted and duplicated
    full["SBIN"] = f
    st = dict(STORE)
    st["ITC"] = st["ITC"].iloc[:-10]  # ends ten sessions ago
    dup = pd.concat([st["SBIN"], st["SBIN"].iloc[-5:]])
    st["SBIN"] = dup.sort_index()
    w = World(tmp_path, full, st)
    w.start(at(L[3], "15:00"))
    for i in (1, 2, 3, 4, 5):
        w.poll(at(L[i], "15:45"))
    jsonable(w.desk)
    assert w.desk.errors == 0
    assert "ITC" not in w.trades()


def test_a_store_that_ends_ten_sessions_ago_is_noted_and_raises_nothing(tmp_path):
    st = dict(STORE)
    for k in ("SBIN", "ITC", "NIFTY"):
        st[k] = st[k].iloc[:-10]
    w = World(tmp_path, store=st, cal=TradingCalendar({dt.date(2026, 1, 10): "x"}, verified=True))
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert w.counts()[0] == 0 and w.desk.notes
    assert "missing" in w.desk.notes["SBIN"]
    jsonable(w.desk)


class FlakyNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.fail = 3

    def send(self, n, dedup_key=None):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("telegram down")
        return super().send(n, dedup_key)


def test_a_failing_notifier_is_retried_without_duplicates(tmp_path):
    w = World(tmp_path)
    w.notifier = FlakyNotifier()
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))  # 2 sends fail
    w.poll(at(L[3], "15:46"))  # 1 more fails, one succeeds
    w.poll(at(L[3], "15:47"))
    w.poll(at(L[3], "15:47"))
    assert sorted(n.title.split()[4] for n in w.notifier.sent) == ["ITC", "SBIN"]
    assert w.desk.errors == 3 and w.counts()[0] == 2
