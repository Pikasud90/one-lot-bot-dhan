"""US-067 D8 and the lead's rulings 2, 3 and 5: the rule's daily bars, their freshness, the
session-complete test and the top-up (injected: no broker, no network)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delivery_helpers import at, bars_of, session_bars, weekdays  # noqa: E402

from onelot.clock import IST, SessionClock, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.data.hygiene import clean_intraday, consistent_daily  # noqa: E402
from onelot.data.store import save_bars  # noqa: E402
from onelot.delivery.data import RuleHistory, join_live, problem, session_complete  # noqa: E402
from onelot.delivery.runner import DeliveryDesk  # noqa: E402
from onelot.desk.config import DeliveryCfg, DeskConfig, DeskInstrumentCfg, StrategySlotCfg  # noqa: E402
from onelot.desk.history import rule_daily  # noqa: E402
from onelot.store import init_database  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CAL = TradingCalendar({dt.date(2026, 11, 10): "Diwali"}, verified=True)
CLOCK = SessionClock(SessionCfg(), CAL)
T1510 = dt.time(15, 10)
SETTLE = dt.timedelta(minutes=10)
DAY = dt.date(2026, 11, 13)  # a Friday


def complete(day, now, last, daily_only=False):
    return session_complete(day, now, last, CLOCK, CAL, T1510, SETTLE, daily_only)


# ── when a session is complete (ruling 2) ───────────────────────────────────────────────────


def test_an_fo_stock_whose_last_bar_is_15_14_is_complete_ten_minutes_after_the_close():
    assert not complete(DAY, at(DAY, "15:39"), dt.time(15, 14))
    assert complete(DAY, at(DAY, "15:40"), dt.time(15, 14))


def test_nifty_whose_last_bar_is_15_29_passes_the_same_test():
    assert complete(DAY, at(DAY, "15:40"), dt.time(15, 29))


def test_a_day_that_stops_at_14_00_is_not_complete_even_the_next_day():
    assert not complete(DAY, at(DAY, "16:00"), dt.time(14, 0))
    assert not complete(DAY, at(DAY + dt.timedelta(days=3), "10:00"), dt.time(14, 0))
    assert not complete(DAY, at(DAY, "16:00"), None)


def test_a_day_not_in_the_calendar_is_complete_the_next_day():
    muhurat = dt.date(2026, 11, 8)  # a Sunday session
    assert not complete(muhurat, at(muhurat, "23:00"), dt.time(19, 14))
    assert complete(muhurat, at(dt.date(2026, 11, 9), "00:01"), dt.time(19, 14))


def test_a_stored_daily_bar_without_minutes_is_a_whole_session_once_past():
    assert complete(DAY, at(DAY + dt.timedelta(days=1), "00:00"), None, daily_only=True)
    assert not complete(DAY, at(DAY, "20:00"), None, daily_only=True)


# ── the days (D8) ───────────────────────────────────────────────────────────────────────────


def daily(days: list[dt.date], base: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in days])
    c = [base + i for i in range(len(days))]
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)


def test_live_days_are_laid_on_the_store_except_a_first_live_day_the_store_has():
    stored_days = weekdays(dt.date(2026, 10, 1), 10)
    live = bars_of({stored_days[-1]: (1.0, 2.0), DAY: (500.0, 510.0)})
    live = live[live.index >= at(stored_days[-1], "12:00")]  # the first live day starts mid-session
    h = join_live("SBIN", daily(stored_days), {}, live)
    assert h.days.loc[stored_days[-1], "close"] == 109.0  # the store's whole day stands
    assert h.days.loc[DAY, "open"] == 500.0 and h.days.loc[DAY, "close"] == 510.0
    assert h.last_bar[DAY] == dt.time(15, 14) and h.stored_to == stored_days[-1]
    assert DAY not in h.daily_only and stored_days[0] in h.daily_only


def test_a_live_day_replaces_a_partial_stored_day():
    days = weekdays(dt.date(2026, 10, 1), 5)
    live = bars_of({days[-2]: (50.0, 60.0), days[-1]: (70.0, 80.0)})
    h = join_live("SBIN", daily(days), {}, live)
    assert h.days.loc[days[-1], "close"] == 80.0  # not the first live day: it replaces
    assert h.days.loc[days[-2], "close"] == 103.0  # the first live day: the store's stands


def hist(days: list[dt.date]) -> RuleHistory:
    return join_live("SBIN", daily(days), {}, None)


def test_problem_short_history_missing_session_and_no_bar_for_the_day():
    days = weekdays(dt.date(2025, 6, 2), 230)
    covers = TradingCalendar({dt.date(2025, 1, 1): "a", dt.date(2026, 1, 1): "b"}, verified=True)
    h = hist(days)
    assert problem(h, days[-1], covers) is None
    assert "221 needed" in problem(hist(days[-100:]), days[-1], CAL)
    holed = hist(days[:-5] + days[-4:])
    assert "missing" in problem(holed, days[-1], covers)
    assert problem(h, days[-1] + dt.timedelta(days=3), CAL).startswith("no daily bar")


def test_a_hole_in_a_year_the_calendar_does_not_cover_is_not_flagged():
    """config/nse_holidays.yaml lists 2026 on: 2025-12-25 (Christmas) is no hole."""
    days = [d for d in weekdays(dt.date(2025, 6, 2), 300) if d != dt.date(2025, 12, 25)]
    assert problem(hist(days), days[-1], CAL) is None


def test_a_calendar_holiday_is_not_a_missing_session():
    days = [d for d in weekdays(dt.date(2025, 12, 1), 240) if d != dt.date(2026, 11, 10)]
    assert problem(hist(days), days[-1], CAL) is None


def test_rule_daily_matches_the_research_construction_and_reports_last_bars(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 's.db'}")
    days = weekdays(dt.date(2026, 9, 1), 30)
    save_bars(db.engine, "SBIN", "1d", daily(days[:20]), "t")
    minutes = bars_of({d: (100.0 + i, 101.0 + i) for i, d in enumerate(days[15:])})
    save_bars(db.engine, "SBIN", "1m", minutes, "t")
    now = at(days[-1], "18:00")
    got, last = rule_daily(db.engine, "SBIN", now)
    want = consistent_daily(daily(days[:20]), clean_intraday("SBIN", minutes))
    assert list(got["close"]) == pytest.approx(list(want["close"]))
    assert last[days[-1]] == dt.time(15, 14) and days[0] not in last


# ── the top-up (ruling 3) ───────────────────────────────────────────────────────────────────


def delivery_desk(engine, calls, api=True, top_up=True) -> DeliveryDesk:
    slot = StrategySlotCfg("pullback", "1d", {}, True, "delivery")
    cfg = DeskConfig(
        stocks=(DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (slot,)),),
        delivery=DeliveryCfg(top_up=top_up),
    )
    return DeliveryDesk(
        engine, cfg, CostConfig.load(ROOT / "config" / "costs.yaml"), SessionCfg(), CAL,
        notifier=None, started_at=at(DAY, "08:00"), top_up=calls.append,
        data_api_active=lambda: api, background=False,
    )  # fmt: skip


@pytest.fixture
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'd.db'}")


def test_the_top_up_runs_after_hours_once_a_day_for_the_19_series(engine):
    calls: list = []
    d = delivery_desk(engine, calls)
    d.load_history(at(DAY, "16:00"))  # an empty store: behind
    d.maybe_top_up(at(DAY, "16:00"))
    assert calls == [["SBIN", "NIFTY"]]
    assert d._reload.is_set()  # the next poll reads the store again
    d.maybe_top_up(at(DAY, "18:00"))
    assert len(calls) == 1  # once a day


@pytest.mark.parametrize("hhmm", ["09:00", "11:00", "15:35"])
def test_the_top_up_never_runs_in_market_hours(engine, hhmm):
    calls: list = []
    d = delivery_desk(engine, calls)
    d.maybe_top_up(at(DAY, hhmm))
    assert calls == []


def test_the_top_up_needs_the_data_api_and_the_switch(engine):
    calls: list = []
    delivery_desk(engine, calls, api=False).maybe_top_up(at(DAY, "16:00"))
    delivery_desk(engine, calls, top_up=False).maybe_top_up(at(DAY, "16:00"))
    assert calls == []


def test_a_store_that_is_up_to_date_is_not_topped_up(engine, tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'u.db'}")
    for key in ("SBIN", "NIFTY"):
        save_bars(db.engine, key, "1m", session_bars(DAY, 1.0, 2.0), "t")
    calls: list = []
    d = delivery_desk(db.engine, calls)
    d.load_history(at(DAY, "16:00"))
    d.maybe_top_up(at(DAY, "16:00"))
    assert calls == []


def test_a_failing_top_up_is_logged_and_carries_on(engine, caplog):
    def boom(keys):
        raise ConnectionError("no network")

    d = delivery_desk(engine, [])
    d._top_up = boom
    d.maybe_top_up(at(DAY, "16:00"))
    assert "failed" in (d.top_up_note or "") and "top-up" in caplog.text
